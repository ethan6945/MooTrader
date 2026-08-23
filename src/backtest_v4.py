#!/usr/bin/env python3
"""backtest_v4 — one engine.

WHAT THIS REPLACES AND WHY

  Three engines used to answer the same question and disagree:

    sandbox.py            replayed live's decision chain by importing live's
                          own gates, on live's 15-minute scan grid, with no
                          look-ahead. Its EXECUTION was fiction: limit orders
                          that never expired, stops that filled at the exact
                          level on an intrabar touch, no cash wall, and equity
                          marked at COST so an open loser was invisible.
    backtest_v3.py        modelled execution honestly — real cash, soft stops,
                          mark-to-market drawdown — on top of a hand-written
                          copy of the decision chain that kept drifting from
                          live: it never had the blacklist, spread, or sector
                          gates at all, and its gap filter was inverted.
    simulate_time_stepped the optimistic oracle, kept to cross-check v3. Its
                          diff-test partner (scripts/engine_compare.py) no
                          longer exists.

  The 2026-08-21 differential scored them at 53.8% trade-level agreement, with
  sandbox at −$16 and v3 at −$286 over the same window. Live, over the same
  window, was −$424. Two engines, neither close, and the OPTIMISTIC one was
  the one wired to change live parameters.

  v4 keeps the half of each that was right: sandbox's skeleton — SimClock,
  SimFeed, and a funnel that calls live's real gate code — with v3's execution
  and accounting bolted where sandbox's fiction used to be.

WHAT IT DOES NOT MODEL
  Named here rather than discovered later. live runs these and v4 does not:

    news_driven entries, auto_budget compounding, cash_yield parking, the
    inverse sleeve, and gap_sentinel's pre-open exits.

  PYRAMIDING IS MODELLED (2026-08-23) through src/stacking.py, the same rule
  live's risk_manager and executor call. An add-on pays the same TTL, chase,
  liquidity and cash frictions as a fresh entry — a stack that always filled
  would be the kind of fiction this engine exists to remove.

  A v4 number is a claim about the core long strategy, not about the whole
  account.

RESOLUTION LIMIT
  Exits resolve once per CLOSED hourly bar. live's fast-stop loop checks every
  tick, so live catches intra-hour stop breaches v4 cannot see — measured as
  live stopping out on 53% of trades against v4's 39% over the same window.
  Finer resolution would mean inventing intra-bar prices, and a replay that
  guesses at prices is not evidence.

CALIBRATION, NOT PARITY
  The old pass/fail was "do the two engines agree". That question is gone with
  the second engine, and it was never the right one — two engines can agree
  and both be wrong. v4 is scored against REAL FILLS
  (scripts/v4_vs_live.py reads db.closed_trades). That is the only comparison
  that can be wrong in a way that costs money.

Run:
  .venv/bin/python3 -m src.backtest_v4 --days 30
  .venv/bin/python3 -m src.backtest_v4 --from 2026-07-22 --to 2026-08-21
"""
from __future__ import annotations

import json
import logging
import sys
import time as _time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moomoo import KLType

from src.config import ROOT, derive_max_positions, settings
from src import (blacklist, entry_gates, entry_threshold, indicators,
                 regime as regime_mod, risk_manager, runtime_config, sector,
                 sizing_rule, strategy_momentum, strategy_mr, strategy_pattern)
from src import concentration, stacking
from src.sim_feed import (SimClock, SimFeed, _bar_session, _note_fill,
                          reset_fill_stats)

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

RESULTS_FILE = ROOT / "data" / "backtest_v4_results.json"


# ── Config ────────────────────────────────────────────────

@dataclass
class V4Config:
    start: datetime
    end: datetime
    tickers: list[str] = field(default_factory=list)
    universe_mode: str = "static"
    # 0 → live's SCAN_INTERVAL_MIN. The replay exists to reproduce live, and
    # live scans on that grid; a coarser one gives the replay fewer decision
    # points than live had and lowers its trade count for a reason that has
    # nothing to do with the strategy.
    scan_interval_min: int = 0
    data_lookback_days: int = 120
    # 0 → risk_manager.budget_usd() (db-state override, else .env ACCOUNT_USD)
    account_usd: float = 0.0
    sessions: str = "RTH"
    # Execution realism. All ON — there is no "optimistic lens" any more. The
    # flags exist so a diagnostic can isolate one friction, not so a caller can
    # quietly turn the honesty off.
    enforce_cash: bool = True
    soft_stops: bool = True
    entry_ttl: bool = True
    # Pyramiding — modelled (src/stacking.py) but DEFAULT OFF, on evidence.
    #
    # Live is configured for it (MAX_STACKS_PER_SYMBOL=5, STACK_MIN_R=0.5) and
    # the code here mirrors live's rule exactly. But the account took ZERO
    # add-ons across all 37 recorded trades, and v4 takes about one per trade.
    # Measured against real fills over 2026-06-11 → 08-10:
    #
    #                     trades   win rate   net PnL    net gap   WR gap
    #     live (truth)      34       20.6%     -$667        —         —
    #     pyramiding ON     47       27.7%     -$992      32.7%     7.1pp
    #     pyramiding OFF    46       39.1%     -$590      11.7%    18.5pp
    #
    # A real trade-off: ON matches live's win rate far better, OFF matches its
    # PnL far better. What breaks the tie is that ON deploys capital the
    # account demonstrably did not — modelling a behaviour the data contradicts
    # is the same class of error as backtest_v3's inverted gap filter, which is
    # what this whole merge existed to remove.
    #
    # Flip this to True the moment live actually stacks, or the moment the
    # reason it does not is found. scripts/v4_vs_live.py prints both rates on
    # every run so the question stays in front of whoever looks.
    model_pyramiding: bool = False


# ── Cost model ────────────────────────────────────────────
# One place. data/fill_model_calibration.json is the evidence these answer to;
# scripts/calibrate_fill_model.py measures the account and prints what it
# thinks they should be, and never edits them.

_COMMISSION_PER_SHARE = 0.0049    # $0.0049/share
_COMMISSION_MIN = 0.99            # min $0.99 per side
_SLIPPAGE_BPS = 5.0               # 5 bps = 0.05% one-way

# A stop that breaks does not fill at the stop. Live's protective exit is a
# marketable limit priced PROTECTIVE_EXIT_SLIP below last, and the measured
# cost of that is an average −1.38R against a modelled −1R. This is the
# multiplier on ordinary slippage that a stop exit pays on top.
_SL_BREAKAWAY_MULT = 3.0

# The most of a bar's traded volume one resting order may claim. A limit that
# "fills because the bar's low touched it" is a claim about LIQUIDITY, and it
# was unconditional — the same assumption for a 3.2M-share regular-hours bar
# and an 8.5K-share overnight one.
_MAX_VOLUME_PARTICIPATION = 0.10

_SPREAD_MAX_PCT = 0.5             # live SPREAD_MAX_PCT
_COOLDOWN_MINUTES = 60            # live SL re-entry cooldown
_EARNINGS_AVOID_DAYS = 2


def _commission(qty: int) -> float:
    """One side. The round trip is two of these."""
    return max(_COMMISSION_MIN, _COMMISSION_PER_SHARE * qty)


# NOTE ON SLIPPAGE — inherited bug, fixed here.
#
# sandbox put slippage in the entry PRICE ("now carried in ep") and then also
# charged it again through its cost helper, so every trade paid its entry
# slippage twice: once in what it recorded as the fill, once as a deduction
# from the net. The recorded entry described a price that was never paid AND
# the net described a cost that was never charged. v4 keeps slippage in the
# prices only — entry pays it up, every modelled exit pays it down — and
# reports the embedded dollar amount separately so it stays visible.


def _business_days(start: datetime, end: datetime) -> int:
    """Weekday count between start (exclusive) and end (inclusive) — the same
    hold-age measure live's executor uses."""
    if end <= start:
        return 0
    days, d, last = 0, start.date(), end.date()
    while d < last:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


# ── Positions and trades ──────────────────────────────────

@dataclass
class Position:
    symbol: str
    entry: float
    stop: float
    tp: float
    qty: int
    entry_time: datetime
    strategy: str
    score: float
    init_stop: float
    atr_at_entry: float
    high_w: float
    low_w: float
    entry_slip_usd: float = 0.0
    breakeven_set: bool = False
    # Pyramiding state, mirroring live's open_trades record. init_risk_per_share
    # is the ORIGINAL per-share risk and does NOT move when the stop ratchets —
    # that stability is the whole reason live measures stack eligibility against
    # it rather than against the current stop.
    stacks: int = 1
    init_risk_per_share: float = 0.0
    high_water: float = 0.0
    # The bar this position last resolved an exit against, so a 15-minute grid
    # does not re-test the same hourly bar four times.
    last_bar_seen: object = None


@dataclass
class ClosedTrade:
    symbol: str
    entry: float
    exit: float
    qty: int
    pnl: float
    pnl_pct: float
    r: float
    reason: str
    strategy: str
    entry_t: datetime
    exit_t: datetime
    commission: float
    slippage: float
    net_pnl: float
    mfe: float
    mae: float
    score: float


# ── Broker ────────────────────────────────────────────────

class V4Broker:
    """Honest execution. Every difference from sandbox's SimBroker is a
    friction sandbox did not charge."""

    def __init__(self, feed: SimFeed, clock: SimClock, cfg: V4Config):
        self.feed, self.clock, self.cfg = feed, clock, cfg
        self.positions: dict[str, Position] = {}
        self.trades: list[ClosedTrade] = []
        self.start_capital = cfg.account_usd or settings.account_usd
        self.cash = self.start_capital
        self.last_sl_time: dict[str, datetime] = {}
        self.last_close: dict[str, float] = {}
        # Honest drawdown: every open position marked to each bar's close.
        self.peak_mtm = self.start_capital
        self.max_dd_mtm = 0.0
        self.n_cash_blocked = 0
        self.n_cash_clipped = 0
        self.n_ttl_expired = 0
        self.n_fills = 0
        self.n_orders = 0
        self.n_stacks = 0

    # -- fills -------------------------------------------------------------

    def try_fill(self, symbol: str, limit: float, stop: float, tp: float,
                 qty: int, strategy: str, score: float, atr: float) -> bool:
        """Place-and-resolve, in one call, because that is what live's order
        lifetime allows.

        live places a limit off the live quote and cancels it after
        ORDER_TIMEOUT_MIN (5) minutes. On the 2026-07-11 calibration that meant
        132 of 177 buy orders expired unfilled — a 25.4% fill rate. sandbox's
        model was "the bar's low touched the limit at any point in the hour, so
        it filled", which is a twelve-times-longer order than live ever places,
        and it filled at the limit even when the bar OPENED through it.

        So the only price a replay may honestly claim is one observable at a
        known instant inside the order's life: the next bar's open. Around it
        go live's own two refusals — it will not chase a market that has
        already run past the signal, and it will not trade a signal whose
        price no longer describes the market.
        """
        self.n_orders += 1
        bar = self._next_bar(symbol)
        if bar is None:
            self.n_ttl_expired += 1
            return False
        op = float(bar["open"])
        if op <= 0:
            self.n_ttl_expired += 1
            return False

        # live's ENTRY_CHASE_TOL: the market ran past the signal price, so the
        # limit is stale to the upside and would never have been hit.
        from src.executor import ENTRY_CHASE_TOL, STALE_SIGNAL_TOL
        if op > limit * (1 + ENTRY_CHASE_TOL):
            self.n_ttl_expired += 1
            return False
        # live's STALE_SIGNAL_TOL: the quote is far BELOW the signal bar close,
        # so the score, ATR and stop were computed on data that no longer
        # describes the market. live skips; so does this.
        if op < limit * (1 - STALE_SIGNAL_TOL):
            self.n_ttl_expired += 1
            return False

        # Liquidity. A partial fill is not a special case — a smaller fill is
        # simply the honest answer.
        try:
            bar_vol = float(bar["volume"])
        except (KeyError, TypeError, ValueError):
            bar_vol = 0.0
        _sess = _bar_session(self.clock.ny_now())
        _note_fill(_sess, "attempted")
        if bar_vol > 0:
            fillable = int(bar_vol * _MAX_VOLUME_PARTICIPATION)
            if fillable <= 0:
                _note_fill(_sess, "blocked")
                self.n_ttl_expired += 1
                return False
            if fillable < qty:
                _note_fill(_sess, "capped")
                qty = fillable

        raw_px = min(op, limit)
        entry_px = round(raw_px * (1 + _SLIPPAGE_BPS / 10000.0), 4)
        comm = _commission(qty)

        # THE CASH WALL. sandbox tracked a cash balance and never once checked
        # it before buying, so it could hold stock it had no money for.
        if self.cfg.enforce_cash:
            affordable = int((self.cash - comm) / max(entry_px, 1e-9))
            if affordable < qty:
                if affordable <= 0:
                    self.n_cash_blocked += 1
                    return False
                qty = affordable
                self.n_cash_clipped += 1
                comm = _commission(qty)
        if qty <= 0:
            self.n_cash_blocked += 1
            return False

        self.positions[symbol] = Position(
            symbol=symbol, entry=entry_px, stop=stop, tp=tp, qty=qty,
            entry_time=self.clock.ny_now(), strategy=strategy, score=score,
            init_stop=stop, atr_at_entry=atr, high_w=entry_px, low_w=entry_px,
            entry_slip_usd=round((entry_px - raw_px) * qty, 2),
            init_risk_per_share=max(entry_px - stop, 0.0), high_water=entry_px)
        self.cash -= (entry_px * qty + comm)
        self.n_fills += 1
        return True

    def try_stack(self, symbol: str, limit: float, stop: float, tp: float,
                  qty: int, atr: float) -> bool:
        """Add a lot to a position already held, on live's terms.

        Deliberately re-uses the fill path rather than assuming the add-on
        lands: an add-on is an ordinary limit order at the broker and expires
        the same way. It also spends cash a brand-new name could have used,
        which is the whole point on a real account — so the cash wall gates it
        exactly as it gates a first entry.
        """
        pos = self.positions.get(symbol)
        if pos is None:
            return False
        # Fill the add-on as if it were a fresh entry, then merge. Borrowing
        # try_fill's model keeps ONE fill model in this engine; the position it
        # writes is popped straight back off.
        held_before = pos
        self.positions.pop(symbol, None)
        filled = self.try_fill(symbol, limit, stop, tp, qty,
                               held_before.strategy, held_before.score, atr)
        if not filled:
            self.positions[symbol] = held_before
            return False
        addon = self.positions.pop(symbol)
        lot = stacking.merge(
            old_qty=held_before.qty, old_entry=held_before.entry,
            old_stop=held_before.stop, old_tp=held_before.tp,
            old_high_water=held_before.high_water,
            old_stacks=held_before.stacks,
            add_qty=addon.qty, add_entry=addon.entry,
            add_stop=addon.stop, add_tp=addon.tp)
        held_before.qty = lot.qty
        held_before.entry = lot.entry
        held_before.stop = lot.stop
        held_before.tp = lot.take_profit
        held_before.init_risk_per_share = lot.init_risk_per_share
        held_before.high_water = lot.high_water
        held_before.stacks = lot.stacks
        # live sets atr = signal.atr on every fill, and re-anchors the initial
        # stop with the merged lot so R-multiples describe the position that
        # now exists rather than the one that used to.
        held_before.atr_at_entry = atr
        held_before.init_stop = lot.stop
        held_before.entry_slip_usd += addon.entry_slip_usd
        # Stacks only happen in profit, so the breakeven ratchet re-arms
        # against the new average entry rather than staying latched.
        held_before.breakeven_set = lot.stop >= lot.entry
        self.positions[symbol] = held_before
        self.n_stacks += 1
        return True

    def _next_bar(self, sym: str):
        k = self.feed.get_kline(sym, bars=1)
        if k is None or k.empty:
            return None
        return k.iloc[-1]

    # -- position management ----------------------------------------------

    def process_bar(self) -> None:
        """Resolve exits against the latest CLOSED bar, then mark to market."""
        for sym, pos in list(self.positions.items()):
            k = self.feed.get_kline(sym, bars=1)
            if k is None or k.empty:
                continue
            bar_ts = k.index[-1]
            bar = k.iloc[-1]
            # One resolution per bar. On a 15-minute grid the same hourly bar
            # is the "latest closed" for four consecutive scans.
            if pos.last_bar_seen is not None and bar_ts == pos.last_bar_seen:
                continue
            pos.last_bar_seen = bar_ts

            hi, lo = float(bar["high"]), float(bar["low"])
            cl, op = float(bar["close"]), float(bar["open"])
            pos.high_w = max(pos.high_w, hi)
            pos.low_w = min(pos.low_w, lo)
            pos.high_water = max(pos.high_water, hi)
            self.last_close[sym] = cl

            atr0 = pos.atr_at_entry if pos.atr_at_entry > 0 else \
                (pos.entry - pos.init_stop) / max(runtime_config.sl_atr_mult(), 1e-9)
            exit_slip = _SLIPPAGE_BPS / 10000.0

            # Breakeven ratchet — armed off the HIGH-WATER mark so a spike
            # between scans still arms it, ratchet only.
            if settings.use_breakeven_stop and not pos.breakeven_set:
                risk = pos.entry - pos.init_stop
                if risk > 0 and pos.high_w >= pos.entry + \
                        runtime_config.breakeven_trigger_r() * risk:
                    pos.breakeven_set = True
                    if pos.entry > pos.stop:
                        pos.stop = round(pos.entry, 2)

            exit_px = reason = raw_exit = None
            sl_trigger = None
            if self.cfg.soft_stops:
                # LIVE STOPS ARE SOFT. They are checked against a snapshot at
                # scan boundaries and filled at market — never intrabar at the
                # exact level. sandbox filled at min(open, stop) on any touch,
                # which is why its worst stop was −1.09R while live's was
                # −2.09R on the same window. A gap-through fills at the open;
                # otherwise a CLOSE beyond the stop fills at the close, paying
                # the overshoot that accrued since the level broke. An intrabar
                # dip that recovers by the close is NOT an exit — live misses
                # it too.
                if op <= pos.stop:
                    sl_trigger = op
                elif cl <= pos.stop:
                    sl_trigger = cl
            elif lo <= pos.stop:
                sl_trigger = min(op, pos.stop)

            if sl_trigger is not None:
                raw_exit = sl_trigger
                exit_px = sl_trigger * (1 - exit_slip * _SL_BREAKAWAY_MULT)
                reason = ("BREAKEVEN" if pos.breakeven_set
                          and pos.stop >= pos.entry else "SL")
            elif hi >= pos.tp:
                # TP keeps touch-fill: live's 5-minute manage tick books last
                # >= TP within minutes of a touch, so this is the conservative
                # side of the real behaviour.
                raw_exit = exit_px = pos.tp
                reason = "TP"
            elif _business_days(pos.entry_time, self.clock.ny_now()) >= \
                    runtime_config.max_hold_days():
                raw_exit = cl
                exit_px = cl * (1 - exit_slip)
                reason = "MAX_HOLD"

            if exit_px is not None:
                self._book(pos, exit_px, reason, raw_exit=raw_exit)

        self._mark_to_market()

    def _book(self, pos: Position, exit_px: float, reason: str,
              raw_exit: float | None = None) -> None:
        gross = (exit_px - pos.entry) * pos.qty
        # Commission is the only cost NOT already inside the prices. Slippage
        # is: the entry paid it up, the exit paid it down. Charging it again
        # here is the double-count inherited from sandbox.
        comm = round(_commission(pos.qty) * 2, 2)
        slip_embedded = round(
            pos.entry_slip_usd
            + (max(0.0, (raw_exit - exit_px)) * pos.qty if raw_exit else 0.0), 2)
        r_unit = pos.entry - pos.init_stop
        self.trades.append(ClosedTrade(
            symbol=pos.symbol, entry=pos.entry, exit=round(exit_px, 4),
            qty=pos.qty, pnl=round(gross, 2),
            pnl_pct=(exit_px - pos.entry) / pos.entry * 100,
            r=(exit_px - pos.entry) / r_unit if r_unit > 0 else 0.0,
            reason=reason, strategy=pos.strategy,
            entry_t=pos.entry_time, exit_t=self.clock.ny_now(),
            commission=comm, slippage=slip_embedded,
            net_pnl=round(gross - comm, 2),
            mfe=(pos.high_w - pos.entry) / pos.entry * 100,
            mae=(pos.low_w - pos.entry) / pos.entry * 100,
            score=pos.score))
        # Proceeds in, exit commission out — the entry half was already
        # debited at fill.
        self.cash += exit_px * pos.qty - _commission(pos.qty)
        if reason == "SL":
            self.last_sl_time[pos.symbol] = self.clock.ny_now()
        del self.positions[pos.symbol]

    def _mark_to_market(self) -> None:
        eq = self.equity()
        self.peak_mtm = max(self.peak_mtm, eq)
        if self.peak_mtm > 0:
            self.max_dd_mtm = max(self.max_dd_mtm,
                                  (self.peak_mtm - eq) / self.peak_mtm * 100)

    def equity(self) -> float:
        """Cash plus open positions AT MARKET.

        sandbox used `entry * qty` — cost, not market — so an open loser was
        invisible to both the drawdown curve and the next entry's sizing.
        """
        return self.cash + sum(
            p.qty * self.last_close.get(p.symbol, p.entry)
            for p in self.positions.values())

    def held_symbols(self) -> set:
        return set(self.positions.keys())

    def close_all(self, why: str = "EOD") -> None:
        for pos in list(self.positions.values()):
            px = self.last_close.get(pos.symbol, pos.entry)
            self._book(pos, px * (1 - _SLIPPAGE_BPS / 10000.0), why, raw_exit=px)


# NEWS-DRIVEN MODE IS NOT MODELLED HERE, and cannot be. When
# NEWS_DRIVEN_ENABLED is on, live selection is made by the AI news read this
# engine deliberately skips, so a v4 run measures a DIFFERENT strategy — not a
# pessimistic version of the live one, a different one. The switch is live-only
# by construction and preflight.check_news_driven() says so on every start.
# Two things would be needed to close the gap, and neither exists yet: a
# point-in-time news archive (Tavily serves "now", not "as of 2026-03-04"), and
# a scorer with a training cutoff before the test window — a local
# FinBERT-class model rather than a frontier LLM.
#
# AI news/veto/sentiment SKIPPED for the same reason: a frontier model asked
# about March already knows how March ended, so annotating historical trades
# with it produces look-ahead that reads exactly like evidence.


# ── Gates that need the replay's own data ─────────────────

def _assess_regime(feed: SimFeed) -> tuple:
    spy_daily = feed.get_kline("SPY", bars=250, ktype=KLType.K_DAY)
    if spy_daily is None or len(spy_daily) < 200:
        return (regime_mod.Regime("NEUTRAL", 0, 0, 0, False, False, "no SPY data"),
                "NEUTRAL", 15.0)
    vix = feed.get_vix()
    try:
        reg = regime_mod.assess(spy_daily, vix=vix)
    except Exception:
        reg = regime_mod.Regime("NEUTRAL", 0, 0, 0, False, False, "assess failed")
    effective = reg.confirmed if settings.smart_regime_enabled else reg.label
    return reg, effective, vix


def _breadth_from_spy(feed: SimFeed, vix: float) -> tuple[bool, str]:
    """live's breadth check, against SimFeed's SPY dailies.

    breadth.assess() needs the SDK quote context; SimFeed offers a different
    interface, so the rule is applied directly to the same inputs.
    """
    from src import breadth
    try:
        spy = feed.get_kline("SPY", bars=55, ktype=KLType.K_DAY)
        if spy is None or len(spy) < 50:
            return True, "insufficient SPY bars — passing"
        close_ser, open_ser = pd.Series(spy["close"]), pd.Series(spy["open"])
        sma50 = float(close_ser.rolling(50).mean().iloc[-1])
        price = float(close_ser.iloc[-1])
        n = len(close_ser)
        days_up = sum(1 for i in range(-10, 0)
                      if i + n >= 0 and close_ser.iloc[i] > open_ser.iloc[i])
        ad_ratio = days_up / 10
        vix_ok = (vix < breadth.VIX_PANIC) if (feed.vix_is_real and vix > 0) else True
        ok = (ad_ratio >= 0.55) and ((price / sma50 - 1) * 100 > -10) and vix_ok
        return ok, (f"AD={ad_ratio:.2f} SPYvs50MA={(price/sma50-1)*100:.1f}%"
                    + ("" if vix_ok else f" VIX {vix:.0f}>={breadth.VIX_PANIC}"))
    except Exception as e:
        return True, f"breadth calc failed: {e} — passing"


def _earnings_blocked(symbol: str, now: datetime, cal: dict) -> tuple[bool, str]:
    """Historical earnings gate. Only dates in the FUTURE relative to the sim
    clock count — a past release is not a reason to refuse an entry."""
    for ds in cal.get(symbol.upper(), []):
        try:
            d = datetime.fromisoformat(str(ds)[:10]).date()
        except (ValueError, TypeError):
            continue
        delta = (d - now.date()).days
        if 0 <= delta <= _EARNINGS_AVOID_DAYS:
            return True, f"earnings in {delta}d ({d})"
    return False, ""


def _spread_pct(symbol: str, feed: SimFeed) -> float:
    """Liquidity proxy: recent bar range stands in for the quoted spread."""
    try:
        df = feed.get_kline(symbol, bars=20)
        if df is None or len(df) < 5:
            return 0.0
        rng = (df["high"] - df["low"]) / df["close"] * 100
        return float(rng.tail(5).mean()) * 0.05
    except Exception:
        return 0.0


def _cooldown_active(symbol: str, broker: V4Broker, now: datetime) -> bool:
    t = broker.last_sl_time.get(symbol)
    return t is not None and (now - t).total_seconds() / 60 < _COOLDOWN_MINUTES


def _load_universe(cfg: V4Config, feed: SimFeed | None = None) -> list[str]:
    if cfg.tickers and cfg.universe_mode != "dynamic":
        return list(cfg.tickers)
    if cfg.universe_mode == "dynamic" and feed is not None:
        try:
            from src.universe import Affordability, select_universe
            return select_universe(feed.daily_dict(), asof=cfg.start.date(),
                                   top_n=runtime_config.universe_top_n(),
                                   afford=Affordability.live())
        except Exception as e:
            log.warning("dynamic universe failed: %s — using watchlist", e)
    wf = ROOT / "config" / "watchlist.json"
    if wf.exists():
        return json.loads(wf.read_text()).get("tickers", [])
    return ["AAPL", "MSFT", "NVDA", "AMD", "GOOGL"]


# ── The run ───────────────────────────────────────────────

def run_v4(cfg: V4Config, progress_cb=None,
           rich_metrics: bool = True) -> dict:
    t0 = _time.time()
    if cfg.account_usd <= 0:
        cfg.account_usd = risk_manager.budget_usd()
    scan_minutes = cfg.scan_interval_min or settings.scan_interval_min

    tickers = _load_universe(cfg) or cfg.tickers or ["AAPL", "MSFT", "NVDA"]
    pool = tickers
    if cfg.universe_mode == "dynamic":
        try:
            from src.universe import load_pool
            pool = list(set(load_pool() + tickers))
        except Exception:
            pool = tickers

    reset_fill_stats()
    clock = SimClock(cfg.start, cfg.end, sessions=cfg.sessions)
    feed = SimFeed(pool, cfg.start, cfg.end, clock, cfg.data_lookback_days,
                   sessions=cfg.sessions)
    broker = V4Broker(feed, clock, cfg)

    if cfg.universe_mode == "dynamic":
        try:
            tickers = _load_universe(cfg, feed) or tickers
        except Exception:
            pass

    max_positions = derive_max_positions(cfg.account_usd)
    from src.earnings import load_earnings_calendar
    try:
        earnings_cal = load_earnings_calendar(pool)
    except Exception:
        earnings_cal = {}
    bl_active = blacklist.get_blacklist() or set()

    scans = eval_scans = signals_found = 0
    skip_counts: dict[str, int] = defaultdict(int)
    skip_events: list[dict] = []
    _SKIP_MAX = 10_000
    max_seen = 0.0

    def _skip(gate: str, symbol: str | None, reason: str) -> None:
        skip_counts[gate] += 1
        if len(skip_events) < _SKIP_MAX:
            skip_events.append({"t": clock.ny_now().isoformat(), "symbol": symbol,
                                "gate": gate, "reason": reason})

    while not clock.done():
        clock.advance(scan_minutes)
        now = clock.ny_now()
        if not clock.in_trade_phase():
            broker.process_bar()
            continue
        scans += 1
        eval_scans += 1
        if progress_cb is not None and scans % 50 == 0:
            progress_cb(scans, 0, clock.date_str())

        regime, effective_label, vix = _assess_regime(feed)
        breadth_ok, _breadth_note = _breadth_from_spy(feed, vix)

        # Kill switch — live refuses new entries in a BEAR tape.
        if effective_label == "BEAR":
            _skip("bear", None, "regime BEAR")
            broker.process_bar()
            continue
        # Breadth is ADVISORY unless BREADTH_BLOCKING (live's default is off).
        if not breadth_ok:
            _skip("breadth", None, _breadth_note)
            if settings.breadth_blocking:
                broker.process_bar()
                continue
        # No new names late on a Friday.
        if now.weekday() == 4 and now.hour * 60 + now.minute >= 14 * 60:
            broker.process_bar()
            continue

        base_thr = runtime_config.entry_threshold()
        thr = entry_threshold.resolve(base=base_thr, regime_label=effective_label,
                                      breadth_ok=breadth_ok)
        floor = thr.floor

        # -- score every name, exactly the four strategies live scores --
        ranked = []
        for sym in tickers:
            df = feed.get_kline(sym, bars=120)
            if df is None or df.empty or len(df) < 20:
                continue
            if len(df) >= 2:
                df = df.iloc[:-1]          # drop the forming bar, like live
            for evaluate, on in ((indicators.evaluate, True),
                                 (strategy_momentum.evaluate, True),
                                 (strategy_mr.evaluate, settings.mr_enabled),
                                 (strategy_pattern.evaluate, settings.pattern_enabled)):
                if not on:
                    continue
                try:
                    s = evaluate(sym, df)
                except Exception:
                    continue
                max_seen = max(max_seen, s.score)
                if s.score >= floor:
                    ranked.append(s)
        ranked.sort(key=lambda s: s.score, reverse=True)
        signals_found += len(ranked)

        # -- the funnel, in live's order --
        held = broker.held_symbols()
        new_names = 0
        equity = broker.equity()
        for sig in ranked:
            if sig.score < floor:
                break
            is_stack = sig.symbol in held
            if not is_stack and new_names >= settings.max_new_names_per_scan:
                continue
            required = thr.required(is_stack=is_stack,
                                    minutes_into_day=now.hour * 60 + now.minute)
            if sig.score < required and required > floor:
                _skip("late_entry", sig.symbol, f"needs {required}")
                continue
            if is_stack and sig.score < base_thr:
                continue
            if not is_stack and len(held) >= max_positions:
                _skip("max_positions", sig.symbol, f"{len(held)}/{max_positions}")
                continue
            # A name already held is a STACK candidate, not a skip. This line
            # used to `continue` unconditionally, so v4 modelled one lot where
            # live presses up to MAX_STACKS_PER_SYMBOL — under-deploying capital
            # into exactly the trades live is most confident in.
            held_pos = broker.positions.get(sig.symbol)
            if held_pos is not None and not cfg.model_pyramiding:
                continue
            if held_pos is not None:
                d = stacking.can_stack(
                    stacks=held_pos.stacks, entry=held_pos.entry,
                    last_px=float(getattr(sig, "price", 0.0) or held_pos.entry),
                    init_risk_per_share=held_pos.init_risk_per_share,
                    current_stop=held_pos.stop,
                    max_stacks=settings.max_stacks_per_symbol,
                    min_r=settings.stack_min_r_multiple)
                if not d:
                    _skip("stack_gate", sig.symbol, d.reason)
                    continue

            df_d = feed.get_kline(sig.symbol, bars=60, ktype=KLType.K_DAY)
            ok, gate, reason = entry_gates.daily_gates_ok(
                df_d, strategy=entry_gates.strategy_of(sig),
                max_gap_pct=settings.max_gap_pct,
                apply_mtf=(settings.timeframe == "HOUR_1"))
            if not ok:
                _skip(gate, sig.symbol, reason)
                continue

            if _cooldown_active(sig.symbol, broker, now):
                _skip("cooldown", sig.symbol, f"SL'd within {_COOLDOWN_MINUTES}min")
                continue
            if sig.symbol in bl_active:
                _skip("blacklist", sig.symbol, "on adaptive blacklist")
                continue
            blocked, why = _earnings_blocked(sig.symbol, now, earnings_cal)
            if blocked:
                _skip("earnings", sig.symbol, why)
                continue
            sp = _spread_pct(sig.symbol, feed)
            if sp > _SPREAD_MAX_PCT:
                _skip("spread", sig.symbol, f"bid-ask {sp:.2f}% > {_SPREAD_MAX_PCT}%")
                continue

            pos_rows = [{"code": f"US.{p.symbol}", "qty": p.qty,
                         "position_side": "LONG"} for p in broker.positions.values()]
            positions_df = (pd.DataFrame(pos_rows) if pos_rows
                            else pd.DataFrame(columns=["code", "qty", "position_side"]))
            try:
                ok, why = sector.check_sector_exposure(sig.symbol, positions_df, set())
                if not ok:
                    _skip("sector", sig.symbol, why)
                    continue
            except Exception:
                pass

            # -- sizing: THE shared rule, on mark-to-market equity --
            sig_df = feed.get_kline(sig.symbol, bars=120)
            if sig_df is None or sig_df.empty:
                continue
            atr = float(getattr(sig, "atr", 0.0) or sig_df["close"].iloc[-1] * 0.02)
            price = float(getattr(sig, "price", None) or sig_df["close"].iloc[-1])
            stop = float(getattr(sig, "stop_loss", None)
                         or (price - runtime_config.sl_atr_mult() * atr))
            tp = float(getattr(sig, "take_profit", None)
                       or (price + runtime_config.tp_atr_mult() * atr))
            if price <= 0 or stop <= 0:
                continue
            regime_mult = (settings.regime_bull_mult
                           if (regime is not None and regime.bullish
                               and vix < settings.regime_vix_calm) else 1.0)
            size = sizing_rule.resolve(
                capital=equity, risk_per_trade=runtime_config.risk_per_trade(),
                max_position_pct=runtime_config.max_position_pct(),
                price=price, stop_loss=stop, vix=vix, regime_mult=regime_mult)
            if not size:
                _skip("qty_zero", sig.symbol, size.reason)
                continue

            # HOW BIG THIS IDEA MAY BECOME, not how big this order may be.
            # Live has run this since 2026-06-26 and no engine modelled it. It
            # is what stops a stack cascade: "five stacks of 36% were five
            # separate legal decisions adding up to an illegal position,
            # stopped only by running out of cash." Without it v4 stacked 49
            # times over a window in which live stacked zero.
            book = {p.symbol: p.qty * p.entry for p in broker.positions.values()}
            ok, why = concentration.check_exposure(
                sig.symbol, size.qty * price, holdings=book, budget=cfg.account_usd)
            if ok:
                try:
                    # SimFeed exposes get_kline(sym, bars, ktype) — the same
                    # interface the live client does, so the correlation cap
                    # runs off replay bars with no adapter.
                    cl_syms = concentration.correlated_cluster(
                        feed, sig.symbol, holdings=book)
                    ok, why = concentration.check_cluster(
                        sig.symbol, size.qty * price, cl_syms,
                        holdings=book, budget=cfg.account_usd)
                except Exception:
                    pass          # fail-open, exactly as live does
            if not ok:
                _skip("concentration", sig.symbol, why)
                continue

            if held_pos is not None:
                # An add-on fills through the SAME TTL / chase / liquidity /
                # cash model as a fresh entry — a stack that always fills would
                # be the fiction this engine exists to remove.
                if not broker.try_stack(sig.symbol, price, stop, tp, size.qty, atr):
                    _skip("no_fill", sig.symbol, "stack: TTL expired / chase / cash")
                continue

            if broker.try_fill(sig.symbol, price, stop, tp, size.qty,
                               entry_gates.strategy_of(sig), sig.score, atr):
                if not is_stack:
                    new_names += 1
                    held.add(sig.symbol)
            else:
                _skip("no_fill", sig.symbol, "TTL expired / chase / cash")

        broker.process_bar()

    broker.close_all("EOD")
    return _report(cfg, broker, tickers, scans, eval_scans, signals_found,
                   max_seen, skip_counts, skip_events, _time.time() - t0,
                   rich_metrics=rich_metrics)


# ── Reporting ─────────────────────────────────────────────

def _fill_stats_snapshot() -> dict:
    from src.sim_feed import fill_stats
    return fill_stats()


def _report(cfg, broker, tickers, scans, eval_scans, signals_found,
            max_seen, skip_counts, skip_events, elapsed,
            rich_metrics: bool = True) -> dict:
    closed = broker.trades
    wins = [t for t in closed if t.net_pnl > 0]
    losses = [t for t in closed if t.net_pnl <= 0]
    net = sum(t.net_pnl for t in closed)
    gross_w = sum(t.net_pnl for t in wins)
    gross_l = abs(sum(t.net_pnl for t in losses))

    by_reason: dict[str, int] = defaultdict(int)
    for t in closed:
        by_reason[t.reason] += 1

    rs = sorted(t.r for t in closed)
    sl_rs = sorted(t.r for t in closed if t.reason in ("SL", "BREAKEVEN"))

    metrics = {
        "total_trades": len(closed),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(closed), 1) if closed else 0.0,
        "net_pnl_usd": round(net, 2),
        "total_return_pct": round(net / broker.start_capital * 100, 2),
        "avg_win_usd": round(gross_w / len(wins), 2) if wins else 0.0,
        "avg_loss_usd": round(-gross_l / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else 0.0,
        "max_dd_mtm_pct": round(broker.max_dd_mtm, 2),
        "median_r": round(rs[len(rs) // 2], 2) if rs else 0.0,
        "worst_r": round(rs[0], 2) if rs else 0.0,
        "median_sl_r": round(sl_rs[len(sl_rs) // 2], 2) if sl_rs else 0.0,
        "commission_usd": round(sum(t.commission for t in closed), 2),
        "slippage_usd": round(sum(t.slippage for t in closed), 2),
        "exit_reasons": dict(by_reason),
        # Execution telemetry — the numbers that say whether the fill model is
        # anywhere near the broker's measured behaviour.
        "orders_attempted": broker.n_orders,
        "orders_filled": broker.n_fills,
        "fill_rate_pct": round(100 * broker.n_fills / broker.n_orders, 1)
                         if broker.n_orders else 0.0,
        "ttl_expired": broker.n_ttl_expired,
        "stack_addons": broker.n_stacks,
        "cash_blocked": broker.n_cash_blocked,
        "cash_clipped": broker.n_cash_clipped,
        "ending_cash": round(broker.cash, 2),
        "ending_equity": round(broker.equity(), 2),
    }

    # Sharpe / Sortino / Calmar / Monte-Carlo, on the SAME shared metrics layer
    # every earlier engine used — so a v4 report drops into the GUI panel, the
    # weekly Telegram, and the autopilot's validation step unchanged. v4's own
    # keys win the merge: net_pnl_usd here is after commission, and
    # max_dd_mtm_pct is the mark-to-market drawdown the shared layer cannot
    # compute (it only sees closed trades).
    if rich_metrics and closed:
        from src.metrics import compute_full_metrics
        n_days = max(1, (cfg.end.date() - cfg.start.date()).days)
        shaped = [{"pnl": t.net_pnl, "pnl_pct": t.pnl_pct,
                   "entry_date": t.entry_t.date().isoformat(),
                   "exit_date": t.exit_t.date().isoformat(),
                   "symbol": t.symbol, "exit_reason": t.reason}
                  for t in closed]
        try:
            rich = compute_full_metrics(shaped, broker.start_capital, n_days)
            rich.update(metrics)
            metrics = rich
        except Exception as e:
            log.warning("rich metrics failed: %s", e)

    return {
        "version": "v4",
        "config": {
            "start": str(cfg.start.date()), "end": str(cfg.end.date()),
            "tickers_n": len(tickers), "tickers": list(tickers),
            "universe": cfg.universe_mode,
            "scan_interval_min": cfg.scan_interval_min or settings.scan_interval_min,
            "entry_threshold": runtime_config.entry_threshold(),
            "sl_atr_mult": runtime_config.sl_atr_mult(),
            "tp_atr_mult": runtime_config.tp_atr_mult(),
            "max_hold_days": runtime_config.max_hold_days(),
            "risk_per_trade": runtime_config.risk_per_trade(),
            "max_position_pct": runtime_config.max_position_pct(),
            "account_usd": cfg.account_usd,
            "enforce_cash": cfg.enforce_cash, "soft_stops": cfg.soft_stops,
            "entry_ttl": cfg.entry_ttl,
        },
        "metrics": metrics,
        "trades": [{
            "symbol": t.symbol, "entry": t.entry, "exit": t.exit, "qty": t.qty,
            "pnl": t.pnl, "net_pnl": t.net_pnl, "pnl_pct": round(t.pnl_pct, 2),
            "r": round(t.r, 3), "reason": t.reason, "strategy": t.strategy,
            "entry_t": t.entry_t.isoformat(), "exit_t": t.exit_t.isoformat(),
            "commission": t.commission, "slippage": t.slippage,
            "mfe": round(t.mfe, 2), "mae": round(t.mae, 2), "score": t.score,
        } for t in closed],
        "scans": scans, "eval_scans": eval_scans,
        "signals_found": signals_found, "max_score_seen": max_seen,
        "fill_stats": _fill_stats_snapshot(),
        "skip_counts": dict(skip_counts),
        "skip_events": skip_events,
        "elapsed_sec": round(elapsed, 1),
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="backtest_v4 — the single engine")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--universe", choices=["static", "dynamic"], default="static")
    ap.add_argument("--output", default=str(RESULTS_FILE))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s | %(message)s")
    if args.start and args.end:
        start = datetime.fromisoformat(args.start).replace(tzinfo=ET)
        end = datetime.fromisoformat(args.end).replace(hour=16, tzinfo=ET)
    else:
        end = datetime.now(ET).replace(hour=16, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=args.days)

    cfg = V4Config(start=start, end=end, tickers=args.tickers or [],
                   universe_mode=args.universe)
    result = run_v4(cfg)
    m = result["metrics"]
    print(f"\n{'='*64}")
    print(f"backtest_v4  {result['config']['start']} → {result['config']['end']}")
    print(f"{'='*64}")
    print(f"  Trades:      {m['total_trades']}  ({m['wins']}W / {m['losses']}L)")
    print(f"  Win rate:    {m['win_rate_pct']}%")
    print(f"  Net PnL:     ${m['net_pnl_usd']:+,.2f}  ({m['total_return_pct']:+.2f}%)")
    print(f"  Profit fac:  {m['profit_factor']}")
    print(f"  Max DD MTM:  {m['max_dd_mtm_pct']}%")
    print(f"  R: median {m['median_r']}  worst {m['worst_r']}  SL-median {m['median_sl_r']}")
    print(f"  Fills:       {m['orders_filled']}/{m['orders_attempted']} "
          f"({m['fill_rate_pct']}%)  TTL-expired {m['ttl_expired']}")
    print(f"  Exits:       {m['exit_reasons']}")
    print(f"  Elapsed:     {result['elapsed_sec']}s")
    Path(args.output).write_text(json.dumps(result, indent=2, default=str))
    print(f"\nSaved → {args.output}")


if __name__ == "__main__":
    main()
