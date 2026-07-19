"""Authoritative NY time + clock-drift detection.

pytz already handles EDT/EST DST switching correctly given the right tz
database (auto-updated by the OS).  What it can't catch: a misconfigured
or drifted machine clock.  If your laptop is 5 minutes fast, the bot will
fire trades 5 minutes early — past the 09:45 ET entry window cutoff if you
were just before it, or into the 15:30 ET MOC zone you're supposed to avoid.

This module:
  • Returns NY-aware datetimes via `ny_now()` (drop-in replacement for the
    old `datetime.now(pytz.timezone('America/New_York'))` calls).
  • Periodically (default: hourly) queries NTP (NIST / Apple) — falling back
    to two public HTTP time APIs that must AGREE with each other — to compute
    the offset between the network and the local clock.
  • If drift > MAX_DRIFT_SEC, applies the offset so every subsequent call
    returns the network-authoritative time.  A single unverified HTTP source
    is never enough to apply a correction.
  • Caches the offset; non-validation calls are zero-cost.
  • Falls through to plain pytz if all network sources fail (so we never
    hard-fail when offline — just log a warning).

DST handling is automatic: the API responses are in America/New_York with
the correct EDT/EST abbreviation, and `pytz.timezone(...)` localises them
the same way.  Spring-forward / fall-back happens without any code change.
"""
from __future__ import annotations

import logging
import struct
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
import requests

log = logging.getLogger(__name__)

NY_TZ = pytz.timezone("America/New_York")

# How often we hit the network to re-check drift.
CHECK_INTERVAL_SEC = 3600              # once per hour is plenty
# Anything beyond this is treated as actual drift worth correcting.
MAX_DRIFT_SEC = 5.0
# HTTP request timeout (seconds).
HTTP_TIMEOUT = 4.0
# NTP servers (RFC 5905), tried in order. NIST is the canonical US reference;
# Apple is what macOS itself syncs against.
NTP_SERVERS = ("time.nist.gov", "time.apple.com")
NTP_PORT = 123
NTP_TIMEOUT = 3.0
# Two HTTP time APIs must agree within this window before we trust either.
# (2026-07-15: timeapi.io's own server clock drifted ~5 min while the local
# clock was NTP-accurate — a single HTTP source can NOT be authoritative.)
HTTP_AGREEMENT_TOL_SEC = 2.0

# Shared state (thread-safe via _lock).
_lock = threading.Lock()
_offset_sec: float = 0.0
_last_check: Optional[datetime] = None
_last_source: str = "system"
_last_drift: float = 0.0


# ---------- time sources ----------

def _from_timeapi_io() -> Optional[datetime]:
    """Most reliable in our smoke tests."""
    try:
        r = requests.get(
            "https://timeapi.io/api/Time/current/zone?timeZone=America/New_York",
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        # Returns: "2026-05-20T10:15:30.1234567" — naive, NY-local
        iso = data.get("dateTime")
        if not iso:
            return None
        naive = datetime.fromisoformat(iso)
        return NY_TZ.localize(naive)
    except Exception as e:
        log.debug("timeapi.io failed: %s", e)
        return None


def _from_worldtimeapi() -> Optional[datetime]:
    """Backup source — handles DST abbreviation explicitly."""
    try:
        r = requests.get(
            "https://worldtimeapi.org/api/timezone/America/New_York",
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        # "datetime" is tz-aware ISO e.g. "2026-05-20T10:15:30.123456-04:00"
        iso = data.get("datetime")
        if not iso:
            return None
        return datetime.fromisoformat(iso)
    except Exception as e:
        log.debug("worldtimeapi failed: %s", e)
        return None


def _from_ntp(server: str) -> Optional[datetime]:
    """RFC 5905 NTP query — the actual time-sync protocol, preferred source."""
    try:
        import socket
        msg = b"\x1b" + 47 * b"\0"
        addr = (server, NTP_PORT)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(NTP_TIMEOUT)
        try:
            s.sendto(msg, addr)
            data, _ = s.recvfrom(1024)
        finally:
            s.close()
        if len(data) < 48:
            return None
        # Transmit timestamp is at byte 40 (8 bytes: seconds + fraction).
        seconds = struct.unpack("!I", data[40:44])[0] - 2208988800  # NTP epoch → Unix epoch
        fraction = struct.unpack("!I", data[44:48])[0] / 2**32
        return datetime.fromtimestamp(seconds + fraction, tz=timezone.utc).astimezone(NY_TZ)
    except Exception as e:
        log.debug("NTP query to %s failed: %s", server, e)
        return None


def _fetch_authoritative_ny() -> tuple[Optional[datetime], str]:
    """Return (ny_time, source_label), or (None, reason) when no TRUSTWORTHY
    source is available.

    Trust policy (2026-07-15): NTP first — it is the purpose-built protocol and
    the same reference the OS syncs against. The HTTP time APIs are convenience
    fallbacks whose own server clocks can drift (timeapi.io did, by ~5 min), so
    a lone HTTP answer is never authoritative: both must agree within
    HTTP_AGREEMENT_TOL_SEC or we refuse to correct anything.
    """
    for server in NTP_SERVERS:
        t = _from_ntp(server)
        if t is not None:
            return t, f"ntp({server})"

    t1 = _from_timeapi_io()
    t2 = _from_worldtimeapi()
    if t1 is not None and t2 is not None:
        gap = abs((t1 - t2).total_seconds())
        if gap <= HTTP_AGREEMENT_TOL_SEC:
            return t2, "timeapi.io+worldtimeapi.org"
        log.warning(
            "clock: HTTP time sources disagree by %.1fs (timeapi.io vs "
            "worldtimeapi.org) — refusing to correct off either", gap,
        )
        return None, "http sources disagree"
    if (t1 is None) != (t2 is None):
        log.info("clock: only one HTTP time source reachable — "
                 "unverified, not correcting")
        return None, "single unverified http source"
    return None, "no source reachable"


# ---------- public API ----------

def _maybe_refresh() -> None:
    """Re-check drift if CHECK_INTERVAL_SEC has elapsed since the last check."""
    global _offset_sec, _last_check, _last_source, _last_drift
    sys_ny = datetime.now(NY_TZ)
    with _lock:
        if _last_check is not None:
            if (sys_ny - _last_check).total_seconds() < CHECK_INTERVAL_SEC:
                return
        net, source = _fetch_authoritative_ny()
        _last_check = sys_ny
        if net is None:
            _last_source = f"system ({source})"
            log.info("clock: no trustworthy time source (%s) — "
                     "using system + pytz, offset unchanged (%+.1fs)",
                     source, _offset_sec)
            return
        drift = (net - sys_ny).total_seconds()
        _last_drift = drift
        _last_source = source
        if abs(drift) > MAX_DRIFT_SEC:
            _offset_sec = drift
            log.warning(
                "clock drift %+.1fs vs %s — applying correction. "
                "(Re-sync your system clock to fix permanently.)",
                drift, source,
            )
        else:
            _offset_sec = 0.0
            log.info("clock OK: %s shows %+.2fs drift (≤ %.1fs threshold)",
                     source, drift, MAX_DRIFT_SEC)


def ny_now() -> datetime:
    """Return current NY-aware datetime, corrected for any detected drift.

    Side effect: triggers a network re-check at most once per
    CHECK_INTERVAL_SEC.  All other calls are pure-Python and instant."""
    _maybe_refresh()
    return datetime.now(NY_TZ) + timedelta(seconds=_offset_sec)


def utc_now_corrected() -> datetime:
    """Same correction, but returned in UTC. Used by the heartbeat timestamps
    so they're consistent with ny_now()."""
    _maybe_refresh()
    return datetime.now(timezone.utc) + timedelta(seconds=_offset_sec)


def status() -> dict:
    """Snapshot of clock health for the GUI / audit."""
    with _lock:
        return {
            "offset_sec": round(_offset_sec, 2),
            "last_drift_sec": round(_last_drift, 2),
            "last_check": _last_check.isoformat() if _last_check else None,
            "source": _last_source,
            "max_drift_threshold": MAX_DRIFT_SEC,
        }


def force_refresh() -> dict:
    """Force-bypass the cache (used by GUI 'Sync Clock' button)."""
    global _last_check
    with _lock:
        _last_check = None
    _maybe_refresh()
    return status()


# ---------- NYSE market session ----------
# Single source of truth for "is the US market open?" — used by the scheduler
# (drift-corrected via ny_now()) AND the web dashboard (plain system NY time, no
# network). Regular trading hours only: 09:30–16:00 ET, weekdays, non-holidays.
# Half-day early closes are intentionally NOT modelled (matches scan behaviour).

def nyse_holidays(year: int) -> set:
    """Return the set of NYSE full-day holiday dates for `year`."""
    from datetime import date, timedelta

    def observed(d: date) -> date:
        if d.weekday() == 5:   # Saturday → Friday
            return d - timedelta(days=1)
        if d.weekday() == 6:   # Sunday → Monday
            return d + timedelta(days=1)
        return d

    def nth_weekday(y: int, m: int, wd: int, n: int) -> date:
        """nth occurrence (1-based) of weekday wd (0=Mon) in month m."""
        d = date(y, m, 1)
        d += timedelta(days=(wd - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    def last_monday(y: int, m: int) -> date:
        import calendar
        last = date(y, m, calendar.monthrange(y, m)[1])
        return last - timedelta(days=last.weekday())   # weekday 0 = Mon

    def easter(y: int) -> date:
        a = y % 19
        b, c = divmod(y, 100)
        d, e = divmod(b, 4)
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = divmod(c, 4)
        ll = (32 + 2 * e + 2 * i - h - k) % 7
        m2 = (a + 11 * h + 22 * ll) // 451
        mo, dy = divmod(114 + h + ll - 7 * m2, 31)
        return date(y, mo, dy + 1)

    return {
        observed(date(year, 1, 1)),            # New Year's Day
        nth_weekday(year, 1, 0, 3),            # MLK Day (3rd Mon Jan)
        nth_weekday(year, 2, 0, 3),            # Presidents Day (3rd Mon Feb)
        easter(year) - timedelta(days=2),      # Good Friday
        last_monday(year, 5),                  # Memorial Day (last Mon May)
        observed(date(year, 6, 19)),           # Juneteenth
        observed(date(year, 7, 4)),            # Independence Day
        nth_weekday(year, 9, 0, 1),            # Labor Day (1st Mon Sep)
        nth_weekday(year, 11, 3, 4),           # Thanksgiving (4th Thu Nov)
        observed(date(year, 12, 25)),          # Christmas
    }


def market_session(now: Optional[datetime] = None) -> str:
    """Coarse NYSE session label for `now`.

    `now` defaults to plain system NY time (instant, no drift check / network) —
    callers that need drift correction pass `ny_now()` explicitly.

    Returns one of: 'open' | 'premarket' | 'afterhours' | 'weekend' | 'holiday'.
    'open' == inside 09:30–16:00 ET regular trading hours.
    """
    if now is None:
        now = datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return "weekend"
    if now.date() in nyse_holidays(now.year):
        return "holiday"
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 30:
        return "premarket"
    if minutes > 16 * 60:
        return "afterhours"
    return "open"


def market_open(now: Optional[datetime] = None) -> bool:
    """True iff inside NYSE regular trading hours (weekday, non-holiday, 09:30–16:00 ET)."""
    return market_session(now) == "open"
