"""A position is what filled, not what was asked for.

Run from repo root: .venv/bin/python scripts/test_partial_fills.py
Fake broker throughout. Contacts no OpenD, places nothing.

WHAT THIS IS ABOUT
  executor wrote the position the instant place_order() returned:

      "qty": qty,               # the REQUESTED quantity
      "entry_price": limit_px,  # the price we ASKED for

  Both are fiction until the broker fills. A limit that filled 40 of 100 left a
  100-share record whose stop and take-profit covered 60 shares nobody owned —
  and the exit for those 60 would have opened a short. Every naked short in
  this account began with an exit for shares that were not held.

  An order that filled nothing left a position that existed in one database and
  nowhere else: the phantom HPE 64 of 2026-08-11, which reconcile later "closed"
  at a price for a sale that never happened.

  And entry_price feeds the R-multiple, which feeds half-Kelly, the optimizer,
  the blacklist and adaptive sizing — so a limit booked as an entry made the
  whole self-improvement loop read slippage-free numbers.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-partial-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

import pandas as pd                                          # noqa: E402
from moomoo import RET_OK, TrdSide                           # noqa: E402
from src import broker_binding as bb, db, identity, order_gate, order_log  # noqa: E402
from src import executor, moo_client                          # noqa: E402
from src.indicators import Signal                             # noqa: E402

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
os.environ["MMT_START_FENCE"] = "1"
(Path(_TMP) / "logs" / "worker.lease").write_text(
    json.dumps({"pid": os.getpid(), "fence": 1, "host": "t"}))
order_gate.permit("test")
executor._ENTRY_FILL_WAIT_SEC = 2.0        # keep the suite quick


class FakeTrade:
    """A broker that fills exactly as much as the test says."""
    def __init__(self, fill_qty, fill_price, status):
        self.fill_qty, self.fill_price, self.status = fill_qty, fill_price, status
        self.rows = []
        self.placed = []
    def get_acc_list(self):
        return (RET_OK, [{"acc_id": 1, "trd_env": "SIMULATE",
                          "security_firm": "N/A", "acc_status": "ACTIVE",
                          "acc_type": "CASH"}])
    def place_order(self, **kw):
        self.placed.append(kw)
        oid = f"BRK{len(self.placed)}"
        self.rows.append({"order_id": oid, "remark": kw.get("remark", ""),
                          "code": kw["code"], "qty": kw["qty"],
                          "dealt_qty": self.fill_qty,
                          "dealt_avg_price": self.fill_price,
                          "order_status": self.status})
        return (RET_OK, pd.DataFrame([{"order_id": oid}]))
    def order_list_query(self, **kw):
        return (RET_OK, pd.DataFrame(self.rows))
    def history_order_list_query(self, **kw):
        return (RET_OK, pd.DataFrame(self.rows))
    def close(self):
        pass


class FakeClient(moo_client.MooClient):
    def __init__(self, fill_qty, fill_price, status="FILLED_ALL", holdings=None):
        super().__init__()
        bb.reset()
        self._trade = FakeTrade(fill_qty, fill_price, status)
        # What the broker says we hold. _assert_still_held asks this before
        # every exit — the duplicate-sell guard — so an exit test that leaves it
        # empty is testing the guard, not the booking.
        self._holdings = holdings or {}
        bb.bind(bb.resolve(self._trade, trade_env="SIMULATE"))
    # Everything that would reach a real gateway is stubbed. Without the
    # `quote` override the first run opened a live OpenQuoteContext against
    # 127.0.0.1:11111 — a unit test quietly talking to the broker, and the
    # non-daemon thread it starts also hung interpreter shutdown, so the whole
    # suite produced no output at all.
    @property
    def quote(self):
        raise RuntimeError("no quote context in this test")
    def get_last_price(self, symbol):
        return None          # no live quote -> the entry uses the signal price
    def get_positions(self):
        if not self._holdings:
            return pd.DataFrame()
        return pd.DataFrame([{"code": f"US.{s}", "qty": float(q)}
                             for s, q in self._holdings.items()])
    def get_short_symbols(self):
        return set()


def sig(symbol="AAPL", price=100.0, atr=2.0):
    return Signal(symbol=symbol, price=price, atr=atr, score=80.0)


def reset_positions():
    for s in list(db.load_open_trades()):
        db.delete_open_trade(s)


# ── 1. nothing filled → no position ────────────────────────────────────────
print("1  an order that fills nothing creates no holding")
reset_positions()
c = FakeClient(fill_qty=0, fill_price=0, status="SUBMITTED")
trade = executor.open_position(c, sig("HPE", 21.5, 0.5), 64)
check("open_position returns nothing", trade is None)
check("no position was written", db.load_open_trades() == {})
check("the order is still recorded", len(order_log.recent(limit=10)) >= 1)
check("...and still live, not written off",
      any(o["symbol"] == "HPE" for o in order_log.live_orders()))
# This is the phantom-HPE case. The old code wrote a 64-share holding here.
check("the skip reason names the cause",
      executor.last_entry_skip()[0] == "no_fill")


# ── 2. a partial fill → the position is what filled ────────────────────────
print("\n2  a partial fill is the position, sized and priced from the fill")
reset_positions()
c = FakeClient(fill_qty=40, fill_price=99.75, status="FILLED_PART")
trade = executor.open_position(c, sig("MSFT", 100.0, 2.0), 100)
check("a position was opened", trade is not None)
check("its quantity is what filled, not what was asked",
      trade and trade["qty"] == 40)
check("its entry price is the broker's average, not our limit",
      trade and abs(trade["entry_price"] - 99.75) < 1e-9)
saved = db.load_open_trades().get("MSFT")
check("the database agrees", saved and saved["qty"] == 40)
check("...on the price too", saved and abs(saved["entry_price"] - 99.75) < 1e-9)

# The consequence that mattered: protective levels sized to shares we hold.
check("the R unit is measured from the real entry",
      trade and abs(trade["init_risk_per_share"]
                    - (99.75 - trade["stop_loss"])) < 1e-9)
check("the scale-out anchor is the real lot", trade and trade["qty_initial"] == 40)
check("water marks start at the real entry",
      trade and abs(trade["high_water"] - 99.75) < 1e-9)


# ── 3. a full fill still books the broker's price ──────────────────────────
print("\n3  a full fill books what the broker charged")
reset_positions()
c = FakeClient(fill_qty=50, fill_price=101.40, status="FILLED_ALL")
trade = executor.open_position(c, sig("NVDA", 100.0, 2.0), 50)
check("the quantity is the full request", trade and trade["qty"] == 50)
check("the entry is the fill, not the limit",
      trade and abs(trade["entry_price"] - 101.40) < 1e-9)
# Booking the limit here is what fed slippage-free numbers to half-Kelly, the
# optimizer, the blacklist and adaptive sizing.
check("...which differs from the limit that was sent",
      abs(101.40 - float(c._trade.placed[-1]["price"])) > 0.01)


# ── 4. a stack averages what was PAID ──────────────────────────────────────
print("\n4  stacking averages the prices actually paid")
reset_positions()
c = FakeClient(fill_qty=10, fill_price=100.0, status="FILLED_ALL")
first = executor.open_position(c, sig("AMD", 100.0, 2.0), 10)
check("the first lot is in", first and first["qty"] == 10)

c2 = FakeClient(fill_qty=10, fill_price=110.0, status="FILLED_ALL")
second = executor.open_position(c2, sig("AMD", 108.0, 2.0), 10)
if second and second.get("stacks", 1) > 1:
    # (10 @ 100 + 10 @ 110) / 20 = 105. Using the limit would have averaged
    # toward a price the broker never charged.
    check("the average entry is the average of the fills",
          abs(second["entry_price"] - 105.0) < 0.01)
    check("the combined quantity is both fills", second["qty"] == 20)
else:
    # Stacking is gated on unrealised R and MAX_STACKS; if the gate refused,
    # say so rather than reporting a pass for a path that did not run.
    check("stacking was refused by risk rules (not a fill-accounting result)",
          second is None or second.get("stacks", 1) == 1)


# ── 5. the order log and the position agree ────────────────────────────────
print("\n5  the log and the position tell the same story")
reset_positions()
c = FakeClient(fill_qty=7, fill_price=55.5, status="FILLED_PART")
trade = executor.open_position(c, sig("IBM", 55.0, 1.0), 20)
ibm_orders = [o for o in order_log.recent(limit=50) if o["symbol"] == "IBM"]
check("the order was logged", bool(ibm_orders))
o = ibm_orders[0]
check("the log records what was REQUESTED", o["requested_qty"] == 20)
check("the log records what FILLED", o["filled_qty"] == 7)
check("the position matches the fill, not the request",
      trade and trade["qty"] == 7)
check("the two never disagree about the fill",
      trade and trade["qty"] == o["filled_qty"])


# ── 6. await_fill returns what is known when the wait expires ──────────────
print("\n6  a wait that times out reports what filled so far")
c = FakeClient(fill_qty=3, fill_price=10.0, status="SUBMITTED")   # never settles
p = c.place_limit_order("XYZ", 10, 10.0, TrdSide.BUY)
row = c.await_fill(p.client_order_id, timeout=1.0, poll=0.2)
check("the wait returns rather than hanging", row is not None)
check("it reports the partial fill", row["filled_qty"] == 3)
check("the order is still live, not written off",
      row["state"] not in order_log.TERMINAL_STATES)
# A timeout is not a fill and not a failure. Treating it as either is how a
# position gets recorded for shares still working at the broker.
check("...so it stays in the live list",
      any(x["client_order_id"] == p.client_order_id
          for x in order_log.live_orders()))


# ── 7. a partial EXIT books what sold and keeps the rest ───────────────────
#
# _sell_and_book_price used to return only a price, so all ten callers booked
# the quantity they had ASKED to sell and then removed the position. On a
# partial that closes shares still held: the software goes flat, the broker
# does not, and the next exit for those shares is a sell of stock we do not
# own. Its own comment said the residual was "reconcile's job", which is the
# thing that must not be true.
print("\n7  a partial exit books what sold, and the rest stays held")
reset_positions()

# Open 100, then let only 30 of the exit fill.
c = FakeClient(fill_qty=100, fill_price=50.0, status="FILLED_ALL")
opened = executor.open_position(c, sig("XOM", 50.0, 1.0), 100)
check("a full position is open", opened and opened["qty"] == 100)

before_closes = len(db.closed_trades(limit=500, include_excluded=True))
c2 = FakeClient(fill_qty=30, fill_price=49.0, status="FILLED_PART",
                holdings={"XOM": 100})
trades = executor._load_open_trades()
pnl, px, sold = executor._exit_and_book(
    c2, "XOM", trades["XOM"], trades, 100, 48.5, 49.0, "SL")

check("only what sold is reported", sold == 30)
after = db.load_open_trades().get("XOM")
check("the position is NOT removed", after is not None)
check("...and holds exactly the residual", after and after["qty"] == 70)

closes = db.closed_trades(limit=500, include_excluded=True)
check("exactly one close was booked", len(closes) == before_closes + 1)
check("...for the quantity that actually sold", closes[-1]["qty"] == 30)
check("...at the price it actually sold for",
      abs(float(closes[-1]["exit"]) - 49.0) < 1e-9)

# Selling the residual finishes the job. The first exit order has to reach a
# terminal state before a second can be placed — a second LIVE exit for the
# same symbol could sell the residual twice, which is what the intent
# constraint refuses.
for o in order_log.live_orders("XOM"):
    order_log.record_fill(o["client_order_id"], filled_qty=o["filled_qty"],
                          avg_price=o["avg_fill_price"], state="CANCELLED")
c3 = FakeClient(fill_qty=70, fill_price=48.8, status="FILLED_ALL",
                holdings={"XOM": 70})
trades = executor._load_open_trades()
pnl, px, sold = executor._exit_and_book(
    c3, "XOM", trades["XOM"], trades, 70, 48.0, 48.8, "SL")
check("the residual sells", sold == 70)
check("...and now the position is gone", "XOM" not in db.load_open_trades())
closes = db.closed_trades(limit=500, include_excluded=True)
check("two closes total, 30 + 70",
      closes[-1]["qty"] == 70 and closes[-2]["qty"] == 30)


# ── 8. an exit that fills nothing books nothing ────────────────────────────
print("\n8  an exit that fills nothing leaves the position exactly as it was")
reset_positions()
c = FakeClient(fill_qty=25, fill_price=10.0, status="FILLED_ALL")
executor.open_position(c, sig("KO", 10.0, 0.2), 25)
before_closes = len(db.closed_trades(limit=500, include_excluded=True))

for o in order_log.live_orders("KO"):
    order_log.record_fill(o["client_order_id"], filled_qty=o["filled_qty"],
                          avg_price=o["avg_fill_price"], state="CANCELLED")
c2 = FakeClient(fill_qty=0, fill_price=0, status="SUBMITTED",
                holdings={"KO": 25})
trades = executor._load_open_trades()
pnl, px, sold = executor._exit_and_book(
    c2, "KO", trades["KO"], trades, 25, 9.5, 9.8, "SL")
check("nothing is reported sold", sold == 0)
check("no close is booked",
      len(db.closed_trades(limit=500, include_excluded=True)) == before_closes)
held = db.load_open_trades().get("KO")
check("the position is untouched", held and held["qty"] == 25)
# The safe direction. Booking a close here would mark shares realized that the
# broker still holds, and the next exit for them would be a short.
check("...so a later exit still has shares to sell", held["qty"] == 25)



# ── 9. a refused order is still written down ───────────────────────────────
#
# The order gate used to be checked before the intent was recorded, so a
# staging run left no trace of what the strategy had wanted to do — and
# watching the decisions without sending them is the entire purpose of running
# without order capability.
print("\n9  an order the gate refuses is recorded as FAILED_LOCAL, never sent")
order_gate.deny("staging-style run")
c = FakeClient(fill_qty=10, fill_price=10.0)
before = len(order_log.recent(limit=500))
try:
    c.place_limit_order("SPY", 10, 500.0, TrdSide.BUY)
    check("the call is refused", False)
except order_gate.OrdersNotPermitted:
    check("the call is refused", True)

after = order_log.recent(limit=500)
check("the intent was recorded anyway", len(after) == before + 1)
row = after[0]
check("...as FAILED_LOCAL", row["state"] == "FAILED_LOCAL")
check("...for the symbol and size that was wanted",
      row["symbol"] == "SPY" and row["requested_qty"] == 10)
check("...with the reason", "NOT permitted" in (row["last_error"] or ""))
check("nothing reached the broker",
      not any(k["code"].endswith("SPY") for k in c._trade.placed))
check("...and it is not counted as live",
      not any(o["client_order_id"] == row["client_order_id"]
              for o in order_log.live_orders()))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
