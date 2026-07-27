"""Regression tests for exit BOOKING PRICE (2026-07-27).

Every exit used to book P&L at the pre-order quote (`last`) while submitting a
marketable limit up to PROTECTIVE_EXIT_SLIP (3%) below it — so real slippage
never reached trades.jsonl. That file is the input to half-Kelly, the AI
optimizer, the blacklist and adaptive sizing, which means the whole
self-improvement loop was reading slippage-free numbers. Exits now read the
broker's dealt_avg_price back (executor._sell_and_book_price /
_bracket_fill_price) and book THAT.

Pinned here, all through the real _manage_one code path with a fake broker:

  1. Soft stop-loss — order price unchanged (last × (1 − slip)); the close is
     booked at the broker's fill, and the action dict + pnl follow it.
  2. Fallback — a broker that returns no fill degrades to the OLD quote-priced
     booking rather than blocking the exit (the order is already live by then).
  3. Take-profit full close — booked at fill; TP orders carry no protective slip.
  4. Max-hold force close — booked at fill.
  5. Scale-out TP1 tranche — booked at fill, correct tranche qty.
  6. Backward compatibility — a client object with NO get_order_fill at all
     (older stub / partial mock) must still complete the exit at the quote.

Case 6 is the important one for safety: the fill lookup is best-effort and must
never be able to turn a protective exit into an exception.

Standalone (no pytest): `python3 scripts/test_exit_fill_price.py`
"""
from __future__ import annotations
import logging, sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.CRITICAL)   # silence the informational fill logs

import src.executor as ex
from src.config import settings as real_settings

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


class FakeBroker:
    """Records placed orders; reports a fill at `fill_px` (None = can't tell)."""

    def __init__(self, last: float, fill_px: float | None = None):
        self.last, self.fill_px = last, fill_px
        self.placed: list[dict] = []

    def place_limit_order(self, symbol, qty, price, side):
        self.placed.append({"symbol": symbol, "qty": qty,
                            "price": round(price, 2), "side": str(side)})
        return "OID-1"

    def get_order_fill(self, order_id):
        if self.fill_px is None:
            return None
        return {"price": self.fill_px, "qty": self.placed[-1]["qty"],
                "status": "FILLED_ALL"}

    def get_snapshot(self, symbol):
        return {"last_price": self.last}

    def cancel_order(self, oid):
        return True

    def is_order_filled(self, oid, include_partial=False):
        return False


class LegacyBroker(FakeBroker):
    """A client that predates get_order_fill — attribute access raises."""

    def __getattribute__(self, name):
        if name == "get_order_fill":
            raise AttributeError("get_order_fill")
        return object.__getattribute__(self, name)


def make_trade(entry=100.0, stop=95.0, tp=115.0, qty=10, age_days=0) -> dict:
    return {"symbol": "AAPL", "qty": qty, "entry_price": entry, "stop_loss": stop,
            "take_profit": tp, "atr": 1.5, "half_closed": False,
            "stop_order_id": None, "tp_order_id": None,
            "opened_at": (datetime.utcnow() - timedelta(days=age_days)).isoformat(),
            "high_water": entry, "low_water": entry, "strategy": "trend",
            "qty_initial": qty, "init_risk_per_share": entry - stop,
            "tp1_done": False, "tp2_done": False, "stacks": 1}


def run(last, trade, fill_px=None, scale_out=False, broker_cls=FakeBroker):
    """Drive _manage_one once, capturing what would be booked.

    _close_and_log and _save_open_trades are stubbed so the real trade log,
    risk state and db are never touched by the test.
    """
    c = broker_cls(last, fill_px)
    trades, actions, booked = {"AAPL": trade}, [], {}

    orig = (ex._settings, ex._close_and_log, ex._save_open_trades)
    # Use the REAL settings object with overrides — a SimpleNamespace stub goes
    # stale the moment _manage_one reads a new flag (which is how the older
    # scripts/test_scale_out.py broke).
    ex._settings = replace(real_settings, use_scale_out=scale_out,
                           use_breakeven_stop=False)

    def fake_close(symbol, tr, qty, exit_price, reason):
        booked.update(symbol=symbol, qty=qty, exit_price=exit_price, reason=reason)
        return round((exit_price - tr["entry_price"]) * qty, 2)

    ex._close_and_log = fake_close
    ex._save_open_trades = lambda *a, **k: None
    try:
        ex._manage_one(c, "AAPL", trade, trades, actions)
    finally:
        ex._settings, ex._close_and_log, ex._save_open_trades = orig
    return c, booked, actions


def main() -> int:
    slip = ex.PROTECTIVE_EXIT_SLIP
    print(f"exit booking price — PROTECTIVE_EXIT_SLIP={slip}\n")

    # 1. Soft stop-loss books the broker's fill, not the quote.
    c, booked, acts = run(94.0, make_trade(), fill_px=93.10)
    check("SL: order still placed at last*(1-slip)",
          bool(c.placed) and c.placed[0]["price"] == round(94.0 * (1 - slip), 2),
          f"placed {c.placed[0]['price'] if c.placed else None}")
    check("SL: booked at fill 93.10, not quote 94.00",
          booked.get("exit_price") == 93.10, f"booked {booked.get('exit_price')}")
    check("SL: action dict reports the fill",
          bool(acts) and acts[0].get("price") == 93.10,
          f"action {acts[0].get('price') if acts else None}")
    check("SL: pnl derives from the fill",
          bool(booked) and abs(abs(booked["exit_price"] - 100.0) * 10 - 69.0) < 1e-6,
          f"(93.10-100)*10 = {round((booked.get('exit_price', 0) - 100) * 10, 2)}")

    # 2. No fill reported → old quote-priced behaviour, exit still completes.
    c, booked, acts = run(94.0, make_trade(), fill_px=None)
    check("SL fallback: books the quote when the fill is unreadable",
          booked.get("exit_price") == 94.0, f"booked {booked.get('exit_price')}")
    check("SL fallback: the order was still placed", len(c.placed) == 1)

    # 3. Take-profit.
    c, booked, acts = run(116.0, make_trade(), fill_px=115.80)
    check("TP: booked at fill 115.80", booked.get("exit_price") == 115.80,
          f"booked {booked.get('exit_price')}")
    check("TP: reason is TP", booked.get("reason") == "TP",
          f"reason {booked.get('reason')}")
    check("TP: no protective slip on a TP order",
          bool(c.placed) and c.placed[0]["price"] == 116.0,
          f"placed {c.placed[0]['price'] if c.placed else None}")

    # 4. Max-hold.
    c, booked, acts = run(101.0, make_trade(age_days=30), fill_px=100.55)
    check("MAX_HOLD: booked at fill 100.55",
          booked.get("exit_price") == 100.55 and booked.get("reason") == "MAX_HOLD",
          f"booked {booked.get('exit_price')} reason {booked.get('reason')}")

    # 5. Scale-out TP1. tp1_r × risk above entry must be reached, and the single
    #    take_profit kept above it so the runner's full close doesn't pre-empt.
    risk = 5.0
    tp1_level = 100.0 + real_settings.tp1_r * risk
    c, booked, acts = run(tp1_level + 0.5, make_trade(qty=9, tp=tp1_level + 5),
                          fill_px=tp1_level + 0.4, scale_out=True)
    check("TP1: tranche booked at its fill with 1/3 of the lot",
          booked.get("exit_price") == tp1_level + 0.4 and booked.get("qty") == 3,
          f"booked {booked.get('exit_price')} qty {booked.get('qty')} "
          f"reason {booked.get('reason')}")

    # 6. A client without get_order_fill must not break the protective exit.
    c, booked, acts = run(94.0, make_trade(), broker_cls=LegacyBroker)
    check("legacy client (no get_order_fill): exit completes at the quote",
          booked.get("exit_price") == 94.0 and len(c.placed) == 1,
          f"booked {booked.get('exit_price')}, placed {len(c.placed)}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
