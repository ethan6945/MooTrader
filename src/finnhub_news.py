"""Finnhub company news — ticker-tagged, and answerable for a PAST date range.

WHY THIS AND NOT JUST TAVILY. Tavily answers "what is being said now". It has
no point-in-time mode, so there is no way to ask it what was visible on
2026-03-04, which is the single reason news-driven mode cannot be backtested:
you cannot replay a decision whose input you cannot reconstruct. Finnhub's
company-news endpoint takes `from` and `to` dates and returns what was
published in that window — the missing half of a backtestable news strategy
(the other half being a scorer with no look-ahead, i.e. src/news_score_local.py).

It is also structurally better for the live path: results are TAGGED to a
ticker by the provider rather than matched by a search query, so "AAPL" stops
pulling in stories about apples, and every item carries a real publish
timestamp instead of one inferred from prose.

WHAT IT DOES NOT DO. Finnhub's free tier caps historical range at roughly a
year and rate-limits to 60 calls/minute. Coverage of small caps is thinner than
of large caps, and the feed includes plenty of low-value aggregator content —
so the source whitelist that news_fetcher applies to Tavily matters here too.

FAIL-SAFE: every failure returns an empty list. In news-driven mode the gate
already refuses to trade without a catalyst, so "could not fetch" and "nothing
published" collapse to the same, safe conclusion.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

import requests

from .config import settings

log = logging.getLogger(__name__)

_BASE = "https://finnhub.io/api/v1/company-news"
_CACHE_TTL_S = 60 * 15
_cache: dict[str, tuple[float, list[dict]]] = {}

# Free tier is 60/min. We are nowhere near it (a handful per scan), but a burst
# across many candidates could brush it, and a 429 costs the whole scan's reads.
_MIN_INTERVAL_S = 0.2
_last_call = 0.0


def enabled() -> bool:
    return bool(getattr(settings, "finnhub_enabled", False) and _key())


def _key() -> str:
    return (getattr(settings, "finnhub_key", "") or "").strip()


def _base_symbol(symbol: str) -> str:
    """'US.AAPL' / 'AAPL.US' → 'AAPL' — the bot passes broker-qualified symbols
    around and Finnhub wants the bare ticker."""
    s = (symbol or "").upper().strip()
    if "." in s:
        parts = [p for p in s.split(".") if p]
        for p in parts:
            if p not in ("US", "HK", "CN", "SH", "SZ"):
                return p
        return parts[-1] if parts else s
    return s


def fetch_company_news(symbol: str, days: int | None = None,
                       *, until: date | None = None) -> list[dict]:
    """News for `symbol` in a window, newest first, in news_fetcher's shape.

    `until` makes this point-in-time: pass a past date and you get what was
    published on or before it, with nothing from after leaking in. That is the
    whole reason this module exists — a live call just leaves it at today.
    Returns [] on any failure.
    """
    if not enabled():
        return []
    days = days or int(getattr(settings, "finnhub_days", 3))
    end = until or datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(1, days))
    base = _base_symbol(symbol)
    key = f"{base}|{start}|{end}"
    now = time.time()
    if key in _cache:
        ts, cached = _cache[key]
        if now - ts < _CACHE_TTL_S:
            return cached

    global _last_call
    gap = time.time() - _last_call
    if gap < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - gap)
    _last_call = time.time()

    try:
        r = requests.get(_BASE, params={"symbol": base, "from": start.isoformat(),
                                        "to": end.isoformat(), "token": _key()},
                         timeout=15)
        if r.status_code == 429:
            log.warning("Finnhub 429 (rate limited) for %s — skipping this read", base)
            return []
        if r.status_code in (401, 403):
            log.warning("Finnhub %s for %s — check FINNHUB_API_KEY", r.status_code, base)
            return []
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        log.warning("Finnhub fetch failed for %s: %s", base, e)
        return []

    if not isinstance(raw, list):
        log.warning("Finnhub returned %s for %s, expected a list", type(raw).__name__, base)
        return []

    cutoff = int(datetime.combine(start, datetime.min.time(),
                                  tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.combine(end + timedelta(days=1), datetime.min.time(),
                                  tzinfo=timezone.utc).timestamp())
    allow = _allowed_domains()
    items: list[dict] = []
    for it in raw:
        try:
            ts = int(it.get("datetime") or 0)
        except Exception:
            continue
        # Finnhub occasionally returns items outside the requested window; for a
        # point-in-time replay a single leaked future item is a look-ahead bug,
        # so the bound is enforced here rather than trusted.
        if ts < cutoff or ts >= end_ts:
            continue
        src = str(it.get("source") or "").lower()
        if allow and not any(d in src for d in allow):
            continue
        items.append({
            "title": str(it.get("headline") or "")[:100],
            "content": str(it.get("summary") or "")[:120],
            "published": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M"),
            "source": it.get("source"),
            "_ts": ts,
        })
    items.sort(key=lambda x: x["_ts"], reverse=True)
    limit = int(getattr(settings, "finnhub_max_results", 5))
    items = items[:limit]
    for it in items:
        it.pop("_ts", None)
    _cache[key] = (now, items)
    return items


def _allowed_domains() -> list[str]:
    """Reuses NEWS_INCLUDE_DOMAINS. Finnhub reports a `source` name rather than
    a URL, so this is a substring match and is deliberately loose — the point is
    to drop obvious aggregator noise, not to be a precise URL filter."""
    try:
        from . import news_fetcher
        return [d.split(".")[0] for d in news_fetcher._domains()]
    except Exception:
        return []


def probe() -> tuple[bool, str]:
    """Preflight helper. (ok, detail) — makes one cheap real call."""
    if not getattr(settings, "finnhub_enabled", False):
        return True, "off"
    if not _key():
        return False, "FINNHUB_API_KEY not set"
    try:
        r = requests.get(_BASE, params={"symbol": "AAPL",
                                        "from": (date.today() - timedelta(days=5)).isoformat(),
                                        "to": date.today().isoformat(),
                                        "token": _key()}, timeout=15)
        if r.status_code in (401, 403):
            return False, f"key rejected ({r.status_code})"
        if r.status_code == 429:
            return False, "rate limited (429) — try again shortly"
        r.raise_for_status()
        n = len(r.json() or [])
        return True, f"ok ({n} AAPL items in the last 5 days)"
    except Exception as e:
        return False, f"probe failed: {str(e)[:70]}"
