"""Applying a fill exactly once, however many times it is seen.

Run from repo root: .venv/bin/python scripts/test_fill_settler.py
No broker at all — these drive the settler against the database directly.

WHAT WAS WRONG
  `filled_qty` is the broker's CUMULATIVE total. The entry and exit paths read
  it once, inside a bounded wait, and applied what they saw. Two consequences:

    * a fill arriving AFTER the wait updated the order row and nothing else,
      so the shares existed at the broker, and in the orders table, and in no
      position — and nothing was watching for them;
    * anything that read the cumulative figure a second time would apply the
      same shares twice.

  So an order now carries what has already been applied, and only the
  difference may be applied — in the same transaction as the position it moves.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-settler-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

from src import db, fill_settler, identity, order_log, risk_manager  # noqa: E402

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


db._ensure_initialised()
identity._session = None
identity.reset_cache()
identity.start_session("SIMULATE")


def reset():
    for s in list(db.load_open_trades()):
        db.delete_open_trade(s)
    with db.transaction() as c:
        c.execute("DELETE FROM orders")
        c.execute("DELETE FROM closed_trades")
    db.update_state({"halted": False, "halt_reason": None, "halt_detail": None,
                     "realized_pnl_total": 0.0, "realized_pnl_today": 0.0})


def an_order(symbol, side, kind, requested, filled, avg, state="PARTIAL"):
    coid = order_log.begin(symbol=symbol, side=side, kind=kind,
                           requested_qty=requested, limit_price=avg,
                           enforce_unique_intent=False)
    order_log.submitted(coid, f"B-{coid[-6:]}")
    order_log.record_fill(coid, filled_qty=filled, avg_price=avg, state=state)
    return coid


def a_position(symbol, qty, entry, stop=None):
    db.upsert_open_trade({"symbol": symbol, "qty": qty, "entry_price": entry,
                          "stop_loss": stop if stop is not None else entry * 0.95,
                          "take_profit": entry * 1.1})


# ── 1. the same fill applied twice moves the books once ────────────────────
print("1  settling twice is settling once")
reset()
a_position("AAPL", 100, 190.0)
coid = an_order("AAPL", "SELL", "EXIT", 100, 40, 195.0)

first = fill_settler.settle(coid)
check("the first settlement applies the fill", first["applied"] == 40)
check("the position is reduced by exactly that",
      db.load_open_trades()["AAPL"]["qty"] == 60)
check("one close is booked", len(db.closed_trades(include_excluded=True)) == 1)

second = fill_settler.settle(coid)
check("the second settlement applies nothing", second["applied"] == 0)
check("the position is unchanged", db.load_open_trades()["AAPL"]["qty"] == 60)
check("no second close appears",
      len(db.closed_trades(include_excluded=True)) == 1)

third = fill_settler.settle(coid)
check("and a third changes nothing either", third["applied"] == 0)


# ── 2. only the NEW shares, at what THEY cost ──────────────────────────────
print("\n2  a growing cumulative fill applies only its increment")
reset()
a_position("MSFT", 100, 400.0)
coid = an_order("MSFT", "SELL", "EXIT", 100, 40, 100.0)
fill_settler.settle(coid)
check("40 applied first", db.load_open_trades()["MSFT"]["qty"] == 60)

# The broker now reports 100 filled at a cumulative average of 106. The 60 new
# shares actually went at 110 — booking them at 106 would put a price the
# broker never charged into the ledger.
order_log.record_fill(coid, filled_qty=100, avg_price=106.0, state="FILLED")
out = fill_settler.settle(coid)
check("only the 60 new shares are applied", out["applied"] == 60)
check("the position is now flat", "MSFT" not in db.load_open_trades())
closes = db.closed_trades(include_excluded=True)
check("two closes: 40 then 60",
      [c["qty"] for c in closes] == [40, 60])
check("...and the second is priced at what those shares made, not the average",
      abs(float(closes[-1]["exit"]) - 110.0) < 1e-6)


# ── 3. a late BUY fill extends the position at its own price ───────────────
print("\n3  a late buy fill reaches the position")
reset()
a_position("NVDA", 50, 100.0)
coid = an_order("NVDA", "BUY", "ENTRY", 100, 50, 100.0)
fill_settler.mark_applied(coid, 50, 100.0)     # the entry path applied its own
check("nothing outstanding yet", fill_settler.settle(coid)["applied"] == 0)

# 50 more fill, minutes after the entry stopped waiting.
order_log.record_fill(coid, filled_qty=100, avg_price=105.0, state="FILLED")
out = fill_settler.settle(coid)
check("the late 50 are applied", out["applied"] == 50)
pos = db.load_open_trades()["NVDA"]
check("the position grows to the full fill", pos["qty"] == 100)
# (50 @ 100 + 50 @ 110) / 100 = 105
check("...at the weighted average of what was paid",
      abs(pos["entry_price"] - 105.0) < 1e-6)


# ── 4. selling more than is held stops everything ──────────────────────────
print("\n4  an oversell is refused, not absorbed")
reset()
a_position("XOM", 30, 50.0)
coid = an_order("XOM", "SELL", "STOP", 100, 100, 49.0)
try:
    fill_settler.settle(coid)
    check("settling an oversell raises", False)
except fill_settler.OversoldError:
    # Two bracket legs both filling looks exactly like this. Booking it would
    # leave a negative position, and the next exit for it is a real short sale.
    check("settling an oversell raises", True)
check("the position is untouched", db.load_open_trades()["XOM"]["qty"] == 30)
check("no close was booked", db.closed_trades(include_excluded=True) == [])
check("trading is halted", db.get_state().get("halted") is True)
check("...with a reason naming the cause",
      db.get_state().get("halt_reason") == "oversold position")
check("...which needs a person to clear",
      risk_manager.halt_status()["needs_manual_release"] is True)


# ── 5. the sweep finds what nothing else was watching ──────────────────────
print("\n5  the sweep picks up every unapplied fill")
reset()
a_position("A", 100, 10.0)
a_position("B", 100, 20.0)
c1 = an_order("A", "SELL", "EXIT", 100, 25, 11.0)
c2 = an_order("B", "SELL", "TP", 100, 10, 22.0)
c3 = an_order("A", "SELL", "EXIT", 100, 0, 0, state="SUBMITTED")   # nothing yet

out = fill_settler.settle_all()
check("both filled orders are settled", out["orders"] == 2)
check("35 shares applied in total", out["applied"] == 35)
check("A is reduced", db.load_open_trades()["A"]["qty"] == 75)
check("B is reduced", db.load_open_trades()["B"]["qty"] == 90)
check("the unfilled order was not touched",
      order_log.get(c3)["applied_qty"] == 0)

again = fill_settler.settle_all()
check("running the sweep again does nothing", again["orders"] == 0)
check("...and applies nothing", again["applied"] == 0)
check("positions are unchanged",
      db.load_open_trades()["A"]["qty"] == 75
      and db.load_open_trades()["B"]["qty"] == 90)


# ── 6. one live order per business intent ──────────────────────────────────
print("\n6  a second live order for the same intent is refused")
reset()
first = order_log.begin(symbol="TSLA", side="BUY", kind="ENTRY",
                        requested_qty=10, limit_price=100.0)
try:
    order_log.begin(symbol="TSLA", side="BUY", kind="ENTRY",
                    requested_qty=10, limit_price=100.0)
    check("a duplicate intent is refused", False)
except order_log.DuplicateIntent as e:
    # client_order_id is new on every attempt — that is what it is for, and
    # exactly why it cannot prevent a second request.
    check("a duplicate intent is refused", True)
    check("...and names the order that already has it",
          e.existing["client_order_id"] == first)

check("a different side is a different intent", bool(
    order_log.begin(symbol="TSLA", side="SELL", kind="EXIT",
                    requested_qty=10, limit_price=100.0)))
check("a different symbol is a different intent", bool(
    order_log.begin(symbol="AMD", side="BUY", kind="ENTRY",
                    requested_qty=10, limit_price=100.0)))

# Once the first order settles, the intent is free again.
order_log.rejected(first, "broker refused")
reopened = order_log.begin(symbol="TSLA", side="BUY", kind="ENTRY",
                           requested_qty=10, limit_price=100.0)
check("a settled order does not block the next attempt", bool(reopened))
check("...and there is still only one live order for it",
      order_log.live_for_intent(order_log.intent_key("TSLA", "BUY", "ENTRY"))
      ["client_order_id"] == reopened)


# ── 7. running recovery twice writes nothing the second time ───────────────
#
# The acceptance condition: two consecutive recoveries, the second a no-op.
# If settlement were not idempotent this is where it would show — the same
# cumulative fill applied again, the PnL counted twice.
print("\n7  a second recovery pass is a no-op")
reset()
a_position("REC", 100, 10.0)
coid = an_order("REC", "SELL", "EXIT", 100, 100, 12.0, state="FILLED")

first = fill_settler.settle_all()
pnl_after_first = float(db.get_state().get("realized_pnl_total") or 0)
closes_after_first = len(db.closed_trades(include_excluded=True))
check("the first pass applies the fill", first["applied"] == 100)
check("...and books the PnL", abs(pnl_after_first - 200.0) < 1e-6)

second = fill_settler.settle_all()
check("the second pass applies nothing", second["applied"] == 0)
check("...and finds no orders to settle", second["orders"] == 0)
check("PnL is not counted twice",
      abs(float(db.get_state().get("realized_pnl_total") or 0)
          - pnl_after_first) < 1e-9)
check("no duplicate close row",
      len(db.closed_trades(include_excluded=True)) == closes_after_first)


# ── 8. the two sleeves are closed in code ──────────────────────────────────
print("\n8  the sleeves that bypass the settler are off")
from src import cash_yield, inverse_sleeve                    # noqa: E402
db.update_state({"inverse_sleeve_enabled": True,
                 "cash_yield_enabled": True})
# Enabled by configuration and STILL off: they book positions from requested
# quantity and PnL from theory, and they run before the kill switch. A flag is
# a thing someone turns on to see what happens.
check("the inverse sleeve refuses even when enabled",
      inverse_sleeve.manage(None, "BEAR", 1000.0)["action"] == "disabled")
check("...and says why",
      "settle" in inverse_sleeve.manage(None, "BEAR", 1000.0).get("reason", ""))
check("the cash sleeve refuses even when enabled",
      cash_yield.manage(None, "BULL", 5000.0, None)["action"] == "disabled")
check("...and says why",
      "settle" in cash_yield.manage(None, "BULL", 5000.0, None).get("reason", ""))
db.update_state({"inverse_sleeve_enabled": False,
                 "cash_yield_enabled": False})


# ── 9. a crash mid-settlement leaves no half-applied fill ──────────────────
#
# The atomicity claim is that the position change and the applied counter
# commit together. A claim like that is only worth its fault injection: kill
# the process at each write of a settlement, reopen, and require that the fill
# is either fully applied or not applied at all — never counted once in the
# position and not in the counter, which the sweep would then apply again.
print("\n9  killing the process mid-settlement never double-applies")

CRASH_SETTLE = """
import os, sqlite3, sys
sys.path.insert(0, {root!r})
target = int(os.environ["CRASH_AFTER"])
seen = [0]
real_connect = sqlite3.connect
def traced(*a, **kw):
    conn = real_connect(*a, **kw)
    def trace(stmt):
        s = stmt.strip().upper()
        if s.startswith(("INSERT", "UPDATE", "DELETE", "COMMIT")):
            seen[0] += 1
            if seen[0] == target:
                os._exit(9)          # no unwinding: a real crash
    conn.set_trace_callback(trace)
    return conn
sqlite3.connect = traced
from src import db, identity, fill_settler
identity._session = None
identity.reset_cache()
identity.start_session("SIMULATE")
out = fill_settler.settle_all()
print("SETTLED " + str(out["applied"]))
"""

import subprocess, shutil, sqlite3 as _sq

def build_case(home):
    """A 100-share position and an exit order showing 40 filled."""
    env = dict(os.environ, MMT_HOME=str(home), PYTHONPATH=str(ROOT))
    subprocess.run([sys.executable, "-c",
        "from src import db, identity, order_log;"
        "db._ensure_initialised();"
        "identity.start_session('SIMULATE');"
        "db.upsert_open_trade({'symbol':'CR','qty':100,'entry_price':10.0,"
        "'stop_loss':9.0,'take_profit':12.0});"
        "coid = order_log.begin(symbol='CR', side='SELL', kind='EXIT',"
        "  requested_qty=100, limit_price=11.0);"
        "order_log.submitted(coid, 'BRK1');"
        "order_log.record_fill(coid, filled_qty=40, avg_price=11.0,"
        "  state='PARTIAL')"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True)


def inspect(home):
    c = _sq.connect(f"file:{home}/data/trader.db?mode=ro", uri=True)
    c.row_factory = _sq.Row
    try:
        o = c.execute("SELECT filled_qty, applied_qty FROM orders").fetchone()
        pos = c.execute("SELECT qty FROM open_trades WHERE symbol='CR'").fetchone()
        n_closed = c.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
        return (int(o["filled_qty"]), int(o["applied_qty"]),
                int(pos["qty"]) if pos else 0, n_closed)
    finally:
        c.close()


crashed = 0
for step in range(1, 9):
    home = Path(tempfile.mkdtemp(prefix=f"mmt-crash{step}-"))
    (home / "data").mkdir(parents=True); (home / "logs").mkdir()
    build_case(home)
    env = dict(os.environ, MMT_HOME=str(home), PYTHONPATH=str(ROOT),
               CRASH_AFTER=str(step))
    r = subprocess.run([sys.executable, "-c", CRASH_SETTLE.format(root=str(ROOT))],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode == 9:
        crashed += 1

    # Whatever happened, reopening and settling again must land on the same
    # answer: 40 applied, 60 held, one close.
    env2 = dict(env); env2["CRASH_AFTER"] = "0"
    subprocess.run([sys.executable, "-c", CRASH_SETTLE.format(root=str(ROOT))],
                   cwd=ROOT, env=env2, capture_output=True, text=True)
    filled, applied, held, closed = inspect(home)
    ok = (filled == 40 and applied == 40 and held == 60 and closed == 1)
    if not ok:
        print(f"        filled={filled} applied={applied} held={held} closes={closed}")
    check(f"crash at write {step}: exactly one application survives", ok)
    shutil.rmtree(home, ignore_errors=True)

check("the injector actually interrupted something", crashed > 0)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
