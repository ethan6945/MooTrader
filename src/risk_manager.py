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
    "halt_reason": None,
    "halt_detail": None,
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
            out = {
                "day": today,
                "starting_cash": current_cash,
                "realized_pnl_today": 0.0,
            }
            # Only a halt that a new day actually answers is cleared by one.
            #
            # This used to write halted=False unconditionally, which was right
            # while the only halt was the daily drawdown — a new day genuinely
            # resets that. It is wrong now: a stop order at the broker this
            # software could not cancel is not less true tomorrow, and an
            # unresolved order is not resolved by the clock. Those halts were
            # being lifted overnight, and the bot resumed trading into exactly
            # the situation the halt existed to keep it out of.
            if s.get("halted") and _halt_needs_a_person(s.get("halt_reason")):
                log.warning("daily rollover: NOT clearing the halt — %s "
                            "requires a person to resolve it",
                            s.get("halt_reason"))
            else:
                out["halted"] = False
                out["halt_reason"] = None
                out["halt_detail"] = None
            return out
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


def compute_peak_equity(base: float, realized: float,
                        prior_peak: float = 0.0) -> float:
    """The ONE definition of the drawdown high-water mark.

        peak = max(prior_peak, base, base + realized)

    `base` (deployable capital) is a floor because an account that has only ever
    lost money is still in drawdown from where it started — without it, a fresh
    account with realized -$625 would report 0% drawdown instead of 6.25%.

    Two callers used to compute this differently and fight each other:
    record_trade_close() used the expression above, while set_budget() used a
    bare `base + realized`, which erased exactly that real drawdown — and was
    then silently overwritten by the `base` floor on the very next close. Pass
    prior_peak=0 to re-anchor on a capital change (deliberately forgetting a
    peak recorded under a different capital base), or the stored peak to advance
    it monotonically.
    """
    return max(float(prior_peak or 0.0), float(base), float(base) + float(realized), 1.0)


def set_budget(value: float, source: str) -> dict:
    """The ONLY way to change deployable capital. Re-anchors the DD breaker.

    Changing the budget without moving `peak_equity` with it silently disables
    the drawdown circuit breaker, because `current_drawdown_pct()` measures
    equity against a peak recorded under the OLD capital base:

      • budget DOWN (50k → 5k): the stale high peak reads as a phantom ~90%
        drawdown and halts all entries — the 2026-07-07 incident.
      • budget UP (4.7k → 10k): equity immediately exceeds the stale low peak,
        so drawdown pins at 0.0% and the breaker can never fire. On 2026-08-10
        this left DD_HALT_PCT=18 needing a 59% real loss before it would trip.

    That re-anchoring used to live inline inside the web /api/budget handler, so
    it protected exactly one caller and silently missed every other. Both
    failure modes above were caused by a writer that skipped it. It lives here
    now; write `budget_usd` through this function and nowhere else.

    Not blocked by the param freeze — setting capital is the owner's decision,
    and the freeze exists to stop the bot retuning ITSELF. It is audited though,
    so a budget change is never anonymous.
    """
    value = float(value)
    if value <= 0:
        raise ValueError(f"budget must be positive, got {value}")

    def _apply(s: dict) -> dict:
        realized = float(s.get("realized_pnl_total") or 0.0)
        # While compounding is armed the equity baseline is the frozen seed, so
        # the peak must be re-anchored to that same base — not to the new
        # deployable budget, which would double-count realized PnL.
        base = float(s.get("auto_budget_seed") or value)
        # prior_peak=0: a peak recorded under the OLD capital base is exactly
        # what we are here to forget. The `base` floor inside the helper keeps
        # a real drawdown visible rather than resetting it to zero.
        return {"budget_usd": value,
                "peak_equity": compute_peak_equity(base, realized, prior_peak=0.0),
                "halt_started_at": None}

    merged = db.atomic_state(_apply)
    try:
        from . import audit
        audit.record("budget_change", reason=f"budget → ${value:,.0f} ({source})",
                     extra={"budget_usd": value,
                            "peak_equity": merged.get("peak_equity"),
                            "source": source})
    except Exception as e:
        log.debug("budget audit record failed: %s", e)
    log.info("budget set to $%.0f by %s — DD peak re-anchored to $%.0f",
             value, source, merged.get("peak_equity", 0))
    return {"budget_usd": value, "peak_equity": merged.get("peak_equity")}


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

    # Open losses count. They did not before: equity was realized-only, so a
    # portfolio down 30% on everything it held reported 0% drawdown until
    # something was sold — the circuit breaker unable to fire in exactly the
    # situation it exists for, and able to fire only after the damage was
    # already booked.
    #
    # The asymmetry is deliberate. Unrealised LOSSES lower equity here, while
    # unrealised GAINS never raise the peak (compute_peak_equity takes realized
    # only). Paper profit that has not been sold is not a high-water mark to
    # measure future losses against; paper loss is money currently gone.
    live = _live_equity_cache.get("value")
    if live is not None and live > 0:
        equity = min(equity, float(live))

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
    # The seven-day release re-anchors peak equity so the drawdown reads zero.
    # That answers a DRAWDOWN halt. Applied to an order or reconciliation halt
    # it would resume trading a week after a discrepancy nobody looked at, and
    # the discrepancy would still be there — with the evidence of it now a week
    # colder. Those wait for a person.
    if _halt_needs_a_person(state.get("halt_reason")):
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
    # THE RULE LIVES IN sizing_rule. This function's job is to resolve live's
    # account state and hand it over — the arithmetic is shared with the replay
    # engine so the two cannot drift (the sandbox's `max(1, qty // 4)` VIX layer
    # is what that drift looked like: 4x the intended risk in exactly the tape
    # the cut exists for).
    from . import runtime_config, sizing_rule

    # Adaptive sizing — follows the rolling-30-trade Sortino. Lazy import
    # avoids a hard dependency at module load time (and a circular if
    # adaptive_sizing ever needs to read settings/portfolio).
    try:
        from . import adaptive_sizing
        adaptive_mult, _adaptive_reason = adaptive_sizing.compute_multiplier()
    except Exception as e:
        log.debug("adaptive_sizing skipped: %s", e)
        adaptive_mult = 1.0

    size = sizing_rule.resolve(
        capital=sizing_capital(),
        risk_per_trade=runtime_config.risk_per_trade(),
        max_position_pct=runtime_config.max_position_pct(),
        price=signal.price, stop_loss=signal.stop_loss,
        vix=vix, conviction=conviction, regime_mult=regime_mult,
        # Account-level DD breaker x adaptive sizing. sizing_rule floors the
        # product so a deep drawdown cannot shrink positions below recovery size.
        state_mult=_dd_size_multiplier() * adaptive_mult,
    )
    if size.qty <= 0 and size.reason:
        # Used to be silent, and the weekly universe refresh kept re-selecting
        # names that could never be sized (2026-07-27: 5 of 15 watchlist tickers
        # were permanently unbuyable, still costing a kline fetch every scan).
        log.info("%s: %s — entry declined", signal.symbol, size.reason)
    return size.qty


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
            halt("daily drawdown",
                 f"{daily_loss_frac:.1%} of ${base:.0f} realized loss today "
                 f"≥ the {settings.daily_drawdown_stop:.0%} limit")
            return False, (f"daily drawdown {daily_loss_frac:.1%} of ${base:.0f} "
                           f"≥ {settings.daily_drawdown_stop:.0%}")

    return True, "ok"


# Halts that time does not answer. Every one of these describes a discrepancy
# between what this software believes and what the broker holds — a live order
# it cannot account for, a position it cannot explain — and none of them become
# untrue at midnight. They are cleared by a person who has looked at the
# account, through release_halt().
MANUAL_RELEASE_REASONS = (
    "protective order cancel failed",
    "unresolved orders after restart",
    "broker order query incomplete",
    "position reconciliation failed",
    # More sold than held. Whatever caused it — two bracket legs filling, a
    # trade placed by hand — the books and the account disagree about what is
    # owned, and no amount of waiting settles that.
    "oversold position",
    # A fill the broker made that this software could not book. The shares are
    # in the account and not in the ledger; tomorrow does not change that.
    "fill settlement failed",
    # A position created from a late fill whose order carried no protective
    # levels. It exists, it is real, and nothing is watching it.
    "late fill without protection",
    # Startup could not establish what the broker holds. Trading from an
    # unknown starting picture is how a duplicate position gets opened.
    "startup recovery failed",
)


def _halt_needs_a_person(reason: object) -> bool:
    return str(reason or "") in MANUAL_RELEASE_REASONS


def halt_status() -> dict:
    """Whether trading is halted, why, and whether time alone can clear it."""
    s = _load_state()
    reason = s.get("halt_reason")
    return {"halted": bool(s.get("halted")), "reason": reason,
            "detail": s.get("halt_detail"), "since": s.get("halt_started_at"),
            "needs_manual_release": _halt_needs_a_person(reason)}


def release_halt(who: str, note: str = "") -> dict:
    """Lift a halt deliberately. The only way to clear a manual-release one.

    Takes `who` because a halt of this kind is cleared by a person who has been
    to the broker and dealt with what caused it — and six weeks later the only
    question that matters about that decision is who made it.
    """
    before = halt_status()
    if not before["halted"]:
        return {"released": False, "note": "not halted"}
    db.atomic_state(lambda _s: {"halted": False, "halt_reason": None,
                                "halt_detail": None, "halt_started_at": None})
    log.warning("halt RELEASED by %s — was: %s (%s)%s",
                who, before["reason"], before["detail"],
                f" — {note}" if note else "")
    try:
        db.audit_insert("halt_released", reason=str(before["reason"] or ""),
                        extra={"by": who, "note": note[:300],
                               "was_detail": str(before["detail"] or "")[:400]})
    except Exception as e:
        log.warning("could not audit the halt release: %s", e)
    return {"released": True, "was": before}


def halt(reason: str, detail: str = "") -> None:
    """Stop trading, and say why. Idempotent; the FIRST reason is kept.

    The existing halt was a bare `{"halted": True}` written by the drawdown
    check, so an operator finding a halted bot could only guess which rule had
    fired. That mattered little while there was one rule. It stops being true
    the moment a protective-order failure can halt as well: "halted" then means
    either "you lost 6% today" or "there is a stop order at the broker that
    this software could not cancel and can no longer account for", and those
    call for opposite actions.

    The first reason wins because it is the one that describes the problem. A
    later cause is usually a consequence of the first.
    """
    from datetime import datetime as _dt, timezone as _tz
    stamp = _dt.now(_tz.utc).isoformat()

    def _apply(state: dict) -> dict:
        if state.get("halted") and state.get("halt_reason"):
            return {}
        return {"halted": True, "halt_reason": reason,
                "halt_detail": detail[:500], "halt_started_at": stamp}

    db.atomic_state(_apply)
    log.error("TRADING HALTED — %s%s", reason, f": {detail}" if detail else "")
    try:
        db.audit_insert("halt", reason=reason, extra={"detail": detail[:500]})
    except Exception as e:
        log.warning("could not audit the halt: %s", e)
    try:
        from . import notifier
        notifier.send(f"🛑 TRADING HALTED — {reason}\n{detail[:300]}")
    except Exception as e:
        log.warning("could not notify about the halt: %s", e)


def ledger_realized_pnl() -> float:
    """Total realized PnL as the CLOSED-TRADE LEDGER reports it, for this account.

    The authority for what was earned is the set of closed trades, not a
    running counter. `realized_pnl_total` is incremented by record_trade_close
    and never recomputed, so every way that increment can be missed or repeated
    is permanent: a close booked while the counter update raised (it is a
    separate write, and the settler logs and continues when it fails), a
    restore from a backup, a hand-edited row.

    That number is not cosmetic. equity = equity_baseline() + realized_pnl_total
    feeds peak_equity, which is the denominator of the drawdown breaker — so a
    counter that has drifted high makes the breaker fire early, and one that has
    drifted low makes it unable to fire at all.
    """
    acct = db._require_account_id("reading the realized-PnL ledger")
    with db.conn() as c:
        rows = c.execute("SELECT pnl, extra FROM closed_trades "
                         "WHERE account_id = ?", (acct,)).fetchall()
    # The EFFECTIVE ledger, not the raw table.
    #
    # Some rows are in closed_trades and are not trades: a synthetic TST record
    # and the second copy of an MRK close that was booked twice. They stay —
    # deleting evidence to make a number look better is how a ledger stops
    # being one — and db.is_excluded marks them so nothing reasoning about
    # performance counts them.
    #
    # Summing the raw table skipped that. On the authoritative database it gave
    # -709.585 against a counter of -602.555, and the 107.03 "drift" was
    # exactly those two rows: a test symbol at -100.00 and a duplicate at
    # -7.03. The counter was RIGHT. Rebuilding from the raw sum would have
    # written a wrong total and re-anchored peak_equity — the denominator of
    # the drawdown breaker — onto a test trade.
    return float(sum(float(r["pnl"] or 0.0) for r in rows
                     if not db.is_excluded(dict(r))))


def realized_pnl_drift() -> dict:
    """How far the counter has drifted from the ledger."""
    counter = float(db.get_state().get("realized_pnl_total") or 0.0)
    ledger = ledger_realized_pnl()
    return {"counter": counter, "ledger": ledger,
            "drift": round(counter - ledger, 6)}


def rebuild_realized_pnl(source: str, *, tolerance: float = 0.01) -> dict:
    """Recompute realized PnL from the ledger, and re-anchor the peak with it.

    Returns what it found and whether it changed anything. Audited, because a
    silent correction to the number the drawdown breaker measures against is
    indistinguishable from the drift it is correcting.

    The peak moves WITH the total, for the reason set_budget documents: leaving
    a peak recorded under a different PnL base either fabricates a drawdown or
    hides one.
    """
    d = realized_pnl_drift()
    if abs(d["drift"]) <= tolerance:
        return {**d, "rebuilt": False}

    def _apply(s: dict) -> dict:
        base = equity_baseline()
        return {"realized_pnl_total": d["ledger"],
                "peak_equity": compute_peak_equity(
                    base, d["ledger"], prior_peak=0.0)}

    db.atomic_state(_apply)
    log.warning("realized PnL rebuilt from the ledger by %s: counter was "
                "%.2f, ledger says %.2f (drift %.2f)",
                source, d["counter"], d["ledger"], d["drift"])
    try:
        db.audit_insert("pnl_rebuilt", reason="counter drifted from ledger",
                        extra={"by": source, **d})
    except Exception as e:
        log.warning("could not audit the PnL rebuild: %s", e)
    return {**d, "rebuilt": True}


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
        # Same expression set_budget uses — see compute_peak_equity. Keeping the
        # two in one place is the point: they used to disagree, and the
        # disagreement only showed up one trade after a budget change.
        peak = compute_peak_equity(base, new_total,
                                   prior_peak=current.get("peak_equity", 0.0))
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
