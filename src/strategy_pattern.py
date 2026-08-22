"""Chart-pattern strategy — scores classic geometric/candlestick setups.

This is the fourth strategy alongside trend / momentum_break / mean_revert. It
asks a different question than the indicator scorers: "is a recognizable chart
PATTERN completing here?" — double bottoms, triangles, inverse H&S, wedges,
flags, range breakouts, and bullish candlesticks (see `pattern_detect`).

Convention (identical to the other strategies, so the funnel is agnostic):
  • 0-100 score on the same scale. Weights sum to 90; the remaining ~10 is the
    AI's — this was `pattern_vision` (chart-image confirmation), deleted
    consulted in main.py exactly where `ai_validator.validate` runs.
  • Returns a Signal with strategy="pattern". The detected pattern's type,
    confidence and key levels ride along in Signal.meta for audit + vision +
    the web dashboard (NOT in the numeric `breakdown`).

The geometry is fully deterministic, so this scorer runs identically in live and
in backtest_v3 (the vision layer is live-only and additive — see plan notes).
"""
from __future__ import annotations

import pandas as pd
import pandas_ta_classic as ta

from .indicators import Signal
from . import pattern_detect
from .config import settings
from .timeframe import TF, current as _tf


# Sum to 90 (vision keeps the remaining 10). The pattern's own quality and the
# breakout TRIGGER carry the most weight; volume + trend alignment confirm it.
WEIGHTS = {
    "pattern_quality": 30,   # geometric fit / symmetry (detector confidence)
    "trigger":         25,   # has price actually broken the key level yet?
    "volume":          20,   # conviction on the trigger bar
    "trend_alignment": 15,   # not fighting the EMA9/21 backdrop
}


def _score_pattern_quality(best: dict) -> tuple[float, str]:
    conf = float(best.get("confidence", 0))
    return conf, f"{best['type']} quality {conf:.0f}"


def _score_trigger(best: dict, df: pd.DataFrame) -> tuple[float, str]:
    """Full credit once the pattern has triggered (broken its key level); partial
    credit when price is coiled just below it (anticipation)."""
    if best.get("triggered"):
        return 100, "pattern triggered (level cleared)"
    level = best.get("key_levels", {}).get("breakout")
    last = float(df["close"].iloc[-1])
    if level and level > 0:
        gap = (level - last) / level
        if gap <= 0.005:                      # within 0.5% of the trigger
            return 55, f"coiled {gap*100:.1f}% below trigger"
        if gap <= 0.02:
            return 30, f"{gap*100:.1f}% below trigger"
    return 0, "not triggered"


def _score_volume(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    """Same volume-surge logic the other strategies use — real breakouts need
    conviction, not drift."""
    vol_ma = df["volume"].rolling(tf.vol_ma).mean().iloc[-1]
    today = df["volume"].iloc[-1]
    ratio = today / vol_ma if vol_ma else 0
    if ratio >= 1.5:
        return 100, f"vol surge ×{ratio:.2f}"
    if ratio >= 1.2:
        return 70, f"vol confirming ×{ratio:.2f}"
    if ratio >= 1.0:
        return 40, f"vol normal ×{ratio:.2f}"
    return 10, f"vol weak ×{ratio:.2f}"


def _admit(patterns: list[dict], df: pd.DataFrame | None = None) -> list[dict]:
    """Apply the configurable admission filters (settings.pattern_*). Read at
    call-time so a backtest sweep can mutate the settings singleton between runs.
    Defaults are inert: all types, no confidence floor, triggered-optional.

    P0-1 (2026-06-26): Wyckoff volume-confirmation hard filter. A triggered
    breakout without volume conviction is a false breakout — filter it, not just
    score it lower. The 1.5× average-volume threshold is the same bar used by
    every volume-confirmation method since Livermore."""
    allowed = settings.pattern_allowed_types
    min_conf = settings.pattern_min_confidence
    req_trig = settings.pattern_require_triggered
    out = []
    for p in patterns:
        if allowed and p["type"] not in allowed:
            continue
        if p["confidence"] < min_conf:
            continue
        if req_trig and not p["triggered"]:
            continue
        # P0-1: volume hard-filter — a triggered breakout MUST have conviction
        # volume (≥1.5× 20-bar average). Patterns that haven't triggered yet
        # (anticipation entries) skip this check — they're coiled, not breaking.
        if p.get("triggered") and df is not None and len(df) >= 21:
            bar_vol = float(df["volume"].iloc[-1])
            avg_vol = float(df["volume"].iloc[-21:-1].mean())
            if avg_vol > 0 and bar_vol < avg_vol * 1.5:
                # Downgrade confidence instead of hard-dropping — caller still
                # sees it in patterns_all for audit, but the score takes the hit.
                p = dict(p)
                p["confidence"] = max(0.0, p["confidence"] - 25)
                p["volume_confirmed"] = False
            else:
                p = dict(p)
                p["volume_confirmed"] = True
        out.append(p)
    return out


def _score_trend_alignment(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    """A bullish pattern with the short-term EMA backdrop up is higher quality.
    We don't hard-block counter-trend reversals (double bottoms are SUPPOSED to
    fire in downtrends), so the floor is 40, not 0."""
    ema_f = ta.ema(df["close"], length=tf.ema_fast).iloc[-1]
    ema_s = ta.ema(df["close"], length=tf.ema_slow).iloc[-1]
    if pd.isna(ema_f) or pd.isna(ema_s):
        return 50, "EMA n/a — neutral"
    if ema_f > ema_s:
        return 100, f"EMA{tf.ema_fast}>EMA{tf.ema_slow} (with-trend)"
    return 40, f"EMA{tf.ema_fast}<=EMA{tf.ema_slow} (reversal context)"


def evaluate(symbol: str, df: pd.DataFrame) -> Signal:
    """Score the best bullish chart pattern. Returns Signal(strategy='pattern')."""
    tf = _tf()
    min_bars = max(tf.ema_slow, tf.atr_period, tf.breakout_lookback, 20) + 5
    if len(df) < min_bars:
        return Signal(
            symbol=symbol,
            price=float(df["close"].iloc[-1]) if len(df) else 0,
            atr=0, score=0, reasons=["insufficient data"],
            strategy="pattern",
        )

    patterns = _admit(pattern_detect.detect_all(df, tf), df)
    price = float(df["close"].iloc[-1])
    atr = float(ta.atr(df["high"], df["low"], df["close"], length=tf.atr_period).iloc[-1])
    if not patterns:
        return Signal(symbol=symbol, price=price, atr=atr, score=0,
                      strategy="pattern", reasons=["no admitted pattern"])

    best = patterns[0]
    factors = {
        "pattern_quality": _score_pattern_quality(best),
        "trigger":         _score_trigger(best, df),
        "volume":          _score_volume(df, tf),
        "trend_alignment": _score_trend_alignment(df, tf),
    }
    weighted = sum(score * WEIGHTS[name] / 100 for name, (score, _) in factors.items())

    return Signal(
        symbol=symbol,
        price=price,
        atr=atr,
        score=round(weighted, 1),
        breakdown={name: score for name, (score, _) in factors.items()},
        reasons=[f"pattern: {best['type']} (conf {best['confidence']:.0f}, "
                 f"trig={best['triggered']})"]
                + [f"{name}: {reason}" for name, (_, reason) in factors.items()],
        strategy="pattern",
        meta={
            "pattern_type": best["type"],
            "pattern_confidence": best["confidence"],
            "pattern_triggered": best["triggered"],
            "key_levels": best["key_levels"],
            # all patterns found this bar (compact) — useful for audit/debug
            "patterns_all": [(p["type"], p["confidence"]) for p in patterns],
        },
    )
