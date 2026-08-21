"""Chart-pattern detection — pure-algorithm geometric + candlestick recognition.

This is the foundation `strategy_pattern.evaluate()` builds on. It does what the
licensed broker engines (Autochartist / Trading Central / Acuity) do under the
hood: locate the geometry of a classic pattern and return a confidence plus the
key price levels (breakout/neckline, target, invalidation/stop).

Design constraints (match the project):
  • Only numpy/pandas — no scipy, no new dependency. Swing pivots are found with
    a simple ±k local-extreme scan rather than scipy.signal.argrelextrema.
  • The bot is LONG-ONLY, so we detect BULLISH setups (reversal + continuation).
    Bearish geometry is not used for entries (it could later feed the gap-exit
    sentinel, but that is out of scope here).
  • Everything is deterministic so the same detection runs in live AND backtest.

Each detector returns a dict (or None):
  {
    "type": str,                 # e.g. "double_bottom"
    "direction": "bullish",
    "confidence": float,         # 0-100 geometric quality
    "triggered": bool,           # has the breakout/confirmation already fired?
    "key_levels": {
        "breakout": float,       # the level price must clear (neckline / resistance)
        "target": float,         # measured-move objective
        "invalidation": float,   # pattern fails below this (stop reference)
    },
    "bar_span": int,             # bars the pattern occupies (recency proxy)
  }
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .timeframe import TF


# ----------------------------------------------------------------------------
# Pivot / trend-line primitives
# ----------------------------------------------------------------------------

def find_pivots(df: pd.DataFrame, k: int = 3) -> tuple[list[int], list[int]]:
    """Indices of swing highs and swing lows.

    A bar i is a swing high if its high is the strict max of the ±k window
    (and the unique argmax, so ties don't double-count). Swing lows are
    symmetric on the low series. The last k bars can't be confirmed as pivots
    yet — that's intentional; patterns are built from confirmed pivots and the
    latest close acts as the breakout trigger.
    """
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    n = len(df)
    highs, lows = [], []
    for i in range(k, n - k):
        wh = h[i - k:i + k + 1]
        if h[i] == wh.max() and wh.argmax() == k:
            highs.append(i)
        wl = l[i - k:i + k + 1]
        if l[i] == wl.min() and wl.argmin() == k:
            lows.append(i)
    return highs, lows


def _fit_line(idx: list[int], vals: np.ndarray) -> tuple[float, float]:
    """Least-squares slope, intercept for points (idx, vals[idx])."""
    x = np.asarray(idx, dtype=float)
    y = np.asarray(vals[idx], dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    return float(slope), float(intercept)


def _pct(a: float, b: float) -> float:
    """|a-b| / |b| as a fraction; safe on zero."""
    return abs(a - b) / abs(b) if b else 1.0


def _min_height(df: pd.DataFrame, price: float, atr_mult: float = 1.2,
                pct: float = 0.02) -> float:
    """Minimum vertical extent for a pattern to count as SIGNIFICANT.

    The single most important false-positive filter: without it, a flat/noisy
    chart trivially satisfies the geometry of a double bottom / H&S / triangle
    (a tiny sine wave "looks like" a head & shoulders). Real pattern engines
    only flag patterns whose price swing is meaningful, so we require the
    pattern's height ≥ max(1.2×ATR, 2% of price).
    """
    atr = _atr(df, 14)
    return max(atr * atr_mult, abs(price) * pct)


# ----------------------------------------------------------------------------
# Geometric patterns (bullish)
# ----------------------------------------------------------------------------

def detect_double_bottom(df, highs, lows, tol=0.03) -> dict | None:
    """Two similar swing lows with a peak between — bullish reversal.

    Neckline = the intervening peak. Triggers when the latest close clears it.
    Measured move target = neckline + (neckline - bottom).
    """
    if len(lows) < 2:
        return None
    i1, i2 = lows[-2], lows[-1]
    l1, l2 = df["low"].iloc[i1], df["low"].iloc[i2]
    if _pct(l1, l2) > tol:            # bottoms must be at a similar price
        return None
    if i2 - i1 < 4:                   # too close → noise, not a real W
        return None
    mids = [h for h in highs if i1 < h < i2]
    if not mids:
        return None
    neck_i = max(mids, key=lambda h: df["high"].iloc[h])
    neckline = float(df["high"].iloc[neck_i])
    bottom = float(min(l1, l2))
    last = float(df["close"].iloc[-1])
    if neckline <= bottom or (neckline - bottom) < _min_height(df, last):
        return None
    symmetry = 1.0 - _pct(l1, l2) / tol           # 1 = identical bottoms
    depth = (neckline - bottom) / bottom          # bigger W → more meaningful
    conf = 55 + 25 * symmetry + min(20.0, depth * 200)
    return {
        "type": "double_bottom", "direction": "bullish",
        "confidence": round(min(95.0, conf), 1),
        "triggered": last > neckline,
        "key_levels": {
            "breakout": round(neckline, 2),
            "target": round(neckline + (neckline - bottom), 2),
            "invalidation": round(bottom, 2),
        },
        "bar_span": len(df) - i1,
    }


def detect_inverse_head_shoulders(df, highs, lows, tol=0.04) -> dict | None:
    """Left shoulder / head (lowest) / right shoulder with symmetric shoulders.

    Neckline = line through the two reaction highs between the lows; triggers
    when the latest close clears the (projected) neckline at the current bar.
    """
    if len(lows) < 3:
        return None
    ls, head, rs = lows[-3], lows[-2], lows[-1]
    pls, phead, prs = (float(df["low"].iloc[x]) for x in (ls, head, rs))
    if not (phead < pls and phead < prs):         # head must be the lowest
        return None
    if _pct(pls, prs) > tol:                       # shoulders roughly level
        return None
    last = float(df["close"].iloc[-1])
    min_h = _min_height(df, last)
    # Head must sit meaningfully below the shoulders, and the whole structure
    # must be significant — otherwise three shallow sine-wave dips qualify.
    if (min(pls, prs) - phead) < 0.4 * min_h:
        return None
    mids = [h for h in highs if ls < h < rs]
    if len(mids) < 1:
        return None
    # Neckline through the reaction highs (flat if only one).
    if len(mids) >= 2:
        slope, intercept = _fit_line([mids[0], mids[-1]], df["high"].to_numpy(float))
        neck_now = slope * (len(df) - 1) + intercept
    else:
        neck_now = float(df["high"].iloc[mids[0]])
    if (neck_now - phead) < min_h:                 # neckline-to-head must matter
        return None
    symmetry = 1.0 - _pct(pls, prs) / tol
    conf = 55 + 25 * symmetry + 15 * (1.0 if len(mids) >= 2 else 0.5)
    return {
        "type": "inverse_head_shoulders", "direction": "bullish",
        "confidence": round(min(95.0, conf), 1),
        "triggered": last > neck_now,
        "key_levels": {
            "breakout": round(float(neck_now), 2),
            "target": round(float(neck_now) + (float(neck_now) - phead), 2),
            "invalidation": round(phead, 2),
        },
        "bar_span": len(df) - ls,
    }


def detect_ascending_triangle(df, highs, lows) -> dict | None:
    """Flat resistance + rising support — bullish continuation."""
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh, ll = highs[-3:], lows[-3:]
    if len(hh) < 2 or len(ll) < 2:
        return None
    high_arr = df["high"].to_numpy(float)
    low_arr = df["low"].to_numpy(float)
    h_slope, h_int = _fit_line(hh, high_arr)
    l_slope, l_int = _fit_line(ll, low_arr)
    res = float(np.mean([high_arr[i] for i in hh]))
    # Resistance ~flat (small |slope| vs price) and support clearly rising.
    flat_res = abs(h_slope) / res < 0.0015 if res else False
    rising_sup = l_slope > 0
    if not (flat_res and rising_sup):
        return None
    last = float(df["close"].iloc[-1])
    base = float(low_arr[ll[0]])
    if (res - base) < _min_height(df, last):        # triangle must be significant
        return None
    conf = 60 + min(20.0, l_slope / res * 5000 if res else 0) + 10
    return {
        "type": "ascending_triangle", "direction": "bullish",
        "confidence": round(min(92.0, conf), 1),
        "triggered": last > res,
        "key_levels": {
            "breakout": round(res, 2),
            "target": round(res + (res - base), 2),
            "invalidation": round(float(l_slope * (len(df) - 1) + l_int), 2),
        },
        "bar_span": len(df) - min(hh[0], ll[0]),
    }


def detect_symmetric_triangle(df, highs, lows) -> dict | None:
    """Descending highs + ascending lows that converge. Long-only ⇒ we only
    act on an UPSIDE break, so it's filed as a bullish continuation candidate."""
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh, ll = highs[-3:], lows[-3:]
    if len(hh) < 2 or len(ll) < 2:
        return None
    high_arr = df["high"].to_numpy(float)
    low_arr = df["low"].to_numpy(float)
    h_slope, h_int = _fit_line(hh, high_arr)
    l_slope, l_int = _fit_line(ll, low_arr)
    if not (h_slope < 0 and l_slope > 0):          # converging wedge of indecision
        return None
    n = len(df) - 1
    upper = h_slope * n + h_int
    lower = l_slope * n + l_int
    if upper <= lower:                              # apex passed
        return None
    last = float(df["close"].iloc[-1])
    height = float(high_arr[hh[0]] - low_arr[ll[0]])
    if height < _min_height(df, last):              # ignore micro-triangles (noise)
        return None
    conf = 58 + 12
    return {
        "type": "symmetric_triangle", "direction": "bullish",
        "confidence": round(conf, 1),
        "triggered": last > upper,
        "key_levels": {
            "breakout": round(float(upper), 2),
            "target": round(float(upper) + height, 2),
            "invalidation": round(float(lower), 2),
        },
        "bar_span": len(df) - min(hh[0], ll[0]),
    }


def detect_falling_wedge(df, highs, lows) -> dict | None:
    """Both boundaries fall but the highs fall faster (converging down) →
    bullish reversal. Breaks to the upside through the upper line."""
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh, ll = highs[-3:], lows[-3:]
    if len(hh) < 2 or len(ll) < 2:
        return None
    high_arr = df["high"].to_numpy(float)
    low_arr = df["low"].to_numpy(float)
    h_slope, h_int = _fit_line(hh, high_arr)
    l_slope, l_int = _fit_line(ll, low_arr)
    if not (h_slope < 0 and l_slope < 0 and h_slope < l_slope):
        return None                                 # converging (highs steeper)
    n = len(df) - 1
    upper = h_slope * n + h_int
    last = float(df["close"].iloc[-1])
    top = float(high_arr[hh[0]])
    bottom = float(low_arr[ll[-1]])
    if (top - bottom) < _min_height(df, last):      # ignore micro-wedges (noise)
        return None
    conf = 56 + 14
    return {
        "type": "falling_wedge", "direction": "bullish",
        "confidence": round(conf, 1),
        "triggered": last > upper,
        "key_levels": {
            "breakout": round(float(upper), 2),
            "target": round(top, 2),                # wedges retrace to their origin
            "invalidation": round(bottom, 2),
        },
        "bar_span": len(df) - min(hh[0], ll[0]),
    }


def detect_bull_flag(df, tf: TF) -> dict | None:
    """Sharp rally (pole) then a tight, slightly-down consolidation (flag).
    Breaks out above the flag's high. Continuation pattern."""
    if len(df) < 14:
        return None
    close = df["close"].to_numpy(float)
    atr = _atr(df, tf.atr_period)
    if atr <= 0:
        return None
    pole = close[-12:-5]                            # the run-up window
    flag = df.iloc[-5:]                             # the consolidation window
    pole_gain = pole[-1] - pole[0]
    if pole_gain < 2.0 * atr:                       # need a real pole
        return None
    flag_high = float(flag["high"].max())
    flag_low = float(flag["low"].min())
    flag_range = flag_high - flag_low
    if flag_range > 1.5 * atr:                      # flag must stay tight
        return None
    last = float(df["close"].iloc[-1])
    conf = 58 + min(22.0, (pole_gain / atr) * 4)
    return {
        "type": "bull_flag", "direction": "bullish",
        "confidence": round(min(90.0, conf), 1),
        "triggered": last > flag_high,
        "key_levels": {
            "breakout": round(flag_high, 2),
            "target": round(flag_high + pole_gain, 2),  # measured move = pole height
            "invalidation": round(flag_low, 2),
        },
        "bar_span": 12,
    }


def detect_breakout(df, tf: TF) -> dict | None:
    """Plain N-bar range breakout (Donchian) — the simplest, highest-value
    continuation signal. Mirrors indicators._score_pattern's breakout leg."""
    lb = tf.breakout_lookback
    if len(df) < lb + 2:
        return None
    prior_high = float(df["high"].iloc[-(lb + 1):-1].max())
    prior_low = float(df["low"].iloc[-(lb + 1):-1].min())
    atr = _atr(df, tf.atr_period)
    last = float(df["close"].iloc[-1])
    if atr <= 0 or prior_high <= prior_low:
        return None
    over_atr = (last - prior_high) / atr
    rng = prior_high - prior_low
    conf = 50 + min(35.0, max(0.0, over_atr) * 70)
    return {
        "type": "range_breakout", "direction": "bullish",
        "confidence": round(conf, 1),
        "triggered": last > prior_high,
        "key_levels": {
            "breakout": round(prior_high, 2),
            "target": round(prior_high + rng, 2),
            "invalidation": round(prior_high - 0.5 * rng, 2),
        },
        "bar_span": lb,
    }


# ----------------------------------------------------------------------------
# Candlestick patterns (bullish) — single/double/triple bar
# ----------------------------------------------------------------------------

def detect_candlestick(df, tf: TF) -> dict | None:
    """Best bullish candlestick on the latest bar(s): hammer, bullish engulfing,
    or morning star. Hammer logic mirrors strategy_mr._score_reversal."""
    if len(df) < 4:
        return None
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    atr = _atr(df, tf.atr_period)
    rng = max(h[-1] - low[-1], 1e-9)
    body = abs(c[-1] - o[-1])
    lower_wick = (min(c[-1], o[-1]) - low[-1]) / rng
    upper_wick = (h[-1] - max(c[-1], o[-1])) / rng
    green = c[-1] > o[-1]

    # Hammer: long lower wick, small upper wick, closes green-ish.
    if lower_wick >= 0.5 and upper_wick <= 0.15 and c[-1] >= o[-1] * 0.999:
        target = c[-1] + (atr * 2 if atr > 0 else rng)
        return _candle("hammer", c[-1], low[-1], target, 60 + 20 * (lower_wick - 0.5) / 0.5)

    # Bullish engulfing: prev red, curr green body engulfs prev body.
    prev_red = c[-2] < o[-2]
    if green and prev_red and c[-1] >= o[-2] and o[-1] <= c[-2]:
        engulf = body / max(abs(c[-2] - o[-2]), 1e-9)
        target = c[-1] + (atr * 2 if atr > 0 else rng)
        return _candle("bullish_engulfing", c[-1], min(low[-1], low[-2]), target,
                       60 + min(25.0, (engulf - 1) * 25))

    # Morning star: big red, small indecision body, big green into the first body.
    big_red = (o[-3] - c[-3]) > 0.5 * atr if atr > 0 else c[-3] < o[-3]
    small_mid = abs(c[-2] - o[-2]) < 0.3 * abs(o[-3] - c[-3] + 1e-9)
    big_green = green and (c[-1] - o[-1]) > 0.5 * atr if atr > 0 else green
    if big_red and small_mid and big_green and c[-1] > (o[-3] + c[-3]) / 2:
        target = c[-1] + (atr * 2 if atr > 0 else rng)
        return _candle("morning_star", c[-1], low[-2], target, 70)

    return None


def _candle(name, last, inval, target, conf) -> dict:
    return {
        "type": name, "direction": "bullish",
        "confidence": round(min(90.0, max(0.0, conf)), 1),
        "triggered": True,                          # candlestick fires on close
        "key_levels": {
            "breakout": round(float(last), 2),
            "target": round(float(target), 2),
            "invalidation": round(float(inval), 2),
        },
        "bar_span": 3,
    }


# ----------------------------------------------------------------------------
# Aggregator
# ----------------------------------------------------------------------------

def _atr(df: pd.DataFrame, period: int) -> float:
    """Wilder ATR (RMA), matching ta.atr — which is what the rest of this
    system means by "ATR".

    This said "Wilder ATR" and returned np.mean(tr[-period:]), a simple mean.
    They are not the same number: measured across 120 windows of real 60m
    bars, the simple mean sits a median 8% away from Wilder, 18% at the 90th
    percentile, 34% at the worst — and higher 74% of the time.

    Every threshold in this module is an ATR MULTIPLE (1.2x for pattern
    height, 2.0x for a flag pole) and every other module — indicators,
    strategy_momentum, strategy_mr, strategy_pattern, moo_client, universe,
    reconcile — takes ta.atr. A multiple only means something against an
    agreed unit, so a systematically larger ATR here made pattern detection
    quietly stricter than the numbers say, and pushed breakout confidence
    (conf = 50 + over_atr * 70) down. universe.py hit this and was migrated
    to ta.atr; its comment at line 128 names the same disagreement.

    Still no pandas_ta import: the recurrence is four lines. The seed is the
    simple mean of the first `period` true ranges, which is Wilder's own.
    """
    if len(df) < period + 1:
        return 0.0
    h = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    prev_c = c[:-1]
    tr = np.maximum(h[1:] - low[1:],
                    np.maximum(np.abs(h[1:] - prev_c), np.abs(low[1:] - prev_c)))
    if len(tr) < period:
        return 0.0
    atr = float(np.mean(tr[:period]))
    for x in tr[period:]:
        atr = (atr * (period - 1) + float(x)) / period
    return atr


def structural_stop(df: pd.DataFrame, entry_price: float,
                    atr_stop: float) -> float:
    """P0-2 (2026-06-26): Return the TIGHTER of the nearest swing-low stop and
    the ATR-based stop. A stop placed at a visible structural level (prior swing
    low minus 1%) is invisible to market-makers scanning for ATR-stop clusters,
    yet still bounded by the ATR stop so it can never be wider than the
    risk-model permits. If no pivots are found, returns the ATR stop unchanged.

    Sources: Al Brooks price-action trading, Bob Volman's structural stop method.
    """
    if df is None or len(df) < 5:
        return atr_stop
    try:
        highs, lows = find_pivots(df)
        if not lows:
            return atr_stop
        nearest_low = float(df["low"].iloc[lows[-1]])
        structural = nearest_low * 0.99  # 1% below the swing low
        # Never use a structural stop BELOW the ATR stop — that would exceed
        # the risk budget. Take the tighter of the two.
        return max(structural, atr_stop)
    except Exception:
        return atr_stop
    """Run every detector and return the found patterns, highest confidence first.

    Each detector is wrapped so a single bad-geometry edge case can never crash
    the scan (mirrors the project's never-block-on-error policy).
    """
    if df is None or len(df) < 20:
        return []
    highs, lows = find_pivots(df, k=pivot_k)
    found: list[dict] = []
    detectors = [
        lambda: detect_breakout(df, tf),
        lambda: detect_double_bottom(df, highs, lows),
        lambda: detect_inverse_head_shoulders(df, highs, lows),
        lambda: detect_ascending_triangle(df, highs, lows),
        lambda: detect_symmetric_triangle(df, highs, lows),
        lambda: detect_falling_wedge(df, highs, lows),
        lambda: detect_bull_flag(df, tf),
        lambda: detect_candlestick(df, tf),
    ]
    for fn in detectors:
        try:
            res = fn()
        except Exception:
            res = None
        if res:
            found.append(res)
    found.sort(key=lambda p: p["confidence"], reverse=True)
    return found
