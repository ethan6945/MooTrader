"""Two merges racing on the same database must produce one import, not two.

Run from repo root: .venv/bin/python scripts/test_merge_concurrency.py
Builds its own databases in a temp dir.

WHY
  The batch check and the import are separate statements. If the check runs
  before the write lock is taken, two processes can both read "not yet applied",
  both pass, and both import — the archive lands in the ledger twice and every
  derived figure doubles. The window is small and entirely real: the merge is a
  manual step, and a manual step gets run twice by someone who is not sure the
  first one worked.

  So the check moved inside BEGIN IMMEDIATE. The loser blocks on the write lock
  until the winner commits, and then sees the receipt.
"""
import json
import multiprocessing as mp
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
    if cond: PASS += 1
    else: FAIL += 1


SCHEMA = """
CREATE TABLE closed_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
  symbol TEXT, qty INT, entry REAL, stop REAL, exit REAL, exit_reason TEXT,
  pnl REAL, pnl_pct REAL, r_multiple REAL, opened_at TEXT, extra TEXT);
CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT,
  symbol TEXT, gate TEXT, reason TEXT, score REAL, extra TEXT);
CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, week TEXT,
  day TEXT, invested REAL, budget REAL, unrealized_pnl REAL,
  realized_pnl_total REAL, total_pnl REAL, positions_count INT, symbols TEXT,
  timeframe TEXT);
CREATE TABLE open_trades (symbol TEXT PRIMARY KEY, qty INT, entry_price REAL,
  stop_loss REAL, take_profit REAL, atr REAL, half_closed INT DEFAULT 0,
  buy_order_id TEXT, stop_order_id TEXT, tp_order_id TEXT, opened_at TEXT,
  extra TEXT);
CREATE TABLE kv_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def build(path, rows, start_day):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for i in range(rows):
        ts = f"2026-{start_day}-{i+1:02d}T10:00:00"
        c.execute("INSERT INTO closed_trades (ts,symbol,qty,entry,stop,exit,pnl,"
                  "opened_at,extra) VALUES (?,?,?,?,?,?,?,?,NULL)",
                  (ts, f"S{i}", 1, 10.0, 9.0, 11.0, 1.0, ts))
        c.execute("INSERT INTO audit (ts,action) VALUES (?, 'buy')", (ts,))
        c.execute("INSERT INTO history (ts) VALUES (?)", (ts,))
    c.execute("INSERT INTO kv_state (key,value) VALUES ('budget_usd','10000')")
    c.commit(); c.close()


def worker(repo, app, q):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ml", ROOT / "scripts" / "merge_ledgers.py")
    ml = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ml)
    ml.REPO_DB = Path(repo)
    ml.APP_DB = Path(app)
    try:
        ml.apply_merge()
        q.put("applied")
    except Exception as e:
        q.put(f"refused: {type(e).__name__}: {str(e)[:80]}")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="mmt-merge-race-"))
    repo, app = tmp / "repo.db", tmp / "app.db"
    build(str(repo), 30, "06")      # archive: June
    build(str(app), 6, "08")        # authoritative: August

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=worker, args=(str(repo), str(app), q))
             for _ in range(2)]
    for p in procs: p.start()
    for p in procs: p.join(60)
    results = [q.get() for _ in procs]

    applied = sum(1 for r in results if r == "applied")
    refused = sum(1 for r in results if r.startswith("refused"))
    print(f"  two racing merges -> {results}")
    check("exactly one merge applied", applied == 1)
    check("the other was refused, not crashed on a half-write", refused == 1)

    c = sqlite3.connect(f"file:{app}?mode=ro", uri=True)
    n = c.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
    check(f"ledger has 36 trades, not 66 (got {n})", n == 36)
    a = c.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    check(f"audit has 36 rows, not 66 (got {a})", a == 36)
    tagged = c.execute("SELECT COUNT(*) FROM closed_trades WHERE extra LIKE ?",
                       (f"%{'repo-ledger'}%",)).fetchone()[0]
    check(f"exactly 30 rows carry the merge tag (got {tagged})", tagged == 30)
    r = c.execute("SELECT value FROM kv_state WHERE key='ledger_merge'").fetchone()
    check("one receipt written", r is not None)
    if r:
        rows = json.loads(r[0]).get("rows", {})
        check("receipt records a single import", rows.get("closed_trades") == 30)

    # And a serial re-run must still refuse.
    q2 = ctx.Queue()
    p = ctx.Process(target=worker, args=(str(repo), str(app), q2))
    p.start(); p.join(60)
    check("a later re-run is refused too", q2.get().startswith("refused"))
    n2 = c.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
    check("ledger unchanged by the re-run", n2 == 36)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
