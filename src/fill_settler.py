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


# Halts raised from inside a settlement transaction, fired once it has
# committed. halt() writes kv_state, and calling it while a transaction is open
# would nest one inside another — so the reason is parked here and flushed
# after the commit. The order matters: the position must be on disk BEFORE
# anything announces that it is unprotected.
_HALT_AFTER_COMMIT: list[tuple[str, str]] = []


def _flush_halts() -> None:
    """Raise any halt the committed transaction asked for. Never raises."""
    while _NEEDS_MIRROR:
        _NEEDS_MIRROR.pop()
        _refresh_mirror()
    while _HALT_AFTER_COMMIT:
        reason, detail = _HALT_AFTER_COMMIT.pop(0)
        try:
            from . import risk_manager
            risk_manager.halt(reason, detail)
        except Exception as e:
            log.error("could not halt after settlement (%s): %s", reason, e)


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
    try:
        if side == "BUY":
            result = _apply_buy(row, qty, price)
        else:
            result = _apply_sell(row, qty, price, allow_oversell_halt)
    finally:
        _flush_halts()
    log.info("settled %s: %s %d %s @ $%.4f (order %s)",
             coid, side, qty, symbol, price, kind)
    return result


def _refresh_mirror() -> None:
    """Rewrite data/open_trades.json from the database.

    The mirror is a DERIVED file — the database is authoritative — but the
    start protocol compares the two and refuses to start when they disagree.
    That check was written when the executor was the only thing that created
    positions, and the executor rewrote the mirror on every pass.

    This module then became the only place a position is created or changed,
    and did not take the mirror with it. The result was not subtle: every
    position opened through the settler left the mirror stale, so the NEXT
    start refused with position_conflict. In staging that is an annoyance; in
    production it is the bot declining to come back up after its first fill.

    Never raises. A stale mirror must not be able to undo a settlement that has
    already committed — the database is the record, and the worst case here is
    that the file is refreshed on the next settlement instead.
    """
    try:
        db.mirror_open_trades_json(db.load_open_trades())
    except Exception as e:
        log.error("position mirror could not be refreshed: %s", e)


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
            # A late FIRST fill: the entry stopped waiting, the fill arrived
            # afterwards, and there is no position to extend.
            #
            # This used to raise. The shares were at the broker, in nothing, and
            # the sweep retried the same failure on every tick forever — the one
            # case where "we own something the books do not know about" was
            # guaranteed to persist. Booking it is the only outcome that makes
            # the ledger describe the account.
            out = _open_from_order(c, row, qty, price)
            _NEEDS_MIRROR.append(True)
            return out
        old_qty = int(cur["qty"])
        old_entry = float(cur["entry_price"])
        new_qty = old_qty + qty
        new_entry = (old_qty * old_entry + qty * price) / new_qty
        c.execute("UPDATE open_trades SET qty = ?, entry_price = ? "
                  "WHERE account_id = ? AND symbol = ?",
                  (new_qty, new_entry, row["account_id"], symbol))
        _mark_applied_c(c, coid, qty, qty * price)
    _refresh_mirror()
    log.info("%s: +%d shares from a late fill → %d @ avg $%.4f",
             symbol, qty, new_qty, new_entry)
    return {"applied": qty, "symbol": symbol, "kind": row["kind"],
            "price": price, "position_qty": new_qty}


_NEEDS_MIRROR: list = []


def _open_from_order(c, row: dict, qty: int, price: float) -> dict:
    """Create the position a late first fill belongs to, inside the caller's
    transaction, with the protection the entry intended.

    The stop is not invented here. executor records intended_stop/intended_tp on
    the order before it is sent, precisely so a fill can be settled by something
    that knows nothing about the scan that caused it. When they are absent — an
    order written before that existed, or one placed by a path that does not set
    them — the position is still created, because shares at the broker must
    appear in the ledger either way, and then trading HALTS: an unprotected
    position is exactly the thing that must never exist quietly.
    """
    from . import identity, risk_manager
    symbol, coid = row["symbol"], row["client_order_id"]
    try:
        meta = json.loads(row.get("extra") or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        meta = {}
    stop = meta.get("intended_stop")
    tp = meta.get("intended_tp")

    try:
        session_id = identity.current_session_id()
    except Exception:
        session_id = None

    trade = {"symbol": symbol, "qty": int(qty), "entry_price": float(price),
             "stop_loss": float(stop) if stop else 0.0,
             "take_profit": float(tp) if tp else 0.0,
             "atr": float(meta.get("atr") or 0.0),
             "strategy": meta.get("strategy") or "late_fill"}
    db._upsert_open_trade_c(c, trade, row["account_id"], session_id,
                            {"opened_by": "late_fill_settlement",
                             "source_order": coid})
    _mark_applied_c(c, coid, int(qty), float(qty) * float(price))

    if stop and tp and 0 < float(stop) < float(price):
        log.warning("%s: late first fill booked — %d @ $%.4f, stop $%.2f, "
                    "tp $%.2f (from the order's recorded intent)",
                    symbol, qty, price, float(stop), float(tp))
    else:
        log.error("%s: late first fill booked WITHOUT protective levels "
                  "(order %s carries none) — halting", symbol, coid)
        _HALT_AFTER_COMMIT.append(
            ("late fill without protection",
             f"{symbol}: {qty} shares settled from order {coid}, which records "
             f"no intended stop. The position exists and is UNPROTECTED."))
    return {"applied": int(qty), "symbol": symbol, "kind": row["kind"],
            "price": float(price), "position_qty": int(qty)}


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

        # The SAME numbers portfolio.record_close computes, from the same
        # inputs. There were two close-booking paths — the executor's, with
        # MFE/MAE and an R-multiple anchored to the ORIGINAL stop, and a
        # simplified one here — which is exactly the divergence that let the
        # bracket paths stay wrong for a month after the main exit path was
        # fixed. One settler means one arithmetic.
        hw = float(cur["high_water"] or entry) if "high_water" in cur.keys() else entry
        lw = float(cur["low_water"] or entry) if "low_water" in cur.keys() else entry
        r_unit = float(cur["init_risk_per_share"] or 0) \
            if "init_risk_per_share" in cur.keys() else 0.0
        if r_unit <= 0:
            r_unit = entry - stop
        db._closed_trade_insert_c(c, {
            "ts": _now(), "symbol": symbol, "qty": qty, "entry": round(entry, 2),
            "stop": round(stop, 2), "exit": round(price, 2),
            "exit_reason": reason, "pnl": round(pnl, 2),
            "pnl_pct": round((price - entry) / entry * 100, 2) if entry else 0.0,
            "r_multiple": round((price - entry) / r_unit, 2) if r_unit > 0 else 0.0,
            "opened_at": cur["opened_at"],
            "mfe_pct": round((hw - entry) / entry * 100, 2) if entry else None,
            "mae_pct": round((lw - entry) / entry * 100, 2) if entry else None,
            "ml_proba_entry": cur["ml_proba_entry"]
                              if "ml_proba_entry" in cur.keys() else None,
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

    _refresh_mirror()
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


def open_position(coid: str, trade: dict, qty: int, price: float) -> dict:
    """Write a new (or replacement) position AND mark its fill applied, together.

    The entry path cannot use settle(): there is no position row yet, and
    settle() only extends one. But it has the same requirement — the position
    and the applied counter must land in one commit, or a crash between them
    lets the sweep add the same shares again.

    So the entry hands the fully-built trade dict here and this writes both.
    """
    from . import identity
    account_id = db._require_account_id("opening a position")
    try:
        session_id = identity.current_session_id()
    except Exception:
        session_id = None

    extra = dict(trade.get("extra") or {}) if isinstance(trade.get("extra"), dict) else {}
    for k, v in trade.items():
        if k not in db._OPEN_TRADE_COLUMNS:
            extra[k] = v

    with db.transaction() as c:
        db._upsert_open_trade_c(c, trade, account_id, session_id, extra)
        _mark_applied_c(c, coid, int(qty), float(qty) * float(price))
    _refresh_mirror()
    return {"applied": int(qty), "symbol": trade["symbol"], "kind": "ENTRY",
            "price": float(price)}


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


def mark_externally_booked(coid: str, qty: int, price: float,
                           *, booked_by: str) -> bool:
    """Record that ANOTHER path already put this order's fill in the ledger.

    Sets filled_qty and applied_qty together, so the settler sees an order with
    nothing left to apply rather than a fill it is about to book a second time.

    Not `mark_applied`: that one goes through _mark_applied_c, whose guard is
    `applied_qty + n <= filled_qty`. It is for a caller that applied a fill the
    poll had ALREADY recorded. Here the poll never happened — filled_qty is 0
    and the evidence came from the broker's order list instead — so the same
    call would raise "applied_qty would exceed filled_qty". Both numbers have to
    move, and they move from the broker's account of what it did.

    This exists because reconcile's ghost handler books a close directly from
    broker evidence, using the broker's order id. That order can still be open
    in our own log — indeed it usually is, because "our log thinks it is still
    working" is exactly the condition that produced the ghost. Once the manage
    tick re-polls live orders (executor._refresh_live_orders), that order comes
    back FILLED, settle_all selects it, and _apply_sell finds the position
    already gone: qty > held, which is the oversell signature. It would halt
    trading over a close that was booked correctly, minutes earlier, by design.

    Returns True if an order row was updated.
    """
    row = order_log.get(coid)
    if row is None:
        return False
    already = int(row.get("applied_qty") or 0)
    if already >= int(qty):
        return False
    filled = max(int(row.get("filled_qty") or 0), int(qty))
    with db.transaction() as c:
        c.execute(
            "UPDATE orders SET filled_qty = ?, applied_qty = ?, "
            "applied_notional = ?, avg_fill_price = ?, state = ?, "
            "resolved_at = ?, last_polled_at = ? WHERE client_order_id = ?",
            (filled, filled, filled * float(price), float(price), "FILLED",
             _now(), _now(), coid))
    log.warning("order %s: %d share(s) @ $%.4f marked applied — the close was "
                "already booked by %s", coid, filled, price, booked_by)
    return True


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
    if failures:
        # A fill the broker made and this software could not book means the
        # account holds something the ledger does not describe. Every later
        # decision — position sizing, concentration, the drawdown breaker, the
        # stop distance — is computed from that ledger, so continuing is not
        # "carrying on despite a warning", it is trading on numbers already
        # known to be wrong.
        #
        # This used to collect the failures into the return value and carry on.
        # Nothing read them.
        from . import risk_manager
        first = failures[0]
        risk_manager.halt(
            "fill settlement failed",
            f"{len(failures)} fill(s) could not be applied to the ledger; "
            f"first: {first['order']} — {first['error']}")
    return {"orders": len(rows), "applied": applied, "failures": failures}
