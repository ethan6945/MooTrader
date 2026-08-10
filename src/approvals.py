"""Approval queue — analyze → notify → APPROVE → execute. Feedback 铁律.

Automated analysis (weekly self-review, reconcile drift, blacklist, and a future
Anthropic-API background optimizer) NEVER changes live behavior silently. It
enqueues a suggestion here; the owner approves/rejects (GUI panel, or the
`approve`/`reject` CLI), and the scan loop applies APPROVED items via
apply_approved(). This is the single choke point that enforces "no silent
execution" — and the reserved mount point for the future autonomous optimizer
(it writes proposals here, never to the live config).
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import db

log = logging.getLogger(__name__)

QUEUE_KEY = "pending_approvals"
# Kinds that perform a real mutation when approved (everything else is
# informational — approving just acknowledges/dismisses it).
EXECUTABLE_KINDS = {"blacklist_review", "param_change", "manual_takeover_sell"}


def _now() -> str:
    return datetime.now().isoformat()


def list_all() -> list[dict]:
    return list(db.get_state().get(QUEUE_KEY, []))


def list_pending() -> list[dict]:
    return [a for a in list_all() if a.get("status") == "pending"]


def _dedupe_key(kind: str, payload: dict | None) -> str:
    p = payload or {}
    tag = p.get("symbol") or p.get("strategy") or p.get("key") or ""
    return f"{kind}:{tag}"


def enqueue(kind: str, detail: str, action: str, payload: dict | None = None) -> str:
    """Add a suggestion for owner approval. De-dupes against existing PENDING
    items with the same (kind, target) so repeated weekly reviews don't pile up
    duplicates. Returns the dedupe key."""
    key = _dedupe_key(kind, payload)

    def _apply(state: dict) -> dict:
        q = list(state.get(QUEUE_KEY, []))
        for a in q:
            if a.get("status") == "pending" and a.get("dedupe") == key:
                return {}  # already queued — no change
        item = {
            "id": f"{int(datetime.now().timestamp() * 1000)}-{len(q)}",
            "kind": kind, "detail": detail, "action": action,
            "payload": payload or {}, "dedupe": key,
            "status": "pending", "created_at": _now(),
        }
        q.append(item)
        return {QUEUE_KEY: q}

    db.atomic_state(_apply)
    return key


def resolve(item_id: str, approved: bool) -> bool:
    """Owner marks an item approved or rejected. Returns True if found+changed."""
    found = {"v": False}

    def _apply(state: dict) -> dict:
        q = list(state.get(QUEUE_KEY, []))
        for a in q:
            if a.get("id") == item_id and a.get("status") == "pending":
                a["status"] = "approved" if approved else "rejected"
                a["resolved_at"] = _now()
                found["v"] = True
        return {QUEUE_KEY: q}

    db.atomic_state(_apply)
    return found["v"]


# An execution claim older than this is treated as a crashed executor and
# may be re-claimed (the lease exists to close the cross-process race, not
# to serialize retries forever).
_CLAIM_LEASE_SEC = 600


def _claim_for_execution(item_id: str) -> bool:
    """Atomically claim an approved item before executing it.

    apply_approved() runs from THREE entry points — the scan loop, the 5-min
    Telegram sync (scheduler process), and the web server right after an
    approve tap — in different processes. Without a claim, two of them can
    read status=approved/executed=False simultaneously and both execute; for
    `manual_takeover_sell` that is a DOUBLE SELL. The claim rides on
    db.atomic_state (BEGIN IMMEDIATE → serialized across processes), so
    exactly one caller wins. A stale claim (executor crashed mid-run) is
    reclaimable after _CLAIM_LEASE_SEC."""
    won = {"v": False}

    def _apply(state: dict) -> dict:
        q = list(state.get(QUEUE_KEY, []))
        for x in q:
            if x.get("id") != item_id:
                continue
            if x.get("status") != "approved" or x.get("executed"):
                return {}
            lease = x.get("executing_at")
            if lease:
                try:
                    age = (datetime.now()
                           - datetime.fromisoformat(lease)).total_seconds()
                except (ValueError, TypeError):
                    age = _CLAIM_LEASE_SEC + 1
                if age < _CLAIM_LEASE_SEC:
                    return {}   # someone else is executing it right now
            x["executing_at"] = _now()
            won["v"] = True
            return {QUEUE_KEY: q}
        return {}

    db.atomic_state(_apply)
    return won["v"]


class ExecutionRefused(Exception):
    """A permanent refusal — the action is not allowed, so retrying is pointless.

    Distinct from a transient failure (market shut, broker timeout), which
    releases the claim so the next cycle tries again. A refusal must not be
    retried forever, and — the reason this exists — must not be reported to the
    owner as applied. Before this, a param_change blocked by the freeze was
    caught, logged at WARNING, and then marked executed=True: the owner got
    "✅ 已执行你批准的建议" for a change that never happened.
    """


def _finish_execution(item_id: str, success: bool,
                      refused_reason: str | None = None) -> None:
    """Close out a claimed item.

    success=True         → executed, reported to the owner as applied.
    refused_reason set   → terminal, NOT retried, NOT reported as applied.
    otherwise            → release the claim so the next cycle retries.
    """
    def _apply(state: dict) -> dict:
        q = list(state.get(QUEUE_KEY, []))
        for x in q:
            if x.get("id") == item_id:
                x.pop("executing_at", None)
                if success:
                    x["executed"] = True
                    x["executed_at"] = _now()
                elif refused_reason:
                    # Terminal: leave executed falsy so nothing claims it worked,
                    # but stamp it so apply_approved stops picking it up.
                    x["refused"] = True
                    x["refused_at"] = _now()
                    x["refused_reason"] = refused_reason
                    x["status"] = "refused"
        return {QUEUE_KEY: q}

    db.atomic_state(_apply)


def apply_approved() -> list[dict]:
    """Execute approved-but-unexecuted items. Called from the scan loop, the
    5-min Telegram sync, and the web approve endpoint — the per-item atomic
    claim guarantees only ONE of them executes each item (2026-07-07 fix).
    Returns the items that were applied (for a Telegram confirmation)."""
    applied: list[dict] = []
    todo = [a for a in list_all()
            if a.get("status") == "approved"
            and not a.get("executed") and not a.get("refused")]
    for a in todo:
        if not _claim_for_execution(a["id"]):
            continue   # another process owns it (or it just got executed)
        try:
            _execute(a)
        except ExecutionRefused as e:
            # Permanent: don't retry, don't count as applied, and say so out
            # loud — an approved item quietly going nowhere is the worst of the
            # three outcomes for the owner.
            log.warning("approved item %s REFUSED: %s", a.get("id"), e)
            _finish_execution(a["id"], success=False, refused_reason=str(e))
            try:
                from . import notifier
                notifier.send(
                    f"🚫 你批准的建议未能执行\n"
                    f"  {a.get('detail', a.get('action', a['id']))}\n"
                    f"  原因: {e}"
                )
            except Exception as ne:
                log.debug("refusal notify failed: %s", ne)
            continue
        except Exception as e:
            log.warning("apply approved %s failed: %s", a.get("id"), e)
            _finish_execution(a["id"], success=False)   # release → retry next cycle
            continue
        _finish_execution(a["id"], success=True)
        applied.append(a)
    return applied


def _execute(item: dict) -> None:
    """Perform the real mutation for an approved executable item. Informational
    kinds are no-ops (approval = acknowledgement)."""
    kind = item.get("kind")
    payload = item.get("payload", {})
    if kind == "blacklist_review":
        sym = payload.get("symbol")
        if sym:
            from . import blacklist
            blacklist.add(sym, reason="weekly self-review (owner-approved)")
            log.info("approval applied: blacklisted %s", sym)
    elif kind == "param_change":
        # {"key": ..., "value": ...} → runtime db-state (NOT .env), effective
        # next scan. 2026-06-11: routed through runtime_config.set_param so the
        # change lands in param_history (which powers the autopilot rollback)
        # and re-validates bounds at execution time.
        key = payload.get("key")
        value = payload.get("value")
        if key is not None:
            from . import runtime_config
            try:
                runtime_config.set_param(key, value, source="owner-approved")
                log.info("approval applied: param %s = %s", key, value)
            except runtime_config.ParamsFrozen as e:
                # Terminal while the freeze is on. Retrying every scan would
                # never succeed, and swallowing it told the owner it worked.
                raise ExecutionRefused(str(e)) from e
            except ValueError as e:
                # Out of ALLOWED_PARAMS bounds — also permanent for this
                # payload; the value itself is the problem.
                raise ExecutionRefused(
                    f"param {key}={value} rejected: {e}") from e
    elif kind == "manual_takeover_sell":
        # Owner approved the bot taking over a HIGH-risk MANUAL position: market-
        # sell it now to cut the loss. Opens its own broker client (apply_approved
        # runs both in-scan and off-hours). close_position cancels any legs, sells
        # at a fresh price, logs the close + removes it from open_trades. If it
        # raises (market shut / halted price), apply_approved leaves the item
        # un-executed and retries next cycle — i.e. it lands at the next open.
        sym = payload.get("symbol")
        if sym:
            from . import executor, notifier
            # Idempotent: if you already sold it yourself before tapping ✅, the
            # GHOST booker has recorded it and it's no longer tracked — nothing to
            # do (and don't let close_position raise → infinite retry).
            if sym not in executor._load_open_trades():
                notifier.send(f"ℹ️ {sym} 已不在持仓（可能你已手动卖出），无需接管。")
                log.info("manual takeover sell %s: already flat — skip", sym)
                return
            from .moo_client import client as _broker
            with _broker() as c:
                action = executor.close_position(c, sym, reason="MANUAL_TAKEOVER")
            log.info("approval applied: manual takeover sell %s → %s", sym, action)
            notifier.send(f"✅ 已接管并卖出手动持仓 {sym}（你已批准止损）")
    # strategy_flag / exit_balance: informational — nothing to execute.


def purge_resolved(keep_recent: int = 50) -> None:
    """Trim executed/rejected items so the queue doesn't grow unbounded."""
    def _apply(state: dict) -> dict:
        q = list(state.get(QUEUE_KEY, []))
        pending = [a for a in q if a.get("status") == "pending"]
        done = [a for a in q if a.get("status") != "pending"][-keep_recent:]
        return {QUEUE_KEY: pending + done}

    db.atomic_state(_apply)
