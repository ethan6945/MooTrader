"""US equity market calendar for the signal desk's forecasting stack.

WHY IT COMPUTES RATHER THAN ENUMERATES. The forecast engine needs more than a
yes/no for today: it has to lay out a strict FUTURE
axis of 30-minute slots across 04:00-20:00 ET sessions, skipping weekends,
holidays and early closes, for 96 steps (three full sessions) at a time. That
needs holidays computed, not enumerated, and it needs the early-close rule.

This module computes the XNYS holiday set from the rules (including Good Friday
via the Gregorian Easter algorithm) so it stays correct in any year, and it is
deliberately free of I/O so the forecast engine stays pure and testable.

Ported from the Stock Probability Prediction Platform's
`companion/us_market_calendar.py`, which this project is merging into the
signal desk. Behaviour is kept identical so a forecast computed here can be
compared bar-for-bar against one the platform produced.

It is used for forecast timestamps and data freshness only; it never places
orders and no execution path imports it.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
EXTENDED_OPEN = time(4, 0)
EXTENDED_CLOSE = time(20, 0)
EARLY_EXTENDED_CLOSE = time(17, 0)

# Unscheduled closures the rules cannot derive. Kept as data, not logic.
EXCEPTIONAL_CLOSURES = {
    date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),
    date(2004, 6, 11), date(2007, 1, 2), date(2012, 10, 29), date(2012, 10, 30),
    date(2018, 12, 5),
}


def _observed(day: date) -> date:
    """Move a fixed-date holiday to the weekday the exchange observes it on."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    # Anonymous Gregorian algorithm. Good Friday is Easter minus two days and is
    # the one market holiday that cannot be written as an nth-weekday rule.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset[date]:
    values = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),    # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),    # Washington's Birthday
        _easter(year) - timedelta(days=2),   # Good Friday
        _last_weekday(year, 5, 0),      # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),    # Labor Day
        _nth_weekday(year, 11, 3, 4),   # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        values.add(_observed(date(year, 6, 19)))   # Juneteenth
    # New Year's Day observed can land on December 31 of the prior year.
    values.add(_observed(date(year + 1, 1, 1)))
    return frozenset(values | {item for item in EXCEPTIONAL_CLOSURES if item.year == year})


def is_session(day: date) -> bool:
    return day.weekday() < 5 and day not in holidays(day.year)


def is_early_close(day: date) -> bool:
    if not is_session(day):
        return False
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        return True
    if day.month == 12 and day.day == 24:
        return True
    if day.month == 7 and day.day == 3:
        return True
    return False


def session_close(day: date) -> time:
    return EARLY_CLOSE if is_early_close(day) else REGULAR_CLOSE


def extended_session_close(day: date) -> time:
    """The advertised late-session close — 20:00 ET, 17:00 on early-close days."""
    return EARLY_EXTENDED_CLOSE if is_early_close(day) else EXTENDED_CLOSE


def session_state(moment: datetime) -> str:
    """One of closed / pre / regular / post for a timezone-aware moment."""
    if moment.tzinfo is None:
        raise ValueError("moment must include a timezone")
    local = moment.astimezone(NEW_YORK)
    if not is_session(local.date()):
        return "closed"
    local_clock = local.time().replace(tzinfo=None)
    if local_clock < EXTENDED_OPEN:
        return "closed"
    if local_clock < REGULAR_OPEN:
        return "pre"
    if local_clock < session_close(local.date()):
        return "regular"
    if local_clock < extended_session_close(local.date()):
        return "post"
    return "closed"


def next_session(day: date) -> date:
    cursor = day + timedelta(days=1)
    while not is_session(cursor):
        cursor += timedelta(days=1)
    return cursor


def future_30m_slots(after: datetime, steps: int) -> list[datetime]:
    """Build a strict future axis of 30-minute slots across 04:00-20:00 ET.

    Slots are stamped at the bar's OPEN, which is the convention the forecast
    engine's features and the bar adapter both use. Early-close dates end at
    17:00 ET. The overnight session is deliberately excluded: it is not in the
    broker's ETH session either, so including it would invent bars no data
    source can later settle against.
    """
    if after.tzinfo is None:
        raise ValueError("after must include a timezone")
    if steps < 1 or steps > 260:
        raise ValueError("steps must be between 1 and 260")
    local = after.astimezone(NEW_YORK)
    cursor_day = local.date()
    cursor_clock = (local + timedelta(minutes=30)).time().replace(tzinfo=None)
    output: list[datetime] = []
    while len(output) < steps:
        if not is_session(cursor_day):
            cursor_day = next_session(cursor_day)
            cursor_clock = EXTENDED_OPEN
            continue
        close_clock = extended_session_close(cursor_day)
        if cursor_clock < EXTENDED_OPEN:
            cursor_clock = EXTENDED_OPEN
        while cursor_clock < close_clock and len(output) < steps:
            output.append(datetime.combine(cursor_day, cursor_clock, tzinfo=NEW_YORK))
            cursor_clock = (datetime.combine(cursor_day, cursor_clock)
                            + timedelta(minutes=30)).time()
        if len(output) < steps:
            cursor_day = next_session(cursor_day)
            cursor_clock = EXTENDED_OPEN
    return output


__all__ = [
    "EARLY_EXTENDED_CLOSE", "EXTENDED_CLOSE", "EXTENDED_OPEN", "NEW_YORK",
    "extended_session_close", "future_30m_slots", "holidays", "is_early_close",
    "is_session", "next_session", "session_close", "session_state",
]
