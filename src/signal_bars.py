"""Broker klines → forecast-engine bars, for the signal desk's forecasting stack.

The forecast engine (`signal_forecast`) was written against a bar contract that
the broker does not speak. This module is the whole of the translation, kept in
one place so the engine can stay pure and the differences stay documented
rather than scattered:

  STAMP CONVENTION. OpenD stamps a kline with the bar's CLOSE time — a 30-minute
  bar covering 04:00-04:30 ET arrives as 04:30. The engine stamps bars at their
  OPEN, because its time-of-day features (slot_sin/slot_cos) and its future axis
  both index the slot a bar STARTS in. Feeding close-stamped bars in shifts every
  bar one slot late and, worse, produces a 20:00 stamp that is not a valid
  session slot at all — the engine rejects the whole request with
  `invalid_session_bar`. So every timestamp is moved back one interval here.

  TIMEZONE. OpenD returns tz-naive timestamps in the exchange's local time. The
  engine requires tz-aware input and converts to New York. Localizing to
  America/New_York is therefore the identity, not a conversion, and it keeps DST
  correct without the caller having to think about it. The 04:00-20:00 window
  never overlaps the 02:00-03:00 DST gap, so no timestamp here is ambiguous.

  COMPLETENESS. `request_history_kline` can include the bar currently forming.
  The engine must never see a partial bar — half a bar of volume reads as a
  volume collapse — so a bar is emitted only once its close time has passed.

  SESSION. The engine wants 04:00-20:00 ET, which is the broker's "ETH" session
  (`_SESSION_HOURS["ETH"] == 16.0`), 32 thirty-minute slots per regular day.

None of this places orders; it only reshapes history the broker already returned.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd
from moomoo import KLType

from .signal_calendar import (
    EXTENDED_OPEN, NEW_YORK, extended_session_close, is_session,
)

log = logging.getLogger(__name__)

# The broker session that spans 04:00-20:00 ET.
ETH = "ETH"

# 32 slots/day at 30 minutes, 192 at 5 minutes. Used to size broker requests
# from a wanted number of SESSIONS rather than a raw bar count, because "ten
# trading days" is the unit the engine actually validates against.
_SLOTS_PER_SESSION = {30: 32, 5: 192}

_INTERVAL_KTYPE = {30: KLType.K_30M, 5: KLType.K_5M}


def _now_ny(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(NEW_YORK)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    return now.astimezone(NEW_YORK)


def _valid_slots(interval_minutes: int) -> set[tuple[int, int]]:
    """The open-stamps a bar of this interval may legally carry, 04:00-20:00 ET."""
    count = _SLOTS_PER_SESSION[interval_minutes]
    return {
        (4 + ((slot * interval_minutes) // 60), (slot * interval_minutes) % 60)
        for slot in range(count)
    }


def to_engine_bars(df: pd.DataFrame, interval_minutes: int,
                   now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Convert a broker kline frame into the engine's bar dicts.

    Returns bars ascending by time, open-stamped, timezone-aware, and complete.
    """
    if df is None or len(df) == 0:
        return []
    if interval_minutes not in _SLOTS_PER_SESSION:
        raise ValueError(f"unsupported interval {interval_minutes}")

    now_ny = _now_ny(now)
    valid = _valid_slots(interval_minutes)
    frame = df.copy()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()

    # Close stamp → open stamp, then attach the exchange's timezone.
    index = pd.DatetimeIndex(frame.index) - pd.Timedelta(minutes=interval_minutes)
    if index.tz is None:
        index = index.tz_localize(NEW_YORK)
    else:
        index = index.tz_convert(NEW_YORK)
    frame.index = index

    bars: list[dict[str, Any]] = []
    off_slot = 0
    for timestamp, row in frame.iterrows():
        moment = timestamp.to_pydatetime()
        if (moment.hour, moment.minute) not in valid or not is_session(moment.date()):
            # Outside the advertised session — an overnight bar, or a stamp the
            # broker placed off the grid. Dropped rather than passed on, because
            # the engine rejects the entire request over a single bad slot.
            off_slot += 1
            continue
        if moment + timedelta(minutes=interval_minutes) > now_ny:
            continue                      # still forming
        try:
            values = [float(row["open"]), float(row["high"]),
                      float(row["low"]), float(row["close"]), float(row["volume"])]
        except (KeyError, TypeError, ValueError):
            continue
        if any(value != value for value in values):    # NaN
            continue
        bars.append({
            "time": moment.isoformat(),
            "open": values[0], "high": values[1], "low": values[2],
            "close": values[3], "volume": values[4],
            "is_complete": True,
        })
    if off_slot:
        log.debug("signal_bars: dropped %d off-grid %dm bars", off_slot, interval_minutes)
    return bars


def _fetch(client, symbol: str, interval_minutes: int, sessions: int,
           now: Optional[datetime] = None) -> list[dict[str, Any]]:
    slots = _SLOTS_PER_SESSION[interval_minutes]
    # One extra session of slack absorbs early closes and the session in
    # progress, both of which return fewer bars than a full day.
    wanted = slots * (sessions + 1)
    df = client.get_kline(symbol, bars=wanted,
                          ktype=_INTERVAL_KTYPE[interval_minutes], session=ETH)
    return to_engine_bars(df, interval_minutes, now=now)


def fetch_30m(client, symbol: str, sessions: int = 10,
              now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Completed 30-minute ETH bars covering roughly `sessions` trading days."""
    return _fetch(client, symbol, 30, sessions, now=now)


def fetch_5m(client, symbol: str, sessions: int = 2,
             now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Completed 5-minute ETH bars — the micro-context score's only input."""
    return _fetch(client, symbol, 5, sessions, now=now)


def market_as_of(bars: list[dict[str, Any]]) -> Optional[str]:
    """The identity a forecast is frozen against: the last complete bar's stamp.

    Everything downstream — the cache key, the stored run, the settlement join —
    keys off this string, so it must come from the data and never from the wall
    clock. Two calls made ten minutes apart inside the same 30-minute bar
    produce the same `market_as_of`, and therefore the same forecast.
    """
    if not bars:
        return None
    return str(bars[-1]["time"])


def session_coverage(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """How many distinct sessions the bars span, and how full the latest one is.

    Used to explain a fail-closed refusal in words a user can act on, instead of
    surfacing the engine's `insufficient_data` code on its own.
    """
    if not bars:
        return {"sessions": 0, "bars": 0, "latest_session": None,
                "latest_session_bars": 0, "latest_session_expected": 0}
    days: dict[str, int] = {}
    for bar in bars:
        day = str(bar["time"])[:10]
        days[day] = days.get(day, 0) + 1
    latest = max(days)
    latest_date = datetime.fromisoformat(str(bars[-1]["time"])).date()
    close_clock = extended_session_close(latest_date)
    expected = int(
        ((close_clock.hour * 60 + close_clock.minute)
         - (EXTENDED_OPEN.hour * 60 + EXTENDED_OPEN.minute)) / 30
    )
    return {
        "sessions": len(days),
        "bars": len(bars),
        "latest_session": latest,
        "latest_session_bars": days[latest],
        "latest_session_expected": expected,
    }


__all__ = ["ETH", "fetch_30m", "fetch_5m", "market_as_of", "session_coverage",
           "to_engine_bars"]
