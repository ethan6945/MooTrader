"""SEC EDGAR filings as a catalyst source (2026-08-07). DEFAULT OFF.

WHY THIS EXISTS. Tavily searches the open web, so "news" arrives as prose of
unknown provenance and unknown age: a wire report, an aggregator's rewrite of
it, a price-move recap, and an SEO listicle all come back looking alike, and a
model reading them cannot reliably tell a catalyst from a retelling. EDGAR is
the opposite in every one of those respects — it is the primary source, the
filing IS the material event, and every item carries an exact filing timestamp
straight from the issuer.

For news_driven mode, whose entire question is "is there a concrete, fresh
catalyst", an 8-K is the highest-signal answer available, and it is free.

WHAT IT IS NOT. Filings cover US domestic issuers (foreign private issuers file
6-K on a different cadence), an 8-K can trail its own press release by minutes
to a day, and plenty of material news — an analyst downgrade, a sector move, a
competitor's blowup — never appears in a filing at all. So this ADDS a source,
it does not replace Tavily.

THE ONE THING THAT WILL GET YOU BLOCKED. SEC requires a User-Agent naming you
with a contact email. A missing or generic one earns a 403 and can get the IP
blocked for ~10 minutes. There is no safe default to invent on your behalf, so
this module refuses to make a request until SEC_EDGAR_USER_AGENT is set — a
silent refusal is much cheaper than an IP block on the machine that also talks
to your broker.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .config import settings

log = logging.getLogger(__name__)

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# SEC publishes 10 req/s. We stay well under it — this bot makes a handful of
# calls per scan, and the ceiling is not a target.
_MIN_INTERVAL_S = 0.15
_last_call = 0.0

_CACHE_TTL_S = 60 * 15
_cache: dict[str, tuple[float, list[dict]]] = {}

# Ticker→CIK map: ~10k entries, changes rarely. Fetched once and kept on disk so
# a restart doesn't re-download it.
_CIK_CACHE = Path(__file__).resolve().parent.parent / "data" / "sec_cik_map.json"
_CIK_TTL_S = 60 * 60 * 24 * 7
_cik_map: dict[str, str] | None = None

# 8-K item codes worth reading. The code says WHAT KIND of event it is, which
# turns "a filing happened" into a typed catalyst — far more useful to a model
# than the raw form name. Codes not listed here (auditor changes, accounting
# restatements, delistings…) still surface; they just have no friendly label.
_ITEM_LABELS = {
    "1.01": "entry into a material agreement",
    "1.02": "termination of a material agreement",
    "1.03": "bankruptcy or receivership",
    "2.01": "completion of acquisition or disposition",
    "2.02": "results of operations (earnings)",
    "2.03": "creation of a material financial obligation",
    "2.05": "costs associated with exit or disposal",
    "2.06": "material impairment",
    "3.01": "delisting / listing-rule non-compliance",
    "4.01": "change in certifying accountant",
    "4.02": "previously issued financials no longer reliable",
    "5.02": "director/officer departure or appointment",
    "5.07": "shareholder vote results",
    "7.01": "Reg FD disclosure",
    "8.01": "other material event",
}

# Forms that carry event news. 10-K/10-Q are periodic and land on a schedule
# everyone already knows, so they are deliberately excluded — a scheduled report
# is not a surprise, and treating it as one is how you buy a priced-in event.
_MATERIAL_FORMS = ("8-K", "6-K")


def enabled() -> bool:
    return bool(getattr(settings, "sec_edgar_enabled", False))


def _user_agent() -> str:
    return (getattr(settings, "sec_edgar_user_agent", "") or "").strip()


def _ua_ok() -> bool:
    """SEC wants "Name contact@example.com". Reject anything without an @ —
    a UA that doesn't identify a contact is the one that gets the IP blocked."""
    ua = _user_agent()
    return bool(ua) and "@" in ua and len(ua) >= 8


def _get(url: str) -> Any | None:
    """One rate-limited GET returning parsed JSON, or None. Never raises."""
    global _last_call
    if not _ua_ok():
        log.warning("SEC EDGAR: SEC_EDGAR_USER_AGENT is unset or has no contact "
                    "email — refusing to call (SEC returns 403 and may block "
                    "this IP for ~10 minutes).")
        return None
    gap = time.time() - _last_call
    if gap < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - gap)
    _last_call = time.time()
    try:
        r = requests.get(url, headers={"User-Agent": _user_agent(),
                                       "Accept-Encoding": "gzip, deflate"},
                         timeout=15)
        if r.status_code == 403:
            log.warning("SEC EDGAR 403 for %s — check SEC_EDGAR_USER_AGENT "
                        "identifies you with a contact email.", url)
            return None
        if r.status_code == 429:
            log.warning("SEC EDGAR 429 (rate limited) for %s — backing off.", url)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("SEC EDGAR fetch failed for %s: %s", url, e)
        return None


def _load_cik_map() -> dict[str, str]:
    """{TICKER: zero-padded 10-digit CIK}. Disk-cached for a week."""
    global _cik_map
    if _cik_map is not None:
        return _cik_map
    try:
        if (_CIK_CACHE.exists()
                and time.time() - _CIK_CACHE.stat().st_mtime < _CIK_TTL_S):
            _cik_map = json.loads(_CIK_CACHE.read_text())
            return _cik_map
    except Exception as e:
        log.debug("SEC CIK cache read failed: %s", e)

    raw = _get(_TICKERS_URL)
    if not raw:
        # Fall back to a stale cache rather than nothing — a week-old ticker↔CIK
        # mapping is still overwhelmingly correct, and losing it means losing
        # the whole source over a transient network blip.
        try:
            if _CIK_CACHE.exists():
                _cik_map = json.loads(_CIK_CACHE.read_text())
                log.info("SEC EDGAR: using stale CIK map (refresh failed)")
                return _cik_map
        except Exception:
            pass
        _cik_map = {}
        return _cik_map

    out: dict[str, str] = {}
    # The file is {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}
    for row in (raw.values() if isinstance(raw, dict) else raw):
        try:
            out[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
        except Exception:
            continue
    _cik_map = out
    try:
        _CIK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CIK_CACHE.write_text(json.dumps(out))
    except Exception as e:
        log.debug("SEC CIK cache write failed: %s", e)
    return out


def _base_symbol(symbol: str) -> str:
    """'US.AAPL' / 'AAPL.US' → 'AAPL'. The bot passes broker-qualified symbols
    around; EDGAR only knows the bare ticker."""
    s = symbol.upper().strip()
    if "." in s:
        parts = [p for p in s.split(".") if p]
        # Prefer the part that looks like a ticker over the market prefix.
        for p in parts:
            if p not in ("US", "HK", "CN", "SH", "SZ"):
                return p
        return parts[-1]
    return s


def _items(raw: str) -> list[str]:
    """'2.02,7.01' or 'Item 2.02: ...' → ['2.02', '7.01']."""
    return re.findall(r"\d\.\d{2}", raw or "")


def fetch_filings(symbol: str, days: int | None = None) -> list[dict]:
    """Material filings for `symbol` in the last `days`, newest first.

    Returns [] on anything unexpected — a missing key, an unknown ticker, a
    network failure. Callers treat an empty list as "no filings", which for a
    catalyst check is the same conclusion as "could not look", and deliberately
    so: the news-driven gate refuses to trade without a catalyst either way.
    """
    if not enabled():
        return []
    days = days or int(getattr(settings, "sec_edgar_days", 3))
    base = _base_symbol(symbol)
    key = f"{base}|{days}"
    now = time.time()
    if key in _cache:
        ts, cached = _cache[key]
        if now - ts < _CACHE_TTL_S:
            return cached

    cik = _load_cik_map().get(base)
    if not cik:
        log.debug("SEC EDGAR: no CIK for %s", base)
        _cache[key] = (now, [])
        return []

    data = _get(_SUBMISSIONS_URL.format(cik=cik))
    if not data:
        return []

    recent = ((data.get("filings") or {}).get("recent") or {})
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    times_ = recent.get("acceptanceDateTime") or []
    items = recent.get("items") or []
    docs = recent.get("primaryDocument") or []
    accs = recent.get("accessionNumber") or []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    out: list[dict] = []
    for i, form in enumerate(forms):
        if form not in _MATERIAL_FORMS:
            continue
        try:
            filed = datetime.strptime(dates[i], "%Y-%m-%d").date()
        except Exception:
            continue
        if filed < cutoff:
            # `recent` is newest-first, so the first too-old entry means every
            # remaining one is older still.
            break
        codes = _items(items[i] if i < len(items) else "")
        acc = (accs[i] if i < len(accs) else "").replace("-", "")
        doc = docs[i] if i < len(docs) else ""
        out.append({
            "form": form,
            "filed": dates[i],
            "accepted": (times_[i] if i < len(times_) else "")[:16],
            "items": codes,
            "labels": [_ITEM_LABELS.get(c, c) for c in codes],
            "url": (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
                    if acc and doc else ""),
        })
    _cache[key] = (now, out)
    return out


def format_filings(items: list[dict], header: str = "SEC filings") -> str:
    """Compact prompt block. Says '(none)' rather than going silent — an
    explicit 'no filings' is information, and a model that sees nothing at all
    is free to assume the section was simply unavailable."""
    if not items:
        return f"{header}: (none in the window)"
    lines = [f"{header}:"]
    for f in items:
        what = ", ".join(f.get("labels") or []) or "unspecified item"
        when = f.get("accepted") or f.get("filed") or ""
        lines.append(f"- [{when}] {f.get('form')}: {what}")
    return "\n".join(lines)


def probe() -> tuple[bool, str]:
    """Preflight helper: is this source actually usable? (ok, detail)."""
    if not enabled():
        return True, "off"
    if not _ua_ok():
        return False, "SEC_EDGAR_USER_AGENT missing or has no contact email"
    cik = _load_cik_map().get("AAPL")
    if not cik:
        return False, "ticker→CIK map unavailable"
    return True, f"ok (AAPL → CIK {cik})"
