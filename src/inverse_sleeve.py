"""Inverse-ETF sleeve — profit from confirmed downtrends in a CASH account.

WHY THIS EXISTS
  A cash account can't short. The only way to make money when the market falls
  is to BUY an inverse ETF (SH = −1× S&P 500, or SQQQ = −3× Nasdaq). This module
  runs a small, trend-following sleeve that goes long the inverse ETF only when a
  downtrend is CONFIRMED, and exits the moment the trend or the bear regime ends.

THE RULE (deliberately strict — inverse ETFs bleed in chop)
  ENTER  when ALL of: regime == BEAR (SPY below its 200 & 50 SMA) AND the inverse
         ETF is in its OWN uptrend (price > its SMA-N, i.e. the market downtrend
         is actually underway, not a falling-knife guess) AND we're flat.
  EXIT   on the FIRST of: regime no longer BEAR · inverse ETF breaks below its
         SMA-N (trend over) · ATR stop hit (market rallied against us) · ATR
         take-profit · max-hold days.
  SIZE   capped at INVERSE_SLEEVE_MAX_PCT of the budget (a SLEEVE, not the book).

⚠ NOT VALIDATED — DEFAULT OFF. This is the only feature here that can lose money
  in a NEW way, so it ships exactly like the pattern strategy did: inert until
  YOU run scripts/inverse_sleeve_backtest.py and it clears the dual-window gate
  (does it add net return over sitting in cash/SGOV during bear windows WITHOUT
  materially worsening drawdown?). Only then flip INVERSE_SLEEVE_ENABLED=true.

ISOLATION
  Tracked in its OWN db-state slot ('inverse_sleeve_position'), never in
  open_trades — so the strategy executor's exit logic never touches it. The ETF
  is excluded from reconcile (like the cash-yield ETF).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas_ta_classic as ta

from . import db
from .config import settings

log = logging.getLogger(__name__)

_K_POS = "inverse_sleeve_position"
_K_HISTORY = "inverse_sleeve_history"


# ── config readers ───────────────────────────────────────────────────────────

def enabled() -> bool:
    try:
        v = db.get_state().get("inverse_sleeve_enabled")
        if v is not None:
            return bool(v)
    except Exception:
        pass
    return settings.inverse_sleeve_enabled


def symbol() -> str:
    return settings.inverse_sleeve_symbol


# ── indicators (pure) ────────────────────────────────────────────────────────

def indicators(df) -> dict | None:
    """Return {price, sma, atr} from daily bars, or None if too few bars."""
    n = max(settings.inverse_sleeve_sma_len, settings.inverse_sleeve_atr_len) + 2
    if df is None or len(df) < n:
        return None
    close, high, low = df["close"], df["high"], df["low"]
    try:
        sma = float(ta.sma(close, length=settings.inverse_sleeve_sma_len).iloc[-1])
        atr = float(ta.atr(high, low, close, length=settings.inverse_sleeve_atr_len).iloc[-1])
    except Exception:
        return None
    return {"price": float(close.iloc[-1]), "sma": sma, "atr": atr}


# ── pure entry / exit decisions (unit-testable) ──────────────────────────────

def entry_signal(regime_label: str, ind: dict) -> dict | None:
    """Return an entry plan {price, stop, tp, atr} or None.

    Confirmed-downtrend rule: BEAR regime AND the inverse ETF trades above its
    own SMA-N (its uptrend == market's downtrend). ATR-based stop & take-profit.
    """
    if regime_label != "BEAR":
        return None
    if ind is None or ind["atr"] <= 0:
        return None
    price, sma, atr = ind["price"], ind["sma"], ind["atr"]
    if price <= sma:                      # inverse ETF not yet trending up → wait
        return None
    stop = round(price - settings.inverse_sleeve_sl_atr * atr, 2)
    tp = round(price + settings.inverse_sleeve_tp_atr * atr, 2)
    return {"price": round(price, 2), "stop": stop, "tp": tp, "atr": round(atr, 4)}


def exit_signal(regime_label: str, ind: dict, position: dict) -> str | None:
    """Return an exit reason or None for a held inverse-sleeve position."""
    if ind is None:
        return None
    price, sma = ind["price"], ind["sma"]
    if regime_label != "BEAR":
        return "regime no longer BEAR"
    if price <= sma:
        return "inverse ETF broke below SMA — downtrend over"
    if price <= float(position.get("stop", 0)):
        return "ATR stop hit (market rallied)"
    if price >= float(position.get("tp", 1e9)):
        return "ATR take-profit hit"
    opened = position.get("opened_at")
    if opened:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(opened)).days
            if age >= settings.inverse_sleeve_max_hold_days:
                return f"max-hold {settings.inverse_sleeve_max_hold_days}d reached"
        except Exception:
            pass
    return None


def size(price: float, stop: float, budget: float) -> int:
    """Shares to buy: ATR-risk sizing capped at MAX_PCT of budget."""
    if price <= 0 or budget <= 0:
        return 0
    risk_dollars = budget * settings.risk_per_trade
    stop_dist = price - stop
    qty_by_risk = int(risk_dollars / stop_dist) if stop_dist > 0 else 0
    qty_by_cap = int(budget * settings.inverse_sleeve_max_pct / price)
    return max(0, min(qty_by_risk, qty_by_cap))


# ── execution (thin wrapper, self-contained) ─────────────────────────────────

def manage(client, regime_label: str, budget: float) -> dict:
    """One sleeve tick. No-op unless enabled. Places at most ONE order."""
    if not enabled():
        return {"action": "disabled"}

    sym = symbol()
    from moomoo import KLType, TrdSide
    try:
        df = client.get_kline(sym, bars=260, ktype=KLType.K_DAY)
    except Exception as e:
        log.info("inverse_sleeve: kline fetch for %s failed: %s", sym, e)
        return {"action": "hold", "reason": "no data"}
    ind = indicators(df)
    if ind is None:
        return {"action": "hold", "reason": "insufficient bars"}

    pos = db.get_state().get(_K_POS)

    # ----- holding → check exit -----
    if pos and int(pos.get("qty", 0)) > 0:
        reason = exit_signal(regime_label, ind, pos)
        if not reason:
            return {"action": "hold", "reason": "in trade, no exit signal"}
        qty = int(pos["qty"])
        px = round(ind["price"] * 0.999, 2)
        try:
            client.place_limit_order(sym, qty, px, TrdSide.SELL,
                                     kind='EXIT', intent='inverse sleeve')
        except Exception as e:
            log.warning("inverse_sleeve sell failed: %s", e)
            return {"action": "sell", "qty": qty, "error": str(e)}
        pnl = (ind["price"] - float(pos["entry"])) * qty
        db.update_state({_K_POS: None})
        _record({"ts": _now(), "action": "sell", "symbol": sym, "qty": qty,
                 "price": round(ind["price"], 2), "pnl": round(pnl, 2), "reason": reason})
        _notify(f"🔻 *反向对冲 · 平仓* {sym} {qty} 股 @≈${px} — {reason}\n"
                f"  本笔盈亏 ≈${pnl:+.0f}。")
        return {"action": "sell", "qty": qty, "pnl": pnl, "reason": reason}

    # ----- flat → check entry -----
    plan = entry_signal(regime_label, ind)
    if not plan:
        return {"action": "hold", "reason": "no entry signal"}
    qty = size(plan["price"], plan["stop"], budget)
    if qty < 1:
        return {"action": "hold", "reason": "qty<1 after sizing/caps"}
    px = round(ind["price"] * 1.001, 2)
    try:
        client.place_limit_order(sym, qty, px, TrdSide.BUY,
                                 kind='ENTRY', intent='inverse sleeve')
    except Exception as e:
        log.warning("inverse_sleeve buy failed: %s", e)
        return {"action": "buy", "qty": qty, "error": str(e)}
    rec = {"symbol": sym, "qty": qty, "entry": plan["price"], "stop": plan["stop"],
           "tp": plan["tp"], "atr": plan["atr"], "opened_at": _now()}
    db.update_state({_K_POS: rec})
    _record({"ts": _now(), "action": "buy", **rec})
    _notify(f"🔺 *反向对冲 · 开仓* 熊市确认下跌，买入 {sym} {qty} 股 @≈${px}\n"
            f"  止损 ${plan['stop']} / 止盈 ${plan['tp']}。市场反弹或趋势结束会自动平仓。")
    return {"action": "buy", "qty": qty, **plan}


# ── helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _record(entry: dict) -> None:
    try:
        hist = list(db.get_state().get(_K_HISTORY, []) or [])
        hist.append(entry)
        db.update_state({_K_HISTORY: hist[-100:]})
    except Exception as e:
        log.debug("inverse_sleeve history write skipped: %s", e)


def _notify(msg: str) -> None:
    try:
        from . import notifier
        notifier.send(msg)
    except Exception:
        pass
