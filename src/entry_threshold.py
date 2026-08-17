"""The score a candidate must clear, defined once.

WHY THIS EXISTS

  Live raised and lowered the entry bar in four places — a regime discount, a
  breadth condition on that discount, a news-driven floor, and a late-session
  premium. The sandbox reimplemented two of them and got the breadth condition
  wrong by omitting it. backtest_v3 implemented none and compared every score
  against a flat cfg.threshold.

  So the three of them looked at different candidate sets, which is most of why
  only a quarter of their trades ever matched. And it is the wrong quarter to
  be missing: v3's numbers are what the optimizer and the autopilot propose
  parameters from, so a v3 that admits candidates live would refuse — or
  refuses ones live would take — is tuning for a strategy nobody runs.

  This is the whole rule, in one place, with the inputs named. A caller that
  cannot supply one has to say so rather than quietly assume it.

THE BREADTH CONDITION IS NOT OPTIONAL
  The BULL discount is withheld when breadth is unhealthy. That is not a
  refinement: on 2026-07-27 the hysteresis label was still BULL while SPY sat
  below its 50-day and only 42% of names were above theirs, the discount pulled
  the bar from 75 to 65, and a 65.2 candidate got in — 17.9% win rate, 12%
  account drawdown. Loosening while the tape deteriorates is the specific
  mistake the condition exists to prevent, and an engine that omits it is
  modelling a strategy that made that mistake.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

BULL_DISCOUNT = 5.0
NEUTRAL_PREMIUM = 5.0
BULL_FLOOR = 55.0
NEUTRAL_CEILING = 85.0

# After this time, a brand-new name has to clear a higher bar: shrinking
# liquidity, wider spreads, and a full overnight gap before the thesis has any
# time to work. Stacks are exempt — they have already survived a session.
LATE_SESSION_MINUTE = 14 * 60
LATE_PREMIUM = 8.0
LATE_CEILING = 88.0


@dataclass(frozen=True)
class Threshold:
    """The bar, and why it is where it is."""
    floor: float          # ranking floor — what any candidate must clear
    reason: str

    def required(self, *, is_stack: bool, minutes_into_day: int) -> float:
        """The score THIS candidate must clear, at this time of day."""
        if is_stack or minutes_into_day < LATE_SESSION_MINUTE:
            return self.floor
        return min(LATE_CEILING, self.floor + LATE_PREMIUM)


def resolve(*, base: float, regime_label: str | None,
            breadth_ok: bool | None, news_floor=None) -> Threshold:
    """The entry floor for this bar.

    `breadth_ok=None` means the caller could not measure it. That withholds the
    BULL discount, which is the same thing live does when breadth is unhealthy
    — the conservative direction, and it is stated in the reason rather than
    hidden, because a run that could not see breadth is not the same as a run
    that saw it and found it fine.
    """
    label = (regime_label or "").upper()
    floor, why = float(base), "flat"

    if label == "BULL":
        if breadth_ok is True:
            floor = max(BULL_FLOOR, base - BULL_DISCOUNT)
            why = f"BULL discount −{BULL_DISCOUNT:g} (breadth healthy)"
        elif breadth_ok is False:
            why = "BULL, discount withheld — breadth unhealthy"
        else:
            why = "BULL, discount withheld — breadth not measurable here"
    elif label == "NEUTRAL":
        floor = min(NEUTRAL_CEILING, base + NEUTRAL_PREMIUM)
        why = f"NEUTRAL premium +{NEUTRAL_PREMIUM:g}"
    elif label == "BEAR":
        # The regime kill-switch blocks entries outright, so the number here is
        # moot; it is left at base rather than invented.
        why = "BEAR (entries blocked upstream)"

    if news_floor is not None:
        try:
            adjusted = float(news_floor(floor))
            if adjusted != floor:
                why += f", news floor {floor:.0f}→{adjusted:.0f}"
                floor = adjusted
        except Exception as e:
            log.warning("news-driven floor failed (%s) — keeping %.0f", e, floor)

    return Threshold(floor=floor, reason=why)


def breadth_ok_from_spy(spy_daily, vix: float = 0.0,
                        vix_is_real: bool = False) -> bool | None:
    """Live's breadth verdict, from SPY daily bars alone.

    Extracted so the engines can answer the same question from the same
    numbers. Returns None when there are not enough bars to judge — which the
    resolver treats as "no discount", the same direction live takes when
    breadth is unhealthy.

    The formula is live's: ten-day up-day ratio, SPY's distance from its
    50-day, and a VIX panic override.
    """
    try:
        import pandas as _pd
        from . import breadth as _breadth
        if spy_daily is None or len(spy_daily) < 50:
            return None
        close = _pd.Series(spy_daily["close"]).astype(float)
        open_ = _pd.Series(spy_daily["open"]).astype(float)
        sma50 = float(close.rolling(50).mean().iloc[-1])
        price = float(close.iloc[-1])
        n = len(close)
        days_up = sum(1 for i in range(-10, 0)
                      if i + n >= 0 and close.iloc[i] > open_.iloc[i])
        ad_ratio = days_up / 10
        vix_ok = (vix < _breadth.VIX_PANIC) if (vix_is_real and vix > 0) else True
        return bool(ad_ratio >= 0.55 and (price / sma50 - 1) * 100 > -10 and vix_ok)
    except Exception as e:
        log.debug("breadth from SPY failed: %s", e)
        return None
