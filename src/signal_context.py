"""Bounded context scores that nudge the signal desk's three-day forecast.

Ported from the Stock Probability Prediction Platform's
`companion/research_context.py`. Pure and I/O-free, exactly as it was there:
the caller gathers the raw material, this module only turns it into numbers in
[-1, 1].

WHAT THESE ARE FOR. The forecast engine sees price and volume and nothing else.
These five scores are the channels through which everything else it cannot see —
recent micro-momentum, news tone, options positioning, capital flow, short
pressure — is allowed to move the terminal price, and only by a bounded amount
(`max_terminal_adjustment_pp`, default 1.5 percentage points). The weights are
what the weekly optimizer in `signal_store` proposes changes to, which is why
each score has to stay on a common, dimensionless scale.

Every score returns None rather than 0.0 when its input is missing. That
distinction is load-bearing: 0.0 means "the evidence says neutral", None means
"there is no evidence", and the weighted sum must not treat them alike.
"""
from __future__ import annotations

import math
import statistics
from typing import Any


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _clip(value: float) -> float:
    return max(-1.0, min(1.0, value))


def five_minute_score(bars: list[dict[str, Any]]) -> tuple[float | None, dict[str, Any]]:
    """Micro-momentum over the last 30 minutes, against the prior 100.

    Blends the 30-minute return, a relative-volume term, and the share of
    bullish bars. Volume is allowed to be missing — in the extended session it
    routinely is — and the score simply drops that component rather than
    reading thin trade as weakness.
    """
    complete = [bar for bar in bars if isinstance(bar, dict) and bar.get("is_complete") is True]
    if len(complete) < 26:
        return None, {"status": "insufficient", "bars": len(complete)}
    recent = complete[-6:]
    previous = complete[-26:-6]
    first = _finite(recent[0].get("open"))
    last = _finite(recent[-1].get("close"))
    recent_volume = sum(_finite(bar.get("volume")) or 0.0 for bar in recent)
    previous_volumes = [_finite(bar.get("volume")) for bar in previous]
    valid_previous = [value for value in previous_volumes if value is not None and value > 0]
    if first is None or last is None or first <= 0 or len(valid_previous) < 12:
        return None, {"status": "invalid"}
    return_30m = last / first - 1.0
    baseline_6 = statistics.median(valid_previous) * 6.0
    rvol = recent_volume / baseline_6 if baseline_6 > 0 and recent_volume > 0 else None
    bullish_share = sum(
        1 for bar in recent
        if (_finite(bar.get("close")) or 0) > (_finite(bar.get("open")) or 0)
    ) / 6.0
    volume_component = math.tanh((rvol - 1.0) * 1.5) * 0.25 if rvol is not None else 0.0
    score = _clip(math.tanh(return_30m * 85.0) * 0.60 + volume_component
                  + (bullish_share - 0.5) * 0.30)
    return round(score, 6), {
        "status": "available",
        "return_30m_percent": round(return_30m * 100.0, 4),
        "relative_volume": round(rvol, 4) if rvol is not None else None,
        "volume_status": "available" if rvol is not None else "unavailable_in_extended_session",
        "bullish_bar_share": round(bullish_share, 4),
    }


def news_score(items: list[dict[str, Any]]) -> tuple[float | None, dict[str, Any]]:
    """Mean of per-item sentiment, weighted down when the catalyst is weak.

    Each item contributes FinBERT's positive-minus-negative split (70%) plus the
    evidence score the news model assigned it (30%), scaled by how much of a
    catalyst the headline looks like. Items the user deselected are skipped.
    """
    values: list[float] = []
    used: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("selected") is False:
            continue
        finbert = item.get("finbert") if isinstance(item.get("finbert"), dict) else {}
        scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}
        positive = _finite(finbert.get("positive_percent"))
        negative = _finite(finbert.get("negative_percent"))
        overall = _finite(scores.get("overall"))
        catalyst = _finite(scores.get("catalyst"))
        if positive is None or negative is None:
            continue
        sentiment = (positive - negative) / 100.0
        evidence = ((overall if overall is not None else 50.0) - 50.0) / 50.0
        catalyst_weight = max(0.25, min(1.0, (catalyst if catalyst is not None else 50.0) / 100.0))
        values.append(_clip((sentiment * 0.70 + evidence * 0.30) * catalyst_weight))
        used.append(str(item.get("id") or ""))
    if not values:
        return None, {"status": "insufficient", "selected_scored_items": 0}
    return round(_clip(statistics.fmean(values)), 6), {
        "status": "available", "selected_scored_items": len(values), "news_ids": used[:30],
    }


def broker_scores(snapshot: dict[str, Any] | None) -> tuple[dict[str, float | None], dict[str, Any]]:
    """Options / capital-flow / short-pressure scores from a broker snapshot.

    Named `opend_scores` in the platform; renamed because in this project every
    quote already comes from OpenD, so the source is not what distinguishes it.
    The snapshot shape is unchanged — see `signal_research.snapshot`.
    """
    if not isinstance(snapshot, dict) or snapshot.get("data_mode") == "unavailable":
        return {"options_score": None, "flow_score": None, "short_score": None}, {"status": "unavailable"}
    options = snapshot.get("options") if isinstance(snapshot.get("options"), dict) else {}
    flow = snapshot.get("flow") if isinstance(snapshot.get("flow"), dict) else {}
    short = snapshot.get("short") if isinstance(snapshot.get("short"), dict) else {}

    put_call = _finite(options.get("put_call_oi_ratio"))
    options_value = _clip(math.tanh((1.0 - put_call) * 1.25)) if put_call is not None else None

    capital_net = _finite(flow.get("capital_net"))
    capital_in = _finite(flow.get("capital_in_total"))
    capital_out = _finite(flow.get("capital_out_total"))
    denominator = abs(capital_in or 0.0) + abs(capital_out or 0.0)
    flow_value = _clip(capital_net / denominator * 3.0) if capital_net is not None and denominator > 0 else None

    daily_short = _finite(short.get("daily_short_percent"))
    interest = _finite(short.get("short_interest_percent"))
    short_components = []
    if daily_short is not None:
        short_components.append(_clip((45.0 - daily_short) / 30.0))
    if interest is not None:
        short_components.append(_clip((5.0 - interest) / 8.0))
    short_value = statistics.fmean(short_components) if short_components else None

    return {
        "options_score": round(options_value, 6) if options_value is not None else None,
        "flow_score": round(flow_value, 6) if flow_value is not None else None,
        "short_score": round(short_value, 6) if short_value is not None else None,
    }, {"status": snapshot.get("quality", {}).get("status", "unknown"),
        "as_of": snapshot.get("as_of")}


def build_context(*, bars_5m: list[dict[str, Any]], news_items: list[dict[str, Any]],
                  broker_snapshot: dict[str, Any] | None,
                  include_news: bool, include_broker: bool) -> dict[str, Any]:
    """Assemble the five scores plus the diagnostics that explain each one."""
    micro, micro_detail = five_minute_score(bars_5m)
    news_value, news_detail = news_score(news_items) if include_news else (None, {"status": "disabled"})
    broker_values, broker_detail = broker_scores(broker_snapshot) if include_broker else (
        {"options_score": None, "flow_score": None, "short_score": None}, {"status": "disabled"},
    )
    return {
        "scores": {"five_minute_score": micro, "news_score": news_value, **broker_values},
        "details": {"five_minute": micro_detail, "news": news_detail, "broker": broker_detail},
        "included": {"five_minute": True, "news": include_news, "broker": include_broker},
    }


__all__ = ["broker_scores", "build_context", "five_minute_score", "news_score"]
