"""How far price runs over the next three days — the stop and target question.

WHY THIS IS CLIMATOLOGY AND NOT A MODEL

This module started as a ridge on the same features `signal_forecast` uses,
because a pooled study said forward excursion was predictable: holdout R^2 of
0.46 on range, 0.19 on adverse excursion, both beating a fitted persistence
baseline. Wiring that in would have been wrong, and the reason is worth keeping
written down.

That study trained and scored ACROSS symbols. Its R^2 was therefore mostly
cross-sectional — the model had learned that TSLA moves more than XLU, which is
true, useful for ranking, and completely beside the point here. A stop is a
per-symbol question: given THIS symbol, will the next three days be wider or
narrower than its own normal? Re-scoring the identical predictions with each
symbol's mean removed answers it:

    holdout, within-symbol      ridge    persistence
    mae96  (max adverse)      -0.0063        -0.0346
    mfe96  (max favourable)   -0.0071        -0.0245
    range96                   -0.0047        -0.0998

Every number is at or below zero. Zero is what you score by predicting the
symbol's own average, so nothing beats the symbol's own average. The ridge is
better than persistence, and still worth nothing over climatology.

So this module is the average. Specifically the empirical quantiles of the
symbol's own realized excursions, expanded point-in-time so no quantile is ever
computed from a bar the caller could not have seen.

WHAT IT REPLACES, AND WHY THAT MATTERED
  The scanner's plan set its stop at 1.15x ATR below an entry that already sat
  0.10 ATR under the close — about 1.25 ATR in total. ATR there is measured on
  THIRTY-MINUTE bars while the plan's horizon is three days, so the level was
  off by roughly the square root of 96. Measured over 4,980 purged origins on
  83 symbols: median stop distance 0.53% against a median actual adverse
  excursion of 2.38%, an 88% stop-out rate, and not one symbol of 83 within
  five points of a sensible rate. Climatology at q0.10 gives 15.06%.

CALIBRATION, MEASURED NOT ASSUMED
  stop   q0.10 -> hit 15.06% of the time, median distance 5.57%
  target q0.50 -> reached 55.56% of the time, median distance 2.32%
  The quantiles run slightly hot because a finite sample under-measures a fat
  tail; the levels below are the ones that came out right, not the ones that
  looked right.

RESEARCH ONLY. No execution path imports this module and `actionable` is never
set. The scanner shows these beside its existing ATR levels rather than in place
of them, so the difference accumulates as evidence before anything relies on it.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from .signal_forecast import (
    BACKTEST_EMBARGO_BARS, ForecastInputError, _build_features,
    _feature_origins, _validate_input,
)

STEPS = 96
METHOD = "point_in_time_empirical_quantiles"
VERSION = "1.0.0"
# Below this many prior non-overlapping observations the quantiles are noise and
# the module says nothing rather than something weak.
MIN_OBSERVATIONS = 20
STOP_QUANTILE = 0.10
TARGET_QUANTILE = 0.50


def _observations(frame) -> tuple[list[float], list[float]]:
    """Non-overlapping realized (adverse, favourable) excursions, oldest first.

    Non-overlapping matters as much here as it does in the walk-forward: 96-bar
    windows that share 95 bars are one observation wearing many hats, and a
    quantile taken over them would be far more confident than the evidence
    supports.
    """
    close = frame["close"].to_numpy(dtype=float)
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    total = len(close)
    origins = _feature_origins(_build_features(frame))

    schedule: list[int] = []
    boundary = total
    for origin in reversed([int(o) for o in origins if o + STEPS < total]):
        if origin + STEPS + BACKTEST_EMBARGO_BARS < boundary:
            schedule.append(origin)
            boundary = origin
    adverse: list[float] = []
    favourable: list[float] = []
    for origin in reversed(schedule):
        window = slice(origin + 1, origin + 1 + STEPS)
        reference = close[origin]
        if reference <= 0:
            continue
        adverse.append(float(low[window].min() / reference - 1.0))
        favourable.append(float(high[window].max() / reference - 1.0))
    return adverse, favourable


def _hit_rate(observations: list[float], quantile: float, adverse: bool) -> Optional[float]:
    """What the quantile ACTUALLY did on this symbol, expanding point-in-time.

    Reported alongside the level so the caller can see whether this particular
    symbol behaves like the pool average or not. A q0.10 stop that this symbol
    hit 40% of the time is a fact worth showing next to the level.
    """
    outcomes: list[bool] = []
    for index in range(MIN_OBSERVATIONS, len(observations)):
        prior = observations[:index]
        level = float(np.quantile(prior, quantile))
        value = observations[index]
        outcomes.append(value < level if adverse else value >= level)
    if not outcomes:
        return None
    return round(float(np.mean(outcomes)) * 100.0, 2)


def estimate(*, symbol: str, bars_30m: list[dict[str, Any]],
             market_as_of: str) -> dict[str, Any]:
    """Stop and target levels from the symbol's own excursion history.

    `abstained` is True when there is not enough non-overlapping history for the
    quantiles to mean anything. The caller keeps whatever it was using.
    """
    data = _validate_input(symbol, "3D", bars_30m, market_as_of,
                           trim_latest_sessions=False)
    frame = data.frame
    adverse, favourable = _observations(frame)
    last_close = float(frame["close"].iloc[-1])
    if last_close <= 0:
        raise ForecastInputError("invalid_ohlc", "最后一根K线收盘价无效。")

    payload: dict[str, Any] = {
        "symbol": data.symbol,
        "market_as_of": market_as_of,
        "steps": STEPS,
        "last_close": round(last_close, 6),
        "method": METHOD,
        "version": VERSION,
        "observations": len(adverse),
        "actionable": False,
    }
    if len(adverse) < MIN_OBSERVATIONS or len(favourable) < MIN_OBSERVATIONS:
        payload.update({
            "abstained": True,
            "reason": (f"只有 {len(adverse)} 个非重叠三日样本，少于 {MIN_OBSERVATIONS} 个；"
                       "不给出止损/目标，调用方沿用原有 ATR 口径。"),
        })
        return payload

    stop_percent = float(np.quantile(adverse, STOP_QUANTILE)) * 100.0
    target_percent = float(np.quantile(favourable, TARGET_QUANTILE)) * 100.0
    stop = last_close * (1.0 + stop_percent / 100.0)
    target = last_close * (1.0 + target_percent / 100.0)
    if not (0 < stop < last_close < target):
        payload.update({"abstained": True,
                        "reason": "分位数给出的止损/目标顺序不合法，已放弃。"})
        return payload

    payload.update({
        "abstained": False,
        "stop": round(stop, 4),
        "target": round(target, 4),
        "stop_percent": round(stop_percent, 4),
        "target_percent": round(target_percent, 4),
        "reward_risk": round((target - last_close) / (last_close - stop), 3),
        "quantiles": {"stop": STOP_QUANTILE, "target": TARGET_QUANTILE},
        "median_adverse_percent": round(float(np.median(adverse)) * 100.0, 4),
        "median_favourable_percent": round(float(np.median(favourable)) * 100.0, 4),
        "realized": {
            "stop_hit_percent": _hit_rate(adverse, STOP_QUANTILE, adverse=True),
            "target_reached_percent": _hit_rate(favourable, TARGET_QUANTILE, adverse=False),
            "pool_stop_hit_percent": 15.06,
            "pool_target_reached_percent": 55.56,
        },
        "basis": "该标的自身非重叠三日实际波动的时点经验分位数",
    })
    return payload


def compare_to_atr(estimate_payload: dict[str, Any], atr_stop: Optional[float],
                   atr_target: Optional[float]) -> Optional[dict[str, Any]]:
    """Side-by-side deltas, for accumulating evidence before anything switches."""
    if estimate_payload.get("abstained") or not atr_stop or not atr_target:
        return None
    close = float(estimate_payload["last_close"])
    stop = float(estimate_payload["stop"])
    target = float(estimate_payload["target"])
    return {
        "atr_stop_distance_percent": round((atr_stop / close - 1.0) * 100.0, 4),
        "model_stop_distance_percent": round((stop / close - 1.0) * 100.0, 4),
        "stop_distance_ratio": round((close - atr_stop) / (close - stop), 3)
        if close > stop else None,
        "atr_target_distance_percent": round((atr_target / close - 1.0) * 100.0, 4),
        "model_target_distance_percent": round((target / close - 1.0) * 100.0, 4),
        "note": ("ATR 口径在30分钟K线上度量，却用于三日窗口；"
                 "样本外实测其止损触发率 88%，此处并列展示，不替换。"),
    }


__all__ = ["METHOD", "STEPS", "VERSION", "compare_to_atr", "estimate"]
