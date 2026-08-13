"""Which broker account this process may touch, decided once and then fixed.

THE TWO PROBLEMS THIS EXISTS FOR

1. The environment was resolved by this expression, on every call:

       TrdEnv.SIMULATE if settings.moo_trade_env == "SIMULATE" else TrdEnv.REAL

   Every value that is not exactly the string "SIMULATE" selects REAL. A typo,
   a lowercase "simulate", an empty string from an unreadable .env, a trailing
   space — all of them route orders to real money. The safe direction is the
   default, so parse_trade_env() accepts a whitelist and refuses everything
   else rather than falling through.

2. Not one of the fourteen broker calls passed acc_id. The SDK fills that in
   with _get_default_acc_id(), which returns the FIRST account in the list
   whose trd_env matches — in whatever order the broker sent them. With one
   account per environment that happens to be right. With a cash and a margin
   account, or any sub-account, it is a coin flip made once per connection and
   never mentioned. Nothing in a log or a fill would say which account traded.

So a binding is resolved once, checked against what the session already
committed to, and then passed explicitly to every query, order and cancel.

WHAT IS DELIBERATELY NOT HERE
  unlock_trade(). Unlocking is a gateway-global state on REAL accounts, and
  calling it automatically at connection time means a process that only meant
  to read quotes has armed order placement for everything sharing that OpenD.
  Per the broker's own documentation SIMULATE has no unlock concept at all, so
  on SIMULATE the call was pure side effect. Arming REAL is an explicit,
  per-start act; it does not belong in a lazily-opened property.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from moomoo import RET_OK, TrdEnv

from .config import settings

log = logging.getLogger(__name__)

VALID_ENVS = ("SIMULATE", "REAL")


class BrokerBindingRefused(Exception):
    """The broker's answer did not match what this run committed to."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def parse_trade_env(raw: object, *, where: str = "configuration") -> str:
    """A trade environment, or an exception. Never a guess.

    Note the asymmetry: an unrecognised value is not quietly treated as
    SIMULATE either. Silently downgrading would be safe for the account and
    dishonest about the configuration — the operator asked for something this
    code does not understand, and continuing under a different environment than
    the one they wrote is how a REAL run turns out to have been paper all along.
    """
    name = str(raw or "").strip().upper()
    if name not in VALID_ENVS:
        raise BrokerBindingRefused(
            "bad_trade_env",
            f"{where} gives trade_env {raw!r}, which is not one of "
            f"{', '.join(VALID_ENVS)}. Refusing rather than defaulting — the "
            f"old expression treated every unrecognised value as REAL.")
    return name


def trd_env_enum(name: str) -> TrdEnv:
    return TrdEnv.SIMULATE if parse_trade_env(name) == "SIMULATE" else TrdEnv.REAL


@dataclass(frozen=True)
class TradeBinding:
    """One broker account, pinned for the life of the process."""
    trade_env: str
    acc_id: int
    security_firm: str
    acc_type: str
    acc_status: str
    firm_verified: bool
    account_ref: str
    resolved_at: float

    @property
    def trd_env(self) -> TrdEnv:
        return trd_env_enum(self.trade_env)

    def describe(self) -> str:
        """Safe to log: the account is an HMAC reference, never the number."""
        firm = self.security_firm if self.firm_verified else \
            f"{self.security_firm or 'unreported'}, firm unverified"
        return f"{self.trade_env} {self.account_ref} ({firm}/{self.acc_type})"


_bound: TradeBinding | None = None
_lock = threading.Lock()


def current() -> TradeBinding | None:
    return _bound


def require(what: str) -> TradeBinding:
    b = _bound
    if b is None:
        raise BrokerBindingRefused(
            "not_bound",
            f"{what} was attempted before the broker account was resolved. "
            f"Bind first — an unbound call lets the SDK choose an account.")
    return b


def reset() -> None:
    """Tests, and Stop. A new run resolves its own binding."""
    global _bound
    with _lock:
        _bound = None


def resolve(trade_ctx, *, trade_env: str, expected_acc_id: str | int | None = None,
            security_firm: str | None = None) -> TradeBinding:
    """Ask the broker which accounts exist and pin exactly one of them.

    Refuses on anything ambiguous. "Exactly one match" is the whole point: if
    two accounts fit the description, the SDK's answer would have been the
    first one, and this refuses instead of reproducing that choice with a
    friendlier name.
    """
    env = parse_trade_env(trade_env, where="the session")
    want_firm = (security_firm or settings.moo_security_firm or "").strip().upper()

    ret, rows = trade_ctx.get_acc_list()
    if ret != RET_OK:
        raise BrokerBindingRefused("acc_list_failed", str(rows)[:200])
    if not isinstance(rows, list):
        rows = rows.to_dict("records")

    def firm_of(r):
        return str(r.get("security_firm") or "").strip().upper()

    same_env = [r for r in rows if str(r.get("trd_env", "")).upper() == env]
    if not same_env:
        raise BrokerBindingRefused(
            "no_account_for_env",
            f"OpenD reports no {env} account. This run committed to {env}; it "
            f"will not fall back to whatever else is available.")

    # The security firm, when the broker reports one.
    #
    # It often does not. This paper account comes back with security_firm='N/A'
    # — the SDK's stand-in for a field the gateway left unset — and an equality
    # check against the configured FUTUMY refused every start against a real
    # OpenD while passing every test written with a fake context.
    #
    # Treating "not reported" as a mismatch is wrong, and so is treating it as a
    # match. What makes the firm safe here is that it is an INPUT, not a filter:
    # OpenSecTradeContext is constructed with it, and this account list is that
    # context's answer. A connection for the wrong firm returns a different list
    # rather than a mislabelled one. So an absent field is recorded as
    # unverified and allowed; a field that is present and different is refused,
    # because then the broker is actively contradicting us.
    _UNREPORTED = {"", "N/A", "NONE", "NA"}
    reported = [r for r in same_env if firm_of(r) not in _UNREPORTED]
    if reported:
        candidates = [r for r in reported if firm_of(r) == want_firm]
        if not candidates:
            raise BrokerBindingRefused(
                "security_firm_mismatch",
                f"no {env} account under security firm {want_firm!r} "
                f"(OpenD reports {sorted({firm_of(r) for r in reported})})")
    else:
        candidates = list(same_env)
        log.info("OpenD did not report a security firm for the %s account; "
                 "relying on the connection, which was opened as %s",
                 env, want_firm)

    # An explicitly remembered account wins over "the only one", so that a
    # second account appearing later cannot silently move the run.
    if expected_acc_id not in (None, "", 0, "0"):
        want = str(expected_acc_id)
        match = [r for r in candidates if str(r.get("acc_id")) == want]
        if not match:
            raise BrokerBindingRefused(
                "acc_id_mismatch",
                f"this account's records are bound to a broker account that "
                f"OpenD did not return for {env}. Refusing rather than trading "
                f"a different account against those records.")
        candidates = match
    elif len(candidates) != 1:
        raise BrokerBindingRefused(
            "ambiguous_account",
            f"{len(candidates)} {env} accounts under {want_firm} and nothing "
            f"recorded to say which one. Record the intended broker account "
            f"before starting; picking one here is what the SDK already did.")

    row = candidates[0]
    from . import account_ref as _aref
    binding = TradeBinding(
        trade_env=env,
        acc_id=int(row["acc_id"]),
        security_firm=firm_of(row),
        acc_type=str(row.get("acc_type") or ""),
        acc_status=str(row.get("acc_status") or ""),
        firm_verified=firm_of(row) not in _UNREPORTED,
        account_ref=_aref.ref(str(row["acc_id"]), kind="broker"),
        resolved_at=time.time(),
    )

    status = binding.acc_status.upper()
    if status and status not in ("ACTIVE", "N/A", "NONE"):
        raise BrokerBindingRefused(
            "account_not_active",
            f"the {env} account is in state {binding.acc_status!r}")
    return binding


def bind(binding: TradeBinding) -> TradeBinding:
    """Pin the binding for this process. Idempotent; conflicting rebinds refuse.

    A process trades one account. Allowing a rebind would mean orders placed
    before and after it went to different places while everything in between —
    the session, the ledger, the risk state — carried on as if they had not.
    """
    global _bound
    with _lock:
        if _bound is not None:
            if (_bound.acc_id, _bound.trade_env) != (binding.acc_id, binding.trade_env):
                raise BrokerBindingRefused(
                    "rebind_refused",
                    f"already bound to {_bound.describe()}; refusing to rebind "
                    f"to {binding.describe()} in the same process")
            return _bound
        _bound = binding
    log.info("broker binding pinned: %s", binding.describe())
    return binding


def verify_against_session(binding: TradeBinding) -> None:
    """The broker's answer must match what the session already committed to.

    The session decided the environment before OpenD was ever contacted. If the
    two disagree, one of them is describing a different account than the other,
    and every record written this run would be filed under the wrong one.
    """
    try:
        from . import identity
        session = identity.current_session()
    except Exception:
        session = None
    if not session:
        return

    if binding.trade_env != session["trade_env"]:
        raise BrokerBindingRefused(
            "session_env_mismatch",
            f"session is {session['trade_env']}, broker binding resolved "
            f"{binding.trade_env}")

    try:
        from . import identity
        info = identity.account_info(session["account_id"]) or {}
    except Exception:
        info = {}
    known = info.get("broker_acc_id")
    if known and str(known) != str(binding.acc_id):
        raise BrokerBindingRefused(
            "account_record_mismatch",
            "this account's records were written against a different broker "
            "account than the one OpenD just returned")
