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


def load_pool() -> list[str]:
    return json.loads(POOL_FILE.read_text())["tickers"]


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
                    sector_cap: int | None = None) -> list[str]:
    """Top-N pool names by 6-1 momentum using ONLY daily bars dated strictly
    before `asof`. Deterministic (momentum desc, then symbol asc for ties) so
    live and backtest reproduce each other exactly.

    sector_cap (2026-07-18, default settings.universe_sector_cap, 0 = off):
    at most this many names per sector bucket, filled in momentum order —
    when a bucket is full the walk continues deeper down the ranking. Pure
    momentum concentrated the whole list into one correlation cluster (15/15
    semis/enterprise-hardware) and the book crashed as a single trade on
    07-15. This is a diversification RULE, part of the replayable selection —
    not performance editing; the pool itself stays liquidity-only."""
    cutoff = pd.Timestamp(asof, tz="US/Eastern")
    scores: dict[str, float] = {}
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
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    if sector_cap is None:
        sector_cap = settings.universe_sector_cap
    if sector_cap <= 0:
        return [sym for sym, _ in ranked[:top_n]]
    from .sector import get_sector
    picked: list[str] = []
    counts: dict[str, int] = {}
    for sym, _ in ranked:
        if len(picked) >= top_n:
            break
        sec = get_sector(sym)
        # Unknown-sector names bypass the cap (same no-false-negatives policy
        # as check_sector_exposure); the whole pool is mapped today.
        if sec != "unknown" and counts.get(sec, 0) >= sector_cap:
            continue
        picked.append(sym)
        counts[sec] = counts.get(sec, 0) + 1
    return picked


def compute_live_universe(client, top_n: int | None = None) -> list[str]:
    """Live-side selection: fetch dailies for the whole pool via OpenD and rank
    as-of today (bars strictly before today = completed days only — the same
    information set the backtest uses)."""
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
    return select_universe(daily_by_sym, asof=date.today(), top_n=top_n)
