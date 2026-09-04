"""Revocation with no server — a signed list on read-only hosting.

Run from repo root: .venv/bin/python scripts/test_licence_crl.py
Serves the list from a loopback HTTP server over a throwaway MMT_HOME.

WHY A LIST AND NOT A SERVICE

  Offline verification proves who issued a licence. It cannot withdraw one, so
  a refund or a chargeback has no answer. Revocation, unlike activation
  counting, only ever needs to be READ — and read-only hosting is free. The
  issuer signs a file and publishes it; there is nothing to run and nothing to
  pay for.

WHAT AN ATTACKER CONTROLS

  The file is fetched over a network the issuer does not own, from a host that
  may be a CDN, a proxy, or the customer's own /etc/hosts. So the list cannot
  be trusted for being fetched — only for being signed — and the two attacks
  that remain are both replay:

    * serve an OLDER list, from before a key was revoked → serial must never
      go backwards.
    * serve the SAME list forever, so a later revocation never arrives → a list
      past CRL_MAX_AGE_S is treated as unreachable, not as innocence.

  Blocking the URL entirely is the third, and it is handled the way every other
  outage is: the gate stays open, until the receipt is stale past the grace.
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PY = str(ROOT / ".venv" / "bin" / "python")
PASS = FAIL = 0
SERVED = {"body": "", "code": 200}


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


def section(n, title):
    print(f"\n{n}  {title}")


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(SERVED["code"])
        self.end_headers()
        self.wfile.write(SERVED["body"].encode())

    def log_message(self, *a):
        pass


KEYFILE = Path.home() / ".mootrader-licence" / "signing-private.pem"
if not KEYFILE.exists():
    print("no issuer key on this machine — nothing to test")
    sys.exit(0)

srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}/revoked.txt"

TMP = Path(tempfile.mkdtemp(prefix="mmt-crl-"))
(TMP / "data").mkdir()
(TMP / "logs").mkdir()
os.environ["MMT_HOME"] = str(TMP)
os.environ["MMT_LICENCE_CRL"] = URL
os.environ.pop("MMT_LICENCE_SERVER", None)

from src import licence, order_gate                      # noqa: E402


def sign_list(ids, issued=None, serial=1):
    """Sign a list directly, so a test can forge dates and serials at will."""
    import base64
    from datetime import datetime, timezone
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(KEYFILE.read_bytes(), password=None)
    payload = {"revoked": ids,
               "issued": issued or datetime.now(timezone.utc).date().isoformat(),
               "serial": serial}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return f"MTCRL1.{b64(raw)}.{b64(key.sign(raw))}"


def fresh(lic_id="MT-CRL"):
    """A fresh installation holding a valid licence, with no CRL state."""
    for p in (*licence._paths(), licence._receipt_path()):
        try:
            p.unlink()
        except OSError:
            pass
    key = subprocess.run(
        [PY, str(ROOT / "scripts" / "licence_gen.py"),
         "--machine", licence.machine_id(), "--id", lic_id],
        capture_output=True, text=True).stdout.strip()
    licence.activate(key)


section(1, "a licence not on the list keeps working")
SERVED["body"] = sign_list(["MT-OTHER"], serial=1)
fresh("MT-CRL")
st = licence.status()
check("still licensed", st.state == "licensed")
check("may trade", st.may_trade)

section(2, "a licence ON the list stops trading")
fresh("MT-CRL")
SERVED["body"] = sign_list(["MT-CRL"], serial=2)
rec = licence._read_receipt()
rec["crl_ok"] = 1                       # force a refresh rather than wait a day
licence._write_receipt(rec)
st = licence.status()
check("state is revoked", st.state == "revoked")
check("may_trade is false", not st.may_trade)
order_gate.reset_for_tests()
order_gate.permit("t")
try:
    order_gate.require("placing a BUY order for AAPL")
    check("the order gate shuts", False)
except order_gate.OrdersNotPermitted:
    check("the order gate shuts", True)

section(3, "the list cannot be forged or edited by whoever hosts it")
fresh("MT-CRL")
for label, body in (("garbage", "not a list"),
                    ("right shape, junk signature", "MTCRL1.eyJhIjoxfQ.AAAA"),
                    ("an empty list swapped in unsigned",
                     json.dumps({"revoked": [], "serial": 99}))):
    SERVED["body"] = body
    check(f"{label} is ignored", licence.verify_crl(body) is None)

# A real list, edited after signing.
good = sign_list(["MT-CRL"], serial=3)
head, payload, sig = good.split(".")
tampered = f"{head}.{payload[:-2]}{'A' if payload[-2] != 'A' else 'B'}{payload[-1]}.{sig}"
check("a list edited after signing is ignored", licence.verify_crl(tampered) is None)

section(4, "replay: an older list cannot un-revoke a key")
fresh("MT-CRL")
SERVED["body"] = sign_list(["MT-CRL"], serial=5)
rec = licence._read_receipt(); rec["crl_ok"] = 1; licence._write_receipt(rec)
check("revoked by serial 5", not licence.status().may_trade)
# Now serve the list from BEFORE the revocation.
SERVED["body"] = sign_list([], serial=4)
rec = licence._read_receipt(); rec["crl_ok"] = 1; licence._write_receipt(rec)
check("a lower serial is refused, so it stays revoked",
      not licence.status().may_trade)
check("the highest serial seen is remembered",
      int(licence._read_receipt().get("crl_serial") or 0) == 5)

section(5, "replay: the same list served forever goes stale")
fresh("MT-CRL")
SERVED["body"] = sign_list([], issued="2020-01-01", serial=9)
check("a list older than the max age is not usable",
      licence._fetch_crl() is None)

section(6, "an unreachable list does not stop a customer")
fresh("MT-CRL")
SERVED["code"] = 500
st = licence.status()
check("still trading when the host errors", st.may_trade)
rec = licence._read_receipt()
rec["crl_ok"] = int(time.time()) - 31 * 86400
licence._write_receipt(rec)
check("but a list unread past the grace does stop it",
      not licence.status().may_trade)
SERVED["code"] = 200

section(7, "with no CRL configured, nothing changes")
os.environ.pop("MMT_LICENCE_CRL")
fresh("MT-CRL")
check("pure offline still licensed", licence.status().state == "licensed")
check("and no network was needed", licence._crl_url() == "")

print(f"\n{PASS} passed, {FAIL} failed")
srv.shutdown()
sys.exit(1 if FAIL else 0)
