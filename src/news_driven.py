"""News-driven mode — the switch that makes news the PRIMARY signal.

Every other AI layer in this bot is advisory: it annotates a trade, vetoes a
bad one, or nudges size, but the technical rule score decides which trade
fires. That is what keeps the backtest an honest description of the live
system. This module is the owner-requested (2026-08-07) inversion of that:

    rule score  →  cheap prefilter ("is this tradeable at all")
    news score  →  selection + sizing ("is this worth betting on")
    close       →  flatten (no overnight carry)

DEFAULT OFF (settings.news_driven_enabled). Off, every function here is inert
and the funnel behaves byte-for-byte as before.

THE FAIL-SAFE DIRECTION IS INVERTED HERE, and it is the most important thing
in this file. Advisory sentiment fails to neutral-50 and the trade proceeds —
correct, because the technicals were the thesis. In this mode the news IS the
thesis, so a missing key, an empty news fetch, a quota error or a blown budget
must all mean NO TRADE. Falling back to a technical entry would silently run a
different strategy than the one the switch says is running. gate() therefore
takes an explicit `ok` flag from the caller and refuses on anything else.

WHAT THIS MODE IS NOT. It is not a fast-news edge: news_fetcher queries a
3-day window with a 30-minute cache, so the input is a narrative, not an
event, and any move the headline caused is already in the price. And no factor
study backs the selection claim — contrast the options-volume factor, which
had to clear scripts/options_factor_study.py over 5,980 name-days before it
was allowed to touch sizing. Live results here ARE the experiment.
"""
from __future__ import annotations

import logging

from .config import settings

log = logging.getLogger(__name__)

# The rule score never stops mattering entirely — below this the setup is
# broken tape regardless of how good the story is, and a headline is not a
# reason to buy a name that is falling apart technically.
_ABSOLUTE_FLOOR = 50.0


def enabled() -> bool:
    return bool(settings.news_driven_enabled)


def threshold_floor(base: float) -> float:
    """Relax the rule-score bar when news is doing the selecting.

    Off ⇒ returns `base` unchanged (parity). On ⇒ base + delta (delta is
    negative by default), clamped so we never drop under _ABSOLUTE_FLOOR.
    """
    if not enabled():
        return base
    return max(_ABSOLUTE_FLOOR, base + settings.news_driven_threshold_delta)


def gate(symbol: str, score: int | None, verdict: str | None,
         has_catalyst: bool, ok: bool) -> tuple[bool, str]:
    """Does this candidate's news read justify a bet? (pass, reason)

    `ok` is the caller's assertion that the read is a REAL AI verdict, not a
    fail-safe placeholder. Everything that is not a real read refuses — see
    the module docstring on why the fail-safe points this way.
    """
    if not ok or score is None:
        return False, "news read unavailable — no thesis, no trade (fail-safe)"
    min_score = settings.news_driven_min_score
    if score < min_score:
        return False, (f"news score {score} < {min_score} "
                       f"({verdict or 'no verdict'}) — not a bet")
    if settings.news_driven_require_catalyst and not has_catalyst:
        return False, (f"news score {score} but no concrete catalyst — "
                       "tone alone is not a reason to buy")
    return True, f"news {verdict or 'bullish'} {score} — catalyst confirmed"


def conviction_multiplier(score: int | None) -> float:
    """Map the news score onto position size. min_score → 1.0, 100 → max_mult.

    Linear between the two and clamped at both ends, so a barely-passing story
    gets a baseline position and only a strong read presses. Never returns >
    max_mult and never scales UP a failing score (gate() already rejected it).
    Downstream max_position_pct still caps the result.
    """
    if not enabled() or score is None:
        return 1.0
    min_score = settings.news_driven_min_score
    span = max(1.0, 100.0 - min_score)
    frac = max(0.0, min(1.0, (score - min_score) / span))
    return 1.0 + frac * (settings.news_driven_max_mult - 1.0)


def _flatten_minute() -> int:
    """NEWS_DRIVEN_FLATTEN_ET as minutes-since-midnight ET. Bad input falls
    back to 15:45 rather than raising — a malformed string must not disarm the
    flatten and leave a position overnight it was told to close."""
    raw = (settings.news_driven_flatten_et or "").strip()
    try:
        hh, mm = raw.split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    log.warning("NEWS_DRIVEN_FLATTEN_ET=%r is not HH:MM — using 15:45", raw)
    return 15 * 60 + 45


def flatten_now() -> bool:
    """True when open positions should be flattened for the close.

    Requires the market to actually be open: outside RTH there is no fill to
    be had, and firing this at 20:00 would only spam failed orders.
    """
    if not (enabled() and settings.news_driven_eod_flatten):
        return False
    try:
        from . import clock
        if not clock.market_open():
            return False
        now = clock.ny_now()
    except Exception as e:
        # No trustworthy clock ⇒ don't guess. A wrong flatten sells the book.
        log.warning("news-driven flatten: clock unavailable (%s) — not flattening", e)
        return False
    return (now.hour * 60 + now.minute) >= _flatten_minute()


def entries_closed() -> tuple[bool, str]:
    """Too late in the session to open a NEW news-driven position? (closed, why)

    The flatten is unconditional, so a position opened minutes before it pays a
    full round trip for minutes of exposure. Entries therefore stop
    news_driven_min_hold_min before the flatten time. Inert (False) when the
    mode is off or the flatten is disabled — then the normal bracket owns the
    exit and a late entry is a normal late entry, governed by the existing
    14:00 late-entry premium in main.py.
    """
    if not (enabled() and settings.news_driven_eod_flatten):
        return False, ""
    try:
        from . import clock
        now = clock.ny_now()
    except Exception as e:
        log.warning("news-driven entry cutoff: clock unavailable (%s) — "
                    "allowing entries", e)
        return False, ""
    cutoff = _flatten_minute() - max(0, settings.news_driven_min_hold_min)
    if (now.hour * 60 + now.minute) < cutoff:
        return False, ""
    return True, (f"news-driven: {now:%H:%M} ET is past the "
                  f"{cutoff // 60:02d}:{cutoff % 60:02d} entry cutoff "
                  f"(flatten at {settings.news_driven_flatten_et}) — "
                  "a new position would be closed before it can work")


def describe() -> str:
    """One-line status for logs and the preflight inventory."""
    if not enabled():
        return "news-driven mode OFF (news is advisory; technicals select)"
    bits = [
        f"min_score={settings.news_driven_min_score}",
        f"catalyst={'required' if settings.news_driven_require_catalyst else 'optional'}",
        f"rule_floor{settings.news_driven_threshold_delta:+.0f}",
        f"size≤{settings.news_driven_max_mult:.2f}x",
        f"budget={settings.news_driven_budget}/scan",
    ]
    if settings.news_driven_eod_flatten:
        bits.append(f"flatten@{settings.news_driven_flatten_et} ET")
    else:
        bits.append("no EOD flatten (bracket runs)")
    return "news-driven mode ON — " + ", ".join(bits)
