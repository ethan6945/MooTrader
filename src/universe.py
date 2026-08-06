"""Rule-based dynamic trading universe (Phase 1, 2026-06-11).

WHY. The previous watchlist was hand-pinned to "the 10 winners" — names picked
because they backtested best over the same history the backtest was then scored
on. That is in-sample selection: the headline $/day partly echoed its own
universe choice, and the list decayed live as the picked momentum ran out
(measured: −$3.4/day over the most recent 23d). This module replaces the hand
pick with a RULE that can be replayed point-in-time:

    universe(asof) = top N of the liquidity pool by 6-1 momentum
                     (126-trading-day return, skipping the most recent 21)

6-1 / 12-1 cross-sectional momentum is the most replicated selection factor in
the equity literature (Jegadeesh-Titman 1993 and hundreds of follow-ups); the
skip month avoids the documented short-term reversal. No parameter here was
fitted to our own backtest history.

HONESTY RULES
  • The pool (config/universe_pool.json) is liquidity-selected, NEVER
    performance-edited.
  • select_universe(asof) reads daily bars STRICTLY BEFORE asof — the backtest
    replays the exact decision the live bot would have made that week.
  • Live refresh is flag-gated (DYNAMIC_UNIVERSE_ENABLED) and every change is
    Telegram-notified — no silent universe drift.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd

from .config import settings

log = logging.getLogger(__name__)

POOL_FILE = settings.root / "config" / "universe_pool.json"

# Selection rule constants — literature-standard, not fitted to our history.
LOOKBACK_D = 126          # ~6 months of trading days
SKIP_D = 21               # ~1 month skip (short-term reversal)
MIN_PRICE = 10.0          # penny/low-price guard
MIN_BARS = LOOKBACK_D + SKIP_D + 10
MIN_DOLLAR_VOL = 25e6     # 20d avg $ volume floor — a data-sanity guard only;
                          # every intended pool member clears it by orders of
                          # magnitude

# ── Affordability (2026-07-27) ────────────────────────────────────────────────
# Momentum ranking has no notion of whether the account can size a name at all,
# so a $1,300 or very-high-ATR ticker can win a watchlist slot it could never
# fill. This is a STRUCTURAL guard against that — deliberately NOT a fix for
# temporary un-sizeability: the 5 names that showed qty=0 on 2026-07-27 (SNDK,
# MU, STX, WDC, AMD) were blocked by protect-mode sizing (state_mult 0.25), and
# all 5 size to 1-3 shares at full risk budget, so excluding them from the
# universe would have been wrong. See Affordability on why state_mult is
# excluded. Against the 2026-07-27 72-name pool this filter drops nothing —
# it exists so a shrinking account or a runaway price can't quietly fill the
# watchlist with slots the bot cannot use.
#
# It drops a name only when untradeable in the MOST FAVOURABLE case, so it can
# never silently exclude something the account could actually have bought
# (verified: 0 false exclusions across all 72 pool names).
#
# Selection sees DAILY bars; sizing uses the strategy's H1 ATR. Measured over
# the 73-name cache, H1_ATR/DAY_ATR ran 0.204 … 0.520 (median 0.320). We assume
# the MINIMUM (= tightest plausible stop = easiest to size); anything still
# unsizeable under that assumption is hopeless on any timeframe.
H1_ATR_OVER_DAY_ATR_MIN = 0.20


@dataclass(frozen=True)
class Affordability:
    """Just enough of the sizing model to answer "could this ever be bought?".

    Mirrors the two qty formulas in risk_manager.calc_position_size. NOTE:
    state_mult (DD cut × adaptive multiplier) is deliberately NOT applied —
    those are transient, and letting them drive the WEEKLY universe would make
    the watchlist churn every time drawdown crosses a threshold. Protect-mode
    still declines individual entries at order time; this only removes names
    that are untradeable even at full risk budget."""
    capital: float
    risk_per_trade: float
    max_position_pct: float
    sl_atr_mult: float

    def can_size(self, price: float, day_atr: float | None) -> bool:
        if price <= 0:
            return False
        # Leg 1 — per-name notional cap: one share must fit under the cap.
        if int(self.capital * self.max_position_pct / price) < 1:
            return False
        # Leg 2 — risk budget: one share's stop distance must fit inside it.
        # Unknown/zero ATR → don't block (no evidence to exclude on).
        if not day_atr or day_atr <= 0:
            return True
        stop_distance = self.sl_atr_mult * day_atr * H1_ATR_OVER_DAY_ATR_MIN
        if stop_distance <= 0:
            return True
        risk_dollars = self.capital * self.risk_per_trade
        return int(risk_dollars / stop_distance) >= 1

    @classmethod
    def live(cls) -> "Affordability":
        """Build from the account's runtime-effective values."""
        from . import risk_manager, runtime_config
        return cls(capital=risk_manager.sizing_capital(),
                   risk_per_trade=runtime_config.risk_per_trade(),
                   max_position_pct=runtime_config.max_position_pct(),
                   sl_atr_mult=runtime_config.sl_atr_mult())

    @classmethod
    def from_cfg(cls, cfg) -> "Affordability":
        """Build from a BacktestConfig so the replay filters identically."""
        return cls(capital=float(cfg.account_usd),
                   risk_per_trade=float(cfg.risk_per_trade),
                   max_position_pct=float(cfg.max_position_pct),
                   sl_atr_mult=float(cfg.sl_atr_mult))


def _day_atr(d: pd.DataFrame, n: int = 14) -> float | None:
    """Wilder-style ATR (simple rolling mean of true range) on daily bars."""
    if "high" not in d or "low" not in d or len(d) < n + 1:
        return None
    try:
        h, l, c = d["high"].astype(float), d["low"].astype(float), d["close"].astype(float)
        pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        v = float(tr.rolling(n).mean().iloc[-1])
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def load_pool() -> list[str]:
    return json.loads(POOL_FILE.read_text())["tickers"]


def load_etfs() -> set[str]:
    """The pool members that are ETFs, from the pool file's own "etfs" key.

    Kept as data next to the tickers rather than a constant in code, so adding a
    fund to the pool and marking it as one is a single edit. Missing key -> empty
    set, which makes the ETF sleeve inert rather than wrong.
    """
    try:
        return {s.upper() for s in json.loads(POOL_FILE.read_text()).get("etfs", [])}
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        return set()


def momentum_6_1(closes: pd.Series) -> float | None:
    """6-1 momentum: return over [t-147, t-21]. None = not computable."""
    if len(closes) < MIN_BARS:
        return None
    c_skip = float(closes.iloc[-1 - SKIP_D])
    c_lb = float(closes.iloc[-1 - SKIP_D - LOOKBACK_D])
    if c_lb <= 0:
        return None
    return c_skip / c_lb - 1.0


def select_universe(daily_by_sym: dict[str, pd.DataFrame | None],
                    asof: date,
                    top_n: int,
                    sector_cap: int | None = None,
                    afford: "Affordability | None" = None,
                    etf_slots: int | None = None,
                    previous: list[str] | None = None,
                    exit_rank: int | None = None) -> list[str]:
    """Top-N pool names by 6-1 momentum using ONLY daily bars dated strictly
    before `asof`. Deterministic (momentum desc, then symbol asc for ties) so
    live and backtest reproduce each other exactly.

    sector_cap (2026-07-18, default settings.universe_sector_cap, 0 = off):
    at most this many names per sector bucket, filled in momentum order —
    when a bucket is full the walk continues deeper down the ranking. Pure
    momentum concentrated the whole list into one correlation cluster (15/15
    semis/enterprise-hardware) and the book crashed as a single trade on
    07-15. This is a diversification RULE, part of the replayable selection —
    not performance editing; the pool itself stays liquidity-only.

    afford (2026-07-27, None = off): drop names this account cannot size at all
    — see Affordability. Part of the replayable rule like sector_cap, so live
    and backtest must pass an equivalent object or they diverge.

    etf_slots (2026-08-05, default settings.universe_etf_slots, 0 = off):
    reserve this many of the top_n slots for pool members marked as ETFs in
    universe_pool.json, filled in the same momentum order. Fixes position
    GRANULARITY, not signal — see the inline note at the sleeve.

    previous / exit_rank (2026-08-06, hysteresis; exit_rank<=top_n = off):
    an incumbent keeps its slot until it falls past `exit_rank`, so entry needs
    rank<=top_n but exit needs rank>exit_rank. Enables DAILY refresh without the
    boundary churn: measured over 40 sessions, refreshing daily with no
    hysteresis cost 45 name-changes to achieve 8 net rotations (5.6x churn),
    with names ping-ponging in and out on consecutive days (BA/MS, CVX/MS,
    INTC/WDC). Weekly got the same 8 rotations in 16 changes.

    NOTE — this makes selection PATH-DEPENDENT: the result is no longer a pure
    function of `asof`, it depends on what was held last time. Live persists that
    in config/watchlist.json; any backtest MUST thread its own previous set
    forward the same way, or the replay silently stops matching live."""
    cutoff = pd.Timestamp(asof, tz="US/Eastern")
    scores: dict[str, float] = {}
    unaffordable: list[str] = []
    for sym, df in daily_by_sym.items():
        if df is None or df.empty or "close" not in df:
            continue
        try:
            d = df.loc[df.index < cutoff]
        except TypeError:
            d = df.loc[df.index < pd.Timestamp(asof)]
        if len(d) < MIN_BARS:
            continue
        closes = d["close"].astype(float)
        last = float(closes.iloc[-1])
        if last < MIN_PRICE:
            continue
        if afford is not None and not afford.can_size(last, _day_atr(d)):
            unaffordable.append(sym)
            continue
        if "volume" in d:
            try:
                dv = float((d["close"].astype(float)
                            * d["volume"].astype(float)).tail(20).mean())
                if dv < MIN_DOLLAR_VOL:
                    continue
            except (TypeError, ValueError):
                pass   # bad volume data → don't block on the sanity guard
        mom = momentum_6_1(closes)
        if mom is not None:
            scores[sym] = mom
    if unaffordable:
        log.info("universe: %d name(s) excluded as un-sizeable on $%.0f "
                 "(cap %.0f%%, risk %.1f%%): %s", len(unaffordable),
                 afford.capital, afford.max_position_pct * 100,
                 afford.risk_per_trade * 100, ", ".join(sorted(unaffordable)))
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

    # ETF sleeve (2026-08-05, default 0 = off). Momentum ranking is blind to
    # whether the account can express what it picks: on $10k with a 20% cap the
    # top-15 has repeatedly been $500-$1,400 names where one share is 9-14% of
    # the book and the ATR-derived share count floors to 1 or 0 — the position is
    # then all-or-nothing and the risk model stops meaning anything. Reserving
    # slots for the (liquid, low-priced) sector funds keeps the same momentum
    # exposure in an instrument the account can actually size.
    # This is a diversification/expressibility RULE like sector_cap — it is part
    # of the replayable selection and applies identically in live and backtest.
    # It never performance-edits: which ETF gets a reserved slot is decided by
    # the same 6-1 momentum ranking as everything else.
    if etf_slots is None:
        etf_slots = settings.universe_etf_slots
    reserved: list[str] = []
    if etf_slots > 0:
        etfs = load_etfs()
        reserved = [s for s, _ in ranked if s in etfs][:min(etf_slots, top_n)]

    # Hysteresis. Incumbents that still rank within exit_rank are carried
    # forward AHEAD of fresh entrants; only a name that has genuinely fallen out
    # of the wider band loses its slot. Order within the carried set stays
    # momentum order, so the result is still deterministic.
    if exit_rank is None:
        exit_rank = settings.universe_exit_rank
    rank_of = {sym: i for i, (sym, _) in enumerate(ranked)}
    held: list[str] = []
    if previous and exit_rank > top_n:
        held = sorted((s for s in previous
                       if s in rank_of and rank_of[s] < exit_rank and s not in reserved),
                      key=lambda s: rank_of[s])

    if sector_cap is None:
        sector_cap = settings.universe_sector_cap
    if sector_cap <= 0:
        keep = reserved + held
        rest = [sym for sym, _ in ranked if sym not in keep]
        return (keep + rest)[:top_n]
    from .sector import get_sector
    picked: list[str] = list(reserved)
    # Incumbents are placed before new entrants but still obey the sector cap —
    # hysteresis must not become a back door around the concentration limit that
    # exists because a 15/15 semis book crashed as one trade on 2026-07-15.
    _counts_pre: dict[str, int] = {}
    for sym in picked:
        _counts_pre[get_sector(sym)] = _counts_pre.get(get_sector(sym), 0) + 1
    for sym in held:
        if len(picked) >= top_n:
            break
        sec = get_sector(sym)
        if sec != "unknown" and _counts_pre.get(sec, 0) >= sector_cap:
            continue
        picked.append(sym)
        _counts_pre[sec] = _counts_pre.get(sec, 0) + 1
    counts = _counts_pre
    for sym, _ in ranked:
        if len(picked) >= top_n:
            break
        if sym in picked:
            continue
        sec = get_sector(sym)
        # Unknown-sector names bypass the cap (same no-false-negatives policy
        # as check_sector_exposure); the whole pool is mapped today.
        if sec != "unknown" and counts.get(sec, 0) >= sector_cap:
            continue
        picked.append(sym)
        counts[sec] = counts.get(sec, 0) + 1
    return picked


def compute_live_universe(client, top_n: int | None = None,
                          previous: list[str] | None = None) -> list[str]:
    """Live-side selection: fetch dailies for the whole pool via OpenD and rank
    as-of today (bars strictly before today = completed days only — the same
    information set the backtest uses).

    `previous` carries the hysteresis state — the currently shipped watchlist.
    Omitting it silently disables hysteresis and reverts to pure top-N, which
    would put live back out of step with a backtest that threads it through."""
    from moomoo import KLType
    from . import runtime_config
    top_n = top_n or runtime_config.universe_top_n()
    pool = load_pool()
    daily_by_sym: dict[str, pd.DataFrame | None] = {}
    for sym in pool:
        try:
            daily_by_sym[sym] = client.get_kline(sym, bars=MIN_BARS + 40,
                                                 ktype=KLType.K_DAY)
        except Exception as e:
            log.warning("universe: daily fetch failed for %s: %s — excluded", sym, e)
            daily_by_sym[sym] = None
    return select_universe(daily_by_sym, asof=date.today(), top_n=top_n,
                           afford=Affordability.live(), previous=previous)
