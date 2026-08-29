"""Who sold it? — attribution of a position the broker no longer holds.

Run from repo root: .venv/bin/python scripts/test_ghost_attribution.py
No OpenD, no orders placed. Drives reconcile's attribution and the executor's
exit-order memory directly.

WHAT WAS WRONG (2026-08-27 JNJ, 2026-08-28 MRK)

  A soft stop-loss placed a SELL, waited _EXIT_FILL_WAIT_SEC (8s), saw nothing
  filled, and returned — leaving the order working at the broker and the
  position on our books. The order then filled.

  Nothing noticed for 16 minutes:

    * settle_all selects on `filled_qty > applied_qty`, both LOCAL columns, and
      filled_qty is only written by a poll. An order whose wait window closed at
      zero filled is 0 > 0 — never selected, no matter how long it runs.
    * the only other re-poll is the bracket-leg refresh, which reads
      stop_order_id/tp_order_id. A soft exit is neither, and in SIMULATE there
      is no bracket at all.

  Reconcile's 15-minute ghost scan eventually asked the broker, found the sale,
  and had to decide whose it was. It compared the broker's order id against
  three keys on the position — stop_order_id, tp_order_id, exit_order_id — of
  which the first two are None in SIMULATE and the third was written NOWHERE in
  the codebase. The set was always empty, so the answer was always MANUAL_SELL:

    * the owner was told by Telegram that they had sold by hand, about the
      bot's own stop order;
    * the trade was logged MANUAL_SELL, which matches neither the SL re-entry
      cooldown query (db.last_sl_close_for_symbol) nor the ops bucket — so a
      stop-out lost the guard against re-buying it and counted as strategy
      performance.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-ghost-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

from src import db, identity, order_gate, order_log   # noqa: E402
from src import executor, portfolio, reconcile        # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond:
        PASS += 1
    else:
        FAIL += 1


db._ensure_initialised()
identity._session = None
identity.reset_cache()
identity.start_session("SIMULATE")
os.environ["MMT_START_FENCE"] = "1"
(Path(_TMP) / "logs" / "worker.lease").write_text(
    json.dumps({"pid": os.getpid(), "fence": 1, "host": "t"}))
order_gate.permit("test")


def logged_order(*, symbol, kind, intent, broker_id, qty=12):
    """An order this software placed, recorded the way the real path records it."""
    coid = order_log.begin(symbol=symbol, side="SELL", kind=kind,
                           requested_qty=qty, limit_price=100.0, intent=intent)
    order_log.submitted(coid, broker_id)
    return coid


# ── 1. the incident: a soft stop that filled after its wait window ─────────
print("\n1  the bot's own late-filling stop is not a manual sale")

logged_order(symbol="MRK", kind="EXIT", intent="SL", broker_id="3190309")
by_bot, reason = reconcile._attribute_sell("3190309", set())
check("recognised as ours from the order log alone", by_bot is True)
check("booked with its real reason, SL", reason == "SL")
check("not labelled MANUAL_SELL", reason != "MANUAL_SELL")

# The label is what the re-entry cooldown matches on. This is the consequence
# the mislabel actually had, so pin the query, not just the string.
row = {"ts": "2026-08-28T09:46:14-04:00", "symbol": "MRK", "qty": 12,
       "entry": 154.51, "stop": 148.74, "exit": 147.09, "exit_reason": reason,
       "pnl": -88.98, "pnl_pct": -4.8, "r_multiple": -1.29,
       "opened_at": "2026-08-25T15:31:21", "mfe_pct": 1.48, "mae_pct": -5.07,
       "strategy": "trend"}
db.closed_trade_insert(row)
check("the SL cooldown query now finds it",
      (db.last_sl_close_for_symbol("MRK") or {}).get("symbol") == "MRK")


# ── 2. bracket legs are named by their kind, not their intent ──────────────
print("\n2  bracket legs keep their own labels")

logged_order(symbol="HPE", kind="STOP", intent="", broker_id="777001")
check("a STOP leg books as SL_BRACKET",
      reconcile._attribute_sell("777001", set()) == (True, "SL_BRACKET"))

logged_order(symbol="DELL", kind="TP", intent="", broker_id="777002")
check("a TP leg books as TP_BRACKET",
      reconcile._attribute_sell("777002", set()) == (True, "TP_BRACKET"))


# ── 3. a real hand-placed sale is still called one ──────────────────────────
print("\n3  a sale we never placed is still MANUAL_SELL")

by_bot, reason = reconcile._attribute_sell("9999999", set())
check("an id absent from the order log is not ours", by_bot is False)
check("...and is labelled MANUAL_SELL", reason == "MANUAL_SELL")
check("MANUAL_SELL lands in the ops bucket, out of headline stats",
      "MANUAL_SELL" in portfolio.OWNER_CLOSE_REASONS)
check("...and so does the owner-asked close, MANUAL",
      "MANUAL" in portfolio.OWNER_CLOSE_REASONS)


# ── 4. the position's own copy answers when the log cannot ─────────────────
print("\n4  a trade whose order log rows have aged out")

by_bot, reason = reconcile._attribute_sell("3188058", {"3188058"})
check("ours by the position's record", by_bot is True)
check("honest about not knowing what for",
      reason == "BOT_SELL_UNRECORDED")
check("an empty/garbage id is not silently claimed",
      reconcile._attribute_sell("", set()) == (False, "MANUAL_SELL"))


# ── 5. the executor writes that copy in the first place ────────────────────
print("\n5  a working exit order is recorded on the position")

trade = {"symbol": "MRK", "qty": 12, "entry_price": 154.51,
         "stop_loss": 148.74, "take_profit": 170.0}
fill = executor.ExitFill(price=148.20, filled=0, client_order_id="mmtabc",
                         broker_order_id="3190309")
executor._remember_exit_order("MRK", trade, fill, "SL")
check("exit_order_id is set — the key nothing used to write",
      trade.get("exit_order_id") == "3190309")
check("and reconcile reads that exact key",
      "exit_order_id" in Path(ROOT / "src/reconcile.py").read_text())
check("the reason is kept alongside it", trade.get("exit_intent") == "SL")

executor._remember_exit_order("MRK", trade, executor.ExitFill(
    price=147.9, filled=0, broker_order_id="3190310"), "SL")
check("a replaced order is remembered too, not overwritten",
      trade["exit_order_ids"] == ["3190309", "3190310"])
check("the newest is the scalar", trade["exit_order_id"] == "3190310")

for _ in range(10):
    executor._remember_exit_order("MRK", trade, executor.ExitFill(
        price=147.9, filled=0, broker_order_id="319999"), "SL")
check("the same id is never duplicated",
      trade["exit_order_ids"].count("319999") == 1)
check("the list stays bounded",
      len(trade["exit_order_ids"]) <= executor._EXIT_OID_MEMORY)

no_id = {"symbol": "X", "qty": 1}
executor._remember_exit_order("X", no_id, executor.ExitFill(
    price=1.0, filled=0, broker_order_id="0"), "SL")
check("a placeholder broker id is not recorded",
      "exit_order_id" not in no_id)


# ── 6. the alert says what happened to the ghost ───────────────────────────
print("\n6  the alert reports the outcome, not just the symptom")

booked = {"reason": "SL", "exit": 147.09, "pnl": -88.98, "qty": 12,
          "order_id": "3190309"}
tail = reconcile._ghost_outcome({"type": "GHOST_DROPPED", "symbol": "MRK",
                                 "booked": booked})
check("a booked ghost says so", "已入账" in tail and "SL" in tail)
check("...with the money", "-89" in tail or "-88" in tail)
check("an unexplained ghost says THAT",
      "隔离" in reconcile._ghost_outcome({"type": "GHOST_DROPPED",
                                          "symbol": "X", "unexplained": True}))
check("a phantom says the buy never filled",
      "从未成交" in reconcile._ghost_outcome({"type": "GHOST_DROPPED",
                                             "symbol": "X", "phantom": True}))
check("a genuinely unresolved ghost reads exactly as before",
      reconcile._ghost_outcome(None) == "")


# ── 7. the sweep asks the broker before it settles ─────────────────────────
print("\n7  the settle sweep re-polls live orders first")

class FakeClient:
    def __init__(self):
        self.polls = 0

    def history_orders(self, start, end):
        self.polls += 1
        import pandas as pd
        df = pd.DataFrame([])
        df.attrs["complete"] = True
        return df

executor._last_live_order_poll = 0.0
c = FakeClient()
executor._refresh_live_orders(c)
check("a live order triggers a broker poll", c.polls == 1)

executor._refresh_live_orders(c)
check("a second call inside the window is throttled", c.polls == 1)

executor._last_live_order_poll = 0.0
executor._refresh_live_orders(c)
check("...and polls again once the window passes", c.polls == 2)

class Boom(FakeClient):
    def history_orders(self, start, end):
        raise RuntimeError("OpenD down")

executor._last_live_order_poll = 0.0
executor._refresh_live_orders(Boom())
check("a failed poll never propagates out of the manage tick", True)

# ── 8. booking a ghost closes the books on the order it came from ─────────
print("\n8  a booked ghost cannot be settled a second time")

from src import fill_settler                          # noqa: E402

coid = logged_order(symbol="FTNT", kind="EXIT", intent="SL",
                    broker_id="4242", qty=9)
before = order_log.get(coid)
check("the order starts live with nothing filled",
      before["state"] == "SUBMITTED" and int(before["filled_qty"]) == 0)

# reconcile books the close from broker evidence, then squares our log.
reconcile._settle_out_our_order("4242", 9, 262.5)
after = order_log.get(coid)
check("the fill is now recorded", int(after["filled_qty"]) == 9)
check("...and marked already applied", int(after["applied_qty"]) == 9)
check("...and the order is terminal", after["state"] == "FILLED")
check("so the settler has nothing left to apply",
      fill_settler.unapplied(after) == (0, 0.0))

# The hazard in full: settle_all must not pick it up. If it did, _apply_sell
# would find no position (reconcile dropped it), read qty > held as an
# oversell, and halt trading over a close that was booked correctly.
out = fill_settler.settle_all()
check("settle_all skips it entirely", out["orders"] == 0)
check("...and nothing failed, so nothing halted", out["failures"] == [])

check("marking twice is idempotent",
      fill_settler.mark_externally_booked(coid, 9, 262.5,
                                          booked_by="test") is False)
check("an unknown broker id is a no-op, not an error",
      reconcile._settle_out_our_order("no-such-order", 1, 1.0) is None)


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
