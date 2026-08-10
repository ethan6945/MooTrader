"""Who is trading, and under which run.

THE MODEL
  account          an internal uuid4 this software mints and never changes.
                   SIMULATE and REAL are ALWAYS two different accounts.
  broker_acc_id    moomoo's own id. Stored beside the account as a locator, not
                   as the identity — it is assigned by the broker, differs
                   between paper and live, and changes when the OpenD
                   environment is rebuilt. Keying our records on it would force
                   a re-migration every time that happened.
  execution_session  one Start→Stop of one real worker process. It binds the
                   worker (pid/host), the internal account, and a trade_env that
                   is immutable for the life of the session.

WHY trade_env IS PINNED TO THE SESSION
  "Which account am I trading?" must be decided once, at startup, and then be
  unable to change while orders are in flight. A setting read fresh on each use
  can flip between placing an order and reconciling its fill — and the two
  accounts hold different positions, so the reconciler would see a position it
  believes is a ghost and try to correct it. Recording the answer on the session
  makes the environment a fact about the run rather than a value someone can
  edit underneath it.

WHAT MAY AND MAY NOT SPAN SESSIONS
  Positions outlive the process that opened them — a restart must not orphan or
  duplicate a live position — so `open_trades.opened_session_id` is nullable and
  informational. Orders and fills may not: each one happened during exactly one
  run, and being unable to say which is how a duplicate order becomes
  unattributable. Rows that predate this model carry `migrated_from` instead of
  a session, so "before we tracked it" never has to be guessed from a bare NULL.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timezone

from . import db
from .config import settings

log = logging.getLogger(__name__)

# The session this process is running under. Process-local by design: a session
# IS a process, so there is nothing to share and nothing to persist here.
_session: dict | None = None
_lock = threading.Lock()

# Cache of the resolved account id. Cleared by tests and by set_active_account.
_account_cache: dict[str, str] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── accounts ────────────────────────────────────────────────────────────────

def get_or_create_account(trade_env: str, broker_acc_id: str | None = None,
                          label: str | None = None) -> str:
    """Return the internal uuid for (trade_env, broker_acc_id), creating it once.

    Matching prefers an exact (env, broker id) pair, then falls back to an
    account for that env whose broker id was never recorded — that is the row
    the v4 migration created, and adopting it keeps the migrated history
    attached to the account once the broker id is finally known.
    """
    env = (trade_env or "").upper()
    if env not in ("SIMULATE", "REAL"):
        raise ValueError(f"trade_env must be SIMULATE or REAL, got {trade_env!r}")

    with db.transaction() as c:
        row = c.execute(
            "SELECT account_id FROM accounts WHERE trade_env = ? "
            "AND broker_acc_id IS ?", (env, broker_acc_id)).fetchone()
        if row:
            return row["account_id"]

        if broker_acc_id is not None:
            row = c.execute(
                "SELECT account_id FROM accounts WHERE trade_env = ? "
                "AND broker_acc_id IS NULL ORDER BY created_at LIMIT 1",
                (env,)).fetchone()
            if row:
                c.execute("UPDATE accounts SET broker_acc_id = ? WHERE account_id = ?",
                          (broker_acc_id, row["account_id"]))
                log.info("identity: adopted %s account %s -> broker %s",
                         env, row["account_id"], broker_acc_id)
                return row["account_id"]

        account_id = str(uuid.uuid4())
        c.execute(
            "INSERT INTO accounts (account_id, trade_env, broker_acc_id, label, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, env, broker_acc_id, label or f"{env} account", _now()))
    log.info("identity: created %s account %s (broker=%s)",
             env, account_id, broker_acc_id)
    return account_id


def active_account_id() -> str | None:
    """The account this process reads and writes state for.

    Inside a session that is the session's account — pinned, so it cannot drift
    mid-run. Outside one (CLI tools, the web panel, tests) it resolves from the
    configured trade_env, creating the row on first use.
    """
    if _session:
        return _session["account_id"]

    env = (settings.moo_trade_env or "SIMULATE").upper()
    if env not in ("SIMULATE", "REAL"):
        env = "SIMULATE"
    hit = _account_cache.get(env)
    if hit:
        return hit
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT account_id FROM accounts WHERE trade_env = ? "
                "ORDER BY created_at LIMIT 1", (env,)).fetchone()
        account_id = row["account_id"] if row else get_or_create_account(env)
    except Exception as e:
        log.debug("identity: cannot resolve account yet (%s)", e)
        return None
    _account_cache[env] = account_id
    return account_id


def account_info(account_id: str | None = None) -> dict | None:
    account_id = account_id or active_account_id()
    if not account_id:
        return None
    with db.conn() as c:
        row = c.execute("SELECT * FROM accounts WHERE account_id = ?",
                        (account_id,)).fetchone()
    return dict(row) if row else None


def reset_cache() -> None:
    """Forget the resolved account (tests, and after changing trade_env)."""
    _account_cache.clear()


# ── execution sessions ──────────────────────────────────────────────────────

def start_session(trade_env: str, broker_acc_id: str | None = None,
                  auth_mode: str = "unknown",
                  authorized_by: str | None = None) -> dict:
    """Open a session for this process and pin its account + trade_env.

    `auth_mode` records HOW the environment was decided — the OpenD startup
    check (Phase 0B-2) is what will pass something other than "unknown". It is
    stored rather than recomputed so that "why was this run allowed to touch
    real money?" is answerable afterwards from the row itself.
    """
    global _session
    env = (trade_env or "").upper()
    if env not in ("SIMULATE", "REAL"):
        raise ValueError(f"trade_env must be SIMULATE or REAL, got {trade_env!r}")

    with _lock:
        if _session is not None:
            raise RuntimeError(
                f"session {_session['session_id']} is already open in this "
                f"process — a worker runs exactly one session")

        account_id = get_or_create_account(env, broker_acc_id)
        session_id = str(uuid.uuid4())
        rec = {
            "session_id": session_id, "account_id": account_id,
            "trade_env": env, "pid": os.getpid(), "host": socket.gethostname(),
            "started_at": _now(), "auth_mode": auth_mode,
            "authorized_at": _now() if env == "REAL" else None,
            "authorized_by": authorized_by,
        }
        with db.transaction() as c:
            c.execute(
                "INSERT INTO execution_sessions (session_id, account_id, "
                "trade_env, pid, host, started_at, auth_mode, authorized_at, "
                "authorized_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (rec["session_id"], rec["account_id"], rec["trade_env"],
                 rec["pid"], rec["host"], rec["started_at"], rec["auth_mode"],
                 rec["authorized_at"], rec["authorized_by"]))
        _session = rec
    log.info("identity: session %s open — %s account %s pid %s (auth=%s)",
             session_id[:8], env, account_id[:8], rec["pid"], auth_mode)
    return dict(rec)


def end_session(reason: str = "stopped") -> None:
    global _session
    with _lock:
        if _session is None:
            return
        sid = _session["session_id"]
        try:
            with db.transaction() as c:
                c.execute("UPDATE execution_sessions SET ended_at = ?, "
                          "end_reason = ? WHERE session_id = ?",
                          (_now(), reason, sid))
        except Exception as e:
            log.warning("identity: failed to close session %s: %s", sid, e)
        _session = None
    log.info("identity: session %s closed (%s)", sid[:8], reason)


def current_session() -> dict | None:
    return dict(_session) if _session else None


def current_session_id() -> str | None:
    return _session["session_id"] if _session else None


def current_trade_env() -> str:
    """The pinned environment for this run, else the configured one.

    Read this instead of settings.moo_trade_env anywhere a decision depends on
    which account is being traded — inside a session it cannot change, which is
    the entire point of the session.
    """
    if _session:
        return _session["trade_env"]
    env = (settings.moo_trade_env or "SIMULATE").upper()
    return env if env in ("SIMULATE", "REAL") else "SIMULATE"


def require_session_id() -> str:
    """Session id for a record that MUST have one (an order, a fill).

    Raises rather than writing NULL: a NULL session on a fill is indistinguishable
    from migrated history, and that ambiguity is exactly what makes a duplicate
    order unattributable later.
    """
    sid = current_session_id()
    if not sid:
        raise RuntimeError(
            "no execution session — orders and fills must be attributable to "
            "one. Call identity.start_session() during startup.")
    return sid


def open_sessions() -> list[dict]:
    """Sessions with no ended_at. More than one means a second worker is live,
    or a previous one died without closing its row."""
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM execution_sessions WHERE ended_at IS NULL "
            "ORDER BY started_at").fetchall()]
