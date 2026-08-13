"""The start protocol: what it refuses, and what it will not inherit.

Run from repo root: .venv/bin/python scripts/test_start_protocol.py
Builds every home, database and worker it uses in a temp dir. Starts nothing
real — the "worker" is a small script that reports READY and exits.

THE SCENARIO EACH OF THESE COMES FROM
  On 2026-08-11 a worker started on its own, from a web server launched the day
  before, and traded for two hours. The parent still held MAX_POSITION_PCT=0.40,
  AUTO_APPLY_PARAMS=true and AUTO_BUDGET_ENABLED=true — values that had been
  corrected on disk hours earlier. Nothing recorded who started it or against
  what. The pid file said one worker; the operator believed none.

  So: the environment is rebuilt rather than inherited, both sides agree on a
  configuration hash before anything trades, one lease decides the winner, and
  no start happens that cannot be written down.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import SCHEMA, SCHEMA_VERSION as SCHEMA_V

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


BASE_ENV = """\
MOO_TRADE_ENV=SIMULATE
ACCOUNT_USD=10000
MAX_POSITION_PCT=0.10
MAX_POSITIONS=5
RISK_PER_TRADE=0.05
DAILY_DRAWDOWN_STOP=0.06
DD_HALT_PCT=18.0
PARAMS_FROZEN=true
AUTO_APPLY_PARAMS=false
AUTO_BUDGET_ENABLED=false
MAX_POSITIONS_AUTOSCALE=false
ENTRY_SCORE_THRESHOLD=70
SL_ATR_MULT=2.8
TP_ATR_MULT=10.0
MAX_HOLD_DAYS=4
"""

# A stand-in worker. It runs BOTH phases, like src.main does: report, then wait
# to be accepted. It writes a marker file the moment it is cleared, which is how
# the tests below can tell "the worker was allowed to trade" from "the worker
# started" — under one phase those were the same event, which is the bug.
FAKE_WORKER = '''\
import os, sys, time
sys.path.insert(0, {root!r})
from src import start_protocol
try:
    start_protocol.worker_verify_and_report()
except Exception as e:
    print("worker refused:", e, file=sys.stderr)
    sys.exit(3)
if os.environ.get("FAKE_WORKER_SKIP_GO") != "1":
    try:
        start_protocol.wait_for_go()
    except Exception as e:
        print("worker not cleared:", e, file=sys.stderr)
        sys.exit(4)
# Derived from MMT_HOME, not passed as its own variable: build_child_env
# strips everything off its keep-list, so a FAKE_WORKER_* variable never
# reaches the worker. That is the environment rebuild doing its job, and a
# test that routed around it would be testing a channel production has not got.
open(os.path.join(os.environ["MMT_HOME"], "logs", "traded.marker"), "w").write(
    str(os.getpid()))
time.sleep(int(os.environ.get("FAKE_WORKER_LINGER", "30")))
'''


def make_home(tmp: Path, name: str, env_text: str = BASE_ENV,
              *, db_version: int | None = None, positions=(), mirror=None,
              accounts=(("SIMULATE",),)) -> Path:
    home = tmp / name
    (home / "data").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(env_text)

    db = home / "data" / "trader.db"
    c = sqlite3.connect(str(db))
    # Built from the real SCHEMA, not a hand-copied one.
    #
    # This fixture used to restate the tables inline, and it went stale twice:
    # first missing history.week, which SCHEMA indexes, and then missing the
    # (account_id, symbol) key that v5 introduced. Both failed inside the
    # worker, and a fixture bug that surfaces there reads as a protocol bug —
    # you go looking at the handshake for a defect that is in the test's idea of
    # what a database looks like. Copying the schema by hand cannot be done
    # carefully enough to be worth doing.
    c.executescript(SCHEMA)
    for i, (envname, *_rest) in enumerate(accounts):
        c.execute("INSERT INTO accounts (account_id, trade_env, broker_acc_id, "
                  "label, created_at) VALUES (?,?,NULL,?,?)",
                  (f"acct-{i}-{envname}", envname, envname, f"2026-01-0{i+1}"))
    first_account = f"acct-0-{accounts[0][0]}" if accounts else ""
    for sym in positions:
        c.execute("INSERT INTO open_trades (symbol,qty,entry_price,stop_loss,"
                  "take_profit,opened_at,account_id) VALUES (?,1,10,9,11,'x',?)",
                  (sym, first_account))
    c.execute(f"PRAGMA user_version = "
              f"{SCHEMA_V if db_version is None else db_version}")
    c.commit(); c.close()

    if mirror is not None:
        (home / "data" / "open_trades.json").write_text(
            json.dumps({s: {"qty": 1} for s in mirror}))
    return home


def in_home(home: Path, code: str, extra_env=None, timeout=90):
    """Run code with settings.root pointed at `home`, in its own interpreter."""
    env = dict(os.environ, MMT_HOME=str(home), PYTHONPATH=str(ROOT))
    env.pop("MMT_ALLOW_SCHEMA_UPGRADE", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=timeout)


tmp = Path(tempfile.mkdtemp(prefix="mmt-start-"))
worker_py = tmp / "fake_worker.py"
worker_py.write_text(FAKE_WORKER.format(root=str(ROOT)))

PREPARE = '''
import json, sys
from src import start_protocol as sp
try:
    r = sp.prepare({src!r})
    print("OK " + json.dumps({{"config_sha256": r.config_sha256,
                              "effective_env": r.effective_env,
                              "safety": r.safety}}))
except sp.StartRefused as e:
    print("REFUSED " + e.code)
'''

# ── 1. a stale parent environment must not reach the worker ─────────────────
print("1  environment is rebuilt, not inherited")
home = make_home(tmp, "inherit")
stale = {"MAX_POSITION_PCT": "0.40", "AUTO_APPLY_PARAMS": "true",
         "AUTO_BUDGET_ENABLED": "true", "MMT_ALLOW_SCHEMA_UPGRADE": "1",
         "ENTRY_SCORE_THRESHOLD": "55"}
r = in_home(home, '''
import os, json
from src import start_protocol as sp
env = sp.build_child_env(__import__("pathlib").Path(os.environ["MMT_HOME"]), "sha")
print(json.dumps({k: env.get(k) for k in
   ["MAX_POSITION_PCT","AUTO_APPLY_PARAMS","AUTO_BUDGET_ENABLED",
    "MMT_ALLOW_SCHEMA_UPGRADE","ENTRY_SCORE_THRESHOLD","MMT_HOME","PATH"]}))
''', extra_env=stale)
child = json.loads([l for l in r.stdout.splitlines() if l.startswith("{")][0])
for k in ("MAX_POSITION_PCT", "AUTO_APPLY_PARAMS", "AUTO_BUDGET_ENABLED",
          "ENTRY_SCORE_THRESHOLD"):
    check(f"stale {k} is not passed through", child[k] is None)
check("MMT_ALLOW_SCHEMA_UPGRADE is never inherited",
      child["MMT_ALLOW_SCHEMA_UPGRADE"] is None)
check("MMT_HOME is pinned explicitly", child["MMT_HOME"] == str(home))
check("PATH survives (the process still has to run)", bool(child["PATH"]))

# The worker resolves from the file, so the file's value is what it gets.
r = in_home(home, PREPARE.format(src="web"), extra_env=stale)
out = [l for l in r.stdout.splitlines() if l.startswith("OK ")]
check("prepare succeeds despite a stale parent", bool(out))
if out:
    d = json.loads(out[0][3:])
    check("safety values come from the FILE, not the parent",
          d["safety"]["MAX_POSITION_PCT"] == "0.10"
          and d["safety"]["AUTO_APPLY_PARAMS"] == "false")

# ── 2. a deleted key must not fall back to the parent's value ───────────────
print("\n2  a key removed from the file")
trimmed = "\n".join(l for l in BASE_ENV.splitlines()
                    if not l.startswith("MAX_POSITION_PCT")) + "\n"
home2 = make_home(tmp, "deleted", trimmed)
r = in_home(home2, PREPARE.format(src="web"),
            extra_env={"MAX_POSITION_PCT": "0.40"})
out = [l for l in r.stdout.splitlines() if l.startswith("OK ")]
check("prepare still succeeds", bool(out))
if out:
    d = json.loads(out[0][3:])
    check("the deleted key reads as absent, not as the parent's 0.40",
          d["safety"]["MAX_POSITION_PCT"] == "<absent>")

# ── 3. editing .env between two starts ─────────────────────────────────────
print("\n3  .env edited between starts, no restart of the parent")
home3 = make_home(tmp, "edited")
a = json.loads([l for l in in_home(home3, PREPARE.format(src="web")).stdout.splitlines()
                if l.startswith("OK ")][0][3:])
(home3 / ".env").write_text(BASE_ENV.replace("MAX_POSITION_PCT=0.10",
                                             "MAX_POSITION_PCT=0.15"))
b = json.loads([l for l in in_home(home3, PREPARE.format(src="web")).stdout.splitlines()
                if l.startswith("OK ")][0][3:])
check("the second prepare sees the edit", b["safety"]["MAX_POSITION_PCT"] == "0.15")
check("and the fingerprint changes with it", a["config_sha256"] != b["config_sha256"])

# ── 4. malformed, duplicated, unreadable configuration ─────────────────────
print("\n4  configuration that cannot be trusted")
cases = [
    ("duplicate keys", BASE_ENV + "MAX_POSITION_PCT=0.40\n", "config_duplicate_keys"),
    ("a line with no '='", BASE_ENV + "THIS LINE IS BROKEN\n", "config_malformed"),
    ("an empty key", BASE_ENV + "=0.40\n", "config_malformed"),
]
for label, text, expect in cases:
    h = make_home(tmp, "cfg-" + expect + label[:4].replace(" ", ""), text)
    r = in_home(h, PREPARE.format(src="web"))
    got = [l for l in r.stdout.splitlines() if l.startswith("REFUSED")]
    check(f"{label} -> {expect}", bool(got) and got[0].split()[1] == expect)

h = make_home(tmp, "cfg-unreadable")
os.chmod(h / ".env", 0o000)
r = in_home(h, PREPARE.format(src="web"))
got = [l for l in r.stdout.splitlines() if l.startswith("REFUSED")]
check("an unreadable file -> config_unreadable",
      bool(got) and got[0].split()[1] == "config_unreadable")
os.chmod(h / ".env", 0o600)

# ── 5. database and account refusals ───────────────────────────────────────
print("\n5  refusals that protect the ledger")
h = make_home(tmp, "v3db", db_version=3)
r = in_home(h, PREPARE.format(src="web"))
got = [l for l in r.stdout.splitlines() if l.startswith("REFUSED")]
check("a v3 database -> db_schema_old",
      bool(got) and got[0].split()[1] == "db_schema_old")

h = make_home(tmp, "conflict", positions=(), mirror=["HPE"])
r = in_home(h, PREPARE.format(src="web"))
got = [l for l in r.stdout.splitlines() if l.startswith("REFUSED")]
check("database/mirror position conflict -> position_conflict",
      bool(got) and got[0].split()[1] == "position_conflict")

# The conflict check reads THIS account's positions, not the table's.
# Unscoped, it compared a two-account union against a one-account mirror, so
# the refusal would have been permanent and would have appeared only once a
# second account existed — i.e. on the first REAL start, and never in any test
# written before then.
h = make_home(tmp, "otheracct", positions=("AAPL",), mirror=["AAPL"],
              accounts=(("SIMULATE",), ("REAL",)))
_c = sqlite3.connect(str(h / "data" / "trader.db"))
_c.execute("INSERT INTO open_trades (symbol,qty,entry_price,stop_loss,"
           "take_profit,opened_at,account_id) VALUES ('NVDA',9,10,9,11,'t',?)",
           ("acct-1-REAL",))
_c.commit(); _c.close()
r = in_home(h, PREPARE.format(src="web"))
check("another account's positions do not trip the conflict check",
      any(l.startswith("OK ") for l in r.stdout.splitlines()))

h = make_home(tmp, "noacct", accounts=())
r = in_home(h, PREPARE.format(src="web"))
got = [l for l in r.stdout.splitlines() if l.startswith("REFUSED")]
check("no account for the environment -> account_unknown",
      bool(got) and got[0].split()[1] == "account_unknown")

h = make_home(tmp, "twoacct", accounts=(("SIMULATE",), ("SIMULATE",)))
r = in_home(h, PREPARE.format(src="web"))
got = [l for l in r.stdout.splitlines() if l.startswith("REFUSED")]
check("two accounts for one environment -> account_unknown",
      bool(got) and got[0].split()[1] == "account_unknown")

h = make_home(tmp, "realreq", BASE_ENV.replace("MOO_TRADE_ENV=SIMULATE",
                                               "MOO_TRADE_ENV=REAL"))
r = in_home(h, PREPARE.format(src="web"))
got = [l for l in r.stdout.splitlines() if l.startswith("REFUSED")]
check("REAL -> real_not_permitted (refused, not downgraded)",
      bool(got) and got[0].split()[1] == "real_not_permitted")

# ── 6. the lease: twenty starts, one worker ────────────────────────────────
print("\n6  concurrent starts")
h = make_home(tmp, "race")
RACE = '''
import json, sys
from src import start_lease
try:
    lease = start_lease.acquire(purpose="worker")
    print("ACQUIRED " + str(lease.fence))
    import time; time.sleep(2)
except start_lease.LeaseUnavailable as e:
    print("REFUSED")
'''
procs = [subprocess.Popen([sys.executable, "-c", RACE], cwd=ROOT,
                          env=dict(os.environ, MMT_HOME=str(h), PYTHONPATH=str(ROOT)),
                          stdout=subprocess.PIPE, text=True) for _ in range(20)]
outs = [p.communicate(timeout=60)[0] for p in procs]
acquired = sum(1 for o in outs if "ACQUIRED" in o)
check(f"exactly one of twenty acquired the lease (got {acquired})", acquired == 1)
check("the rest were refused, not crashed",
      sum(1 for o in outs if "REFUSED" in o) == 19)

# The lease race above is the mechanism. This is the property: twenty full
# Starts, each spawning a real worker process, must leave at most one worker
# that was ever cleared to trade. Racing acquire() alone would still pass if
# commit() spawned before taking the lease, or if a loser's worker were left
# running after its parent was refused.
h = make_home(tmp, "race_full")
FULL_START = '''
import sys
from src import start_protocol as sp
try:
    res = sp.start("web", worker_cmd=[sys.executable, {worker!r}])
    print("STARTED " + str(res["pid"]))
except sp.StartRefused as e:
    print("REFUSED " + e.code)
except Exception as e:
    print("ERROR " + type(e).__name__ + " " + str(e)[:80])
'''.format(worker=str(worker_py))
procs = [subprocess.Popen([sys.executable, "-c", FULL_START], cwd=ROOT,
                          env=dict(os.environ, MMT_HOME=str(h),
                                   PYTHONPATH=str(ROOT)),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True) for _ in range(20)]
outs = [p.communicate(timeout=180)[0] for p in procs]
started = [o for o in outs if "STARTED" in o]
errored = [o for o in outs if "ERROR" in o]
check(f"at most one of twenty Starts produced a worker (got {len(started)})",
      len(started) <= 1)
check("every other Start refused cleanly, none errored",
      not errored, )
if errored:
    print("        " + errored[0].strip())

# And the survivor is the one the lease names — no orphan workers left behind
# by refused parents.
time.sleep(1)
lease_rec = json.loads((h / "logs" / "worker.lease").read_text()) \
    if (h / "logs" / "worker.lease").exists() else {}
live = []
for o in outs:
    if "STARTED" in o:
        pid = int(o.split()[1])
        try:
            os.kill(pid, 0)
            live.append(pid)
        except OSError:
            pass
check(f"at most one worker process is alive (got {len(live)})", len(live) <= 1)
check("the lease names the surviving worker",
      not live or lease_rec.get("pid") == live[0])
for pid in live:
    try:
        os.killpg(os.getpgid(pid), 15)
    except OSError:
        pass
(h / "logs" / "worker.lease").unlink(missing_ok=True)

# A dead holder must not block forever; a live one must.
(h / "logs" / "worker.lease").write_text(json.dumps(
    {"pid": 999999, "host": "x", "fence": 7, "started_at_str": "never"}))
r = in_home(h, "from src import start_lease; "
               "print('FENCE', start_lease.acquire().fence)")
check("a dead holder's lease is breakable", "FENCE 8" in r.stdout)

import os as _os
(h / "logs" / "worker.lease").write_text(json.dumps(
    {"pid": _os.getpid(), "host": "x", "fence": 9,
     "started_at_str": "Mon Jan  1 00:00:00 1970"}))
r = in_home(h, "from src import start_lease; "
               "print('FENCE', start_lease.acquire().fence)")
check("a REUSED pid is not mistaken for the original holder", "FENCE 10" in r.stdout)

# ── 7. commit: READY, timeout, reclamation ─────────────────────────────────
print("\n7  commit and the READY handshake")
h = make_home(tmp, "commit")
COMMIT = '''
import json, sys
from pathlib import Path
from src import start_protocol as sp
req = sp.prepare("web")
try:
    res = sp.commit(req, worker_cmd=[sys.executable, {worker!r}], timeout={to})
    print("READY " + json.dumps(res))
except sp.StartRefused as e:
    print("REFUSED " + e.code)
'''
r = in_home(h, COMMIT.format(worker=str(worker_py), to=45))
line = [l for l in r.stdout.splitlines() if l.startswith("READY ")]
check("a well-behaved worker reaches READY", bool(line))
if line:
    res = json.loads(line[0][6:])
    check("commit returns the worker's real pid", isinstance(res["pid"], int))
    check("and a session id", bool(res.get("session_id")))
    try:
        _os.killpg(_os.getpgid(res["pid"]), 15)
    except OSError:
        pass
(h / "logs" / "worker.lease").unlink(missing_ok=True)

# A worker that never reports must be killed and the lease freed.
silent = tmp / "silent_worker.py"
silent.write_text("import time; time.sleep(120)\n")
h2 = make_home(tmp, "silent")
r = in_home(h2, COMMIT.format(worker=str(silent), to=5), timeout=120)
got = [l for l in r.stdout.splitlines() if l.startswith("REFUSED")]
check("a worker that never reports -> ready_timeout",
      bool(got) and got[0].split()[1] == "ready_timeout")
check("the lease was released after reclamation",
      not (h2 / "logs" / "worker.lease").exists())
audit = [json.loads(l) for l in
         (h2 / "logs" / "start_audit.jsonl").read_text().splitlines()]
check("the reclamation is audited", any(a["result"] == "reclaimed" for a in audit))

# ── 7b. the worker may not trade until the parent says so ──────────────────
#
# READY means "I checked myself and I am consistent". It does not mean the
# parent agrees: four comparisons happen only on the parent side, and any of
# them can fail. Before the GO gate the worker simply carried on after writing
# READY, and its first real act — _startup_protect_stops() — places and cancels
# broker orders. So a worker the parent was about to reject would have traded
# first, and the kill that follows cannot recall a sent order.
print("\n7b  READY is not permission")

h = make_home(tmp, "gogate")
traded = h / "logs" / "traded.marker"
r = in_home(h, COMMIT.format(worker=str(worker_py), to=45))
line = [l for l in r.stdout.splitlines() if l.startswith("READY ")]
check("a worker that waits for GO still starts normally", bool(line))
for _ in range(100):
    if traded.exists():
        break
    time.sleep(0.1)
check("...and only acts after being cleared", traded.exists())
if line:
    res = json.loads(line[0][6:])
    lease_now = json.loads((h / "logs" / "worker.lease").read_text())
    # Criterion 4/6: the lease must name the trader, not the web server that
    # asked for one. A lease in the parent's name dies with the parent, and the
    # next Start reads it as stale and spawns a second worker beside this one.
    check("the lease names the worker, not the parent",
          lease_now.get("pid") == res["pid"])
    check("the fencing token survived the handover",
          lease_now.get("fence") == res["fence"])
    check("the handover recorded who it came from",
          lease_now.get("handed_over_from") not in (None, res["pid"]))

    # With the worker holding a live lease, a second Start must lose — this is
    # the "parent died, worker lives" case. The parent is gone (in_home exited)
    # and the worker is still running.
    r2 = in_home(h, COMMIT.format(worker=str(worker_py), to=20), timeout=90)
    got2 = [l for l in r2.stdout.splitlines() if l.startswith("REFUSED")]
    check("a second start refuses while the worker still holds the lease",
          bool(got2) and got2[0].split()[1] == "lease_unavailable")
    try:
        _os.killpg(_os.getpgid(res["pid"]), 15)
    except OSError:
        pass
    time.sleep(0.5)
(h / "logs" / "worker.lease").unlink(missing_ok=True)

# A worker that reports and is never cleared must exit rather than trade.
h = make_home(tmp, "nogo")
traded2 = h / "logs" / "traded.marker"
NOGO = '''
import json, subprocess, sys, time
from pathlib import Path
from src import start_protocol as sp
req = sp.prepare("web")
# Take the lease and spawn exactly as commit() would, then never issue the GO.
from src import start_lease
lease = start_lease.acquire(purpose="worker")
env = sp.build_child_env(Path(r"{home}"), req.config_sha256)
env["MMT_START_REQUEST_ID"] = req.request_id
env["MMT_START_FENCE"] = str(lease.fence)
env["MMT_READY_FILE"] = str(Path(r"{home}") / "logs" / "ready.json")
env["MMT_GO_FILE"] = str(Path(r"{home}") / "logs" / "go.json")
env["MMT_ACCOUNT_ID"] = req.account_id or ""
p = subprocess.Popen([sys.executable, r"{worker}"], env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
out, err = p.communicate(timeout=120)
print("EXIT " + str(p.returncode))
print("ERR " + err.replace(chr(10), " "))
'''
r = in_home(h, NOGO.format(home=str(h), worker=str(worker_py)), timeout=180)
check("a worker that is never cleared exits", "EXIT 4" in r.stdout)
check("...with go_timeout", "go_timeout" in r.stdout)
check("...and never traded", not traded2.exists())
(h / "logs" / "worker.lease").unlink(missing_ok=True)


# ── 7c. a stale fence stops trading, even in a healthy process ─────────────
print("\n7c  the fencing token gates every order")
h = make_home(tmp, "fence")
FENCE_CHECK = '''
import json, os
from src import start_protocol as sp, start_lease
os.environ["MMT_START_FENCE"] = "5"
(sp.settings.root / "logs").mkdir(parents=True, exist_ok=True)
lease = sp.settings.root / "logs" / "worker.lease"
lease.write_text(json.dumps({"pid": os.getpid(), "fence": 5, "host": "h"}))
try:
    sp.assert_may_trade("placing an order")
    print("ALLOWED-1")
except Exception as e:
    print("BLOCKED-1 " + type(e).__name__)
# Someone else took the lease while this process stayed perfectly healthy.
lease.write_text(json.dumps({"pid": os.getpid() + 1, "fence": 6, "host": "h"}))
try:
    sp.assert_may_trade("placing an order")
    print("ALLOWED-2")
except start_lease.LeaseUnavailable:
    print("BLOCKED-2")
# No token at all: not started through the protocol.
del os.environ["MMT_START_FENCE"]
try:
    sp.assert_may_trade("placing an order")
    print("ALLOWED-3")
except start_lease.LeaseUnavailable:
    print("BLOCKED-3")
'''
r = in_home(h, FENCE_CHECK)
check("the holder may trade", "ALLOWED-1" in r.stdout)
check("a process that lost the lease may not, though it is healthy",
      "BLOCKED-2" in r.stdout)
check("a process with no token may not trade", "BLOCKED-3" in r.stdout)


# ── 8. a direct launch is refused ──────────────────────────────────────────
print("\n8  bypassing the protocol")
h3 = make_home(tmp, "direct")
r = subprocess.run([sys.executable, str(worker_py)], cwd=ROOT,
                   env={k: v for k, v in os.environ.items()
                        if k in ("PATH", "HOME")} | {"MMT_HOME": str(h3),
                                                     "PYTHONPATH": str(ROOT)},
                   capture_output=True, text=True, timeout=60)
check("a worker launched directly refuses to run",
      "not_via_protocol" in (r.stderr + r.stdout))

# ── 9. the audit: what it records, and what it refuses to ──────────────────
print("\n9  audit content")
h4 = make_home(tmp, "audit")
in_home(h4, PREPARE.format(src="web"))
recs = [json.loads(l) for l in
        (h4 / "logs" / "start_audit.jsonl").read_text().splitlines()]
check("prepare is recorded", any(r["event"] == "prepare" for r in recs))
rec = recs[-1]
for f in ("request_id", "source", "build_id", "config_sha256", "db_version",
          "db_integrity", "account_ref", "effective_env", "safety",
          "executable_sha256", "result"):
    check(f"records {f}", f in rec)
check("the broker account appears only as a reference",
      str(rec.get("account_ref", "")).startswith("ref:"))
blob = json.dumps(recs)
check("no email address anywhere in the audit", "@" not in blob.replace("@sha", ""))
check("audit file is 0600",
      oct((h4 / "logs" / "start_audit.jsonl").stat().st_mode)[-3:] == "600")

FORBIDDEN = '''
from src import start_audit
for kwargs in (
    {"source":"web","result":"prepared","refusal_detail":"user tanjunxian00@gmail.com"},
    {"source":"web","result":"prepared","refusal_detail":"from 192.168.1.10"},
    # SYNTHETIC-CREDENTIALS-OK — invented fixture for the forbidden-value test
    {"source":"web","result":"prepared","refusal_detail":"key sk-abcdefghijklmnopqrstuvwx"},
    {"source":"web","result":"prepared","password":"hunter2hunter2"},
    {"source":"web","result":"prepared","user_agent":"Mozilla/5.0"},
    {"source":"web","result":"prepared","unknown_field":"x"},
    {"source":"badsource","result":"prepared"},
    {"source":"web","result":"not-a-result"},
    {"source":"web","result":"prepared","safety":{"nested":"a@b.com"}},
):
    try:
        start_audit.record(**kwargs)
        print("ACCEPTED", sorted(kwargs))
    except start_audit.AuditRefused:
        print("REFUSED")
'''
r = in_home(h4, FORBIDDEN)
refused = r.stdout.count("REFUSED")
check(f"all nine forbidden records are refused (got {refused})", refused == 9)
check("none were accepted", "ACCEPTED" not in r.stdout)

# An unwritable audit must stop the start.
h5 = make_home(tmp, "auditfail")
(h5 / "logs").chmod(0o500)
r = in_home(h5, PREPARE.format(src="web"))
check("an unwritable audit refuses the start",
      "AuditRefused" in (r.stderr + r.stdout) or "REFUSED" in r.stdout)
(h5 / "logs").chmod(0o700)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
