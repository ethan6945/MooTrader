"""The only way the trading worker starts.

    prepare(source)  → validate everything; returns a request or a refusal
    commit(request)  → take the lease, spawn with a rebuilt environment,
                       wait for the worker to report READY
    worker_ready()   → called by the worker once it has verified its own world

Every direct path is gone: no bare Popen, no restart-that-reuses-the-old-env, no
"already running so return ok". A start either goes through all three stages or
does not happen.

WHY A REBUILT ENVIRONMENT, NOT AN INHERITED ONE
  The old spawn passed no `env=` at all, so the worker inherited whatever the
  parent happened to hold. On 2026-08-11 the parent was a web server launched
  the previous day, carrying MAX_POSITION_PCT=0.40, AUTO_APPLY_PARAMS=true and
  AUTO_BUDGET_ENABLED=true — values corrected on disk hours earlier. The worker
  traded on the parent's memory of a configuration that no longer existed.

  So the child environment is CONSTRUCTED: every application variable is
  stripped, MMT_HOME is pinned explicitly, and the worker reads the authoritative
  .env from disk. Parent and child then compare a hash of what each resolved. A
  mismatch means one of them is reading something the other is not, and neither
  is trusted to guess which.

  MMT_ALLOW_SCHEMA_UPGRADE is removed unconditionally. A trading worker must
  never migrate a database as a side effect of starting; that is a separate,
  deliberate step.

REAL IS REFUSED IN THIS PHASE
  moomoo exposes no reliable read-only way to ask whether trading is unlocked —
  only an unlock call that either works or does not. With no way to *know*, the
  honest options are "assume" and "refuse". This refuses. effective_env is
  SIMULATE, and a REAL request is turned away with a code, not silently
  downgraded, because silently downgrading is how someone ends up believing they
  are live when they are not.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db, start_audit, start_lease
from .config import settings

log = logging.getLogger(__name__)

READY_TIMEOUT_S = 60.0
# How long a worker waits to be accepted after reporting READY. Short: the
# parent's checks are four comparisons against values it already holds, so
# anything slower than this means the parent is gone or wedged, and a worker
# waiting on a parent that will never answer must not sit there holding a lease.
GO_TIMEOUT_S = 30.0
PHASE_ALLOWS_REAL = False        # flipped only when OpenD authorization lands

# Application variables the child must NOT inherit. It reads them from the
# authoritative .env instead, so a parent holding a stale copy cannot leak it in.
_APP_ENV_PREFIXES = ("MOO_", "MOOMOO_", "OPEND_", "TELEGRAM_", "GEMINI_",
                     "DEEPSEEK_", "TAVILY_", "FINNHUB_", "WEB_", "AI_",
                     "AUTO_", "MAX_", "MIN_", "ENTRY_", "TP_", "SL_", "DD_",
                     "RISK_", "SCAN_", "UNIVERSE_", "NEWS_", "PATTERN_",
                     "SENTIMENT_", "OPTIONS_", "SMART_", "STALL_", "BREADTH_",
                     "CASH_", "INVERSE_", "REGIME_", "GAP_", "HEALTH_",
                     "FINBERT_", "PARAMS_", "USE_", "TIMEFRAME", "ACCOUNT_",
                     "SLOT_", "DAILY_", "MMT_", "STRATEGY_", "MR_", "LIVE_")
# Variables that exist only for tests and migrations. A worker inheriting one is
# a worker running with a safety rail removed.
_DANGEROUS_ENV = ("MMT_ALLOW_SCHEMA_UPGRADE", "MMT_HOME", "PYTEST_CURRENT_TEST",
                  "MMT_TEST_MODE", "MMT_STAGING")

# Non-sensitive settings whose values define how much can be lost. Hashed into
# the config fingerprint and recorded in the audit in the clear.
SAFETY_KEYS = ("MOO_TRADE_ENV", "MAX_POSITION_PCT", "MAX_POSITIONS",
               "RISK_PER_TRADE", "DAILY_DRAWDOWN_STOP", "DD_HALT_PCT",
               "PARAMS_FROZEN", "AUTO_APPLY_PARAMS", "AUTO_BUDGET_ENABLED",
               "MAX_POSITIONS_AUTOSCALE", "ENTRY_SCORE_THRESHOLD",
               "SL_ATR_MULT", "TP_ATR_MULT", "MAX_HOLD_DAYS")


class StartRefused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class StartRequest:
    request_id: str
    source: str
    effective_env: str
    config_sha256: str
    safety: dict
    db_version: int
    db_integrity: str
    account_ref: str | None
    build_id: str
    executable: str
    executable_sha256: str
    # The internal account this start was validated against. Handed to the
    # worker so it binds to this one rather than resolving its own — otherwise
    # it can create a second account row and file the run under a record the
    # parent never saw. account_ref above is the HMAC, safe to log; this is the
    # raw id, and it goes to the child environment, never to the audit.
    account_id: str | None = None
    # Whether this run may reach the broker's order book. Separate from whether
    # it may run: a staging worker starts, connects, reads positions and scores
    # candidates, and places nothing. Under one permission that is not
    # expressible, and staging becomes production against a paper account —
    # whose fills land in the same broker account the authoritative ledger
    # tracks, invisibly to it.
    allow_orders: bool = True
    prepared_at: float = field(default_factory=time.time)


# ── configuration fingerprint ────────────────────────────────────────────────

def env_file_path() -> Path:
    return settings.root / ".env"


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse the authoritative .env, refusing anything ambiguous.

    A duplicate key is not a warning: two lines disagreeing about
    MAX_POSITION_PCT means the effective value depends on parser order, and the
    parent and the child may not agree on what that is.
    """
    p = path or env_file_path()
    if not p.exists():
        raise StartRefused("config_missing", f"no .env at {p}")
    try:
        text = p.read_text()
    except OSError as e:
        raise StartRefused("config_unreadable", f"cannot read {p}: {e}") from e

    out: dict[str, str] = {}
    dupes: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            raise StartRefused("config_malformed",
                               f"{p.name} line {lineno} has no '='")
        k, _, v = s.partition("=")
        k = k.strip()
        if not k:
            raise StartRefused("config_malformed",
                               f"{p.name} line {lineno} has an empty key")
        if k in out:
            dupes.append(k)
        out[k] = v.split("#")[0].strip()
    if dupes:
        raise StartRefused("config_duplicate_keys",
                           f"{p.name} defines {sorted(set(dupes))} more than once")
    return out


def config_fingerprint(env: dict[str, str]) -> tuple[str, dict]:
    """(sha256, safety values). Covers only the safety-relevant keys.

    Hashing the whole file would make the parent/child comparison fail on a
    comment edit, and a check that fails for harmless reasons gets bypassed.
    A missing key is hashed as an explicit absent marker, so deleting a line is
    a different fingerprint from setting it — that is exactly the case where a
    child must not fall back to an inherited value.
    """
    safety = {k: env.get(k, "<absent>") for k in SAFETY_KEYS}
    blob = json.dumps(safety, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest(), safety


def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    try:
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return "unavailable"
    return h.hexdigest()


def build_id() -> str:
    """Something that identifies this exact code. Frozen builds carry a version;
    a source checkout is identified by its commit."""
    frozen = getattr(sys, "frozen", False)
    if frozen:
        return f"frozen:{os.environ.get('MMT_BUILD_ID', 'unknown')}"
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=str(Path(__file__).resolve().parent.parent),
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return f"src:{r.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        pass
    return "src:unknown"


# ── child environment ────────────────────────────────────────────────────────

def build_child_env(home: Path, config_sha: str) -> dict[str, str]:
    """A constructed environment, not an inherited one.

    Keeps only what a process needs to exist — PATH, HOME, locale, TMPDIR — and
    adds back exactly the handful of variables the worker is meant to receive.
    Everything application-shaped is dropped so the worker reads it from disk.
    """
    keep = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TZ",
            "LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "PYTHONPATH")
    env = {k: v for k, v in os.environ.items() if k in keep}

    for name in _DANGEROUS_ENV:
        env.pop(name, None)
    for k in list(env):
        if any(k.startswith(pre) for pre in _APP_ENV_PREFIXES):
            env.pop(k, None)

    env["MMT_HOME"] = str(home)                 # pinned, never inherited
    env["MMT_EXPECTED_CONFIG_SHA"] = config_sha  # the child verifies against this
    env["MMT_STARTED_BY_PROTOCOL"] = "1"        # the worker refuses without this
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ── prepare ──────────────────────────────────────────────────────────────────

def prepare(source: str, *, requested_env: str | None = None,
            home: Path | None = None, allow_orders: bool = True) -> StartRequest:
    """Validate everything that must be true before a worker may exist.

    Refuses — with a stable code — on: an unknown source, a REAL request, a
    database older than this build, a failed integrity check, an unknown
    account, a database/mirror disagreement about positions, and a malformed or
    ambiguous configuration.
    """
    t0 = time.time()
    request_id = str(uuid.uuid4())
    home = home or settings.root

    def refuse(code: str, detail: str):
        start_audit.record(request_id=request_id, event="prepare",
                           result="refused", source=source if source in
                           start_audit.SOURCES else "cli",
                           refusal_code=code, refusal_detail=detail,
                           duration_ms=int((time.time() - t0) * 1000))
        raise StartRefused(code, detail)

    if source not in start_audit.SOURCES:
        # Cannot audit an unknown source under its own name; record it as cli.
        raise StartRefused("bad_source",
                           f"source must be one of {sorted(start_audit.SOURCES)}")

    env = read_env_file(home / ".env")
    config_sha, safety = config_fingerprint(env)

    effective = (requested_env or env.get("MOO_TRADE_ENV") or "SIMULATE").upper()
    if effective == "REAL" and not PHASE_ALLOWS_REAL:
        refuse("real_not_permitted",
               "REAL is refused in this phase: OpenD offers no reliable "
               "read-only unlock status, so the environment cannot be verified, "
               "and a start that cannot be verified is not downgraded silently")
    if effective != "SIMULATE":
        refuse("bad_trade_env", f"effective_env must be SIMULATE, got {effective}")

    # ── database ──
    from . import db as _db
    db_path = home / "data" / "trader.db"
    if not db_path.exists():
        refuse("db_missing", f"no database at {db_path}")
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        db_version = c.execute("PRAGMA user_version").fetchone()[0]
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as e:
        refuse("db_unreadable", f"cannot open the database: {type(e).__name__}")
    if integrity != "ok":
        c.close()
        refuse("db_integrity", f"integrity_check returned {integrity!r}")
    if db_version < _db.SCHEMA_VERSION:
        c.close()
        refuse("db_schema_old",
               f"database is v{db_version}, this build expects "
               f"v{_db.SCHEMA_VERSION}. Migrate deliberately; a trading worker "
               f"does not upgrade a schema on the way up")
    if db_version > _db.SCHEMA_VERSION:
        c.close()
        refuse("db_schema_new",
               f"database is v{db_version}, newer than this build "
               f"(v{_db.SCHEMA_VERSION})")

    # ── account ──
    account_ref = None
    try:
        rows = c.execute("SELECT account_id, trade_env, broker_acc_id "
                         "FROM accounts WHERE trade_env = ?",
                         (effective,)).fetchall()
    except sqlite3.Error:
        rows = []
    if len(rows) != 1:
        c.close()
        refuse("account_unknown",
               f"expected exactly one {effective} account, found {len(rows)}")
    from . import account_ref as _aref
    account_id = rows[0]["account_id"]
    account_ref = _aref.ref(account_id, kind="account")

    # ── positions: database vs mirror ──
    # Scoped to the account we are about to start, not the whole table. Reading
    # every account's positions here would compare a two-account union against a
    # one-account mirror, so the first REAL start would refuse with
    # position_conflict every single time — a check that fires only once a
    # second account exists, which is the moment it is hardest to read.
    db_positions = {r["symbol"] for r in c.execute(
        "SELECT symbol FROM open_trades WHERE account_id = ?", (account_id,))}
    c.close()
    mirror = home / "data" / "open_trades.json"
    if mirror.exists():
        try:
            raw = json.loads(mirror.read_text()) or {}
        except (json.JSONDecodeError, OSError) as e:
            refuse("mirror_unreadable", f"cannot read open_trades.json: {e}")
        # There is one mirror file for the whole installation, so it may have
        # been written by the other account. Comparing this account's positions
        # against that file is comparing two different things and calling the
        # difference a conflict — and the operator's fix for a bogus conflict
        # is to overwrite the mirror, which discards the other account's record.
        mirror_account = raw.get("_account_id") if isinstance(raw, dict) else None
        if mirror_account and mirror_account != account_id:
            refuse("mirror_other_account",
                   "data/open_trades.json was written by a different account. "
                   "Rebuild it for this one rather than reconciling against it")
        mirror_positions = {k for k in raw if not k.startswith("_")}
        if mirror_positions != db_positions:
            refuse("position_conflict",
                   f"database holds {sorted(db_positions) or 'nothing'} but the "
                   f"JSON mirror holds {sorted(mirror_positions) or 'nothing'} — "
                   f"rebuild the mirror before starting")

    exe = Path(sys.executable)
    req = StartRequest(
        request_id=request_id, source=source, effective_env=effective,
        config_sha256=config_sha, safety=safety,
        db_version=db_version, db_integrity=integrity, account_ref=account_ref,
        account_id=account_id, allow_orders=allow_orders,
        build_id=build_id(), executable=str(exe),
        executable_sha256=_file_sha256(exe),
    )
    start_audit.record(
        request_id=request_id, event="prepare", result="prepared", source=source,
        effective_env=effective, config_sha256=config_sha, safety=safety,
        config_path=str(home / ".env"), db_path=str(db_path),
        db_version=db_version, db_integrity=integrity, account_ref=account_ref,
        build_id=req.build_id, executable=req.executable,
        executable_sha256=req.executable_sha256,
        duration_ms=int((time.time() - t0) * 1000))
    return req


# ── commit ───────────────────────────────────────────────────────────────────

def _ready_path(home: Path, request_id: str) -> Path:
    return home / "logs" / f"worker-ready-{request_id}.json"


def _go_path(home: Path, request_id: str) -> Path:
    return home / "logs" / f"worker-go-{request_id}.json"


def commit(req: StartRequest, *, home: Path | None = None,
           worker_cmd: list[str] | None = None,
           timeout: float = READY_TIMEOUT_S) -> dict:
    """Take the lease, spawn the worker, and wait for it to report READY.

    Returns only after the worker has confirmed its own pid, session, config
    hash and safety checks. A worker that does not report in time is killed and
    the lease released — a process that could not say it was healthy is not left
    running on the assumption that it probably is.
    """
    t0 = time.time()
    home = home or settings.root
    if time.time() - req.prepared_at > 300:
        raise StartRefused("request_expired",
                           "this request was prepared more than 5 minutes ago; "
                           "prepare again so the checks reflect current state")

    try:
        lease = start_lease.acquire(purpose="worker")
    except start_lease.LeaseUnavailable as e:
        start_audit.record(request_id=req.request_id, event="commit",
                           result="refused", source=req.source,
                           refusal_code="lease_unavailable",
                           refusal_detail=str(e),
                           duration_ms=int((time.time() - t0) * 1000))
        raise StartRefused("lease_unavailable", str(e)) from e

    ready_file = _ready_path(home, req.request_id)
    go_file = _go_path(home, req.request_id)
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    ready_file.unlink(missing_ok=True)
    go_file.unlink(missing_ok=True)

    env = build_child_env(home, req.config_sha256)
    env["MMT_START_REQUEST_ID"] = req.request_id
    env["MMT_START_FENCE"] = str(lease.fence)
    env["MMT_READY_FILE"] = str(ready_file)
    env["MMT_GO_FILE"] = str(go_file)
    # The account the parent validated. The worker binds to this rather than
    # resolving one of its own, so it cannot mint a second account row and file
    # this run under a record nothing checked.
    if req.account_id:
        env["MMT_ACCOUNT_ID"] = req.account_id

    cmd = worker_cmd or [sys.executable, "-m", "src.main", "run"]
    proc = None
    try:
        logf = (home / "logs" / "scheduler.log").open("a")
        proc = subprocess.Popen(cmd, cwd=str(home), env=env, stdout=logf,
                                stderr=logf, start_new_session=True)

        deadline = time.time() + timeout
        ready = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise StartRefused(
                    "worker_exited",
                    f"the worker exited with code {proc.returncode} before "
                    f"reporting ready")
            if ready_file.exists():
                try:
                    ready = json.loads(ready_file.read_text())
                    break
                except json.JSONDecodeError:
                    pass          # still being written
            time.sleep(0.2)

        if ready is None:
            raise StartRefused("ready_timeout",
                               f"no READY within {timeout:g}s")

        # The worker's own account of itself must match what we spawned.
        if int(ready.get("pid", -1)) != proc.pid:
            raise StartRefused("ready_pid_mismatch",
                               f"READY came from pid {ready.get('pid')}, "
                               f"we spawned {proc.pid}")
        if ready.get("config_sha256") != req.config_sha256:
            raise StartRefused(
                "config_mismatch",
                "parent and worker resolved different configurations — one is "
                "reading something the other is not")
        if int(ready.get("fence", -1)) != lease.fence:
            raise StartRefused("fence_mismatch",
                               "the worker holds a different fencing token")
        if ready.get("effective_env") != req.effective_env:
            raise StartRefused("env_mismatch",
                               f"worker reports {ready.get('effective_env')}, "
                               f"expected {req.effective_env}")
        if req.account_id and ready.get("account_id") != req.account_id:
            raise StartRefused(
                "account_mismatch",
                "the worker bound a different account than the one validated")
        if int(ready.get("lease_pid", -1)) != proc.pid:
            raise StartRefused(
                "lease_not_transferred",
                "the worker did not take the lease into its own name — if this "
                "parent exits, the next start would find the lease stale and "
                "spawn a second worker beside this one")

        # Everything the worker could not check about itself now holds. Only
        # now is it allowed to trade: until the GO lands it is blocked inside
        # wait_for_go(), before protective exits and before any broker call.
        go = {"request_id": req.request_id, "fence": lease.fence,
              "worker_pid": proc.pid, "issued_at": time.time(),
              # Two grants, not one. "You may run" and "you may place orders"
              # are different sentences, and a staging run needs the first
              # without the second.
              "orders": bool(req.allow_orders)}
        tmp_go = go_file.with_suffix(".tmp")
        tmp_go.write_text(json.dumps(go))
        os.replace(tmp_go, go_file)

        start_audit.record(
            request_id=req.request_id, event="commit", result="ready",
            source=req.source, effective_env=req.effective_env,
            config_sha256=req.config_sha256, safety=req.safety,
            db_version=req.db_version, db_integrity=req.db_integrity,
            account_ref=req.account_ref, build_id=req.build_id,
            lease_holder=lease.holder_pid, fence_token=lease.fence,
            worker_pid=proc.pid, worker_host=socket.gethostname(),
            session_id=ready.get("session_id"),
            duration_ms=int((time.time() - t0) * 1000))
        return {"pid": proc.pid, "fence": lease.fence,
                "session_id": ready.get("session_id"),
                "request_id": req.request_id}

    except Exception as e:
        # Reclaim: a worker that could not confirm itself does not get to keep
        # running just because nobody stopped it.
        code = getattr(e, "code", "commit_error")
        detail = getattr(e, "detail", type(e).__name__)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), 15)
            except OSError:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        lease.release()
        ready_file.unlink(missing_ok=True)
        # Withdraw the clearance if it was already written. A worker being
        # reclaimed must not find a GO waiting for it.
        go_file.unlink(missing_ok=True)
        try:
            start_audit.record(request_id=req.request_id, event="commit",
                               result="reclaimed", source=req.source,
                               refusal_code=code, refusal_detail=detail,
                               worker_pid=(proc.pid if proc else None),
                               fence_token=lease.fence,
                               duration_ms=int((time.time() - t0) * 1000))
        except start_audit.AuditRefused:
            log.error("could not audit the reclaim of request %s", req.request_id)
        raise
    finally:
        # The READY file has been read by now, so removing it is safe. The GO
        # file is NOT removed here: the worker is still polling for it, and
        # deleting it on the way out would turn every successful start into a
        # go_timeout. The worker unlinks it once it has been accepted.
        ready_file.unlink(missing_ok=True)


# ── the one entry point ──────────────────────────────────────────────────────

def start(source: str, *, home: Path | None = None,
          worker_cmd: list[str] | None = None,
          allow_orders: bool = True) -> dict:
    """Start a worker. The only way to do so, for every caller.

    Web, the macOS shell and the CLI all land here, so there is exactly one
    place where a start can be refused, audited, or go wrong. The previous
    arrangement had the web server spawning its own process and the macOS app
    asking the web server to — which meant the rules lived in whichever caller
    happened to implement them, and the CLI implemented none.

    Raises StartRefused, whose `code` is what callers should surface. Every
    outcome, including every refusal, is already in the audit before this
    returns or raises.
    """
    req = prepare(source, home=home, allow_orders=allow_orders)
    return commit(req, home=home, worker_cmd=worker_cmd)


def stop(source: str = "cli", *, home: Path | None = None,
         timeout: float = 30.0) -> dict:
    """Stop the running worker, and close what it was holding.

    Three things end together or the next start is wrong: the process, the
    execution session, and the lease. Stopping the process alone leaves a
    session with no ended_at (so open_sessions() reports a worker that is not
    there) and a lease naming a dead pid (so the next start has to break it
    before it can proceed, and "breaking a lease" is a thing that should be
    rare enough to be alarming).
    """
    home = home or settings.root
    t0 = time.time()
    lease = start_lease.read() or {}
    pid = lease.get("pid")
    fence = lease.get("fence")

    stopped = False
    if isinstance(pid, int) and pid != os.getpid():
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            stopped = True
        except (OSError, ProcessLookupError):
            # Already gone. Not an error — but the lease and session it left
            # behind still have to be closed, which is the rest of this.
            log.info("stop: no live process for pid %s", pid)
        if stopped:
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.2)
            else:
                log.warning("stop: pid %s did not exit within %gs — SIGKILL",
                            pid, timeout)
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    pass

    # The worker closes its own session on a clean exit. This covers the rest:
    # a kill, a crash, or a worker that never got that far.
    closed = _close_orphan_sessions(pid)

    # Only remove a lease that belongs to the worker we just stopped. Deleting
    # one that has already moved on would hand a second worker's exclusion away.
    if isinstance(pid, int):
        cur = start_lease.read() or {}
        if cur.get("pid") == pid and cur.get("fence") == fence:
            try:
                start_lease._lease_path().unlink(missing_ok=True)
            except OSError as e:
                log.warning("stop: could not remove the lease: %s", e)

    try:
        start_audit.record(request_id=f"stop-{int(t0)}", event="stop",
                           result="committed", source=source,
                           worker_pid=pid if isinstance(pid, int) else None,
                           fence_token=fence if isinstance(fence, int) else None,
                           duration_ms=int((time.time() - t0) * 1000))
    except start_audit.AuditRefused as e:
        log.error("stop happened but could not be audited: %s", e)

    return {"stopped": stopped, "pid": pid, "sessions_closed": closed}


def _close_orphan_sessions(pid: int | None) -> int:
    """Close session rows left open by a worker that is no longer running."""
    from . import identity
    closed = 0
    try:
        for row in identity.open_sessions():
            row_pid = row.get("pid")
            alive = False
            if isinstance(row_pid, int):
                try:
                    os.kill(row_pid, 0)
                    alive = row_pid != pid
                except OSError:
                    alive = False
            if alive:
                continue
            with db.transaction() as c:
                c.execute("UPDATE execution_sessions SET ended_at = ?, "
                          "end_reason = ? WHERE session_id = ?",
                          (datetime.now(timezone.utc).isoformat(),
                           "stopped", row["session_id"]))
            closed += 1
    except Exception as e:
        log.warning("could not close orphaned sessions: %s", e)
    return closed


# ── worker side ──────────────────────────────────────────────────────────────

def worker_verify_and_report() -> dict:
    """Called by the worker once it is up. Verifies its own world, then reports.

    Refuses to run at all when it was not started through the protocol: that is
    what closes `python -m src.main run` typed by hand, a leftover launchd job,
    or a parent that still remembers how starts used to work.
    """
    if os.environ.get("MMT_STARTED_BY_PROTOCOL") != "1":
        raise StartRefused(
            "not_via_protocol",
            "the worker was launched outside the start protocol. Use the "
            "prepare/commit path; a direct launch skips the lease, the "
            "environment rebuild and the audit")

    request_id = os.environ.get("MMT_START_REQUEST_ID", "")
    ready_file = os.environ.get("MMT_READY_FILE", "")
    expected_sha = os.environ.get("MMT_EXPECTED_CONFIG_SHA", "")
    fence = int(os.environ.get("MMT_START_FENCE", "-1"))
    if not (request_id and ready_file and expected_sha):
        raise StartRefused("incomplete_handoff",
                           "the protocol variables are incomplete")

    if "MMT_ALLOW_SCHEMA_UPGRADE" in os.environ:
        raise StartRefused(
            "schema_upgrade_inherited",
            "the worker inherited MMT_ALLOW_SCHEMA_UPGRADE; a trading process "
            "must never be able to migrate a database")

    env = read_env_file()
    sha, _safety = config_fingerprint(env)
    if sha != expected_sha:
        raise StartRefused(
            "config_mismatch",
            "the configuration on disk no longer matches what the parent "
            "validated — it changed between prepare and start")

    effective = (env.get("MOO_TRADE_ENV") or "SIMULATE").upper()
    if effective != "SIMULATE":
        raise StartRefused("bad_trade_env",
                           f"worker refuses to run as {effective} in this phase")

    lease = start_lease.read() or {}
    if int(lease.get("fence", -2)) != fence:
        raise StartRefused("fence_mismatch",
                           "another worker took the lease while this one was "
                           "starting")

    # The lease moves into this process's name. Until now it was the parent's,
    # and a lease held by a web server describes the wrong thing: kill the
    # parent and the record names a pid that no longer exists, so the next
    # Start reads it as stale, breaks it, and spawns a second worker alongside
    # this one — which is still holding positions and still placing orders.
    try:
        held = start_lease.take_over(fence)
    except start_lease.LeaseUnavailable as e:
        raise StartRefused("lease_lost", str(e)) from e

    # The account is not re-derived here. The parent already established that
    # exactly one account matches this environment, and it passes the answer
    # down; resolving it again would let the worker mint a second account row
    # and file this run's trades under a record the parent never validated.
    expected_account = os.environ.get("MMT_ACCOUNT_ID", "")
    from . import identity
    session = identity.start_session(effective, auth_mode="protocol_simulate",
                                     account_id=expected_account or None)
    if expected_account and session["account_id"] != expected_account:
        identity.end_session("account_mismatch")
        raise StartRefused(
            "account_mismatch",
            "the worker resolved a different account than the parent validated")

    # The broker is asked WHO THIS IS before READY is written.
    #
    # Everything above this line is the worker checking its own files: its
    # config hash, its lease, its local account row. None of it has touched the
    # gateway. The binding used to be resolved lazily, the first time something
    # reached for MooClient.trade — which is after GO, inside trading. So a
    # worker could report "I have checked myself and I am consistent", be
    # cleared to trade, and only then discover that the gateway presents a
    # different account, or a different environment, than the one the parent
    # validated. By then it holds the lease and the order gate is open.
    #
    # READY is supposed to mean the worker verified its world. The broker is
    # the half of that world that matters most, and it was the half nobody
    # checked.
    broker_ref = None
    _probe = None
    try:
        from . import broker_binding
        from .moo_client import MooClient
        _probe = MooClient()
        _probe.trade                      # opening the context resolves + pins
        binding = broker_binding.require("verifying broker identity for READY")
        if binding.trade_env != effective:
            raise StartRefused(
                "broker_env_mismatch",
                f"the gateway serves a {binding.trade_env} account but this "
                f"worker was started as {effective}")
        broker_ref = binding.account_ref
    except StartRefused:
        identity.end_session("broker_identity_mismatch")
        raise
    except Exception as e:
        identity.end_session("broker_unreachable")
        raise StartRefused(
            "broker_unreachable",
            f"the broker could not confirm this run's account before READY: "
            f"{str(e)[:200]}") from e
    finally:
        try:
            if _probe is not None:
                _probe.close()
        except Exception:
            pass

    payload = {"pid": os.getpid(), "host": socket.gethostname(),
               "session_id": session["session_id"], "fence": fence,
               "config_sha256": sha, "effective_env": effective,
               "account_id": session["account_id"],
               "broker_ref": broker_ref,
               "lease_pid": held.holder_pid,
               "reported_at": time.time()}
    p = Path(ready_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, p)      # atomic: the parent never reads a half-written file
    return payload


def current_fence() -> int:
    """This worker's fencing token, or -1 outside a protocol start.

    Read from the environment rather than kept in a module variable: the token
    was handed to this process at spawn and is a fact about the process, not
    state something later can update.
    """
    try:
        return int(os.environ.get("MMT_START_FENCE", "-1"))
    except ValueError:
        return -1


def assert_may_trade(what: str) -> None:
    """Refuse unless this process is still the worker, by lease AND by session.

    Called before every order, modification and cancellation.

    The lease answers "am I still the one allowed to trade?". The case it
    catches is invisible from anywhere else: this process is fine, its pid is
    alive, its connection works — and it lost a race it never knew it was in.
    Something found it stale, took the lease, and is now the worker. Both would
    place orders against the same account, and only the token separates them.

    The session answers "is this run still supposed to be happening?". A Stop
    closes the session row and then signals the process; if the signal is lost,
    or the process is mid-scan and slow to die, the gap between those two is a
    window in which a worker nobody believes is running places an order. The
    row is re-read rather than trusted from memory, precisely because the thing
    that changed it is outside this process.
    """
    fence = current_fence()
    if fence < 0:
        raise start_lease.LeaseUnavailable(
            f"{what} refused: this process holds no fencing token, so it was "
            f"not started through the protocol")
    start_lease.assert_ours(fence, what=what)

    from . import identity
    session = identity.current_session()
    if not session:
        raise start_lease.LeaseUnavailable(
            f"{what} refused: this process has no execution session, so the "
            f"order could not be attributed to a run")
    try:
        with db.conn() as c:
            row = c.execute("SELECT ended_at, trade_env FROM execution_sessions "
                            "WHERE session_id = ?",
                            (session["session_id"],)).fetchone()
    except Exception as e:
        # Fail closed. Not being able to confirm the run is still open is not
        # the same as it being open, and the cost of pausing is a missed scan.
        raise start_lease.LeaseUnavailable(
            f"{what} refused: cannot confirm this session is still open ({e})")
    if row is None:
        raise start_lease.LeaseUnavailable(
            f"{what} refused: this session is not in the database")
    if row["ended_at"]:
        raise start_lease.LeaseUnavailable(
            f"{what} refused: this session was ended at {row['ended_at']} — a "
            f"Stop has already been recorded for this run")
    if row["trade_env"] != session["trade_env"]:
        raise start_lease.LeaseUnavailable(
            f"{what} refused: the session's recorded environment "
            f"({row['trade_env']}) is not the one this process is running as "
            f"({session['trade_env']})")


def wait_for_go(*, timeout: float = GO_TIMEOUT_S) -> dict:
    """Block until the parent accepts this worker. Nothing may trade before it.

    READY says "I have checked myself and I am consistent". It does not say the
    parent agrees. Between the two there are four comparisons the worker cannot
    make on its own — that its pid is the one that was spawned, that its config
    hash matches what was validated, that its fence is still current, that its
    account is the one that was authorised — and any of them can fail.

    Without this gate the worker simply carried on after writing READY. Its
    first act is _startup_protect_stops(), which places and cancels orders at
    the broker. So a worker the parent was about to reject could, and would,
    have traded first; the parent's kill arrives afterwards and cannot recall an
    order that has already been sent.

    A GO that never comes is a refusal. The parent may have died, or decided
    against this worker and failed to kill it — neither is permission.
    """
    go_file = os.environ.get("MMT_GO_FILE", "")
    request_id = os.environ.get("MMT_START_REQUEST_ID", "")
    fence = int(os.environ.get("MMT_START_FENCE", "-1"))
    if not go_file:
        raise StartRefused("incomplete_handoff", "no GO file was designated")

    p = Path(go_file)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if p.exists():
            try:
                go = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                time.sleep(0.1)
                continue
            if go.get("request_id") != request_id:
                raise StartRefused("go_mismatch",
                                   "the GO names a different start request")
            if int(go.get("fence", -1)) != fence:
                raise StartRefused("go_mismatch",
                                   "the GO carries a different fencing token")
            if int(go.get("worker_pid", -1)) != os.getpid():
                raise StartRefused("go_mismatch",
                                   "the GO was issued for a different worker")
            # Still ours to hold, at the moment we are cleared to use it.
            start_lease.assert_ours(fence, what="starting to trade")

            # The second grant. The gate starts shut, so this is the only thing
            # that opens it — and a GO that does not say `orders: true` shuts it
            # for good rather than leaving it merely ungranted. The difference
            # matters: "not yet granted" is a state something could later change
            # its mind about, and a staging run must not be able to.
            from . import order_gate
            if go.get("orders") is True:
                order_gate.permit(f"granted by the parent for request "
                                  f"{request_id[:8]}")
            else:
                order_gate.deny(f"the parent cleared request {request_id[:8]} to "
                                f"run but not to place orders")

            p.unlink(missing_ok=True)   # consumed; clears it for the next start
            log.info("start protocol: GO received for request %s (%s)",
                     request_id[:8], order_gate.describe())
            return go
        time.sleep(0.1)

    raise StartRefused(
        "go_timeout",
        f"no GO within {timeout:g}s — the parent never accepted this worker. "
        f"Exiting without trading; a start nobody confirmed is not a start")
