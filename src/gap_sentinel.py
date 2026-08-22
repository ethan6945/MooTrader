"""AI gap-risk sentinel for HELD positions (2026-06-08).

WHY: a stop-loss cannot protect against an overnight GAP — when price jumps past
the stop, the stop becomes a market order and fills at the gapped price, not the
stop level. Monitoring more often doesn't help (there are no trades between the
stop and the gap; pre/post-market liquidity is thin). The ONLY effective defense
is to exit a known-catalyst name DURING regular hours, BEFORE the gap. Sizing
(MAX_POSITION_PCT) is the other real defense; this sentinel is the catalyst one.

Two layers, evaluated per held symbol each scan:

  Layer 1 — earnings (deterministic, high-value): if the name reports within
    GAP_EXIT_EARNINGS_DAYS, exit. Earnings is the biggest single-name gap source
    and the date is KNOWN, so no AI guess is needed. This is the reliable layer.

  Layer 2 — AI news (probabilistic, secondary, FAIL-SAFE): the model + Tavily judge
    fresh PUBLIC bad news (downgrade / lawsuit / guidance cut / sector shock) and
    sell only on a high-confidence verdict. It CANNOT foresee an earnings surprise
    (that's a coin-flip), and on any error/quota/doubt it HOLDS — a real position
    is never liquidated on AI uncertainty.

The caller (executor.manage_open_trades) does the actual _force_close + notify,
so every sentinel exit is visible (no silent execution).
"""
from __future__ import annotations

import logging
from datetime import date

from .config import settings

log = logging.getLogger(__name__)


def _earnings_gap_risk(symbol: str) -> tuple[bool, str]:
    """Layer 1: exit if earnings is within GAP_EXIT_EARNINGS_DAYS (incl. today)."""
    try:
        from . import earnings
        next_date = earnings.get_next_earnings(symbol)
    except Exception as e:
        log.debug("sentinel earnings lookup failed for %s: %s", symbol, e)
        return False, ""
    if not next_date:
        return False, ""
    try:
        d = date.fromisoformat(next_date)
    except ValueError:
        return False, ""
    days_until = (d - date.today()).days
    if 0 <= days_until <= settings.gap_exit_earnings_days:
        when = "today" if days_until == 0 else f"in {days_until}d"
        return True, f"财报 {when} ({next_date}) — 收盘前清仓避跳空"
    return False, ""


def _ai_gap_risk(symbol: str) -> tuple[bool, str]:
    """Layer 2: AI judges fresh public bad news. FAIL-SAFE → hold on any doubt."""
    if not settings.gap_sentinel_ai:
        return False, ""
    try:
        from . import ai_validator
        should_sell, conf, reason = ai_validator.assess_gap_risk(symbol)
    except Exception as e:
        log.warning("sentinel AI layer error for %s: %s — holding", symbol, e)
        _record_verdict(symbol, None, None, f"error: {e}", acted=False)
        return False, ""
    # Only act on a CONFIDENT sell verdict; everything else holds.
    acted = bool(should_sell and conf >= settings.gap_sentinel_ai_min_conf)
    # Recorded whether it acted or not — see _record_verdict.
    _record_verdict(symbol, should_sell, conf, reason, acted=acted)
    if acted:
        return True, f"AI 跳空预警(信心 {conf}): {reason}"
    return False, ""


def _record_verdict(symbol: str, should_sell, conf, reason: str,
                    *, acted: bool) -> None:
    """Persist what the sentinel thought, acted on or not. Never raises.

    The entry path learned this on 2026-06-11 — it stores ai_score on every buy
    precisely so the layer can be judged later against outcomes. This path,
    which is the only AI in the system that CHANGES anything, kept no record at
    all: a "hold" verdict was discarded and a "sell" was visible only as the
    exit it caused.

    That is not merely thin data, it is biased data. Judging the sentinel from
    the days it sold means judging it on the cases it chose, which says nothing
    about the days it held something that then gapped down. Both halves have to
    be written for the comparison to mean anything.
    """
    try:
        from . import db
        db.audit_insert(
            "gap_sentinel_verdict", symbol=symbol,
            reason=str(reason or "")[:300],
            extra={"should_sell": None if should_sell is None else bool(should_sell),
                   "confidence": conf, "acted": acted,
                   "min_conf": settings.gap_sentinel_ai_min_conf})
    except Exception as e:
        log.debug("could not record the sentinel verdict for %s: %s", symbol, e)


def assess(symbol: str, use_ai: bool = True) -> tuple[bool, str]:
    """Return (should_exit, reason) for a HELD symbol. Layer 1 (earnings — cheap,
    deterministic) always runs; the AI layer runs only when use_ai is True (the
    pre-market job passes True; the per-scan RTH caller passes
    gap_sentinel_ai_intraday, default False — so AI cost is pre-market-only).
    Disabled sentinel → always (False, '')."""
    if not settings.gap_sentinel_enabled:
        return False, ""
    flagged, reason = _earnings_gap_risk(symbol)
    if flagged:
        return True, reason
    if not use_ai:
        return False, ""
    return _ai_gap_risk(symbol)


# ---- pre-market queue (analyzed before the open, executed at the open) ----
# The pre-market job writes flagged names here; the at-open job drains them and
# sells at a fresh real-time price. We sell AT THE OPEN (not pre-market) because
# pre-market liquidity is thin — a stale-price limit would fill badly or not at
# all. The date stamp makes a stale queue from a prior day a no-op.

def _today() -> str:
    from . import clock
    return clock.ny_now().date().isoformat()


def queue_exits(flagged: list[tuple[str, str]]) -> None:
    """Persist the pre-market-flagged (symbol, reason) names for at-open exit."""
    from . import db
    payload = {"date": _today(),
               "symbols": [{"symbol": s, "reason": r} for s, r in flagged]}
    db.atomic_state(lambda _s: {"gap_exit_queue": payload})


def pending_exits() -> list[tuple[str, str]]:
    """(symbol, reason) names queued pre-market TODAY; empty if none/stale."""
    from . import db
    q = db.get_state().get("gap_exit_queue") or {}
    if q.get("date") != _today():
        return []
    return [(d["symbol"], d.get("reason", "")) for d in q.get("symbols", [])]


def clear_queue() -> None:
    """Empty the queue once the at-open exits have been placed."""
    from . import db
    db.atomic_state(lambda _s: {"gap_exit_queue": {"date": _today(), "symbols": []}})
