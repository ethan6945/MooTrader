"""Whether this installation may trade at all — trial, licence, or neither.

WHERE THE CHECK LIVES

  In order_gate.require(), which is the same choke point the order capability
  and the broker lease already use. Not at startup, and not per call site.

  The distinction order_gate draws between RUNNING and TRADING is exactly the
  product boundary: an unlicensed copy starts, connects, scores candidates,
  backtests and shows every panel. What it will not do is reach the broker's
  order book. So the trial does not cripple the thing being evaluated, and the
  expiry cannot half-execute a strategy — the gate shuts between decisions, not
  inside one.

WHY SIGNATURES AND NOT A DECODER

  The instinct is to encrypt the licence and put the key in the app. That key
  is then in every copy: extract it once and you can mint licences forever, and
  the generator is worthless. Ed25519 inverts it — the private key never leaves
  the issuer, the build carries only the public half, and a copy of the public
  key buys an attacker nothing, because it verifies and cannot sign.

WHAT THIS DOES NOT DO

  Stop a determined user. The build is frozen Python: it can be unpacked, the
  bytecode decompiled, and the call below patched to return True. That is true
  of every client-side check in any language and it is not worth pretending
  otherwise. This raises sharing from "send them the folder" to "decompile and
  patch it", which is the honest goal. Revocation and activation counting are
  what the online step is for; neither is possible offline.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import platform
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Public half of the issuer's Ed25519 key. Safe to publish: it verifies and
# cannot sign. The private half lives only on the issuer's machine.
SIGNING_PUBLIC_KEY = bytes.fromhex(
    "b7a1ce641ab856a95fc00b93e435708a1db9db386a84b41d18455313d0ebebb3")

TRIAL_DAYS = 30
_PREFIX = "MT1"

# Where a first activation phones home. Empty disables the online step entirely,
# which is the right setting for a build that must work air-gapped.
def _server() -> str:
    import os
    from .config import ROOT                                   # noqa: F401
    return (os.getenv("MMT_LICENCE_SERVER", "") or "").strip().rstrip("/")


# How long an activated copy trades without being able to reach the server.
# Reaching it is not a condition of trading: the server going down must not stop
# a customer's bot mid-session, so an unreachable server FAILS OPEN and is
# retried later. Only an explicit "revoked" answer closes the gate. The window
# exists so a revoked licence cannot simply firewall the check away forever.
RECHECK_EVERY_S = 7 * 86400
RECHECK_GRACE_S = 30 * 86400


class NotLicensed(Exception):
    """Trading is not permitted: no licence, and the trial is over or broken."""


# ── machine identity ─────────────────────────────────────────────────────────

def _raw_machine_id() -> str:
    """A stable per-machine string, best effort, per platform.

    Best effort is the operative phrase. A licence binds to the hash of this;
    if a platform gives nothing stable the licence still verifies, it just
    stops being machine-bound — which is a weaker licence, not a broken one.
    """
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        elif sysname == "Windows":
            out = subprocess.run(
                ["reg", "query",
                 r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"],
                capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "MachineGuid" in line:
                    return line.split()[-1]
        else:
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                try:
                    return Path(p).read_text().strip()
                except OSError:
                    continue
    except (OSError, subprocess.SubprocessError, IndexError) as e:
        log.warning("machine id unavailable (%s) — licence will not be bound", e)
    return ""


def machine_id() -> str:
    """The 16-hex fingerprint a licence is issued against. "" when unknown."""
    raw = _raw_machine_id()
    if not raw:
        return ""
    return hashlib.sha256(("mootrader:" + raw).encode()).hexdigest()[:16]


# ── licence keys ─────────────────────────────────────────────────────────────

def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_signature_only(key: str) -> dict | None:
    """Payload of a licence whose signature and expiry are good, else None.

    Deliberately does NOT check the machine binding, because the two callers
    that need this are not the machine the licence is for: the activation
    service, which must validate a licence it will never run on, and any tool
    that inspects a key. verify() is what an installation asks about itself.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        from cryptography.exceptions import InvalidSignature
    except ImportError:                                   # pragma: no cover
        log.error("cryptography missing — cannot verify a licence")
        return None

    parts = (key or "").strip().split(".")
    if len(parts) != 3 or parts[0] != _PREFIX:
        return None
    try:
        payload_raw, sig = _b64d(parts[1]), _b64d(parts[2])
        Ed25519PublicKey.from_public_bytes(SIGNING_PUBLIC_KEY).verify(
            sig, payload_raw)
        payload = json.loads(payload_raw)
    except (InvalidSignature, ValueError, json.JSONDecodeError):
        return None

    expires = payload.get("expires")
    if expires:
        try:
            if date.fromisoformat(expires) < datetime.now(timezone.utc).date():
                log.warning("licence %s expired on %s",
                            payload.get("id", "?"), expires)
                return None
        except ValueError:
            return None
    return payload


def verify(key: str) -> dict | None:
    """Return the payload of a licence valid FOR THIS MACHINE, else None.

    Signature and expiry, then the machine binding. A licence that fails any of
    them is not a licence here.
    """
    payload = verify_signature_only(key)
    if payload is None:
        return None
    bound = payload.get("machine") or ""
    if bound and bound != machine_id():
        log.warning("licence %s is bound to another machine",
                    payload.get("id", "?"))
        return None
    return payload


# ── stored state ─────────────────────────────────────────────────────────────

def _paths() -> tuple[Path, Path]:
    from .config import ROOT
    return ROOT / "data" / ".licence", ROOT / "data" / ".trial"


def _receipt_path() -> Path:
    from .config import ROOT
    return ROOT / "data" / ".activation"


def _seal(data: dict) -> str:
    """HMAC the trial record to the machine, so editing it is detectable.

    Not a secret — the key is derived from the machine id, which is not one
    either. It only means the file cannot be edited by hand without noticing,
    which is the difference between "delete a file to reset the trial" and
    "understand the format first".
    """
    body = json.dumps(data, sort_keys=True, separators=(",", ":"))
    mac = hmac.new(("mootrader-trial:" + machine_id()).encode(),
                   body.encode(), hashlib.sha256).hexdigest()[:32]
    return body + "|" + mac


def _unseal(text: str) -> dict | None:
    body, _, mac = (text or "").rpartition("|")
    if not body or not mac:
        return None
    want = hmac.new(("mootrader-trial:" + machine_id()).encode(),
                    body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(mac, want):
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _read_trial() -> dict:
    """The trial record, starting one if this is the first run.

    Clock rollback is treated as tampering rather than ignored: the record
    carries the latest time it has seen, and system time going backwards past a
    day's tolerance is the one way a wall clock check can be cheated for free.
    """
    _, trial_path = _paths()
    now = int(time.time())
    rec = None
    try:
        rec = _unseal(trial_path.read_text())
    except OSError:
        pass

    if rec is None:
        rec = {"first_run": now, "last_seen": now, "tampered": False}
    else:
        if now < rec.get("last_seen", now) - 86400:
            log.warning("system clock moved backwards — trial marked tampered")
            rec["tampered"] = True
        rec["last_seen"] = max(now, rec.get("last_seen", now))

    try:
        trial_path.parent.mkdir(parents=True, exist_ok=True)
        trial_path.write_text(_seal(rec))
    except OSError as e:
        log.warning("could not persist trial state: %s", e)
    return rec


def stored_licence() -> str:
    lic_path, _ = _paths()
    try:
        return lic_path.read_text().strip()
    except OSError:
        return ""


def _call_server(path: str, body: dict, timeout: float = 8.0) -> dict | None:
    """POST to the activation service. None means "could not reach it".

    None and a refusal are deliberately different values. Only the service
    saying no closes the gate; not being able to ask keeps it open, because a
    customer's bot must not stop trading because the issuer's host is down.
    """
    base = _server()
    if not base:
        return None
    import json as _json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        base + path, data=_json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return _json.loads(e.read().decode())
        except Exception:
            log.warning("activation service returned HTTP %s", e.code)
            return None
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("activation service unreachable: %s", e)
        return None


def _read_receipt() -> dict:
    try:
        return _unseal(_receipt_path().read_text()) or {}
    except OSError:
        return {}


def _write_receipt(rec: dict) -> None:
    try:
        pth = _receipt_path()
        pth.parent.mkdir(parents=True, exist_ok=True)
        pth.write_text(_seal(rec))
    except OSError as e:
        log.warning("could not persist activation receipt: %s", e)


def activate(key: str) -> tuple[bool, str]:
    """Verify a licence key, register it with the service, and store it.

    The signature is checked locally FIRST, so a typo or a forgery never reaches
    the network and the customer gets an instant answer. Only a genuine licence
    is worth an activation slot.

    If a service is configured it decides the rest: it counts activations,
    binds the machine, and can refuse a revoked or over-used licence. If it is
    not configured, or cannot be reached, activation still succeeds — a licence
    that verifies is a licence, and refusing to activate one because the
    issuer's host is offline punishes the customer for the issuer's outage.
    """
    payload = verify(key)
    if payload is None:
        return False, ("That licence key is not valid for this machine. Check "
                       "it was copied whole, and that it was issued for this "
                       "computer.")

    lic_id = payload.get("id", "?")
    answer = _call_server("/activate", {
        "licence": key.strip(), "machine": machine_id(), "id": lic_id})

    if answer is not None and not answer.get("ok"):
        reason = answer.get("error") or "the licence service refused it"
        log.warning("activation refused for %s: %s", lic_id, reason)
        return False, reason

    lic_path, _ = _paths()
    try:
        lic_path.parent.mkdir(parents=True, exist_ok=True)
        lic_path.write_text(key.strip())
    except OSError as e:
        return False, f"The licence is valid but could not be saved: {e}"

    now = int(time.time())
    _write_receipt({"id": lic_id, "activated": now,
                    "last_ok": now if answer is not None else 0,
                    "online": answer is not None})
    log.info("licence %s activated (%s, %s)", lic_id,
             payload.get("edition", "?"),
             "registered" if answer is not None else "offline")
    if answer is None and _server():
        return True, (f"Activated — {payload.get('edition', 'licensed')}. The "
                      f"licence service could not be reached; it will be "
                      f"registered automatically later.")
    return True, f"Activated — {payload.get('edition', 'licensed')}."


def _revoked(lic_id: str) -> bool:
    """Has the service explicitly revoked this licence?

    Only ever returns True on an explicit answer. Unreachable is not revoked;
    it just means the receipt goes stale, and after RECHECK_GRACE_S a stale
    receipt is what stops trading — otherwise blocking the host in /etc/hosts
    would be a permanent bypass.
    """
    if not _server():
        return False
    rec = _read_receipt()
    now = int(time.time())
    last_ok = int(rec.get("last_ok") or 0)
    if last_ok and now - last_ok < RECHECK_EVERY_S:
        return False

    answer = _call_server("/check", {"id": lic_id, "machine": machine_id()},
                          timeout=5.0)
    if answer is None:
        if last_ok and now - last_ok > RECHECK_GRACE_S:
            log.warning("licence %s has not been confirmed for %d days",
                        lic_id, (now - last_ok) // 86400)
            return True
        return False
    if answer.get("ok"):
        rec.update({"id": lic_id, "last_ok": now})
        _write_receipt(rec)
        return False
    log.warning("licence %s was revoked by the service", lic_id)
    return True


# ── the question everything else asks ────────────────────────────────────────

@dataclass(frozen=True)
class Status:
    state: str            # "licensed" | "trial" | "expired"
    may_trade: bool
    days_left: int | None
    detail: str
    licence_id: str | None = None


def status() -> Status:
    key = stored_licence()
    if key:
        payload = verify(key)
        if payload:
            lic_id = payload.get("id", "?")
            if _revoked(lic_id):
                return Status("revoked", False, 0,
                              "This licence is no longer active. Contact "
                              "support if you believe that is wrong.", lic_id)
            return Status("licensed", True, None,
                          f"Licensed to {lic_id}"
                          f" ({payload.get('edition', 'perpetual')}).", lic_id)
        return Status("expired", False, 0,
                      "The stored licence is no longer valid for this machine. "
                      "Enter a current licence key to keep trading.")

    rec = _read_trial()
    if rec.get("tampered"):
        return Status("expired", False, 0,
                      "The trial could not be verified — the system clock moved "
                      "backwards. Enter a licence key to trade.")
    used = (int(time.time()) - int(rec["first_run"])) / 86400
    left = max(0, TRIAL_DAYS - int(used))
    if left <= 0:
        return Status("expired", False, 0,
                      f"The {TRIAL_DAYS}-day trial has ended. Everything except "
                      f"placing orders keeps working; enter a licence key to "
                      f"resume trading.")
    return Status("trial", True, left,
                  f"Trial — {left} day{'s' if left != 1 else ''} of trading left.")


def require_trading() -> None:
    """Raise NotLicensed unless this installation may place orders."""
    st = status()
    if not st.may_trade:
        raise NotLicensed(st.detail)
