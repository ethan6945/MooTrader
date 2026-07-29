"""Regression tests for the 2026-07-28 DDOG incident and its root cause.

What happened, in order of causation:

  1. May-June 2026: eight exits each executed TWICE (a sell reached the broker,
     something after it raised, the trade record survived, the next cycle sold
     the same shares again). Each duplicate turned a flat position into a naked
     SHORT: XLF, MSFT, SWKS, INTC, MCHP, HPQ, DDOG, GOOGL.
  2. Nothing noticed, because every position read in the codebase filtered
     `qty > 0` — a short is invisible to a long-only bot.
  3. 2026-07-28: the bot bought 2 DDOG. The fill NETTED against the -3 short
     (leaving -1) instead of opening a long.
  4. reconcile saw no long, classified it a GHOST, and — because paper trading
     refuses deal queries, so the fill lookup could never succeed — priced the
     "exit" off a live quote and booked a +$2.2 MANUAL_SELL, telling the owner
     they had sold it. They hadn't. Nobody had.

Four guarantees are pinned here, one per link in that chain:

  A. An exit sell is never placed for shares the broker doesn't hold (1).
  B. Short positions are read and reported, not filtered away (2).
  C. A buy is refused on any symbol the account is net short (3).
  D. A close is attributed from broker evidence, and books P&L only when a
     real dealt sell order backs it (4).

Standalone (no pytest): `python3 scripts/test_short_drift.py`.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolate every write (SQLite, state.json, quarantine file) from the live data
# dir — these tests book trades and mutate P&L state.
_HOME = tempfile.mkdtemp(prefix="mmt-short-drift-")
os.environ["MMT_HOME"] = _HOME
os.makedirs(f"{_HOME}/data", exist_ok=True)
if (ROOT / ".env").exists():
    shutil.copy(ROOT / ".env", f"{_HOME}/.env")

logging.basicConfig(level=logging.CRITICAL)

import pandas as pd                                          # noqa: E402
import src.executor as ex                                    # noqa: E402
import src.reconcile as rc                                   # noqa: E402
from src import db, notifier                                 # noqa: E402

SENT: list[str] = []
notifier.send = lambda msg, *a, **k: SENT.append(msg)        # no real Telegram

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  — {detail}" if detail else ""))


class FakeClient:
    """Minimal broker stand-in.

    `rows` is the position frame (signed qty, negative = short). `sells` is what
    find_dealt_sell_orders returns. `positions_raise` simulates an unreachable
    broker.
    """

    def __init__(self, rows=None, sells=None, buy_status="FILLED_ALL",
                 positions_raise=False):
        self.rows = rows or []
        self.sells = sells or []
        self.buy_status = buy_status
        self.positions_raise = positions_raise
        self.placed: list[tuple] = []

    def get_positions(self):
        if self.positions_raise:
            raise RuntimeError("OpenD unreachable")
        return pd.DataFrame(self.rows)

    def get_short_symbols(self):
        return rc.short_symbols(self.get_positions())

    def place_limit_order(self, symbol, qty, price, side):
        self.placed.append((symbol, qty, price))
        return "test-order"

    def get_order_fill(self, order_id):
        return None

    def get_order_status(self, order_id):
        return self.buy_status

    def find_dealt_sell_orders(self, symbol, lookback_days=7):
        return list(self.sells)

    def get_kline(self, symbol, bars=30):
        raise RuntimeError("no klines under test")


DDOG_TRADE = {
    "symbol": "DDOG", "qty": 2, "entry_price": 252.36, "stop_loss": 241.75,
    "take_profit": 281.89, "atr": 3.5, "half_closed": 0,
    "buy_order_id": "3136211", "stop_order_id": None, "tp_order_id": None,
    "opened_at": "2026-07-28T15:16:20.229633",   # naive UTC == 11:16:20 ET
    "high_water": 254.9, "low_water": 252.26, "strategy": "trend",
}
HOLD_MRK = [{"code": "US.MRK", "qty": 7.0, "cost_price": 131.885}]


def reset_trades(*seed: dict) -> None:
    for s in list(db.load_open_trades()):
        db.delete_open_trade(s)
    for t in seed:
        db.upsert_open_trade(dict(t))
    SENT.clear()


def n_closed() -> int:
    with db.conn() as c:
        return c.execute("select count(*) from closed_trades").fetchone()[0]


def last_closed() -> tuple:
    with db.conn() as c:
        r = c.execute("select symbol, exit, exit_reason, pnl from closed_trades "
                      "order by id desc limit 1").fetchone()
    return tuple(r) if r else ()


def fix_for(res: dict, symbol: str) -> dict:
    return next((f for f in res["fixes_applied"] if f["symbol"] == symbol), {})


# --------------------------------------------------------------------------
print("\nA. Duplicate-sell guard — the origin of all eight shorts")
# --------------------------------------------------------------------------
c = FakeClient([{"code": "US.DDOG", "qty": 3.0, "cost_price": 250.0}])
ex._sell_and_book_price(c, "DDOG", 3, 247.0, 247.7, "SL")
check("normal exit places the sell", c.placed == [("DDOG", 3, 247.0)], str(c.placed))

c = FakeClient(HOLD_MRK)          # DDOG already gone — this is the retry
try:
    ex._sell_and_book_price(c, "DDOG", 3, 247.0, 247.7, "SL")
    check("retry after a completed exit is blocked", False, "sell was placed")
except RuntimeError:
    check("retry after a completed exit is blocked", not c.placed)

c = FakeClient([{"code": "US.DDOG", "qty": -3.0, "cost_price": 247.7}])
try:
    ex._sell_and_book_price(c, "DDOG", 3, 247.0, 247.7, "SL")
    check("selling into an existing short is blocked", False, "sell was placed")
except RuntimeError:
    check("selling into an existing short is blocked", not c.placed)

c = FakeClient([{"code": "US.DDOG", "qty": 6.0, "cost_price": 250.0}])
ex._sell_and_book_price(c, "DDOG", 3, 247.0, 247.7, "TP1")
check("scale-out tranche of a held position is allowed", len(c.placed) == 1)

c = FakeClient([{"code": "US.DDOG", "qty": 3.0, "cost_price": 250.0}])
try:
    ex._sell_and_book_price(c, "DDOG", 5, 247.0, 247.7, "SL")
    check("overselling our actual holding is blocked", False, "sell was placed")
except RuntimeError:
    check("overselling our actual holding is blocked", not c.placed)

c = FakeClient(positions_raise=True)
ex._sell_and_book_price(c, "DDOG", 3, 247.0, 247.7, "SL")
check("guard fails OPEN — a broker error can't block a protective exit",
      len(c.placed) == 1)

# --------------------------------------------------------------------------
print("\nB. Shorts are read, not filtered away")
# --------------------------------------------------------------------------
frame = pd.DataFrame(HOLD_MRK + [
    {"code": "US.DDOG", "qty": -1.0, "cost_price": 238.625},
    {"code": "US.GOOGL", "qty": -10.0, "cost_price": 353.75},
])
check("net_positions keeps the sign",
      rc.net_positions(frame) == {"MRK": 7.0, "DDOG": -1.0, "GOOGL": -10.0},
      str(rc.net_positions(frame)))
check("short_symbols finds every short",
      rc.short_symbols(frame) == {"DDOG", "GOOGL"})

reset_trades({**DDOG_TRADE, "symbol": "MRK", "qty": 7, "entry_price": 132.06,
              "stop_loss": 128.31, "take_profit": 139.01,
              "buy_order_id": "3133892", "opened_at": "2026-07-27T15:31:01.660762"})
res = rc.reconcile(frame, auto_fix=False, client=FakeClient(frame.to_dict("records")))
check("reconcile reports SHORT_DRIFT",
      {s["symbol"] for s in res["shorts"]} == {"DDOG", "GOOGL"}, str(res["shorts"]))
check("short drift is reported, never auto-fixed", not res["fixes_applied"])
check("short drift alone does not trip the halt streak", not res["severe"])

# --------------------------------------------------------------------------
print("\nC. No entry on a symbol the account is short")
# --------------------------------------------------------------------------
sig = type("Signal", (), {"symbol": "DDOG", "price": 252.36, "atr": 3.5,
                          "structural_stop": None})()
short_client = FakeClient([{"code": "US.DDOG", "qty": -1.0, "cost_price": 238.6}])
check("buy refused while net short",
      ex.open_position(short_client, sig, 2) is None and not short_client.placed)
check("skip reason is net_short", ex.last_entry_skip()[0] == "net_short",
      str(ex.last_entry_skip()))

# --------------------------------------------------------------------------
print("\nD. Closes are attributed from evidence, not assumed")
# --------------------------------------------------------------------------
# D1 — THE INCIDENT: the buy netted against a short. Not a sale at all.
reset_trades(DDOG_TRADE)
before = n_closed()
res = rc.reconcile(pd.DataFrame(HOLD_MRK + [
    {"code": "US.DDOG", "qty": -1.0, "cost_price": 238.625}]),
    client=FakeClient())
check("netted-against-short record is dropped", "DDOG" not in db.load_open_trades())
check("NO P&L booked (was a fabricated +$2.2)", n_closed() == before)
check("flagged as netted, not sold",
      fix_for(res, "DDOG").get("netted_against_short") == -1, str(res["fixes_applied"]))
check("owner is told it was NOT a sale", any("并没有被卖出" in m for m in SENT))

# D2 — a real manual sell at the broker: evidence exists, so book it.
reset_trades(DDOG_TRADE)
before = n_closed()
rc.reconcile(pd.DataFrame(HOLD_MRK), client=FakeClient(sells=[
    {"order_id": "9999", "price": 253.46, "qty": 2,
     "time": "2026-07-28 11:29:00", "status": "FILLED_ALL"}]))
check("real manual sell is booked", n_closed() == before + 1)
check("booked at the broker's actual fill, not a live quote",
      last_closed()[1] == 253.46, str(last_closed()))
check("attributed MANUAL_SELL — order id is not ours",
      last_closed()[2] == "MANUAL_SELL", str(last_closed()))

# D3 — our own stop fired but never got recorded: that is the BOT, not the owner.
reset_trades({**DDOG_TRADE, "stop_order_id": "7777"})
before = n_closed()
rc.reconcile(pd.DataFrame(HOLD_MRK), client=FakeClient(sells=[
    {"order_id": "7777", "price": 241.80, "qty": 2,
     "time": "2026-07-28 12:00:00", "status": "FILLED_ALL"}]))
check("unrecorded bot exit is booked", n_closed() == before + 1)
check("attributed to the bot, not the owner",
      last_closed()[2] == "BOT_SELL_UNRECORDED", str(last_closed()))

# D4 — no sell anywhere: quarantine it, never invent a price.
reset_trades(DDOG_TRADE)
before = n_closed()
rc.reconcile(pd.DataFrame(HOLD_MRK), client=FakeClient(sells=[]))
quarantine = Path(_HOME) / "data" / "unexplained_closes.jsonl"
check("record dropped (broker is authoritative)", "DDOG" not in db.load_open_trades())
check("NO P&L invented", n_closed() == before)
check("quarantined for review", quarantine.exists())
if quarantine.exists():
    rec = json.loads(quarantine.read_text().strip().splitlines()[-1])
    check("quarantine keeps the entry details", rec["symbol"] == "DDOG" and rec["qty"] == 2)

# D5 — a sell that predates our entry belongs to an earlier round trip.
reset_trades(DDOG_TRADE)
before = n_closed()
rc.reconcile(pd.DataFrame(HOLD_MRK), client=FakeClient(sells=[
    {"order_id": "111", "price": 250.00, "qty": 3,
     "time": "2026-06-03 10:57:39", "status": "FILLED_ALL"}]))
check("pre-entry sell is not accepted as evidence", n_closed() == before)

# D6 — the 2026-07-09 phantom guard must still hold.
reset_trades(DDOG_TRADE)
before = n_closed()
res = rc.reconcile(pd.DataFrame(HOLD_MRK),
                   client=FakeClient(buy_status="CANCELLED_ALL"))
check("unfilled buy drops as a phantom, no P&L", n_closed() == before)
check("flagged phantom", fix_for(res, "DDOG").get("phantom") is True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
shutil.rmtree(_HOME, ignore_errors=True)
sys.exit(1 if FAIL else 0)
