"""Walk-forward backtester using the live 6-factor scoring pipeline.

Fetches historical K-lines from the broker, replays the exact same indicator
scoring used in production, simulates entries/exits, and emits a
performance report.

Usage:
    python -m src.backtest                          # watchlist, 180 days, current TF
    python -m src.backtest --days 90
    python -m src.backtest --tickers AAPL MSFT NVDA
    python -m src.backtest --timeframe HOUR_1
    python -m src.backtest --threshold 65           # lower entry bar to get more trades
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# Hard wall-clock cap on a single broker kline fetch. Without this, the SDK's
# request_history_kline can block forever when OpenD's socket goes silent —
# the whole prefetch loop hangs on whichever ticker tripped it.
#
# Budget: get_kline can internally sleep up to ~30s waiting on the 55-calls-
# per-30s rate limiter, plus the actual SDK round-trip (1-2s healthy, up to
# ~32s on the documented retry-after-rate-limit path). 60s leaves comfortable
# headroom for legitimate slow calls while still fast-failing a true hang.
_FETCH_TIMEOUT_SEC = 60.0

WATCHLIST_FILE = Path(__file__).parent.parent / "config" / "watchlist.json"
RESULTS_FILE = Path(__file__).parent.parent / "data" / "backtest_results.json"


# ---------- data classes ----------

@dataclass
class Trade:
    symbol: str
    entry_bar: int
    entry_date: str
    entry_price: float
    stop_loss: float
    take_profit: float
    qty: int
    score: float = 0.0
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""   # SL | TP | MAX_HOLD | EOD | TP1 | TP2 | TRAIL
    pnl: float = 0.0
    pnl_pct: float = 0.0
    # ── Scale-out + breakeven tracking (2026-05-30) ──
    # qty_initial preserves the original size so R-multiple math stays correct
    # even after partial closes.
    qty_initial: int = 0
    tp1_done: bool = False     # +2R partial close happened
    tp2_done: bool = False     # +4R partial close happened
    breakeven_set: bool = False  # stop raised to entry after +1R
    # ── Chandelier trailing-stop tracking (2026-05-30) ──
    # atr_at_entry is frozen at fill so trailing + slippage math stays correct
    # after the stop ratchets up (the old code back-derived ATR from the stop,
    # which breaks the moment the stop moves). highest_high is the peak since
    # entry that the Chandelier stop hangs off of.
    atr_at_entry: float = 0.0
    highest_high: float = 0.0
    trail_active: bool = False   # True once price cleared trail_activate_r × R
    # ── Pyramiding / stacking (2026-05-31) — mirrors live open_position ──
    # Number of entries merged into this position (1 = original lot, no add-ons).
    stacks: int = 1


@dataclass
class BacktestConfig:
    days: int = 180
    timeframe: str = "HOUR_1"
    threshold: float = 70.0
    tickers: list[str] = field(default_factory=list)
    data_source: str = "moo"        # "moo" (OpenD) | "yfinance" (independent ~730d 1h window)
    account_usd: float = 4500.0
    risk_per_trade: float = 0.02
    max_position_pct: float = 0.20
    max_hold_days: int = 10
    # ATR multiples — exposed for optimizer
    tp_atr_mult: float = 1.5            # take-profit = entry + N × ATR
    sl_atr_mult: float = 2.0            # stop-loss   = entry - N × ATR
    # Realism knobs
    base_slip_bp: float = 2.0           # baseline one-side slippage in bps
    atr_slip_k: float = 0.5             # +k × (atr_pct * 100) bps; ATR 2% → +1bp
    sl_breakaway_mult: float = 2.0      # on SL hit, slip × this (gap-thru exits hurt more)
    commission_per_trade: float = 1.0   # $1 round-trip approx
    realistic_limit_fills: bool = True  # if True: limit only fills if next bar low ≤ limit
    apply_mtf_gate: bool = True
    apply_gap_gate: bool = True
    max_gap_pct: float = 3.0
    # Live-funnel gates
    apply_regime_gate: bool = True  # need SPY > 200SMA at entry bar (BULL/NEUTRAL)
    # No apply_sector_gate / apply_ml_gate / apply_ml_conviction_sizing here.
    # All three were declared and never read by any engine. There is no ML in
    # this codebase — no model, no predict_proba, no veto, live or backtest —
    # and the sector cap was rejected on purpose: see src/concentration.py,
    # which uses correlation because the broker returns no sector field and
    # "a stale risk constraint is worse than none because it reads as
    # protection". A flag for a rejected design reads as protection too.
    # 2026-05-29: defaults to False because combo sweep proved MR was a net
    # drag on this watchlist ($23→$27/day when disabled). Flip on for chop-
    # heavy regimes if a regime-detection layer ever lands.
    apply_mr_strategy: bool = False  # was True until 2026-05-29
    # Live (main.py) ALWAYS scores trend + momentum_break and takes the max.
    # This flag lets the honest engine mirror that. Default False so the frozen
    # oracle + parity diff-test are unchanged; _run_live_engine sets it True.
    apply_momentum_strategy: bool = False
    # DD circuit breaker — exposed for backtest realism + the live risk_manager
    # uses the same knobs. Per the 142-day backtest (Nov 2025 –22% peak DD),
    # cutting size when DD breaches 10% materially softens the regime-change
    # disaster month.
    dd_size_cut_pct: float = 10.0   # DD ≥ this → halve qty
    dd_halt_pct: float = 15.0       # DD ≥ this → no new entries until recovered
    apply_dd_breaker: bool = True
    # Portfolio simulator (time-stepped) — when True, the simulator enforces
    # MAX_POSITIONS like the live system. Off by default so old runs stay
    # comparable; flip on for true live-parity backtests.
    apply_max_positions: bool = True
    # ── Scale-out + breakeven exit features (2026-05-30) ──
    # All default to False to keep old backtests comparable. Flip on per-run
    # to test if they lift PnL.
    use_breakeven_stop: bool = False    # raise stop to entry once +1R hit
    breakeven_trigger_r: float = 1.0    # R-multiple that triggers breakeven
    use_scale_out: bool = False         # close 1/3 at +2R, 1/3 at +4R, trail
    tp1_r: float = 2.0                  # first partial close at this R
    tp2_r: float = 4.0                  # second partial close at this R
    # ── Chandelier ATR trailing stop (2026-05-30) ──
    # The MAX_HOLD guillotine was force-closing 30-40% of trades, capping the
    # fat-tail winners this trend strategy lives on. A Chandelier exit trails
    # the stop at (highest_high_since_entry − trail_atr_mult × ATR), ratcheting
    # up only. It lets winners run while still protecting profit — the classic
    # LeBeau trend-following exit. Pair with a longer max_hold_days so the trail
    # (not the calendar) decides the exit.
    use_trailing_stop: bool = False
    trail_atr_mult: float = 3.0         # stop = peak − N × ATR(entry)
    trail_activate_r: float = 1.0       # only start trailing past +N × R (room early)
    # ── Regime-scaled sizing (2026-05-30) ──
    # Concentrate capital when the trend edge is strongest. In a confirmed
    # strong bull (SPY > 50MA > 200MA AND VIX < vix_calm), scale qty up; in a
    # weak/neutral tape, stay at 1.0×. Risk is already vol-normalised by the
    # ATR stop, so this is pure regime risk-on, not naive leverage.
    use_regime_scaling: bool = False
    regime_bull_mult: float = 1.35      # qty × this in confirmed strong bull
    regime_vix_calm: float = 20.0       # VIX below this = calm enough to lever
    # ── Live-fidelity knobs (2026-05-31) — close the backtest↔live gap ──
    # All default OFF so parity_mode + engine_compare stay byte-exact (the frozen
    # oracle never reads these; simulate_v3 force-disables them in parity_mode).
    # Turn them ON only in the realistic enforce_cash lens for a closest-to-live
    # read. None of them can perturb the A-vs-B diff-test.
    apply_vix_sizing: bool = False        # VIX>25 → ½ size, VIX>35 → ¼ (live risk_manager)
    apply_earnings_gate: bool = False     # block new entries within N days before earnings
    earnings_avoid_days: int = 2          # live EARNINGS_AVOID_DAYS
    # ── Phase 3-A: relative-strength entry gate (new alpha; default OFF) ──
    # Block a new entry unless the name's rs_lookback_days DAILY return beats
    # SPY's by ≥ rs_min_pct (percentage points). Only outperformers get in.
    apply_rs_gate: bool = False
    rs_lookback_days: int = 20            # ≈ 1 trading month — classic RS window
    rs_min_pct: float = 0.0               # stock_ret − spy_ret must be ≥ this (pp)
    # ── Phase 3-B: fast sector-regime gate (experiment; default OFF) ──
    # Pause new entries when the sector ETF's EMA(fast) <= EMA(slow). Catches a
    # semiconductor roll sooner than the slow SPY-200MA market-regime gate.
    apply_sector_regime_gate: bool = False
    sector_regime_fast: int = 20
    sector_regime_slow: int = 50
    # ── Phase 0 realism knobs (2026-06-10) — close the exit/fill/lookahead gaps
    # the live-parity audit measured (live SL averaged −1.38R vs the modeled −1R;
    # entry limits live ~5 min not a full hour; daily gates read the same-day
    # final close). All default OFF so parity_mode + engine_compare + every old
    # run stay byte-exact; _run_live_engine flips them ON so each user-facing
    # number pays the same frictions the live bot does.
    scan_grid_exits: bool = False       # stops are SOFT live: gap-open or bar-close fill, never intrabar touch
    entry_fill_open_only: bool = False  # fill only at next-bar open ≤ limit (≈ 5-min TTL, not 60-min window)
    no_same_day_daily: bool = False     # daily-bar gates see COMPLETED days only (no same-day-close lookahead)
    reclamp_position_cap: bool = False  # re-apply max_position_pct AFTER qty multipliers (regime mult broke it)
    apply_trade_windows: bool = False   # entries only 09:45–15:30 ET, none Friday ≥ 14:00 (live kill_switch)
    # ── Phase 1 (2026-06-11): walk-forward dynamic universe ──
    # When on (and cfg.tickers = the liquidity pool), new entries are gated on
    # membership in the week's top-N by 6-1 momentum, recomputed each week from
    # daily bars STRICTLY before that week — the same decision the live weekly
    # refresh makes. Kills the pinned-watchlist survivorship bias.
    apply_dynamic_universe: bool = False
    universe_top_n: int = 15
    # Live caps how many NEW names may open per scan (config
    # max_new_names_per_scan=2); the engine never modeled it (found 2026-06-11).
    # 0 = uncapped (old behaviour); _run_live_engine passes the live value.
    max_new_names_per_scan: int = 0
    # ── EXPERIMENT (2026-06-12): conviction-gated margin ──
    # When conviction_lev_score > 0, an entry whose rule score is at/above it
    # may breach the cash wall: cash may go negative down to
    # −(conviction_lev_mult − 1) × start_capital (i.e. 1.5 → borrow up to 50%
    # of the account). Requires a REAL margin account to ever go live; the
    # engine does NOT charge margin interest — the harness estimates it from
    # the emitted borrowed dollar-days. Default 0 = off, parity untouched.
    conviction_lev_score: float = 0.0
    conviction_lev_mult: float = 1.5
    # ── EXPERIMENT (2026-06-25): continuous score-based conviction sizing ──
    # Replaces the binary "full size once score ≥ threshold" with a size that
    # scales with how far the score clears the threshold: score_size_lo× at
    # score==threshold → score_size_hi× at score==100. Stacks into qty_mult like
    # the regime/VIX multipliers, so the 40% position cap still re-clamps the top
    # end. Default off + parity-suppressed → the honest baseline is unchanged.
    use_score_sizing: bool = False
    score_size_lo: float = 0.6     # multiplier at score == threshold (marginal signal)
    score_size_hi: float = 1.3     # multiplier at score == 100 (max conviction)
    use_realistic_commission: bool = False  # broker per-order fees instead of flat $1
    commission_pct_per_order: float = 0.0003  # broker MY US: 0.03% × notional / order / side
    platform_fee_per_order: float = 0.99      # broker MY US: $0.99 / order / side
    sell_regulatory_bps: float = 0.5          # SEC+TAF+settlement ≈ bps of sell notional (sell only)
    # SL cooldown — block re-entry on a name we just stopped out of.
    # Defaults match the live risk_manager.SL_COOLDOWN_HOURS so backtest ≈ live.
    # Set to 0 to disable (matches pre-fix behaviour).
    sl_cooldown_hours: int = 24
    # ── Pyramiding / stacking (2026-05-31) ──
    # Model the LIVE open_position add-on behaviour inside the cash engine: when a
    # held name re-fires a qualifying buy AND it is already ≥ STACK_MIN_R_MULTIPLE
    # in unrealised profit AND it has < MAX_STACKS_PER_SYMBOL stacks, merge an
    # add-on (weighted-avg entry; stop/TP trail UP only). The stack gates
    # (max_stacks_per_symbol, stack_min_r_multiple) are read from `settings` at
    # run time — exactly like max_positions — so backtest ≈ live without extra
    # plumbing. OFF by default → parity with the incumbent (which never stacks)
    # is preserved exactly; only simulate_v3(use_pyramiding=True) adds add-ons.
    use_pyramiding: bool = False

    def __post_init__(self) -> None:
        # DAILY trading mode was removed 2026-06-07 (HOUR_1 won the head-to-head).
        # Coerce any legacy/explicit "DAILY" to HOUR_1 so a stray value can't
        # produce a half-converted run (hourly bars but the string-keyed MTF/gap
        # branches skipped → nonsense metrics). Belt-and-suspenders with config.py.
        if str(self.timeframe).upper() == "DAILY":
            self.timeframe = "HOUR_1"


# ---------- shared portfolio state (DD breaker) ----------

@dataclass
class PortfolioState:
    """Shared across all tickers in a backtest run.

    Lets the DD circuit breaker measure account-level drawdown — same as the
    live `risk_manager.current_drawdown_pct()` — instead of per-ticker pretend-
    DD which over-fires when one ticker happens to lose money even if the
    whole portfolio is up.

    Caveat: the backtester still iterates per-ticker sequentially, so when
    ticker N's loop is mid-window, only tickers 1..N-1's complete trade
    history has been booked. This is an approximation — a true time-stepped
    portfolio simulator would interleave bars across tickers — but it's
    materially closer to live behaviour than the per-ticker version.
    """
    starting_capital: float
    realized_pnl: float = 0.0
    peak_equity: float = 0.0
    # Halt timer — when DD ≥ dd_halt_pct, record the bar timestamp. After
    # `halt_auto_release_days` we forget the peak and let DD reset to 0, so a
    # stuck halt never blocks the simulator (or the live bot) forever.
    # See `is_halted()` for the release logic.
    halt_started_at: object = None     # pd.Timestamp | None

    HALT_AUTO_RELEASE_DAYS: float = 7.0

    def __post_init__(self) -> None:
        # Peak starts at the seed capital — DD is always measured from a
        # high-water mark ≥ starting balance.
        self.peak_equity = max(self.peak_equity, self.starting_capital)

    @property
    def equity(self) -> float:
        return self.starting_capital + self.realized_pnl

    @property
    def dd_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity * 100)

    def record(self, pnl: float) -> None:
        self.realized_pnl += pnl
        self.peak_equity = max(self.peak_equity, self.equity)

    def is_halted(self, current_ts, dd_halt_pct: float) -> bool:
        """Return True if new entries should be refused right now.

        Auto-release: once a halt has been active for `HALT_AUTO_RELEASE_DAYS`,
        we reset peak_equity to the CURRENT equity. This forgets the
        underwater mark so DD drops to 0 — letting trading resume. Other risk
        guards (size_cut at 10% DD, adaptive sizing by Sortino, loss-streak)
        keep size reasonable; the DD halt is a hard stop, not a soft brake.

        Without this fix the 360-day backtest sat halted for 9 months after
        one bad month locked the peak above current equity permanently.
        """
        # Already halted — check the timer.
        if self.halt_started_at is not None:
            days_halted = (current_ts - self.halt_started_at).total_seconds() / 86400
            if days_halted >= self.HALT_AUTO_RELEASE_DAYS:
                # Force-release: peak = current equity so DD = 0.
                self.peak_equity = max(self.equity, 1.0)
                self.halt_started_at = None
                return False
            # If DD naturally recovered below the halt line, release early.
            if self.dd_pct < dd_halt_pct:
                self.halt_started_at = None
                return False
            return True
        # Not halted — check if we should be.
        if self.dd_pct >= dd_halt_pct:
            self.halt_started_at = current_ts
            return True
        return False


# ---------- helpers ----------

def _load_watchlist() -> list[str]:
    return json.loads(WATCHLIST_FILE.read_text())["tickers"]


def _position_size(entry: float, stop: float, cfg: BacktestConfig) -> int:
    dist = entry - stop
    if dist <= 0:
        return 0
    by_risk = int(cfg.account_usd * cfg.risk_per_trade / dist)
    by_cap = int(cfg.account_usd * cfg.max_position_pct / entry)
    return max(0, min(by_risk, by_cap))


# Bars-per-trading-day by timeframe — used for sizing data fetches and
# converting max-hold days to bars.
_BARS_PER_DAY = {
    "DAILY":  1,
    "HOUR_1": 7,    # 6.5 trading hours, rounded up
    "MIN_30": 13,
    "MIN_10": 39,
}

# Warm-up bars per timeframe — enough to seed all indicators (EMA50, ADX14,
# BB20, ATR14). Intraday frames need a deeper warm-up because the EMA/ADX
# horizons are shorter but the noise is higher.
_WARM_UP_BARS = {
    "DAILY":  70,
    "HOUR_1": 80,
    "MIN_30": 100,
    "MIN_10": 120,
}


def _max_hold_bars(cfg: BacktestConfig) -> int:
    """Convert max_hold_days to bars based on timeframe."""
    return cfg.max_hold_days * _BARS_PER_DAY.get(cfg.timeframe.upper(), 1)


def _bars_needed(cfg: BacktestConfig) -> int:
    """Total bars to fetch: backtest window + warm-up buffer."""
    bpd = _BARS_PER_DAY.get(cfg.timeframe.upper(), 1)
    test_bars = int(cfg.days * bpd) + max(20, bpd)
    return test_bars + _warm_up(cfg)


def _warm_up(cfg: BacktestConfig) -> int:
    return _WARM_UP_BARS.get(cfg.timeframe.upper(), 70)


# ---------- time-stepped portfolio simulator (live-parity) ----------

def simulate_time_stepped(cfg: BacktestConfig, cache: dict, progress_cb=None) -> dict:
    """Portfolio-level backtest — processes all tickers' bars by timestamp.

    ⚠ FROZEN TEST-ONLY ORACLE. This is the optimistic, implicit-leverage
    reference engine. It is deliberately NOT what the user reads: every
    user-facing report goes through `_run_live_engine` → the honest V3 simulator.
    This stays untouched on purpose so it can serve two jobs: (1) the fixed
    baseline that `scripts/engine_compare.py` diffs V3(parity_mode) against to
    prove V3's mechanics are bug-for-bug correct, and (2) the objective the
    Optuna optimizer scores through `simulate_with_cache`. Do not "fix" its
    leverage assumptions here — that would defeat the differential test. Tune
    realism in V3 (src/backtest_v3.py) instead.

    Unlike `backtest_ticker` (per-ticker sequential), this iterates a single
    chronologically-sorted event stream across the entire watchlist. The
    `PortfolioState`, open-trades dict, and `MAX_POSITIONS` cap all behave
    the way the live `risk_manager` sees them — so the DD breaker and the
    concurrent-position limit actually fire in the right moments. It does NOT
    model the live concentration gate (src/concentration.py: per-symbol, gross
    and correlated-cluster caps); at MAX_POSITION_PCT=0.10 x MAX_POSITIONS=5
    those cap at 50% gross and none of the three thresholds (50/100/60%) can
    bind, but raise max_position_pct and this engine stops matching live.

    Returns the same dict shape as `simulate_with_cache` for drop-in use.
    """
    from .indicators import check_gap, daily_trend_bullish, evaluate
    from . import regime as regime_mod
    from .config import derive_max_positions, settings as _settings
    if cfg.apply_mr_strategy:
        from . import strategy_momentum, strategy_mr
    ml_pred = None   # ML subsystem removed 2026-06-03 (proven inert)

    tf = cache["tf"]
    spy_daily = cache["spy_daily"]
    per_ticker = cache["per_ticker"]
    warm_up = _warm_up(cfg)
    max_hold = _max_hold_bars(cfg)
    commission = cfg.commission_per_trade
    _lookback = warm_up + 20   # fixed-size window — all TA indicators fit within warm_up bars

    # ---------- build the chronological event stream ----------
    events: list[tuple] = []
    for sym, bundle in per_ticker.items():
        df = bundle["intraday"]
        for i in range(warm_up, len(df) - 1):
            events.append((df.index[i], sym, i))
    events.sort(key=lambda x: x[0])
    log.info("[time-step] event stream: %d bars across %d tickers",
             len(events), len(per_ticker))

    # ---------- portfolio & open positions ----------
    portfolio = PortfolioState(starting_capital=cfg.account_usd)
    open_trades: dict[str, Trade] = {}
    closed_trades: list[Trade] = []
    # Per-symbol last SL time — used to enforce SL cooldown identically to live.
    last_sl_at: dict[str, pd.Timestamp] = {}

    # Cache spy/regime lookups & daily-by-date lookups to avoid recomputing.
    daily_lookup: dict[str, dict] = {}
    for sym, bundle in per_ticker.items():
        d = bundle.get("daily")
        if d is not None and not d.empty:
            daily_lookup[sym] = {str(ix.date()): row for ix, row in d.iterrows()}
        else:
            daily_lookup[sym] = {}

    # ---------- helpers (mirror backtest_ticker logic exactly) ----------
    def _slip_for(sig_obj) -> float:
        if sig_obj.price <= 0:
            return cfg.base_slip_bp / 10000.0
        atr_pct = sig_obj.atr / sig_obj.price
        return (cfg.base_slip_bp + cfg.atr_slip_k * (atr_pct * 100)) / 10000.0

    def _finalise(t: Trade) -> None:
        t.pnl = round((t.exit_price - t.entry_price) * t.qty - commission, 2)
        t.pnl_pct = round(
            (t.exit_price - t.entry_price) / t.entry_price * 100, 2
        )

    def _close(active: Trade, sym: str, exit_ts: pd.Timestamp) -> None:
        _finalise(active)
        closed_trades.append(active)
        portfolio.record(active.pnl)
        open_trades.pop(sym, None)
        # Stamp the SL time so the cooldown gate can refuse re-entry.
        if active.exit_reason == "SL":
            last_sl_at[sym] = exit_ts

    # ---------- main loop ----------
    # Progress: fire every ~2% of events so the GUI shows movement during the
    # simulation phase (not just the prefetch phase). Without this the label
    # is frozen at "Fetching <last ticker>" for the entire simulation, which
    # looks identical to a hang.
    total_events = len(events)
    progress_step = max(1, total_events // 50)
    for evt_idx, (ts, sym, i) in enumerate(events):
        if progress_cb is not None and evt_idx % progress_step == 0:
            progress_cb(evt_idx, total_events, sym)
        df = per_ticker[sym]["intraday"]
        daily_df = per_ticker[sym]["daily"]
        bar = df.iloc[i]
        bar_date = str(df.index[i].date())
        hi = float(bar["high"])
        lo = float(bar["low"])

        # --- (A) manage an open position on this symbol ---
        active = open_trades.get(sym)
        if active is not None:
            bars_held = i - active.entry_bar
            # ATR frozen at entry — trailing/breakeven move the stop, so we can
            # no longer back-derive ATR from (entry − stop) without corrupting
            # both the slippage estimate and the R-unit. Fall back to the old
            # derivation only for legacy trades that never stored atr_at_entry.
            atr_at_entry = active.atr_at_entry if active.atr_at_entry > 0 else \
                (active.entry_price - active.stop_loss) / max(cfg.sl_atr_mult, 1e-9)
            atr_est = atr_at_entry
            exit_slip = (cfg.base_slip_bp + cfg.atr_slip_k
                         * (atr_est / max(active.entry_price, 1e-9) * 100)) / 10000.0

            # R-unit = original entry risk. Must stay fixed even after the stop
            # ratchets up, else breakeven/scale-out R-targets collapse toward 0.
            r_unit = cfg.sl_atr_mult * atr_at_entry
            if r_unit <= 0:
                r_unit = 1e-9

            # ── Breakeven stop: once price has touched entry + breakeven_trigger_r × r,
            # raise the stop to entry (book a no-loss outcome on remaining qty).
            if cfg.use_breakeven_stop and not active.breakeven_set:
                trigger_price = active.entry_price + cfg.breakeven_trigger_r * r_unit
                if hi >= trigger_price:
                    active.stop_loss = max(active.stop_loss, active.entry_price)
                    active.breakeven_set = True

            # NB: the Chandelier trailing stop is updated AFTER the exit checks
            # below — so the stop protecting THIS bar only reflects highs through
            # the PRIOR bar (no intrabar look-ahead). See the post-exit block.

            # ── Scale-out: partial closes at +tp1_r and +tp2_r ──
            # tp1: close 1/3 at +2R; tp2: close another 1/3 at +4R; remaining
            # 1/3 trails via a tightened breakeven (already set above).
            if cfg.use_scale_out:
                tp1_price = active.entry_price + cfg.tp1_r * r_unit
                tp2_price = active.entry_price + cfg.tp2_r * r_unit
                # tp1 — record the partial as its own closed Trade so metrics see it.
                if not active.tp1_done and hi >= tp1_price:
                    partial_qty = max(1, active.qty_initial // 3)
                    if partial_qty < active.qty:
                        partial = Trade(
                            symbol=sym, entry_bar=active.entry_bar,
                            entry_date=active.entry_date,
                            entry_price=active.entry_price,
                            stop_loss=active.stop_loss,
                            take_profit=tp1_price,
                            qty=partial_qty, score=active.score,
                            qty_initial=partial_qty,
                        )
                        partial.exit_price = round(tp1_price, 2)
                        partial.exit_date = bar_date
                        partial.exit_reason = "TP1"
                        _finalise(partial)
                        closed_trades.append(partial)
                        portfolio.record(partial.pnl)
                        active.qty -= partial_qty
                        active.tp1_done = True
                # tp2 — close another 1/3.
                if active.tp1_done and not active.tp2_done and hi >= tp2_price:
                    partial_qty = max(1, active.qty_initial // 3)
                    if partial_qty < active.qty:
                        partial = Trade(
                            symbol=sym, entry_bar=active.entry_bar,
                            entry_date=active.entry_date,
                            entry_price=active.entry_price,
                            stop_loss=active.stop_loss,
                            take_profit=tp2_price,
                            qty=partial_qty, score=active.score,
                            qty_initial=partial_qty,
                        )
                        partial.exit_price = round(tp2_price, 2)
                        partial.exit_date = bar_date
                        partial.exit_reason = "TP2"
                        _finalise(partial)
                        closed_trades.append(partial)
                        portfolio.record(partial.pnl)
                        active.qty -= partial_qty
                        active.tp2_done = True

            if lo <= active.stop_loss:
                open_price = float(bar["open"])
                trigger = min(open_price, active.stop_loss)
                worsened = exit_slip * cfg.sl_breakaway_mult
                active.exit_price = round(trigger * (1 - worsened), 2)
                active.exit_date = bar_date
                # Distinguish a profit-taking trailing/breakeven exit from a real
                # SL so metrics aren't misleading. TRAIL = stop ratcheted above
                # entry by the Chandelier trail; BREAKEVEN = parked at entry.
                if active.trail_active and active.stop_loss > active.entry_price:
                    active.exit_reason = "TRAIL"
                elif active.breakeven_set and trigger >= active.entry_price:
                    active.exit_reason = "BREAKEVEN"
                else:
                    active.exit_reason = "SL"
            elif hi >= active.take_profit:
                active.exit_price = round(active.take_profit, 2)
                active.exit_date = bar_date
                active.exit_reason = "TP"
            elif bars_held >= max_hold:
                active.exit_price = round(float(bar["close"]) * (1 - exit_slip), 2)
                active.exit_date = bar_date
                active.exit_reason = "MAX_HOLD"

            if active.exit_reason:
                _close(active, sym, ts)
            else:
                # ── Chandelier ATR trailing stop — updated only AFTER this bar's
                # exit checks, so it never uses a high the same bar could have
                # stopped out on. Hangs the stop off the highest high SINCE entry
                # (incl. this bar) and ratchets up only; the new level protects
                # the NEXT bar. Arms once price clears +trail_activate_r × R so
                # the trade has room early. This is what lets fat-tail winners
                # run past the MAX_HOLD guillotine.
                if cfg.use_trailing_stop:
                    if hi > active.highest_high:
                        active.highest_high = hi
                    if not active.trail_active and active.highest_high >= \
                            active.entry_price + cfg.trail_activate_r * r_unit:
                        active.trail_active = True
                    if active.trail_active:
                        trail_stop = active.highest_high - cfg.trail_atr_mult * atr_at_entry
                        if trail_stop > active.stop_loss:
                            active.stop_loss = round(trail_stop, 2)
            continue   # already has (or had) position on this bar — no new entry

        # --- (B) try to open a new position ---
        # Portfolio max_positions cap (true portfolio-level — what live enforces).
        if cfg.apply_max_positions and len(open_trades) >= derive_max_positions(cfg.account_usd):
            continue

        # SL cooldown — refuse re-entry on a name we just stopped out of.
        # Mirrors `risk_manager.in_sl_cooldown` in live.
        if cfg.sl_cooldown_hours > 0 and sym in last_sl_at:
            elapsed_hours = (ts - last_sl_at[sym]).total_seconds() / 3600
            if elapsed_hours < cfg.sl_cooldown_hours:
                continue

        # --- score the bar (trend + optional MR) ---
        window = df.iloc[max(0, i + 1 - _lookback): i + 1]
        try:
            sig_trend = evaluate(sym, window)
            if cfg.apply_mr_strategy:
                sig_mr = strategy_mr.evaluate(sym, window)
                sig_mom = strategy_momentum.evaluate(sym, window)
                # Pick the highest-scoring of the three.
                candidates = [sig_trend, sig_mr, sig_mom]
                sig = max(candidates, key=lambda s: s.score)
            else:
                sig = sig_trend
        except Exception:
            continue
        if sig.score < cfg.threshold or sig.atr <= 0:
            continue

        # --- gates: MTF + gap + regime + ML ---
        if daily_df is not None and not daily_df.empty:
            d_until = daily_df.loc[daily_df.index <= df.index[i]].tail(100)
            if len(d_until) >= 2:
                if cfg.apply_mtf_gate and cfg.timeframe == "HOUR_1":
                    ok, _ = daily_trend_bullish(d_until)
                    if not ok:
                        continue
                if cfg.apply_gap_gate:
                    ok, _ = check_gap(d_until, max_gap_pct=cfg.max_gap_pct)
                    if not ok:
                        continue

        regime = None
        if (cfg.apply_regime_gate or cfg.use_regime_scaling) and \
                spy_daily is not None and not spy_daily.empty:
            s_until = spy_daily.loc[spy_daily.index <= df.index[i]].tail(250)
            if len(s_until) >= 200:
                regime = regime_mod.assess(s_until)
                if cfg.apply_regime_gate and regime.block_new_entries:
                    continue

        # --- DD circuit breaker (TRUE portfolio-level now that we're chronological) ---
        # is_halted() also handles the 7-day auto-release so a stuck halt
        # doesn't lock the simulator for months.
        qty_mult = 1.0
        if cfg.apply_dd_breaker:
            if portfolio.is_halted(ts, cfg.dd_halt_pct):
                continue   # halt active — refuse new entries this bar
            if portfolio.dd_pct >= cfg.dd_size_cut_pct:
                qty_mult = 0.5

        # --- regime-scaled sizing: lever up in a confirmed strong bull (SPY >
        # 50MA > 200MA) when VIX is calm. Stacks on top of the DD cut, so a
        # strong-bull-but-drawn-down state still de-risks first. ---
        if cfg.use_regime_scaling and regime is not None and regime.bullish:
            try:
                vix_now = float(bar["vix"])
            except (KeyError, TypeError, ValueError):
                vix_now = 15.0
            if vix_now < cfg.regime_vix_calm:
                qty_mult *= cfg.regime_bull_mult

        # --- realistic limit-buy fill ---
        next_bar = df.iloc[i + 1]
        limit_price = float(sig.price)
        next_open = float(next_bar["open"])
        next_low = float(next_bar["low"])
        slip = _slip_for(sig)

        if cfg.realistic_limit_fills:
            if next_open <= limit_price:
                entry_price = next_open * (1 + slip)
            elif next_low <= limit_price:
                entry_price = limit_price * (1 + slip)
            else:
                continue
        else:
            entry_price = next_open * (1 + slip)
        if entry_price <= 0:
            continue

        stop_loss = round(entry_price - cfg.sl_atr_mult * sig.atr, 2)
        take_profit = round(entry_price + cfg.tp_atr_mult * sig.atr, 2)
        qty = _position_size(entry_price, stop_loss, cfg)
        qty = max(0, int(qty * qty_mult))
        if qty == 0:
            continue

        open_trades[sym] = Trade(
            symbol=sym,
            entry_bar=i + 1,
            entry_date=str(df.index[i + 1].date()),
            entry_price=round(entry_price, 2),
            stop_loss=stop_loss,
            take_profit=take_profit,
            qty=qty,
            qty_initial=qty,
            score=sig.score,
            atr_at_entry=sig.atr,
            highest_high=round(entry_price, 2),
        )

    # ---------- close anything still open at the very end ----------
    for sym, active in list(open_trades.items()):
        df = per_ticker[sym]["intraday"]
        last = df.iloc[-1]
        atr_est = (active.entry_price - active.stop_loss) / max(cfg.sl_atr_mult, 1e-9)
        eod_slip = (cfg.base_slip_bp + cfg.atr_slip_k
                    * (atr_est / max(active.entry_price, 1e-9) * 100)) / 10000.0
        active.exit_price = round(float(last["close"]) * (1 - eod_slip), 2)
        active.exit_date = str(df.index[-1].date())
        active.exit_reason = "EOD"
        _close(active, sym, df.index[-1])

    metrics = compute_metrics(closed_trades, cfg)
    log.info("[time-step] done — %d trades, final DD=%.2f%%, equity=$%.2f",
             len(closed_trades), portfolio.dd_pct, portfolio.equity)
    return {
        "config": {
            "days": cfg.days, "timeframe": cfg.timeframe,
            "threshold": cfg.threshold,
            "tickers": list(per_ticker.keys()),
            "account_usd": cfg.account_usd,
            "simulator": "time_stepped",
        },
        "metrics": metrics,
        "trades": [asdict(t) for t in sorted(closed_trades, key=lambda t: t.entry_date)],
        "errors": [],
        "generated_at": str(date.today()),
    }


# ---------- metrics ----------

def compute_metrics(trades: list[Trade], cfg: Optional[BacktestConfig] = None) -> dict:
    """Risk-adjusted metrics + Monte Carlo. Delegates to src.metrics.

    `cfg` provides starting_capital + horizon — falls back to defaults when None
    so legacy callers (older saved results) still work.
    """
    from .metrics import compute_full_metrics

    if not trades:
        return {"total_trades": 0, "note": "no trades generated"}

    starting_capital = cfg.account_usd if cfg else 4500.0
    n_days = cfg.days if cfg else 180
    trade_dicts = [asdict(t) for t in trades]

    # Core risk-adjusted block (Sharpe daily, Sortino, Calmar, MAR, Ulcer, MC).
    metrics = compute_full_metrics(trade_dicts, starting_capital, n_days)

    # Per-trade-style add-ons the optimizer + GUI still want.
    sorted_t = sorted(trades, key=lambda t: t.exit_date)
    monthly: dict[str, float] = {}
    reasons: dict[str, int] = {}
    by_sym: dict[str, dict] = {}
    avg_win_pct = avg_loss_pct = 0.0
    wins_pct, losses_pct = [], []
    for t in sorted_t:
        monthly[t.exit_date[:7]] = round(monthly.get(t.exit_date[:7], 0.0) + t.pnl, 2)
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        s = by_sym.setdefault(t.symbol, {"trades": 0, "wins": 0, "pnl": 0.0})
        s["trades"] += 1
        s["wins"] += 1 if t.pnl > 0 else 0
        s["pnl"] = round(s["pnl"] + t.pnl, 2)
        if t.pnl > 0:
            wins_pct.append(t.pnl_pct)
        elif t.pnl < 0:
            losses_pct.append(t.pnl_pct)
    if wins_pct:
        avg_win_pct = round(sum(wins_pct) / len(wins_pct), 2)
    if losses_pct:
        avg_loss_pct = round(sum(losses_pct) / len(losses_pct), 2)

    metrics.update({
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "exit_reasons": reasons,
        "monthly_pnl": {k: v for k, v in sorted(monthly.items())},
        "by_symbol": by_sym,
    })
    return metrics


# ---------- main runner ----------

def prefetch_data(cfg: BacktestConfig, progress_cb=None) -> dict:
    """Fetch all kline data once. Returns a cache dict the optimizer can re-use
    across many Optuna trials without re-hitting OpenD.

    Layout:
        {
          "tf":        TF preset for the timeframe,
          "kltype":    SDK KLType for the intraday frame,
          "spy_daily": DataFrame | None,
          "per_ticker": {sym: {"intraday": df, "daily": df_d_or_none}, ...},
        }
    """
    from .moo_client import MooClient
    from .timeframe import HOUR_1, MIN_10, MIN_30
    from moomoo import KLType

    _TF_BY_NAME = {"HOUR_1": HOUR_1, "MIN_10": MIN_10, "MIN_30": MIN_30}
    tf = _TF_BY_NAME.get(cfg.timeframe.upper(), HOUR_1)   # DAILY mode removed 2026-06-07
    kltype = tf.kltype
    total_bars = _bars_needed(cfg)

    tickers = cfg.tickers or _load_watchlist()
    per_ticker: dict[str, dict] = {}
    spy_daily = None
    soxx_daily = None

    # Watchdog: future.result(timeout=) forces a TimeoutError when the SDK
    # call goes silent, instead of blocking forever. Background: caught this
    # with LRCX where the underlying socket read hung — there's no built-in
    # SDK timeout.
    #
    # CRITICAL: on timeout we must replace the executor — the stuck thread
    # will keep occupying the single worker slot otherwise, queueing every
    # subsequent fetch behind it. shutdown(wait=False) abandons the hung
    # thread (it dies with the process); a fresh executor gives us a fresh
    # worker slot.
    executor_holder = {
        "ex": concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="kline-fetch"
        )
    }

    def _fetch(sym, **kwargs):
        ex = executor_holder["ex"]
        future = ex.submit(c.get_kline, sym, **kwargs)
        try:
            return future.result(timeout=_FETCH_TIMEOUT_SEC)
        except concurrent.futures.TimeoutError:
            # Abandon the hung worker, spin up a fresh executor for the next call.
            ex.shutdown(wait=False)
            executor_holder["ex"] = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="kline-fetch"
            )
            raise RuntimeError(
                f"get_kline({sym}) hung > {_FETCH_TIMEOUT_SEC}s — OpenD silent; skipping"
            )

    if getattr(cfg, "data_source", "moo") == "yfinance":
        from .yfinance_source import YFinanceSource
        c = YFinanceSource()
        log.info("[prefetch] data source: yfinance (independent window)")
    else:
        c = MooClient()
    try:
        # SPY needed for the regime gate AND the Phase 3-A RS gate.
        if cfg.apply_regime_gate or cfg.apply_rs_gate:
            try:
                spy_daily = _fetch("SPY", bars=max(cfg.days + 250, 350),
                                   ktype=KLType.K_DAY)
                log.info("[prefetch] SPY daily: %d bars", len(spy_daily))
            except Exception as e:
                log.warning("[prefetch] SPY fetch failed: %s", e)

        # SOXX needed for the Phase 3-B fast sector-regime gate.
        if cfg.apply_sector_regime_gate:
            try:
                soxx_daily = _fetch("SOXX", bars=max(cfg.days + 80, 200),
                                    ktype=KLType.K_DAY)
                log.info("[prefetch] SOXX daily: %d bars", len(soxx_daily))
            except Exception as e:
                log.warning("[prefetch] SOXX fetch failed: %s", e)

        for idx, sym in enumerate(tickers):
            if progress_cb:
                progress_cb(idx, len(tickers), sym)
            try:
                df = _fetch(sym, bars=total_bars, ktype=kltype)
                if len(df) < _warm_up(cfg) + 20:
                    log.warning("[prefetch] %s: only %d bars — skipping", sym, len(df))
                    continue
                daily_df = None
                if cfg.apply_mtf_gate or cfg.apply_gap_gate or cfg.apply_dynamic_universe:
                    try:
                        # Dynamic universe needs ~157 daily bars BEFORE the sim
                        # start for the 6-1 momentum rank; all other consumers
                        # are tail()-bounded, so extra depth never changes them.
                        _d_bars = max(cfg.days + (220 if cfg.apply_dynamic_universe
                                                  else 60), 250)
                        daily_df = _fetch(sym, bars=_d_bars,
                                          ktype=KLType.K_DAY)
                    except Exception as e:
                        log.warning("[prefetch] daily fetch failed for %s: %s", sym, e)
                per_ticker[sym] = {"intraday": df, "daily": daily_df}
            except Exception as e:
                log.warning("[prefetch] %s failed: %s", sym, e)
    finally:
        # wait=False so a still-hung future doesn't block shutdown; the leaked
        # worker thread will die when the process exits.
        executor_holder["ex"].shutdown(wait=False)
        c.close()

    # ── 2026-05-28: join VIX history into each intraday df so the ML model's
    # macro features (vix_level, vix_change_5) read real values during backtest.
    # the broker's API doesn't serve VIX kline; we use yfinance for the daily series
    # and forward-fill (with a 1-day shift to avoid look-ahead).
    #
    # Bug fix 2026-05-29: buffer was `cfg.days + 60` calendar days, but the
    # OHLCV pull is `cfg.days * 7 + 80` HOUR_1 bars ≈ `cfg.days * 1.6` calendar
    # days back (more than 360 days for HOUR_1 timeframe). Insufficient VIX
    # buffer caused the early backtest bars to fall back to VIX=15.0 (neutral),
    # making the ML gate veto most signals and starving the 360-day run to
    # just 19 trades total. Padding generously: 3× cfg.days handles
    # HOUR_1 + reasonable warm-up.
    try:
        import yfinance as yf
        from datetime import date as _date, timedelta as _td
        end_d = _date.today()
        start_d = end_d - _td(days=cfg.days * 3 + 60)
        vix_raw = yf.download("^VIX", start=start_d, end=end_d, interval="1d",
                              progress=False, auto_adjust=True)
        if not vix_raw.empty:
            if isinstance(vix_raw.columns, pd.MultiIndex):
                vix_raw.columns = [c[0] for c in vix_raw.columns]
            vix_daily = pd.DataFrame(
                {"vix": vix_raw["Close"].astype(float)}, index=vix_raw.index)
            # Shift forward 1 day → bar reads yesterday's VIX close.
            vix_daily.index = pd.to_datetime(vix_daily.index).tz_localize(None).normalize() \
                              + pd.Timedelta(days=1)
            for sym, bundle in per_ticker.items():
                df = bundle["intraday"]
                df_dates = pd.to_datetime(df.index).tz_localize(None).normalize()
                df["vix"] = vix_daily.reindex(df_dates, method="ffill")["vix"].values
                df["vix"] = df["vix"].ffill().fillna(15.0)
            log.info("[prefetch] joined VIX history onto %d tickers", len(per_ticker))
        else:
            log.warning("[prefetch] VIX history empty — using neutral 15.0")
            for bundle in per_ticker.values():
                bundle["intraday"]["vix"] = 15.0
    except Exception as e:
        log.warning("[prefetch] VIX join failed: %s — using neutral 15.0", e)
        for bundle in per_ticker.values():
            bundle["intraday"]["vix"] = 15.0

    # (SEC EDGAR insider join removed 2026-06-03 with the ML subsystem — the
    # insider_30d_net_log feature was ablated to zero importance / zero $/day.)

    # ── 2026-05-28 v2: join sector-ETF close per ticker for `rs_vs_sector_5d`.
    # Fetches each unique sector ETF once (same broker path), then re-indexes
    # onto each ticker's bar index. Missing data → 'sector_close' = NaN; the
    # feature falls back to 0 in compute_features, so backtest survives.
    try:
        from .sector import SECTOR_TO_ETF, get_sector
        c2 = MooClient()
        unique_etfs = sorted({
            SECTOR_TO_ETF[s] for s in (get_sector(t) for t in per_ticker.keys())
            if s in SECTOR_TO_ETF
        })
        sector_etf_data: dict[str, pd.DataFrame] = {}
        try:
            for etf in unique_etfs:
                try:
                    sector_etf_data[etf] = c2.get_kline(etf, bars=total_bars, ktype=kltype)
                except Exception as e:
                    log.warning("[prefetch] sector ETF %s failed: %s", etf, e)
        finally:
            c2.close()
        for sym, bundle in per_ticker.items():
            etf = SECTOR_TO_ETF.get(get_sector(sym))
            df = bundle["intraday"]
            if etf and etf in sector_etf_data:
                df["sector_close"] = (
                    sector_etf_data[etf]["close"]
                    .reindex(df.index, method="ffill")
                    .ffill().bfill()
                    .values
                )
            else:
                df["sector_close"] = float("nan")
        log.info("[prefetch] joined %d sector ETFs onto ticker bars",
                 len(sector_etf_data))
    except Exception as e:
        log.warning("[prefetch] sector-ETF join failed: %s — feature defaults to 0", e)
        for bundle in per_ticker.values():
            bundle["intraday"]["sector_close"] = float("nan")

    log.info("[prefetch] complete: %d tickers cached", len(per_ticker))
    return {"tf": tf, "kltype": kltype, "spy_daily": spy_daily,
            "soxx_daily": soxx_daily, "per_ticker": per_ticker}


def simulate_with_cache(cfg: BacktestConfig, cache: dict, progress_cb=None) -> dict:
    """Run the simulation phase only, using pre-fetched data.

    Delegates to the TIME-STEPPED portfolio simulator (`simulate_time_stepped`)
    so DD breaker / max_positions / sector caps see the same chronological
    state the live `risk_manager` sees. The old per-ticker `backtest_ticker`
    is kept around for ad-hoc single-symbol debugging only.

    No OpenD calls — pure CPU. This is what the Optuna optimizer calls 20-30x
    while only the params change.
    """
    _orig = os.environ.get("TIMEFRAME", "")
    os.environ["TIMEFRAME"] = cfg.timeframe
    try:
        return simulate_time_stepped(cfg, cache, progress_cb=progress_cb)
    finally:
        if _orig:
            os.environ["TIMEFRAME"] = _orig
        else:
            os.environ.pop("TIMEFRAME", None)


# ── the one honest production engine ─────────────────────────────────────────
# Single source of truth for "what a real cash account would actually have done":
# the deleveraged V3 simulator with all three live-fidelity gaps closed (real
# cash wall, VIX risk-off sizing, earnings gate, real broker commissions). Every
# user-facing report — GUI panel, weekly Telegram self-check, CLI — flows through
# here, so there is exactly ONE honest number and no optimistic implicit-leverage
# path can leak into anything the user reads. The frozen oracle
# `simulate_time_stepped` is deliberately NOT used here; it stays a test-only
# reference (see its docstring) backing `simulate_with_cache` + engine_compare.
def _run_live_engine(cfg: BacktestConfig, cache: dict, progress_cb=None,
                     rich_metrics: bool = True) -> dict:
    from .backtest_v3 import simulate_v3  # lazy: avoids a circular import at module load
    # Mirror live sizing: trend + momentum_break, VIX/earnings/commissions, AND the
    # owner-approved regime up-scaling (inert at the default REGIME_BULL_MULT=1.0,
    # so the honest baseline is unchanged until the owner activates it in .env).
    from .config import settings as _s
    from . import runtime_config as _rc
    cfg_live = replace(cfg, apply_vix_sizing=True, apply_earnings_gate=True,
                       use_realistic_commission=True,
                       apply_momentum_strategy=True,
                       use_regime_scaling=True,
                       regime_bull_mult=_s.regime_bull_mult,
                       regime_vix_calm=_s.regime_vix_calm,
                       # Phase 0 realism (2026-06-10): pay live's exit/fill/
                       # lookahead frictions in every user-facing number.
                       scan_grid_exits=True,
                       entry_fill_open_only=True,
                       no_same_day_daily=True,
                       reclamp_position_cap=True,
                       apply_trade_windows=True,
                       max_new_names_per_scan=_s.max_new_names_per_scan,
                       use_breakeven_stop=_s.use_breakeven_stop,
                       breakeven_trigger_r=_rc.breakeven_trigger_r())
    return simulate_v3(cfg_live, cache, enforce_cash=True,
                       rich_metrics=rich_metrics, progress_cb=progress_cb)


def run_backtest(
    cfg: BacktestConfig,
    progress_cb=None,
) -> dict:
    """One-shot: fetch + simulate. Equivalent to prefetch_data + simulate_with_cache.

    Kept for backwards compatibility with the GUI, CLI, and any external callers.
    The same progress_cb fires during BOTH phases — the callback's (cur, total,
    label) args mean ticker-being-fetched during prefetch and event-being-replayed
    during the simulation (label = symbol whose bar is currently being scored).

    HONEST ENGINE (2026-06): this user-facing path runs the deleveraged V3
    simulator with the three live-fidelity gaps closed — real $5k cash wall
    (enforce_cash), VIX risk-off sizing, earnings gate, and real broker (MY)
    commissions — so the GUI panel and the weekly Telegram self-check report
    what a real cash account would have done, not the optimistic implicit-leverage
    number. The frozen oracle `simulate_time_stepped` is untouched and still backs
    `simulate_with_cache` (Optuna) and the engine_compare differential test.
    """
    cache = prefetch_data(cfg, progress_cb=progress_cb)
    # Phase 0 guard (2026-06-10): a dead network/OpenD makes prefetch return 0
    # tickers and the engine "completes" with 0 trades — which then OVERWRITES
    # data/backtest_results.json with garbage the GUI displays as the honest
    # number (happened on the 2026-06-08 Sunday catch-up run). Refuse loudly;
    # the weekly job's except-branch telegrams the failure and the GUI keeps
    # the last good result.
    if not cache.get("per_ticker"):
        raise RuntimeError(
            "prefetch returned 0 tickers (network/OpenD down?) — "
            "refusing to run and overwrite the last good backtest result")
    result = _run_live_engine(cfg, cache, progress_cb=progress_cb)
    # Log per-symbol trade counts for parity with the old behaviour.
    by_sym: dict[str, int] = {}
    for t in result["trades"]:
        by_sym[t["symbol"]] = by_sym.get(t["symbol"], 0) + 1
    for sym in cache["per_ticker"]:
        log.info("%s: %d trades", sym, by_sym.get(sym, 0))
    _save_result(result)
    return result


def _save_result(result: dict) -> None:
    """Write the backtest result JSON to disk for the GUI to pick up."""
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(result, indent=2))
    log.info("Results saved to %s", RESULTS_FILE)


# ---------- pretty print ----------

def print_report(result: dict) -> None:
    m = result["metrics"]
    cfg = result["config"]
    print("\n" + "=" * 64)
    print(f"  BACKTEST REPORT  |  {cfg['timeframe']}  |  {cfg['days']} days")
    print("=" * 64)
    print(f"  Tickers tested   : {len(cfg['tickers'])}")
    print(f"  Total trades     : {m.get('total_trades', 0)}")
    if m.get("total_trades", 0) == 0:
        print("  No trades generated — try lowering --threshold")
        return

    # --- Sample stats ---
    print()
    print("  -- SAMPLE --")
    print(f"  Win rate           : {m['win_rate_pct']}%")
    print(f"  Profit factor      : {m['profit_factor']}")
    print(f"  Expectancy/trade   : ${m.get('expectancy_per_trade_usd', 0):+.2f}")
    print(f"  Kelly fraction     : {m.get('kelly_fraction_pct', 0):+.2f}%")
    print(f"  Avg win / loss     : {m['avg_win_pct']:+.2f}% / {m['avg_loss_pct']:+.2f}%")

    # --- Return ---
    print()
    print("  -- RETURN --")
    print(f"  Starting capital   : ${m.get('starting_capital_usd', 0):,.2f}")
    print(f"  Final equity       : ${m.get('final_equity_usd', 0):,.2f}")
    print(f"  Net PnL            : ${m.get('net_pnl_usd', 0):+,.2f}  "
          f"({m.get('total_return_pct', 0):+.2f}%)")
    print(f"  CAGR               : {m.get('cagr_pct', 0):+.2f}%")

    # --- Risk-adjusted ---
    print()
    print("  -- RISK-ADJUSTED --")
    print(f"  Sharpe (daily)     : {m['sharpe_ratio']}")
    print(f"  Sortino            : {m.get('sortino_ratio', 0)}")
    print(f"  Calmar             : {m.get('calmar_ratio', 0)}")
    print(f"  MAR                : {m.get('mar_ratio', 0)}")
    print(f"  Ulcer Index        : {m.get('ulcer_index', 0)}")

    # --- Drawdown ---
    print()
    print("  -- DRAWDOWN --")
    print(f"  Max DD             : ${m.get('max_drawdown_usd', 0):.2f}  "
          f"({m.get('max_drawdown_pct', 0):.2f}%)")
    print(f"  Underwater days    : {m.get('max_drawdown_days', 0)}")

    # --- Monte Carlo (the bullshit detector) ---
    mc = m.get("monte_carlo", {})
    if mc and "n_simulations" in mc:
        print()
        print(f"  -- MONTE CARLO ({mc['n_simulations']} sims) --")
        print(f"  Final equity P5/P50/P95 : ${mc['p5_final']:,.0f}  /  "
              f"${mc['p50_final']:,.0f}  /  ${mc['p95_final']:,.0f}")
        print(f"  Max DD%   P5/P50/P95    : {mc['p5_max_dd_pct']}%  /  "
              f"{mc['p50_max_dd_pct']}%  /  {mc['p95_max_dd_pct']}%")
        print(f"  P(profitable)           : {mc['prob_profitable_pct']}%")
        print(f"  P(ruin: -50% DD)        : {mc['prob_ruin_pct']}%")
    elif mc and "note" in mc:
        print(f"\n  Monte Carlo: {mc['note']}")

    # --- Exit / Monthly / Symbol breakdown (unchanged) ---
    print()
    print("  Exit breakdown:")
    for reason, count in sorted(m.get("exit_reasons", {}).items()):
        print(f"    {reason:<12}: {count}")

    print()
    print("  Monthly PnL:")
    for month, pnl in m.get("monthly_pnl", {}).items():
        bar = "█" * min(20, max(0, int(abs(pnl) / 5)))
        sign = "+" if pnl >= 0 else "-"
        print(f"    {month}  {sign}${abs(pnl):.2f}  {bar}")

    print()
    print("  Top 5 symbols:")
    by_sym = m.get("by_symbol", {})
    ranked = sorted(by_sym.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for sym, s in ranked[:5]:
        wr = round(s["wins"] / s["trades"] * 100)
        print(f"    {sym:<8}  {s['trades']} trades  {wr}% WR  ${s['pnl']:+.2f}")
    print("=" * 64)
    print(f"  Full results saved to: data/backtest_results.json")
    print("=" * 64 + "\n")


# ---------- CLI entry point ----------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    ap = argparse.ArgumentParser(description="moo-trader backtester")
    ap.add_argument("--days", type=int, default=180, help="Lookback window in calendar days")
    ap.add_argument("--timeframe", default=None, help="HOUR_1 / MIN_10 / MIN_30 (default: from .env; DAILY mode removed)")
    ap.add_argument("--threshold", type=float, default=None, help="Entry score threshold (default: from .env)")
    ap.add_argument("--tickers", nargs="*", help="Specific tickers (default: watchlist)")
    args = ap.parse_args()

    from .config import settings

    cfg = BacktestConfig(
        days=args.days,
        timeframe=args.timeframe or settings.timeframe,
        threshold=args.threshold or settings.entry_threshold,
        tickers=args.tickers or [],
        account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days,
        # NEW: pull tuned exit / gap knobs from settings (Optuna writes these).
        tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
        # Bug fix 2026-05-29: pull DD circuit knobs from .env too. Without this
        # the backtest hardcoded dd_halt_pct=15 even when .env said 18, causing
        # a permanent halt early in the 360-day backtest as a temporary 16% DD
        # locked all future entries.
        dd_size_cut_pct=settings.dd_size_cut_pct,
        dd_halt_pct=settings.dd_halt_pct,
    )

    print(f"\nRunning backtest: {cfg.timeframe}, {cfg.days} days, threshold={cfg.threshold}")
    print(f"Tickers: {cfg.tickers or 'watchlist'}\n")

    result = run_backtest(cfg)
    print_report(result)


if __name__ == "__main__":
    main()
