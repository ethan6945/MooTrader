"""Boundaries of the ledger-quality migration.

Run from repo root: .venv/bin/python scripts/test_quality_migration.py
Builds its own databases in a temp dir.

WHAT IS BEING PINNED
  This migration rewrites realized_pnl_total, which the drawdown breaker sizes
  against. Four ways that goes wrong quietly:

    · It guesses the account. "The first one" is right until a REAL account
      exists beside the paper one, and then it rewrites the wrong account's
      risk state.
    · A key living in both the global and an account scope — exactly what an
      accidental v4 upgrade produced here — gets silently resolved to one of
      them, hiding the corruption instead of reporting it.
    · peak_equity re-anchored with prior_peak=0. Removing non-trades can only
      RAISE effective equity, so the high-water mark must never move down; a
      peak that walks down is a drawdown the breaker stops seeing.
    · A second run rewrites rows to their own values, so "idempotent" means
      "same result" rather than "changed nothing" — and the audit trail fills
      with entries recording that nothing happened.
"""
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "migrate_ledger_quality.py"

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


def run(db, *args):
    import os
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), *args],
        cwd=ROOT, capture_output=True, text=True,
        env=dict(os.environ, PYTHONPATH=str(ROOT)))
    if r.returncode not in (0, 2):
        print("      unexpected exit:", r.returncode)
        print("      " + (r.stderr or r.stdout).strip()[-300:])
    return r


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build(path: Path, *, scoped: bool, accounts: list[tuple[str, str]],
          kv: list[tuple[str, str, object]]):
    """kv entries are (account_id_or_empty, key, value)."""
    c = sqlite3.connect(str(path))
    c.executescript("""
        CREATE TABLE closed_trades (id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT, symbol TEXT, qty INT, entry REAL, stop REAL, exit REAL,
          exit_reason TEXT, pnl REAL, pnl_pct REAL, r_multiple REAL,
          opened_at TEXT, extra TEXT);
    """)
    if scoped:
        c.executescript("""
            CREATE TABLE kv_state (account_id TEXT NOT NULL DEFAULT '',
              key TEXT NOT NULL, value TEXT NOT NULL,
              PRIMARY KEY (account_id, key));
            CREATE TABLE accounts (account_id TEXT PRIMARY KEY, trade_env TEXT,
              broker_acc_id TEXT, label TEXT, created_at TEXT);
        """)
        for i, (aid, env) in enumerate(accounts):
            c.execute("INSERT INTO accounts VALUES (?,?,NULL,?,?)",
                      (aid, env, env, f"2026-01-0{i+1}T00:00:00"))
        for aid, k, v in kv:
            c.execute("INSERT INTO kv_state (account_id,key,value) VALUES (?,?,?)",
                      (aid, k, json.dumps(v)))
    else:
        c.execute("CREATE TABLE kv_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for _aid, k, v in kv:
            c.execute("INSERT INTO kv_state (key,value) VALUES (?,?)",
                      (k, json.dumps(v)))
    # Four real trades, one synthetic TST, one duplicated close.
    rows = [("2026-06-01T10:00:00", "AAA", 1, 10, 9, 11, 1.0, "TP", "o1"),
            ("2026-06-02T10:00:00", "BBB", 1, 10, 9, 9, -1.0, "SL", "o2"),
            ("2026-06-03T10:00:00", "TST", 10, 100, 90, 90, -100.0, "SL", "o3"),
            ("2026-06-04T10:00:00", "CCC", 1, 10, 9, 9.5, -0.5, "SL", "o4"),
            ("2026-06-05T10:00:00", "DDD", 7, 132.06, 130, 131.06, -7.03, "MANUAL_SELL", "o5"),
            ("2026-06-05T10:00:04", "DDD", 7, 132.06, 130, 131.06, -7.03, "MANUAL_SELL", "o5")]
    for ts, sym, qty, e, s, x, pnl, why, oa in rows:
        c.execute("INSERT INTO closed_trades (ts,symbol,qty,entry,stop,exit,pnl,"
                  "exit_reason,opened_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (ts, sym, qty, e, s, x, pnl, why, oa))
    c.commit(); c.close()


tmp = Path(tempfile.mkdtemp(prefix="mmt-qual-"))
A1, A2 = "11111111-aaaa-4000-8000-000000000001", "22222222-bbbb-4000-8000-000000000002"

# ── 1. two accounts, none named -> refuse ───────────────────────────────────
print("1  ambiguous account")
db = tmp / "two.db"
build(db, scoped=True, accounts=[(A1, "SIMULATE"), (A2, "REAL")],
      kv=[(A1, "budget_usd", 10000.0), (A1, "realized_pnl_total", -115.53),
          (A1, "peak_equity", 10000.0)])
before = sha(db)
r = run(db, "--apply")
check("refuses without --account", r.returncode == 2)
check("says why", "more than one" in r.stdout or "accounts in this database" in r.stdout)
check("wrote nothing", sha(db) == before)

r = run(db, "--apply", "--account", A1)
check("proceeds when told which account", r.returncode == 0)
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True); c.row_factory = sqlite3.Row
scoped = {(x["account_id"], x["key"]): json.loads(x["value"])
          for x in c.execute("SELECT * FROM kv_state")}
check("wrote to the named account", scoped.get((A1, "realized_pnl_total")) == -7.53)
check("left the other account alone",
      not any(k[0] == A2 for k in scoped))
c.close()

# ── 2. a key in two scopes at once -> refuse ────────────────────────────────
print("\n2  key present in both scopes")
db = tmp / "dual.db"
build(db, scoped=True, accounts=[(A1, "SIMULATE")],
      kv=[(A1, "realized_pnl_total", -115.53), ("", "realized_pnl_total", -8.53),
          (A1, "budget_usd", 10000.0), (A1, "peak_equity", 10000.0)])
before = sha(db)
r = run(db, "--apply")
check("refuses on a double-scoped key", r.returncode == 2)
check("names the key", "realized_pnl_total" in r.stdout)
check("wrote nothing", sha(db) == before)

# ── 3. peak must not be lowered ─────────────────────────────────────────────
print("\n3  peak_equity is a high-water mark")
db = tmp / "peak.db"
build(db, scoped=True, accounts=[(A1, "SIMULATE")],
      kv=[(A1, "budget_usd", 10000.0), (A1, "realized_pnl_total", -115.53),
          (A1, "peak_equity", 12500.0)])       # account genuinely reached 12.5k
r = run(db, "--apply", "--account", A1)
check("runs", r.returncode == 0)
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
peak = json.loads(c.execute(
    "SELECT value FROM kv_state WHERE key='peak_equity'").fetchone()[0])
c.close()
check(f"peak kept at its high-water mark (got {peak})", peak == 12500.0)

# ── 4. second run changes nothing at all ────────────────────────────────────
print("\n4  re-run is a true no-op")
db = tmp / "idem.db"
build(db, scoped=True, accounts=[(A1, "SIMULATE")],
      kv=[(A1, "budget_usd", 10000.0), (A1, "realized_pnl_total", -115.53),
          (A1, "peak_equity", 10000.0)])
r1 = run(db, "--apply", "--account", A1)
check("first run applies", r1.returncode == 0)
after_first = sha(db)

r2 = run(db, "--apply", "--account", A1)
check("second run succeeds", r2.returncode == 0)
check("second run leaves the file byte-identical", sha(db) == after_first)

c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
receipt = json.loads(c.execute(
    "SELECT value FROM kv_state WHERE key='ledger_quality_migration'").fetchone()[0])
c.close()
check(f"receipt has exactly one entry (got {len(receipt)})", len(receipt) == 1)
check("and it records the real first run", receipt[0]["marked"] == 2)
check("receipt names the account", receipt[0].get("account_id") == A1)

r3 = run(db, "--apply", "--account", A1)
check("third run still byte-identical", sha(db) == after_first)

# ── 5. v3 still works, and rejects --account ────────────────────────────────
print("\n5  v3 database")
db = tmp / "v3.db"
build(db, scoped=False, accounts=[], kv=[("", "budget_usd", 10000.0),
                                         ("", "realized_pnl_total", -115.53),
                                         ("", "peak_equity", 10000.0)])
r = run(db, "--apply")
check("v3 applies without an account", r.returncode == 0)
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
val = json.loads(c.execute(
    "SELECT value FROM kv_state WHERE key='realized_pnl_total'").fetchone()[0])
c.close()
check(f"effective total written (got {val})", val == -7.53)
after = sha(db)
check("v3 re-run is byte-identical", run(db, "--apply").returncode == 0 and sha(db) == after)

r = run(db, "--apply", "--account", A1)
check("v3 refuses an --account it cannot honour", r.returncode == 2)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
