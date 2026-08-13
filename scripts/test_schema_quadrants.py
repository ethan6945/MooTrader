"""Four ways a database gets opened, and what each is allowed to do to it.

Run from repo root: .venv/bin/python scripts/test_schema_quadrants.py
Builds every database it touches in a temp dir. Never opens the real one.

    1  fresh          no file yet            -> create at the current version
    2  v3, refused    upgrade not permitted  -> ZERO schema writes, still v3
    3  v3, allowed    upgrade permitted      -> v4, data intact
    4  v4, reopened   already current        -> no-op, nothing re-imported

Quadrant 2 is the one that matters and the one that was broken. The guard was
placed after executescript(SCHEMA), and SCHEMA carries the v4 tables — so
refusing to upgrade a v3 database still created `accounts` and
`execution_sessions` inside it. The log said it had refused. The file said
otherwise: user_version=3, holding half of v4.

That combination is worse than either outcome alone. The packaged app's frozen
backend checks nothing and would have kept writing to a database whose shape had
changed underneath it, and the next tool to look at the version would have been
told v3 and believed it.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


# Read from the code under test rather than restated here. Every bump used to
# mean four hand-edits, and a stale literal in a schema test does not fail
# loudly — it asserts that the migration produced the version it produced last
# release, which is the one thing this suite is supposed to notice.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.db import SCHEMA_VERSION as SCHEMA_V
V3_TABLES = {"audit", "closed_trades", "history", "kv_state", "open_trades"}
V4_ONLY = {"accounts", "execution_sessions"}


def snapshot(db: Path) -> dict:
    import hashlib
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = {
            "version": c.execute("PRAGMA user_version").fetchone()[0],
            "tables": {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")},
            "kv_cols": [r[1] for r in c.execute("PRAGMA table_info(kv_state)")],
            "closed": c.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0],
            "audit": c.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
            "journal": c.execute("PRAGMA journal_mode").fetchone()[0],
        }
    finally:
        c.close()
    # Hash and sidecars, so "no schema changes" can be checked against the file
    # rather than against the schema alone. journal_mode is persistent: forcing
    # WAL on a rollback-journal database rewrites its header and creates
    # -wal/-shm. A refused open used to do exactly that, on every read.
    out["sha"] = hashlib.sha256(db.read_bytes()).hexdigest()
    out["sidecars"] = sorted(p.name[len(db.name):] for p in
                             db.parent.glob(db.name + "-*"))
    return out


def open_db(home: Path, allow: bool, code: str = "from src import db; db.get_state()"):
    """Open a database the way a real process does — a separate interpreter, so
    the module-level _initialised cache cannot mask a second open."""
    env = dict(os.environ, MMT_HOME=str(home), PYTHONPATH=str(ROOT))
    env.pop("MMT_ALLOW_SCHEMA_UPGRADE", None)
    if allow:
        env["MMT_ALLOW_SCHEMA_UPGRADE"] = "1"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    return r


def make_v3(path: Path):
    """A database shaped exactly like the authoritative one: v3, with data."""
    c = sqlite3.connect(str(path))
    c.executescript("""
        CREATE TABLE open_trades (symbol TEXT PRIMARY KEY, qty INTEGER NOT NULL,
          entry_price REAL NOT NULL, stop_loss REAL NOT NULL,
          take_profit REAL NOT NULL, atr REAL, half_closed INTEGER DEFAULT 0,
          buy_order_id TEXT, stop_order_id TEXT, tp_order_id TEXT,
          opened_at TEXT NOT NULL, high_water REAL, low_water REAL,
          ml_proba_entry REAL, strategy TEXT, extra TEXT);
        CREATE TABLE kv_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
          action TEXT NOT NULL, symbol TEXT, gate TEXT, reason TEXT, score REAL,
          extra TEXT);
        CREATE TABLE closed_trades (id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL, symbol TEXT NOT NULL, qty INTEGER NOT NULL,
          entry REAL NOT NULL, stop REAL NOT NULL, exit REAL NOT NULL,
          exit_reason TEXT, pnl REAL, pnl_pct REAL, r_multiple REAL,
          opened_at TEXT, mfe_pct REAL, mae_pct REAL, ml_proba_entry REAL,
          strategy TEXT, extra TEXT);
        CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
          week TEXT, day TEXT, invested REAL, budget REAL, unrealized_pnl REAL,
          realized_pnl_total REAL, total_pnl REAL, positions_count INTEGER,
          symbols TEXT, timeframe TEXT);
        PRAGMA user_version = 3;
    """)
    for i in range(5):
        c.execute("INSERT INTO closed_trades (ts,symbol,qty,entry,stop,exit,pnl) "
                  "VALUES (?,?,1,10,9,11,1.0)", (f"2026-07-{i+1:02d}T10:00:00", f"S{i}"))
        c.execute("INSERT INTO audit (ts,action) VALUES (?,'buy')",
                  (f"2026-07-{i+1:02d}T10:00:00",))
    c.execute("INSERT INTO kv_state (key,value) VALUES ('budget_usd','10000')")
    c.execute("INSERT INTO kv_state (key,value) VALUES ('ai_provider','\"deepseek\"')")
    # Leave it on a rollback journal with no sidecars. A WAL database already
    # has -wal/-shm, so testing against one hides the conversion entirely: the
    # hash does not move and neither does the mode. This is the shape that
    # exposes it.
    c.execute("PRAGMA journal_mode=DELETE")
    c.commit(); c.close()
    for side in path.parent.glob(path.name + "-*"):
        side.unlink()


tmp = Path(tempfile.mkdtemp(prefix="mmt-quadrants-"))

# ── 1. fresh ────────────────────────────────────────────────────────────────
print("1  fresh database")
home = tmp / "fresh"; (home / "data").mkdir(parents=True); (home / "logs").mkdir()
r = open_db(home, allow=False)
db = home / "data" / "trader.db"
check("created without needing the upgrade flag", db.exists() and r.returncode == 0)
s = snapshot(db)
check(f"at the current version (v{s['version']})", s["version"] == SCHEMA_V)
check("has the v3 tables", V3_TABLES <= s["tables"])
check("has the v4 tables", V4_ONLY <= s["tables"])
check("kv_state is account-scoped", "account_id" in s["kv_cols"])

# ── 2. v3, upgrade refused ──────────────────────────────────────────────────
print("\n2  v3 database, upgrade NOT permitted")
home = tmp / "v3refused"; (home / "data").mkdir(parents=True); (home / "logs").mkdir()
db = home / "data" / "trader.db"
make_v3(db)
before = snapshot(db)
r = open_db(home, allow=False)
after = snapshot(db)
check("open succeeds (it must still be usable)", r.returncode == 0)
check("refusal is announced", "NOT applied" in (r.stderr + r.stdout))
check(f"still v3 (got v{after['version']})", after["version"] == 3)
check("NO v4 tables created", not (V4_ONLY & after["tables"]))
check("table set completely unchanged", after["tables"] == before["tables"])
check("kv_state shape unchanged", after["kv_cols"] == before["kv_cols"])
check("data untouched", (after["closed"], after["audit"]) == (before["closed"], before["audit"]))
check(f"journal mode unchanged ({before['journal']})", after["journal"] == before["journal"])
check("file hash unchanged", after["sha"] == before["sha"])
check(f"no -wal/-shm sidecars created (got {after['sidecars']})",
      after["sidecars"] == before["sidecars"])
# Reading and writing state must still work on v3.
r2 = open_db(home, allow=False, code=(
    "from src import db;"
    "assert db.get_state()['budget_usd'] == 10000, 'read failed';"
    "db.update_state({'probe': 1});"
    "assert db.get_state()['probe'] == 1, 'write failed';"
    "print('rw-ok')"))
check("v3 database stays readable and writable", "rw-ok" in r2.stdout)
check("still v3 after a write", snapshot(db)["version"] == 3)

# ── 3. v3, upgrade permitted ────────────────────────────────────────────────
print("\n3  v3 database, upgrade permitted")
home = tmp / "v3allowed"; (home / "data").mkdir(parents=True); (home / "logs").mkdir()
db = home / "data" / "trader.db"
make_v3(db)
before = snapshot(db)
r = open_db(home, allow=True)
after = snapshot(db)
check("upgrade succeeds", r.returncode == 0)
check(f"now v{SCHEMA_V} (got v{after['version']})", after["version"] == SCHEMA_V)
check("v4 tables present", V4_ONLY <= after["tables"])
check("kv_state gained the account scope", "account_id" in after["kv_cols"])
check("closed_trades preserved", after["closed"] == before["closed"])
check("audit preserved", after["audit"] == before["audit"])
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
check("every closed trade got an account", c.execute(
    "SELECT COUNT(*) FROM closed_trades WHERE account_id IS NULL").fetchone()[0] == 0)
check("exactly one account minted", c.execute(
    "SELECT COUNT(*) FROM accounts").fetchone()[0] == 1)
check("history rows are marked pre-identity", c.execute(
    "SELECT COUNT(*) FROM closed_trades WHERE migrated_from IS NOT NULL"
).fetchone()[0] == before["closed"])
c.close()

# ── 4. v4 reopened ──────────────────────────────────────────────────────────
print("\n4  existing v4 database, reopened")
first = snapshot(db)
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
acct_before = c.execute("SELECT account_id FROM accounts").fetchone()[0]
kv_before = c.execute("SELECT COUNT(*) FROM kv_state").fetchone()[0]
c.close()
for i in (1, 2):
    r = open_db(home, allow=(i == 1))     # with and without the flag
    check(f"reopen #{i} succeeds", r.returncode == 0)
again = snapshot(db)
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
check("version unchanged", again["version"] == first["version"] == SCHEMA_V)
check("no rows duplicated", (again["closed"], again["audit"]) ==
      (first["closed"], first["audit"]))
check("no second account minted", c.execute(
    "SELECT COUNT(*) FROM accounts").fetchone()[0] == 1)
check("account id is stable", c.execute(
    "SELECT account_id FROM accounts").fetchone()[0] == acct_before)
check("kv_state not duplicated", c.execute(
    "SELECT COUNT(*) FROM kv_state").fetchone()[0] == kv_before)
check("no key exists in two scopes at once", c.execute(
    "SELECT COUNT(*) FROM (SELECT key FROM kv_state GROUP BY key HAVING COUNT(*) > 1)"
).fetchone()[0] == 0)
c.close()

# ── 5. fresh database with legacy JSON to import ────────────────────────────
# _migrate_from_json runs on a fresh database and loads the pre-SQLite files.
# On v4 those rows and keys have to land under the SIMULATE account, not in the
# global scope — global state is visible to EVERY account, so a paper budget and
# a paper realized PnL parked there would be read by a REAL account as its own.
print("\n5  fresh database importing legacy JSON")
home = tmp / "legacy"
(home / "data").mkdir(parents=True); (home / "logs").mkdir()
import json as _json
(home / "data" / "state.json").write_text(_json.dumps({
    "budget_usd": 4500.0, "realized_pnl_total": -625.47, "peak_equity": 4500.0,
    "halted": False, "ai_provider": "deepseek",
}))
(home / "data" / "trades.jsonl").write_text("\n".join(_json.dumps({
    "ts": f"2026-06-{i+1:02d}T10:00:00", "symbol": f"L{i}", "qty": 1,
    "entry": 10.0, "stop": 9.0, "exit": 9.5, "pnl": -0.5,
    "exit_reason": "SL", "opened_at": f"2026-06-{i+1:02d}T09:00:00",
}) for i in range(4)))

r = open_db(home, allow=False)
db = home / "data" / "trader.db"
check("fresh + legacy import succeeds", r.returncode == 0 and db.exists())
s = snapshot(db)
check(f"built at v{SCHEMA_V} directly", s["version"] == SCHEMA_V)

c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
accounts = [dict(x) for x in c.execute("SELECT * FROM accounts")]
check(f"exactly one account exists (got {len(accounts)})", len(accounts) == 1)
check("and it is the SIMULATE one",
      bool(accounts) and accounts[0]["trade_env"] == "SIMULATE")
sim = accounts[0]["account_id"] if accounts else None

imported = c.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
check(f"legacy trades imported (got {imported})", imported == 4)
check("every imported trade belongs to the SIMULATE account", c.execute(
    "SELECT COUNT(*) FROM closed_trades WHERE account_id IS NOT ?",
    (sim,)).fetchone()[0] == 0)

# The money keys must be account-scoped; only software-level keys may be global.
scopes = {r["key"]: r["account_id"] for r in
          c.execute("SELECT key, account_id FROM kv_state")}
for k in ("budget_usd", "realized_pnl_total", "peak_equity"):
    check(f"{k} is scoped to the account, not global", scopes.get(k) == sim)
check("ai_provider stays global (it is not account state)",
      scopes.get("ai_provider") == "")
c.close()

# The property that matters: a REAL account must see none of it.
probe = (
    "import os, json;"
    "from src.config import settings;"
    "object.__setattr__(settings, 'moo_trade_env', 'REAL');"
    "from src import db, identity;"
    "identity.reset_cache();"
    "st = db.get_state();"
    "print(json.dumps({k: st.get(k) for k in "
    "['budget_usd','realized_pnl_total','peak_equity','ai_provider']}))"
)
r = open_db(home, allow=False, code=probe)
seen = {}
for line in r.stdout.splitlines():
    if line.startswith("{"):
        seen = _json.loads(line)
check("REAL sees no paper budget", seen.get("budget_usd") is None)
check("REAL sees no paper realized PnL", seen.get("realized_pnl_total") is None)
check("REAL sees no paper drawdown peak", seen.get("peak_equity") is None)
check("REAL still sees global software settings",
      seen.get("ai_provider") == "deepseek")

c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
check("the REAL read did not mint a second SIMULATE account", c.execute(
    "SELECT COUNT(*) FROM accounts WHERE trade_env='SIMULATE'").fetchone()[0] == 1)
c.close()


# ── 6. v4 -> v5: the open_trades rebuild ────────────────────────────────────
#
# This is a table rebuild (SQLite cannot ALTER a primary key), so it is the one
# migration in this file that can lose rows. The authoritative database is v3
# and will arrive here through v4, carrying live positions.
print("\n6  v4 -> v5 rebuild of open_trades")


def make_v4_open_trades(path: Path, rows):
    """A v4-shaped open_trades: account_id present, symbol the only key."""
    c = sqlite3.connect(str(path))
    c.executescript("""
        CREATE TABLE open_trades (symbol TEXT PRIMARY KEY, qty INTEGER NOT NULL,
          entry_price REAL NOT NULL, stop_loss REAL NOT NULL,
          take_profit REAL NOT NULL, atr REAL,
          half_closed INTEGER NOT NULL DEFAULT 0, buy_order_id TEXT,
          stop_order_id TEXT, tp_order_id TEXT, opened_at TEXT NOT NULL,
          high_water REAL, low_water REAL, ml_proba_entry REAL, strategy TEXT,
          extra TEXT, account_id TEXT, opened_session_id TEXT);
        CREATE TABLE kv_state (account_id TEXT NOT NULL DEFAULT '',
          key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (account_id, key));
        CREATE TABLE accounts (account_id TEXT PRIMARY KEY,
          trade_env TEXT NOT NULL CHECK (trade_env IN ('SIMULATE','REAL')),
          broker_acc_id TEXT, label TEXT, created_at TEXT NOT NULL);
        CREATE TABLE execution_sessions (session_id TEXT PRIMARY KEY,
          account_id TEXT NOT NULL, trade_env TEXT NOT NULL, pid INTEGER,
          host TEXT, started_at TEXT, ended_at TEXT, end_reason TEXT,
          auth_mode TEXT, authorized_at TEXT, authorized_by TEXT);
        CREATE TABLE closed_trades (id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL, symbol TEXT NOT NULL, qty INTEGER NOT NULL,
          entry REAL NOT NULL, stop REAL NOT NULL, exit REAL NOT NULL,
          exit_reason TEXT, pnl REAL, pnl_pct REAL, r_multiple REAL,
          opened_at TEXT, mfe_pct REAL, mae_pct REAL, ml_proba_entry REAL,
          strategy TEXT, extra TEXT, account_id TEXT, session_id TEXT,
          migrated_from TEXT);
        CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL, action TEXT NOT NULL, symbol TEXT, gate TEXT,
          reason TEXT, score REAL, extra TEXT, account_id TEXT, session_id TEXT);
        CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL, week TEXT, day TEXT, invested REAL, budget REAL,
          unrealized_pnl REAL, realized_pnl_total REAL, total_pnl REAL,
          positions_count INTEGER, symbols TEXT, timeframe TEXT,
          account_id TEXT);
    """)
    c.execute("INSERT INTO accounts VALUES ('acct-sim','SIMULATE',NULL,'p',"
              "'2026-01-01')")
    for sym, qty, acct in rows:
        c.execute("INSERT INTO open_trades (symbol,qty,entry_price,stop_loss,"
                  "take_profit,opened_at,account_id) VALUES (?,?,10,9,11,'t',?)",
                  (sym, qty, acct))
    c.execute("PRAGMA user_version = 4")
    c.commit(); c.close()


home = tmp / "v4tov5"; (home / "data").mkdir(parents=True); (home / "logs").mkdir()
db = home / "data" / "trader.db"
# One normally-stamped position and one that a post-v4 writer left unattributed.
make_v4_open_trades(db, [("AAPL", 100, "acct-sim"), ("MSFT", 7, None)])
open_db(home, allow=True)
after = snapshot(db)
check(f"now v{SCHEMA_V} (got v{after['version']})", after["version"] == SCHEMA_V)

c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
kept = {r["symbol"]: r for r in c.execute("SELECT * FROM open_trades")}
pk = {r[1] for r in c.execute("PRAGMA table_info(open_trades)") if r[5]}
leftover = {r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE name LIKE 'open_trades_v%'")}
c.close()
check("both positions survived the rebuild", set(kept) == {"AAPL", "MSFT"})
check("quantities are unchanged", kept["AAPL"]["qty"] == 100
      and kept["MSFT"]["qty"] == 7)
# A NULL account_id here can only come from code that wrote between the two
# migrations. Dropping the row would be dropping a position that is real at the
# broker, so it is adopted by the local account instead.
check("the unattributed position was adopted, not dropped",
      kept["MSFT"]["account_id"] == "acct-sim")
check("the key is now (account_id, symbol)", pk == {"account_id", "symbol"})
check("the rebuild left no scratch table behind", not leftover)

# The one case where guessing would be worse than stopping: the same symbol
# twice under one account cannot be keyed, and picking a winner automatically
# would discard a real position.
#
# A v4 database cannot produce this on its own — symbol is its primary key, so
# (account, symbol) is unique for free. The guard is for a database that arrived
# some other way: hand-repaired, reassembled from a backup, or written by a tool
# that built the table itself. That is not hypothetical here; this ledger was
# reassembled from two sources three weeks ago. So the fixture drops the key
# rather than pretending v4 could have made the row.
home = tmp / "v5dupe"; (home / "data").mkdir(parents=True); (home / "logs").mkdir()
db2 = home / "data" / "trader.db"
make_v4_open_trades(db2, [("AAPL", 100, "acct-sim")])
c = sqlite3.connect(str(db2))
c.executescript("""
    ALTER TABLE open_trades RENAME TO ot_keyed;
    CREATE TABLE open_trades (symbol TEXT NOT NULL, qty INTEGER NOT NULL,
      entry_price REAL NOT NULL, stop_loss REAL NOT NULL,
      take_profit REAL NOT NULL, atr REAL,
      half_closed INTEGER NOT NULL DEFAULT 0, buy_order_id TEXT,
      stop_order_id TEXT, tp_order_id TEXT, opened_at TEXT NOT NULL,
      high_water REAL, low_water REAL, ml_proba_entry REAL, strategy TEXT,
      extra TEXT, account_id TEXT, opened_session_id TEXT);
    INSERT INTO open_trades SELECT * FROM ot_keyed;
    DROP TABLE ot_keyed;
    INSERT INTO open_trades (symbol,qty,entry_price,stop_loss,take_profit,
      opened_at,account_id) VALUES ('AAPL',55,12,9,11,'t','acct-sim');
    PRAGMA user_version = 4;
""")
c.commit(); c.close()
def open_count(path: Path) -> int:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return c.execute("SELECT COUNT(*) FROM open_trades").fetchone()[0]
    finally:
        c.close()


before_dupe = open_count(db2)
r = open_db(home, allow=True)
check("a duplicate (account, symbol) refuses the migration",
      snapshot(db2)["version"] == 4)
check("...and leaves both positions in place", open_count(db2) == before_dupe == 2)


# ── 7. fault injection: a migration that dies partway ───────────────────────
#
# The v5 rebuild is the one migration that can lose data: SQLite cannot ALTER a
# primary key, so the table is renamed, recreated and copied. Every point in
# that sequence is a point the process can die at — power, SIGKILL, a full disk.
#
# It DID lose data before this. c.executescript() issues an implicit COMMIT
# before running, so the original rebuild committed in the middle of itself: a
# crash right after left an empty, correctly-keyed open_trades beside a full
# open_trades_v4, on disk, permanently. The early-return guard then saw the new
# key and skipped the migration on every later run, so the positions stayed
# hidden in the scratch table and the bot came up believing it held nothing —
# while the broker still held everything.
print("\n7  a migration interrupted at every step")

CRASH_AT = '''
import os, sqlite3, sys
sys.path.insert(0, {root!r})
from src import db

# Die at the Nth write of the upgrade. sqlite3's trace callback fires for every
# statement, which is a fault injector that needs no hooks in the code itself.
target = int(os.environ["CRASH_AFTER"])
seen = [0]
real_connect = sqlite3.connect
def traced(*a, **kw):
    conn = real_connect(*a, **kw)
    def trace(stmt):
        s = stmt.strip().upper()
        if s.startswith(("ALTER", "CREATE TABLE", "INSERT", "DROP", "UPDATE")):
            seen[0] += 1
            if seen[0] == target:
                os._exit(9)      # no unwinding, no cleanup: a real crash
    conn.set_trace_callback(trace)
    return conn
sqlite3.connect = traced
db._ensure_initialised()
print("SURVIVED")
'''

def positions_in(path: Path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return {r["symbol"]: r["qty"] for r in c.execute(
            "SELECT symbol, qty FROM open_trades")}
    finally:
        c.close()


ORIGINAL = {"AAPL": 100, "MSFT": 7, "NVDA": 42}
crashed_at_least_once = False
survivors = 0
for step in range(1, 13):
    home = tmp / f"crash{step}"
    (home / "data").mkdir(parents=True); (home / "logs").mkdir()
    dbp = home / "data" / "trader.db"
    make_v4_open_trades(dbp, [(s, q, "acct-sim") for s, q in ORIGINAL.items()])

    env = dict(os.environ, MMT_HOME=str(home), PYTHONPATH=str(ROOT),
               MMT_ALLOW_SCHEMA_UPGRADE="1", CRASH_AFTER=str(step))
    r = subprocess.run([sys.executable, "-c", CRASH_AT.format(root=str(ROOT))],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode == 9:
        crashed_at_least_once = True
    elif "SURVIVED" in r.stdout:
        survivors += 1

    # Whatever happened, reopening must produce every position exactly once.
    env2 = dict(env); env2["CRASH_AFTER"] = "0"      # no injection this time
    r2 = subprocess.run([sys.executable, "-c", CRASH_AT.format(root=str(ROOT))],
                        cwd=ROOT, env=env2, capture_output=True, text=True)
    ok = "SURVIVED" in r2.stdout
    got = positions_in(dbp) if ok else {}
    if not ok:
        print("        " + (r2.stderr.strip().splitlines() or ["?"])[-1][:150])
    check(f"crash after write {step}: recovers with all positions intact",
          ok and got == ORIGINAL)

check("the injector actually interrupted something", crashed_at_least_once)

# And the specific shape that used to be permanent: a scratch table left behind
# with the rows in it, next to an empty correctly-keyed table.
home = tmp / "orphaned_scratch"
(home / "data").mkdir(parents=True); (home / "logs").mkdir()
dbp = home / "data" / "trader.db"
make_v4_open_trades(dbp, [(s, q, "acct-sim") for s, q in ORIGINAL.items()])
c = sqlite3.connect(str(dbp))
c.execute("ALTER TABLE open_trades RENAME TO open_trades_v4")
c.execute(__import__("importlib").import_module("src.db").OPEN_TRADES_DDL)
c.commit(); c.close()                       # exactly the committed half-state
r = subprocess.run([sys.executable, "-c", CRASH_AT.format(root=str(ROOT))],
                   cwd=ROOT, env=dict(os.environ, MMT_HOME=str(home),
                                      PYTHONPATH=str(ROOT),
                                      MMT_ALLOW_SCHEMA_UPGRADE="1",
                                      CRASH_AFTER="0"),
                   capture_output=True, text=True)
check("an orphaned scratch table is recovered, not skipped past",
      "SURVIVED" in r.stdout and positions_in(dbp) == ORIGINAL)
c = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
check("...and the scratch table is gone afterwards",
      not c.execute("SELECT name FROM sqlite_master WHERE "
                    "name='open_trades_v4'").fetchone())
c.close()

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
