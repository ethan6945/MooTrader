"""Whether this process may send an order at all. Separate from whether it may run.

WHY THIS IS NOT A SETTING

  The instinct is `NO_ORDERS=true` in .env. That is the shape of thing that
  already failed here: AUTO_APPLY_PARAMS and AUTO_BUDGET_ENABLED were both off
  and optimize_system._inject() went on writing production parameters anyway,
  because it did not go through the code the switches guarded. A switch is only
  worth what its narrowest choke point is worth.

  So this is not read from configuration and not consulted per call site. It is
  a process-wide capability that:

    - starts DENIED, so code that never grants it cannot place an order;
    - is granted once, by the parent's GO, and not by anything in this process;
    - can be revoked but never re-granted, so the only direction it moves is
      toward safety;
    - is enforced at the same three methods as the lease check — the ones that
      change broker state — rather than at the eighteen places that call them.

  The absence of a grant is a denial. A missing variable, a mangled GO, a code
  path nobody thought about: all of them arrive here with the gate still shut.

RUNNING VS TRADING
  These are two different permissions and the parent issues them separately. A
  staging worker is meant to start, connect, read positions, score candidates
  and manage state — the whole run, minus the part that reaches the broker's
  order book. Under one permission that is not expressible, and "staging" ends
  up meaning "production against a paper account", which is what makes staging
  orders land in the same account the authoritative ledger tracks.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class OrdersNotPermitted(Exception):
    """This process was not cleared to place, modify or cancel orders."""


_permitted = False
_reason = "no grant has been issued"
_frozen = False           # set once denied explicitly; blocks any later grant
_lock = threading.Lock()


def permit(reason: str) -> None:
    """Grant order capability. Refused if it was ever explicitly denied.

    Idempotent for repeat grants, so a retry or a second code path arriving at
    the same conclusion is not an error. What it will not do is reopen a gate
    that was shut on purpose: a staging run that could talk itself back into
    trading is not a staging run.
    """
    global _permitted, _reason
    with _lock:
        if _frozen:
            raise OrdersNotPermitted(
                f"orders were explicitly denied for this run ({_reason}); "
                f"they cannot be re-enabled without restarting")
        if not _permitted:
            _permitted = True
            _reason = reason
            log.info("order gate OPEN — %s", reason)


def deny(reason: str) -> None:
    """Shut the gate for the life of the process. Always allowed."""
    global _permitted, _reason, _frozen
    with _lock:
        _permitted = False
        _frozen = True
        _reason = reason
    log.warning("order gate SHUT — %s", reason)


def permitted() -> bool:
    return _permitted


def describe() -> str:
    return ("orders permitted: " + _reason) if _permitted else \
           ("orders NOT permitted: " + _reason)


def require(what: str) -> None:
    """Raise unless this process may change broker state."""
    if not _permitted:
        raise OrdersNotPermitted(
            f"{what} refused — {describe()}. This process is running without "
            f"order capability; nothing it does may reach the broker's order "
            f"book.")


def reset_for_tests() -> None:
    global _permitted, _reason, _frozen
    with _lock:
        _permitted = False
        _frozen = False
        _reason = "no grant has been issued"
