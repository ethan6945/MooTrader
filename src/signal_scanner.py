"""Three-session candidate scanner for the signal desk.

Ported from the Stock Probability Prediction Platform's `companion/scanner_service.py`.
`analyze_bars` is the platform's `_analyze` unchanged — a pure 0-100 score over
three sessions of 30-minute bars, plus hard risk gates and a research plan.

TWO THINGS CHANGED IN THE MERGE.

1. WHERE CANDIDATES COME FROM. The platform interleaved Yahoo's "most active"
   and "day gainers" screens. This project has no Yahoo screener and does have a
   momentum-ranked live universe that `watchlist_updater` already refreshes, so
   discovery reads that instead. It is still explicitly not an exhaustive scan
   of every US listing, and it is still bounded.

2. THE CANDIDATE GATE GOT STRICTER, DELIBERATELY. The platform required
   `backtest.status == "available"` before letting a forecast set a row's score
   at 72% weight. That check only asks whether enough non-overlapping samples
   existed to SCORE the model — it says nothing about whether the model was any
   good. Measured over the whole 82-symbol pool and a year of history — 6,478
   purged walk-forward samples, see `scripts/signal_forecast_study.py` —
   direction accuracy came in 1.95 percentage points BELOW the majority-class
   baseline, with a standard error of 0.65, and no month in twelve where it
   worked. Meanwhile terminal probabilities read 99%+ because the model was
   extrapolating past its training range. Under the platform's gate those rows
   would have ranked as CANDIDATE at a score of 72+. So a row now also needs
   `quality.skill.beats_baseline` and `quality.extrapolation.within_training_range`
   — both surfaced by `signal_forecast` for exactly this purpose.

NO ROW IS AN INSTRUCTION. `actionable` is False on every payload, WAIT and
NO_TRADE are first-class outcomes, and the scanner never places an order.
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .signal_calendar import NEW_YORK

log = logging.getLogger(__name__)

CANDIDATE_SCORE = 62.0
WAIT_SCORE = 48.0
DISCOVERY_SCORE_FLOOR = 70.0
FORECAST_WEIGHT = 0.72
MARKET_WEIGHT = 0.28


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _rsi(values: list[float], period: int = 14) -> float:
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    recent = changes[-period:]
    gains = sum(max(change, 0) for change in recent) / max(1, len(recent))
    losses = sum(max(-change, 0) for change in recent) / max(1, len(recent))
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def analyze_bars(symbol: str, bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Score three sessions of 30-minute bars. Pure; no I/O, no broker calls."""
    completed = [bar for bar in bars if isinstance(bar, dict) and bar.get("is_complete") is True]
    dated: list[tuple[str, dict[str, Any]]] = []
    for item in completed:
        try:
            stamp = datetime.fromisoformat(str(item["time"]))
        except (KeyError, TypeError, ValueError):
            continue
        if stamp.tzinfo is None:
            continue
        dated.append((stamp.astimezone(NEW_YORK).date().isoformat(), item))
    dates = list(dict.fromkeys(day for day, _item in dated))[-3:]
    complete = [item for day, item in dated if day in set(dates)]
    if len(dates) < 3 or len(complete) < 60:
        return {"symbol": symbol, "status": "NO_TRADE", "score": 0.0,
                "reasons": ["近三交易日全时段30分钟K线不足"], "indicators": {}, "plan": {}}

    closes = [float(bar["close"]) for bar in complete]
    highs = [float(bar["high"]) for bar in complete]
    lows = [float(bar["low"]) for bar in complete]
    volumes = [float(bar["volume"]) for bar in complete]
    if any(not math.isfinite(value) for value in closes + highs + lows + volumes):
        return {"symbol": symbol, "status": "NO_TRADE", "score": 0.0,
                "reasons": ["行情包含无效数值"], "indicators": {}, "plan": {}}

    first, last = closes[0], closes[-1]
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
    return_3d = last / first - 1
    ema8, ema21 = _ema(closes, 8), _ema(closes, 21)
    trend = ema8 / ema21 - 1 if ema21 else 0.0
    rsi = _rsi(closes)
    true_ranges = [max(highs[index] - lows[index],
                       abs(highs[index] - closes[index - 1]),
                       abs(lows[index] - closes[index - 1]))
                   for index in range(1, len(closes))]
    atr = statistics.fmean(true_ranges[-14:]) if true_ranges else 0.0
    atr_percent = atr / last if last else 0.0
    typical = [(highs[index] + lows[index] + closes[index]) / 3 for index in range(len(closes))]
    volume_sum = sum(volumes)
    vwap = (sum(price * volume for price, volume in zip(typical, volumes)) / volume_sum
            if volume_sum > 0 else last)
    vwap_gap = last / vwap - 1 if vwap else 0.0
    prior_high = max(highs[:-1])
    breakout = last / prior_high - 1 if prior_high else 0.0
    positive_volumes = [value for value in volumes if value > 0]
    recent_volume = statistics.fmean(positive_volumes[-3:]) if len(positive_volumes) >= 3 else 0.0
    baseline_volume = statistics.median(positive_volumes[:-3]) if len(positive_volumes) > 3 else 0.0
    rvol = recent_volume / baseline_volume if baseline_volume > 0 else 0.0
    realized = statistics.pstdev(returns) if len(returns) > 1 else 0.0

    score = 50.0
    score += _clip(return_3d * 600, -18, 18)
    score += _clip(trend * 1000, -14, 14)
    score += _clip(vwap_gap * 500, -8, 8)
    score += _clip((rvol - 1) * 8, -6, 8)
    score += _clip(breakout * 600, -5, 8)
    score -= _clip(max(0.0, atr_percent - 0.025) * 300, 0, 8)
    if rsi > 78:
        score -= 8
    elif 52 <= rsi <= 68:
        score += 4
    score = round(_clip(score, 0, 100), 2)

    hard_risks: list[str] = []
    if last < vwap and ema8 <= ema21:
        hard_risks.append("价格低于三日VWAP且短均线未转强")
    if rsi > 82:
        hard_risks.append("RSI过热，不建议追价")
    if rvol <= 0:
        hard_risks.append("成交量基线无效")
    status = ("CANDIDATE" if score >= CANDIDATE_SCORE and not hard_risks
              else "WAIT" if score >= WAIT_SCORE else "NO_TRADE")

    # The research zone spans the current price and the three-session VWAP with
    # a small ATR buffer, so it stays ordered even when price is below VWAP (a
    # reclaim setup) instead of emitting a reversed high-low range.
    entry_low = min(vwap, last) - 0.10 * atr
    entry_high = max(vwap, last) + 0.10 * atr
    stop = entry_low - 1.15 * atr
    target = entry_high + 1.8 * max(entry_high - stop, atr)
    return {
        "symbol": symbol, "status": status, "score": score,
        "as_of": complete[-1]["time"], "price": round(last, 4),
        "indicators": {
            "return_3d_percent": round(return_3d * 100, 3),
            "ema8_vs_ema21_percent": round(trend * 100, 3),
            "rsi14": round(rsi, 2),
            "vwap_gap_percent": round(vwap_gap * 100, 3),
            "relative_volume": round(rvol, 3),
            "atr_percent": round(atr_percent * 100, 3),
            "realized_volatility_percent": round(realized * 100, 3),
        },
        "plan": {
            "entry_low": round(entry_low, 4), "entry_high": round(entry_high, 4),
            "stop": round(stop, 4), "target": round(target, 4),
            "label": "条件式研究候选" if status == "CANDIDATE" else "等待/不交易",
        },
        "reasons": hard_risks,
    }


def _forecast_is_usable(forecast: dict[str, Any]) -> tuple[bool, list[str]]:
    """Is this forecast strong enough to be allowed to set a candidate's score?

    Four conditions, and all of them must hold. The first two are the platform's
    (a full 96-bar path, a scorable walk-forward); the last two are this
    project's addition and are the ones that actually fire in practice.
    """
    reasons: list[str] = []
    bars = forecast.get("forecast_30m")
    backtest = forecast.get("backtest") or {}
    quality = forecast.get("quality") or {}
    if not isinstance(bars, list) or len(bars) != 96:
        reasons.append("三日预测路径不完整")
    if backtest.get("status") != "available" or int(backtest.get("samples") or 0) < 10:
        reasons.append("walk-forward 验证样本不足10个")
    if quality.get("abstained"):
        reasons.append("模型已弃权（无实测方向技能），概率为基准率而非判断")
        return False, reasons
    skill = quality.get("skill") or {}
    if not skill.get("beats_baseline"):
        margin = skill.get("margin_percentage_points")
        reasons.append(f"方向准确率未超过多数类基线（{margin:+} 个百分点）"
                       if margin is not None else "方向技能未测量")
    extrapolation = quality.get("extrapolation") or {}
    if not extrapolation.get("within_training_range", False):
        reasons.append("终点预测落在训练目标区间之外（模型在外推）")
    return (not reasons), reasons


def score_row(symbol: str, bars_30m: list[dict[str, Any]],
              forecast: Optional[dict[str, Any]], origin: str,
              company: str = "",
              excursion: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Combine the market read with the forecast, and decide the row's status.

    `excursion` is `signal_excursion.estimate`'s payload when the caller has
    one. It adds a SECOND stop/target beside the ATR pair below; it does not
    replace them. The ATR levels are kept visible on purpose — measured over
    4,980 purged origins their stop is hit 72.9% of the time inside the plan's
    own three-day horizon, and showing both is how that gets believed rather
    than argued about.
    """
    item = analyze_bars(symbol, bars_30m)
    item["origin"] = origin
    item["company"] = company or symbol
    if isinstance(excursion, dict) and not excursion.get("abstained"):
        from .signal_excursion import compare_to_atr
        plan = item.get("plan") or {}
        item["plan_model"] = {
            "stop": excursion["stop"],
            "target": excursion["target"],
            "stop_percent": excursion["stop_percent"],
            "target_percent": excursion["target_percent"],
            "reward_risk": excursion["reward_risk"],
            "observations": excursion["observations"],
            "realized": excursion["realized"],
            "basis": excursion["basis"],
        }
        item["plan_comparison"] = compare_to_atr(
            excursion, plan.get("stop"), plan.get("target"))
    elif isinstance(excursion, dict):
        item["plan_model"] = {"abstained": True,
                              "reason": excursion.get("reason", "样本不足")}
    hard_blocked = item["status"] == "NO_TRADE" and bool(item["reasons"])

    if not isinstance(forecast, dict):
        item["status"] = "NO_TRADE"
        item["reasons"].append("三日预测不可用")
        return item

    usable, blockers = _forecast_is_usable(forecast)
    probabilities = forecast.get("probabilities") or {}
    up_probability = probabilities.get("up_percent")
    item["forecast_up_probability_percent"] = (
        round(float(up_probability), 2)
        if isinstance(up_probability, (int, float)) and math.isfinite(float(up_probability))
        else None
    )
    item["forecast_confidence_percent"] = probabilities.get("confidence_percent")
    item["forecast_quality"] = forecast.get("quality", {}).get("status")
    item["forecast_backtest"] = forecast.get("backtest")
    item["forecast_skill"] = forecast.get("quality", {}).get("skill")
    item["forecast_extrapolation"] = forecast.get("quality", {}).get("extrapolation")
    item["forecast_terminal"] = (forecast.get("forecast_30m") or [{}])[-1]

    item["market_only_score"] = item["score"]
    if not usable or item["forecast_up_probability_percent"] is None:
        # The model contributed nothing, so the row is scored on the market read
        # alone and says so. It does NOT become NO_TRADE on that account.
        #
        # Marking it NO_TRADE was conflating two different statements: "the
        # model has no view" and "the technicals say stay out". The first is now
        # the normal case — the estimator abstains whenever it has no measured
        # direction skill, which on this data is always — and letting it veto
        # every row threw away the rule-based three-session read as well, for a
        # uniformly empty and unexplained candidate list.
        #
        # `analyze_bars` already set status from its own hard risk gates. That
        # stands. What is withheld is the 72% weight a validated forecast would
        # have carried, and `actionable` is False on the payload regardless.
        item["model_contribution"] = "none"
        item["model_blockers"] = blockers or ["三日预测不可用"]
        return item

    item["model_contribution"] = "weighted"
    item["score"] = round(FORECAST_WEIGHT * float(item["forecast_up_probability_percent"])
                          + MARKET_WEIGHT * float(item["score"]), 2)
    if hard_blocked:
        item["status"] = "NO_TRADE"
    else:
        item["status"] = ("CANDIDATE"
                          if item["score"] >= 60 and float(item["forecast_up_probability_percent"]) >= 50
                          else "WAIT")
    return item


def rank(rows: list[dict[str, Any]], *, discovery_floor: float = DISCOVERY_SCORE_FLOOR,
         limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    """Split rows into the starred ranking and the discovery ranking.

    Starred names are always shown, whatever they scored — the user asked to
    watch them. Discovery names have to earn their place: CANDIDATE status and
    a score at or above the floor.
    """
    def key(row: dict[str, Any]):
        return (-float(row["score"]),
                -float(row.get("forecast_up_probability_percent") or -1),
                row["symbol"])

    starred = sorted((row for row in rows if row.get("origin") == "starred"), key=key)[:limit]
    discovery = sorted(
        (row for row in rows
         if row.get("origin") != "starred" and row.get("status") == "CANDIDATE"
         and float(row["score"]) >= discovery_floor),
        key=key)[:limit]
    for row in discovery:
        if row.get("model_contribution") == "none":
            row.setdefault("label_note", "纯技术面候选，模型无贡献")
    return {"starred": starred, "discovery": discovery}


def _tally(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Why the discovery list is the length it is.

    `rank` drops every discovery row that is not a CANDIDATE, which on a day
    when the model fails its quality gates means dropping all of them. An empty
    list with no explanation reads as a broken panel; this is the explanation.
    """
    counts: dict[str, int] = {}
    blockers: dict[str, int] = {}
    for row in rows:
        if row.get("origin") == "starred":
            continue
        counts[str(row.get("status"))] = counts.get(str(row.get("status")), 0) + 1
        for reason in (row.get("reasons") or []):
            blockers[str(reason)] = blockers.get(str(reason), 0) + 1
    return {
        "by_status": counts,
        "top_blockers": sorted(blockers.items(), key=lambda item: -item[1])[:5],
    }


def build_payload(rows: list[dict[str, Any]], *, scanner_day: str,
                  universe_size: int, discovery_screened: int,
                  warnings: list[str], forced: bool,
                  discovery_source: str) -> dict[str, Any]:
    ranked = rank(rows)
    return {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "window": "3_market_days_full_session_30m",
        "method": "real_3d_full_session_forecast_plus_market_indicators",
        "universe_size": universe_size,
        "results": ranked["starred"],
        "starred_results": ranked["starred"],
        "market_results": ranked["discovery"],
        "market_threshold": DISCOVERY_SCORE_FLOOR,
        "market_universe": {
            "source": discovery_source,
            "screened_symbols": discovery_screened,
            "scope": "momentum_ranked_live_universe",
            "exhaustive": False,
            "outcome": _tally(rows),
        },
        "score_formula": {
            "model_up_probability_weight": FORECAST_WEIGHT,
            "market_indicator_weight": MARKET_WEIGHT,
            "market_indicators": ["return_3d", "ema8_vs_ema21", "rsi14",
                                  "vwap_gap", "relative_volume", "atr"],
            "forecast_gates": ["96_bar_path", "walk_forward_samples>=10",
                               "beats_majority_baseline", "within_training_range"],
        },
        "warnings": warnings,
        "actionable": False,
        "daily_cache": {"hit": False, "scanner_day": scanner_day,
                        "policy": "once_daily_unless_manual", "forced": forced},
        "disclaimer": "仅为长仓研究候选。缺乏成交量确认、数据陈旧、模型未通过质量门时必须 WAIT/NO_TRADE。",
    }


__all__ = ["analyze_bars", "build_payload", "rank", "score_row", "utc_now"]
