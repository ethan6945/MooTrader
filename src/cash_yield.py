"""Bear-market cash-yield sweep — earn "petty money" when the strategy is idle.

WHY THIS EXISTS
  When SPY is in a BEAR regime the bot pauses new long entries (regime gate) and
  the cash just SITS there earning 0%. This module parks that idle cash in a
  T-bill ETF (SGOV by default, ~4–5%/yr, near-zero duration risk, very liquid)
  so the capital keeps working, then unwinds back to cash the moment the regime
  is tradeable again. This is the HONEST version of "make money in a bad market":
  not magic alpha — just stop leaving risk-free yield on the table.

SCOPE (deliberately conservative)
  Default only sweeps during a BEAR regime (CASH_YIELD_ONLY_BEAR=true) — the
  exact window where the strategy is paused, so the sweep can never starve a
  real entry of cash. On every non-BEAR scan it fully unwinds (sells all SGOV)
  BEFORE the strategy's entry logic runs, freeing cash for longs again. Keeps a
  liquid buffer and ignores dust-sized trades. DEFAULT OFF.

  SGOV is excluded from reconcile orphan-adoption (see reconcile.py) so it is
  never mistaken for a strategy position; it is a cash equivalent, not a trade.

ORDER MODEL
  Reuses the same marketable-limit path as the strategy (place_limit_order):
  buy at last×1.001, sell at last×0.999. SGOV moves in cents, so these fill.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import db
from .config import settings

log = logging.getLogger(__name__)

_K_HISTORY = "cash_yield_history"


# ── config readers (runtime db-state override wins, else .env) ────────────────

def enabled() -> bool:
    try:
        v = db.get_state().get("cash_yield_enabled")
        if v is not None:
            return bool(v)
    except Exception:
        pass
    return settings.cash_yield_enabled


def symbol() -> str:
    return settings.cash_yield_symbol


# ── pure decision (unit-testable, no I/O) ────────────────────────────────────

def decide(regime_label: str, current_cash: float, held_qty: int,
           price: float) -> dict:
    """Decide the sweep action. Pure: no broker / db calls.

    Returns {"action": "buy"|"sell"|"hold", "qty": int, "reason": str}.

    Logic:
      • Tradeable regime (not BEAR, when ONLY_BEAR) → we must be OUT of the ETF:
        sell the whole holding so cash is free for strategy longs.
      • BEAR regime → we want to be IN: buy as many shares as (cash − buffer)
        affords, provided the trade clears the dust minimum.
    """
    want_in = (regime_label == "BEAR") if settings.cash_yield_only_bear else True

    if not want_in:
        if held_qty > 0:
            return {"action": "sell", "qty": held_qty,
                    "reason": f"regime {regime_label} tradeable — free cash for longs"}
        return {"action": "hold", "qty": 0, "reason": "tradeable & flat — nothing to unwind"}

    # want_in (BEAR): deploy idle cash above the liquidity buffer.
    if price <= 0:
        return {"action": "hold", "qty": 0, "reason": "no valid ETF price"}
    investable = current_cash - settings.cash_yield_buffer_usd
    if investable < settings.cash_yield_min_trade_usd:
        return {"action": "hold", "qty": 0,
                "reason": f"idle cash ${max(investable,0):.0f} < min sweep "
                          f"${settings.cash_yield_min_trade_usd:.0f}"}
    qty = int(investable / (price * 1.001))   # account for the marketable offset
    if qty < 1:
        return {"action": "hold", "qty": 0, "reason": "less than 1 share affordable"}
    return {"action": "buy", "qty": qty,
            "reason": f"BEAR — park ${qty * price:.0f} idle cash in {symbol()} for yield"}


# ── execution (thin wrapper over the existing client) ────────────────────────

def manage(client, regime_label: str, current_cash: float,
           broker_positions) -> dict:
    """Run one sweep tick. No-op unless enabled. Places at most ONE order.

    `broker_positions` is the get_positions() DataFrame; we read our ETF's held
    qty from it. Returns the action taken (also handy for tests / logging).
    """
    if not enabled():
        return {"action": "disabled"}

    # Closed in code, not by a flag.
    #
    # This sleeve records a position from the REQUESTED quantity the moment
    # place_order returns, and books a theoretical PnL when it sells — the exact
    # pattern the main strategy was moved off. It does not go through
    # fill_settler, so running it would put positions and PnL into the books
    # that no fill supports; and it runs BEFORE the kill switch, so nothing
    # downstream would catch that.
    #
    # A flag is a thing someone turns on to see what happens. This comes back
    # when it settles fills like every other path.
    return {"action": "disabled", "reason": "not settled through fill_settler"}

    sym = symbol()
    held_qty = _held_qty(broker_positions, sym)

    # Fetch a validated last price via the executor's guarded helper.
    from .executor import _last_price
    price = _last_price(client, sym)
    if price is None or price <= 0:
        log.info("cash_yield: no valid price for %s — skip", sym)
        return {"action": "hold", "reason": "no price"}

    plan = decide(regime_label, current_cash, held_qty, price)
    action, qty = plan["action"], plan["qty"]
    if action == "hold" or qty <= 0:
        return plan

    from moomoo import TrdSide
    try:
        if action == "buy":
            limit_px = round(price * 1.001, 2)
            client.place_limit_order(sym, qty, limit_px, TrdSide.BUY,
                                     kind='ENTRY', intent='cash sleeve')
            msg = (f"🏦 *现金生息* — 熊市闲置资金买入 {sym} {qty} 股 @≈${limit_px}\n"
                   f"  ≈${qty * price:.0f} 停泊在国债 ETF 吃 ~4-5% 无风险年化，"
                   f"市场转好会自动卖出换回现金。")
        else:  # sell
            limit_px = round(price * 0.999, 2)
            client.place_limit_order(sym, qty, limit_px, TrdSide.SELL,
                                     kind='EXIT', intent='cash sleeve')
            msg = (f"🏦 *现金生息 · 解除* — 卖出 {sym} {qty} 股 @≈${limit_px}\n"
                   f"  市场恢复可交易，资金换回现金供策略开多。")
    except Exception as e:
        log.warning("cash_yield %s %s×%d failed: %s", action, sym, qty, e)
        return {"action": action, "qty": qty, "error": str(e)}

    log.info("cash_yield: %s %s ×%d @≈$%.2f (%s)", action, sym, qty, price, plan["reason"])
    _record({"ts": _now(), "action": action, "symbol": sym, "qty": qty,
             "price": round(price, 2), "reason": plan["reason"]})
    try:
        from . import notifier
        notifier.send(msg)
    except Exception:
        pass
    return {"action": action, "qty": qty, "price": price, "reason": plan["reason"]}


# ── helpers ──────────────────────────────────────────────────────────────────

def _held_qty(positions, sym: str) -> int:
    """Shares of `sym` currently held at the broker (0 if none / df empty)."""
    try:
        if positions is None or positions.empty:
            return 0
        codes = positions["code"].str.split(".").str[-1]
        row = positions[codes == sym]
        if row.empty:
            return 0
        return int(float(row.iloc[0]["qty"]))
    except Exception:
        return 0


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _record(entry: dict) -> None:
    try:
        hist = list(db.get_state().get(_K_HISTORY, []) or [])
        hist.append(entry)
        db.update_state({_K_HISTORY: hist[-100:]})
    except Exception as e:
        log.debug("cash_yield history write skipped: %s", e)
