"""Activation, revocation, and what happens when the issuer's host is down.

Run from repo root: .venv/bin/python scripts/test_licence_server.py
Starts licence_server on a free loopback port against a throwaway SQLite file.
No OpenD, no orders, no outbound network.

THE ASYMMETRY BEING TESTED

  Offline verification already proves who issued a licence. The service exists
  for the two questions a signature cannot answer once it is handed out: how
  many machines it is on, and whether it should still work. Refunds and leaked
  keys are both revocation.

  Which makes the failure direction the important behaviour, and the one most
  easily got backwards. An unreachable service must NOT stop a customer
  trading — the cost of the issuer's outage has to fall on the issuer, not on
  someone holding a position. Only an explicit refusal closes the gate. The
  30-day grace is the counterweight: block the host in /etc/hosts and you buy a
  month, not forever.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PY = str(ROOT / ".venv" / "bin" / "python")
ADMIN = "test-admin-token"
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post(url: str, body: dict, token: str = "") -> tuple[int, dict]:
    hdr = {"Content-Type": "application/json"}
    if token:
        hdr["X-Admin-Token"] = token
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def issue(**kw) -> str:
    args = [PY, str(ROOT / "scripts" / "licence_gen.py")]
    for k, v in kw.items():
        args += [f"--{k}", str(v)]
    out = subprocess.run(args, capture_output=True, text=True)
    return out.stdout.strip()


TMP = Path(tempfile.mkdtemp(prefix="mmt-licsrv-"))
PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
KEYFILE = Path.home() / ".mootrader-licence" / "signing-private.pem"

if not KEYFILE.exists():
    print("no issuer key on this machine — nothing to test")
    sys.exit(0)

env = {**os.environ,
       "MMT_LICENCE_DB": str(TMP / "act.db"),
       "MMT_LICENCE_ADMIN_TOKEN": ADMIN,
       "MMT_LICENCE_PORT": str(PORT),
       "MMT_LICENCE_MAX_MACHINES": "2"}
srv = subprocess.Popen([PY, str(ROOT / "licence_server" / "server.py")],
                       env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            urllib.request.urlopen(BASE + "/health", timeout=1)
            break
        except OSError:
            time.sleep(0.25)
    else:
        print("server never came up")
        sys.exit(1)

    section(1, "a genuine licence activates")
    key = issue(machine="aaaa000000000001", id="MT-1")
    code, r = post(BASE + "/activate", {"licence": key, "machine": "aaaa000000000001"})
    check("accepted", code == 200 and r.get("ok"))

    check("the same machine re-activating is not a new slot",
          post(BASE + "/activate",
               {"licence": key, "machine": "aaaa000000000001"})[1].get("ok"))

    section(2, "the service verifies for itself")
    code, r = post(BASE + "/activate", {"licence": "MT1.aaaa.bbbb", "machine": "x"})
    check("a forged key never reaches the table", code == 400 and not r.get("ok"))
    code, r = post(BASE + "/activate", {"licence": key, "machine": "beef000000000002"})
    check("a licence used on the machine it was NOT issued for is refused",
          code == 403 and not r.get("ok"))

    section(3, "the machine cap holds")
    unbound = issue(id="MT-2")
    for m in ("1111000000000001", "2222000000000002"):
        post(BASE + "/activate", {"licence": unbound, "machine": m})
    code, r = post(BASE + "/activate", {"licence": unbound, "machine": "3333000000000003"})
    check("a third machine is refused", code == 403 and not r.get("ok"))
    check("the message says what to do", "support" in (r.get("error") or "").lower())

    section(4, "revocation")
    check("revoke needs the admin token",
          post(BASE + "/admin/revoke", {"id": "MT-1"})[0] == 401)
    check("revoked with it",
          post(BASE + "/admin/revoke", {"id": "MT-1", "reason": "refunded"},
               ADMIN)[1].get("ok"))
    code, r = post(BASE + "/check", {"id": "MT-1", "machine": "aaaa000000000001"})
    check("/check now refuses", code == 403 and not r.get("ok"))
    check("and says why", r.get("error") == "refunded")
    check("re-activating a revoked licence is refused",
          not post(BASE + "/activate",
                   {"licence": key, "machine": "aaaa000000000001"})[1].get("ok"))
    post(BASE + "/admin/unrevoke", {"id": "MT-1"}, ADMIN)
    check("un-revoking restores it",
          post(BASE + "/check", {"id": "MT-1",
                                 "machine": "aaaa000000000001"})[1].get("ok"))

    section(5, "an installation talks to it")
    home = TMP / "home"
    (home / "data").mkdir(parents=True)
    (home / "logs").mkdir(parents=True)
    probe = f'''
import sys, time; sys.path.insert(0, {str(ROOT)!r})
from src import licence, order_gate
mid = licence.machine_id()
import subprocess
key = subprocess.run([{PY!r}, {str(ROOT / "scripts" / "licence_gen.py")!r},
                      "--machine", mid, "--id", "MT-LIVE"],
                     capture_output=True, text=True).stdout.strip()
ok, _ = licence.activate(key)
print("ACTIVATED", ok, licence.status().state)
import urllib.request, json
req = urllib.request.Request({BASE!r} + "/admin/revoke",
    data=json.dumps({{"id": "MT-LIVE", "reason": "test"}}).encode(),
    headers={{"Content-Type": "application/json", "X-Admin-Token": {ADMIN!r}}})
urllib.request.urlopen(req).read()
rec = licence._read_receipt(); rec["last_ok"] = 1; licence._write_receipt(rec)
st = licence.status()
print("AFTER_REVOKE", st.state, st.may_trade)
order_gate.reset_for_tests(); order_gate.permit("t")
try:
    order_gate.require("placing a BUY order for AAPL"); print("GATE open")
except order_gate.OrdersNotPermitted:
    print("GATE shut")
'''
    out = subprocess.run(
        [PY, "-c", probe], capture_output=True, text=True,
        env={**os.environ, "MMT_HOME": str(home), "MMT_LICENCE_SERVER": BASE}).stdout
    check("it activates against the service", "ACTIVATED True licensed" in out)
    check("a revoked licence stops trading", "AFTER_REVOKE revoked False" in out)
    check("and the order gate shuts", "GATE shut" in out)

    section(6, "the issuer's host going down does not stop a customer")
    srv.terminate()
    srv.wait(timeout=10)
    home2 = TMP / "home2"
    (home2 / "data").mkdir(parents=True)
    (home2 / "logs").mkdir(parents=True)
    probe2 = f'''
import sys, time; sys.path.insert(0, {str(ROOT)!r})
from src import licence, order_gate
import subprocess
mid = licence.machine_id()
key = subprocess.run([{PY!r}, {str(ROOT / "scripts" / "licence_gen.py")!r},
                      "--machine", mid, "--id", "MT-OFF"],
                     capture_output=True, text=True).stdout.strip()
ok, msg = licence.activate(key)
print("OFFLINE_ACTIVATE", ok, licence.status().may_trade)
order_gate.reset_for_tests(); order_gate.permit("t")
try:
    order_gate.require("placing a BUY order for AAPL"); print("GATE open")
except order_gate.OrdersNotPermitted:
    print("GATE shut")
rec = licence._read_receipt(); rec["last_ok"] = int(time.time()) - 31*86400
licence._write_receipt(rec)
print("STALE", licence.status().may_trade)
'''
    out = subprocess.run(
        [PY, "-c", probe2], capture_output=True, text=True,
        env={**os.environ, "MMT_HOME": str(home2), "MMT_LICENCE_SERVER": BASE}).stdout
    check("activation still succeeds with the service unreachable",
          "OFFLINE_ACTIVATE True True" in out)
    check("and the order gate stays open", "GATE open" in out)
    check("but a receipt unconfirmed past the grace does stop trading",
          "STALE False" in out)
finally:
    if srv.poll() is None:
        srv.terminate()
        srv.wait(timeout=10)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
