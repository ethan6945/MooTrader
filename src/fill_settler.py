"""Applying a fill to the books exactly once, however many times it is seen.

THE PROBLEM THIS SOLVES

  An order's `filled_qty` is the broker's CUMULATIVE total. Read it twice and
  you see the same shares twice; act on it twice and you buy the position
  twice, or book the same close twice.

  The entry and exit paths avoided that by only looking once, inside a bounded
  wait — which trades one bug for another. A fill arriving after the wait
  updated the order row and nothing else, so the shares existed at the broker,
  and in the orders table, and in no position. Nobody was watching for them.

  So the order carries a second number: how much this software has ALREADY
  applied. The difference is the only thing that may be applied, and it is
  written in the same transaction as the position it moves. Run this a hundred
  times and the books move once.

WHY ORDER-LEVEL AND NOT PER-DEAL
  Per-deal ids would be finer and are not available on this account: the broker
  answers "Paper trading does not support deal data" to both deal_list_query
  and history_deal_list_query. Order-level cumulative totals are the finest
  grain it will give, and the delta between filled and applied is exact at that
  grain — what it cannot do is attribute a delta to individual executions,
  which matters for nothing here except a per-fill audit nobody can produce.

ONE SETTLER, EVERY PATH
  Entries, stacks, exits, stop and take-profit legs, scale-out tranches, manual
  closes. They previously each did their own arithmetic, which is why the
  bracket paths still booked whole positions on partial fills long after the
  main exit path stopped. There is one place to be right now.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import db, order_log

log = logging.getLogger(__name__)

BUY_KINDS = ("ENTRY", "STACK")
SELL_KINDS = ("EXIT", "STOP", "TP", "SCALE_OUT")


class OversoldError(Exception):
    """A sale settled for more shares than the position held."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def unapplied(row: dict) -> tuple[int, float]:
    """(shares, average price) not yet applied from this order.

    The price is derived from notional rather than taken from avg_fill_price,
    because avg_fill_price is the average over ALL fills. If 40 filled at 100
    and then 60 more at 110, the cumulative average is 106 — but the 60 new
    shares cost 110, and booking them at 106 would put a price the broker never
    charged into the position's cost basis.
    """
    filled = int(row.get("filled_qty") or 0)
    applied = int(row.get("applied_qty") or 0)
    delta = filled - applied
    if delta <= 0:
        return 0, 0.0
    total_notional = filled * float(row.get("avg_fill_price") or 0.0)
    applied_notional = float(row.get("applied_notional") or 0.0)
    delta_notional = total_notional - applied_notional
    return delta, (delta_notional / delta if delta else 0.0)


def settle(coid: str, *, allow_oversell_halt: bool = True) -> dict:
    """Apply whatever of this order has filled and not yet been applied.

    Idempotent. Returns what it did; a second call on the same order returns
    zeros and touches nothing.
    """
    row = order_log.get(coid)
    if row is None:
        raise order_log.OrderLogError(f"no order {coid}")

    qty, price = unapplied(row)
    if qty <= 0:
        return {"applied": 0, "symbol": row["symbol"], "kind": row["kind"]}

    side, kind, symbol = row["side"], row["kind"], row["symbol"]
    if side == "BUY":
        result = _apply_buy(row, qty, price)
    else:
        result = _apply_sell(row, qty, price, allow_oversell_halt)
    log.info("settled %s: %s %d %s @ $%.4f (order %s)",
             coid, side, qty, symbol, price, kind)
    return result


def _mark_applied_c(c, coid: str, qty: int, notional: float) -> None:
    """Bump the applied counters. MUST run in the caller's transaction.

    Conditional on the current value so a concurrent settlement cannot double
    it: whichever transaction commits second finds applied_qty already moved
    and its UPDATE matches nothing.
    """
    n = c.execute(
        "UPDATE orders SET applied_qty = applied_qty + ?, "
        "applied_notional = applied_notional + ?, last_polled_at = ? "
        "WHERE client_order_id = ? AND applied_qty + ? <= filled_qty",
        (qty, notional, _now(), coid, qty)).rowcount
    if not n:
        raise order_log.OrderLogError(
            f"order {coid}: applied_qty would exceed filled_qty — another "
            f"settlement got there first")


def _apply_buy(row: dict, qty: int, price: float) -> dict:
    """Add shares to the position at what they actually cost."""
    symbol, coid = row["symbol"], row["client_order_id"]
    with db.transaction() as c:
        cur = c.execute("SELECT * FROM open_trades WHERE account_id = ? AND "
                        "symbol = ?", (row["account_id"], symbol)).fetchone()
        if cur is None:
            raise order_log.OrderLogError(
                f"{symbol}: a BUY settled with no position row to add it to. "
                f"The entry path creates the position; this only extends it.")
        old_qty = int(cur["qty"])
        old_entry = float(cur["entry_price"])
        new_qty = old_qty + qty
        new_entry = (old_qty * old_entry + qty * price) / new_qty
        c.execute("UPDATE open_trades SET qty = ?, entry_price = ? "
                  "WHERE account_id = ? AND symbol = ?",
                  (new_qty, new_entry, row["account_id"], symbol))
        _mark_applied_c(c, coid, qty, qty * price)
    log.info("%s: +%d shares from a late fill → %d @ avg $%.4f",
             symbol, qty, new_qty, new_entry)
    return {"applied": qty, "symbol": symbol, "kind": row["kind"],
            "price": price, "position_qty": new_qty}


def _apply_sell(row: dict, qty: int, price: float,
                allow_oversell_halt: bool) -> dict:
    """Book a close for what sold, and reduce the position by exactly that."""
    symbol, coid = row["symbol"], row["client_order_id"]
    reason = _exit_reason(row)

    # Checked BEFORE the write transaction opens. Halting from inside one means
    # a second connection trying to write while this one holds the lock, which
    # blocks — so the first version detected the oversell correctly and then
    # failed to record it.
    with db.conn() as c:
        cur = c.execute("SELECT qty FROM open_trades WHERE account_id = ? AND "
                        "symbol = ?", (row["account_id"], symbol)).fetchone()
    held = int(cur["qty"]) if cur else 0
    if qty > held:
        # More sold than held. Either two legs of a bracket both filled, or
        # something sold outside this software. Booking it would put a negative
        # position in the books and the next "exit" for it would be a genuine
        # short sale, so this stops instead.
        if allow_oversell_halt:
            _halt_oversold(symbol, qty, held, coid)
        raise OversoldError(
            f"{symbol}: order {coid} settled {qty} sold but only {held} "
            f"were held. Trading halted; this is the shape of an OCO race "
            f"where both legs filled.")

    with db.transaction() as c:
        cur = c.execute("SELECT * FROM open_trades WHERE account_id = ? AND "
                        "symbol = ?", (row["account_id"], symbol)).fetchone()
        if cur is None or int(cur["qty"]) < qty:
            # Changed under us between the check and the write.
            raise OversoldError(
                f"{symbol}: the position changed while settling {coid}")
        held = int(cur["qty"])
        entry = float(cur["entry_price"])
        stop = float(cur["stop_loss"])
        pnl = (price - entry) * qty
        db._closed_trade_insert_c(c, {
            "ts": _now(), "symbol": symbol, "qty": qty, "entry": round(entry, 4),
            "stop": round(stop, 4), "exit": round(price, 4),
            "exit_reason": reason, "pnl": round(pnl, 4),
            "pnl_pct": round((price - entry) / entry * 100, 4) if entry else 0.0,
            "r_multiple": round((price - entry) / (entry - stop), 4)
                          if entry > stop else 0.0,
            "opened_at": cur["opened_at"],
            "strategy": cur["strategy"] if "strategy" in cur.keys() else None,
        }, row["account_id"], row["session_id"], None)

        remaining = held - qty
        if remaining > 0:
            c.execute("UPDATE open_trades SET qty = ? WHERE account_id = ? "
                      "AND symbol = ?", (remaining, row["account_id"], symbol))
        else:
            c.execute("DELETE FROM open_trades WHERE account_id = ? AND "
                      "symbol = ?", (row["account_id"], symbol))
        _mark_applied_c(c, coid, qty, qty * price)

    # PnL state is updated outside the transaction on purpose: it is a
    # different concern with its own atomic write, and the ledger row above is
    # the record of truth either way.
    try:
        from . import risk_manager
        risk_manager.record_trade_close(pnl)
    except Exception as e:
        log.error("%s: close booked but PnL state update failed: %s", symbol, e)

    log.info("%s: -%d shares booked as %s @ $%.4f (pnl %+.2f), %d remain",
             symbol, qty, reason, price, pnl, remaining)
    return {"applied": qty, "symbol": symbol, "kind": row["kind"],
            "price": price, "pnl": pnl, "position_qty": remaining}


def _exit_reason(row: dict) -> str:
    kind = str(row.get("kind") or "").upper()
    if kind == "STOP":
        return "SL_BRACKET"
    if kind == "TP":
        return "TP_BRACKET"
    if kind == "SCALE_OUT":
        return "SCALE_OUT"
    return str(row.get("intent") or "EXIT")[:32] or "EXIT"


def _halt_oversold(symbol: str, sold: int, held: int, coid: str) -> None:
    try:
        from . import risk_manager
        risk_manager.halt(
            "oversold position",
            f"{symbol}: order {coid} reports {sold} sold against {held} held. "
            f"Two protective legs may both have filled, or something sold "
            f"outside this software. The books are NOT being adjusted to fit; "
            f"check the account.")
    except Exception as e:
        log.error("could not halt after an oversell on %s: %s", symbol, e)


def mark_applied(coid: str, qty: int, price: float) -> None:
    """Record that the caller has already applied this much of an order.

    For the entry and exit paths, which apply their own first fill: the entry
    CREATES the position (this module can only extend one) and the exit books
    its close with the reason and metadata the settler cannot reconstruct. They
    do the work; this stops the sweep from doing it again.

    The mark is a separate transaction from their write, so a crash between the
    two leaves the fill unapplied-looking and the sweep applies it a second
    time. That window is small and one-sided — it can duplicate, not lose — and
    closing it means routing position creation through the settler too, which
    is the next thing to do rather than something to claim is done.
    """
    if qty <= 0:
        return
    with db.transaction() as c:
        _mark_applied_c(c, coid, int(qty), float(qty) * float(price))


def settle_all(*, limit: int = 200) -> dict:
    """Settle every order with fills this software has not applied.

    The sweep that catches a fill arriving after its wait window closed. Cheap
    when there is nothing to do — the index is partial on filled_qty >
    applied_qty, so an account with no outstanding work touches no rows.
    """
    acct = db._require_account_id("settling fills")
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT client_order_id FROM orders WHERE account_id = ? AND "
            "filled_qty > applied_qty ORDER BY created_at LIMIT ?",
            (acct, limit))]
    applied, failures = 0, []
    for r in rows:
        try:
            out = settle(r["client_order_id"])
            applied += out["applied"]
        except Exception as e:
            failures.append({"order": r["client_order_id"], "error": str(e)[:200]})
            log.error("could not settle %s: %s", r["client_order_id"], e)
    if rows:
        log.info("fill settlement: %d order(s) with unapplied fills, %d shares "
                 "applied, %d failed", len(rows), applied, len(failures))
    return {"orders": len(rows), "applied": applied, "failures": failures}
