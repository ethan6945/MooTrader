"""Deterministic, leakage-aware research forecast baseline for 30-minute bars.

Ported from the Stock Probability Prediction Platform's
`companion/forecast_engine.py` as part of merging that tool into the signal
desk. The maths is unchanged so a forecast produced here can be compared
bar-for-bar with one the platform produced; what changed is the data source
(the broker's ETH session via `signal_bars`, not yfinance) and therefore the
data-quality warnings at the bottom of `generate_forecast`.

This module is deliberately pure: it performs no file, network or process I/O.
It implements a direct multi-horizon ridge baseline and expanding-window
walk-forward diagnostics.

RESEARCH ONLY, AND THAT IS LOAD-BEARING. `quality.actionable` is hard-wired to
False and there is no code path that sets it True. This project executes real
orders; a probability that looks confident is exactly the kind of number that
drifts into a sizing decision. Nothing in `src/executor.py`, `order_gate.py` or
any strategy module imports this file, and that separation is the safety
property — not an oversight to be tidied up later.

WHAT THE MODEL IS. A ridge regression predicting each of the next 96 log-return
steps directly (not recursively) from 28 features of the last 10 sessions, with
class probabilities read off a normal fitted to the training residuals. It is a
baseline. It has no view on news, earnings, or anything outside the price and
volume path it was handed.
"""
from __future__ import annotations

import math
import numbers
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from .signal_calendar import NEW_YORK, future_30m_slots


SCHEMA_VERSION = "1.0"
MODEL_NAME = "ridge_direct_30m_full_session"
MODEL_VERSION = "1.3.0"
MODEL_KIND = "deterministic_ridge_direct_full_session_research_baseline"
HORIZON_STEPS = {"3D": 96}
ANALYSIS_SESSION_DAYS = 10
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")
FEATURE_LOOKBACK = 64
MIN_FINAL_TRAIN = 50
MIN_WALK_FORWARD_TRAIN = 10
MIN_BACKTEST_SAMPLES = 10
# Walk-forward samples needed before their spread is trusted to set the width of
# the predicted distribution. Below this the estimator falls back to in-sample
# residuals and says so, because a standard deviation of four numbers is not a
# better answer than the one it replaces.
MIN_CALIBRATION_SAMPLES = 8
# q10/q90 is an 80% band; for a normal that is +/- 1.2816 sigma.
Q80_Z = 1.2815515655446004
MAX_BACKTEST_SAMPLES = 32
BACKTEST_EMBARGO_BARS = 1
RIDGE_ALPHA = 6.0
FULL_SESSION_30M_SLOTS = {
    (4 + ((slot * 30) // 60), (slot * 30) % 60)
    for slot in range(32)
}


class ForecastInputError(ValueError):
    """Fail-closed input error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedMarketData:
    symbol: str
    horizon: str
    steps: int
    frame: pd.DataFrame
    market_as_of: pd.Timestamp


@dataclass(frozen=True)
class RidgeFit:
    x_mean: np.ndarray
    x_scale: np.ndarray
    y_mean: np.ndarray
    coefficients: np.ndarray
    residuals: np.ndarray

    def predict(self, features: np.ndarray) -> np.ndarray:
        standardized = np.clip((features - self.x_mean) / self.x_scale, -8.0, 8.0)
        return self.y_mean + standardized @ self.coefficients


def minimum_bars(horizon: str) -> int:
    steps = HORIZON_STEPS.get(horizon)
    if steps is None:
        raise ForecastInputError("invalid_horizon", "horizon 仅支持 3D。")
    return max(240, FEATURE_LOOKBACK + steps + MIN_FINAL_TRAIN + 1)


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ForecastInputError("invalid_bar", f"{field} 必须是有限数值。")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ForecastInputError("invalid_bar", f"{field} 不得包含 NaN 或 Infinity。")
    return numeric


def _aware_timestamp(value: Any, field: str) -> pd.Timestamp:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ForecastInputError("invalid_timestamp", f"{field} 必须是带时区的 ISO-8601 时间。")
    try:
        timestamp = pd.Timestamp(value)
    except Exception as exc:
        raise ForecastInputError("invalid_timestamp", f"{field} 不是有效时间。") from exc
    if timestamp.tzinfo is None:
        raise ForecastInputError("invalid_timestamp", f"{field} 必须包含时区。")
    return timestamp.tz_convert(NEW_YORK)


def _validate_input(
    symbol: Any,
    horizon: Any,
    bars_30m: Any,
    market_as_of: Any,
    *,
    trim_latest_sessions: bool = True,
) -> ValidatedMarketData:
    normalized_symbol = str(symbol).strip().upper() if isinstance(symbol, str) else ""
    if (
        not SYMBOL_PATTERN.fullmatch(normalized_symbol)
        or ".." in normalized_symbol
        or normalized_symbol.endswith((".", "-"))
    ):
        raise ForecastInputError("invalid_symbol", "股票代码格式无效。")
    if horizon not in HORIZON_STEPS:
        raise ForecastInputError("invalid_horizon", "horizon 仅支持 3D。")
    if not isinstance(bars_30m, list):
        raise ForecastInputError("invalid_bars", "bars_30m 必须是数组。")
    required = minimum_bars(horizon)
    if len(bars_30m) < required:
        raise ForecastInputError(
            "insufficient_data",
            f"{horizon} 预测至少需要 {required} 根完整30分钟K线，当前只有 {len(bars_30m)} 根。",
        )
    if len(bars_30m) > 20_000:
        raise ForecastInputError("invalid_bars", "bars_30m 数量超过安全上限。")

    rows: list[dict[str, Any]] = []
    timestamps: list[pd.Timestamp] = []
    previous: pd.Timestamp | None = None
    for index, raw in enumerate(bars_30m):
        if not isinstance(raw, dict):
            raise ForecastInputError("invalid_bar", f"第 {index + 1} 根K线不是对象。")
        timestamp = _aware_timestamp(raw.get("time"), f"bars_30m[{index}].time")
        if previous is not None and timestamp <= previous:
            raise ForecastInputError("invalid_timestamp_order", "K线时间必须严格递增且不得重复。")
        if timestamp.weekday() >= 5 or (timestamp.hour, timestamp.minute) not in FULL_SESSION_30M_SLOTS:
            raise ForecastInputError("invalid_session_bar", "30分钟K线必须位于美股交易日 04:00–20:00 ET 全时段。")
        previous = timestamp
        # ForecastService passes a pre-filtered completed-bar array and omits
        # this marker.  If a caller does include it, an explicit false value
        # must still fail closed.
        if "is_complete" in raw and raw.get("is_complete") is not True:
            raise ForecastInputError("incomplete_bar", "预测引擎只接受 is_complete=true 的30分钟K线。")
        open_price = _finite_number(raw.get("open"), "open")
        high = _finite_number(raw.get("high"), "high")
        low = _finite_number(raw.get("low"), "low")
        close = _finite_number(raw.get("close"), "close")
        volume = _finite_number(raw.get("volume"), "volume")
        if min(open_price, high, low, close) <= 0:
            raise ForecastInputError("invalid_ohlc", "OHLC 必须全部大于0。")
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise ForecastInputError("invalid_ohlc", "OHLC 高低关系不合法。")
        if volume < 0:
            raise ForecastInputError("invalid_volume", "历史成交量必须是有限非负数。")
        timestamps.append(timestamp)
        rows.append({
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })

    as_of = _aware_timestamp(market_as_of, "market_as_of")
    if timestamps[-1] > as_of:
        raise ForecastInputError("invalid_market_as_of", "market_as_of 不得早于最后一根已完成K线。")
    frame = pd.DataFrame(rows, index=pd.DatetimeIndex(timestamps))
    if float((frame["volume"] > 0).mean()) < 0.25:
        raise ForecastInputError("insufficient_volume", "全时段历史中有效正成交量覆盖不足25%。")
    session_dates = list(dict.fromkeys(frame.index.date))
    selected_dates = set(session_dates[-ANALYSIS_SESSION_DAYS:])
    if trim_latest_sessions:
        frame = frame[[session_date in selected_dates for session_date in frame.index.date]]
    if len(selected_dates) < ANALYSIS_SESSION_DAYS or len(frame) < required:
        raise ForecastInputError(
            "insufficient_data",
            f"3D 全时段预测需要最近 {ANALYSIS_SESSION_DAYS} 个交易日且至少 {required} 根完整30分钟K线；"
            f"当前为 {len(selected_dates)} 日/{len(frame)} 根。",
        )
    return ValidatedMarketData(
        symbol=normalized_symbol,
        horizon=horizon,
        steps=HORIZON_STEPS[horizon],
        frame=frame,
        market_as_of=as_of,
    )


def _build_features(frame: pd.DataFrame) -> pd.DataFrame:
    close = frame["close"]
    log_close = np.log(close)
    return_1 = log_close.diff()
    previous_close = close.shift(1)
    true_range = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - previous_close).abs(),
        (frame["low"] - previous_close).abs(),
    ], axis=1).max(axis=1) / close
    range_fraction = (frame["high"] - frame["low"]) / close
    body_fraction = (frame["close"] - frame["open"]) / frame["open"]
    log_volume = np.log1p(frame["volume"])
    volume_mean_20 = log_volume.rolling(20, min_periods=20).mean()
    volume_std_20 = log_volume.rolling(20, min_periods=20).std(ddof=0)
    volume_z_20 = (log_volume - volume_mean_20) / volume_std_20.where(volume_std_20 > 1e-12)
    volume_z_20 = volume_z_20.where(volume_std_20 > 1e-12, 0.0)
    gains = return_1.clip(lower=0).rolling(32, min_periods=32).mean()
    losses = (-return_1.clip(upper=0)).rolling(32, min_periods=32).mean()
    rsi_centered = gains / (gains + losses + 1e-12) - 0.5
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    rolling_volume = frame["volume"].rolling(32, min_periods=32).sum()
    rolling_vwap = (typical * frame["volume"]).rolling(32, min_periods=32).sum() / rolling_volume.where(rolling_volume > 0)

    local_index = frame.index.tz_convert(NEW_YORK)
    minutes = local_index.hour * 60 + local_index.minute
    slots = (minutes - (4 * 60)) / 30.0
    day = local_index.dayofweek.to_numpy(dtype=float)
    features = pd.DataFrame({
        "return_1": return_1,
        "return_2": log_close - log_close.shift(2),
        "return_3": log_close - log_close.shift(3),
        "return_6": log_close - log_close.shift(6),
        "return_32": log_close - log_close.shift(32),
        "return_64": log_close - log_close.shift(64),
        "momentum_acceleration": (log_close - log_close.shift(3)) - (log_close.shift(3) - log_close.shift(6)),
        "volatility_6": return_1.rolling(6, min_periods=6).std(ddof=0),
        "volatility_32": return_1.rolling(32, min_periods=32).std(ddof=0),
        "volatility_64": return_1.rolling(64, min_periods=64).std(ddof=0),
        "true_range": true_range,
        "range_mean_6": range_fraction.rolling(6, min_periods=6).mean(),
        "range_mean_32": range_fraction.rolling(32, min_periods=32).mean(),
        "body_fraction": body_fraction,
        "price_vs_ma_6": close / close.rolling(6, min_periods=6).mean() - 1.0,
        "price_vs_ma_32": close / close.rolling(32, min_periods=32).mean() - 1.0,
        "price_vs_ma_64": close / close.rolling(64, min_periods=64).mean() - 1.0,
        "price_vs_vwap_32": close / rolling_vwap - 1.0,
        "volume_z_20": volume_z_20,
        "volume_ratio_6_20": np.log(
            (frame["volume"].rolling(6, min_periods=6).mean() + 1.0)
            / (frame["volume"].rolling(20, min_periods=20).mean() + 1.0)
        ),
        "rsi_32_centered": rsi_centered,
        "slot_sin": np.sin(2.0 * np.pi * slots / 32.0),
        "slot_cos": np.cos(2.0 * np.pi * slots / 32.0),
        "day_sin": np.sin(2.0 * np.pi * day / 5.0),
        "day_cos": np.cos(2.0 * np.pi * day / 5.0),
    }, index=frame.index)
    return features.replace([np.inf, -np.inf], np.nan)


def _fit_ridge(features: np.ndarray, targets: np.ndarray) -> RidgeFit:
    if features.ndim != 2 or targets.ndim not in (1, 2) or len(features) != len(targets):
        raise ForecastInputError("model_failure", "Ridge 训练矩阵维度不合法。")
    if len(features) < MIN_WALK_FORWARD_TRAIN:
        raise ForecastInputError("insufficient_training", "Ridge 可用训练样本不足。")
    target_matrix = targets[:, None] if targets.ndim == 1 else targets
    x_mean = features.mean(axis=0)
    x_scale = features.std(axis=0, ddof=0)
    x_scale = np.where(x_scale > 1e-12, x_scale, 1.0)
    standardized = np.clip((features - x_mean) / x_scale, -8.0, 8.0)
    clipped_targets = np.clip(target_matrix, -0.50, 0.50)
    y_mean = clipped_targets.mean(axis=0)
    centered_targets = clipped_targets - y_mean
    gram = standardized.T @ standardized
    regularized = gram + RIDGE_ALPHA * np.eye(standardized.shape[1], dtype=float)
    coefficients = np.linalg.solve(regularized, standardized.T @ centered_targets)
    fitted = y_mean + standardized @ coefficients
    residuals = clipped_targets - fitted
    if not all(np.isfinite(array).all() for array in (x_mean, x_scale, y_mean, coefficients, residuals)):
        raise ForecastInputError("model_failure", "Ridge 计算产生非有限数值。")
    return RidgeFit(x_mean, x_scale, y_mean, coefficients, residuals)


def _feature_origins(features: pd.DataFrame) -> np.ndarray:
    values = features.to_numpy(dtype=float)
    return np.flatnonzero(np.isfinite(values).all(axis=1))


def _direct_target_matrix(log_close: np.ndarray, origins: np.ndarray, steps: int) -> np.ndarray:
    offsets = np.arange(1, steps + 1, dtype=int)
    return log_close[origins[:, None] + offsets[None, :]] - log_close[origins, None]


def _recent_sigma(log_close: np.ndarray, origin: int) -> float:
    start = max(0, origin - 64)
    returns = np.diff(log_close[start:origin + 1])
    sigma = float(np.std(returns, ddof=0)) if len(returns) else 0.0
    return max(0.0005, sigma)


def _interval_for_prediction(
    prediction: float,
    residuals: np.ndarray,
    sigma: float,
    steps: int,
    oos_sigma: float | None = None,
) -> tuple[float, float]:
    """The q10/q90 band around a prediction.

    `oos_sigma` is the standard deviation of realized walk-forward errors at this
    horizon. When present it sets the floor width, because the training-residual
    quantiles this used to rely on are measured on 96-step overlapping targets
    the model was fitted to — they describe how well it memorised, not how well
    it predicts. Measured across the pool, intervals built the old way covered
    64% of outcomes instead of the 80% they advertise.
    """
    residual_vector = np.asarray(residuals, dtype=float).reshape(-1)
    lower_residual = float(np.quantile(residual_vector, 0.10))
    upper_residual = float(np.quantile(residual_vector, 0.90))
    minimum_half_width = max(0.001, 0.55 * sigma * math.sqrt(steps))
    if oos_sigma is not None:
        minimum_half_width = max(minimum_half_width, Q80_Z * oos_sigma)
    lower = min(prediction, prediction + lower_residual, prediction - minimum_half_width)
    upper = max(prediction, prediction + upper_residual, prediction + minimum_half_width)
    return max(-0.60, lower), min(0.60, upper)


def _flat_threshold(sigma: float, steps: int) -> float:
    return min(0.03, max(0.0025, 0.35 * sigma * math.sqrt(steps)))


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _class_probabilities(
    prediction: float,
    residuals: np.ndarray,
    sigma: float,
    steps: int,
    oos_sigma: float | None = None,
) -> tuple[np.ndarray, float]:
    """Down / flat / up probabilities from a normal fitted around `prediction`.

    THE WIDTH IS THE WHOLE BALLGAME. This used to take its spread from the
    ridge's own training residuals. Those targets overlap by 95 of 96 bars, so
    consecutive samples are nearly the same stretch of tape and the fit tracks
    them closely; the resulting sigma is small, and a small sigma turns any
    prediction — including one extrapolated far past anything the model was
    trained on — into a near-certainty.

    That is not a theory. Across 6,478 purged walk-forward samples on this
    project's pool, predictions stated at 90-100% confidence came true 45.5% of
    the time, and stated confidence carried no information about accuracy at
    all: the 70-90% bucket scored WORSE than the 33-50% bucket. Passing
    `oos_sigma` — the spread of realized out-of-sample errors — replaces the
    memorisation estimate with a prediction estimate.
    """
    residual_vector = np.asarray(residuals, dtype=float).reshape(-1)
    residual_sigma = float(np.std(residual_vector, ddof=1)) if len(residual_vector) > 1 else 0.0
    volatility_floor = 0.50 * sigma * math.sqrt(steps)
    if oos_sigma is not None:
        distribution_sigma = max(0.001, oos_sigma, volatility_floor)
    else:
        distribution_sigma = max(0.001, residual_sigma, volatility_floor)
    threshold = _flat_threshold(sigma, steps)
    down = _normal_cdf((-threshold - prediction) / distribution_sigma)
    up = 1.0 - _normal_cdf((threshold - prediction) / distribution_sigma)
    flat = max(0.0, 1.0 - down - up)
    probabilities = np.asarray([down, flat, up], dtype=float)
    probabilities = np.clip(probabilities, 0.0, 1.0)
    probabilities /= probabilities.sum()
    return probabilities, threshold


def _return_class(value: float, threshold: float) -> int:
    if value > threshold:
        return 2  # up
    if value < -threshold:
        return 0  # down
    return 1  # flat


def _preferred_argmax(values: np.ndarray) -> int:
    maximum = float(np.max(values))
    if math.isclose(float(values[1]), maximum, rel_tol=0.0, abs_tol=1e-12):
        return 1
    return int(np.argmax(values))


def _prediction_at_origin(
    data: ValidatedMarketData,
    features: pd.DataFrame,
    origin: int,
) -> dict[str, Any] | None:
    steps = data.steps
    feature_values = features.to_numpy(dtype=float)
    valid_origins = _feature_origins(features)
    # At origin t, a training target j->j+H is available only when j+H <= t.
    train_origins = valid_origins[valid_origins + steps <= origin]
    if len(train_origins) < MIN_WALK_FORWARD_TRAIN:
        return None
    current_features = feature_values[origin]
    if not np.isfinite(current_features).all():
        return None
    log_close = np.log(data.frame["close"].to_numpy(dtype=float))
    targets = log_close[train_origins + steps] - log_close[train_origins]
    fit = _fit_ridge(feature_values[train_origins], targets)
    prediction = float(np.clip(fit.predict(current_features)[0], -0.35, 0.35))
    sigma = _recent_sigma(log_close, origin)
    probabilities, threshold = _class_probabilities(prediction, fit.residuals[:, 0], sigma, steps)
    lower, upper = _interval_for_prediction(prediction, fit.residuals[:, 0], sigma, steps)
    training_classes = np.asarray([_return_class(float(value), threshold) for value in targets], dtype=int)
    counts = np.bincount(training_classes, minlength=3)
    baseline_class = _preferred_argmax(counts.astype(float))
    return {
        "origin": origin,
        "prediction": prediction,
        "probabilities": probabilities,
        "threshold": threshold,
        "lower": lower,
        "upper": upper,
        "baseline_class": baseline_class,
        "trained_samples": int(len(train_origins)),
        # Recent realized volatility and the terminal-step residual vector, kept
        # so a later pass can rebuild this origin's probabilities against an
        # out-of-sample width without refitting the ridge.
        "sigma": sigma,
        "residuals": fit.residuals[:, 0],
        # The range the model was actually fitted on, carried so a caller can
        # ask whether a given prediction is interpolation or extrapolation.
        "train_target_min": float(np.min(targets)),
        "train_target_max": float(np.max(targets)),
        # In-sample residual spread at this origin. Kept for comparison against
        # the out-of-sample spread the walk-forward measures; the gap between
        # the two is the whole calibration problem in one number.
        "residual_sigma_in_sample": float(np.std(fit.residuals[:, 0], ddof=1))
                                    if len(fit.residuals) > 1 else 0.0,
    }


def diagnostic_prediction_at_origin(
    *,
    symbol: str,
    horizon: str,
    bars_30m: list[dict[str, Any]],
    market_as_of: str,
    origin_index: int,
) -> dict[str, Any]:
    """Return prediction-only diagnostics for a fixed historical origin.

    This helper exists to make temporal leakage tests explicit.  It never reads
    the realized target after ``origin_index``.
    """
    data = _validate_input(symbol, horizon, bars_30m, market_as_of)
    if origin_index < 0 or origin_index >= len(data.frame):
        raise ForecastInputError("invalid_origin", "origin_index 超出范围。")
    point = _prediction_at_origin(data, _build_features(data.frame), origin_index)
    if point is None:
        raise ForecastInputError("insufficient_training", "该历史时点的已揭晓训练样本不足。")
    return {
        "prediction": point["prediction"],
        "probabilities": [float(item) for item in point["probabilities"]],
        "threshold": point["threshold"],
        "lower": point["lower"],
        "upper": point["upper"],
        "baseline_class": point["baseline_class"],
        "trained_samples": point["trained_samples"],
    }


def walk_forward_records(data: ValidatedMarketData, features: pd.DataFrame,
                         *, max_samples: int = MAX_BACKTEST_SAMPLES,
                         calibrate: bool = True) -> list[dict[str, Any]]:
    """Per-sample purged walk-forward results, newest-anchored, non-overlapping.

    Factored out of `_walk_forward_backtest` so that the aggregate reported in a
    forecast and any offline study read the SAME samples. A study that
    re-implemented this selection would be measuring a different estimator than
    the one that ships, and the two would drift the first time either changed.

    `max_samples` exists because the production cap (32) is a latency budget, not
    a statistical one: it walks backwards from the newest bar, so raising it
    reaches further into the past rather than changing recent samples.
    """
    steps = data.steps
    valid_origins = _feature_origins(features)
    candidates = valid_origins[valid_origins + steps < len(data.frame)]
    raw_eligible = [
        int(origin)
        for origin in candidates
        if int(np.sum(valid_origins + steps <= origin)) >= MIN_WALK_FORWARD_TRAIN
    ]
    # Evaluation targets must not overlap.  Walk backwards so the newest
    # evidence is retained, with one extra embargo bar between test windows.
    eligible_reversed: list[int] = []
    next_allowed_before = len(data.frame) + steps + BACKTEST_EMBARGO_BARS
    for origin in reversed(raw_eligible):
        if origin + steps + BACKTEST_EMBARGO_BARS < next_allowed_before:
            eligible_reversed.append(origin)
            next_allowed_before = origin
        if len(eligible_reversed) >= max_samples:
            break
    eligible = list(reversed(eligible_reversed))
    log_close = np.log(data.frame["close"].to_numpy(dtype=float))
    records: list[dict[str, Any]] = []
    for origin in eligible:
        point = _prediction_at_origin(data, features, origin)
        if point is None:
            continue
        actual = float(log_close[origin + steps] - log_close[origin])
        actual_class = _return_class(actual, point["threshold"])
        predicted_class = _preferred_argmax(point["probabilities"])
        one_hot = np.zeros(3, dtype=float)
        one_hot[actual_class] = 1.0
        records.append({
            **point,
            "steps": steps,
            "actual": actual,
            "actual_class": actual_class,
            "predicted_class": predicted_class,
            "brier": float(np.mean((point["probabilities"] - one_hot) ** 2)),
            "origin_at": data.frame.index[origin].isoformat(),
            "settled_at": data.frame.index[origin + steps].isoformat(),
        })
    if calibrate:
        records = _recalibrate_records(records)
    for record in records:
        record.pop("residuals", None)     # large, and of no use to callers
    return records


def _recalibrate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild each sample's probabilities from the errors known BEFORE it.

    Sample i is re-scored using the realized errors of samples 0..i-1 and
    nothing else. Those samples are non-overlapping with an embargo bar, so
    every one of them had already settled by the time sample i's origin arrived
    — the calibration is genuinely point-in-time and adds no look-ahead.

    The alternative, calibrating on the whole sample at once, would have made
    the study report a reliability the live forecast could never reproduce.
    Doing it this way means the numbers a backtest prints are the numbers the
    shipped estimator will earn.
    """
    errors: list[float] = []
    for record in records:
        oos_sigma = _oos_sigma(errors)
        if oos_sigma is not None:
            probabilities, threshold = _class_probabilities(
                record["prediction"], record["residuals"], record["sigma"],
                record["steps"], oos_sigma=oos_sigma,
            )
            lower, upper = _interval_for_prediction(
                record["prediction"], record["residuals"], record["sigma"],
                record["steps"], oos_sigma=oos_sigma,
            )
            actual = record["actual"]
            actual_class = _return_class(actual, threshold)
            one_hot = np.zeros(3, dtype=float)
            one_hot[actual_class] = 1.0
            record.update({
                "probabilities": probabilities,
                "threshold": threshold,
                "lower": lower,
                "upper": upper,
                "actual_class": actual_class,
                "predicted_class": _preferred_argmax(probabilities),
                "brier": float(np.mean((probabilities - one_hot) ** 2)),
                "calibration": "out_of_sample",
                "calibration_samples": len(errors),
            })
        else:
            record["calibration"] = "in_sample"
            record["calibration_samples"] = len(errors)
        errors.append(record["actual"] - record["prediction"])
    return records


def _oos_sigma(errors: list[float]) -> float | None:
    """Standard deviation of realized walk-forward errors, or None if too few."""
    if len(errors) < MIN_CALIBRATION_SAMPLES:
        return None
    value = float(np.std(np.asarray(errors, dtype=float), ddof=1))
    return value if math.isfinite(value) and value > 1e-9 else None


def _walk_forward_backtest(data: ValidatedMarketData, features: pd.DataFrame) -> dict[str, Any]:
    return _backtest_summary(walk_forward_records(data, features), data, data.steps)


def _backtest_summary(records: list[dict[str, Any]], data: ValidatedMarketData,
                      steps: int) -> dict[str, Any]:
    sample_count = len(records)
    if not records:
        return {
            "status": "insufficient",
            "samples": 0,
            "direction_accuracy_percent": None,
            "baseline_accuracy_percent": None,
            "brier_score": None,
            "interval_coverage_percent": None,
            "mae_percent": None,
            "start_at": None,
            "end_at": None,
            "baseline_name": "expanding_majority_class",
            "method": "purged_expanding_walk_forward",
            "purge_bars": steps,
            "embargo_bars": BACKTEST_EMBARGO_BARS,
        }
    direction_accuracy = np.mean([record["predicted_class"] == record["actual_class"] for record in records])
    baseline_accuracy = np.mean([record["baseline_class"] == record["actual_class"] for record in records])
    coverage = np.mean([record["lower"] <= record["actual"] <= record["upper"] for record in records])
    mae = np.mean([abs(record["prediction"] - record["actual"]) for record in records])
    return {
        "status": "available" if sample_count >= MIN_BACKTEST_SAMPLES else "insufficient",
        "samples": sample_count,
        "direction_accuracy_percent": round(float(direction_accuracy * 100.0), 2),
        "baseline_accuracy_percent": round(float(baseline_accuracy * 100.0), 2),
        "brier_score": round(float(np.mean([record["brier"] for record in records])), 6),
        "interval_coverage_percent": round(float(coverage * 100.0), 2),
        "mae_percent": round(float(mae * 100.0), 4),
        "start_at": data.frame.index[records[0]["origin"]].isoformat(),
        "end_at": data.frame.index[records[-1]["origin"]].isoformat(),
        "baseline_name": "expanding_majority_class",
        "method": "purged_expanding_walk_forward",
        "purge_bars": steps,
        "embargo_bars": BACKTEST_EMBARGO_BARS,
    }


def _future_market_slots(after: pd.Timestamp, steps: int) -> list[pd.Timestamp]:
    return [pd.Timestamp(item) for item in future_30m_slots(after.to_pydatetime(), steps)]


def _slot_volume_profile(frame: pd.DataFrame) -> tuple[dict[tuple[int, int], float], float]:
    local = frame.index.tz_convert(NEW_YORK)
    slot_series = pd.Series(list(zip(local.hour, local.minute)), index=frame.index)
    profile: dict[tuple[int, int], float] = {}
    for slot in sorted(set(slot_series)):
        values = frame.loc[slot_series == slot, "volume"].to_numpy(dtype=float)
        profile[slot] = float(np.median(values))
    positive = frame.loc[frame["volume"] > 0, "volume"].to_numpy(dtype=float)
    fallback = float(np.median(positive[-64:])) if len(positive) else 0.0
    return profile, max(0.0, fallback)


def _round_price(value: float) -> float:
    rounded = round(float(value), 6)
    if not math.isfinite(rounded) or rounded <= 0:
        raise ForecastInputError("model_failure", "预测价格不是有限正数。")
    return rounded


def _percentage_probabilities(probabilities: np.ndarray) -> tuple[float, float, float]:
    down = round(float(probabilities[0] * 100.0), 2)
    flat = round(float(probabilities[1] * 100.0), 2)
    up = round(100.0 - down - flat, 2)
    if up < 0:
        flat = round(flat + up, 2)
        up = 0.0
    return up, flat, down


def _confidence_percent(probabilities: np.ndarray, backtest: dict[str, Any]) -> float | None:
    """How often this model has ACTUALLY been right out of sample, conservatively.

    The original index was `30 + 30*distribution_strength + 10*skill +
    10*sample_factor`, capped at 70. Every term but the skill one rewards the
    model for being emphatic rather than for being correct, so a prediction the
    ridge had extrapolated far past its training range — the least trustworthy
    kind it makes — scored the HIGHEST confidence. Across the pool it read 62 on
    an estimator that was right 36.6% of the time.

    A confidence number should be checkable against something. This one is:
    realized walk-forward direction accuracy, less one standard error so a
    twelve-sample measurement is not quoted as if it were precise. It returns
    None rather than a floor value when the walk-forward could not be scored,
    because "not measured" and "measured as poor" are different facts.
    """
    accuracy = backtest.get("direction_accuracy_percent")
    samples = int(backtest.get("samples") or 0)
    if backtest.get("status") != "available" or accuracy is None or samples < 1:
        return None
    proportion = float(accuracy) / 100.0
    standard_error = math.sqrt(max(proportion * (1.0 - proportion), 1e-9) / samples) * 100.0
    return round(max(0.0, float(accuracy) - standard_error), 2)


def _extrapolation_report(prediction: float, targets: np.ndarray) -> dict[str, Any]:
    """Is the terminal prediction inside the range the model was trained on?

    ADDED DURING THE MERGE — not part of the original platform engine.

    A ridge fitted on 160 heavily-overlapping samples will happily extrapolate
    far past anything it has seen, and because those overlapping targets make
    its training residuals small, `_class_probabilities` then reports that
    out-of-range guess with near-total confidence. Observed in practice: a
    -11.6% three-day terminal prediction from a training set whose worst
    realized target was -5.1%, published as 99.98% down.

    The distance is measured in training-range widths so it is comparable
    across symbols and volatility regimes. This function only reports; the
    caller decides what to do about it.
    """
    terminal_targets = np.asarray(targets[:, -1], dtype=float)
    low = float(np.min(terminal_targets))
    high = float(np.max(terminal_targets))
    width = high - low
    if prediction < low:
        excess = low - prediction
    elif prediction > high:
        excess = prediction - high
    else:
        excess = 0.0
    ratio = float(excess / width) if width > 1e-12 else 0.0
    return {
        "within_training_range": excess <= 0.0,
        "predicted_return_percent": round(float((math.exp(prediction) - 1.0) * 100.0), 4),
        "training_min_percent": round(float((math.exp(low) - 1.0) * 100.0), 4),
        "training_max_percent": round(float((math.exp(high) - 1.0) * 100.0), 4),
        "excess_training_range_widths": round(ratio, 4),
    }


def _skill_report(backtest: dict[str, Any]) -> dict[str, Any]:
    """Did the walk-forward beat the majority-class baseline, and by how much?

    ADDED DURING THE MERGE — not part of the original platform engine.

    `backtest.status == "available"` only says enough non-overlapping samples
    existed to score the model. It says nothing about whether the model was any
    good, and the scanner's candidate gate was reading it as if it did. This
    puts the one number that answers that question where a caller cannot miss
    it: accuracy minus the baseline it has to beat to be worth anything.
    """
    accuracy = backtest.get("direction_accuracy_percent")
    baseline = backtest.get("baseline_accuracy_percent")
    if not isinstance(accuracy, (int, float)) or not isinstance(baseline, (int, float)):
        return {"status": "unmeasured", "margin_percentage_points": None,
                "beats_baseline": False}
    margin = float(accuracy) - float(baseline)
    return {
        "status": "measured",
        "margin_percentage_points": round(margin, 2),
        "beats_baseline": margin > 0.0,
    }


def _base_rate_probabilities(terminal_targets: np.ndarray, threshold: float) -> np.ndarray | None:
    """The down/flat/up mix of the targets the model was trained on, or None.

    ADDED DURING THE MERGE. This is what gets reported when the model has no
    measured ability to call direction — the honest answer being the base rate,
    not a fabricated one.

    It reads the class mix from the TRAINING targets rather than from the
    walk-forward samples, because that is the exact information
    `expanding_majority_class` uses to form the baseline. Matching it matters:
    with the same input the abstaining forecast scores the baseline by
    construction, since `_preferred_argmax` of the base rate is the majority
    class. Estimating the same quantity from the ~30 non-overlapping
    walk-forward samples instead was noisier and gave back only two thirds of
    the gap (-0.61pp against the model's -1.85pp), for no reason other than
    using less data to answer the same question.
    """
    values = np.asarray(terminal_targets, dtype=float).reshape(-1)
    if len(values) < MIN_FINAL_TRAIN:
        return None
    counts = np.bincount([_return_class(float(value), threshold) for value in values],
                         minlength=3)
    total = float(counts.sum())
    if total <= 0:
        return None
    return counts.astype(float) / total


def generate_forecast(
    *,
    symbol: str,
    horizon: str,
    bars_30m: list[dict[str, Any]],
    market_as_of: str,
    generated_at: str | None = None,
    backtest_bars_30m: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate a deterministic research forecast and walk-forward report."""
    data = _validate_input(symbol, horizon, bars_30m, market_as_of)
    # The final fit trains on the FULL window supplied, not the 10-session slice
    # the chart is drawn from.
    #
    # Ten sessions is 320 bars: after the 64-bar feature lookback and the 96-bar
    # horizon that leaves ~160 training origins, and consecutive origins overlap
    # by 95 of 96 bars, so the effective independent sample count is under two.
    # Twenty-five features fitted on two effective observations do not estimate
    # anything; they memorise, and the memorised residuals are what used to make
    # every probability look certain. Measured on the pool over a year, moving
    # to the full window takes out-of-sample R^2 from -1.84 to -0.10 — an
    # eighteen-fold cut in the variance the model was inventing.
    training = data
    if isinstance(backtest_bars_30m, list) and len(backtest_bars_30m) >= minimum_bars(horizon):
        training = _validate_input(symbol, horizon, backtest_bars_30m, market_as_of,
                                   trim_latest_sessions=False)
    features = _build_features(training.frame)
    feature_values = features.to_numpy(dtype=float)
    valid_origins = _feature_origins(features)
    train_origins = valid_origins[valid_origins + data.steps < len(training.frame)]
    if len(train_origins) < MIN_FINAL_TRAIN:
        raise ForecastInputError(
            "insufficient_training",
            f"清洗后只剩 {len(train_origins)} 个直接预测训练样本，至少需要 {MIN_FINAL_TRAIN} 个。",
        )
    if not np.isfinite(feature_values[-1]).all():
        raise ForecastInputError("invalid_latest_features", "最后一根K线无法生成完整特征。")

    log_close = np.log(training.frame["close"].to_numpy(dtype=float))
    targets = _direct_target_matrix(log_close, train_origins, data.steps)
    fit = _fit_ridge(feature_values[train_origins], targets)
    prediction_path = np.clip(fit.predict(feature_values[-1]), -0.35, 0.35)
    sigma = _recent_sigma(log_close, len(training.frame) - 1)
    future_times = _future_market_slots(data.market_as_of, data.steps)
    if len(future_times) != data.steps or future_times[0] <= data.market_as_of:
        raise ForecastInputError("model_failure", "无法生成严格未来的美股全时段时间轴。")

    last_close = float(training.frame["close"].iloc[-1])
    recent_range = (training.frame["high"] - training.frame["low"]) / training.frame["close"]
    expected_range = min(0.08, max(0.001, float(np.median(recent_range.to_numpy(dtype=float)[-64:]))))
    volume_profile, fallback_volume = _slot_volume_profile(training.frame)

    # The walk-forward runs BEFORE the path is built, because the spread of its
    # realized errors is what sets the width of every interval below and of the
    # terminal probabilities. Ordering it after, as this did originally, is what
    # forced both to fall back on training residuals.
    backtest_data = training
    backtest_records = walk_forward_records(backtest_data, features)
    backtest = _backtest_summary(backtest_records, backtest_data, data.steps)
    terminal_oos_sigma = _oos_sigma([record["actual"] - record["prediction"]
                                     for record in backtest_records])
    # Errors are only measured at the full horizon. Scaling by sqrt(h/H) carries
    # that one measurement back over the intermediate steps the same way the
    # volatility floor already scales with sqrt(steps); it is the random-walk
    # assumption, stated rather than assumed.
    def _step_oos_sigma(step: int) -> float | None:
        if terminal_oos_sigma is None:
            return None
        return terminal_oos_sigma * math.sqrt(step / float(data.steps))

    forecast_bars: list[dict[str, Any]] = []
    previous_close = last_close
    for offset, (timestamp, predicted_return) in enumerate(zip(future_times, prediction_path), start=1):
        residuals = fit.residuals[:, offset - 1]
        lower_return, upper_return = _interval_for_prediction(
            float(predicted_return), residuals, sigma, offset,
            oos_sigma=_step_oos_sigma(offset),
        )
        q50 = last_close * math.exp(float(predicted_return))
        q10 = last_close * math.exp(lower_return)
        q90 = last_close * math.exp(upper_return)
        q10 = min(q10, q50)
        q90 = max(q90, q50)
        open_price = previous_close
        half_range = expected_range * 0.5
        high = max(open_price, q50) * (1.0 + half_range)
        low = max(0.01, min(open_price, q50) * (1.0 - half_range))
        slot = (timestamp.hour, timestamp.minute)
        volume = max(0.0, float(volume_profile.get(slot, fallback_volume)))
        forecast_bars.append({
            "time": timestamp.isoformat(),
            "open": _round_price(open_price),
            "high": _round_price(high),
            "low": _round_price(low),
            "close": _round_price(q50),
            "volume": round(volume, 2),
            "q10": _round_price(q10),
            "q50": _round_price(q50),
            "q90": _round_price(q90),
        })
        previous_close = q50

    terminal_probabilities, flat_threshold = _class_probabilities(
        float(prediction_path[-1]), fit.residuals[:, -1], sigma, data.steps,
        oos_sigma=terminal_oos_sigma,
    )
    backtest["input_bars"] = int(len(backtest_data.frame))
    backtest["calibration"] = {
        "source": "out_of_sample_walk_forward" if terminal_oos_sigma is not None else "in_sample_residuals",
        "oos_sigma": round(terminal_oos_sigma, 6) if terminal_oos_sigma is not None else None,
        "minimum_samples": MIN_CALIBRATION_SAMPLES,
        "note": ("方向概率的分布宽度取自已实现的样本外误差；"
                 "样本不足时退回训练残差，并在此标注。"),
    }
    generated = _aware_timestamp(generated_at or market_as_of, "generated_at")
    # The quality verdict is reached BEFORE the probabilities are formatted,
    # because one of its outcomes is to replace them.
    extrapolation = _extrapolation_report(float(prediction_path[-1]), targets)
    skill = _skill_report(backtest)
    quality_status = "research_only" if backtest["status"] == "available" else "insufficient"
    reasons = ["确定性 Ridge 研究基线尚未通过独立样本外、仿真与实盘验证，不得作为交易指令。"]
    if backtest["status"] == "insufficient":
        reasons.append("长期历史内可用的非重叠 purged walk-forward 样本少于10个，历史指标仅供排查模型。")
    # Two ADDED gates. Either one means the terminal probability below is not
    # evidence of anything, however confident it looks, so the status is pulled
    # down to the bucket that already means "diagnostics only".
    if not extrapolation["within_training_range"]:
        quality_status = "insufficient"
        reasons.append(
            f"终点预测 {extrapolation['predicted_return_percent']}% 落在训练目标区间 "
            f"[{extrapolation['training_min_percent']}%, {extrapolation['training_max_percent']}%] "
            f"之外 {extrapolation['excess_training_range_widths']} 倍区间宽；模型在外推，"
            "其高置信度来自重叠样本导致的残差偏小，不构成证据。"
        )
    if skill["status"] == "measured" and not skill["beats_baseline"]:
        quality_status = "insufficient"
        reasons.append(
            f"walk-forward 方向准确率比多数类基线低 {abs(skill['margin_percentage_points'])} 个百分点；"
            "该窗口内模型没有可验证的方向性技能。"
        )
    # ABSTAIN. Without measured direction skill the model's own probabilities
    # are not evidence, and printing them anyway is how a research number turns
    # into a decision. What gets reported instead is the realized base rate, and
    # the status says plainly that this is not a view.
    abstained = False
    if skill["status"] != "measured" or not skill["beats_baseline"]:
        base_rate = _base_rate_probabilities(targets[:, -1], flat_threshold)
        if base_rate is not None:
            terminal_probabilities = base_rate
            abstained = True
            quality_status = "no_opinion"
            reasons.append(
                "已弃权：方向概率改用该标的 walk-forward 实测的涨/平/跌基准率，"
                "不是模型的方向判断。点预测与区间仍然给出，仅用于观察路径形状。"
            )

    up_percent, flat_percent, down_percent = _percentage_probabilities(terminal_probabilities)
    probability_source = "walk_forward_base_rate" if abstained else "model_distribution"
    if round(up_percent + flat_percent + down_percent, 2) != 100.0:
        raise ForecastInputError("model_failure", "方向概率未能归一化至100%。")
    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": data.symbol,
        "horizon": data.horizon,
        # Preserve the request identity byte-for-byte; the normalized timestamp
        # above is used only for ordering and future-slot calculations.
        "market_as_of": market_as_of,
        "generated_at": generated.isoformat(),
        "forecast_30m": forecast_bars,
        "probabilities": {
            "source": probability_source,
            "abstained": abstained,
            "up_percent": up_percent,
            "flat_percent": flat_percent,
            "down_percent": down_percent,
            "confidence_percent": _confidence_percent(terminal_probabilities, backtest),
            "flat_threshold_percent": round(float(flat_threshold * 100.0), 4),
        },
        "backtest": backtest,
        "model": {
            "name": MODEL_NAME,
            "version": MODEL_VERSION,
            "kind": MODEL_KIND,
            "trained_samples": int(len(train_origins)),
            "feature_count": int(features.shape[1]),
            "forecast_steps": data.steps,
            "analysis_window_trading_days": ANALYSIS_SESSION_DAYS,
            "ridge_alpha": RIDGE_ALPHA,
            # These index the TRAINING frame. They referenced the ten-session
            # chart frame while the origins indexed the full one, which silently
            # reported a training start three days ago for a model fitted on a
            # year of bars.
            "trained_from": training.frame.index[int(train_origins[0])].isoformat(),
            "trained_through": training.frame.index[-1].isoformat(),
            "training_bars": int(len(training.frame)),
            "training_sessions": int(len({stamp.date() for stamp in training.frame.index})),
        },
        "quality": {
            "status": quality_status,
            "actionable": False,
            "reasons": reasons,
            "warnings": [
                "未来K线覆盖盘前、常规盘与盘后（通常04:00–20:00 ET；提前收市日到17:00）；临时停市仍需人工确认。",
                "盘前/盘后K线成交量稀薄且常为0；模型保留价格K线，并对成交量覆盖不足执行 fail-closed。",
                "q10/q90 来自训练残差区间，不能解释为保证价格范围。",
                "行情来自券商 OpenD 的 ETH 时段（收盘打点已回移为开盘打点）；与其他数据源的K线不保证逐根一致。",
                "模型输入固定为最近10个美股交易日；较短窗口会提高对近期行情变化的敏感度，也会减少验证样本。",
                "研究基线，不可作为交易指令：actionable 恒为 false，且没有任何下单路径引用本模块。",
            ],
            "input_bars": int(len(training.frame)),
            "complete_bars": int(len(training.frame)),
            "extrapolation": extrapolation,
            "skill": skill,
            "abstained": abstained,
        },
    }
