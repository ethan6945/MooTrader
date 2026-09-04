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
  patch it", which is the honest goal.

  Withdraw a licence. This is offline by choice — nothing here reaches the
  network, and a signature already handed out cannot be taken back by one. A
  refund or a chargeback has no technical answer; issue short-dated licences
  and renew them if that matters. Adding revocation later does not help the
  copies already sold, because those builds have nothing that would go looking
  for it: a revocation mechanism has to ship BEFORE the licence it revokes.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Public half of the issuer's Ed25519 key. Safe to publish: it verifies and
# cannot sign. The private half lives only on the issuer's machine.
SIGNING_PUBLIC_KEY = bytes.fromhex(
    "ece3cddcf8479a9ac6cae738a93659a92d1ed15a64604da5101adf9f0e2007e9")

TRIAL_DAYS = 30
_PREFIX = "MT1"



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


def verify(key: str) -> dict | None:
    """Return the payload of a licence valid FOR THIS MACHINE, else None.

    Checks, in order: shape, signature, expiry, machine binding. A licence that
    fails any of them is not a licence here.
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


def activate(key: str) -> tuple[bool, str]:
    """Verify a licence key and store it. Returns (ok, message for the user).

    Nothing leaves the machine. The signature proves the issuer wrote it, and
    the machine binding proves it was written for this computer; neither needs
    to be asked of anyone.
    """
    payload = verify(key)
    if payload is None:
        return False, ("That licence key is not valid for this machine. Check "
                       "it was copied whole, and that it was issued for this "
                       "computer.")

    lic_path, _ = _paths()
    try:
        lic_path.parent.mkdir(parents=True, exist_ok=True)
        lic_path.write_text(key.strip())
    except OSError as e:
        return False, f"The licence is valid but could not be saved: {e}"

    log.info("licence %s activated (%s)", payload.get("id", "?"),
             payload.get("edition", "?"))
    return True, f"Activated — {payload.get('edition', 'licensed')}."


# ── the question everything else asks ────────────────────────────────────────

@dataclass(frozen=True)
class Status:
    state: str            # "licensed" | "trial" | "expired"
    may_trade: bool
    days_left: int | None
    detail: str
    licence_id: str | None = None
    # The day the trial runs out. None once licensed — every licence issued is
    # perpetual, so there is nothing for it to name. A countdown alone ("14
    # days left") cannot be checked against anything; a date can.
    ends_on: str | None = None


def status() -> Status:
    key = stored_licence()
    if key:
        payload = verify(key)
        if payload:
            lic_id = payload.get("id", "?")
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
    ends_on = (datetime.fromtimestamp(int(rec["first_run"]), timezone.utc).date()
               + timedelta(days=TRIAL_DAYS)).isoformat()
    if left <= 0:
        return Status("expired", False, 0,
                      f"The {TRIAL_DAYS}-day trial ended on {ends_on}. "
                      f"Everything except placing orders keeps working; enter a "
                      f"licence key to resume trading.",
                      None, ends_on)
    return Status("trial", True, left,
                  f"Trial — {left} day{'s' if left != 1 else ''} of trading "
                  f"left, until {ends_on}.", None, ends_on)


def require_trading() -> None:
    """Raise NotLicensed unless this installation may place orders."""
    st = status()
    if not st.may_trade:
        raise NotLicensed(st.detail)
