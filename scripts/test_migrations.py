"""Every schema version this project has ever written, upgraded to current.

Run from repo root: .venv/bin/python scripts/test_migrations.py
No broker. Each fixture is built from the REAL schema, not from a hand-written
smaller one — a fixture you invent tests the fixture.

WHY

  The authoritative database on this machine is v3 and the code is v7. That
  upgrade runs exactly once, against the only copy of thirty-seven closed
  trades, and if it is wrong there is no second attempt.

  The dispatch had a hole this file exists to keep shut: it ran migrations up
  to v5 and then stamped user_version = SCHEMA_VERSION. _migrate_v7 was defined
  and called from nowhere. A v3 database survived that by accident — SCHEMA
  creates the orders table outright when it is missing — but a v6 database,
  which HAS an orders table missing three columns, could not be upgraded at
  all, because CREATE TABLE IF NOT EXISTS does not add columns.

  It failed loudly and rolled back rather than corrupting anything, which is
  the safe direction and is not the same as working. The general fault is
  structural: the version the code stamps and the migrations it actually runs
  were two independent facts that happened to agree for one input.
"""
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond:
        PASS += 1
    else:
        FAIL += 1


def fresh_home():
    home = tempfile.mkdtemp(prefix="mmt-mig-")
    (Path(home) / "data").mkdir(parents=True, exist_ok=True)
    (Path(home) / "logs").mkdir(parents=True, exist_ok=True)
    return home


def upgrade(home):
    """Run the real upgrade in a subprocess — db caches its init per process."""
    import subprocess
    env = dict(os.environ, MMT_HOME=home, MMT_ALLOW_SCHEMA_UPGRADE="1")
    return subprocess.run(
        [sys.executable, "-c", "from src import db; db._ensure_initialised()"],
        cwd=str(ROOT), env=env, capture_output=True, text=True)


def cols(path, table="orders"):
    c = sqlite3.connect(path)
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        c.close()


def uv(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


from src.db import SCHEMA, SCHEMA_VERSION                      # noqa: E402

V7_COLUMNS = ("applied_qty", "applied_notional", "intent_key")


FIXTURES = ROOT / "scripts" / "fixtures"


def build_v3(path):
    """The REAL v3 shape, captured from the authoritative database.

    An earlier version of this file hand-wrote a five-table approximation. It
    passed the assertions that mattered least and failed on `audit.action` — a
    column the invented fixture did not have and the real one does. Checked in
    as DDL rather than reconstructed, because the whole point of this test is
    the upgrade meeting a database it did not create.
    """
    c = sqlite3.connect(path)
    c.executescript((FIXTURES / "schema_v3.sql").read_text())
    c.commit()
    c.close()


def build_v6(path):
    """v6 = the current schema minus the three v7 columns and every index that
    references them. Derived from SCHEMA so it cannot drift away from v6."""
    s = SCHEMA
    for col in V7_COLUMNS:
        s = re.sub(rf"^\s*{col}\s+[^,\n]+,\s*$", "", s, flags=re.M)
    # Any index over a column that does not exist yet must go with it.
    s = re.sub(r"CREATE\s+(UNIQUE\s+)?INDEX[^;]*?(" + "|".join(V7_COLUMNS)
               + r")[^;]*;", "", s, flags=re.S | re.I)
    c = sqlite3.connect(path)
    c.executescript(s)
    c.execute("PRAGMA user_version = 6")
    # A terminal order whose fills the OLD code already applied. If the v7
    # backfill misses it, the settler replays a filled entry on first run and
    # opens a position that was already opened.
    c.execute("INSERT INTO orders (client_order_id, account_id, session_id, "
              "symbol, side, kind, requested_qty, state, filled_qty, "
              "avg_fill_price, created_at) VALUES ('c1','a1','s1','AAPL',"
              "'BUY','ENTRY',10,'FILLED',10,50.0,'2026-08-01T00:00:00Z')")
    c.commit()
    c.close()


print(f"code is at schema v{SCHEMA_VERSION}\n")

# ── 1. the version stamped is the version reached ──────────────────────────
print("1  every migration up to the stamped version actually runs")
import src.db as _db                                           # noqa: E402
import inspect                                                 # noqa: E402

src_txt = inspect.getsource(_db._ensure_initialised)
defined = {int(m) for m in re.findall(r"def _migrate_v(\d+)", Path(_db.__file__).read_text())}
called = {int(m) for m in re.findall(r"_migrate_v(\d+)\(c\)", src_txt)}
check(f"every _migrate_vN defined is dispatched (defined {sorted(defined)}, "
      f"called {sorted(called)})", defined <= called)
check("nothing is dispatched that does not exist", called <= defined)
check(f"the highest migration reaches the stamped version {SCHEMA_VERSION}",
      max(called) >= max(defined))

# ── 2. the real live database shape: v3 ────────────────────────────────────
print("\n2  v3 → current (the shape of the authoritative database)")
home = fresh_home()
p = Path(home) / "data" / "trader.db"
build_v3(p)
c = sqlite3.connect(p)
c.execute("INSERT INTO closed_trades (ts, symbol, qty, entry, stop, exit, pnl) "
          "VALUES ('2026-08-01T00:00:00Z','AAPL',10,50.0,48.0,51.25,12.5)")
c.execute("INSERT INTO kv_state (key, value) VALUES ('budget_usd', '10000')")
c.commit()
c.close()
r = upgrade(home)
check("the upgrade succeeds", r.returncode == 0)
check(f"...and stamps v{SCHEMA_VERSION}", uv(p) == SCHEMA_VERSION)
check("...creating the orders table complete", set(V7_COLUMNS) <= cols(p))
check("...with the one-live-order-per-intent index", 1 == sqlite3.connect(p).execute(
    "SELECT count(*) FROM sqlite_master WHERE name='idx_orders_one_live_per_intent'"
).fetchone()[0])
kept = sqlite3.connect(p).execute("SELECT count(*) FROM closed_trades").fetchone()[0]
check("...and the closed trades survive", kept == 1)

# ── 3. v6 → current: the case that could not upgrade at all ────────────────
print("\n3  v6 → current (an orders table missing only the v7 columns)")
home = fresh_home()
p = Path(home) / "data" / "trader.db"
build_v6(p)
check("the fixture really is missing the v7 columns",
      not (set(V7_COLUMNS) & cols(p)))
r = upgrade(home)
check("the upgrade succeeds", r.returncode == 0)
check(f"...and stamps v{SCHEMA_VERSION}", uv(p) == SCHEMA_VERSION)
check("...adding all three v7 columns", set(V7_COLUMNS) <= cols(p))
row = sqlite3.connect(p).execute(
    "SELECT applied_qty, applied_notional FROM orders WHERE client_order_id='c1'"
).fetchone()
check("...and back-filling an already-applied terminal order",
      row is not None and abs(row[0] - 10) < 1e-9 and abs(row[1] - 500.0) < 1e-6)

# ── 4. a failed upgrade leaves the version it started at ───────────────────
print("\n4  a broken upgrade rolls back rather than half-landing")
home = fresh_home()
p = Path(home) / "data" / "trader.db"
c = sqlite3.connect(p)
# An orders table shaped like nothing this code has ever written: the upgrade
# must refuse it whole, not stamp a version it did not reach.
c.executescript("CREATE TABLE orders (nonsense TEXT); PRAGMA user_version = 6;")
c.commit()
c.close()
r = upgrade(home)
check("the upgrade fails", r.returncode != 0)
check("...and the database is still at the version it started at", uv(p) == 6)
check("...with no v7 columns invented", not (set(V7_COLUMNS) & cols(p)))

# ── 5. the upgrade is off unless asked for ─────────────────────────────────
print("\n5  a schema change is never a side effect of reading state")
home = fresh_home()
p = Path(home) / "data" / "trader.db"
c = sqlite3.connect(p)
c.executescript("CREATE TABLE kv_state (key TEXT, value TEXT); "
                "PRAGMA user_version = 3;")
c.commit()
c.close()
import subprocess                                              # noqa: E402
env = dict(os.environ, MMT_HOME=home)
env.pop("MMT_ALLOW_SCHEMA_UPGRADE", None)
subprocess.run([sys.executable, "-c", "from src import db; db._ensure_initialised()"],
               cwd=str(ROOT), env=env, capture_output=True, text=True)
check("without MMT_ALLOW_SCHEMA_UPGRADE the version does not move", uv(p) == 3)

# ── 6. re-running the upgrade changes nothing ──────────────────────────────
print("\n6  the upgrade is idempotent")
home = fresh_home()
p = Path(home) / "data" / "trader.db"
build_v6(p)
upgrade(home)
first = Path(str(p) + ".firstpass")
shutil.copy(p, first)
before = sqlite3.connect(p).execute(
    "SELECT applied_qty, applied_notional FROM orders WHERE client_order_id='c1'").fetchone()
r = upgrade(home)
after = sqlite3.connect(p).execute(
    "SELECT applied_qty, applied_notional FROM orders WHERE client_order_id='c1'").fetchone()
check("a second upgrade succeeds", r.returncode == 0)
check("...and does not double-count the backfill", before == after)
check("...leaving the version where it was", uv(p) == SCHEMA_VERSION)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
