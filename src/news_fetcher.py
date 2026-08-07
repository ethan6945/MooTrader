"""Tavily news search for the AI validator.

Two queries:
  • per-ticker news: "AAPL stock earnings news"
  • macro news: shared across the scan (Fed / CPI / tariff)

We cache results in-process for `CACHE_TTL_S` seconds so a single scan does
not hammer the API. Tavily free tier = 1000 req/month, which is plenty if we
respect the cache (one macro call per scan + one per top-candidate).
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .config import settings

log = logging.getLogger(__name__)

CACHE_TTL_S = 60 * 30  # 30 min — news doesn't change every minute
_cache: dict[str, tuple[float, list[dict]]] = {}


# Reputable financial press + the primary source. Tavily's news topic otherwise
# happily returns SEO listicles ("3 AI stocks to watch"), aggregator rewrites and
# price-move recaps, which read as catalysts to a model and are not. sec.gov is
# in the list deliberately: an 8-K IS the material event, timestamped, before
# the press writes it up.
#
# NOT the default — NEWS_INCLUDE_DOMAINS is empty out of the box so behaviour is
# unchanged for existing installs. Set it to `recommended` to get this list
# without hand-copying it, or to your own comma-separated list.
RECOMMENDED_DOMAINS = (
    "reuters.com,bloomberg.com,wsj.com,ft.com,cnbc.com,barrons.com,"
    "marketwatch.com,businesswire.com,prnewswire.com,globenewswire.com,sec.gov"
)


def _domains() -> list[str]:
    raw = (getattr(settings, "news_include_domains", "") or "").strip()
    if raw.lower() == "recommended":
        raw = RECOMMENDED_DOMAINS
    return [d.strip() for d in raw.split(",") if d.strip()]


def _tavily(query: str, days: int = 3, max_results: int = 5) -> list[dict]:
    if not settings.tavily_key:
        return []
    domains = _domains()
    depth = getattr(settings, "news_search_depth", "basic")
    cache_key = f"{query}|{days}|{max_results}|{depth}|{','.join(domains)}"
    now = time.time()
    if cache_key in _cache:
        ts, cached = _cache[cache_key]
        if now - ts < CACHE_TTL_S:
            return cached
    payload = {
        "api_key": settings.tavily_key,
        "query": query,
        "search_depth": depth,
        "max_results": max_results,
        "days": days,
        "topic": "news",
    }
    if domains:
        payload["include_domains"] = domains
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("Tavily %s -> %s %s", query, resp.status_code, resp.text[:120])
            return []
        # published_date is kept (2026-08-07). It used to be dropped, which
        # meant every consumer saw a 5-minute-old headline and a 3-day-old one
        # as identical text — fatal for news-driven mode, whose whole job is
        # asking "is this catalyst fresh or already priced in?". The model
        # cannot answer that from a title alone. `score` is Tavily's own
        # relevance rank, kept for the same reason: it lets a caller tell a
        # direct hit from a loose keyword match.
        items = [
            {"title": i.get("title", "")[:100],
             "content": i.get("content", "")[:120],
             "published": (i.get("published_date") or "")[:16],
             "score": i.get("score")}
            for i in resp.json().get("results", [])
        ]
        _cache[cache_key] = (now, items)
        return items
    except Exception as e:
        log.warning("Tavily error for %r: %s", query, e)
        return []


def fetch_macro_news() -> list[dict]:
    """Shared across all tickers in a scan."""
    return _tavily(
        "US Federal Reserve interest rate CPI stock market tariff earnings season",
        days=3,
        max_results=3,
    )


def fetch_ticker_news(symbol: str) -> list[dict]:
    """Per-ticker news. Window is NEWS_TICKER_DAYS (default 3 — unchanged).

    Narrow it for a same-session strategy: news_driven mode flattens at the
    close, so a catalyst from two sessions ago has already had two sessions to
    be priced. Widening it does not make the read better, only staler.
    """
    days = max(1, getattr(settings, "news_ticker_days", 3))
    return _tavily(f"{symbol} stock news earnings guidance", days=days, max_results=3)


def format_news(items: list[dict], header: str) -> str:
    """Compact format — date + title + 1-line summary, capped per item.

    The leading [date] is what lets a reader (human or model) separate "this
    broke an hour ago" from "this is the third rewrite of Monday's story".
    Omitted when Tavily didn't supply one rather than guessed at.
    """
    if not items:
        return f"{header}: (none)"
    lines = [f"{header}:"]
    for it in items:
        title = it.get("title", "").strip()
        body = it.get("content", "").strip()
        when = (it.get("published") or "").strip()
        prefix = f"[{when}] " if when else ""
        if body:
            lines.append(f"- {prefix}{title} | {body}")
        else:
            lines.append(f"- {prefix}{title}")
    return "\n".join(lines)
