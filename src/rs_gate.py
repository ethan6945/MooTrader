"""Relative strength: does this name beat the market it is being bought in?

WHY IT EXISTS

  The 2026-06-24 → 07-24 window lost $676 across 22 trades at a 13.6% win rate,
  and four hypotheses for it were tested and three refuted:

    concentration      peak concurrent exposure was 4 symbols, 2 of them semis
    sector-regime EMA  would have blocked 0 of the 22 entries — SOXX fell 12%
                       while its EMA20 was still above its EMA50
    entry score        median 66.9, against 65.2 in the window that MADE money

  The two windows are nearly indistinguishable by signal. What differed is the
  tape (SOXX -12.4% against -0.9%) and the activity (31 buys against 11): the
  strategy fired three times as often into a universe that was falling, because
  nothing it runs asks whether the things it trades are going down.

  This is the narrowest mechanism that asks. A long entry into a name that is
  underperforming the index over the last month is a bet against the only trend
  the strategy claims to follow.

WHY IT DEFAULTS TO OFF

  On those two windows, requiring excess return >= 0 would have cut the loss
  from $774 to $425. That number is 31 trades, in-sample, and chosen by looking
  at the outcome — which is how a curve gets fit. It also blocked three WINNERS
  in the profitable window.

  So the default floor is low enough to be inert, and the value is a tunable:
  the weekly sweep proposes it from multiple independent windows on
  expectancy-per-trade, and the owner approves it, exactly like every other
  parameter. A filter chosen from one look at one month is not evidence, and
  hardcoding it here would launder it into one.
"""
from __future__ import annotations

import pandas as pd

# The lookback the ranking layer already uses — roughly one trading month.
# Keeping the two in step means "strong" means one thing in this codebase.
from .relative_strength import RS_LOOKBACK_DAYS

# A floor at or below this is treated as "no opinion": a name would have to
# collapse against the index for it to bite, which is not a filter, it is a
# circuit breaker that happens to live here.
INERT_BELOW_PCT = -9.99


def excess_return_pct(sym_daily: pd.DataFrame, index_daily: pd.DataFrame,
                      lookback: int = RS_LOOKBACK_DAYS) -> float | None:
    """`lookback`-day return of the name minus the index's, in percentage points.

    None when either series is too short — an unmeasurable relative strength is
    not weakness, and this gate fails OPEN on it.
    """
    def _ret(df):
        if df is None or len(df) < lookback + 1:
            return None
        c = df["close"]
        prev = float(c.iloc[-1 - lookback])
        if prev <= 0:
            return None
        return (float(c.iloc[-1]) / prev - 1.0) * 100.0

    a, b = _ret(sym_daily), _ret(index_daily)
    if a is None or b is None:
        return None
    return a - b


def passes(sym_daily: pd.DataFrame, index_daily: pd.DataFrame, *,
           min_pct: float, lookback: int = RS_LOOKBACK_DAYS) -> tuple[bool, str]:
    """(ok, reason). Fails OPEN on missing data and when the floor is inert."""
    if min_pct <= INERT_BELOW_PCT:
        return True, ""
    excess = excess_return_pct(sym_daily, index_daily, lookback)
    if excess is None:
        return True, f"RS n/a — <{lookback + 1} daily bars (pass)"
    if excess < min_pct:
        return False, (f"{lookback}d excess {excess:+.2f}pp < {min_pct:+.2f}pp "
                       f"— underperforming the index it is bought in")
    return True, f"{lookback}d excess {excess:+.2f}pp"
