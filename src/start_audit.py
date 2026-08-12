"""Append-only audit of every attempt to start the trading worker.

A start is the moment software begins placing orders. Six weeks of this project's
history turned on questions the process table could not answer: who started it,
against which configuration, which database, and why nobody noticed. On
2026-08-11 the scheduler came up on its own and traded for two hours; the only
evidence was a parent pid and a log timestamp.

So every attempt — accepted or refused — writes a record before anything is
spawned. If the record cannot be written, the start does not happen. An
unauditable start is indistinguishable from the thing this exists to detect.

WHAT IS RECORDED
  A fixed allowlist, declared below. Anything not on it is dropped, and anything
  matching the forbidden patterns makes the write fail loudly rather than
  quietly redacting — a field that should never have been passed is a bug at the
  call site, and silently cleaning it up hides that.

WHAT IS NEVER RECORDED
  Broker account numbers, login identifiers, passwords, API keys, IP addresses,
  user agents, whole request bodies, raw OpenD responses, exception text. The
  broker account appears only as an irreversible per-install reference
  (src/account_ref.py) — enough to answer "the same account as yesterday?",
  useless to anyone who obtains the log.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

_LOCK = threading.Lock()

# Every field that may appear in a record. A start audit is read during an
# incident, and a schema that drifts is one more thing to reverse-engineer then.
ALLOWED_FIELDS = frozenset({
    "ts", "request_id", "session_id", "event", "result", "source",
    "build_id", "executable", "executable_sha256",
    "config_sha256", "config_path", "safety",
    "db_path", "db_version", "db_integrity", "account_ref", "effective_env",
    "lease_holder", "fence_token", "worker_pid", "worker_host",
    "refusal_code", "refusal_detail", "duration_ms",
})

# Where the worker/API is allowed to have come from.
SOURCES = frozenset({"macos", "web", "cli", "test"})

# Result codes are stable strings: they end up in dashboards and in support
# conversations, and renaming one silently breaks both.
RESULTS = frozenset({
    "prepared", "committed", "ready", "refused", "timeout", "reclaimed", "error",
})

# Values that must never reach the audit. Matching one is an error at the call
# site, not something to quietly strip.
_FORBIDDEN_VALUE = [
    ("telegram token",   re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}")),
    ("api key",          re.compile(r"\b(?:sk-|tvly-|ghp_|AIza|xox[baprs]-)[A-Za-z0-9_-]{12,}")),
    ("email address",    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("ip address",       re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("private key",      re.compile(r"-----BEGIN")),
]
# Field names that must never be added to ALLOWED_FIELDS. Checked once, when
# this module loads, rather than on every record: the allowlist is what vets a
# field name, so re-testing an already-vetted name at runtime only produces
# false alarms — `fence_token` is a monotonic integer that happens to contain
# "token", and rejecting it broke the reclaim audit, which is the one record
# that gets written when something has already gone wrong.
_FORBIDDEN_KEY = re.compile(
    r"(password|passwd|pwd|secret|api_key|apikey|cookie|authorization|"
    r"user_agent|useragent|remote_addr|client_ip|request_body|payload|"
    r"traceback|stack|acc_id|account_no|login)", re.I)

_bad = sorted(f for f in ALLOWED_FIELDS if _FORBIDDEN_KEY.search(f))
if _bad:      # pragma: no cover — a wiring mistake, caught at import
    raise RuntimeError(
        f"start_audit.ALLOWED_FIELDS contains dangerous field name(s): {_bad}")


class AuditRefused(Exception):
    """The record could not be written, so the start must not proceed."""


def _path() -> Path:
    return settings.root / "logs" / "start_audit.jsonl"


def _validate(record: dict) -> dict:
    unknown = set(record) - ALLOWED_FIELDS
    if unknown:
        raise AuditRefused(f"unknown audit field(s): {sorted(unknown)}")
    for key, value in record.items():
        # Nested structures are flattened to text for scanning: a forbidden
        # value hidden one level down is still in the file.
        blob = value if isinstance(value, str) else json.dumps(value, default=str)
        for label, pat in _FORBIDDEN_VALUE:
            if pat.search(blob):
                raise AuditRefused(
                    f"field {key!r} contains something that looks like a {label}; "
                    f"pass a reference (src/account_ref.py) or omit it")
    if record.get("source") not in SOURCES:
        raise AuditRefused(f"source must be one of {sorted(SOURCES)}")
    if record.get("result") not in RESULTS:
        raise AuditRefused(f"result must be one of {sorted(RESULTS)}")
    return record


def record(**fields) -> dict:
    """Append one audit record. Raises AuditRefused if it cannot be written.

    Callers must treat that exception as "do not start". It is raised for a
    malformed record too, deliberately: a start whose audit is wrong is not
    better than a start with no audit.
    """
    rec = {"ts": datetime.now(timezone.utc).isoformat(), **fields}
    rec = _validate(rec)
    line = json.dumps(rec, default=str, sort_keys=True) + "\n"
    p = _path()
    try:
        with _LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            # O_APPEND so concurrent writers interleave whole lines rather than
            # overwriting each other, and 0600 because the file records which
            # account was traded and when.
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode())
                os.fsync(fd)      # an audit that is still in a buffer when the
                                  # machine dies did not happen
            finally:
                os.close(fd)
    except AuditRefused:
        raise
    except OSError as e:
        raise AuditRefused(f"cannot write the start audit at {p}: {e}") from e
    return rec


def tail(n: int = 20) -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(errors="ignore").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
