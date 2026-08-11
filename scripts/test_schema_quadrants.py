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


V3_TABLES = {"audit", "closed_trades", "history", "kv_state", "open_trades"}
V4_ONLY = {"accounts", "execution_sessions"}


def snapshot(db: Path) -> dict:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            "version": c.execute("PRAGMA user_version").fetchone()[0],
            "tables": {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")},
            "kv_cols": [r[1] for r in c.execute("PRAGMA table_info(kv_state)")],
            "closed": c.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0],
            "audit": c.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
        }
    finally:
        c.close()


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
    c.commit(); c.close()


tmp = Path(tempfile.mkdtemp(prefix="mmt-quadrants-"))

# ── 1. fresh ────────────────────────────────────────────────────────────────
print("1  fresh database")
home = tmp / "fresh"; (home / "data").mkdir(parents=True); (home / "logs").mkdir()
r = open_db(home, allow=False)
db = home / "data" / "trader.db"
check("created without needing the upgrade flag", db.exists() and r.returncode == 0)
s = snapshot(db)
check(f"at the current version (v{s['version']})", s["version"] == 4)
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
check(f"now v4 (got v{after['version']})", after["version"] == 4)
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
check("version unchanged", again["version"] == first["version"] == 4)
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

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
