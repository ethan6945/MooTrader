"""Every order this software asks for, written down before it is sent.

THE ORDERING IS THE DESIGN

  begin() writes the row and flushes it to disk, and only then may the broker
  be called. Doing it the other way round — call, then record what happened —
  cannot survive the case that matters: the call that neither succeeds nor
  fails, because the connection dropped after the broker accepted the order and
  before the answer came back.

  With the row written first, a crash or a timeout leaves a record that says
  "this may exist at the broker". That is recoverable: ask, using our own id.
  Without it there is silence, and silence is indistinguishable from "nothing
  was sent" — which is how a retry buys twice.

UNKNOWN IS A STATE, NOT AN ERROR

  A failed call proves nothing about the order book. The only honest thing to
  record is that we do not know, and the only way out is to ask the broker.
  Nothing may assume an order did not arrive because sending it raised.

CLAIMING BY NAME

  client_order_id goes out as the order's `remark`, which the broker returns on
  order queries. So an order whose outcome we never saw is claimed by name,
  rather than guessed at from (symbol, side, quantity, roughly when) — a guess
  that is wrong precisely when it matters, because the situation that produces
  an UNKNOWN is also the one that produces a retry sitting next to it.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from . import db

log = logging.getLogger(__name__)

# States from which an order may still change. The recovery sweep asks the
# broker about every order in one of these.
LIVE_STATES = ("PENDING_SUBMIT", "SUBMITTED", "UNKNOWN", "PARTIAL")
TERMINAL_STATES = ("FILLED", "CANCELLED", "EXPIRED", "REJECTED", "FAILED_LOCAL")

# What the broker's order_status maps to. Anything not listed is treated as
# still live and polled again rather than guessed at.
BROKER_STATUS_MAP = {
    "FILLED_ALL": "FILLED",
    "FILLED_PART": "PARTIAL",
    "CANCELLED_ALL": "CANCELLED",
    "CANCELLED_PART": "CANCELLED",
    "SUBMITTED": "SUBMITTED",
    "WAITING_SUBMIT": "SUBMITTED",
    "SUBMITTING": "SUBMITTED",
    "FAILED": "REJECTED",
    "DISABLED": "CANCELLED",
    "DELETED": "CANCELLED",
    "TIMEOUT": "UNKNOWN",
    "SUBMIT_FAILED": "REJECTED",
}


class OrderLogError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_client_order_id() -> str:
    """Short, unique, and safe to send as a broker remark.

    Kept to 20 characters because `remark` is a broker-side field with limits
    this code does not control, and an id that is silently truncated is an id
    that cannot be matched back.
    """
    return "mmt" + uuid.uuid4().hex[:17]


class DuplicateIntent(Exception):
    """A live order already serves this business intent."""

    def __init__(self, existing: dict) -> None:
        super().__init__(
            f"{existing['symbol']}: order {existing['client_order_id']} is "
            f"already {existing['state']} for the same intent "
            f"({existing.get('intent_key')})")
        self.existing = existing


def intent_key(symbol: str, side: str, kind: str) -> str:
    """The stable name of what an order is FOR.

    client_order_id is new on every attempt — it correlates one request with
    one answer, which is what it is for, and is exactly why it cannot prevent a
    second request. "Buy AAPL as an entry" is the thing there may only be one
    live order for, and it has to be the same string on the retry as on the
    original or it prevents nothing.
    """
    return f"{symbol.upper()}:{side.upper()}:{kind.upper()}"


def begin(*, symbol: str, side: str, kind: str, requested_qty: int,
          limit_price: float | None = None, aux_price: float | None = None,
          intent: str = "", extra: dict | None = None,
          enforce_unique_intent: bool = True) -> str:
    """Record the intent to send an order. Returns the client_order_id.

    Must be called BEFORE the broker. The row is committed and the database
    flushed before this returns, so that the record cannot be younger than the
    order it describes.

    Raises DuplicateIntent when a live order already serves the same intent.
    That check is the UNIQUE index, not a read-then-write in Python: two
    threads, or a retry racing a slow first attempt, both pass a read check and
    only the database can refuse them both.
    """
    from . import identity
    account_id = db._require_account_id("recording an order")
    session_id = identity.require_session_id()
    coid = new_client_order_id()
    ikey = intent_key(symbol, side, kind) if enforce_unique_intent else None

    try:
        with db.transaction() as c:
            c.execute("""
                INSERT INTO orders (client_order_id, account_id, session_id,
                    symbol, side, kind, intent, requested_qty, limit_price,
                    aux_price, state, created_at, extra, intent_key)
                VALUES (?,?,?,?,?,?,?,?,?,?, 'PENDING_SUBMIT', ?, ?, ?)
            """, (coid, account_id, session_id, symbol, side.upper(),
                  kind.upper(), intent, int(requested_qty), limit_price,
                  aux_price, _now(),
                  json.dumps(extra, default=str) if extra else None, ikey))
    except sqlite3.IntegrityError as e:
        # SQLite names the COLUMNS in this message, not the index, so matching
        # on the index name silently never fired and the IntegrityError went to
        # the caller as an unexplained crash.
        msg = str(e)
        if ikey and "intent_key" in msg and "UNIQUE" in msg.upper():
            existing = live_for_intent(ikey)
            if existing:
                log.error("refusing a second live order for %s — %s is still %s",
                          ikey, existing["client_order_id"], existing["state"])
                raise DuplicateIntent(existing) from e
        raise
    if False:
        pass
    log.info("order %s PENDING_SUBMIT — %s %s %s", coid, side, requested_qty,
             symbol)
    return coid


def submitted(coid: str, broker_order_id: str) -> None:
    _set(coid, state="SUBMITTED", broker_order_id=str(broker_order_id),
         submitted_at=_now())
    log.info("order %s SUBMITTED — broker id %s", coid, broker_order_id)


def rejected(coid: str, error: str) -> None:
    """The broker explicitly refused. Nothing reached the order book.

    Distinct from unknown() on purpose: a rejection is evidence, and acting on
    evidence is allowed. Treating every failure as UNKNOWN would make the
    recovery sweep chase orders that provably never existed.
    """
    _set(coid, state="REJECTED", last_error=error[:500], resolved_at=_now())
    log.warning("order %s REJECTED — %s", coid, error[:200])


def failed_local(coid: str, error: str) -> None:
    """We refused it ourselves; it was never sent."""
    _set(coid, state="FAILED_LOCAL", last_error=error[:500], resolved_at=_now())


def unknown(coid: str, error: str) -> None:
    """The call failed in a way that does not prove the order never arrived.

    This is the state the whole log exists for. It must never be resolved by
    reasoning — only by asking the broker and matching on our own id.
    """
    _set(coid, state="UNKNOWN", last_error=error[:500])
    log.error("order %s UNKNOWN — %s. The order may or may not be at the "
              "broker; it will be resolved by querying, not assumed.",
              coid, error[:200])


def record_fill(coid: str, *, filled_qty: int, avg_price: float | None,
                state: str, broker_order_id: str | None = None) -> None:
    """Update from what the broker reports. Fill quantity only ever grows.

    A poll that reports LESS than we have already recorded is a stale or partial
    view, not a reversal — orders do not un-fill. Taking the lower number would
    shrink a position we hold, and the correction would look like a sale.
    """
    row = get(coid)
    if row is None:
        raise OrderLogError(f"no order {coid}")
    prev = int(row.get("filled_qty") or 0)
    qty = max(prev, int(filled_qty or 0))
    if int(filled_qty or 0) < prev:
        log.warning("order %s: broker reports %s filled but %s was already "
                    "recorded — keeping the higher figure",
                    coid, filled_qty, prev)
    fields = {"filled_qty": qty, "state": state, "last_polled_at": _now()}
    if avg_price:
        fields["avg_fill_price"] = float(avg_price)
    if broker_order_id:
        fields["broker_order_id"] = str(broker_order_id)
    if state in TERMINAL_STATES:
        fields["resolved_at"] = _now()
    _set(coid, **fields)


def _set(coid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction() as c:
        n = c.execute(f"UPDATE orders SET {cols} WHERE client_order_id = ?",
                      (*fields.values(), coid)).rowcount
    if not n:
        raise OrderLogError(f"no order {coid} to update")


def get(coid: str) -> dict | None:
    with db.conn() as c:
        r = c.execute("SELECT * FROM orders WHERE client_order_id = ?",
                      (coid,)).fetchone()
    return dict(r) if r else None


def live_for_intent(ikey: str) -> dict | None:
    """The live order serving this intent, if there is one."""
    acct = db._require_account_id("checking an intent")
    with db.conn() as c:
        r = c.execute(
            f"SELECT * FROM orders WHERE account_id = ? AND intent_key = ? "
            f"AND state IN ({','.join('?' * len(LIVE_STATES))}) "
            f"ORDER BY created_at DESC LIMIT 1",
            (acct, ikey, *LIVE_STATES)).fetchone()
    return dict(r) if r else None


def by_broker_id(broker_order_id: str) -> dict | None:
    acct = db._require_account_id("looking up an order")
    with db.conn() as c:
        r = c.execute("SELECT * FROM orders WHERE account_id = ? AND "
                      "broker_order_id = ? ORDER BY created_at DESC LIMIT 1",
                      (acct, str(broker_order_id))).fetchone()
    return dict(r) if r else None


def cancel_requested(coid: str, *, accepted: bool, detail: str = "") -> None:
    """Record that a cancellation was ASKED FOR. Never that it happened.

    The broker accepting a cancel request says the request was well-formed. It
    does not say the order is gone: it may have filled in the moment between
    the decision to cancel and the request arriving. That gap is exactly the
    OCO race — the stop and the take-profit both filling before either could be
    pulled — so an order stays live here until a poll shows what became of it.

    A refused cancel is worse and is recorded loudly: the order is still
    working, and anything that assumed otherwise is about to act on a position
    it does not have the protection it thinks it has.
    """
    row = get(coid)
    if row is None:
        return
    extra = {}
    if row.get("extra"):
        try:
            extra = json.loads(row["extra"])
        except (json.JSONDecodeError, TypeError):
            extra = {}
    extra["cancel_requested_at"] = _now()
    extra["cancel_accepted"] = bool(accepted)
    if detail:
        extra["cancel_detail"] = detail[:300]
    _set(coid, extra=json.dumps(extra, default=str),
         last_error=None if accepted else f"cancel refused: {detail[:400]}")
    log.log(logging.INFO if accepted else logging.ERROR,
            "order %s: cancel %s%s", coid,
            "accepted (not yet confirmed gone)" if accepted else "REFUSED",
            f" — {detail[:200]}" if detail else "")


def live_orders(symbol: str | None = None) -> list[dict]:
    """Orders that have not reached a terminal state, for THIS account."""
    acct = db._require_account_id("listing live orders")
    q = ("SELECT * FROM orders WHERE account_id = ? AND state IN "
         f"({','.join('?' * len(LIVE_STATES))})")
    args: list = [acct, *LIVE_STATES]
    if symbol:
        q += " AND symbol = ?"
        args.append(symbol)
    q += " ORDER BY created_at"
    with db.conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def recent(limit: int = 100, symbol: str | None = None) -> list[dict]:
    acct = db._require_account_id("listing orders")
    q = "SELECT * FROM orders WHERE account_id = ?"
    args: list = [acct]
    if symbol:
        q += " AND symbol = ?"
        args.append(symbol)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with db.conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def map_broker_status(status: object) -> str:
    """A broker order_status as one of our states, or 'SUBMITTED' if unknown.

    Falling back to a live state rather than a terminal one is deliberate: an
    order status this code has not seen before must keep being polled, not be
    written off as settled on the strength of not recognising it.
    """
    s = str(status or "").upper().strip()
    return BROKER_STATUS_MAP.get(s, "SUBMITTED")


def reconcile_live(client, *, lookback_days: int = 2) -> dict:
    """Ask the broker about every order that has not settled, and converge.

    This is the only thing that resolves an UNKNOWN. Nothing else may: the
    state exists precisely because the process has no evidence, and reasoning
    from its absence is what produces a duplicate order.

    Returns a summary. Anything it could not settle stays live and is reported
    rather than quietly dropped — an order the software cannot account for is
    the thing an operator most needs to be told about.
    """
    live = live_orders()
    if not live:
        # complete=True, explicitly. Nothing outstanding is the HEALTHIEST
        # outcome this function has, and omitting the key made it the only
        # outcome a caller checking `complete` reads as a failure. A contract
        # that inverts on the good path is one every caller gets wrong once.
        return {"checked": 0, "resolved": 0, "complete": True,
                "still_unknown": 0, "orders": []}

    # One query for the whole window rather than one per order: the history
    # endpoint is rate-limited hard enough that a per-order loop starts failing
    # partway through, and a partial answer here would settle some orders and
    # silently leave others looking unresolvable.
    from datetime import timedelta
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(1, lookback_days))
    try:
        df = client.history_orders(start.isoformat(), end.isoformat())
    except Exception as e:
        log.error("order recovery could not query the broker: %s", e)
        return {"checked": len(live), "resolved": 0, "complete": False,
                "still_unknown": len(live), "error": str(e)[:200],
                "orders": [o["client_order_id"] for o in live]}

    # A partial answer settles nothing. An order missing from an incomplete
    # list is missing from the LIST, which says nothing about the order book —
    # and the caller's next step for an unfound order is to conclude it never
    # landed. Rows that ARE present are still applied: they are evidence, and
    # evidence does not become unreliable because a second query failed.
    complete = bool(df.attrs.get("complete", True))
    if not complete:
        log.error("order recovery: the broker's answer was incomplete (%s) — "
                  "orders absent from it are NOT being written off",
                  "; ".join(df.attrs.get("failures", []))[:300])

    resolved, unresolved = 0, []
    for o in live:
        coid = o["client_order_id"]
        hit = claim_from_broker(coid, df)
        if hit is None and o.get("broker_order_id"):
            hit = _claim_by_broker_id(o["broker_order_id"], df)
        if hit is None:
            # Not at the broker. For an order we know was submitted this means
            # it fell outside the window; for a PENDING_SUBMIT or UNKNOWN it is
            # evidence it never landed — but only once the window is wide enough
            # to have seen it, so the decision is left to the caller's policy
            # rather than made silently here.
            unresolved.append(coid)
            continue
        state = map_broker_status(hit.get("order_status"))
        record_fill(coid, filled_qty=int(float(hit.get("dealt_qty") or 0)),
                    avg_price=float(hit.get("dealt_avg_price") or 0) or None,
                    state=state,
                    broker_order_id=str(hit.get("order_id") or "") or None)
        resolved += 1
        log.info("order %s resolved from the broker: %s (%s filled)",
                 coid, state, hit.get("dealt_qty"))

    if unresolved:
        log.warning("order recovery: %d order(s) not found at the broker — %s",
                    len(unresolved), unresolved)
    return {"checked": len(live), "resolved": resolved, "complete": complete,
            "still_unknown": len(unresolved), "orders": unresolved}


def _claim_by_broker_id(broker_order_id: str, orders_df) -> dict | None:
    """Fallback for orders placed before remarks were carried."""
    if orders_df is None or not len(orders_df):
        return None
    if "order_id" not in getattr(orders_df, "columns", []):
        return None
    hits = orders_df[orders_df["order_id"].astype(str) == str(broker_order_id)]
    return hits.iloc[0].to_dict() if len(hits) else None


def claim_from_broker(coid: str, orders_df) -> dict | None:
    """Find our order among the broker's, by remark. Returns the matched row.

    This is how an UNKNOWN is resolved. Matching on the remark rather than on
    (symbol, side, qty, time) matters most in exactly the situation that
    produces an UNKNOWN: a retry standing next to the original, identical in
    every attribute a heuristic would compare.
    """
    if orders_df is None or not len(orders_df):
        return None
    if "remark" not in getattr(orders_df, "columns", []):
        return None
    hits = orders_df[orders_df["remark"].astype(str).str.strip() == coid]
    if not len(hits):
        return None
    return hits.iloc[0].to_dict()
