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

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
