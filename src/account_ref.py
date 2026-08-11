"""Irreversible, stable references for external account identifiers.

WHAT THIS IS FOR
  The broker's account number, the OpenD login, the Telegram chat id — these
  identify a person and an account, and we need them in two places that should
  never hold the raw value: our own database rows, and any snapshot or backup
  that gets copied around.

  Hashing them plainly would not do: a bare SHA-256 of an 8-digit moomoo account
  number falls to a dictionary of every 8-digit number in under a second. So the
  mapping is keyed HMAC against a secret generated once per install and stored
  0600 outside version control. Same input always gives the same reference, so
  "is this the same account as last week?" stays answerable; without the key
  file the reference says nothing about the input.

  It is deliberately per-install. Two machines produce different references for
  the same account, which means a leaked database cannot be correlated against
  anything else.

WHERE THE RAW VALUE STILL LIVES
  `.env`, because OpenD needs it to connect. This module is about everything
  downstream of that: db rows, backups, logs, exports.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_KEY_LOCK = threading.Lock()
_key_cache: bytes | None = None

REF_PREFIX = "ref:"
_REF_LEN = 20          # 80 bits of the digest — collision-free at this scale


def _key_path() -> Path:
    from .config import settings
    return settings.root / "data" / ".account_ref_key"


def _load_or_create_key() -> bytes:
    """Per-install HMAC key. Created on first use, 0600, never committed."""
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    with _KEY_LOCK:
        if _key_cache is not None:
            return _key_cache
        p = _key_path()
        if p.exists():
            _key_cache = p.read_bytes().strip()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            key = secrets.token_hex(32).encode()
            # Create with the right mode from the start — writing then chmod
            # leaves a window where the key is world-readable.
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
            _key_cache = key
            log.info("account_ref: generated a new per-install key at %s", p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return _key_cache


def ref(value: str | int | None, kind: str = "acct") -> str | None:
    """Stable, irreversible reference for an external identifier.

    ref("283812345", "moo")  ->  "ref:moo:9f3c1a2b8d4e5f60718a"

    Same value + same kind + same install => same output, forever. Returns None
    for an empty input so callers can pass through "not known yet" unchanged.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    digest = hmac.new(_load_or_create_key(), f"{kind}:{raw}".encode(),
                      hashlib.sha256).hexdigest()
    return f"{REF_PREFIX}{kind}:{digest[:_REF_LEN]}"


def is_ref(value: str | None) -> bool:
    return bool(value) and str(value).startswith(REF_PREFIX)


def matches(value: str | int | None, reference: str | None,
            kind: str = "acct") -> bool:
    """Does a raw identifier correspond to a stored reference?

    The only supported direction — a reference cannot be turned back into the
    identifier, so verification is always "hash the candidate and compare".
    Uses a constant-time compare out of habit rather than necessity.
    """
    if not reference:
        return False
    candidate = ref(value, kind)
    return bool(candidate) and hmac.compare_digest(candidate, reference)


def reset_cache() -> None:
    """Forget the loaded key (tests)."""
    global _key_cache
    _key_cache = None
