"""Risk gate — hard rules. AI cannot bypass these.

Each function returns (allowed: bool, reason: str). The caller MUST short-circuit
on the first False.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from . import clock, db
from .config import derive_max_positions, settings
from .indicators import Signal

log = logging.getLogger(__name__)

STATE_FILE = settings.root / "data" / "state.json"   # legacy mirror only

_DEFAULT_STATE = {
    "day": str(date.today()),
    "starting_cash": 0.0,
    "realized_pnl_today": 0.0,
    "halted": False,
    # Account-level peak equity for the drawdown circuit breaker. Tracked
    # via record_trade_close on every realised PnL — independent of
    # starting_cash so it survives day rollovers.
    "peak_equity": 0.0,
}


def _load_state() -> dict:
    """Read all kv_state rows; fill defaults for missing keys."""
    s = dict(_DEFAULT_STATE)
    s.update(db.get_state())
    return s


def _save_state(state: dict) -> None:
    """Persist state atomically to SQLite; mirror to legacy JSON for any
    external script still poking at it."""
    db.save_state(state)
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        log.warning("legacy state.json mirror failed: %s", e)


def reset_for_new_day(current_cash: float) -> dict:
    """Idempotent daily rollover, keyed on the NY trading date.

    2026-07-07 P0 fix: this used `date.today()` (SYSTEM LOCAL date) while
    kill_switch.reset_for_new_day used the NY date — both write the same
    kv_state 'day' key. On a GMT+8 host the local date flips to "tomorrow"
    mid-session (~12:00 ET), so the two resets ping-ponged every scan for the
    rest of the session: realized_pnl_today wiped to 0 (killing the daily-DD
    fuse) and `halted` cleared (un-halting a halted bot). Both now key on the
    NY date, and the rollover is an atomic read-modify-write instead of a
    whole-state save that could clobber concurrent updates."""
    today = clock.ny_now().strftime("%Y-%m-%d")

    def _apply(s: dict) -> dict:
        if s.get("day") != today:
            return {
                "day": today,
                "starting_cash": current_cash,
                "realized_pnl_today": 0.0,
                "halted": False,
            }
        return {}

    merged = db.atomic_state(_apply)
    s = dict(_DEFAULT_STATE)
    s.update(merged)
    return s


# Loss-streak day counter REMOVED 2026-07-07 (owner decision): the old
# semantics only counted a day when its FIRST close was a loss and reset on
# any single win — not a meaningful "consecutive losing days" signal. The
# account-level DD breaker (_dd_size_multiplier + DD halt) and the adaptive
# Sortino sizing already cover drawdown response with cleaner definitions.


# Auto-release a sticky DD halt after this many calendar days. Without this
# the live bot (and the backtester) sat halted after a single bad month
# because peak_equity stays high and no new trades = equity stays flat = DD
# never recovers naturally. 7 days lets a regime change play out while
# bounding the damage of being out of the market for too long.
HALT_AUTO_RELEASE_DAYS = 7


# ---- Dynamic capital ────────────────────────────────────────────────────────
# 2026-06-03: sizing/risk/DD no longer anchor to the static .env ACCOUNT_USD.
# They derive from the owner's allocated BUDGET (runtime db-state key
# 'budget_usd' — GUI-editable, takes effect next scan, NO restart), capped by
# the live account equity (so a large SIMULATE balance can't oversize, and a
# drawn-down REAL account auto-sizes down). Falls back to .env ACCOUNT_USD when
# the budget is unset. The backtest engine is untouched — it sizes off
# cfg.account_usd via its own _position_size, so honest-engine numbers are
# unchanged.
_live_equity_cache: dict = {"value": None}


def set_live_equity(value) -> None:
    """main.py calls this once per scan with cash + open-position market value."""
    try:
        v = float(value)
        _live_equity_cache["value"] = v if v > 0 else None
    except (TypeError, ValueError):
        _live_equity_cache["value"] = None


def budget_usd() -> float:
    """Owner's allocated capital. Runtime db-state 'budget_usd', else .env.

    NOTE: when auto-compounding is armed this value GROWS/SHRINKS with realized
    profit (auto_budget writes the same db-state key). It is the DEPLOYABLE cap
    used for sizing — NOT the equity baseline for the DD breaker. For equity/DD
    math use equity_baseline() instead, so realized PnL is never double-counted.
    """
    try:
        v = float(_load_state().get("budget_usd") or 0.0)
        if v > 0:
            return v
    except Exception:
        pass
    return settings.account_usd


def equity_baseline() -> float:
    """Capital base for equity/drawdown math (peak_equity, current_drawdown_pct).

    Delegates to auto_budget: while compounding is armed this is the FROZEN seed
    (so the compounding deployable budget can't inflate equity = base + realized
    and mis-fire the DD breaker); otherwise it is the live budget — byte-
    identical to the pre-compounding behavior. Lazy import avoids a cycle
    (auto_budget reads budget_usd here)."""
    try:
        from . import auto_budget
        return auto_budget.equity_baseline()
    except Exception:
        return budget_usd()


def sizing_capital() -> float:
    """Capital used for position sizing + the hard budget cap: the allocated
    budget, never exceeding live account equity (when known)."""
    b = budget_usd()
    le = _live_equity_cache["value"]
    return min(b, le) if le else b


def max_positions() -> int:
    """Live position-slot cap, derived from the allocated budget so it scales when
    the owner changes capital (req#1). Mirrors the backtest engine's
    derive_max_positions(cfg.account_usd); at the $4.5k default both clamp to the
    max_positions floor (5), so behaviour is unchanged until capital grows."""
    return derive_max_positions(budget_usd())


def current_drawdown_pct() -> float:
    """Account-level drawdown as a percent, computed from peak_equity in state.

    Returns 0.0 if peak is unknown (no closed trades yet) — i.e. a fresh
    account starts at 0% DD and can't be circuit-broken.
    """
    state = _load_state()
    peak = float(state.get("peak_equity") or 0.0)
    if peak <= 0:
        return 0.0
    realized = float(state.get("realized_pnl_total") or 0.0)
    equity = equity_baseline() + realized
    if equity >= peak:
        return 0.0
    return (peak - equity) / peak * 100


def _check_halt_auto_release(state: dict) -> tuple[float, bool]:
    """Check if a sticky DD halt should be auto-released.

    Returns (current_dd_pct, was_released). When the halt has been active
    for >= HALT_AUTO_RELEASE_DAYS, we reset peak_equity to current equity so
    DD drops to 0 and trading resumes. Logged so the user can see when it
    fires (rare event in practice — most halts recover via TP within 7d).
    """
    from datetime import datetime, timezone, timedelta
    dd_pct = current_drawdown_pct()
    halt_at = state.get("halt_started_at")
    if not halt_at:
        return dd_pct, False
    try:
        started = datetime.fromisoformat(halt_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = datetime.now(timezone.utc) - started
        if elapsed >= timedelta(days=HALT_AUTO_RELEASE_DAYS):
            # Auto-release: reset peak to CURRENT equity so DD = 0. Other
            # risk multipliers (size_cut at 10% DD, adaptive Sortino, loss
            # streak) still scale qty down — the DD halt is a hard stop, not
            # a soft brake.
            realized = float(state.get("realized_pnl_total") or 0.0)
            new_peak = max(equity_baseline() + realized, 1.0)
            def _apply(_s):
                return {"peak_equity": new_peak, "halt_started_at": None}
            db.atomic_state(_apply)
            log.warning(
                "DD halt auto-released after %.1f days — peak reset from $%.0f to $%.0f",
                elapsed.total_seconds() / 86400,
                state.get("peak_equity", 0),
                new_peak,
            )
            # Feedback 铁律: this resumes trading after a halt — notify the owner.
            try:
                from . import notifier
                notifier.send(
                    f"🔄 DD 熔断自动解除（已暂停 {HALT_AUTO_RELEASE_DAYS} 天）— "
                    f"峰值重置为 ${new_peak:.0f}，恢复开新仓。"
                )
            except Exception:
                pass
            return 0.0, True
    except (ValueError, TypeError) as e:
        log.debug("halt timer parse error: %s", e)
    return dd_pct, False


def _dd_size_multiplier() -> float:
    """Halve qty when DD ≥ DD_SIZE_CUT_PCT (default 10%).
    Returns 1.0 below threshold."""
    dd = current_drawdown_pct()
    if dd >= settings.dd_size_cut_pct:
        return 0.5
    return 1.0


def calc_position_size(signal: Signal, vix: float = 15.0,
                       conviction: float = 1.0, regime_mult: float = 1.0) -> int:
    """Risk-based sizing with independent scaling layers.

    Layer 1 — Base risk:      account_usd × risk_per_trade  (e.g. 2% of $4500 = $90)
    Layer 2 — DD size cut:    50% when account drawdown ≥ DD_SIZE_CUT_PCT
                              (loss-streak layer removed 2026-07-07 — owner
                              decision; the DD breaker covers it cleanly)
    Layer 3 — VIX mult:       100% / 50% / 25% by vol regime
    Layer 4 — ML conviction:  caller-supplied 0.0-1.0 multiplier
                              (0.5 for neutral-zone ML, 1.0 for high-conviction)
    Layer 5 — Regime boost:   caller-supplied ≥1.0 UP-scaler, used ONLY in a
                              confirmed strong bull + calm VIX (owner-approved
                              tailwind press; default 1.0 = no change). Mirrors
                              the honest engine's use_regime_scaling for parity.

    Final qty = floor(min(risk_qty, cap_qty)) — all multipliers compose, and the
    regime boost still bows to the per-name cap so it can't breach concentration.
    """
    if conviction <= 0:
        return 0
    # Defensive: Layer 5 is an UP-scaler only — never let a stray <1 value secretly
    # shrink risk (that's what the DD/VIX/conviction layers are for).
    regime_mult = max(1.0, float(regime_mult))
    # Account-level DD breaker: halve size at deep drawdown.
    dd_size_mult = _dd_size_multiplier()
    # Adaptive sizing — follows the rolling-30-trade Sortino. Lazy import
    # avoids a hard dependency at module load time (and a circular if
    # adaptive_sizing ever needs to read settings/portfolio).
    try:
        from . import adaptive_sizing
        adaptive_mult, _adaptive_reason = adaptive_sizing.compute_multiplier()
    except Exception as e:
        log.debug("adaptive_sizing skipped: %s", e)
        adaptive_mult = 1.0
    cap = sizing_capital()
    # risk_per_trade is runtime-overridable (half-Kelly proposal, owner-approved).
    from . import runtime_config

    # PnL-optimised (2026-06-27 MS audit): composite floor on account-state
    # multipliers. dd_size_mult × adaptive_mult can compound to 0.25× in deep
    # DD — shrinking positions so small the bot can never recover its
    # drawdown. Floor it at 0.25× so even in worst conditions, risk stays
    # ≥ 1.25% of account (0.25 × 5% base). Signal-quality (conviction) and
    # regime tailwind (regime_mult) compose ON TOP and are not floored — a
    # marginal signal in a deep DD SHOULD still be half-sized, and a bull
    # tailwind SHOULD still add its boost.
    state_mult = dd_size_mult * adaptive_mult
    if state_mult < 0.25:
        state_mult = 0.25
    risk_dollars = (cap * runtime_config.risk_per_trade()
                    * state_mult * conviction * regime_mult)

    stop_distance = signal.price - signal.stop_loss
    if stop_distance <= 0:
        return 0
    qty_by_risk = int(risk_dollars / stop_distance)
    # 2026-06-12: cap is runtime-tunable (cap ablation: 40% ≥ 70% on every
    # in-sample metric; optimizer may tune within [0.20, 0.55] — the ceiling
    # is the untunable tail-risk guard against single-name overnight gaps).
    qty_by_cap = int(cap * runtime_config.max_position_pct() / signal.price)
    base = max(0, min(qty_by_risk, qty_by_cap))
    if base <= 0:
        # Un-sizeable on this account: either one share already breaches the
        # per-name notional cap, or one share's stop distance exceeds the whole
        # risk budget. Declining is correct — but say so, because it used to be
        # silent and the weekly universe refresh kept re-selecting such names
        # (2026-07-27: 5 of 15 watchlist tickers were permanently unbuyable,
        # still consuming a kline fetch + AI budget on every scan).
        log.info("%s: not sizeable — qty_by_risk=%d (risk $%.0f / stop $%.2f), "
                 "qty_by_cap=%d (cap $%.0f / price $%.2f) — entry declined",
                 signal.symbol, qty_by_risk, risk_dollars, stop_distance,
                 qty_by_cap, cap * runtime_config.max_position_pct(), signal.price)
        return 0

    # VIX de-risking. 2026-07-27: this used to be max(1, base // 4) — the floor
    # of 1 meant a name whose base size was already 1-2 shares got its FULL
    # (or 2-4x its intended) size in exactly the tape the cut exists for. At
    # VIX>35 with base=1 the intended size is 0.25 shares and the old code
    # delivered 1 — 4x the intended risk. Integer share sizes can't express
    # a partial de-risk, so the honest answer is to skip the name rather than
    # take a position 4x larger than the vol regime allows. This is the only
    # place the composed multipliers could be silently overridden upward; the
    # DD/adaptive/conviction layers all scale risk_dollars, which rounds down
    # cleanly through qty_by_risk.
    divisor = 4 if vix > 35 else (2 if vix > 25 else 1)
    if divisor == 1:
        return base
    scaled = base // divisor
    if scaled <= 0:
        log.info("%s: VIX %.1f wants 1/%d size but base is only %d share(s) — "
                 "skipping rather than taking %dx the intended risk",
                 signal.symbol, vix, divisor, base, divisor)
        return 0
    return scaled


def _can_stack_onto(signal: Signal, held: pd.DataFrame) -> tuple[bool, str]:
    """Gate for adding another entry to a symbol we already hold.

    Requires:
      • current stack count < MAX_STACKS_PER_SYMBOL
      • unrealised R-multiple ≥ STACK_MIN_R_MULTIPLE (only add to winners)
    """
    if settings.max_stacks_per_symbol <= 1:
        return False, "stacking disabled (MAX_STACKS_PER_SYMBOL ≤ 1)"

    open_trades = db.load_open_trades()
    rec = open_trades.get(signal.symbol)
    if not rec:
        # Held at broker but no local trade record — refuse to stack blindly.
        return False, f"no local trade record for {signal.symbol} (skip stack)"

    stacks = int(rec.get("stacks", 1))
    if stacks >= settings.max_stacks_per_symbol:
        return False, (f"max stacks ({settings.max_stacks_per_symbol}) "
                       f"reached for {signal.symbol}")

    entry = float(rec.get("entry_price", 0) or 0)
    # Use init_risk_per_share (ORIGINAL R unit at entry) instead of current
    # stop_loss for stacking gate. Breakeven ratchet can raise stop to entry
    # (stop == entry → R=0), which makes _can_stack_onto reject every future
    # add-on even when the position has run far into profit. The original R
    # unit is stable — it measures the thesis risk at entry, which is the
    # right baseline for "is this trade working?".
    irps = float(rec.get("init_risk_per_share", 0) or 0)
    if irps > 0:
        r_unit = irps
    else:
        # Fallback for legacy trades opened before init_risk_per_share existed.
        stop = float(rec.get("stop_loss", 0) or 0)
        r_unit = entry - stop
    if r_unit <= 0:
        return False, f"invalid R unit for {signal.symbol} (r_unit={r_unit})"

    # Use broker's last price for the symbol if available, else fall back to
    # the live signal price (close of latest scoring bar — same magnitude).
    last_px = float(signal.price)
    if not held.empty:
        row = held[held["code"].str.split(".").str[-1] == signal.symbol]
        if not row.empty:
            np = float(row.iloc[0].get("nominal_price") or 0)
            if np > 0:
                last_px = np

    r_now = (last_px - entry) / r_unit
    if r_now < settings.stack_min_r_multiple:
        return False, (f"{signal.symbol} unrealised {r_now:.2f}R < "
                       f"{settings.stack_min_r_multiple}R (need profit to stack)")

    return True, "ok"


# ── PnL-optimised (2026-06-27 MS audit) ──────────────────────────────────────
# Reduced from 24h → 4h. The 142-day audit measured 29 rebleed losses (−$425)
# from re-entering within 3 days, so 24h was chosen conservatively. But in the
# current high-VIX semiconductor regime, a name that stops out on a volatile
# swing often reverses within the same session — and the bot misses the recovery
# trade. At 4h the bot can re-enter a new signal on the SAME ticker after one
# H1 bar confirms the reversal, while still blocking instant re-bleeds (the
# audit's 3-day window was for trend-era hold periods, not current volatility).
# ⚠ MONITOR: if rebleed count rises above ~5 trades/month, revert to ≥12h.
SL_COOLDOWN_HOURS = 6   # 2026-07-04 timing audit: 12h too slow for fast H1 trading; allows same-session re-entry


def in_sl_cooldown(symbol: str, hours: int = SL_COOLDOWN_HOURS
                   ) -> tuple[bool, str]:
    """Check if `symbol` recently hit SL — caller refuses entry if True.

    2026-05-29 fix: do all comparison in tz-aware UTC. The previous version
    called `astimezone(tz=None)` which converts to SYSTEM LOCAL time, then
    compared with `datetime.utcnow()` (naive UTC) — producing 4-12 hour drift
    depending on the user's timezone. On a Malaysia laptop the cooldown would
    have either never fired (8h ahead, 'elapsed' negative) or fired forever.
    """
    from datetime import datetime, timedelta, timezone
    last = db.last_sl_close_for_symbol(symbol)
    if not last:
        return False, "no prior SL"
    ts = last.get("ts") or ""
    if not ts:
        return False, "no SL timestamp"
    try:
        sl_dt = datetime.fromisoformat(ts)
        # Normalize to UTC. ts comes from `datetime.now(NY).isoformat()`
        # (tz-aware) for new rows; legacy migrations may be naive — in that
        # case we conservatively assume the stamp was UTC, so cooldown will be
        # at worst a few hours OFF, never inverted.
        if sl_dt.tzinfo is None:
            sl_dt = sl_dt.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        elapsed = now_utc - sl_dt
        if elapsed < timedelta(hours=hours):
            remaining = timedelta(hours=hours) - elapsed
            mins = int(remaining.total_seconds() / 60)
            return True, (
                f"SL cooldown: stopped out {int(elapsed.total_seconds()/3600)}h ago, "
                f"resume in {mins // 60}h{mins % 60:02d}m"
            )
    except (ValueError, TypeError) as e:
        log.debug("SL cooldown parse error for %s: %s", symbol, e)
    return False, "cooldown elapsed"


def can_open_new(
    signal: Signal,
    positions: pd.DataFrame,
    current_cash: float,
    pending_value: float = 0.0,
    pending_symbols: set[str] | None = None,
    vix: float = 15.0,
    conviction: float = 1.0,
    regime_mult: float = 1.0,
) -> tuple[bool, str]:
    state = reset_for_new_day(current_cash)
    pending_symbols = pending_symbols or set()

    if state.get("halted"):
        return False, "trading halted (daily drawdown)"

    # SL cooldown — block re-entry on a name that just stopped out. Only
    # applies to brand-new entries; stacking adds onto an EXISTING profitable
    # position so the SL pattern doesn't apply.
    held = positions[positions["qty"].astype(float) > 0] if not positions.empty else positions
    held_symbols = set(held["code"].str.split(".").str[-1].tolist()) if not held.empty else set()
    if signal.symbol not in held_symbols:
        cooling, cool_reason = in_sl_cooldown(signal.symbol)
        if cooling:
            return False, cool_reason

    # Account-level DD halt — independent of daily/streak.
    # Auto-releases after HALT_AUTO_RELEASE_DAYS (7d) so a single bad month
    # doesn't lock trading out for the rest of the year.
    dd_pct, released = _check_halt_auto_release(state)
    if dd_pct >= settings.dd_halt_pct:
        # First time hitting halt → stamp the start time so the auto-release
        # timer starts. Subsequent checks see the timestamp and check elapsed.
        from datetime import datetime, timezone
        if not state.get("halt_started_at"):
            stamp = datetime.now(timezone.utc).isoformat()
            db.atomic_state(lambda _s: {"halt_started_at": stamp})
            log.warning("DD halt triggered: %.1f%% — 7d auto-release timer started",
                        dd_pct)
        return False, (f"DD halt: account drawdown {dd_pct:.1f}% "
                       f"≥ DD_HALT_PCT {settings.dd_halt_pct:.0f}% — auto-release in ≤7d")

    held = positions[positions["qty"].astype(float) > 0] if not positions.empty else positions
    held_symbols = held["code"].str.split(".").str[-1].tolist() if not held.empty else []
    is_stack = signal.symbol in held_symbols

    # Brand-new ticker: enforce MAX_POSITIONS cap. Stacking onto an existing
    # name doesn't count — it's the same broker position with bigger qty.
    if not is_stack:
        unique_names = len(set(held_symbols) | set(pending_symbols))
        cap = max_positions()
        if unique_names >= cap:
            return False, f"max positions ({cap}) reached (incl. pending)"
    else:
        # Stacking path — gated by stacks-count + min unrealised R-multiple.
        ok, reason = _can_stack_onto(signal, held)
        if not ok:
            return False, reason

    if signal.symbol in pending_symbols:
        return False, f"buy order for {signal.symbol} already pending"

    # 2026-07-07: regime_mult is passed through so the cash/budget checks below
    # price the SAME qty the caller will actually order (previously a >1 bull
    # boost was applied only at order time, so these checks under-estimated
    # the capital the entry would commit).
    qty = calc_position_size(signal, vix=vix, conviction=conviction,
                             regime_mult=regime_mult)
    if qty == 0:
        return False, "computed qty=0 (stop too tight, price too high, or conviction=0)"

    required_cash = qty * signal.price
    if required_cash > current_cash:
        return False, f"insufficient cash: need ${required_cash:.0f}, have ${current_cash:.0f}"

    # ----- Hard budget cap (ACCOUNT_USD) — never exceed user's allocated capital.
    # Committed = filled positions + pending buy orders (avoid double-spending).
    invested = 0.0
    if not held.empty:
        invested = float((held["qty"].astype(float) * held["cost_price"].astype(float)).sum())
    committed = invested + pending_value
    cap = sizing_capital()
    if committed + required_cash > cap:
        return False, (f"budget cap ${cap:.0f} would be exceeded "
                       f"(committed ${committed:.0f} + new ${required_cash:.0f})")

    # Daily drawdown stop — measured on TODAY'S REALIZED PnL against the equity
    # baseline (2026-07-02 audit P0-3). The old cash-based measure
    # ((starting_cash − current_cash) / starting_cash) was structurally wrong on
    # both ends: in SIMULATE the paper cash (~$1M) dwarfs the $50k budget so the
    # 6% line could never fire even with the whole budget lost, and in REAL
    # (cash ≈ budget) simply BUYING positions burns >6% of cash and would halt a
    # perfectly healthy day. current_cash no longer feeds this check.
    base = equity_baseline()
    realized_today = float(state.get("realized_pnl_today", 0.0) or 0.0)
    if base > 0 and realized_today < 0:
        daily_loss_frac = -realized_today / base
        if daily_loss_frac >= settings.daily_drawdown_stop:
            # Atomic single-key write — a whole-state save here could clobber
            # concurrent updates (e.g. a close booking PnL on another thread).
            db.atomic_state(lambda _s: {"halted": True})
            return False, (f"daily drawdown {daily_loss_frac:.1%} of ${base:.0f} "
                           f"≥ {settings.daily_drawdown_stop:.0%}")

    return True, "ok"


def record_trade_close(realized_pnl: float, account_usd: float | None = None) -> None:
    """Race-safe R-M-W of PnL totals via SQLite atomic_state.

    Also bumps `peak_equity` for the DD circuit breaker. We measure equity as
    starting_capital + realized_pnl_total — a slight underestimate vs. mark-
    to-market open positions, but stable across position open/close cycles
    and good enough for the DD breaker's 10/15% thresholds.

    (Loss-streak counter removed 2026-07-07 — owner decision; the DD breaker
    and the daily-DD fuse are the drawdown responses.)
    """
    def _apply(current: dict) -> dict:
        new_today = current.get("realized_pnl_today", 0.0) + realized_pnl
        new_total = current.get("realized_pnl_total", 0.0) + realized_pnl
        # account_usd is the equity baseline. Fall back to equity_baseline()
        # (frozen seed when compounding is armed) so growing the deployable
        # budget never double-counts realized PnL into equity / peak.
        base = account_usd if account_usd is not None else equity_baseline()
        current_equity = base + new_total
        peak = max(current.get("peak_equity", 0.0) or 0.0,
                   base,        # never report a peak below starting capital
                   current_equity)
        return {
            "realized_pnl_today": new_today,
            "realized_pnl_total": new_total,
            "peak_equity": peak,
        }

    merged = db.atomic_state(_apply)
    # legacy mirror
    try:
        STATE_FILE.write_text(json.dumps(merged, indent=2, default=str))
    except Exception:
        pass
