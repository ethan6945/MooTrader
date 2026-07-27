"""Earnings calendar — block entries within N days of next earnings report.

Uses `yfinance` (handles Yahoo's crumb/cookie flow).  Results cached
per-symbol for 7 days in data/earnings.json so we don't hammer Yahoo on
every scan.  ETFs return None (no earnings) — they always pass.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

CACHE_FILE = settings.root / "data" / "earnings.json"
CACHE_TTL_DAYS = 7
EARNINGS_AVOID_DAYS = 2   # block if earnings <= N days away


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _fetch_yahoo(symbol: str) -> str | None:
    """Return ISO date of next earnings, or None if unavailable (ETFs, missing data)."""
    try:
        import yfinance as yf
        cal = yf.Ticker(symbol).calendar
        if not cal:
            return None
        ed = cal.get("Earnings Date")
        if not ed:
            return None
        # yfinance returns a list[date]; take the soonest future date.
        if isinstance(ed, list):
            future = [d for d in ed if isinstance(d, date) and d >= date.today()]
            if future:
                return min(future).isoformat()
            # All dates are in the past — use most recent as a hint.
            past = [d for d in ed if isinstance(d, date)]
            return max(past).isoformat() if past else None
        if isinstance(ed, date):
            return ed.isoformat()
        return None
    except Exception as e:
        log.warning("yfinance earnings fetch failed for %s: %s", symbol, e)
        return None


def get_next_earnings(symbol: str, force_refresh: bool = False) -> str | None:
    cache = _load_cache()
    key = symbol.upper()
    entry = cache.get(key, {})

    if not force_refresh and entry:
        try:
            age = (date.today() - date.fromisoformat(entry.get("fetched_at", ""))).days
            if age <= CACHE_TTL_DAYS:
                return entry.get("next_earnings")
        except ValueError:
            pass

    next_date = _fetch_yahoo(symbol)
    cache[key] = {
        "next_earnings": next_date,
        "fetched_at": date.today().isoformat(),
    }
    _save_cache(cache)
    return next_date


# ── Historical earnings calendar (for backtest gating) ───────────────────────
# The live gate above only needs the NEXT earnings date. A backtest needs the
# PAST dates that fell inside its window, so it can replay the same "block within
# N days before earnings" rule bar-by-bar. yfinance.get_earnings_dates returns
# both past and near-future reports; we cache them separately (longer TTL is fine
# — past earnings dates never change).
HIST_CACHE_FILE = settings.root / "data" / "earnings_hist.json"
HIST_CACHE_TTL_DAYS = 7


def get_earnings_history(symbol: str, limit: int = 24,
                        force_refresh: bool = False) -> list[str]:
    """Return ISO dates of past + near-future earnings for `symbol`.

    Cached HIST_CACHE_TTL_DAYS in data/earnings_hist.json. Returns [] on any
    failure (ETF / yfinance error) so the caller simply never blocks it — same
    graceful degradation as the live gate.
    """
    cache: dict = {}
    if HIST_CACHE_FILE.exists():
        try:
            cache = json.loads(HIST_CACHE_FILE.read_text())
        except json.JSONDecodeError:
            cache = {}
    key = symbol.upper()
    entry = cache.get(key, {})
    if not force_refresh and entry:
        try:
            age = (date.today() - date.fromisoformat(entry.get("fetched_at", ""))).days
            if age <= HIST_CACHE_TTL_DAYS:
                return entry.get("dates", [])
        except ValueError:
            pass

    dates: list[str] = []
    fetch_ok = False
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).get_earnings_dates(limit=limit)
        if df is not None and len(df):
            dates = sorted({d.date().isoformat() for d in df.index})
        fetch_ok = True
    except Exception as e:
        log.warning("earnings history fetch failed for %s: %s", symbol, e)

    if not fetch_ok:
        # 2026-07-27: NEVER cache a failure as an authoritative empty result.
        # The old code wrote {"dates": [], "fetched_at": today} on any exception,
        # so one transient Yahoo hiccup pinned "this name has no earnings dates"
        # for the full HIST_CACHE_TTL_DAYS=7 — and this feeds the earnings gap
        # gate, i.e. a scrape blip silently disabled gap protection for a week.
        # Found via AMAT/MS failing at 22:32 on 2026-07-27 with a yfinance-
        # internal KeyError('Earnings Date'); both fetch fine on retry.
        # Keep whatever we had and leave fetched_at alone so the next call retries.
        stale = entry.get("dates", [])
        log.info("earnings history for %s not refreshed (fetch failed) — keeping "
                 "%d cached date(s), will retry next call", symbol, len(stale))
        return stale

    cache[key] = {"dates": dates, "fetched_at": date.today().isoformat()}
    HIST_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    HIST_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    return dates


def load_earnings_calendar(symbols: list[str]) -> dict[str, list[str]]:
    """{SYMBOL: [ISO earnings dates]} for backtest gating. Best-effort per name."""
    return {s.upper(): get_earnings_history(s) for s in symbols}


def earnings_block(symbol: str) -> tuple[bool, str]:
    """Return (blocked, reason). Blocked if earnings within EARNINGS_AVOID_DAYS."""
    next_date = get_next_earnings(symbol)
    if not next_date:
        return False, "no earnings (ETF or unavailable)"
    try:
        d = date.fromisoformat(next_date)
    except ValueError:
        return False, f"invalid date: {next_date}"
    days_until = (d - date.today()).days
    if 0 <= days_until <= EARNINGS_AVOID_DAYS:
        return True, f"earnings in {days_until}d ({next_date})"
    if days_until < 0:
        # Stale: earnings has passed; cache will refresh in next TTL window.
        return False, f"earnings was {next_date} (past — refresh pending)"
    return False, f"next earnings {next_date} ({days_until}d away)"
