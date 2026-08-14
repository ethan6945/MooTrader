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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytz

_ET = pytz.timezone("America/New_York")

from moomoo import TrdSide

from .config import settings as _settings  # noqa: F401 (used in caller too)
from . import db, news_driven, portfolio, risk_manager, runtime_config

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


def _extension_verdict(signal, last: float) -> tuple[bool, str, str]:
    """Scale-invariant replacement for the fixed ENTRY_CHASE_TOL gate.

    Returns (allow, gate, reason). See settings.entry_extension_mode for the
    why: 20 bps means something different on a $50 name than on a $1,400 one,
    while ATR-relative extension means the same thing on both.

    Two questions, both asked at the LIVE price:
      1. extension = (last - signal.price) / ATR. Past
         settings.entry_max_extension_atr the setup that produced the score is
         no longer the setup standing in front of us.
      2. reward/risk at the live price, with reward measured to the target the
         SETUP implied at signal time. That target does not move, so the reward
         shrinks as price runs — which is exactly the economics of being late.
         (Measuring to a live-anchored ATR target instead would make R:R the
         constant tp_mult/sl_mult and this leg could never fire.)

    With today's wide tp_atr_mult the extension leg is the one that usually
    decides; the R:R leg bites when the target sits close to the live price —
    a tight tp_atr_mult, or a setup price already near its objective.
    """
    # NaN must be caught explicitly: `nan <= 0` is False, so a plain positivity
    # check lets a NaN ATR through and then every comparison below silently
    # evaluates False — i.e. the gate would ALLOW on exactly the malformed-kline
    # input it exists to catch. (math.isfinite covers nan and both infinities.)
    try:
        atr = float(getattr(signal, "atr", 0) or 0)
    except (TypeError, ValueError):
        atr = 0.0
    if not math.isfinite(atr) or atr <= 0:
        return False, "bad_levels", f"ATR unusable ({signal.atr!r}) — cannot judge extension"
    try:
        px = float(signal.price)
    except (TypeError, ValueError):
        px = float("nan")
    if not math.isfinite(px) or px <= 0 or not math.isfinite(last):
        return False, "bad_levels", f"price unusable (signal {signal.price!r}, live {last!r})"
    extension = (last - px) / atr
    if extension > settings.entry_max_extension_atr:
        return False, "extended", (
            f"live ${last:.2f} is {extension:.2f} ATR past signal ${signal.price:.2f} "
            f"(max {settings.entry_max_extension_atr:.2f} ATR)")

    # Reward is measured to the target the SETUP implied at signal time, which
    # does not move, so it shrinks as price runs — that is the whole economics
    # of being late. Measuring to a live-anchored target instead would make R:R
    # the constant tp_mult/sl_mult (1.75 today) and this leg could never fire.
    # Risk uses the same stop rule the entry path applies, so the number here is
    # the trade's real R.
    struct = getattr(signal, "structural_stop", None)
    stop = round(last - runtime_config.sl_atr_mult() * atr, 2)
    if struct is not None:
        try:
            s = float(struct)
            if math.isfinite(s) and s < last:
                stop = max(stop, round(s, 2))
        except (TypeError, ValueError):
            pass
    try:
        target = float(signal.take_profit)
    except (TypeError, ValueError, AttributeError):
        target = float("nan")
    if not math.isfinite(target):
        target = round(px + runtime_config.tp_atr_mult() * atr, 2)
    risk = last - stop
    if risk <= 0:
        return False, "bad_levels", f"stop ${stop:.2f} not below live ${last:.2f}"
    if target <= last:
        return False, "rr", (
            f"live ${last:.2f} already at/past the setup's target ${target:.2f} — "
            "no reward left")
    rr = (target - last) / risk
    if rr < settings.entry_min_rr:
        return False, "rr", (
            f"R:R {rr:.2f} at live ${last:.2f} (stop ${stop:.2f}, setup target "
            f"${target:.2f}) below min {settings.entry_min_rr:.2f}")
    return True, "", f"extension {extension:.2f} ATR, R:R {rr:.2f} at ${last:.2f}"

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
        stop_id = client.place_stop_loss(symbol, qty, stop_price,
                                         intent='bracket stop').broker_order_id
        log.info("Bracket STOP attached %s qty=%d @ $%.2f (id=%s)",
                 symbol, qty, stop_price, stop_id)
    except Exception as e:
        log.error("Bracket STOP failed for %s: %s", symbol, e)
    try:
        tp_id = client.place_limit_order(symbol, qty, tp_price, TrdSide.SELL,
                                        kind='TP',
                                        intent='bracket take-profit').broker_order_id
        log.info("Bracket TP attached %s qty=%d @ $%.2f (id=%s)",
                 symbol, qty, tp_price, tp_id)
    except Exception as e:
        log.error("Bracket TP failed for %s: %s", symbol, e)
    return stop_id, tp_id

log = logging.getLogger(__name__)



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
    # Through db so it carries the account stamp — there is one file for both
    # accounts, and an unstamped one cannot be told apart from the other's.
    db.mirror_open_trades_json(trades)


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
    # risk_manager.can_open already rejects qty=0, but it and main.py each call
    # calc_position_size independently (adaptive/DD multipliers are read live),
    # so a disagreement is possible. Placing a 0-qty order just raises at the
    # broker — refuse here instead. 2026-07-27: calc_position_size returns 0 in
    # more cases now (un-sizeable name, VIX cut that can't be expressed in whole
    # shares), which makes this path reachable in normal operation.
    if qty <= 0:
        _LAST_ENTRY_SKIP = ("risk", f"computed qty={qty} — nothing to order")
        log.info("%s: qty=%d at order time — entry skipped", signal.symbol, qty)
        return None

    cooldown = reentry_cooldown_reason(signal.symbol)
    if cooldown:
        _LAST_ENTRY_SKIP = ("cooldown",
                            f"re-entry blocked — {cooldown} close earlier this session")
        log.info("%s: re-entry blocked — %s close earlier this session "
                 "(cooldown until next NY trading day)", signal.symbol, cooldown)
        return None

    # NET-SHORT GUARD (2026-07-29). Buying a symbol the account is short does
    # not open a long — it covers the short. The position we'd then track would
    # not exist at the broker, reconcile would read that as a vanished holding,
    # and the P&L would be booked against a trade that never happened (DDOG,
    # 2026-07-28). The bot is long-only and does not unwind shorts on its own,
    # so the only safe move is to leave the symbol alone until the owner clears
    # it. Fail-open: a broker hiccup must not silently stop all trading.
    try:
        if signal.symbol in client.get_short_symbols():
            _LAST_ENTRY_SKIP = ("net_short",
                                "account is net SHORT this symbol — a buy would "
                                "cover the short, not open a long")
            log.warning("%s: entry blocked — account is NET SHORT this symbol; "
                        "a buy would net against it instead of opening a long. "
                        "Clear the short first.", signal.symbol)
            return None
    except Exception as e:
        log.warning("%s: net-short check failed (%s) — proceeding", signal.symbol, e)

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
        # 2026-08-05: the fixed-bps chase gate and its scale-invariant
        # replacement. Only ONE of them decides; "shadow" (the default) runs the
        # new one purely to log what it would have done, so it accumulates
        # evidence without touching behaviour. This gate has no backtest
        # counterpart — the engine fills at the next bar open and never sees a
        # live quote — so shadow logging is the only honest way to evaluate it.
        mode = settings.entry_extension_mode
        legacy_ran_past = last > signal.price * (1 + ENTRY_CHASE_TOL)
        if mode in ("shadow", "atr"):
            allow_new, new_gate, new_reason = _extension_verdict(signal, last)
            if mode == "shadow" and allow_new != (not legacy_ran_past):
                log.info("%s: [extension shadow] new gate would %s (%s); legacy %s",
                         signal.symbol, "ALLOW" if allow_new else f"SKIP[{new_gate}]",
                         new_reason, "skipped" if legacy_ran_past else "allowed")
            if mode == "atr" and not allow_new:
                _LAST_ENTRY_SKIP = (new_gate, new_reason)
                log.info("%s: entry skipped [%s] — %s", signal.symbol, new_gate, new_reason)
                return None
        if mode != "atr" and legacy_ran_past:
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
        # Marketable limit at the live quote. In legacy/shadow mode it is also
        # capped at signal+tol — safe there, because the gate above already
        # guaranteed last <= that cap. In "atr" mode the whole point is that we
        # may be entering ABOVE the stale signal price, so keeping the cap would
        # post an unfillable limit under the market; the extension gate is what
        # bounds how far above we are willing to pay.
        if settings.entry_extension_mode == "atr":
            limit_px = round(last * 1.001, 2)
        else:
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

    placement = client.place_limit_order(
        signal.symbol, qty, limit_px, TrdSide.BUY,
        kind="STACK" if is_stack else "ENTRY",
        intent=f"score {getattr(signal, 'score', '?')}",
    )
    buy_order_id = placement.broker_order_id

    # Wait for the broker to say what actually happened, and use THAT.
    #
    # This block replaces "write the position at the requested quantity and the
    # limit price, immediately". Both halves of that were fiction:
    #
    #   qty         — a limit order that filled 40 of 100 produced a 100-share
    #                 record, so the stop and take-profit covered 60 shares
    #                 nobody owned, and the exit for them would have opened a
    #                 short. Every naked short in this account began that way.
    #   entry_price — the limit is what we ASKED for. Booking it as the entry
    #                 puts a price the broker never gave us into the R-multiple,
    #                 which feeds half-Kelly, the optimizer and adaptive sizing.
    #
    # The wait is bounded. A timeout is not a failure: it means the order is
    # still working, and the position is whatever has filled SO FAR. The
    # remainder is not invented and not deferred to the next reconcile — it
    # simply is not a position yet, and the order stays live in the log.
    final = client.await_fill(placement.client_order_id,
                              timeout=_ENTRY_FILL_WAIT_SEC)
    filled = int(final.get("filled_qty") or 0)
    fill_px = float(final.get("avg_fill_price") or 0) or None

    if filled <= 0:
        # Nothing was bought. Recording a position here is exactly what left a
        # phantom HPE 64 in the books on 2026-08-11 — a holding that existed in
        # one database and nowhere else, which reconcile later "closed" at a
        # price for a sale that never happened.
        _LAST_ENTRY_SKIP = ("no_fill",
                            f"order {final.get('state')} with nothing filled")
        log.warning("%s: no position — order %s is %s with 0/%d filled. The "
                    "order remains tracked; no holding is recorded for shares "
                    "that were not bought.",
                    signal.symbol, placement.client_order_id,
                    final.get("state"), qty)
        return None

    if filled < qty:
        log.warning("%s: partial entry %d/%d @ $%.4f — the position is the %d "
                    "shares that filled. Protective levels are sized to those.",
                    signal.symbol, filled, qty, fill_px or limit_px, filled)

    # From here on `qty` means what was bought, and `entry_px` what it cost.
    qty = filled
    entry_px = fill_px or limit_px

    if is_stack:
        old_qty = int(existing["qty"])
        old_entry = float(existing["entry_price"])
        new_total_qty = old_qty + qty
        # entry_px, not limit_px: the average cost of a position is the average
        # of what was paid. Using the limit here made every stack drift the
        # recorded basis toward a price the broker never charged.
        new_avg_entry = (old_qty * old_entry + qty * entry_px) / new_total_qty
        # Stop/TP trail UP only — never weaken protection on the original lot.
        new_stop = max(float(existing.get("stop_loss", 0)), stop_px)
        new_tp = max(float(existing.get("take_profit", 0)), tp_px)
        stacks = int(existing.get("stacks", 1)) + 1

        # On REAL, replace the OCO bracket so it covers the new combined qty
        # (only when broker brackets are in use — soft-exit mode skips this).
        stop_order_id = existing.get("stop_order_id")
        tp_order_id = existing.get("tp_order_id")
        if settings.moo_trade_env == "REAL" and not settings.real_use_soft_exits:
            # Both old legs must be gone before new ones go on. This used to
            # catch only exceptions — so a clean `False` from the broker, an
            # explicit refusal, fell straight through — and then placed the
            # replacement bracket regardless. The result is two stop orders
            # covering overlapping quantity: when price reaches them, both sell,
            # and the second sale is stock we no longer hold.
            cancels_ok = all(
                cancel_protective(client, signal.symbol, oid, leg)
                for oid, leg in ((stop_order_id, "stop"), (tp_order_id, "take-profit"))
            )
            if not cancels_ok:
                raise RuntimeError(
                    f"{signal.symbol}: cannot re-bracket a stack while an old "
                    f"protective leg is still live at the broker. Trading is "
                    f"halted; the added shares were bought and are recorded, "
                    f"and the existing bracket still covers the original lot.")
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
            "last_stack_price": entry_px,
        })
        # Refresh water-marks to current price for the new combined lot.
        trade["high_water"] = max(float(trade.get("high_water") or entry_px), entry_px)
        log.info("STACK #%d on %s: +%d @ $%.2f → total %d, avg $%.2f, stop $%.2f, tp $%.2f",
                 stacks, signal.symbol, qty, entry_px,
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
            "entry_price": entry_px,
            "stop_loss": stop_px,
            "take_profit": tp_px,
            "atr": signal.atr,
            "half_closed": False,
            # Scale-out state (used only when USE_SCALE_OUT=true). qty_initial
            # anchors the 1/3 tranche size to the ORIGINAL lot; init_risk_per_share
            # is the R unit (entry − initial stop) for the +TP1_R / +TP2_R levels.
            "qty_initial": qty,
            "init_risk_per_share": max(float(entry_px) - float(stop_px), 0.0),
            "tp1_done": False,
            "tp2_done": False,
            "buy_order_id": buy_order_id,
            "stop_order_id": stop_order_id,
            "tp_order_id": tp_order_id,
            "opened_at": datetime.utcnow().isoformat(),
            # Water-marks start at the entry price — updated each manage tick.
            "high_water": entry_px,
            "low_water": entry_px,
            "strategy": getattr(signal, "strategy", "trend"),
            # Pattern strategy: remember which chart pattern triggered the entry
            # so the dashboard/GUI can badge it (None for the other strategies).
            "pattern": (getattr(signal, "meta", {}) or {}).get("pattern_type"),
            "stacks": 1,
        }

    trades[signal.symbol] = trade
    # The position and the applied mark commit TOGETHER. Writing the position
    # and then marking the fill in a second transaction leaves a window where a
    # crash lets the sweep add the same shares again — small, one-sided, and
    # avoidable, which is the only reason to have it at all.
    from . import fill_settler
    fill_settler.open_position(placement.client_order_id, trade, filled, entry_px)
    # The JSON mirror is a copy; it follows the write rather than carrying it.
    _save_open_trades(trades)
    return trade


# How long to wait for a just-placed exit order to report its fill before
# booking at the pre-order quote instead. Short on purpose: the ORDER is the
# protection and it is already live by the time we poll — this only decides
# which price lands in the books. Falling back is safe (reconcile re-syncs).
# How long an entry waits for its fill before the position is written from
# whatever has filled so far. Longer than the exit poll below: an exit is
# already protective the moment it is live, whereas an entry that is recorded
# before it fills creates a holding that does not exist.
_ENTRY_FILL_WAIT_SEC = 20.0

# An exit waits less. The ORDER is the protection and it is live the moment it
# is accepted, so this only decides how long we wait before recording what has
# happened so far — and an unrecorded partial is corrected on the next tick,
# whereas an unplaced exit is unprotected.
_EXIT_FILL_WAIT_SEC = 8.0

_FILL_POLL_ATTEMPTS = 3
_FILL_POLL_SLEEP_SEC = 0.4


def _assert_still_held(client: MooClient, symbol: str, qty: int, reason: str) -> None:
    """Refuse to place an exit SELL for shares the broker no longer holds.

    THE duplicate-sell guard. Every naked short in the account traces to one
    exit executing twice: the sell reached the broker, then something after it
    raised (a place_order response that timed out with the order live, a
    booking write that failed), the caller's `except` left the trade record in
    place, and the next cycle sold the same shares again — into a short.
    Fingerprint: paired fills one scan interval apart (XLF, MSFT, SWKS, INTC,
    MCHP, HPQ, DDOG, GOOGL — 8 for 8, May-June 2026), which is why the account
    still carries them.

    Every retry-after-partial-failure path is protected by asking the ONE
    authority on what we hold. Fails OPEN when the broker can't be reached: a
    protective exit must not be blocked by a flaky query, and absence of an
    answer is not evidence that we're flat. It fails CLOSED only on a positive
    answer of "you don't hold that" — which is exactly the duplicate case.
    """
    try:
        positions = client.get_positions()
    except Exception as e:
        log.warning("%s %s: holdings check failed (%s) — placing the exit anyway",
                    symbol, reason, e)
        return
    from .reconcile import net_positions
    held = net_positions(positions).get(symbol, 0.0)
    if held >= qty:
        return
    raise RuntimeError(
        f"{symbol}: refusing {reason} sell of {qty} — broker shows {held:+.0f} "
        f"held. The exit already went through (or the position was closed "
        f"elsewhere); selling again would open a SHORT. Dropping the stale "
        f"record is reconcile's job.")


@dataclass(frozen=True)
class ExitFill:
    """What an exit actually achieved: a price, and how many shares moved.

    Returning only the price — as this did — made every caller book the
    quantity it had asked to sell, which on a partial fill is a close for
    shares that are still held.
    """
    price: float
    filled: int
    client_order_id: str | None = None


def _sell_and_book_price(client: MooClient, symbol: str, qty: int,
                         limit_px: float, quote_px: float,
                         reason: str) -> "ExitFill":
    """Place an exit SELL and report the fill price AND the quantity sold.

    Returns the broker's actual dealt_avg_price when it can be read within
    ~1.2s, else `quote_px` (the pre-order quote) so booking never blocks on the
    broker. 2026-07-27: every exit used to book `quote_px` unconditionally while
    submitting a limit up to PROTECTIVE_EXIT_SLIP below it, so real slippage
    never reached trades.jsonl — and that file is the input to half-Kelly, the
    AI optimizer, the blacklist and adaptive sizing. Raises only if the order
    itself fails to place (callers already handle that).
    """
    _assert_still_held(client, symbol, qty, reason)
    placement = client.place_limit_order(symbol, qty, limit_px, TrdSide.SELL,
                                         kind='EXIT', intent=reason)
    order_id = placement.broker_order_id
    try:
        final = client.await_fill(placement.client_order_id,
                                  timeout=_EXIT_FILL_WAIT_SEC)
        got = int(final.get("filled_qty") or 0)
        px = float(final.get("avg_fill_price") or 0) or None
        if got > 0 and px:
            if got < qty:
                log.warning("%s %s: partial exit %d/%d @ $%.2f — booking %d "
                            "sold; %d shares remain held and stay tracked",
                            symbol, reason, got, qty, px, got, qty - got)
            slip_bps = (px - quote_px) / quote_px * 1e4 if quote_px else 0.0
            log.info("%s %s: booked at actual fill $%.2f (quote was $%.2f, "
                     "%+.1f bps)", symbol, reason, px, quote_px, slip_bps)
            return ExitFill(price=px, filled=got,
                            client_order_id=placement.client_order_id)
        log.info("%s %s: nothing filled yet on order %s — booking nothing. The "
                 "order stays live; the position stays held.",
                 symbol, reason, order_id)
        return ExitFill(price=quote_px, filled=0,
                        client_order_id=placement.client_order_id)
    except Exception as e:
        # A lookup failure is not evidence about the sale. Reporting zero filled
        # keeps the position held, which is the safe direction: the alternative
        # is booking a close for shares that may still be ours, and the next
        # exit for them would be a sell we do not hold — a short.
        log.warning("%s %s: fill lookup failed (%s) — treating as unsold; the "
                    "order remains tracked", symbol, reason, e)
        return ExitFill(price=quote_px, filled=0)


def cancel_protective(client: MooClient, symbol: str, order_id: str,
                      leg: str) -> bool:
    """Cancel a protective leg. A refusal halts trading. Returns True if accepted.

    A stop or take-profit that would not cancel is still working at the broker.
    Everything after that point is reasoning about a position whose protection
    this software can no longer account for: place a replacement and there are
    two stops for overlapping quantity; sell the shares and the surviving leg
    becomes a naked short the moment it triggers.

    There is no local recovery from that. The order belongs to the broker, and
    only a person looking at the account can decide what to do about it. So
    trading stops, the residual is recorded with the symbol and leg that owns
    it, and the position is deliberately NOT removed — a holding whose
    protection is in doubt must stay visible.

    Note the asymmetry with success: True means the CANCEL REQUEST was
    accepted, not that the order is gone. That is why callers must still poll
    rather than assume the leg is dead.
    """
    if not order_id:
        return True
    try:
        accepted = client.cancel_order(order_id)
    except Exception as e:
        accepted = False
        detail = f"{type(e).__name__}: {e}"
    else:
        detail = "" if accepted else "the broker refused the cancellation"

    from . import order_log
    if accepted:
        # Accepted is not gone. Poll until the broker says what actually became
        # of it — an order can fill in the moment between the decision to cancel
        # and the request landing, and a caller that places a replacement on the
        # strength of "accepted" ends up with two live orders for one position.
        row = order_log.by_broker_id(order_id) if order_id else None
        if row is not None:
            settled = _await_cancel_terminal(client, row["client_order_id"])
            if settled is None:
                risk_manager.halt(
                    "cancel not confirmed",
                    f"{symbol} {leg} order {order_id}: the cancel was accepted "
                    f"but the broker never confirmed what became of it. It may "
                    f"still be working; no replacement order will be placed.")
                return False
            if int(settled.get("filled_qty") or 0) > int(
                    settled.get("applied_qty") or 0):
                # It filled while we were cancelling. Apply that before anything
                # else decides what the position is.
                try:
                    from . import fill_settler
                    fill_settler.settle(settled["client_order_id"])
                except Exception as e:
                    log.error("%s: could not settle a leg that filled during "
                              "cancellation: %s", symbol, e)
        return True

    residual = {"symbol": symbol, "leg": leg, "broker_order_id": str(order_id),
                "at": datetime.utcnow().isoformat(), "detail": detail[:300]}
    try:
        db.update_state({"residual_orders": [
            *(db.get_state().get("residual_orders") or []), residual]})
    except Exception as e:
        log.error("could not record the residual order %s: %s", order_id, e)
    try:
        risk_manager.halt(
            "protective order cancel failed",
            f"{symbol} {leg} order {order_id} is still live at the broker and "
            f"could not be cancelled ({detail}). The position is left held and "
            f"protected by whatever that order does; resolve it by hand.")
    except Exception as e:
        log.error("could not halt after a failed protective cancel: %s", e)
    return False


_CANCEL_CONFIRM_SEC = 10.0


def _await_cancel_terminal(client: MooClient, coid: str,
                           timeout: float = _CANCEL_CONFIRM_SEC) -> dict | None:
    """Poll a cancelled order until the broker says what became of it.

    Returns the settled row, or None if the broker never said. None is not
    "cancelled" — it is "unknown", and the caller treats it as a refusal.
    """
    from . import order_log
    deadline = time.time() + timeout
    while True:
        row = order_log.get(coid)
        if row and row["state"] in order_log.TERMINAL_STATES:
            return row
        try:
            oid = (row or {}).get("broker_order_id")
            fill = client.get_order_fill(oid) if oid else None
            if fill:
                order_log.record_fill(
                    coid, filled_qty=int(fill["qty"]),
                    avg_price=float(fill["price"]),
                    state=order_log.map_broker_status(fill.get("status")))
            elif oid and not client.is_order_filled(oid, include_partial=True):
                # Not filled and not findable as working: the cancellation took.
                order_log.record_fill(coid, filled_qty=int(
                    (row or {}).get("filled_qty") or 0),
                    avg_price=(row or {}).get("avg_fill_price"),
                    state="CANCELLED")
        except Exception as e:
            log.debug("cancel confirmation poll failed for %s: %s", coid, e)
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def residual_orders() -> list[dict]:
    """Protective legs this software failed to cancel, still unaccounted for."""
    try:
        return list(db.get_state().get("residual_orders") or [])
    except Exception:
        return []


def clear_residual_order(broker_order_id: str) -> bool:
    """Drop a residual once it is confirmed gone. Returns True if one was held."""
    current = residual_orders()
    kept = [r for r in current if str(r.get("broker_order_id")) != str(broker_order_id)]
    if len(kept) == len(current):
        return False
    db.update_state({"residual_orders": kept})
    log.info("residual order %s resolved and cleared", broker_order_id)
    return True


def _exit_and_book(client: MooClient, symbol: str, trade: dict, trades: dict,
                   qty: int, limit_px: float, quote_px: float,
                   reason: str) -> tuple[float, float, int]:
    """Sell up to `qty`, book exactly what sold, and leave any residual held.

    Returns (pnl, exit_price, sold).

    This exists because the booking quantity and the decision to remove the
    position were made in ten different places, each from the number the caller
    ASKED to sell. A partial sale then booked a close for shares that had not
    sold and removed a position that was still held — after which the software
    believed it was flat while the broker was not, and the next exit for those
    shares would have been a sell of stock we did not own. That is the shape of
    every naked short in this account.

    Both decisions now come from one number, the filled quantity, and the caller
    must not remove the position itself.
    """
    fill = _sell_and_book_price(client, symbol, qty, limit_px, quote_px, reason)
    return _book_exit(symbol, trade, trades, fill, reason)


def _book_exit(symbol: str, trade: dict, trades: dict, fill: "ExitFill",
               reason: str) -> tuple[float, float, int]:
    """Book a completed sale and settle the position. Returns (pnl, price, sold).

    Split from the sell so the stop-hit path can decide its label — BREAKEVEN
    or SL — from the price the fill actually came back at, which is the rule
    that path is written around, and still have the quantity and the removal
    decided here rather than in the caller.
    """
    if fill.filled <= 0:
        return 0.0, fill.price, 0

    # Through the settler, not around it.
    #
    # This used to book the close itself and then mark the fill applied in a
    # SEPARATE transaction — a crash between the two re-applied the same shares
    # on the next sweep. And its arithmetic was the executor's while the
    # bracket paths used the settler's, which is how the two drifted.
    #
    # One writer now: the ledger row, the position change and the applied mark
    # all commit together, and there is a single definition of what a close
    # looks like.
    from . import fill_settler
    if fill.client_order_id:
        out = fill_settler.settle(fill.client_order_id)
        # The settler works on the database; refresh the caller's in-memory map
        # so it does not save a stale copy over what just committed.
        fresh = db.get_open_trade(symbol)
        if fresh:
            trades[symbol] = fresh
            log.warning("%s: %d share(s) still held after a partial %s — the "
                        "position stays open and protected",
                        symbol, fresh["qty"], reason)
        else:
            trades.pop(symbol, None)
        return out.get("pnl", 0.0), out["price"], out["applied"]

    # No order id: a legacy path that placed without the log. Book it the old
    # way rather than silently doing nothing, and say so.
    log.warning("%s: booking a %s close with no order id — the fill cannot be "
                "settled idempotently", symbol, reason)
    pnl = _close_and_log(symbol, trade, fill.filled, fill.price, reason)
    remaining = int(trade.get("qty", 0)) - fill.filled
    if remaining > 0:
        trade["qty"] = remaining
        trades[symbol] = trade
    else:
        trades.pop(symbol, None)
    _save_open_trades(trades)
    return pnl, fill.price, fill.filled


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
    """OCO check: settle whatever each leg actually did, and pull the other.

    Returns an action dict if a leg moved shares, else None. Trades without
    bracket ids (SIMULATE or REAL-fallback) return None — soft logic handles
    them downstream.

    REWRITTEN because this was the last place still booking whole positions on
    partial fills. It read `is_order_filled(include_partial=True)`, treated that
    boolean as "the position is gone", booked trade["qty"] and let the caller
    remove the trade. A stop that filled 10 of 100 therefore closed the whole
    holding in the books while 90 shares stayed at the broker — and its own
    comment said reconcile would re-sync the residual, which is the thing that
    must not be relied on.

    It also priced the close at the TRIGGER LEVEL when the fill lookup failed.
    A broker STOP becomes a market order once touched, so in a gap it fills well
    below the level; booking the level makes every gapped stop-out look like a
    clean -1R and feeds that fiction to half-Kelly and the optimizer.

    BOTH legs are now settled, every time. They are separate orders and can both
    have moved shares — that is the OCO race, and the settler's oversell check is
    what catches it rather than whichever leg happened to be inspected first.
    """
    stop_id = trade.get("stop_order_id")
    tp_id = trade.get("tp_order_id")
    if not (stop_id and tp_id):
        return None

    from . import fill_settler, order_log

    # Refresh both legs from the broker, then settle the increments. Order
    # matters only in that both must happen: settling one and returning would
    # leave the other's fills unapplied until some later sweep.
    moved, action = [], None
    for oid, leg, kind in ((stop_id, "stop", "SL_BRACKET"),
                           (tp_id, "take-profit", "TP_BRACKET")):
        row = order_log.by_broker_id(oid)
        if row is None:
            continue
        try:
            fill = client.get_order_fill(oid)
        except Exception as e:
            log.warning("%s: could not read the %s leg (%s) — leaving it for "
                        "the next pass", symbol, leg, e)
            continue
        if fill:
            order_log.record_fill(row["client_order_id"],
                                  filled_qty=int(fill["qty"]),
                                  avg_price=float(fill["price"]),
                                  state=order_log.map_broker_status(
                                      fill.get("status")))
        try:
            out = fill_settler.settle(row["client_order_id"])
        except fill_settler.OversoldError:
            # Both legs sold. The settler has already halted; re-raising here
            # would look like a manage-loop crash rather than the account-level
            # problem it is.
            log.error("%s: both bracket legs moved shares — trading halted",
                      symbol)
            return {"type": "bracket_oversold", "symbol": symbol, "qty": 0,
                    "price": 0.0, "pnl": 0.0}
        if out["applied"]:
            moved.append((leg, kind, out))

    if not moved:
        return None

    # A leg fired, so the opposite one is a live SELL for shares that are now
    # gone. cancel_protective halts if it will not cancel.
    for oid, leg in ((tp_id, "take-profit"), (stop_id, "stop")):
        other = order_log.by_broker_id(oid)
        if other and other["state"] not in order_log.TERMINAL_STATES \
                and not any(m[2].get("kind") == other["kind"] for m in moved):
            cancel_protective(client, symbol, oid, leg)

    leg, kind, out = moved[-1]
    log.info("OCO: %s %s leg settled %d share(s) @ $%.4f, %d remain",
             symbol, leg, out["applied"], out["price"], out["position_qty"])
    return {"type": "stop_hit_bracket" if kind == "SL_BRACKET"
                    else "tp_hit_bracket",
            "symbol": symbol, "price": out["price"], "qty": out["applied"],
            "partial": out["position_qty"] > 0, "pnl": out.get("pnl", 0.0)}


def _bracket_fill_price(client: MooClient, order_id: str, level: float,
                        symbol: str, reason: str) -> float:
    """Actual fill price of an already-filled bracket leg, else its `level`.

    No polling: the leg is known filled before we get here, so one query is
    enough. Falls back to the trigger level (the old behaviour) if the broker
    can't tell us, so a lookup failure degrades instead of blocking the close."""
    fill = client.get_order_fill(order_id)
    if not fill:
        log.info("%s %s: fill price unavailable for leg %s — booking at level "
                 "$%.2f", symbol, reason, order_id, level)
        return level
    px = fill["price"]
    if abs(px - level) / max(level, 1e-9) > 0.001:
        log.info("%s %s: filled $%.2f vs level $%.2f (%+.1f bps slippage)",
                 symbol, reason, px, level, (px - level) / level * 1e4)
    return px


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
                 last: float, reason: str,
                 trades: dict | None = None) -> tuple[float, dict]:
    """Cancel any bracket legs and market-sell the position. Returns (pnl, action).

    `trades` is the live position map. It is passed in so the removal happens
    HERE, from the quantity that actually sold, rather than in each of the nine
    callers from the quantity that was requested — on a partial sale those are
    different numbers, and popping on the second one leaves the software flat
    while the broker still holds shares.

    2026-07-07: a bracket leg that FAILS to cancel may still be live — or may
    already have filled — so selling on top of it risks a double sell. Same
    defer-to-next-cycle policy the max-hold path adopted (2026-07-02 P2):
    raise, let the per-symbol wrapper log it, retry next cycle."""
    for key, leg in (("stop_order_id", "stop"), ("tp_order_id", "take-profit")):
        oid = trade.get(key)
        if oid and not cancel_protective(client, symbol, oid, leg):
            raise RuntimeError(
                f"{symbol}: cancel of bracket leg {key}={oid} failed — deferring "
                f"{reason} close to next cycle (leg may be live or already "
                f"filled). Trading is halted until the residual is resolved.")
    # The SELL must actually be placed before the close is booked (2026-07-02
    # audit P1-1). The old version swallowed a failed order and booked the close
    # anyway — the broker still held the shares, the books said "realized", and
    # the next reconcile re-adopted the live position as an orphan with
    # fabricated levels. Now a failure propagates to the caller (every call
    # site wraps per-symbol), the trade record stays tracked, and the exit is
    # retried next cycle. Note: any bracket legs were already cancelled above,
    # so until the retry succeeds the position is soft-tracked only.
    requested = int(trade["qty"])
    pnl, exit_px, sold = _exit_and_book(
        client, symbol, trade, trades if trades is not None else {},
        requested, last * (1 - PROTECTIVE_EXIT_SLIP), last, reason)
    if sold <= 0:
        raise RuntimeError(
            f"{symbol}: {reason} exit filled nothing — the position is still "
            f"held and stays tracked; retrying next cycle")
    # An AI/sentinel risk-off close means "don't hold this today" — block the
    # scanner from buying it straight back this session (DELL churn, 2026-07-14).
    # EOD_FLAT joins them for a different reason: the flatten runs on the 5-min
    # manage tick but the scanner keeps scanning until 16:00, so without a
    # cooldown a name flattened at 15:45 is re-bought at 15:50 and flattened
    # again at 15:55 — pure churn at the widest spreads of the day.
    if reason in ("GAP_RISK", "SMART_EXIT", "EOD_FLAT"):
        _set_reentry_cooldown(symbol, reason)
    return pnl, {"type": reason.lower(), "symbol": symbol, "price": exit_px,
                 "qty": sold, "partial": sold < requested, "pnl": pnl}


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
            # The settler already removed the position if it went to zero, and
            # deliberately kept it if a partial fill left shares held. Popping
            # here unconditionally is what closed a whole holding on a leg that
            # moved ten shares.
            if bracket_action.get("partial"):
                trades[symbol] = db.get_open_trade(symbol) or trade
            else:
                trades.pop(symbol, None)
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
            pnl, action = _force_close(client, symbol, trade, last, "STALL_OUT", trades)
            actions.append(action)
            # The position was removed inside _force_close, from the quantity
            # that actually sold. Popping again here would discard a residual
            # that a partial fill legitimately left held.
            log.info("Stall-out close (bracket): %s @ $%.2f (pnl=%.2f)", symbol, last, pnl)
            return

        if age_days >= runtime_config.max_hold_days():
            # Pull bracket legs first, then market-sell remaining qty. A leg
            # that fails to cancel may still be live — or already FILLED —
            # so selling on top of it risks a double sell. Defer to the next
            # cycle: _check_bracket_fills will book a fill, or the cancel is
            # retried (2026-07-02 audit P2).
            for oid, leg in ((trade["stop_order_id"], "stop"),
                             (trade["tp_order_id"], "take-profit")):
                if oid and not cancel_protective(client, symbol, oid, leg):
                    log.warning("%s max-hold: cancel of bracket leg %s failed — "
                                "position left held, trading halted", symbol, oid)
                    return
            pnl, exit_px, sold = _exit_and_book(
                client, symbol, trade, trades, int(trade["qty"]),
                last * (1 - PROTECTIVE_EXIT_SLIP), last, "MAX_HOLD")
            if sold:
                actions.append({"type": "max_hold_bracket", "symbol": symbol,
                                "price": exit_px, "qty": sold,
                                "age_days": age_days, "pnl": pnl})
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
        fill = _sell_and_book_price(
            client, symbol, int(trade["qty"]),
            last * (1 - PROTECTIVE_EXIT_SLIP), last, reason)
        exit_px = fill.price
        # Relabel AFTER the fill is known — the rule above is explicitly about
        # "the price the close is actually booked at", and since 2026-07-27 that
        # is the broker's fill, not the pre-order quote.
        if reason == "BREAKEVEN":
            _risk = float(trade.get("init_risk_per_share") or 0.0)
            _entry = float(trade["entry_price"])
            _loss_r = ((exit_px - _entry) / _risk) if _risk > 0 else 0.0
            if _loss_r <= -0.25 or (_risk <= 0 and exit_px < _entry * 0.995):
                reason = "SL"
        stop_level = trade["stop_loss"]
        pnl, exit_px, sold = _book_exit(symbol, trade, trades, fill, reason)
        if sold:
            actions.append({"type": "stop_hit", "symbol": symbol,
                            "price": exit_px, "qty": sold,
                            "stop": stop_level, "pnl": pnl})
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
        pnl, action = _force_close(client, symbol, trade, last, "STALL_OUT", trades)
        actions.append(action)
        # The position was removed inside _force_close, from the quantity
        # that actually sold. Popping again here would discard a residual
        # that a partial fill legitimately left held.
        log.info("Stall-out close: %s @ $%.2f (pnl=%.2f)", symbol, last, pnl)
        return

    # Max-hold force close.
    opened = datetime.fromisoformat(trade["opened_at"])
    age_days = _business_days_between(opened, datetime.utcnow())
    if age_days >= runtime_config.max_hold_days():
        pnl, exit_px, sold = _exit_and_book(
            client, symbol, trade, trades, int(trade["qty"]),
            last * (1 - PROTECTIVE_EXIT_SLIP), last, "MAX_HOLD")
        if sold:
            actions.append({"type": "max_hold", "symbol": symbol,
                            "price": exit_px, "qty": sold,
                            "age_days": age_days, "pnl": pnl})
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
                pnl, exit_px, sold = _exit_and_book(
                    client, symbol, trade, trades, tranche, last, last, "TP1")
                # tp1_done only when the tranche actually sold. Marking it on a
                # request would skip TP1 forever after a partial fill.
                if sold:
                    trade["tp1_done"] = True
                    actions.append({"type": "scale_out", "tranche": 1,
                                    "symbol": symbol, "price": exit_px,
                                    "qty": sold, "pnl": pnl})
            # TP2 — second 1/3 (may also fire this same tick if price is already
            # through both levels, matching the backtest's per-bar sequential check)
            if trade.get("tp1_done") and not trade.get("tp2_done") \
                    and last >= entry + _settings.tp2_r * risk and tranche < trade["qty"]:
                pnl, exit_px, sold = _exit_and_book(
                    client, symbol, trade, trades, tranche, last, last, "TP2")
                if sold:
                    trade["tp2_done"] = True
                    actions.append({"type": "scale_out", "tranche": 2,
                                    "symbol": symbol, "price": exit_px,
                                    "qty": sold, "pnl": pnl})
        # runner exits WHOLE at the single take-profit (no trail) — same full-TP
        # close the backtest books for the remaining lot.
        if last >= trade["take_profit"] and trade["qty"] > 0:
            pnl, exit_px, sold = _exit_and_book(
                client, symbol, trade, trades, int(trade["qty"]),
                last, last, "TP")
            if sold:
                actions.append({"type": "tp_full", "symbol": symbol,
                                "price": exit_px, "qty": sold, "pnl": pnl})
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
        pnl, exit_px, sold = _exit_and_book(
            client, symbol, trade, trades, int(trade["qty"]), last, last, "TP")
        if sold:
            actions.append({"type": "tp_full", "symbol": symbol,
                            "price": exit_px, "qty": sold, "pnl": pnl})
        return


def manage_open_trades(client: MooClient) -> list[dict]:
    """Thread-safe wrapper — the scan job and the 5-min manage tick both call
    this from different scheduler threads; the lock keeps their load→mutate→
    save cycles on the open-trades store from clobbering each other."""
    with _TRADES_LOCK:
        _settle_outstanding_fills()
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
        # Before reading positions, not after: a late fill may BE the position
        # this pass is about to decide a stop for.
        _settle_outstanding_fills()
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


def _settle_outstanding_fills() -> None:
    """Apply any fills that arrived after their order's wait window closed.

    Runs at the top of every manage tick. Before this, a fill that landed one
    second after the entry stopped waiting updated the orders table and nothing
    else — the shares were at the broker and in no position, and the only thing
    that would ever have noticed was a restart.
    """
    try:
        from . import fill_settler
        fill_settler.settle_all()
    except Exception as e:
        log.error("fill settlement sweep failed: %s", e)


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
                                           last, "BLACKLIST", trades)
                actions.append(action)
                # The position was removed inside _force_close, from the quantity
                # that actually sold. Popping again here would discard a residual
                # that a partial fill legitimately left held.
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
                                           last, "GAP_RISK", trades)
                actions.append(action)
                # The position was removed inside _force_close, from the quantity
                # that actually sold. Popping again here would discard a residual
                # that a partial fill legitimately left held.
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
                                           last, "SMART_EXIT", trades)
                actions.append(action)
                # The position was removed inside _force_close, from the quantity
                # that actually sold. Popping again here would discard a residual
                # that a partial fill legitimately left held.
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

    # --- Auto-flush 0.7: news-driven EOD flatten (收盘平仓) ---
    # In news-driven mode the thesis is one session long, so nothing carries
    # overnight — at NEWS_DRIVEN_FLATTEN_ET (default 15:45 ET) every bot-managed
    # position is closed regardless of P&L, TP or stop. This is the second half
    # of what the switch promises; the first half is the entry gate in main.py.
    #
    # Owner-held manual positions are exempt via `_skip`, same as every other
    # auto-exit. A halted/priceless name is left alone rather than dumped at $0,
    # which does mean it can survive to the next session — the alternative is
    # selling into a void, and the protective bracket still covers it.
    if news_driven.flatten_now():
        for symbol in list(trades.keys()):
            if symbol in _skip:
                continue
            last = _last_price(client, symbol)
            if last is None:
                log.warning("EOD flatten: no price for %s — leaving it "
                            "(will carry overnight)", symbol)
                continue
            try:
                pnl, action = _force_close(client, symbol, trades[symbol],
                                           last, "EOD_FLAT", trades)
                actions.append(action)
                # The position was removed inside _force_close, from the quantity
                # that actually sold. Popping again here would discard a residual
                # that a partial fill legitimately left held.
                log.warning("News-driven EOD flatten: %s @ $%.2f (pnl=%.2f)",
                            symbol, last, pnl)
                try:
                    from . import notifier
                    notifier.send(f"🔔 收盘平仓: {symbol} @ ${last:.2f} "
                                  f"(已实现 ${pnl:+.0f}) — 新闻主导模式不留隔夜仓")
                except Exception:
                    pass
            except Exception as e:
                log.warning("EOD flatten %s failed: %s", symbol, e)

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
                                           last, "OVER_CAP", trades)
                actions.append(action)
                # The position was removed inside _force_close, from the quantity
                # that actually sold. Popping again here would discard a residual
                # that a partial fill legitimately left held.
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
    for key, leg in (("stop_order_id", "stop"), ("tp_order_id", "take-profit")):
        oid = trade.get(key)
        if oid and not cancel_protective(client, symbol, oid, leg):
            raise RuntimeError(
                f"{symbol}: the {leg} order {oid} could not be cancelled — "
                f"refusing to sell into a live protective order. Trading is "
                f"halted; the position stays held and stays tracked.")
    requested = int(trade["qty"])
    pnl, exit_px, sold = _exit_and_book(
        client, symbol, trade, trades, requested,
        last * (1 - PROTECTIVE_EXIT_SLIP), last, reason)
    if sold <= 0:
        raise RuntimeError(f"{symbol}: {reason} exit filled nothing — the "
                           f"position is still held and stays tracked")
    if reason in ("GAP_RISK", "SMART_EXIT"):
        _set_reentry_cooldown(symbol, reason)
    return {"type": reason.lower(), "symbol": symbol, "qty": sold,
            "partial": sold < requested, "price": exit_px, "pnl": pnl}


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
    # A manual close is still a close: selling on top of a protective leg that
    # would not cancel is the same double-sale as anywhere else, and the fact
    # that a person asked for it does not make the surviving order go away.
    for key, leg in (("stop_order_id", "stop"), ("tp_order_id", "take-profit")):
        oid = trade.get(key)
        if oid and not cancel_protective(client, symbol, oid, leg):
            raise RuntimeError(
                f"{symbol}: the {leg} order {oid} could not be cancelled — "
                f"refusing the manual close rather than selling into a live "
                f"protective order. Trading is halted; resolve it at the broker.")

    requested = int(trade["qty"])
    pnl, exit_px, sold = _exit_and_book(
        client, symbol, trade, trades, requested,
        last * (1 - PROTECTIVE_EXIT_SLIP), last, "MANUAL")
    if sold <= 0:
        raise RuntimeError(f"{symbol}: the manual close filled nothing — the "
                           f"position is still held and stays tracked")
    return {"type": "manual_close", "symbol": symbol, "qty": sold,
            "partial": sold < requested, "price": exit_px, "pnl": pnl}


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
            if not cancel_protective(client, symbol, new_stop_id, "stop"):
                raise RuntimeError(
                    f"{symbol}: the existing stop {new_stop_id} could not be "
                    f"cancelled — refusing to place a second one. Trading is "
                    f"halted; the OLD stop is still live and still protecting "
                    f"the position at its previous level.")
            new_stop_id = client.place_stop_loss(
                symbol, trade["qty"], new_stop,
                intent='stop edit').broker_order_id
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
