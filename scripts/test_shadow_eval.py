"""The forward evaluation of the AI layer, checked before it decides anything.

Run from repo root: .venv/bin/python scripts/test_shadow_eval.py
No broker, no network.

WHY

  This analysis is meant to answer "keep or drop the AI layer". A join that
  attributes the wrong outcome to a verdict, or a threshold that lets noise
  read as signal, produces a confident wrong answer — which is worse than the
  "we cannot backtest this" it replaces.

  Two exclusions carry most of the weight, and both are easy to get wrong:
  score 50 is what main.py writes when the AI budget is exhausted, and a null
  is what it writes when no key is configured. Neither is a judgement about the
  trade, and counting either as a verdict would fill the sample with rows that
  say nothing while pulling every split toward the mean.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ai_shadow_eval as ev                                  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name + (f"   [{detail}]" if detail else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1


def build(rows_buy, rows_close) -> Path:
    """A database shaped like the real one, holding exactly what the test says."""
    import json
    d = Path(tempfile.mkdtemp(prefix="mmt-shadow-")) / "trader.db"
    c = sqlite3.connect(str(d))
    c.executescript("""
        CREATE TABLE audit (id INTEGER PRIMARY KEY, ts TEXT, action TEXT,
                            symbol TEXT, extra TEXT);
        CREATE TABLE closed_trades (id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT,
                                    pnl REAL, r_multiple REAL, exit_reason TEXT);
    """)
    for ts, sym, extra in rows_buy:
        c.execute("INSERT INTO audit (ts,action,symbol,extra) VALUES (?,'buy',?,?)",
                  (ts, sym, json.dumps(extra)))
    for ts, sym, pnl in rows_close:
        c.execute("INSERT INTO closed_trades (ts,symbol,pnl,r_multiple,exit_reason)"
                  " VALUES (?,?,?,?,'SL')", (ts, sym, pnl, pnl / 100.0))
    c.commit()
    c.close()
    return d


# ── 1. a verdict is joined to ITS OWN outcome ──────────────────────────────
print("1  each verdict meets the trade it was about")
db = build(
    [("2026-01-01T10:00", "AAA", {"ai_score": 90}),
     ("2026-01-05T10:00", "AAA", {"ai_score": 10})],
    [("2026-01-02T15:00", "AAA", 100.0),
     ("2026-01-06T15:00", "AAA", -200.0)])
buys, closes = ev.load(db)
m = ev.join(buys, closes)
check("both buys matched", len(m) == 2, str(len(m)))
by = {r["ai_score"]: r["pnl"] for r in m}
check("the 90 got the FIRST close, not the best one", by.get(90) == 100.0, str(by))
check("...and the 10 got the second", by.get(10) == -200.0, str(by))

# A close BEFORE the buy belongs to neither.
db = build([("2026-02-10T10:00", "BBB", {"ai_score": 80})],
           [("2026-02-01T15:00", "BBB", 500.0)])
buys, closes = ev.load(db)
check("a close that predates the buy is not claimed", len(ev.join(buys, closes)) == 0)

# One close cannot serve two buys.
db = build([("2026-03-01T10:00", "CCC", {"ai_score": 70}),
            ("2026-03-02T10:00", "CCC", {"ai_score": 30})],
           [("2026-03-03T15:00", "CCC", 50.0)])
buys, closes = ev.load(db)
m = ev.join(buys, closes)
check("one close is claimed once", len(m) == 1, str(len(m)))
check("...by the EARLIER buy", m[0]["ai_score"] == 70, str(m[0]["ai_score"]))

# A different symbol's close is never borrowed.
db = build([("2026-04-01T10:00", "DDD", {"ai_score": 60})],
           [("2026-04-02T15:00", "EEE", 900.0)])
buys, closes = ev.load(db)
check("another symbol's close is not borrowed", len(ev.join(buys, closes)) == 0)


# ── 2. non-verdicts are excluded ───────────────────────────────────────────
print("\n2  a placeholder is not an opinion")
# 50 = "AI budget exhausted — neutral"; null = "no key configured". Both would
# otherwise pad the sample with rows that say nothing.
db = build(
    [("2026-05-01T10:00", "AAA", {"ai_score": 95}),
     ("2026-05-02T10:00", "BBB", {"ai_score": 50}),
     ("2026-05-03T10:00", "CCC", {"ai_score": None}),
     ("2026-05-04T10:00", "DDD", {})],
    [("2026-05-01T15:00", "AAA", 10.0), ("2026-05-02T15:00", "BBB", 10.0),
     ("2026-05-03T15:00", "CCC", 10.0), ("2026-05-04T15:00", "DDD", 10.0)])
r = ev.evaluate(db, min_n=1)
check("all four matched to a close", r["n_matched"] == 4, str(r["n_matched"]))
check("only the real verdict is scored", r["n_scored"] == 1, str(r["n_scored"]))
check("...and 50 specifically is dropped",
      r["all"]["n"] == 1 and r["all"]["net"] == 10.0, str(r["all"]))


# ── 3. it refuses to conclude from too little ──────────────────────────────
print("\n3  a small sample is reported as small, not as a result")
db = build([(f"2026-06-0{i}T10:00", "AAA", {"ai_score": 90 if i % 2 else 10})
            for i in range(1, 8)],
           [(f"2026-06-0{i}T15:00", "AAA", 100.0 if i % 2 else -100.0)
            for i in range(1, 8)])
r = ev.evaluate(db, min_n=50)
check("seven trades is not ready", r["ready"] is False, str(r["n_scored"]))
check("...and the shortfall is reported", r["n_scored"] == 7 and r["min_n"] == 50)
r2 = ev.evaluate(db, min_n=5)
check("the same data IS ready against a lower bar", r2["ready"] is True)
check("...so `ready` is about the threshold, not the numbers",
      r2["all"]["n"] == r["all"]["n"])


# ── 4. the split is computed the way it is reported ────────────────────────
print("\n4  above and below the median are what they claim")
db = build(
    [("2026-07-01T10:00", "A", {"ai_score": 100}),
     ("2026-07-02T10:00", "B", {"ai_score": 80}),
     ("2026-07-03T10:00", "C", {"ai_score": 20}),
     ("2026-07-04T10:00", "D", {"ai_score": 0})],
    [("2026-07-01T15:00", "A", 400.0), ("2026-07-02T15:00", "B", 200.0),
     ("2026-07-03T15:00", "C", -100.0), ("2026-07-04T15:00", "D", -300.0)])
r = ev.evaluate(db, min_n=1)
check("the median of 0/20/80/100 is 50", r["median_score"] == 50.0,
      str(r["median_score"]))
check("two above", r["above_median"]["n"] == 2, str(r["above_median"]))
check("two below", r["below_median"]["n"] == 2, str(r["below_median"]))
check("above nets +600", r["above_median"]["net"] == 600.0)
check("below nets -400", r["below_median"]["net"] == -400.0)
check("win rates are per side", r["above_median"]["win_pct"] == 100.0
      and r["below_median"]["win_pct"] == 0.0)
check("the total is the whole sample, not a side",
      r["all"]["n"] == 4 and r["all"]["net"] == 200.0, str(r["all"]))


# ── 5. an empty or absent database says so ─────────────────────────────────
print("\n5  nothing to report is reported as nothing")
db = build([], [])
r = ev.evaluate(db, min_n=1)
check("an empty database is not ready", r["ready"] is False)
check("...with zero scored", r["n_scored"] == 0)
check("...and no invented split", r.get("all") is None, str(r.get("all")))
r = ev.evaluate(Path("/nonexistent/trader.db"))
check("a missing database reports an error rather than raising",
      bool(r.get("error")) and r["ready"] is False)


# ── 6. an open position is not counted as an outcome ───────────────────────
print("\n6  a trade still open has no outcome to be judged against")
db = build([("2026-08-01T10:00", "AAA", {"ai_score": 95})], [])
r = ev.evaluate(db, min_n=1)
check("a buy with no close is not scored", r["n_scored"] == 0, str(r["n_scored"]))
check("...but it is still counted as a buy", r["n_buys"] == 1)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
