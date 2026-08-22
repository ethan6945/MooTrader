"""Keep credentials out of the log files.

WHY THIS EXISTS
  notifier.send() logs `telegram send failed: %s` on a network error. The
  requests exception carries the full request URL, and the Telegram API embeds
  the bot token IN the URL path. So a handful of timeouts wrote the live bot
  token into logs/trader.log and logs/scheduler.log — 20 occurrences by the
  2026-08-10 audit. Nothing was misconfigured; a normal error path did it.

  The lesson is that no single call site can be trusted to remember, so this
  scrubs at the logging layer: whatever a record says, known secret values are
  replaced before it reaches a handler. Call sites that already know they hold
  a secret should still use scrub() directly — belt and braces.

  Values come from settings, so a rotated credential is picked up on restart
  and an unset one is simply never matched.
"""
from __future__ import annotations

import logging
import re

REDACTED = "«redacted»"

# Anything shorter is not a credential and would cause collateral damage —
# A short WEB_PASSWORD is a real value in the wild, and blanket-replacing it
# would corrupt prices, quantities and timestamps across the whole log.
_MIN_SECRET_LEN = 12

# Telegram embeds the token in the URL path, so it appears even when the token
# string itself was never formatted into a message.
_TELEGRAM_URL = re.compile(r"(/bot)\d{6,}:[A-Za-z0-9_-]{10,}")


def _secret_values() -> list[str]:
    import os

    from .config import settings

    raw: list[str] = []
    # Single-value credentials.
    for attr in ("telegram_token", "tavily_key", "moo_trade_pwd", "finnhub_key"):
        v = getattr(settings, attr, "")
        if isinstance(v, str) and v.strip():
            raw.append(v.strip())
    # Multi-key cascades are tuples of keys — each must match on its own.
    for attr in ("deepseek_keys",):
        for k in getattr(settings, attr, ()) or ():
            if str(k).strip():
                raw.append(str(k).strip())
    # And the LIVE environment, for every credential .env can hold.
    #
    # settings is a snapshot frozen at process start. A key pasted into the
    # panel is written to .env and does not appear there — so the filter went
    # on redacting the OLD key while the NEW one, which the panel immediately
    # probes, was not in the list at all. Measured: after a rotation,
    # scrub("... tvly-NEWKEY...") returned the key verbatim, and calling
    # reset_cache() did not help because the re-read hit the same snapshot.
    #
    # The leak this filter exists for came from a third-party logger
    # (requests/urllib3/httpx) echoing a request, which is exactly what a
    # post-rotation preflight probe triggers.
    for env_name in ("WEB_PASSWORD", "WEB_SECRET", "TELEGRAM_TOKEN",
                     "TAVILY_API_KEY", "MOO_TRADE_PWD", "FINNHUB_API_KEY",
                     "DEEPSEEK_API_KEY", "OPEND_LOGIN_ACCOUNT"):
        v = os.getenv(env_name, "")
        # The cascades are comma-separated; each key must match on its own.
        for part in v.split(","):
            if part.strip():
                raw.append(part.strip())
    # Longest first: a key that contains another as a prefix must be replaced
    # whole, or the shorter match would leave a readable tail behind.
    return sorted({s for s in raw if len(s) >= _MIN_SECRET_LEN}, key=len, reverse=True)


_cache: list[str] | None = None


def scrub(text: str) -> str:
    """Replace every known credential in `text`. Safe on any string."""
    global _cache
    if not text:
        return text
    if _cache is None:
        try:
            _cache = _secret_values()
        except Exception:
            _cache = []
    out = text
    for secret in _cache:
        if secret in out:
            out = out.replace(secret, REDACTED)
    return _TELEGRAM_URL.sub(r"\1" + REDACTED, out)


def reset_cache() -> None:
    """Re-read secrets from settings (after a rotation, or in tests)."""
    global _cache
    _cache = None


class RedactingFilter(logging.Filter):
    """Scrub the formatted message and args of every record passing through.

    Attached to the root logger, so it covers third-party loggers (requests,
    urllib3, httpx) that this codebase never calls directly — which is exactly
    where the Telegram leak came from.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        cleaned = scrub(msg)
        if cleaned != msg:
            # Collapse to the scrubbed text: args are already interpolated in,
            # and leaving them would let a handler re-expand the original.
            record.msg = cleaned
            record.args = ()
        return True


def install(logger: logging.Logger | None = None) -> None:
    """Attach the filter to `logger` (root by default), once."""
    target = logger or logging.getLogger()
    if any(isinstance(f, RedactingFilter) for f in target.filters):
        return
    target.addFilter(RedactingFilter())
    # A Filter on a Logger is not consulted for records that propagate up from
    # child loggers, so also attach to each handler — handlers see everything.
    for h in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())
