"""backtest_v3 — an independent, conservative re-implementation of the portfolio
backtest, built SIDE-BY-SIDE with `simulate_time_stepped` for cross-validation.

WHY A SECOND ENGINE
-------------------
A backtest number is only trustworthy if it survives an *independent*
re-derivation. `simulate_time_stepped` (src/backtest.py) reports e.g.
+$18,988 on $5,000 in 180 days (≈ +805 % CAGR). Before sizing live capital on
that, we want a second engine — written from scratch, NOT sharing the
execution/accounting code — to either reproduce it (→ trust) or break it
(→ we found the bias). The old engine is left fully intact; this file never
imports its loop, only its *strategy* primitives (see below).

WHAT IS SHARED vs RE-IMPLEMENTED
--------------------------------
SHARED — this is the *strategy*, not the *engine*. Re-deriving it would compare
two strategies instead of validating one engine, so we deliberately reuse it:
  • the prefetched cache (identical bars / VIX / daily / SPY)
  • the signal layer: indicators.evaluate, MR/momentum, MTF/gap/regime/ML gates
  • the risk-sizing *policy*: _position_size (risk_per_trade + max_position_pct)

RE-IMPLEMENTED INDEPENDENTLY — this is the *engine*, where backtest bias hides:
  • the event loop and per-bar exit resolution (SL / TP / MAX_HOLD / EOD,
    scale-out / breakeven / Chandelier trail)
  • the fill model
  • PnL, cash, and equity accounting

TWO HONESTY UPGRADES the incumbent lacks — each catches a specific inflation:
  1. REAL CASH ACCOUNTING (enforce_cash=True). `simulate_time_stepped` sizes
     every position off the *static* $5,000 seed and never checks whether the
     cash to buy the shares exists — so with max_position_pct=0.50 and the
     5-slot cap it can hold up to 5 × $2,500 = $12,500 of stock on a $5,000
     account (~2.5× implicit, cost-free leverage). V3 debits/credits a real
     cash balance and (when enforce_cash) refuses/clips buys it can't fund.
     The B-vs-C gap in engine_compare = the implicit-leverage premium baked
     into the headline.
  2. MARK-TO-MARKET DRAWDOWN. The incumbent measures DD off *realized* PnL
     only, so an open losing position is invisible to the DD curve until it
     closes — understating max-DD. V3 marks every open position to each bar's
     close, so `max_dd_mtm_pct` is the honest peak-to-trough an account
     actually lives through.

GATING PARITY. So that V3(cash off) lines up with the incumbent trade-for-trade,
the *entry gates* (threshold, MTF, gap, regime, ML, SL-cooldown, max-positions,
and the realized-DD halt/size-cut) are replicated exactly. Only the cash
constraint and the MTM-DD reporting are new. That makes the comparison clean:
  A  = simulate_time_stepped(cfg, cache)            # incumbent
  B  = simulate_v3(cfg, cache, enforce_cash=False)  # independent mechanics → ≈ A
  C  = simulate_v3(cfg, cache, enforce_cash=True)   # honest, deleveraged result

Run it via `scripts/engine_compare.py`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict
from datetime import date, time as dtime

import pandas as pd

# SHARED strategy primitives (intentionally — see module docstring).
from .backtest import (
    BacktestConfig,
    Trade,
    _BARS_PER_DAY,
    _max_hold_bars,
    _position_size,
    _warm_up,
)

log = logging.getLogger(__name__)

# Live kill_switch trade-phase windows (apply_trade_windows realism flag):
# new entries only 09:45–15:30 ET, and none Friday from 14:00.
_ENTRY_OPEN = dtime(9, 45)
_ENTRY_CLOSE = dtime(15, 30)
_FRI_CUTOFF = dtime(14, 0)


# ──────────────────────────────────────────────────────────────────────────
#  Independent realized-DD breaker (mirrors PortfolioState's halt logic, but
#  re-implemented here so V3 shares no accounting code with the incumbent).
# ──────────────────────────────────────────────────────────────────────────
class _RealizedDD:
    """Tracks realized-PnL equity, its high-water peak, and a halt timer with
    the same 7-day auto-release the live risk_manager / PortfolioState use."""

    HALT_AUTO_RELEASE_DAYS = 7.0

    def __init__(self, starting_capital: float) -> None:
        self.start = starting_capital
        self.realized = 0.0
        self.peak = starting_capital
        self.halt_started_at = None

    @property
    def equity(self) -> float:
        return self.start + self.realized

    @property
    def dd_pct(self) -> float:
        if self.peak <= 0:
            return 0.0
        return max(0.0, (self.peak - self.equity) / self.peak * 100)

    def record(self, pnl: float) -> None:
        self.realized += pnl
        self.peak = max(self.peak, self.equity)

    def is_halted(self, ts, dd_halt_pct: float) -> bool:
        if self.halt_started_at is not None:
            days = (ts - self.halt_started_at).total_seconds() / 86400
            if days >= self.HALT_AUTO_RELEASE_DAYS:
                self.peak = max(self.equity, 1.0)
                self.halt_started_at = None
                return False
            if self.dd_pct < dd_halt_pct:
                self.halt_started_at = None
                return False
            return True
        if self.dd_pct >= dd_halt_pct:
            self.halt_started_at = ts
            return True
        return False


# ──────────────────────────────────────────────────────────────────────────
#  Pyramiding / stacking mechanics (mirror the live open_position add-on).
#  Pulled out as PURE functions so the gate + merge math can be unit-tested in
#  isolation (scripts/test_pyramiding.py) and shared by the single call site in
#  the loop. Neither runs in parity_mode (use_pyramiding is forced off there),
#  so they can never perturb the differential parity check.
# ──────────────────────────────────────────────────────────────────────────
def _stack_gate(stacks, entry, stop, last_px, *, max_stacks, min_r) -> bool:
    """True iff a held position currently qualifies for a pyramid add-on:
    room under the per-symbol stack cap AND unrealised profit has reached
    `min_r` × the position's initial per-share risk (entry − stop). A
    non-positive R-unit (stop ≥ entry — e.g. already trailed up to/above the
    average entry) can never qualify, matching the live `_can_stack_onto`."""
    if stacks >= max_stacks:
        return False
    r_unit = entry - stop
    if r_unit <= 0:
        return False
    return (last_px - entry) / r_unit >= min_r


def _merge_addon(pos, s_entry, s_stop, s_tp, s_qty, s_atr) -> float:
    """Merge a pyramid add-on into `pos` IN PLACE, mirroring live open_position:
      • entry  → share-weighted average of old + add-on (rounded 2 dp)
      • stop   → max(old, add-on): trail UP only, never loosen
      • TP     → max(old, add-on): raise only
      • qty / qty_initial → new total (qty_initial re-anchored so the scale-out
        tranches re-base on the bigger position)
      • atr_at_entry → the add-on's ATR (re-anchors the R-unit; live sets
        atr = signal.atr on every fill)
      • stacks += 1; highest_high lifted to include the add-on entry
    Returns the cash to debit for the add-on (s_qty × s_entry); the caller
    subtracts it from the live balance so the cash wall still bites."""
    new_total = pos.qty + s_qty
    new_avg = (pos.qty * pos.entry_price + s_qty * s_entry) / new_total
    pos.entry_price = round(new_avg, 2)
    pos.stop_loss = max(pos.stop_loss, s_stop)
    pos.take_profit = max(pos.take_profit, s_tp)
    pos.qty = new_total
    pos.qty_initial = new_total
    pos.atr_at_entry = s_atr
    pos.stacks += 1
    pos.highest_high = max(pos.highest_high, round(s_entry, 2))
    return s_qty * s_entry


# ──────────────────────────────────────────────────────────────────────────
#  The engine
# ──────────────────────────────────────────────────────────────────────────
def simulate_v3(
    cfg: BacktestConfig,
    cache: dict,
    *,
    enforce_cash: bool = True,
    parity_mode: bool = False,
    rich_metrics: bool = False,
    progress_cb=None,
) -> dict:
    """Independent portfolio backtest. Same return shape as
    `simulate_time_stepped` (config / metrics / trades / errors / generated_at)
    plus extra V3-only fields under metrics: max_dd_mtm_pct, final_equity_mtm,
    ending_cash, peak_gross_exposure_usd, peak_gross_exposure_pct, n_skipped_cash.

    enforce_cash:
      False → notional sizing exactly like the incumbent (cash may go negative;
              this is the apples-to-apples mechanics check vs the active engine).
      True  → a buy is clipped to what the cash balance can fund, and skipped if
              that is zero shares. This is the realistic, deleveraged result.

    parity_mode:
      The named "mechanism-validation" configuration. Forces enforce_cash=False
      AND pyramiding off, so simulate_v3 reproduces the incumbent engine
      trade-for-trade and dollar-for-dollar (verified by scripts/engine_compare).
      Use this for the differential regression test; use enforce_cash=True
      (parity_mode off) for the honest strategy result. The two modes are the
      "two engines in one": same code, mechanism-check vs strategy-check.
    """
    from .indicators import (check_gap, daily_trend_bullish, evaluate,
                             relative_strength, sector_regime_bullish)
    from . import regime as regime_mod
    from .config import derive_max_positions, settings as _settings

    # parity_mode = the mechanism-validation lens: deleveraged accounting is
    # turned OFF (size like the incumbent) and pyramiding is suppressed, so the
    # run is byte-for-byte comparable to simulate_time_stepped.
    if parity_mode:
        enforce_cash = False
    # Pyramiding only ever runs in the strategy lens, never in parity_mode
    # (the incumbent has no stacking, so adding it would break the diff-test).
    use_pyramiding = bool(cfg.use_pyramiding) and not parity_mode
    # Live-fidelity knobs — ALL force-disabled in parity_mode so the A-vs-B
    # diff-test stays byte-exact. They only ever bite in the strategy lens.
    _vix_sizing = bool(cfg.apply_vix_sizing) and not parity_mode
    _earn_gate = bool(cfg.apply_earnings_gate) and not parity_mode
    _real_comm = bool(cfg.use_realistic_commission) and not parity_mode
    # Phase 3 entry gates — parity-suppressed like the other live-fidelity knobs
    # so the A-vs-B diff-test stays byte-exact; they only bite in the strategy lens.
    _rs_gate = bool(cfg.apply_rs_gate) and not parity_mode
    _sector_regime = bool(cfg.apply_sector_regime_gate) and not parity_mode
    # (ML conviction sizing removed 2026-06-03 — apply_ml_conviction_sizing stays
    # as an inert BacktestConfig flag; nothing reads it now.)
    # Fidelity fix 2026-06-03: live (main.py) ALWAYS scores trend + momentum_break
    # and takes the max; the honest engine previously only ran momentum when
    # apply_mr_strategy was on, so the production run (MR off) was trend-only and
    # did NOT represent the live bot. Decoupled: momentum has its own flag, set
    # True by _run_live_engine, parity-suppressed so the diff-test stays exact.
    _momentum = bool(cfg.apply_momentum_strategy) and not parity_mode
    # Pattern strategy (2026-06-22): gated on the SAME env flag as live
    # (settings.pattern_enabled), parity-suppressed so the A-vs-B diff stays
    # byte-exact. Algo-only here — the live vision layer is NOT modelled (it is
    # an additive confirmation, not a signal-set change; see plan parity note).
    _pattern = bool(_settings.pattern_enabled) and not parity_mode
    # Phase 0 realism knobs (2026-06-10) — parity-suppressed like every other
    # live-fidelity flag so the A-vs-B diff-test stays byte-exact.
    _scan_exits = bool(cfg.scan_grid_exits) and not parity_mode
    _ttl_entry = bool(cfg.entry_fill_open_only) and not parity_mode
    _no_same_day = bool(cfg.no_same_day_daily) and not parity_mode
    _reclamp = bool(cfg.reclamp_position_cap) and not parity_mode
    _windows = bool(cfg.apply_trade_windows) and not parity_mode
    # Phase 1 (2026-06-11): walk-forward dynamic universe — parity-suppressed.
    _dyn_universe = bool(cfg.apply_dynamic_universe) and not parity_mode

    if cfg.apply_mr_strategy or _momentum:
        from . import strategy_momentum, strategy_mr
    if _pattern:
        from . import strategy_pattern

    ml_pred = None   # ML subsystem removed 2026-06-03 (proven inert)

    spy_daily = cache["spy_daily"]
    soxx_daily = cache.get("soxx_daily")
    per_ticker = cache["per_ticker"]
    warm_up = _warm_up(cfg)
    max_hold = _max_hold_bars(cfg)
    lookback = warm_up + 20

    # Commission model. When _real_comm is off this returns the flat per-trade
    # value byte-for-byte (so parity is preserved); when on it charges the
    # broker per-ORDER schedule on each side: pct × notional + platform fee, plus
    # sell-side regulatory bps. (Run the realistic lens without scale-out so each
    # trade is one buy + one sell — partials would re-charge the buy fee.)
    flat_commission = cfg.commission_per_trade

    def _commission_for(qty: int, entry: float, exit_: float) -> float:
        if not _real_comm:
            return flat_commission
        buy_amt, sell_amt = qty * entry, qty * exit_
        buy = cfg.commission_pct_per_order * buy_amt + cfg.platform_fee_per_order
        sell = (cfg.commission_pct_per_order * sell_amt + cfg.platform_fee_per_order
                + cfg.sell_regulatory_bps / 10000.0 * sell_amt)
        return round(buy + sell, 2)

    # Historical earnings calendar (only when the gate is on). Best-effort; any
    # symbol that fails to fetch just never gets earnings-blocked (like live).
    earn_cal: dict[str, list] = {}
    if _earn_gate:
        from . import earnings as _earn
        from datetime import date as _date
        raw = _earn.load_earnings_calendar(list(per_ticker.keys()))
        earn_cal = {s: [_date.fromisoformat(d) for d in ds] for s, ds in raw.items()}

    # ---------- chronological event stream (independent build) ----------
    events: list[tuple] = []
    for sym, bundle in per_ticker.items():
        df = bundle["intraday"]
        for i in range(warm_up, len(df) - 1):
            events.append((df.index[i], sym, i))
    events.sort(key=lambda x: x[0])
    log.info("[v3] event stream: %d bars across %d tickers (enforce_cash=%s)",
             len(events), len(per_ticker), enforce_cash)

    # Walk-forward universe: one top-N set per ISO week, ranked at that week's
    # FIRST trading day from daily bars STRICTLY before it — replaying exactly
    # the decision the live Sunday refresh would have shipped into that week.
    active_by_week: dict = {}
    # ONE definition of the refresh period, used by both the builder and the
    # membership test below. Two hardcoded copies is how a weekly key ends up
    # querying a daily map and every entry silently gets gated out.
    from .config import settings as _cfg_settings
    _daily_mode = _cfg_settings.universe_refresh_freq == "daily"

    def _univ_key(d):
        return d if _daily_mode else d.isocalendar()[:2]

    if _dyn_universe:
        from .universe import Affordability, select_universe
        _daily_by_sym = {s: b.get("daily") for s, b in per_ticker.items()}
        # Same affordability rule the live Sunday refresh applies (2026-07-27) —
        # if the replay kept ranking names the live account can't size, every
        # validated $/day would come partly from trades live could never take.
        _afford = Affordability.from_cfg(cfg)
        # Refresh period must match live exactly: weekly keys on the ISO week,
        # daily keys on the date. Getting this wrong doesn't error — it quietly
        # scores a strategy nobody is running.
        # Hysteresis makes selection PATH-DEPENDENT (an incumbent keeps its slot
        # until it falls past exit_rank), so the replay has to carry its own
        # previous set forward in chronological order, exactly as live carries
        # config/watchlist.json. Events are already chronological; `_prev` is
        # what makes this a replay of the path rather than of isolated snapshots.
        _prev: list[str] = []
        for ts0, _, _ in events:
            key = _univ_key(ts0.date())
            if key not in active_by_week:
                picked = select_universe(
                    _daily_by_sym, asof=ts0.date(), top_n=cfg.universe_top_n,
                    afford=_afford, previous=_prev)
                active_by_week[key] = set(picked)
                _prev = picked
        log.info("[v3] dynamic universe: %d %s top-%d sets from a %d-name pool "
                 "(exit_rank=%s)", len(active_by_week),
                 "daily" if _daily_mode else "weekly", cfg.universe_top_n,
                 len(per_ticker), _cfg_settings.universe_exit_rank or "off")

    # ---------- account state ----------
    start_capital = cfg.account_usd
    cash = start_capital                    # real cash balance
    rdd = _RealizedDD(start_capital)        # realized-PnL DD breaker (gating)
    open_pos: dict[str, Trade] = {}
    closed: list[Trade] = []
    last_sl_at: dict[str, pd.Timestamp] = {}
    last_close: dict[str, float] = {}       # most-recent close per symbol (MTM)

    # MTM equity high-water tracking — the HONEST drawdown.
    peak_mtm = start_capital
    max_dd_mtm = 0.0
    peak_gross = 0.0          # peak Σ|position notional| in $
    n_skipped_cash = 0        # buys blocked/clipped to zero by the cash wall
    n_earnings_blocked = 0    # new entries blocked by the earnings gate
    n_rs_blocked = 0          # new entries blocked by the Phase 3-A RS gate
    n_sector_blocked = 0      # new entries blocked by the Phase 3-B sector-regime gate
    n_ml_halved = 0           # entries half-sized by ML conviction (Phase 4-2)
    n_lev_entries = 0         # entries that left cash negative (conviction margin)
    min_cash = start_capital  # deepest borrow point
    borrow_bar_sum = 0.0      # Σ max(0, −cash) per event — for interest estimate

    def slip_frac(price: float, atr: float) -> float:
        if price <= 0:
            return cfg.base_slip_bp / 10000.0
        atr_pct = atr / price
        return (cfg.base_slip_bp + cfg.atr_slip_k * (atr_pct * 100)) / 10000.0

    def book_close(t: Trade, exit_price: float, reason: str, exit_date: str,
                   ts) -> None:
        """Finalize a (full or partial) close: PnL, cash credit, DD record."""
        nonlocal cash
        t.exit_price = round(exit_price, 2)
        t.exit_date = exit_date
        t.exit_reason = reason
        comm = _commission_for(t.qty, t.entry_price, t.exit_price)
        t.pnl = round((t.exit_price - t.entry_price) * t.qty - comm, 2)
        t.pnl_pct = round((t.exit_price - t.entry_price) / t.entry_price * 100, 2)
        cash += t.qty * t.exit_price - comm
        rdd.record(t.pnl)
        closed.append(t)
        if reason == "SL":
            last_sl_at[t.symbol] = ts

    def _completed_daily(ddf, bar_ts):
        """Daily bars visible from an intraday bar. The daily row stamped on the
        SAME calendar day carries that day's FINAL close — information the live
        bot cannot have mid-session — so the realism lens excludes it and gates
        on completed days only (live uses the forming bar's partial close, which
        sits between the two; yesterday's close is the honest lower bound)."""
        if _no_same_day:
            return ddf.loc[ddf.index < bar_ts.normalize()]
        return ddf.loc[ddf.index <= bar_ts]

    def _consider_entry(sym, df, daily_df, bar, i, ts):
        """Shared signal → gates → size → cash funnel for BOTH a brand-new
        position (block B) AND a pyramid add-on (block A). Returns
        (sig, entry_price, stop_loss, take_profit, qty) when a fill should
        happen, else None. Mirrors the live funnel one-for-one.

        Caller-specific gates are NOT here: MAX_POSITIONS + SL-cooldown gate a
        *new* name (block B); stack-count + min-unrealised-R gate an *add-on*
        (block A). Keeping them in the callers lets this stay the single source
        of truth for the strategy/sizing/fill logic — which is exactly why the
        new-entry path still matches the incumbent to the dollar after the
        refactor (proven by engine_compare's parity assertion)."""
        nonlocal n_skipped_cash, n_earnings_blocked, n_rs_blocked, n_sector_blocked, n_ml_halved
        # Dynamic-universe gate first (cheapest): only this week's top-N may
        # open NEW positions. Held names that drop out keep being managed by
        # the exit logic — the gate covers entries (and pyramid add-ons) only.
        if _dyn_universe and sym not in active_by_week.get(_univ_key(ts.date()), ()):
            return None
        window = df.iloc[max(0, i + 1 - lookback): i + 1]
        try:
            sig = evaluate(sym, window)
            # Mirror the live funnel: trend + momentum_break always (when the
            # momentum flag is on); mean-revert only when separately enabled.
            cands = [sig]
            if _momentum:
                cands.append(strategy_momentum.evaluate(sym, window))
            if cfg.apply_mr_strategy:
                cands.append(strategy_mr.evaluate(sym, window))
            if _pattern:
                cands.append(strategy_pattern.evaluate(sym, window))
            if len(cands) > 1:
                sig = max(cands, key=lambda s: s.score)
        except Exception:
            return None
        if sig.score < cfg.threshold or sig.atr <= 0:
            return None

        # Trade-phase windows (live kill_switch): new entries only between
        # 09:45–15:30 ET, and none on Friday ≥ 14:00. Gate on the FILL bar's
        # timestamp (i+1) since that is when live would be placing the order.
        if _windows:
            fill_ts = df.index[min(i + 1, len(df) - 1)]
            ft = fill_ts.time()
            if ft < _ENTRY_OPEN or ft >= _ENTRY_CLOSE:
                return None
            if fill_ts.weekday() == 4 and ft >= _FRI_CUTOFF:
                return None

        # gates: MTF + gap
        if daily_df is not None and not daily_df.empty:
            d_until = _completed_daily(daily_df, df.index[i]).tail(100)
            if len(d_until) >= 2:
                if cfg.apply_mtf_gate and cfg.timeframe == "HOUR_1":
                    ok, _ = daily_trend_bullish(d_until)
                    if not ok:
                        return None
                if cfg.apply_gap_gate:
                    ok, _ = check_gap(d_until, max_gap_pct=cfg.max_gap_pct)
                    if not ok:
                        return None

        # regime gate / scaling input
        regime = None
        if (cfg.apply_regime_gate or cfg.use_regime_scaling) and \
                spy_daily is not None and not spy_daily.empty:
            s_until = _completed_daily(spy_daily, df.index[i]).tail(250)
            if len(s_until) >= 200:
                regime = regime_mod.assess(s_until)
                if cfg.apply_regime_gate and regime.block_new_entries:
                    return None

        # Phase 3-A: relative-strength gate — only let in names beating SPY over
        # the lookback. Uses DAILY closes for both (parity with live). Missing
        # bars ⇒ rs=0.0 which passes when rs_min_pct<=0 (graceful, like live).
        if _rs_gate and spy_daily is not None and not spy_daily.empty \
                and daily_df is not None and not daily_df.empty:
            st = _completed_daily(daily_df, df.index[i]).tail(cfg.rs_lookback_days + 5)
            sp = _completed_daily(spy_daily, df.index[i]).tail(cfg.rs_lookback_days + 5)
            rs, _ = relative_strength(st, sp, cfg.rs_lookback_days)
            if rs < cfg.rs_min_pct:
                n_rs_blocked += 1
                return None

        # Phase 3-B: fast sector-regime gate — pause new entries when SOXX EMA
        # fast <= slow (semiconductor roll, caught sooner than SPY-200MA).
        if _sector_regime and soxx_daily is not None and not soxx_daily.empty:
            sx = _completed_daily(soxx_daily, df.index[i]).tail(cfg.sector_regime_slow + 10)
            ok, _ = sector_regime_bullish(sx, cfg.sector_regime_fast, cfg.sector_regime_slow)
            if not ok:
                n_sector_blocked += 1
                return None

        # Earnings gate (live earnings.earnings_block): block a NEW entry when an
        # earnings report falls within `earnings_avoid_days` ahead of this bar.
        # Replays the live forward-only rule bar-by-bar off the historical
        # calendar. ETFs / fetch failures carry an empty list and so never block
        # (same graceful degradation as live). Flag-gated + parity-suppressed.
        if _earn_gate:
            bar_d = df.index[i].date()
            for e in earn_cal.get(sym.upper(), ()):
                if 0 <= (e - bar_d).days <= cfg.earnings_avoid_days:
                    n_earnings_blocked += 1
                    return None

        # DD breaker (realized — same signal the incumbent gates on)
        qty_mult = 1.0
        if cfg.apply_dd_breaker:
            if rdd.is_halted(ts, cfg.dd_halt_pct):
                return None
            if rdd.dd_pct >= cfg.dd_size_cut_pct:
                qty_mult = 0.5

        # regime-scaled sizing
        if cfg.use_regime_scaling and regime is not None and regime.bullish:
            try:
                vix_now = float(bar["vix"])
            except (KeyError, TypeError, ValueError):
                vix_now = 15.0
            if vix_now < cfg.regime_vix_calm:
                qty_mult *= cfg.regime_bull_mult

        # VIX risk-off sizing (live risk_manager.calc_position_size): halve the
        # share count when VIX>25, quarter it when VIX>35. Neither engine modeled
        # this, so backtests over-sized through fear regimes. Flag-gated +
        # parity-suppressed; falls back to a calm VIX=15 if the column is missing.
        if _vix_sizing:
            try:
                vix_rf = float(bar["vix"])
            except (KeyError, TypeError, ValueError):
                vix_rf = 15.0
            if vix_rf > 35:
                qty_mult *= 0.25
            elif vix_rf > 25:
                qty_mult *= 0.5

        # Continuous score-based conviction sizing (experiment, default off,
        # parity-suppressed). Binary threshold gives 72 and 95 the same size;
        # this scales size with how far the score clears the threshold.
        if cfg.use_score_sizing and not parity_mode:
            span = max(1e-9, 100.0 - cfg.threshold)
            frac = min(1.0, max(0.0, (sig.score - cfg.threshold) / span))
            qty_mult *= cfg.score_size_lo + (cfg.score_size_hi - cfg.score_size_lo) * frac

        # ---- fill model (independent copy) ----
        next_bar = df.iloc[i + 1]
        limit_price = float(sig.price)
        next_open = float(next_bar["open"])
        next_low = float(next_bar["low"])
        slip = slip_frac(sig.price, sig.atr)
        if cfg.realistic_limit_fills:
            if next_open <= limit_price:
                entry_price = next_open * (1 + slip)
            elif not _ttl_entry and next_low <= limit_price:
                # 60-min touch-fill window — only without the realism lens. Live
                # limits are cancelled after ~5 minutes, so a touch later in the
                # hour is a fill the live bot can never get.
                entry_price = limit_price * (1 + slip)
            else:
                return None
        else:
            entry_price = next_open * (1 + slip)
        if entry_price <= 0:
            return None

        stop_loss = round(entry_price - cfg.sl_atr_mult * sig.atr, 2)
        take_profit = round(entry_price + cfg.tp_atr_mult * sig.atr, 2)
        qty = _position_size(entry_price, stop_loss, cfg)
        qty = max(0, int(qty * qty_mult))
        if _reclamp and qty > 0:
            # Live applies regime_mult inside risk dollars and THEN takes
            # min(qty_by_risk, qty_by_cap) — the cap always wins. Multiplying
            # after _position_size let a 1.4× boost put up to 98% of the
            # account in one name; re-clamp restores the live invariant.
            qty = min(qty, int(cfg.account_usd * cfg.max_position_pct
                               / max(entry_price, 1e-9)))
        if qty == 0:
            return None

        # ---- THE cash wall (the honest constraint) ----
        if enforce_cash:
            # Conviction-gated margin experiment: a high-score entry may draw
            # cash down to −(mult−1)×capital instead of 0 (flag, default off).
            floor = 0.0
            if cfg.conviction_lev_score > 0 and sig.score >= cfg.conviction_lev_score:
                floor = -(cfg.conviction_lev_mult - 1.0) * start_capital
            affordable = int((cash - flat_commission - floor) / max(entry_price, 1e-9))
            if affordable < qty:
                if affordable <= 0:
                    n_skipped_cash += 1
                    return None
                qty = affordable
                n_skipped_cash += 1   # had to clip — count it as a cash-limited entry

        return sig, entry_price, stop_loss, take_profit, qty

    total = len(events)
    step = max(1, total // 50)
    _cap_ts, _cap_n = None, 0   # per-scan new-name counter (max_new_names_per_scan)

    for evt_idx, (ts, sym, i) in enumerate(events):
        if progress_cb is not None and evt_idx % step == 0:
            progress_cb(evt_idx, total, sym)

        df = per_ticker[sym]["intraday"]
        daily_df = per_ticker[sym]["daily"]
        bar = df.iloc[i]
        bar_date = str(df.index[i].date())
        o = float(bar["open"])
        hi = float(bar["high"])
        lo = float(bar["low"])
        cl = float(bar["close"])
        last_close[sym] = cl

        pos = open_pos.get(sym)

        # ================= (A) manage an open position =================
        if pos is not None:
            bars_held = i - pos.entry_bar
            atr0 = pos.atr_at_entry if pos.atr_at_entry > 0 else \
                (pos.entry_price - pos.stop_loss) / max(cfg.sl_atr_mult, 1e-9)
            exit_slip = (cfg.base_slip_bp + cfg.atr_slip_k
                         * (atr0 / max(pos.entry_price, 1e-9) * 100)) / 10000.0
            r_unit = cfg.sl_atr_mult * atr0
            if r_unit <= 0:
                r_unit = 1e-9

            # breakeven: ratchet stop to entry once +trigger_r booked
            if cfg.use_breakeven_stop and not pos.breakeven_set:
                if hi >= pos.entry_price + cfg.breakeven_trigger_r * r_unit:
                    pos.stop_loss = max(pos.stop_loss, pos.entry_price)
                    pos.breakeven_set = True

            # scale-out partials (each booked as its own closed Trade)
            if cfg.use_scale_out:
                tp1 = pos.entry_price + cfg.tp1_r * r_unit
                tp2 = pos.entry_price + cfg.tp2_r * r_unit
                if not pos.tp1_done and hi >= tp1:
                    pq = max(1, pos.qty_initial // 3)
                    if pq < pos.qty:
                        part = Trade(
                            symbol=sym, entry_bar=pos.entry_bar,
                            entry_date=pos.entry_date, entry_price=pos.entry_price,
                            stop_loss=pos.stop_loss, take_profit=tp1,
                            qty=pq, score=pos.score, qty_initial=pq)
                        book_close(part, tp1, "TP1", bar_date, ts)
                        pos.qty -= pq
                        pos.tp1_done = True
                if pos.tp1_done and not pos.tp2_done and hi >= tp2:
                    pq = max(1, pos.qty_initial // 3)
                    if pq < pos.qty:
                        part = Trade(
                            symbol=sym, entry_bar=pos.entry_bar,
                            entry_date=pos.entry_date, entry_price=pos.entry_price,
                            stop_loss=pos.stop_loss, take_profit=tp2,
                            qty=pq, score=pos.score, qty_initial=pq)
                        book_close(part, tp2, "TP2", bar_date, ts)
                        pos.qty -= pq
                        pos.tp2_done = True

            # ---- resolve the bar's exit (conservative SL-before-TP order) ----
            exit_reason = None
            exit_price = 0.0
            sl_trigger = None
            if _scan_exits:
                # Live stops are SOFT: checked against a snapshot at scan
                # boundaries and filled at market — never intrabar at the exact
                # level (measured live cost −1.38R vs the −1R touch model).
                # Hourly-bar equivalent: a gap-through open fills at the open,
                # otherwise a CLOSE beyond the stop fills at the close, paying
                # whatever overshoot accrued since the level broke. An intrabar
                # dip that recovers by the close is NOT an exit (live skips it
                # too). TP keeps touch-fill: the live 5-min manage tick books
                # last ≥ TP within minutes of a touch (slightly BETTER than the
                # exact level, so touch-fill here is the conservative side).
                if o <= pos.stop_loss:
                    sl_trigger = o
                elif cl <= pos.stop_loss:
                    sl_trigger = cl
            elif lo <= pos.stop_loss:
                sl_trigger = min(o, pos.stop_loss)
            if sl_trigger is not None:
                worsened = exit_slip * cfg.sl_breakaway_mult
                exit_price = sl_trigger * (1 - worsened)
                if pos.trail_active and pos.stop_loss > pos.entry_price:
                    exit_reason = "TRAIL"
                elif pos.breakeven_set and sl_trigger >= pos.entry_price:
                    exit_reason = "BREAKEVEN"
                else:
                    exit_reason = "SL"
            elif hi >= pos.take_profit:
                exit_price = pos.take_profit
                exit_reason = "TP"
            elif bars_held >= max_hold:
                exit_price = cl * (1 - exit_slip)
                exit_reason = "MAX_HOLD"

            if exit_reason is not None:
                book_close(pos, exit_price, exit_reason, bar_date, ts)
                open_pos.pop(sym, None)
            else:
                # Chandelier trail — updated AFTER exits (no intrabar look-ahead)
                if cfg.use_trailing_stop:
                    if hi > pos.highest_high:
                        pos.highest_high = hi
                    if not pos.trail_active and pos.highest_high >= \
                            pos.entry_price + cfg.trail_activate_r * r_unit:
                        pos.trail_active = True
                    if pos.trail_active:
                        ts_stop = pos.highest_high - cfg.trail_atr_mult * atr0
                        if ts_stop > pos.stop_loss:
                            pos.stop_loss = round(ts_stop, 2)

                # ---- pyramid add-on (mirrors live open_position stacking) ----
                # Live: a held name that RE-FIRES a qualifying buy while already
                # ≥ STACK_MIN_R_MULTIPLE in unrealised profit and with room under
                # MAX_STACKS_PER_SYMBOL gets an add-on merged in (weighted-avg
                # entry; stop/TP raised, never lowered). MAX_POSITIONS does not
                # gate a stack (same broker position). The cash wall still does,
                # which is the whole point on a real $5k account — adding to a
                # winner spends cash a brand-new name could have used. The add-on
                # "fills" next bar (via _consider_entry's i+1 fill model), same as
                # a fresh entry; we merge it here and let this bar's MTM reflect it.
                if use_pyramiding and _stack_gate(
                        pos.stacks, pos.entry_price, pos.stop_loss, cl,
                        max_stacks=_settings.max_stacks_per_symbol,
                        min_r=_settings.stack_min_r_multiple):
                    res = _consider_entry(sym, df, daily_df, bar, i, ts)
                    if res is not None:
                        s_sig, s_entry, s_stop, s_tp, s_qty = res
                        cash -= _merge_addon(pos, s_entry, s_stop, s_tp,
                                             s_qty, s_sig.atr)
            # mark-to-market AFTER managing, then move on (no new entry this bar)
            borrow_bar_sum += max(0.0, -cash)
            equity_mtm = cash + sum(p.qty * last_close.get(p.symbol, p.entry_price)
                                    for p in open_pos.values())
            peak_mtm = max(peak_mtm, equity_mtm)
            if peak_mtm > 0:
                max_dd_mtm = max(max_dd_mtm, (peak_mtm - equity_mtm) / peak_mtm * 100)
            gross = sum(p.qty * last_close.get(p.symbol, p.entry_price)
                        for p in open_pos.values())
            peak_gross = max(peak_gross, gross)
            continue

        # ================= (B) consider a new entry =================
        if cfg.apply_max_positions and len(open_pos) >= derive_max_positions(cfg.account_usd):
            continue

        # Per-scan new-name cap (live: max_new_names_per_scan=2). Events at the
        # same timestamp = candidates of the same scan; once the cap is hit,
        # the rest of that bar's candidates wait for the next scan, like live.
        if cfg.max_new_names_per_scan > 0 and not parity_mode:
            if ts != _cap_ts:
                _cap_ts, _cap_n = ts, 0
            if _cap_n >= cfg.max_new_names_per_scan:
                continue

        if cfg.sl_cooldown_hours > 0 and sym in last_sl_at:
            if (ts - last_sl_at[sym]).total_seconds() / 3600 < cfg.sl_cooldown_hours:
                continue

        res = _consider_entry(sym, df, daily_df, bar, i, ts)
        if res is None:
            continue
        sig, entry_price, stop_loss, take_profit, qty = res

        cost = qty * entry_price
        cash -= cost   # debit (may go negative when enforce_cash=False → implicit margin)
        if cfg.max_new_names_per_scan > 0 and not parity_mode:
            _cap_n += 1
        if enforce_cash and cash < -1e-6:
            n_lev_entries += 1
            min_cash = min(min_cash, cash)

        open_pos[sym] = Trade(
            symbol=sym, entry_bar=i + 1,
            entry_date=str(df.index[i + 1].date()),
            entry_price=round(entry_price, 2),
            stop_loss=stop_loss, take_profit=take_profit,
            qty=qty, qty_initial=qty, score=sig.score,
            atr_at_entry=sig.atr, highest_high=round(entry_price, 2),
        )

        # mark-to-market after the entry
        borrow_bar_sum += max(0.0, -cash)
        gross = sum(p.qty * last_close.get(p.symbol, p.entry_price)
                    for p in open_pos.values())
        peak_gross = max(peak_gross, gross)
        equity_mtm = cash + gross
        peak_mtm = max(peak_mtm, equity_mtm)
        if peak_mtm > 0:
            max_dd_mtm = max(max_dd_mtm, (peak_mtm - equity_mtm) / peak_mtm * 100)

    # ---------- close anything still open at the end ----------
    for sym, pos in list(open_pos.items()):
        df = per_ticker[sym]["intraday"]
        last = df.iloc[-1]
        atr0 = (pos.entry_price - pos.stop_loss) / max(cfg.sl_atr_mult, 1e-9)
        eod_slip = (cfg.base_slip_bp + cfg.atr_slip_k
                    * (atr0 / max(pos.entry_price, 1e-9) * 100)) / 10000.0
        book_close(pos, float(last["close"]) * (1 - eod_slip), "EOD",
                   str(df.index[-1].date()), df.index[-1])
        open_pos.pop(sym, None)

    metrics = _metrics_v3(closed, cfg, start_capital, cash,
                          max_dd_mtm, peak_gross, n_skipped_cash,
                          n_earnings_blocked, n_rs_blocked, n_sector_blocked,
                          n_ml_halved)
    if cfg.conviction_lev_score > 0:
        # Conviction-margin experiment telemetry. borrow_bar_sum accumulates
        # once per EVENT (bar × ticker) — normalize to dollar-DAYS so the
        # harness can price margin interest.
        metrics["n_leveraged_entries"] = n_lev_entries
        metrics["max_borrowed_usd"] = round(max(0.0, -min_cash), 2)
        metrics["borrowed_dollar_days"] = round(
            borrow_bar_sum / max(len(per_ticker), 1) / 7.0, 2)
    # rich_metrics: the production path (run_backtest → GUI panel + weekly Telegram
    # self-check) needs the full report shape the oracle emits — Sharpe/Sortino/
    # Calmar/MonteCarlo/by_symbol/monthly_pnl/avg_win_pct/max_drawdown_usd. We reuse
    # the oracle's compute_metrics over our OWN closed trades, then let V3's honest
    # keys win the merge (net_pnl, max_dd_mtm_pct, n_cash_limited_entries, …). The
    # diff-test scripts never pass rich_metrics, so _metrics_v3 stays the lean,
    # independent source that engine_compare validates byte-for-byte.
    if rich_metrics and closed:
        from .backtest import compute_metrics
        rich = compute_metrics(closed, cfg)
        rich.update(metrics)          # V3's authoritative keys override the oracle's
        metrics = rich
    log.info("[v3] done — %d trades, realized-DD=%.2f%%, MTM-DD=%.2f%%, "
             "final equity=$%.2f, ending cash=$%.2f",
             len(closed), rdd.dd_pct, max_dd_mtm, rdd.equity, cash)
    return {
        "config": {
            "days": cfg.days, "timeframe": cfg.timeframe,
            "threshold": cfg.threshold, "tickers": list(per_ticker.keys()),
            "account_usd": cfg.account_usd,
            "simulator": f"v3_cash{'_on' if enforce_cash else '_off'}"
                         f"{'_parity' if parity_mode else ''}"
                         f"{'_pyr' if use_pyramiding else ''}",
            "enforce_cash": enforce_cash,
            "parity_mode": parity_mode,
            "use_pyramiding": use_pyramiding,
        },
        "metrics": metrics,
        "trades": [asdict(t) for t in sorted(closed, key=lambda t: t.entry_date)],
        "errors": [],
        "generated_at": str(date.today()),
    }


# ──────────────────────────────────────────────────────────────────────────
#  Independent metrics — deliberately NOT compute_metrics(), so a metric bug in
#  one engine can't silently mask a divergence. Only the fields the side-by-side
#  needs are produced; Sharpe/Sortino/MonteCarlo are out of scope here.
# ──────────────────────────────────────────────────────────────────────────
def _metrics_v3(trades, cfg, start_capital, ending_cash,
                max_dd_mtm, peak_gross, n_skipped_cash,
                n_earnings_blocked=0, n_rs_blocked=0, n_sector_blocked=0,
                n_ml_halved=0) -> dict:
    if not trades:
        return {"total_trades": 0, "note": "no trades generated",
                "net_pnl_usd": 0.0, "max_drawdown_pct": 0.0,
                "max_dd_mtm_pct": 0.0}

    net = round(sum(t.pnl for t in trades), 2)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf")
    wr = round(len(wins) / len(trades) * 100, 1)

    # realized-DD curve (exit-ordered) — apples-to-apples with the incumbent.
    eq = start_capital
    peak = start_capital
    max_dd_real = 0.0
    for t in sorted(trades, key=lambda t: (t.exit_date, t.entry_date)):
        eq += t.pnl
        peak = max(peak, eq)
        if peak > 0:
            max_dd_real = max(max_dd_real, (peak - eq) / peak * 100)

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    # Per-POSITION win rate: scale-out partials are each booked as their own
    # Trade with a profitable exit by construction, which inflates the per-fill
    # win rate whenever scale-out/breakeven are active. Group fills by the
    # position they came from (symbol + entry bar) and judge the NET PnL.
    pos_pnl: dict[tuple, float] = {}
    for t in trades:
        key = (t.symbol, t.entry_bar, t.entry_date)
        pos_pnl[key] = pos_pnl.get(key, 0.0) + t.pnl
    n_pos = len(pos_pnl)
    wr_pos = round(sum(1 for p in pos_pnl.values() if p > 0) / n_pos * 100, 1) \
        if n_pos else 0.0

    final_equity = start_capital + net
    return {
        "total_trades": len(trades),
        "total_positions": n_pos,
        "win_rate_pct": wr,
        "win_rate_per_position_pct": wr_pos,
        "profit_factor": pf,
        "net_pnl_usd": net,
        "net_pnl_pct": round(net / start_capital * 100, 2),
        "daily_pnl_usd": round(net / max(cfg.days, 1), 2),
        "final_equity_usd": round(final_equity, 2),
        "ending_cash_usd": round(ending_cash, 2),
        "max_drawdown_pct": round(max_dd_real, 2),    # realized (matches incumbent)
        "max_dd_mtm_pct": round(max_dd_mtm, 2),       # honest mark-to-market DD
        "peak_gross_exposure_usd": round(peak_gross, 2),
        "peak_gross_exposure_pct": round(peak_gross / max(start_capital, 1e-9) * 100, 1),
        "n_cash_limited_entries": n_skipped_cash,
        "n_earnings_blocked": n_earnings_blocked,
        "n_rs_blocked": n_rs_blocked,
        "n_sector_regime_blocked": n_sector_blocked,
        "n_ml_conviction_halved": n_ml_halved,
        "exit_reasons": reasons,
    }
