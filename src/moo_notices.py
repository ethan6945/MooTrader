"""Regulatory filings and analyst actions, relayed through moomoo's own feed.

This replaces src/sec_edgar.py. The owner did not want the bot talking to a US
government service, and there is a working intermediary already connected: the
same OpenD gateway the rest of this program trades through carries SEC filings
under NewsSubType.NOTICE and analyst actions under RATING. Nothing here reaches
sec.gov — OpenD does, on its own account, and this process only talks to
127.0.0.1.

What that costs, stated plainly because it changes what the model can conclude:

  • NO ITEM CODES. EDGAR said "8-K, item 2.02 = results of operations"; this
    feed says "8-K: Current report" and stops. You learn that something material
    was filed, not what it was. A named-catalyst prompt therefore cannot treat a
    notice as the catalyst itself — it is a prompt to look, and the actual event
    has to come from the news sources.
  • DATE ONLY, NO CLOCK TIME, NO YEAR. publish_time is "8/5". EDGAR gave
    "2026-07-30T20:30", which is what tells you a filing landed after the close
    and belongs to the next session. Everything here is day-resolution, the year
    is inferred, and format_block() says so rather than implying precision the
    data does not have.

    Measured against EDGAR on 2026-08-08, AAPL, 120-day window: the SAME three
    8-Ks come back, each dated ONE DAY LATER (EDGAR accepted 07-30 20:30 /
    04-30 20:30 / 04-20 21:29; here 07-31 / 05-01 / 04-21). All three were
    accepted after the close, and this feed dates a filing by when it published
    rather than when it was accepted. Coverage is therefore equivalent and the
    timestamps are systematically shifted — which is the honest reading for a
    same-day catalyst, but means a point-in-time replay must NOT treat these
    dates as the filing instant.
  • THINNER. ~30 items and a couple of months per symbol, against EDGAR's full
    history.

What it adds, which EDGAR never had: analyst actions. Upgrades, initiations and
price-target changes are named catalysts by the news-driven prompt's own
definition, and they are not filings at all.

Filtering is on `related_securities`, not on the title. NVDA's own filings come
back tagged ['US.NVDA'] while a leveraged ETF that merely tracks NVDA is tagged
['US.NVDS'] — an exact match drops the wrapper noise, and no amount of string
surgery on "497K: Tradr 1.5X Short NVDA Daily ETF" would have done it safely.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, timedelta

from .config import settings

log = logging.getLogger(__name__)

CACHE_TTL_S = 60 * 30          # matches news_fetcher: filings do not churn
_MAX_COUNT = 100               # the API's own ceiling; it returns ~30 in practice

# 8-K (domestic) and 6-K (foreign issuer) are the "something happened" forms.
# 10-K/10-Q are excluded on purpose — a scheduled periodic report is not a
# surprise, and treating it as one manufactures a catalyst every quarter.
# Forms 3/4/5 and 13D/G are ownership disclosures, not events at the issuer.
MATERIAL_FORMS = ("8-K", "6-K")

_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()
_last_call = 0.0
_MIN_GAP_S = 0.35              # be a polite client of a gateway we also trade on


def enabled() -> bool:
    return bool(getattr(settings, "moo_notices_enabled", False))


def _base_symbol(symbol: str) -> str:
    """'US.AAPL' / 'AAPL.US' / 'AAPL' -> 'AAPL'."""
    s = (symbol or "").strip().upper()
    for sep in (".",):
        if sep in s:
            parts = [p for p in s.split(sep) if p not in ("US", "HK", "CN")]
            if parts:
                s = parts[0]
    return s


def _parse_when(raw: str) -> date | None:
    """publish_time is "8/5" — month/day, no year, no clock.

    The year is inferred as the current one, rolled back when that would put the
    item in the future. Two days of slack absorbs a timezone disagreement
    between this machine and the feed; without it, anything published "today" in
    a market ahead of us would be read as eleven months old and silently
    dropped by the window filter.
    """
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return date(*time.strptime(s[:10], fmt)[:3])
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", s)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    today = date.today()
    try:
        guess = date(today.year, month, day)
    except ValueError:
        return None
    if guess > today + timedelta(days=2):
        try:
            guess = date(today.year - 1, month, day)
        except ValueError:
            return None
    return guess


def _tail(title: str) -> str:
    """The segment after the last '|', which is where the form code always is.

    The feed uses at least two title shapes and reading the FRONT of the string
    gets one of them wrong:

        "8-K: NVIDIA | 8-K: Current report"   -> front is "8-K"          ✓
        "NVIDIA | 8-K: ..."                   -> front is "NVIDIA"       ✗
        "NVIDIA | SCHEDULE 13G/A"             -> front is "NVIDIA"       ✗

    Parsing the front silently dropped every filing in the second shape, which
    reads downstream as "this source is thin" rather than as a bug — NVDA showed
    3 filings in 120 days when the feed had more.
    """
    parts = [p.strip() for p in str(title or "").split("|")]
    return parts[-1] if parts else ""


def _form_of(title: str) -> str:
    """'… | 8-K: Current report' -> '8-K'; '… | SCHEDULE 13G/A' -> that."""
    tail = _tail(title)
    return (tail.split(":", 1)[0] if ":" in tail else tail).strip().upper()


def _describe(title: str) -> str:
    """The form's plain-language name, when the feed bothers to give one."""
    tail = _tail(title)
    return (tail.split(":", 1)[1].strip() if ":" in tail else "") or "unspecified"


def _fetch(symbol: str, sub_type: str) -> list[dict]:
    """One search against OpenD, cached. Returns [] on any failure — this is an
    additive source and must never be able to break a news read."""
    global _last_call
    key = f"{symbol}|{sub_type}"
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    try:
        from moomoo import RET_OK, NewsSubType

        from .moo_client import client
        with _lock:
            gap = _MIN_GAP_S - (time.time() - _last_call)
            if gap > 0:
                time.sleep(gap)
            _last_call = time.time()
        with client() as c:
            ret, data = c.quote.get_search_news(
                _base_symbol(symbol), max_count=_MAX_COUNT,
                news_sub_type=getattr(NewsSubType, sub_type))
        if ret != RET_OK:
            log.warning("moomoo %s search for %s failed: %s", sub_type, symbol, data)
            return []
        want = f"US.{_base_symbol(symbol)}"
        out: list[dict] = []
        for _, r in data.iterrows():
            rel = r.get("related_securities") or []
            # Exact match on the tagged security. A leveraged ETF tracking this
            # ticker carries its OWN code here, which is the whole point.
            if want not in list(rel):
                continue
            out.append({
                "title": str(r.get("title") or ""),
                "form": _form_of(r.get("title")),
                "what": _describe(r.get("title")),
                "source": str(r.get("source") or ""),
                "when": _parse_when(r.get("publish_time")),
                "url": str(r.get("url") or ""),
            })
        with _lock:
            _cache[key] = (time.time(), out)
        return out
    except Exception as e:                                      # noqa: BLE001
        log.warning("moomoo %s lookup failed for %s: %s", sub_type, symbol, e)
        return []


def _within(items: list[dict], days: int) -> list[dict]:
    """Day-resolution window. The boundary is checked here rather than trusted
    to the feed: a point-in-time replay that lets one future item through is a
    look-ahead bug, and those do not announce themselves."""
    cutoff = date.today() - timedelta(days=max(0, days))
    today = date.today()
    return [i for i in items
            if i["when"] is not None and cutoff <= i["when"] <= today]


def fetch_filings(symbol: str, days: int | None = None) -> list[dict]:
    """Material regulatory filings for this issuer, newest first."""
    if not enabled():
        return []
    d = int(getattr(settings, "moo_notices_days", 3) if days is None else days)
    items = [i for i in _fetch(symbol, "NOTICE") if i["form"] in MATERIAL_FORMS]
    return _within(items, d)


def fetch_ratings(symbol: str, days: int | None = None) -> list[dict]:
    """Analyst actions — upgrades, initiations, price-target changes."""
    if not enabled():
        return []
    d = int(getattr(settings, "moo_notices_days", 3) if days is None else days)
    return _within(_fetch(symbol, "RATING"), d)


def format_block(filings: list[dict], ratings: list[dict]) -> str:
    """Compact prompt block.

    Says '(none)' rather than going silent — an explicit "nothing filed" is
    information, while an absent section lets the model assume the lookup was
    simply unavailable and reason as if it might have been anything.

    The dates carry no clock time and the notice lines carry no item code, and
    both facts are stated in the block. A model told "8-K on 08-05" will happily
    guess what the 8-K said; one told that the subject is not available asks the
    news instead, which is where the answer actually is.
    """
    lines: list[str] = []
    if filings:
        lines.append("Regulatory filings (date only, no clock time; the filing's "
                     "subject is NOT available from this source):")
        for f in filings:
            when = f["when"].strftime("%m-%d") if f["when"] else "?"
            lines.append(f"- [{when}] {f['form']}: {f['what']}")
    else:
        lines.append("Regulatory filings: (none in the window)")
    if ratings:
        lines.append("Analyst actions:")
        for r in ratings:
            when = r["when"].strftime("%m-%d") if r["when"] else "?"
            lines.append(f"- [{when}] {r['title'][:140]}")
    else:
        lines.append("Analyst actions: (none in the window)")
    return "\n".join(lines)


def probe() -> tuple[bool, str]:
    """Preflight helper: is this source actually usable? (ok, detail)."""
    if not enabled():
        return True, "off"
    items = _fetch("AAPL", "NOTICE")
    if not items:
        return False, ("no notices for AAPL — OpenD unreachable, or this "
                       "account's feed does not carry them")
    forms = sorted({i["form"] for i in items})
    return True, f"ok ({len(items)} AAPL notices; forms {', '.join(forms[:6])})"
