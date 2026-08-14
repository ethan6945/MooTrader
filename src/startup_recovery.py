"""What happened while we were not running.

A worker that restarts inherits a broker account that kept going without it.
Orders it left working may have filled, been cancelled, or still be sitting
there. Until every one of those is settled, this software does not know what it
holds — and the first thing run_loop does is place protective exits, which is a
decision about a position.

So this runs BEFORE any of that, and its job is narrow: take every order that
never reached a terminal state and ask the broker what became of it.

WHY THIS CANNOT BE INFERRED
  The tempting shortcut is to compare the broker's positions against ours and
  correct the difference. That answers "what do we hold" without answering "what
  did each order do", and the two are not the same question. A position that
  matches by quantity can still be the result of an entry we lost track of and
  an exit we never booked; the PnL, the entry price, the R-multiple and the
  holding period all come from the orders, not from the quantity.

WHAT IT DOES ABOUT WHAT IT CANNOT SETTLE
  It refuses to trade. An order this software cannot account for is a claim on
  the account that may fire at any moment, and continuing means sizing new
  positions against a picture known to be incomplete. That is a halt with a
  reason, not a warning in a log nobody reads at 09:30.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import db, order_log

log = logging.getLogger(__name__)

# How far back to ask the broker. Generous: the cost of a wider window is one
# larger query, and the cost of one too narrow is an order the broker has but
# does not mention, which reads as "it never existed".
LOOKBACK_DAYS = 7

# An order created less than this ago and absent from the broker's list is not
# yet evidence of anything — the order book and the query endpoint do not update
# in the same instant. Older than this, absence is an answer.
SETTLE_GRACE = timedelta(minutes=10)


def _age(row: dict) -> timedelta:
    try:
        created = datetime.fromisoformat(row["created_at"])
    except (KeyError, TypeError, ValueError):
        return timedelta(days=999)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created


def recover(client, *, halt_on_unresolved: bool = True) -> dict:
    """Settle every unfinished order against the broker. Returns a summary.

    Called after the start protocol's GO and before anything trades.
    """
    live_before = order_log.live_orders()
    if not live_before:
        log.info("startup recovery: no unfinished orders")
        return {"checked": 0, "resolved": 0, "never_landed": 0,
                "unresolved": [], "halted": False}

    log.warning("startup recovery: %d order(s) were left unfinished — asking "
                "the broker what became of them", len(live_before))
    summary = order_log.reconcile_live(client, lookback_days=LOOKBACK_DAYS)

    # Whatever the broker did not return is now judged on its age. An order the
    # broker's own list does not contain, covering a window that includes when
    # it was created, did not reach the order book — that is evidence, not a
    # guess, and leaving it live forever would block every future start.
    # An incomplete answer is not an answer. Concluding "never landed" from a
    # list the broker could not fully produce is exactly how a live order gets
    # written off and placed a second time — and a duplicate fill is the worst
    # outcome available here, worse than stopping.
    complete = bool(summary.get("complete", True))
    if not complete:
        from . import risk_manager
        still_live = [o["client_order_id"] for o in order_log.live_orders()]
        risk_manager.halt(
            "broker order query incomplete",
            f"Could not get a complete order list from the broker, so "
            f"{len(still_live)} unfinished order(s) cannot be settled: "
            f"{still_live[:5]}. Nothing has been written off — an order absent "
            f"from an incomplete list may be working right now. Retry when the "
            f"connection is healthy.")
        return {"checked": len(live_before),
                "resolved": summary.get("resolved", 0),
                "never_landed": 0, "unresolved": still_live, "halted": True,
                "complete": False}

    never_landed, unresolved = [], []
    for coid in summary.get("orders", []):
        row = order_log.get(coid)
        if row is None:
            continue
        if _age(row) < SETTLE_GRACE:
            unresolved.append(coid)          # too soon to conclude anything
            continue
        if row["state"] == "PENDING_SUBMIT":
            # Written, then the process died before or during the call. The
            # broker has never heard of it.
            order_log.rejected(coid, "never submitted — the process stopped "
                                     "between recording the order and sending it")
            never_landed.append(coid)
        elif row["state"] == "UNKNOWN":
            order_log.rejected(
                coid, f"absent from the broker's order list for the last "
                      f"{LOOKBACK_DAYS} days; it never reached the order book")
            never_landed.append(coid)
        else:
            # SUBMITTED or PARTIAL and missing from the list. The broker gave us
            # an id for it, so it existed; not finding it now is a gap in what
            # we can see, and that is not something to resolve by deciding.
            unresolved.append(coid)

    result = {"checked": len(live_before), "complete": True,
              "resolved": summary.get("resolved", 0),
              "never_landed": len(never_landed),
              "unresolved": unresolved,
              "halted": False}

    if unresolved and halt_on_unresolved:
        from . import risk_manager
        risk_manager.halt(
            "unresolved orders after restart",
            f"{len(unresolved)} order(s) were accepted by the broker but could "
            f"not be found or settled: {unresolved[:5]}. Trading is stopped "
            f"until they are accounted for — sizing new positions against an "
            f"incomplete picture is how the same shares get bought twice.")
        result["halted"] = True

    log.info("startup recovery: %d checked, %d settled from the broker, "
             "%d never landed, %d unresolved",
             result["checked"], result["resolved"],
             result["never_landed"], len(unresolved))
    return result


def reconcile_positions_from_orders(client) -> dict:
    """Compare what our orders say we hold against what the broker holds.

    Runs after recover(), so every order has been settled. A disagreement here
    is not corrected silently: it means an order moved shares that this software
    has no record of asking for — someone trading the account by hand, or a fill
    from before the order log existed — and quietly adopting it would put a
    position with invented levels into the risk calculation.
    """
    try:
        broker = client.get_positions()
    except Exception as e:
        log.error("startup recovery: cannot read broker positions (%s)", e)
        return {"ok": False, "error": str(e)[:200]}

    from .reconcile import net_positions
    held = net_positions(broker)
    ours = {s: int(t["qty"]) for s, t in db.load_open_trades().items()}

    differences = []
    for sym in sorted(set(held) | set(ours)):
        b, o = float(held.get(sym, 0)), float(ours.get(sym, 0))
        if abs(b - o) > 1e-9:
            differences.append({"symbol": sym, "broker": b, "ours": o})

    if differences:
        log.warning("startup recovery: %d position disagreement(s) after "
                    "settling every order — %s", len(differences), differences)
    return {"ok": True, "differences": differences}
