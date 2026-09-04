"""Trial, licence and forgery — the gate that decides whether a copy may trade.

Run from repo root: .venv/bin/python scripts/test_licence.py
No OpenD, no orders. Drives src/licence.py and src/order_gate.py directly
against a throwaway MMT_HOME, so nothing touches the real installation.

WHAT IS BEING PROTECTED

  The product boundary is RUNNING vs TRADING, which order_gate already draws.
  An unlicensed copy must still start, connect, score and backtest — only the
  three methods that change broker state are gated. So these tests care about
  two things: that an expired trial cannot place an order, and that nothing
  short of the issuer's private key can produce a licence that says otherwise.

  The forgery cases are the point. A licence system whose keys can be minted by
  the people holding the app is theatre, and the way that happens is shipping a
  decoder instead of a verifier.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-lic-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

from src import licence, order_gate            # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


def section(n: int, title: str) -> None:
    print(f"\n{n}  {title}")


def _clear() -> None:
    for p in licence._paths():
        try:
            p.unlink()
        except OSError:
            pass


def _issue(**over) -> str:
    """Sign a licence with the real issuer key, if this machine has one."""
    import base64, json
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(
        (ROOT / "Keygen Activator" / "signing-private.pem").read_bytes(),
        password=None)
    payload = {"id": "MT-TEST", "machine": licence.machine_id(),
               "edition": "perpetual", "issued": "2026-09-03", "expires": None}
    payload.update(over)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return f"MT1.{b64(raw)}.{b64(key.sign(raw))}"


HAVE_KEY = (ROOT / "Keygen Activator" / "signing-private.pem").exists()

# ── 1 ────────────────────────────────────────────────────────────────────────
section(1, "a fresh install is in trial and may trade")
_clear()
st = licence.status()
check("state is trial", st.state == "trial")
check(f"{licence.TRIAL_DAYS} days of trading left", st.days_left == licence.TRIAL_DAYS)
check("the gate lets an order through", st.may_trade)

# ── 2 ────────────────────────────────────────────────────────────────────────
section(2, "an expired trial stops orders, and only orders")
_clear()
rec = licence._read_trial()
rec["first_run"] = int(time.time()) - (licence.TRIAL_DAYS + 1) * 86400
_, trial_path = licence._paths()
trial_path.write_text(licence._seal(rec))
st = licence.status()
check("state is expired", st.state == "expired")
check("may_trade is false", not st.may_trade)

order_gate.reset_for_tests()
order_gate.permit("test grant")
check("the process still HAS the order capability", order_gate.permitted())
try:
    order_gate.require("placing a BUY order for AAPL")
    check("require() refuses the order", False)
except order_gate.OrdersNotPermitted as e:
    check("require() refuses the order", True)
    check("the message names the trial, not a missing grant",
          "trial" in str(e).lower())

# ── 3 ────────────────────────────────────────────────────────────────────────
section(3, "a real licence resumes trading on an expired trial")
if not HAVE_KEY:
    print("  --  skipped: no issuer key on this machine")
else:
    ok, msg = licence.activate(_issue())
    check("activate() accepts it", ok)
    st = licence.status()
    check("state is licensed", st.state == "licensed")
    check("may_trade is true again", st.may_trade)
    order_gate.reset_for_tests()
    order_gate.permit("test grant")
    try:
        order_gate.require("placing a BUY order for AAPL")
        check("the order goes through", True)
    except order_gate.OrdersNotPermitted:
        check("the order goes through", False)

# ── 4 ────────────────────────────────────────────────────────────────────────
section(4, "forgeries are refused")
check("garbage is not a licence", licence.verify("nonsense") is None)
check("an empty key is not a licence", licence.verify("") is None)
check("the right shape with a junk signature fails",
      licence.verify("MT1.eyJpZCI6ICJYIn0.AAAA") is None)
if HAVE_KEY:
    good = _issue()
    head, payload, sig = good.split(".")
    # Flip one character of the payload — the signature no longer covers it.
    tampered = f"{head}.{payload[:-2]}{'A' if payload[-2] != 'A' else 'B'}{payload[-1]}.{sig}"
    check("a payload edited after signing fails", licence.verify(tampered) is None)
    check("a licence bound to another machine fails",
          licence.verify(_issue(machine="0000deadbeef0000")) is None)
    check("an expired licence fails",
          licence.verify(_issue(expires="2020-01-01")) is None)
    check("an unbound licence still verifies", licence.verify(_issue(machine="")) is not None)

# ── 5 ────────────────────────────────────────────────────────────────────────
section(5, "the trial record cannot be edited by hand")
_clear()
licence._read_trial()
_, trial_path = licence._paths()
raw = trial_path.read_text()
trial_path.write_text(raw.replace(raw.split("|")[0],
                                  '{"first_run":9999999999,"last_seen":9999999999,"tampered":false}'))
rec = licence._read_trial()
check("a hand-edited record is discarded and the trial restarts",
      rec["first_run"] <= int(time.time()) + 1)

section(6, "a clock moved backwards is treated as tampering")
_clear()
rec = licence._read_trial()
rec["last_seen"] = int(time.time()) + 10 * 86400      # pretend we saw the future
trial_path.write_text(licence._seal(rec))
rec = licence._read_trial()
check("rollback is flagged", rec.get("tampered") is True)
check("and trading stops", not licence.status().may_trade)

_clear()
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
