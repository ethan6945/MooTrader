"""Order execution layer.

Places a limit buy at the signal price, then attaches a sell-stop at the ATR
stop-loss level. The take-profit half is tracked locally in `data/state.json`
and resolved on the next scan (the broker doesn't natively bracket OCOs across
contexts in the simple SDK path).
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytz

_ET = pytz.timezone("America/New_York")

from moomoo import TrdSide

from .config import settings as _settings  # noqa: F401 (used in caller too)
from . import db, portfolio, risk_manager, runtime_config

from .config import settings
from .indicators import Signal
from .moo_client import MooClient

ORDER_TIMEOUT_MIN = 5    # cancel unfilled BUY orders after this many minutes
# Protective exits (soft stop-loss, force-close, max-hold, stall-out) are
# marketable limits — priced BELOW last so they fill even through a fast
# gap-down. The old 0.5% (last×0.995) was too tight: in a gap the price blows
# straight through it and the position is left unprotected. 3% gives fill
# headroom while bounding worst-case slippage. (Bug fix 2026-06-03.)
PROTECTIVE_EXIT_SLIP = 0.03

# Phase 0 (2026-06-10): entry limits are priced off the live quote, not the
# (possibly session-stale) signal bar close. If the market already ran more
# than this fraction past the signal price, skip — the backtest's open-only
# fill model wouldn't have filled there either.
ENTRY_CHASE_TOL = 0.002

# 2026-07-16: the mirror guard for the crash side. If the live quote sits more
# than this fraction BELOW the signal bar close, the score/ATR/stop were all
# computed on data that no longer describes the market — skip the entry.
# (2026-07-15 night: hourly klines lagged a full session during the IBM-crash
# open; six entries fired with signal prices 3–8% above the live market and
# every one died inside an hour, DELL with its "stop" ABOVE its entry.)
STALE_SIGNAL_TOL = 0.02

# Why open_position() most recently returned None — (gate, reason) consumed by
# main.py's skip audit so cooldown / chase / stale-signal skips are labelled
# truthfully instead of all landing under gate="chase". Entries only run on the
# scan thread (under _TRADES_LOCK), so a single slot is safe.
_LAST_ENTRY_SKIP: tuple[str, str] = ("chase", "market ran past signal price")


def last_entry_skip() -> tuple[str, str]:
    """(gate, reason) for the most recent open_position() skip (None return)."""
    return _LAST_ENTRY_SKIP

# Serializes every load→mutate→save cycle on the open-trades store. The scan
# job and the 5-min manage tick run on different scheduler threads; without
# this, one writer can clobber the other's whole-dict save (a freshly opened
# position vanishes from the record → reconcile later "adopts" it back as an
# orphan with a fabricated stop).
_TRADES_LOCK = threading.RLock()


def has_open_trades() -> bool:
    """Cheap pre-check so the 5-min manage tick can skip opening a broker
    connection when the book is flat."""
    try:
        return bool(_load_open_trades())
    except Exception:
        return True   # unsure → let the tick run and find out properly


def _business_days_between(start: datetime, end: datetime) -> int:
    """Count weekdays (Mon-Fri) minus NYSE holidays between `start` (exclusive)
    and `end` (inclusive).

    Backtest measures max_hold in TRADING bars (HOUR_1 → 7 bars / day × 7 days
    = 49 hour-bars ≈ 7 trading days). Live executor used calendar days, which
    means a Mon→next Mon trade was 7 calendar days but only 5 trading days —
    so live closed 2 trading days earlier than backtest and ate the rebalance
    cost on otherwise-winning swings.

    2026-05-28: extended to subtract NYSE holidays so 1-day drift around the
    ~10 yearly holidays doesn't leak into max_hold counting.
    """
    if end <= start:
        return 0
    # Lazy import to avoid main→executor circular ref at module load.
    from .main import _nyse_holidays
    days = 0
    d = start.date()
    last = end.date()
    # Holiday lookup spans at most one year — pull once.
    holidays = _nyse_holidays(d.year)
    if last.year != d.year:
        holidays = holidays | _nyse_holidays(last.year)
    while d < last:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in holidays:
            days += 1
    return days


def place_bracket(
    client: MooClient,
    symbol: str,
    qty: int,
    stop_price: float,
    tp_price: float,
) -> tuple[str | None, str | None]:
    """Place SELL-stop + SELL-limit pair on REAL accounts. Returns (stop_id, tp_id).

    Either one filling means the position is (at least partly) gone, and the
    other leg must be cancelled — done in `manage_open_trades`.  Either side
    raising is logged and returned as None; the caller decides whether to
    fall back to soft tracking."""
    stop_id, tp_id = None, None
    try:
        stop_id = client.place_stop_loss(symbol, qty, stop_price)
        log.info("Bracket STOP attached %s qty=%d @ $%.2f (id=%s)",
                 symbol, qty, stop_price, stop_id)
    except Exception as e:
        log.error("Bracket STOP failed for %s: %s", symbol, e)
    try:
        tp_id = client.place_limit_order(symbol, qty, tp_price, TrdSide.SELL)
        log.info("Bracket TP attached %s qty=%d @ $%.2f (id=%s)",
                 symbol, qty, tp_price, tp_id)
    except Exception as e:
        log.error("Bracket TP failed for %s: %s", symbol, e)
    return stop_id, tp_id

log = logging.getLogger(__name__)

OPEN_TRADES_FILE = settings.root / "data" / "open_trades.json"


def _load_open_trades() -> dict:
    """Now reads from SQLite. Old JSON file still mirrored for legacy GUI compat."""
    return db.load_open_trades()


def _save_open_trades(trades: dict) -> None:
    """Replace open_trades table with the supplied dict. Routes through
    `db.upsert_open_trade` so v2 columns + the `extra` JSON blob (stacks,
    high/low water marks, etc.) are preserved across save/load cycles."""
    existing = set(db.load_open_trades().keys())
    incoming = set(trades.keys())
    for sym in existing - incoming:
        db.delete_open_trade(sym)
    for sym, t in trades.items():
        # Ensure symbol key matches dict key.
        t = dict(t)
        t["symbol"] = sym
        db.upsert_open_trade(t)
    # Legacy JSON mirror (kept until all GUI readers migrate; cheap to write).
    try:
        OPEN_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
        OPEN_TRADES_FILE.write_text(json.dumps(trades, indent=2, default=str))
    except Exception as e:
        log.warning("legacy JSON mirror write failed: %s", e)


# --- same-day re-entry cooldown (2026-07-15) ---
# A sentinel/AI risk-off close (GAP_RISK / SMART_EXIT) means "too dangerous to
# hold today" — but the very next scan often still ranks the name top-N and
# buys it straight back (DELL 2026-07-14: gap-closed 01:11, re-bought 01:16,
# gap-closed again 01:21 — two spreads paid for nothing). Block re-entry for
# the rest of the NY trading day. Persisted in db state so a restart keeps it.

def _ny_today() -> str:
    from . import clock
    return clock.ny_now().date().isoformat()


def _set_reentry_cooldown(symbol: str, exit_reason: str) -> None:
    try:
        today = _ny_today()

        def _upd(state: dict) -> dict:
            cd = dict(state.get("reentry_cooldown") or {})
            cd = {k: v for k, v in cd.items() if v.get("date") == today}
            cd[symbol] = {"date": today, "reason": exit_reason}
            return {"reentry_cooldown": cd}

        db.atomic_state(_upd)
    except Exception as e:
        log.warning("could not persist re-entry cooldown for %s: %s", symbol, e)


def reentry_cooldown_reason(symbol: str) -> str | None:
    """Exit reason if `symbol` was risk-off closed TODAY (NY date), else None."""
    try:
        entry = (db.get_state().get("reentry_cooldown") or {}).get(symbol)
    except Exception:
        return None
    if entry and entry.get("date") == _ny_today():
        return entry.get("reason") or "risk-off close"
    return None


def open_position(client: MooClient, signal: Signal, qty: int) -> dict | None:
    """Buy at limit. REAL → attach OCO bracket (STOP + TP); SIMULATE → soft-track.

    Returns None when the entry is SKIPPED (market ran past the signal price —
    the backtest's open-only fill model wouldn't have filled there either).

    If a trade record for this symbol already exists, this is a STACK entry
    (pyramid add-on): merge into the existing position with a weighted-avg
    entry, raise stop/TP to the new signal's levels, and re-place OCO so the
    bracket protects the full combined qty.

    Captures `strategy` at entry so it can be matched against actual outcome
    (R-multiple, MFE/MAE) at close-time → enables calibration."""
    with _TRADES_LOCK:
        return _open_position_locked(client, signal, qty)


def _open_position_locked(client: MooClient, signal: Signal, qty: int) -> dict | None:
    global _LAST_ENTRY_SKIP
    cooldown = reentry_cooldown_reason(signal.symbol)
    if cooldown:
        _LAST_ENTRY_SKIP = ("cooldown",
                            f"re-entry blocked — {cooldown} close earlier this session")
        log.info("%s: re-entry blocked — %s close earlier this session "
                 "(cooldown until next NY trading day)", signal.symbol, cooldown)
        return None

    trades = _load_open_trades()
    existing = trades.get(signal.symbol)
    is_stack = existing is not None and int(existing.get("qty", 0)) > 0

    # Phase 0 (2026-06-10): price the limit off the CURRENT quote, not the
    # signal bar close. At the 09:45/10:15 scans the last CLOSED hourly bar is
    # the prior session's, so a signal-price limit could sit ~18h stale and
    # fill only by adverse selection (price dropping through it). Skip when
    # the market already ran > ENTRY_CHASE_TOL past the signal; otherwise
    # place a marketable limit at the live quote (capped at signal + tol).
    limit_px = round(float(signal.price), 2)
    last = _last_price(client, signal.symbol)
    if last is not None:
        if last > signal.price * (1 + ENTRY_CHASE_TOL):
            _LAST_ENTRY_SKIP = ("chase",
                                f"market ran past signal (${last:.2f} > ${signal.price:.2f})")
            log.info("%s: market ran past signal ($%.2f > $%.2f +%.1f bps) — entry skipped",
                     signal.symbol, last, signal.price, ENTRY_CHASE_TOL * 1e4)
            return None
        # 2026-07-16: crash-side freshness gate. Live far BELOW the signal bar
        # means the bar (and everything scored off it) is stale — refuse to
        # trade a setup the market has already invalidated.
        if last < signal.price * (1 - STALE_SIGNAL_TOL):
            _LAST_ENTRY_SKIP = ("stale_signal",
                                f"live ${last:.2f} is {(signal.price - last) / signal.price:.1%} "
                                f"below signal bar ${signal.price:.2f} — signal stale/invalidated")
            log.warning("%s: live $%.2f sits %.1f%% below signal bar $%.2f — score/stop "
                        "were computed on stale data; entry skipped",
                        signal.symbol, last, (signal.price - last) / signal.price * 100,
                        signal.price)
            return None
        limit_px = round(min(last * 1.001,
                             signal.price * (1 + ENTRY_CHASE_TOL)), 2)
    else:
        log.warning("%s: no live quote — signal freshness unverified; proceeding "
                    "at signal price with fill-anchored stops", signal.symbol)

    # 2026-07-16: anchor protective levels to the ACTUAL entry price, not the
    # signal bar close. Keeps the ATR distances the strategy chose, but measured
    # from where we really bought (2026-07-15: DELL's signal-anchored "stop"
    # 435.25 sat ABOVE its 418.91 entry → stopped out 7 seconds after entry).
    # The structural (swing-low) stop is an absolute level — honor it only when
    # it still sits below the entry.
    stop_px = round(limit_px - runtime_config.sl_atr_mult() * float(signal.atr), 2)
    tp_px = round(limit_px + runtime_config.tp_atr_mult() * float(signal.atr), 2)
    if signal.structural_stop is not None and float(signal.structural_stop) < limit_px:
        stop_px = max(stop_px, round(float(signal.structural_stop), 2))
    if not (0 < stop_px < limit_px < tp_px):
        # Garbage ATR (0/NaN from bad klines) or degenerate rounding — never
        # enter a position whose protective levels are malformed.
        _LAST_ENTRY_SKIP = ("bad_levels",
                            f"malformed protective levels (entry {limit_px}, "
                            f"stop {stop_px}, tp {tp_px}, atr {signal.atr})")
        log.error("%s: refusing entry — malformed protective levels "
                  "(entry %.2f, stop %.2f, tp %.2f, atr %s)",
                  signal.symbol, limit_px, stop_px, tp_px, signal.atr)
        return None

    buy_order_id = client.place_limit_order(
        signal.symbol, qty, limit_px, TrdSide.BUY
    )

    if is_stack:
        old_qty = int(existing["qty"])
        old_entry = float(existing["entry_price"])
        new_total_qty = old_qty + qty
        new_avg_entry = (old_qty * old_entry + qty * limit_px) / new_total_qty
        # Stop/TP trail UP only — never weaken protection on the original lot.
        new_stop = max(float(existing.get("stop_loss", 0)), stop_px)
        new_tp = max(float(existing.get("take_profit", 0)), tp_px)
        stacks = int(existing.get("stacks", 1)) + 1

        # On REAL, replace the OCO bracket so it covers the new combined qty
        # (only when broker brackets are in use — soft-exit mode skips this).
        stop_order_id = existing.get("stop_order_id")
        tp_order_id = existing.get("tp_order_id")
        if settings.moo_trade_env == "REAL" and not settings.real_use_soft_exits:
            for oid in (stop_order_id, tp_order_id):
                if oid:
                    try:
                        client.cancel_order(oid)
                    except Exception as e:
                        log.warning("cancel old bracket leg %s failed: %s", oid, e)
            stop_order_id, tp_order_id = place_bracket(
                client, signal.symbol, new_total_qty, new_stop, new_tp
            )
            if not (stop_order_id and tp_order_id):
                log.warning("Re-bracket incomplete for stack %s — soft tracking fallback",
                            signal.symbol)

        trade = dict(existing)
        trade.update({
            "symbol": signal.symbol,
            "qty": new_total_qty,
            "entry_price": new_avg_entry,
            "stop_loss": new_stop,
            "take_profit": new_tp,
            "atr": signal.atr,
            # Re-anchor scale-out to the new combined lot (stacks only happen in
            # profit, so resetting the R unit off the new avg entry/stop is sane).
            "qty_initial": new_total_qty,
            "init_risk_per_share": max(new_avg_entry - new_stop, 0.0),
            "buy_order_id": buy_order_id,
            "stop_order_id": stop_order_id,
            "tp_order_id": tp_order_id,
            "stacks": stacks,
            "last_stack_at": datetime.utcnow().isoformat(),
            "last_stack_qty": qty,
            "last_stack_price": limit_px,
        })
        # Refresh water-marks to current price for the new combined lot.
        trade["high_water"] = max(float(trade.get("high_water") or limit_px), limit_px)
        log.info("STACK #%d on %s: +%d @ $%.2f → total %d, avg $%.2f, stop $%.2f, tp $%.2f",
                 stacks, signal.symbol, qty, limit_px,
                 new_total_qty, new_avg_entry, new_stop, new_tp)
    else:
        stop_order_id, tp_order_id = None, None
        # REAL with a broker OCO bracket ONLY when soft-exits are off. When
        # REAL_USE_SOFT_EXITS=true, REAL is managed exactly like SIMULATE
        # (soft scale-out/trailing/stop) so live matches the honest backtest.
        if settings.moo_trade_env == "REAL" and not settings.real_use_soft_exits:
            stop_order_id, tp_order_id = place_bracket(
                client, signal.symbol, qty, stop_px, tp_px
            )
            if not (stop_order_id and tp_order_id):
                log.warning("Bracket incomplete for %s (stop=%s tp=%s) — soft tracking active as fallback",
                            signal.symbol, stop_order_id, tp_order_id)
        else:
            mode = "REAL soft-exits" if settings.moo_trade_env == "REAL" else "SIMULATE"
            log.info("%s: soft stop @ $%.2f & TP @ $%.2f tracked locally (scale-out enabled)",
                     mode, stop_px, tp_px)

        trade = {
            "symbol": signal.symbol,
            "qty": qty,
            "entry_price": limit_px,
            "stop_loss": stop_px,
            "take_profit": tp_px,
            "atr": signal.atr,
            "half_closed": False,
            # Scale-out state (used only when USE_SCALE_OUT=true). qty_initial
            # anchors the 1/3 tranche size to the ORIGINAL lot; init_risk_per_share
            # is the R unit (entry − initial stop) for the +TP1_R / +TP2_R levels.
            "qty_initial": qty,
            "init_risk_per_share": max(float(limit_px) - float(stop_px), 0.0),
            "tp1_done": False,
            "tp2_done": False,
            "buy_order_id": buy_order_id,
            "stop_order_id": stop_order_id,
            "tp_order_id": tp_order_id,
            "opened_at": datetime.utcnow().isoformat(),
            # Water-marks start at the entry price — updated each manage tick.
            "high_water": limit_px,
            "low_water": limit_px,
            "strategy": getattr(signal, "strategy", "trend"),
            # Pattern strategy: remember which chart pattern triggered the entry
            # so the dashboard/GUI can badge it (None for the other strategies).
            "pattern": (getattr(signal, "meta", {}) or {}).get("pattern_type"),
            "stacks": 1,
        }

    trades[signal.symbol] = trade
    _save_open_trades(trades)
    return trade


def _close_and_log(symbol: str, trade: dict, qty: int, exit_price: float, reason: str) -> float:
    """Record a close to risk_manager (state) + portfolio (R-multiple, MFE/MAE).
    Returns the realised pnl."""
    pnl = (exit_price - trade["entry_price"]) * qty
    # account_usd omitted on purpose → record_trade_close uses equity_baseline()
    # (frozen seed while auto-compounding is armed), so the DD breaker's equity
    # is never inflated by the compounding deployable budget.
    risk_manager.record_trade_close(pnl)

    entry = float(trade["entry_price"]) or 1e-9
    hw = float(trade.get("high_water") or trade["entry_price"])
    lw = float(trade.get("low_water") or trade["entry_price"])
    mfe_pct = (hw - entry) / entry * 100
    mae_pct = (lw - entry) / entry * 100

    portfolio.record_close(
        symbol=symbol,
        qty=qty,
        entry=trade["entry_price"],
        stop=trade["stop_loss"],
        exit_price=exit_price,
        exit_reason=reason,
        opened_at=trade.get("opened_at", ""),
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        strategy=trade.get("strategy", "trend"),
        initial_risk=trade.get("init_risk_per_share"),
    )
    # Defensive: if init_risk_per_share is missing (legacy trade or data loss),
    # the R-multiple will be 0 because breakeven may have raised stop to entry.
    # Log it so we can detect and fix — this should never fire for new trades.
    if not trade.get("init_risk_per_share"):
        log.warning(
            "R-MULTIPLE MAY BE WRONG: %s close missing init_risk_per_share — "
            "stop=%.2f entry=%.2f → R will fallback to entry-stop calculation. "
            "If breakeven raised stop to entry, R will show 0.0.",
            symbol, float(trade.get("stop_loss", 0)), float(trade.get("entry_price", 0)))
    # Tombstone the close so reconcile's orphan scan ignores the broker's
    # still-filling position during the grace window (in-flight sell ≠ manual buy).
    try:
        from .reconcile import record_recent_close
        record_recent_close(symbol)
    except Exception as e:
        log.debug("recent-close tombstone %s failed: %s", symbol, e)
    return pnl


def cancel_stale_orders(client: MooClient) -> list[dict]:
    """Cancel BUY orders older than ORDER_TIMEOUT_MIN minutes.
    Stale unfilled limits eat budget headroom and let prices drift away."""
    canceled: list[dict] = []
    try:
        pending = client.list_pending_buys()
    except Exception as e:
        log.warning("list_pending_buys failed: %s", e)
        return canceled
    if pending.empty:
        return canceled

    # Use ET (America/New_York) for both sides — SDK create_time is naive ET.
    now_et = datetime.now(_ET).replace(tzinfo=None)
    for _, row in pending.iterrows():
        ct = str(row.get("create_time", ""))
        try:
            created = datetime.fromisoformat(ct.split(".")[0]) if ct else now_et
            age_min = (now_et - created).total_seconds() / 60
        except (ValueError, IndexError):
            age_min = 0.0
        if age_min > ORDER_TIMEOUT_MIN:
            order_id = str(row.get("order_id", ""))
            sym = str(row.get("code", "?")).split(".")[-1]
            if order_id and client.cancel_order(order_id):
                canceled.append({"type": "cancel_stale", "symbol": sym,
                                 "order_id": order_id, "age_min": round(age_min, 1)})
                log.info("Canceled stale buy %s (id=%s, age=%.1fm)", sym, order_id, age_min)
                # Clean up our `open_trades` record too — we wrote it speculatively
                # the moment we placed the order. If the order never filled, our
                # entry was never real. BUT a PARTIAL fill before the cancel means
                # the broker really holds shares: dropping the record then would
                # manufacture an orphan that reconcile re-adopts with fabricated
                # levels (the path behind the first 8 orphan trades). Keep the
                # record at the dealt qty instead.
                dealt = 0
                try:
                    dealt = int(float(row.get("dealt_qty") or 0))
                except (TypeError, ValueError):
                    pass
                with _TRADES_LOCK:
                    tracked = _load_open_trades()
                    tracked_trade = tracked.get(sym)
                    if tracked_trade and str(tracked_trade.get("buy_order_id")) == order_id:
                        if dealt > 0:
                            tracked_trade["qty"] = dealt
                            tracked_trade["qty_initial"] = dealt
                            _save_open_trades(tracked)
                            log.info("Stale buy %s partially filled (%d) — record kept at dealt qty",
                                     sym, dealt)
                        else:
                            tracked.pop(sym)
                            _save_open_trades(tracked)
                            log.info("Removed ghost open_trades entry for %s (order %s never filled)",
                                     sym, order_id)
    return canceled


def _check_bracket_fills(client: MooClient, symbol: str, trade: dict) -> dict | None:
    """OCO check: if either bracket leg filled, cancel the other and close the trade.

    Returns an action dict if a bracket leg fired (caller pops the trade), else None.
    Trades without bracket IDs (SIMULATE or REAL-fallback) return None — soft logic
    handles them downstream."""
    stop_id = trade.get("stop_order_id")
    tp_id = trade.get("tp_order_id")
    if not (stop_id and tp_id):
        return None

    # include_partial=True: a partially-filled leg counts as fired so we still
    # cancel the opposite leg (avoids both legs filling → oversell). reconcile()
    # re-syncs any residual qty on the next scan. (Bug fix 2026-06-03.)
    stop_filled = client.is_order_filled(stop_id, include_partial=True)
    tp_filled = client.is_order_filled(tp_id, include_partial=True)

    if stop_filled:
        if client.cancel_order(tp_id):
            log.info("OCO: %s STOP filled, cancelled TP %s", symbol, tp_id)
        else:
            log.warning("OCO: %s STOP filled but TP cancel failed (id=%s)", symbol, tp_id)
        pnl = _close_and_log(symbol, trade, trade["qty"], trade["stop_loss"], "SL_BRACKET")
        return {"type": "stop_hit_bracket", "symbol": symbol,
                "price": trade["stop_loss"], "qty": trade["qty"], "pnl": pnl}

    if tp_filled:
        if client.cancel_order(stop_id):
            log.info("OCO: %s TP filled, cancelled STOP %s", symbol, stop_id)
        else:
            log.warning("OCO: %s TP filled but STOP cancel failed (id=%s)", symbol, stop_id)
        pnl = _close_and_log(symbol, trade, trade["qty"], trade["take_profit"], "TP_BRACKET")
        return {"type": "tp_hit_bracket", "symbol": symbol,
                "price": trade["take_profit"], "qty": trade["qty"], "pnl": pnl}

    return None


def _last_price(client: MooClient, symbol: str) -> float | None:
    """Fetch a *validated* last price, or None if the symbol looks halted/anomalous.

    A halted, suspended, or limit-up/down stock can return last_price of 0, None,
    NaN, negative, or a non-numeric string from the broker snapshot. Feeding any
    of those into a SELL would place the order at ~$0 — a phantom total-loss fill —
    or crash the manage loop. Returning None tells every caller to SKIP the symbol
    this cycle and re-check next scan, instead of acting on a bad price.
    """
    try:
        snap = client.get_snapshot(symbol)
        last = float(snap["last_price"])
    except Exception as e:
        log.warning("snapshot unreadable for %s: %s — skipping this cycle", symbol, e)
        return None
    if not math.isfinite(last) or last <= 0:
        log.warning("%s last_price=%r looks halted/anomalous — skipping this cycle",
                    symbol, last)
        return None
    return last


def _is_stalled(trade: dict, last_price: float, atr_ref: float) -> bool:
    """A position is 'stalled' when after STALL_MIN_DAYS business days:
      • Its high-water-mark is below entry + 0.5×ATR (never made meaningful progress)
      • Its current price isn't deeply negative (just sideways, not yet SL'd)

    Stall-out frees capital that would otherwise burn the rest of MAX_HOLD_DAYS
    going nowhere. Set STALL_MIN_DAYS=3 — gives one weekend + a couple of
    sessions for a thesis to play out before pulling the plug.
    """
    # 2026-06-11 exit parity: OFF by default. The validated engine has no
    # stall-out, its MAX_HOLD bucket is net POSITIVE (+$833/140d), and the only
    # live stall-out closes on record were both losers (−$33.7). Re-enable via
    # STALL_OUT_ENABLED only after the engine models it and the dual-window
    # gate passes.
    if not _settings.stall_out_enabled:
        return False
    STALL_MIN_DAYS = 3
    STALL_HW_THRESHOLD_R = 0.3   # high-water < entry + 0.3×ATR = "barely moved"
    try:
        opened = datetime.fromisoformat(trade["opened_at"])
        age = _business_days_between(opened, datetime.utcnow())
    except (KeyError, ValueError):
        return False
    if age < STALL_MIN_DAYS:
        return False
    entry = float(trade["entry_price"])
    hw = float(trade.get("high_water") or entry)
    progress_atr = (hw - entry) / atr_ref if atr_ref > 0 else 0
    if progress_atr >= STALL_HW_THRESHOLD_R:
        return False  # made enough progress; let MAX_HOLD or TP decide
    return True


def _force_close(client: MooClient, symbol: str, trade: dict,
                 last: float, reason: str) -> tuple[float, dict]:
    """Cancel any bracket legs and market-sell the position. Returns (pnl, action).

    2026-07-07: a bracket leg that FAILS to cancel may still be live — or may
    already have filled — so selling on top of it risks a double sell. Same
    defer-to-next-cycle policy the max-hold path adopted (2026-07-02 P2):
    raise, let the per-symbol wrapper log it, retry next cycle."""
    for key in ("stop_order_id", "tp_order_id"):
        oid = trade.get(key)
        if oid and not client.cancel_order(oid):
            raise RuntimeError(
                f"{symbol}: cancel of bracket leg {key}={oid} failed — deferring "
                f"{reason} close to next cycle (leg may be live or already filled)")
    # The SELL must actually be placed before the close is booked (2026-07-02
    # audit P1-1). The old version swallowed a failed order and booked the close
    # anyway — the broker still held the shares, the books said "realized", and
    # the next reconcile re-adopted the live position as an orphan with
    # fabricated levels. Now a failure propagates to the caller (every call
    # site wraps per-symbol), the trade record stays tracked, and the exit is
    # retried next cycle. Note: any bracket legs were already cancelled above,
    # so until the retry succeeds the position is soft-tracked only.
    client.place_limit_order(symbol, trade["qty"],
                             last * (1 - PROTECTIVE_EXIT_SLIP), TrdSide.SELL)
    pnl = _close_and_log(symbol, trade, trade["qty"], last, reason)
    # An AI/sentinel risk-off close means "don't hold this today" — block the
    # scanner from buying it straight back this session (DELL churn, 2026-07-14).
    if reason in ("GAP_RISK", "SMART_EXIT"):
        _set_reentry_cooldown(symbol, reason)
    return pnl, {"type": reason.lower(), "symbol": symbol, "price": last,
                 "qty": trade["qty"], "pnl": pnl}


def _manage_one(client: MooClient, symbol: str, trade: dict,
                trades: dict, actions: list[dict], stops_only: bool = False) -> None:
    """Manage ONE open position for a single scan cycle.

    Mutates `trades` (pops on close) and `actions` (appends every action) in
    place. Factored out of manage_open_trades so the caller can wrap each symbol
    in try/except — that way one halted/anomalous name can never abort the
    management of the others (each `continue` here is just a `return`).

    stops_only=True (the fast-stop loop, src/main.py) runs ONLY the time-critical
    protective exits — breakeven ratchet + soft stop-loss for soft positions, and
    the broker bracket fill-check for REAL — then returns. Stall-out / max-hold /
    partials / take-profit are day-granularity and stay on the 5-min tick + scan."""
    # Owner-held manual position (a pending-review orphan, or a HIGH-risk manual
    # adoption whose takeover you haven't approved) — OFF-LIMITS to every auto-exit.
    if trade.get("user_managed"):
        return
    has_bracket = bool(trade.get("stop_order_id") and trade.get("tp_order_id"))

    # --- REAL bracket path: OCO check + stall-out + max-hold (broker owns SL/TP) ---
    if has_bracket:
        bracket_action = _check_bracket_fills(client, symbol, trade)
        if bracket_action is not None:
            actions.append(bracket_action)
            trades.pop(symbol)
            return
        # Fast-stop loop: the broker owns SL/TP, so the cheap fill-check above is
        # all the protective work needed — skip the housekeeping exits below.
        if stops_only:
            return
        # Bracket still alive. The bot owns two housekeeping exits on top of it:
        # stall-out (free idle capital) and the max-hold timeout.
        try:
            opened = datetime.fromisoformat(trade["opened_at"])
            age_days = _business_days_between(opened, datetime.utcnow())
        except (KeyError, ValueError):
            age_days = 0

        last = _last_price(client, symbol)
        if last is None:
            return   # halted — leave the bracket protecting it, retry next scan
        # Sample the high-water mark so the stall heuristic is meaningful for
        # bracket positions too (broker tracks fills, not our HW mark).
        trade["high_water"] = max(float(trade.get("high_water") or trade["entry_price"]), last)

        # Stall-out (2026-05-30): a bracket position that has gone nowhere for
        # STALL_MIN_DAYS gets culled — cancel the bracket legs then force-close —
        # so the slot/capital can chase a fresh signal instead of idling until
        # the 7-day max-hold guillotine.
        atr_ref = float(trade.get("atr") or 0) or max(
            (float(trade["entry_price"]) - float(trade["stop_loss"])) / 3.5, 0.01
        )
        if _is_stalled(trade, last, atr_ref):
            pnl, action = _force_close(client, symbol, trade, last, "STALL_OUT")
            actions.append(action)
            trades.pop(symbol)
            log.info("Stall-out close (bracket): %s @ $%.2f (pnl=%.2f)", symbol, last, pnl)
            return

        if age_days >= runtime_config.max_hold_days():
            # Pull bracket legs first, then market-sell remaining qty. A leg
            # that fails to cancel may still be live — or already FILLED —
            # so selling on top of it risks a double sell. Defer to the next
            # cycle: _check_bracket_fills will book a fill, or the cancel is
            # retried (2026-07-02 audit P2).
            for oid in (trade["stop_order_id"], trade["tp_order_id"]):
                if oid and not client.cancel_order(oid):
                    log.warning("%s max-hold: cancel of bracket leg %s failed — "
                                "deferring close to next cycle", symbol, oid)
                    return
            client.place_limit_order(symbol, trade["qty"], last * (1 - PROTECTIVE_EXIT_SLIP), TrdSide.SELL)
            pnl = _close_and_log(symbol, trade, trade["qty"], last, "MAX_HOLD")
            actions.append({"type": "max_hold_bracket", "symbol": symbol,
                            "price": last, "qty": trade["qty"],
                            "age_days": age_days, "pnl": pnl})
            trades.pop(symbol)
        return   # bracket path done — don't fall through to soft logic

    # --- SIMULATE / REAL-fallback (no bracket): soft-track via snapshot polling ---
    last = _last_price(client, symbol)
    if last is None:
        return   # halted/anomalous — don't soft-stop at $0; recheck next scan

    # Update water-marks for MFE/MAE on eventual close.
    trade["high_water"] = max(float(trade.get("high_water") or trade["entry_price"]), last)
    trade["low_water"] = min(float(trade.get("low_water") or trade["entry_price"]), last)

    # Breakeven ratchet (2026-06-11): once the trade has been +trigger_r×R in
    # profit, the stop may never sit below entry again. Mirrors the engine's
    # use_breakeven_stop; the trigger reads the high-water mark so a spike
    # between manage ticks still arms it (parity with the engine's intrabar
    # high check). Ratchet only — never lowers an already-higher stop.
    if _settings.use_breakeven_stop and not trade.get("breakeven_set"):
        be_entry = float(trade["entry_price"])
        be_risk = float(trade.get("init_risk_per_share") or 0.0)
        be_hw = float(trade.get("high_water") or be_entry)
        if be_risk > 0 and be_hw >= be_entry + runtime_config.breakeven_trigger_r() * be_risk:
            trade["breakeven_set"] = True
            if be_entry > float(trade["stop_loss"]):
                trade["stop_loss"] = round(be_entry, 2)
                actions.append({"type": "breakeven", "symbol": symbol,
                                "new_stop": trade["stop_loss"]})

    # Soft stop-loss check (SIMULATE, or REAL when bracket attach failed).
    if last <= trade["stop_loss"]:
        reason = ("BREAKEVEN" if trade.get("breakeven_set")
                  and float(trade["stop_loss"]) >= float(trade["entry_price"])
                  else "SL")
        # 2026-07-18: label by the price the close is actually booked at, not
        # the stop level. On a gap/fast tape `last` can sit far below the
        # raised-to-entry stop (07-15 HPE: "BREAKEVEN" at -1.36R), and the
        # label decides whether the SL re-entry cooldown fires. Anything worse
        # than -0.25R is a stop-out, whatever the stop was raised to.
        if reason == "BREAKEVEN":
            _risk = float(trade.get("init_risk_per_share") or 0.0)
            _entry = float(trade["entry_price"])
            _loss_r = ((last - _entry) / _risk) if _risk > 0 else 0.0
            if _loss_r <= -0.25 or (_risk <= 0 and last < _entry * 0.995):
                reason = "SL"
        client.place_limit_order(symbol, trade["qty"], last * (1 - PROTECTIVE_EXIT_SLIP), TrdSide.SELL)
        pnl = _close_and_log(symbol, trade, trade["qty"], last, reason)
        actions.append({"type": "stop_hit", "symbol": symbol, "price": last,
                        "qty": trade["qty"], "stop": trade["stop_loss"], "pnl": pnl})
        trades.pop(symbol)
        return

    # Fast-stop loop: protective checks (breakeven ratchet + soft stop) are done.
    # The exits below (stall-out / max-hold / partials / TP) are day-granularity
    # and stay on the 5-min manage tick + the scan.
    if stops_only:
        return

    # Stall-out: if position has gone nowhere for 3+ business days, close.
    # Frees capital for fresh signals instead of waiting the full MAX_HOLD.
    atr_ref = float(trade.get("atr") or 0) or max(
        (float(trade["entry_price"]) - float(trade["stop_loss"])) / 3.5, 0.01
    )
    if _is_stalled(trade, last, atr_ref):
        pnl, action = _force_close(client, symbol, trade, last, "STALL_OUT")
        actions.append(action)
        trades.pop(symbol)
        log.info("Stall-out close: %s @ $%.2f (pnl=%.2f)", symbol, last, pnl)
        return

    # Max-hold force close.
    opened = datetime.fromisoformat(trade["opened_at"])
    age_days = _business_days_between(opened, datetime.utcnow())
    if age_days >= runtime_config.max_hold_days():
        client.place_limit_order(symbol, trade["qty"], last * (1 - PROTECTIVE_EXIT_SLIP), TrdSide.SELL)
        pnl = _close_and_log(symbol, trade, trade["qty"], last, "MAX_HOLD")
        actions.append({"type": "max_hold", "symbol": symbol, "price": last,
                        "qty": trade["qty"], "age_days": age_days, "pnl": pnl})
        trades.pop(symbol)
        return

    # --- partial profit-taking ---
    if _settings.use_scale_out:
        # 3-tranche scale-out (USE_SCALE_OUT). Mirrors backtest_v3 exactly so the
        # SIMULATE PnL reproduces the cash-on backtest: bank 1/3 of the ORIGINAL
        # lot at +TP1_R, another 1/3 at +TP2_R (R = entry − initial stop), then let
        # the final ~1/3 RIDE the single take-profit / SL / max-hold. Deliberately
        # NO trailing on the runner: the cash-frontier sweep showed trailing the
        # runner in an uptrend HURTS (so3/6+trail ≈ $38/day vs so3/6 ≈ $55/day) —
        # the runner does best riding to the wide ATR TP.
        entry = float(trade["entry_price"])
        risk = float(trade.get("init_risk_per_share") or 0.0)
        if risk > 0:
            qty_init = int(trade.get("qty_initial") or trade["qty"])
            tranche = max(1, qty_init // 3)
            # TP1 — first 1/3
            if not trade.get("tp1_done") and last >= entry + _settings.tp1_r * risk \
                    and tranche < trade["qty"]:
                client.place_limit_order(symbol, tranche, last, TrdSide.SELL)
                pnl = _close_and_log(symbol, trade, tranche, last, "TP1")
                trade["qty"] -= tranche
                trade["tp1_done"] = True
                actions.append({"type": "scale_out", "tranche": 1, "symbol": symbol,
                                "price": last, "qty": tranche, "pnl": pnl})
            # TP2 — second 1/3 (may also fire this same tick if price is already
            # through both levels, matching the backtest's per-bar sequential check)
            if trade.get("tp1_done") and not trade.get("tp2_done") \
                    and last >= entry + _settings.tp2_r * risk and tranche < trade["qty"]:
                client.place_limit_order(symbol, tranche, last, TrdSide.SELL)
                pnl = _close_and_log(symbol, trade, tranche, last, "TP2")
                trade["qty"] -= tranche
                trade["tp2_done"] = True
                actions.append({"type": "scale_out", "tranche": 2, "symbol": symbol,
                                "price": last, "qty": tranche, "pnl": pnl})
        # runner exits WHOLE at the single take-profit (no trail) — same full-TP
        # close the backtest books for the remaining lot.
        if last >= trade["take_profit"] and trade["qty"] > 0:
            client.place_limit_order(symbol, trade["qty"], last, TrdSide.SELL)
            pnl = _close_and_log(symbol, trade, trade["qty"], last, "TP")
            actions.append({"type": "tp_full", "symbol": symbol, "price": last,
                            "qty": trade["qty"], "pnl": pnl})
            trades.pop(symbol)
            return
        return   # scale-out path done — skip the legacy half-close + trail below

    # Take-profit: FULL close at the TP touch — exit parity with the validated
    # honest engine (2026-06-11). The engine that produced every validated
    # $/day number books the entire position at take_profit; the legacy
    # TP_HALF + EMA20-trail path that used to live here was never measured
    # under that lens, and the TP bucket carries essentially all of the net
    # PnL (+$2,840/140d) — running an unvalidated exit on it was the largest
    # remaining live↔backtest divergence. (Positions opened before this change
    # with half_closed=True simply ride their remainder to this same full TP.)
    if last >= trade["take_profit"]:
        client.place_limit_order(symbol, trade["qty"], last, TrdSide.SELL)
        pnl = _close_and_log(symbol, trade, trade["qty"], last, "TP")
        actions.append({"type": "tp_full", "symbol": symbol, "price": last,
                        "qty": trade["qty"], "pnl": pnl})
        trades.pop(symbol)
        return


def manage_open_trades(client: MooClient) -> list[dict]:
    """Thread-safe wrapper — the scan job and the 5-min manage tick both call
    this from different scheduler threads; the lock keeps their load→mutate→
    save cycles on the open-trades store from clobbering each other."""
    with _TRADES_LOCK:
        return _manage_open_trades_locked(client)


def manage_stops_only(client: MooClient) -> list[dict]:
    """Lightweight protective-exit pass for the fast-stop loop (src/main.py).

    Checks ONLY the breakeven ratchet + soft stop-loss on soft-tracked positions
    and broker bracket fills on REAL positions — the time-critical, capital-
    protecting exits. Deliberately SKIPS stall-out / max-hold / partials / TP /
    blacklist / gap-sentinel / over-cap flush; those are day-granularity and stay
    on the 5-min manage tick + the scan. Shares _TRADES_LOCK with the full manage
    pass so the two never clobber the open-trades store.

    Motivation: SIMULATE has no native STOP order, so a soft stop otherwise waits
    for the 5-min tick (the live audit measured a ~−1.38R late-fill overshoot from
    that lag). Running this every FAST_STOP_SECONDS shrinks the overshoot to a few
    seconds of a bar. In REAL it doubles as a fast OCO-fill detector that frees the
    slot (and cancels the opposite leg) promptly."""
    with _TRADES_LOCK:
        trades = _load_open_trades()
        if not trades:
            return []
        actions: list[dict] = []
        for symbol, trade in list(trades.items()):
            try:
                _manage_one(client, symbol, trade, trades, actions, stops_only=True)
            except Exception as e:
                # Per-symbol isolation, same as the full pass: one halted/anomalous
                # name must never abort protective management of the rest.
                log.exception("fast-stop manage %s failed (skip this symbol): %s",
                              symbol, e)
        _save_open_trades(trades)
        return actions


def _manage_open_trades_locked(client: MooClient) -> list[dict]:
    """Per-scan housekeeping: stale order cancel → OCO bracket check → soft fallback.

    2026-05-30 additions to keep capital flowing:
      • Blacklist auto-close: any open position whose symbol is now on the
        adaptive blacklist gets force-closed (don't let dead names linger).
      • Stall-out: positions that go nowhere for STALL_MIN_DAYS are exited so
        their capital can chase fresh signals.
      • Over-capacity flush: when total holdings exceed MAX_POSITIONS, close
        the worst-performing one — happens after a watchlist/.env tightening
        that left old positions over-allocated.
    """
    actions: list[dict] = []
    actions.extend(cancel_stale_orders(client))

    # --- Auto-flush 0: positions whose symbol is now blacklisted ---
    try:
        from . import blacklist as _bl
        blacklisted = set(_bl.get_blacklist().keys())
    except Exception as e:
        log.debug("blacklist load failed: %s", e)
        blacklisted = set()

    trades = _load_open_trades()
    # Owner-held manual positions are off-limits to EVERY auto-exit below
    # (blacklist / gap-sentinel / over-cap flush / per-symbol manage).
    _skip = {s for s, t in trades.items() if t.get("user_managed")}
    for symbol in list(trades.keys()):
        if symbol in _skip:
            continue
        if symbol in blacklisted:
            last = _last_price(client, symbol)
            if last is None:
                continue   # halted/anomalous — re-check next scan, don't sell at $0
            try:
                pnl, action = _force_close(client, symbol, trades[symbol],
                                           last, "BLACKLIST")
                actions.append(action)
                trades.pop(symbol)
                log.warning("Blacklist auto-close: %s @ $%.2f (pnl=%.2f)",
                            symbol, last, pnl)
                # Feedback 铁律: force-closing a real position must be visible.
                try:
                    from . import notifier
                    notifier.send(f"⛔ 黑名单平仓: {symbol} @ ${last:.2f} "
                                  f"(已实现 ${pnl:+.0f}) — 该票已进黑名单")
                except Exception:
                    pass
            except Exception as e:
                log.warning("blacklist auto-close %s failed: %s", symbol, e)

    # --- Auto-flush 0.5: gap-risk sentinel (earnings + AI, pre-close exit) ---
    # Exit a held name DURING regular hours when it carries a known overnight
    # gap-down catalyst (earnings imminent, or fresh public bad news). A stop
    # can't catch a gap, so this acts before it. AI decision overrides the
    # strategy's hold; FAIL-SAFE (holds on any doubt). Every exit notifies.
    from .config import settings as _settings
    if _settings.gap_sentinel_enabled:
        from . import gap_sentinel
        for symbol in list(trades.keys()):
            if symbol in _skip:
                continue
            try:
                # RTH per-scan: earnings layer always; AI only if intraday AI is on
                # (default off → no per-scan Gemini cost; pre-market job does AI).
                should_exit, reason = gap_sentinel.assess(
                    symbol, use_ai=_settings.gap_sentinel_ai_intraday)
            except Exception as e:
                log.warning("gap-sentinel assess %s failed: %s — holding", symbol, e)
                continue
            if not should_exit:
                continue
            last = _last_price(client, symbol)
            if last is None:
                continue   # halted/anomalous — don't sell at $0, re-check next scan
            try:
                pnl, action = _force_close(client, symbol, trades[symbol],
                                           last, "GAP_RISK")
                actions.append(action)
                trades.pop(symbol)
                log.warning("Gap-sentinel close: %s @ $%.2f (pnl=%.2f) — %s",
                            symbol, last, pnl, reason)
                try:
                    from . import notifier
                    notifier.send(f"⚠️ 跳空哨兵平仓: {symbol} @ ${last:.2f} "
                                  f"(已实现 ${pnl:+.0f}) — {reason}")
                except Exception:
                    pass
            except Exception as e:
                log.warning("gap-sentinel close %s failed: %s", symbol, e)

    # --- Auto-flush 0.6: smart exit (AI bearish-catalyst / algo lock-profit) ---
    # Broader than the gap sentinel: exit a held long DURING the day when the
    # picture turns bearish — concrete bad news (AI) or a technical break-down
    # while in profit (algo lock-profit). Reuses the same _force_close + notify.
    # DEFAULT OFF; FAIL-SAFE (smart_exit.assess holds on any doubt/error).
    if _settings.smart_exit_enabled:
        from . import smart_exit
        for symbol in list(trades.keys()):
            if symbol in _skip:
                continue
            last = _last_price(client, symbol)
            if last is None:
                continue   # halted/anomalous — don't sell at $0, re-check next scan
            try:
                should_exit, reason, _conf = smart_exit.assess(
                    symbol, trades[symbol], last, client)
            except Exception as e:
                log.warning("smart-exit assess %s failed: %s — holding", symbol, e)
                continue
            if not should_exit:
                continue
            try:
                pnl, action = _force_close(client, symbol, trades[symbol],
                                           last, "SMART_EXIT")
                actions.append(action)
                trades.pop(symbol)
                log.warning("Smart-exit close: %s @ $%.2f (pnl=%.2f) — %s",
                            symbol, last, pnl, reason)
                try:
                    from . import notifier
                    notifier.send(f"🤖 智能退出: {symbol} @ ${last:.2f} "
                                  f"(已实现 ${pnl:+.0f}) — {reason}")
                except Exception:
                    pass
            except Exception as e:
                log.warning("smart-exit close %s failed: %s", symbol, e)

    # --- Auto-flush 1: over-capacity flush ---
    # If we hold MORE than MAX_POSITIONS (e.g. user just tightened the cap),
    # close the worst-performing one (most negative unrealized R) to free a slot.
    # Over-cap counts only BOT-managed names; owner-held manual positions don't
    # consume a bot slot and can't be flushed.
    managed = {s: t for s, t in trades.items() if s not in _skip}
    cap = risk_manager.max_positions()
    if len(managed) > cap:
        excess = len(managed) - cap
        per_symbol_r: list[tuple[str, float]] = []
        for symbol, trade in managed.items():
            last = _last_price(client, symbol)
            if last is None:
                continue   # halted name can't be ranked/flushed this cycle
            try:
                entry = float(trade["entry_price"])
                stop = float(trade["stop_loss"])
                r_unit = entry - stop
                r_now = (last - entry) / r_unit if r_unit > 0 else 0
                per_symbol_r.append((symbol, r_now))
            except Exception:
                continue
        # Close the `excess` worst names.
        for symbol, r_now in sorted(per_symbol_r, key=lambda x: x[1])[:excess]:
            last = _last_price(client, symbol)
            if last is None:
                continue
            try:
                pnl, action = _force_close(client, symbol, trades[symbol],
                                           last, "OVER_CAP")
                actions.append(action)
                trades.pop(symbol)
                log.warning("Over-cap flush: %s (R=%.2f) @ $%.2f (pnl=%.2f)",
                            symbol, r_now, last, pnl)
            except Exception as e:
                log.warning("over-cap flush %s failed: %s", symbol, e)

    for symbol, trade in list(trades.items()):
        try:
            _manage_one(client, symbol, trade, trades, actions)
        except Exception as e:
            # Per-symbol isolation (2026-05-30): one halted/anomalous name must
            # never abort management of the rest. Log + move on; it gets retried
            # next scan, and any REAL bracket keeps protecting it meanwhile.
            log.exception("manage %s failed (skipping this symbol this cycle): %s",
                          symbol, e)

    _save_open_trades(trades)
    return actions


def close_position(client: MooClient, symbol: str, reason: str = "MANUAL") -> dict:
    """Cancel any bracket legs, then market-sell the tracked position at a FRESH
    real-time price (refuses if halted/anomalous, leaving brackets intact). The
    `reason` labels the trade log + returned action. Used by the gap-sentinel
    at-open exit so the fill happens against real liquidity, not a stale price."""
    trades = _load_open_trades()
    if symbol not in trades:
        raise RuntimeError(f"no tracked position for {symbol}")
    trade = trades[symbol]
    last = _last_price(client, symbol)
    if last is None:
        raise RuntimeError(f"{symbol} price looks halted/anomalous — refusing to "
                           f"market-sell at ~$0; brackets left intact, retry next cycle.")
    for key in ("stop_order_id", "tp_order_id"):
        oid = trade.get(key)
        if oid:
            ok = client.cancel_order(oid)
            log.info("close_position(%s): cancel %s leg %s → %s", reason, key, oid, ok)
    client.place_limit_order(symbol, trade["qty"], last * (1 - PROTECTIVE_EXIT_SLIP), TrdSide.SELL)
    pnl = _close_and_log(symbol, trade, trade["qty"], last, reason)
    trades.pop(symbol)
    _save_open_trades(trades)
    if reason in ("GAP_RISK", "SMART_EXIT"):
        _set_reentry_cooldown(symbol, reason)
    return {"type": reason.lower(), "symbol": symbol, "qty": trade["qty"],
            "price": last, "pnl": pnl}


def manual_close(client: MooClient, symbol: str) -> dict:
    """GUI helper — cancel any bracket legs, then market-sell the position."""
    trades = _load_open_trades()
    if symbol not in trades:
        raise RuntimeError(f"no tracked position for {symbol}")
    trade = trades[symbol]

    # Validate price BEFORE touching the brackets — if the symbol is halted we
    # refuse the close and leave any broker stop/TP in place to keep protecting it.
    last = _last_price(client, symbol)
    if last is None:
        raise RuntimeError(f"{symbol} price looks halted/anomalous — refusing to "
                           f"market-sell at ~$0. Bracket legs left intact; "
                           f"retry once the symbol resumes trading.")

    # Cancel any live bracket legs so we don't oversell.
    for key in ("stop_order_id", "tp_order_id"):
        oid = trade.get(key)
        if oid:
            ok = client.cancel_order(oid)
            log.info("manual_close: cancel %s leg %s → %s", key, oid, ok)

    client.place_limit_order(symbol, trade["qty"], last * (1 - PROTECTIVE_EXIT_SLIP), TrdSide.SELL)
    pnl = _close_and_log(symbol, trade, trade["qty"], last, "MANUAL")
    trades.pop(symbol)
    _save_open_trades(trades)
    return {"type": "manual_close", "symbol": symbol, "qty": trade["qty"],
            "price": last, "pnl": pnl}


def edit_stop(client: MooClient | None, symbol: str, new_stop: float) -> dict:
    """GUI helper — adjust stop loss. If a broker stop is active, re-place it."""
    trades = _load_open_trades()
    if symbol not in trades:
        raise RuntimeError(f"no tracked position for {symbol}")
    trade = trades[symbol]
    old = trade["stop_loss"]
    new_stop = round(float(new_stop), 2)

    new_stop_id = trade.get("stop_order_id")
    if new_stop_id and client is not None:
        try:
            client.cancel_order(new_stop_id)
            new_stop_id = client.place_stop_loss(symbol, trade["qty"], new_stop)
            log.info("edit_stop: %s broker stop re-placed @ $%.2f (id=%s)",
                     symbol, new_stop, new_stop_id)
        except Exception as e:
            log.error("edit_stop: re-place failed for %s: %s — JSON updated, broker stop stale!", symbol, e)
            notifier_msg = f"⚠ {symbol} stop edit: broker re-place FAILED, manual fix needed"
            try:
                from . import notifier
                notifier.send(notifier_msg)
            except Exception:
                pass

    trade["stop_loss"] = new_stop
    trade["stop_order_id"] = new_stop_id
    _save_open_trades(trades)
    return {"type": "edit_stop", "symbol": symbol, "old": old, "new": new_stop}
