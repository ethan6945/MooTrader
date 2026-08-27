"""Point-in-time news sentiment for the signal desk's forecast context.

Ported from the Stock Probability Prediction Platform's `companion/news_engine.py`
and `companion/news_service.py` — the sentiment half of them. What came across is
the scoring pipeline: the catalyst rule table, the source-quality tiers, the
recency-decay weighting, and the aggregation that turns a pile of headlines into
one bounded number for `signal_context.news_score`.

WHAT DID NOT COME ACROSS, AND WHY. The platform also shipped a second ridge
model inside `news_engine.py` that predicted price direction from news features
with its own walk-forward. It is not here. This project already has three news
paths (`news_fetcher`, `news_score_local`, `finnhub_news`) and the forecast model
being merged alongside it does not beat a constant prediction on this data; a
second unvalidated predictor stacked on the first would add confidence, not
accuracy. Ask for it and it is a contained follow-up — everything it needs
(`news_items` with `available_at`, settled outcomes) is already stored.

INFERENCE IS THIS PROJECT'S OWN. The platform ran FinBERT as a one-shot ONNX
subprocess against `FINBERT_MODEL_DIR`. This project already owns a FinBERT
runtime with a downloaded model and a `finbert_enabled` switch, so the pipeline
calls `news_score_local.score_texts_detailed` instead of spawning anything. When
FinBERT is off or its model is missing, every item scores None and the news
channel reports "no evidence" — it never falls back to keyword sentiment
wearing FinBERT's name, which is the one behaviour the platform was emphatic
about.

POINT-IN-TIME IS NOT DECORATION. Every item carries `available_at`, and the
aggregate only ever reads items available at or before the cutoff. That is what
lets `signal_store.settle_forecasts` later ask whether news that arrived AFTER a
forecast explains its error, instead of quietly scoring the forecast with
information it never had.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from .config import settings
from . import news_score_local

log = logging.getLogger(__name__)

# Half-life-ish decay applied to an item's weight by age at the cutoff.
DECAY_HOURS = 30.0
# The window the platform searched, kept exactly: three days of headlines.
WINDOW_HOURS = 72
MAX_ITEMS = 30

CATALYST_RULES: tuple[tuple[str, int, str, tuple[str, ...]], ...] = (
    ("merger_or_acquisition", 92, "mixed", ("acquire", "acquisition", "merger", "takeover", "buyout")),
    ("regulatory_or_fda", 88, "mixed", ("fda", "approval", "approved", "clinical trial", "regulator", "antitrust")),
    ("earnings", 80, "mixed", ("earnings", "quarterly results", "eps", "revenue", "profit", "loss")),
    ("guidance", 84, "mixed", ("guidance", "outlook", "forecast", "raises forecast", "cuts forecast")),
    ("legal_or_investigation", 74, "negative", ("lawsuit", "investigation", "subpoena", "fraud", "settlement", "probe")),
    ("capital_action", 68, "mixed", ("buyback", "dividend", "offering", "secondary offering", "debt issuance", "split")),
    ("management_change", 58, "mixed", ("chief executive", "ceo", "cfo", "resigns", "appointed", "succession")),
    ("analyst_action", 42, "mixed", ("upgrade", "downgrade", "price target", "initiates coverage")),
    ("macro_or_policy", 48, "mixed", ("federal reserve", "interest rate", "inflation", "tariff", "sanction", "jobs report")),
)
POSITIVE_CATALYST_CUES = (
    "beats", "beat estimates", "raises guidance", "record revenue", "approved",
    "approval", "upgrade", "buyback", "wins contract", "special dividend",
)
NEGATIVE_CATALYST_CUES = (
    "misses", "missed estimates", "cuts guidance", "downgrade", "recall",
    "lawsuit", "investigation", "offering", "bankruptcy", "fraud",
)

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_)", re.I)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_url(url: str) -> str:
    """Strip tracking parameters and normalize, so one story dedupes to one row."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw[:500]
    query = "&".join(
        piece for piece in parts.query.split("&")
        if piece and not _TRACKING.match(piece.split("=", 1)[0])
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))[:500]


def _domain(url: str) -> Optional[str]:
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return None
    return host[4:] if host.startswith("www.") else (host or None)


def catalyst_diagnostic(title: str, summary: str) -> dict[str, Any]:
    """How much of an event is this headline, and which way does it cut?

    Deterministic keyword rules. This measures event MATERIALITY, not sentiment
    — FinBERT supplies the tone separately, and the two disagreeing on the same
    item is informative rather than a bug.
    """
    text = f"{title} {summary}".lower()
    matches = [(label, weight, direction)
               for label, weight, direction, phrases in CATALYST_RULES
               if any(phrase in text for phrase in phrases)]
    labels = [match[0] for match in matches]
    score = (min(100, max(match[1] for match in matches) + max(0, len(matches) - 1) * 3)
             if matches else 15)
    positive = sum(1 for cue in POSITIVE_CATALYST_CUES if cue in text)
    negative = sum(1 for cue in NEGATIVE_CATALYST_CUES if cue in text)
    if positive > negative:
        direction = "positive"
    elif negative > positive:
        direction = "negative"
    elif positive and negative:
        direction = "mixed"
    elif matches:
        declared = {match[2] for match in matches}
        direction = next(iter(declared)) if len(declared) == 1 else "mixed"
    else:
        direction = "neutral"
    return {
        "score": int(score), "direction": direction, "labels": labels,
        "provenance": {"method": "deterministic_keyword_catalyst_rules", "version": "1.0",
                       "finbert_used": False,
                       "meaning": "event materiality heuristic, not model sentiment"},
    }


def source_quality(source: str, domain: str) -> dict[str, Any]:
    """Tier a publisher. A regulator filing and a Reddit post are not peers."""
    key = f"{source} {domain}".lower()
    if any(token in key for token in ("sec.gov", "investor relations", "press release", "fda.gov", "federal reserve")):
        score, tier, rule = 95, "primary", "primary_or_regulator"
    elif any(token in key for token in ("reuters", "associated press", "apnews", "bloomberg", "dow jones")):
        score, tier, rule = 88, "high", "major_wire"
    elif any(token in key for token in ("wall street journal", "wsj", "financial times", "ft.com", "cnbc", "marketwatch", "barron")):
        score, tier, rule = 78, "high", "established_financial_press"
    elif any(token in key for token in ("seeking alpha", "motley fool", "benzinga", "investing.com", "yahoo finance")):
        score, tier, rule = 62, "medium", "financial_aggregator_or_commentary"
    elif any(token in key for token in ("reddit", "twitter", "x.com", "substack", "blog")):
        score, tier, rule = 28, "low", "social_or_self_published"
    else:
        score, tier, rule = 50, "unrated", "unrated_source"
    return {
        "score": score, "tier": tier,
        "provenance": {"method": "deterministic_source_tiers", "version": "1.0",
                       "matched_rule": rule, "finbert_used": False,
                       "meaning": "source class heuristic, not factual verification"},
    }


def _parse_published(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw[:len(pattern) + 2], pattern)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ─── providers ────────────────────────────────────────────────────────────────
def _tavily(symbol: str, company: str, hours: int) -> list[dict[str, Any]]:
    """Tavily search keeping the fields the pipeline needs.

    `news_fetcher._tavily` exists but truncates content to 120 characters and
    drops the URL, and this pipeline needs the URL for canonical dedup and the
    domain for source tiering. Same key, same endpoint, wider projection.
    """
    if not settings.tavily_key:
        return []
    import requests
    query = f"{symbol} {company} stock news".strip()
    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": settings.tavily_key, "query": query, "topic": "news",
                  "search_depth": getattr(settings, "news_search_depth", "basic"),
                  "max_results": 10, "days": max(1, math.ceil(hours / 24))},
            timeout=15,
        )
        if response.status_code != 200:
            log.warning("signal_news: tavily %s -> %s", symbol, response.status_code)
            return []
        results = response.json().get("results", [])
    except Exception as exc:
        log.warning("signal_news: tavily failed for %s: %s", symbol, exc)
        return []
    items = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "")
        items.append({
            "title": str(entry.get("title") or "")[:300],
            "summary": str(entry.get("content") or "")[:600],
            "url": url,
            "source": _domain(url) or "tavily",
            "published": entry.get("published_date"),
            "provider": "tavily",
        })
    return items


def _finnhub(symbol: str, hours: int) -> list[dict[str, Any]]:
    try:
        from . import finnhub_news
        if not finnhub_news.enabled():
            return []
        raw = finnhub_news.fetch_company_news(symbol, days=max(1, math.ceil(hours / 24)))
    except Exception as exc:
        log.warning("signal_news: finnhub failed for %s: %s", symbol, exc)
        return []
    return [{
        "title": str(item.get("title") or "")[:300],
        "summary": str(item.get("content") or "")[:600],
        "url": str(item.get("url") or ""),
        "source": str(item.get("source") or "finnhub"),
        "published": item.get("published"),
        "provider": "finnhub",
    } for item in raw]


def fetch(symbol: str, company: str = "", hours: int = WINDOW_HOURS) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect headlines from every configured provider, deduped and windowed."""
    now = _utc_now()
    floor = now - timedelta(hours=hours)
    collected: list[dict[str, Any]] = []
    providers: list[str] = []
    for name, items in (("tavily", _tavily(symbol, company, hours)),
                        ("finnhub", _finnhub(symbol, hours))):
        if items:
            providers.append(name)
            collected.extend(items)

    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for item in collected:
        published = _parse_published(item.get("published"))
        if published is None or published < floor or published > now + timedelta(minutes=5):
            continue
        url = canonical_url(item.get("url") or "")
        title = (item.get("title") or "").strip()
        if not title:
            continue
        identity = url or hashlib.sha256(title.lower().encode()).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        item["url"] = url
        item["published_at"] = published.isoformat()
        # Absent a provider-supplied first-seen time, an item is treated as
        # having been available when it was published. Conservative in the
        # direction that matters: it never makes a forecast look like it knew
        # something later than it did.
        item["available_at"] = published.isoformat()
        item["id"] = hashlib.sha256(f"{symbol}|{identity}".encode()).hexdigest()[:32]
        output.append(item)
    output.sort(key=lambda entry: entry["published_at"], reverse=True)
    return output[:MAX_ITEMS], providers


# ─── scoring ──────────────────────────────────────────────────────────────────
def score_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Attach FinBERT probabilities, catalyst and source scores to each item."""
    if not items:
        return [], "no items"
    texts = [f"{item.get('title', '')}. {item.get('summary', '')}".strip() for item in items]
    finbert_rows, detail = news_score_local.score_texts_detailed(texts)
    scored: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        catalyst = catalyst_diagnostic(item.get("title", ""), item.get("summary", ""))
        quality = source_quality(str(item.get("source") or ""), _domain(item.get("url") or "") or "")
        finbert = finbert_rows[index] if finbert_rows else None
        if finbert is not None:
            sentiment = finbert["positive_percent"] - finbert["negative_percent"]
            # Blend tone with how credible the publisher is; a strong opinion
            # from an unrated blog should not read like a wire story.
            overall = round(max(0.0, min(100.0, 50.0 + sentiment * 0.35
                                         + (quality["score"] - 50.0) * 0.30)), 2)
        else:
            overall = None
        scored.append({
            **item,
            "finbert": finbert,
            "catalyst": catalyst,
            "source_quality": quality,
            "scores": {"overall": overall, "catalyst": catalyst["score"],
                       "source_quality": quality["score"]},
            "selected": True,
        })
    return scored, detail


def aggregate(items: list[dict[str, Any]], cutoff_at: Optional[datetime] = None) -> dict[str, Any]:
    """Recency-weighted sentiment over the items available at the cutoff."""
    cutoff = cutoff_at or _utc_now()
    eligible = []
    for item in items:
        if item.get("selected") is False or not isinstance(item.get("finbert"), dict):
            continue
        available = _parse_published(item.get("available_at"))
        if available is None or available > cutoff:
            continue
        eligible.append((item, available))
    if not eligible:
        return {"visible_items": len(items), "pit_eligible_items": 0,
                "positive_percent": None, "neutral_percent": None,
                "negative_percent": None, "sentiment_score": None,
                "catalyst_score": None, "source_quality_score": None,
                "finbert_used": False}
    weights = [math.exp(-max(0.0, (cutoff - available).total_seconds() / 3600.0) / DECAY_HOURS)
               for _item, available in eligible]
    total = sum(weights) or 1.0

    def _weighted(pick) -> float:
        return sum(pick(item) * weight for (item, _a), weight in zip(eligible, weights)) / total

    positive = _weighted(lambda item: item["finbert"]["positive_percent"])
    neutral = _weighted(lambda item: item["finbert"]["neutral_percent"])
    negative = _weighted(lambda item: item["finbert"]["negative_percent"])
    return {
        "visible_items": len(items),
        "pit_eligible_items": len(eligible),
        "positive_percent": round(positive, 4),
        "neutral_percent": round(neutral, 4),
        "negative_percent": round(negative, 4),
        "sentiment_score": round(positive - negative, 4),
        "catalyst_score": round(_weighted(lambda item: float(item["catalyst"]["score"])), 4),
        "source_quality_score": round(_weighted(lambda item: float(item["source_quality"]["score"])), 4),
        "finbert_used": True,
    }


def analyze(symbol: str, company: str = "", hours: int = WINDOW_HOURS,
            cutoff_at: Optional[datetime] = None) -> dict[str, Any]:
    """Fetch, score and aggregate one symbol's news window in one call."""
    items, providers = fetch(symbol, company, hours)
    scored, detail = score_items(items)
    summary = aggregate(scored, cutoff_at)
    finbert_ok, finbert_detail = news_score_local.available()
    return {
        "symbol": symbol,
        "generated_at": _utc_now().isoformat(),
        "window_hours": hours,
        "providers": providers,
        "items": scored,
        "aggregate": summary,
        "finbert": {"available": finbert_ok, "detail": finbert_detail or detail},
    }


__all__ = ["aggregate", "analyze", "canonical_url", "catalyst_diagnostic",
           "fetch", "score_items", "source_quality"]
