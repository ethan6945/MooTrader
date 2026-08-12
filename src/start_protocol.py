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
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import start_audit, start_lease
from .config import settings

log = logging.getLogger(__name__)

READY_TIMEOUT_S = 60.0
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
            home: Path | None = None) -> StartRequest:
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
    account_ref = _aref.ref(rows[0]["account_id"], kind="account")

    # ── positions: database vs mirror ──
    db_positions = {r["symbol"] for r in c.execute("SELECT symbol FROM open_trades")}
    c.close()
    mirror = home / "data" / "open_trades.json"
    if mirror.exists():
        try:
            mirror_positions = set(json.loads(mirror.read_text()) or {})
        except (json.JSONDecodeError, OSError) as e:
            refuse("mirror_unreadable", f"cannot read open_trades.json: {e}")
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
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    ready_file.unlink(missing_ok=True)

    env = build_child_env(home, req.config_sha256)
    env["MMT_START_REQUEST_ID"] = req.request_id
    env["MMT_START_FENCE"] = str(lease.fence)
    env["MMT_READY_FILE"] = str(ready_file)

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
        ready_file.unlink(missing_ok=True)


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

    from . import identity
    session = identity.start_session(effective, auth_mode="protocol_simulate")

    payload = {"pid": os.getpid(), "host": socket.gethostname(),
               "session_id": session["session_id"], "fence": fence,
               "config_sha256": sha, "effective_env": effective,
               "reported_at": time.time()}
    p = Path(ready_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, p)      # atomic: the parent never reads a half-written file
    return payload
