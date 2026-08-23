"""The daily-bar entry gates, defined once.

WHY THIS EXISTS

  `indicators.check_gap` takes a `block_up_gaps` flag that decides whether a
  gap-UP disqualifies an entry. The answer depends on the strategy: a trend or
  momentum-breakout signal WANTS the gap-up — it IS the breakout — while mean
  reversion and pattern signals should refuse to chase one.

  live passes that flag. The sandbox passes it. Both backtest engines called
  `check_gap(...)` without it and took the default, which is `True` — so they
  blocked exactly the entries live takes. The audit that found this measured
  the cost the other way round first: 76 of 76 gap skips in live trading were
  positive gaps on scores 72-90, which is why live stopped blocking them.

  An engine that refuses trades live takes is not a conservative engine, it is
  a different strategy — and it is the engine whose numbers the optimizer and
  the autopilot propose live parameters from.

  So the flag is not a caller's choice any more. It is derived from the signal,
  here, and every caller gets the same answer.
"""
from __future__ import annotations

import pandas as pd

from .indicators import check_gap, daily_trend_bullish

# The strategies for which a gap-up is the signal rather than a warning.
# Anything else (mean reversion, pattern) is chasing when it buys one.
TREND_LIKE = ("trend", "momentum_break")


def strategy_of(sig) -> str:
    """The signal's strategy label, defaulting the way live defaults it."""
    return getattr(sig, "strategy", "trend") or "trend"


def blocks_up_gaps(strategy: str) -> bool:
    """Whether a positive overnight gap disqualifies THIS strategy's entry."""
    return strategy not in TREND_LIKE


def gap_ok(daily_df: pd.DataFrame, *, strategy: str,
           max_gap_pct: float) -> tuple[bool, str]:
    """The directional overnight-gap filter. Gap-DOWNS are always blocked."""
    return check_gap(daily_df, max_gap_pct=max_gap_pct,
                     block_up_gaps=blocks_up_gaps(strategy))


def mtf_ok(daily_df: pd.DataFrame) -> tuple[bool, str]:
    """Multi-timeframe confirmation: the daily trend must agree with the entry."""
    return daily_trend_bullish(daily_df)


def daily_gates_ok(daily_df: pd.DataFrame, *, strategy: str, max_gap_pct: float,
                   apply_mtf: bool = True,
                   apply_gap: bool = True) -> tuple[bool, str, str]:
    """Both daily-bar gates in live's order. Returns (ok, gate, reason).

    `gate` names which one refused ("mtf" / "gap") so a replay can attribute a
    skip without re-deriving it, and is "" when both pass.
    """
    if daily_df is None or daily_df.empty or len(daily_df) < 2:
        return True, "", "insufficient daily bars — passing"
    if apply_mtf:
        ok, reason = mtf_ok(daily_df)
        if not ok:
            return False, "mtf", reason
    if apply_gap:
        ok, reason = gap_ok(daily_df, strategy=strategy, max_gap_pct=max_gap_pct)
        if not ok:
            return False, "gap", reason
    return True, "", ""
