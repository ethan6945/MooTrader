"""The one place this system talks to an LLM. DeepSeek, and only DeepSeek.

This said "Gemini OR DeepSeek, switchable at runtime", and had done since
2026-07-22, when Gemini left PROVIDERS and became unselectable. Everything
downstream inherited the claim: GEMINI_MODEL stayed in the parameter file as
an editable setting, GEMINI_API_KEYS stayed in .env, AI_ENSEMBLE_ENABLED
stayed `true` for a two-engine vote that had already collapsed to one, and
signal_reporter's `_call_gemini` went on calling DeepSeek under a name that
said otherwise. The owner reasonably concluded Gemini was still in use.

WHAT IS ACTUALLY HERE
  One provider (PROVIDERS), one transport (_deepseek), no cascade and no
  ensemble. The MODEL is still a runtime override in db-state (`ai_model`)
  over the .env default, fetched live so a newly released model is usable
  without a code change. The scheduler reads it per call, so a change in the
  web panel lands on the next scan with no restart.

  Keys stay in .env (DEEPSEEK_API_KEYS comma-rotated, or DEEPSEEK_API_KEY).

  DeepSeek is text-only. supports_vision() returns False for every provider
  and the `image` / `search` arguments are accepted and ignored — the callers
  that pass them already gate on it, and removing the parameters would be a
  change to those callers rather than to this fact.

TO ADD A PROVIDER BACK
  PROVIDERS, PROVIDER_LABELS, _FALLBACK_MODELS, provider_keys, default_model,
  and a transport beside _deepseek. Deliberately more than a one-line change:
  the previous arrangement made re-adding cheap by leaving the dead half in
  place, and the cost of that was a month of configuration describing a
  provider nobody was using.
"""
from __future__ import annotations

import json
import logging
import time

import requests

from . import db
from .config import settings

log = logging.getLogger(__name__)

# One provider. The block that stood here said the Gemini helpers "stay
# defined but unreachable — keeping them makes re-adding Gemini a one-line
# change". They are gone now: that arrangement is what left GEMINI_MODEL in
# the parameter file, GEMINI_API_KEYS in .env, and AI_ENSEMBLE_ENABLED true
# for an ensemble that had collapsed to one engine.
PROVIDERS = ("deepseek",)
PROVIDER_LABELS = {"deepseek": "DeepSeek"}

# Static fallbacks for the model dropdown when a live fetch fails (offline / no
# key). Kept tiny and current; the live fetch is the source of truth.
_FALLBACK_MODELS = {
    "deepseek": ["deepseek-v4-flash", "deepseek-v4-pro"],
}

# DeepSeek retired the legacy alias names on 2026-07-24 15:59 UTC. They do NOT
# soft-redirect — a request naming one fails outright, which for this bot means
# every AI layer silently degrading to its neutral fallback while the trading
# loop carries on looking healthy. Exactly the failure mode preflight.py was
# written for, so we do not rely on the user noticing: the name is rewritten
# here (with a warning) and preflight.check_ai() reports the stale config.
#
# Both aliases pointed at deepseek-v4-flash — deepseek-chat was its
# non-thinking mode, deepseek-reasoner its thinking mode. Thinking is now a
# request parameter rather than a model name, so both map to the same model and
# _deepseek() sets the mode explicitly.
_RETIRED_DEEPSEEK_MODELS = {
    "deepseek-chat": ("deepseek-v4-flash", False),
    "deepseek-reasoner": ("deepseek-v4-flash", True),
}


def migrate_deepseek_model(model: str) -> tuple[str, bool]:
    """(effective_model, thinking) for a configured DeepSeek model name.

    Unknown/current names pass through untouched with thinking left off — this
    must never guess at a model the user deliberately chose.
    """
    hit = _RETIRED_DEEPSEEK_MODELS.get((model or "").strip())
    if not hit:
        return model, False
    new, thinking = hit
    log.warning("DEEPSEEK_MODEL=%r was retired on 2026-07-24 and no longer "
                "resolves — using %r (thinking=%s). Update the setting to "
                "silence this.", model, new, thinking)
    return new, thinking


_DEEPSEEK_BASE = "https://api.deepseek.com"


# ── active provider / model (runtime override beats .env) ────────────────────
def _state() -> dict:
    try:
        return db.get_state()
    except Exception:
        return {}


def active_provider() -> str:
    """Effective provider — db-state override else .env default. Always one of
    PROVIDERS. Anything else — a typo, or the retired "gemini" — is
    deepseek."""
    p = (_state().get("ai_provider") or settings.ai_provider or "deepseek")
    p = str(p).strip().lower()
    return p if p in PROVIDERS else "deepseek"


def default_model(provider: str) -> str:
    return settings.deepseek_model


def active_model(provider: str | None = None) -> str:
    """Effective model for the active provider — db-state `ai_model` override
    else the provider's .env default. The db override is provider-scoped: it is
    ignored if it doesn't look like it belongs to the current provider, so a
    model left over from another one never leaks into a DeepSeek run."""
    provider = provider or active_provider()
    override = _state().get("ai_model")
    if override:
        override = str(override).strip()
        looks_deepseek = override.startswith("deepseek")
        if (provider == "deepseek") == looks_deepseek:
            return override
    return default_model(provider)


# ── keys ─────────────────────────────────────────────────────────────────────
def provider_keys(provider: str | None = None) -> list[str]:
    provider = provider or active_provider()
    return list(settings.deepseek_keys)


def has_key(provider: str | None = None) -> bool:
    return bool(provider_keys(provider))


def supports_vision(provider: str | None = None) -> bool:
    """No configured provider is multimodal. Kept because callers gate on it.

    It read `== "gemini"`. Gemini stopped being a selectable provider on
    2026-07-22 and the name went on implying a capability the system has not
    had since.
    """
    return False


def model_cascade(provider: str | None = None) -> list[str]:
    """Models tried in order for one call. DeepSeek has no cascade.

    The fallback list this walked was the Gemini free tier — a floor to drop
    to on a 429. There is no second provider to fall to now.
    """
    return [active_model(provider or active_provider())]


# ── generation ───────────────────────────────────────────────────────────────
# ── runtime call-outcome ledger (2026-08-07) ─────────────────────────────────
# health_check probes the provider with its own "ping" on a timer. That is not
# the same question as "are the calls this bot actually makes succeeding": the
# probe can pass while a layer's real prompts fail, and — as the retired
# deepseek-chat name proved — a real outage can run for days between probes
# with every layer fail-safing to neutral and the loop looking healthy.
#
# So every generate() records its outcome. health_check reads the streak and
# alerts on it; the web status endpoint shows it. Best-effort throughout: a
# db hiccup must never turn a working AI call into a failed one.
_K_FAIL_STREAK = "ai_fail_streak"
_K_LAST_ERR = "ai_last_error"
_K_LAST_OK = "ai_last_ok_ts"
_K_LAST_FAIL = "ai_last_fail_ts"


def _record_outcome(ok: bool, err: str = "") -> None:
    try:
        import time as _t
        now = _t.time()
        if ok:
            def _upd(s: dict) -> dict:
                return {_K_FAIL_STREAK: 0, _K_LAST_OK: now, _K_LAST_ERR: ""}
        else:
            def _upd(s: dict) -> dict:
                return {_K_FAIL_STREAK: int(s.get(_K_FAIL_STREAK) or 0) + 1,
                        _K_LAST_FAIL: now, _K_LAST_ERR: err[:200]}
        db.atomic_state(_upd)
    except Exception as e:
        log.debug("ai outcome ledger write failed: %s", e)


def call_health() -> dict:
    """What the LAST real calls did. Consumed by health_check + /api/status."""
    try:
        s = db.get_state()
    except Exception:
        return {"fail_streak": 0, "last_error": "", "last_ok": None, "last_fail": None}
    return {
        "fail_streak": int(s.get(_K_FAIL_STREAK) or 0),
        "last_error": s.get(_K_LAST_ERR) or "",
        "last_ok": s.get(_K_LAST_OK),
        "last_fail": s.get(_K_LAST_FAIL),
    }


def generate(prompt: str, *, temperature: float | None = None,
             image: bytes | None = None, search: bool = False) -> tuple[str, str]:
    """Run one prompt on the ACTIVE provider. Returns (text, model_used).

    Encapsulates key rotation + model cascade + 429→next-key / 503→retry — the
    machinery that used to be copy-pasted into every caller. Raises RuntimeError
    if no key is configured or every key/model is exhausted; callers keep their
    own try/except neutral defaults.

    image    — accepted and ignored. No configured provider is multimodal;
               callers gate on supports_vision(), which is False.
    search   — accepted and ignored, same reason.
    """
    provider = active_provider()
    keys = provider_keys(provider)
    if not keys:
        # NOT a failure of the provider — nothing was called. Recording this as
        # one would make "you never configured a key" indistinguishable from
        # "your key stopped working", which are different problems with
        # different fixes.
        raise RuntimeError(f"no {PROVIDER_LABELS[provider]} key configured")
    try:
        out = _deepseek(prompt, keys, temperature=temperature)
    except Exception as e:
        _record_outcome(False, str(e))
        raise
    _record_outcome(True)
    return out


def generate_text(prompt: str, **kw) -> str:
    """Convenience wrapper — just the text (drops the model-used tag)."""
    return generate(prompt, **kw)[0]


# ── P1-1 (2026-06-26): Dual-provider ensemble voting ─────────────────────────


def _extract_verdict(text: str) -> str:
    """Extract pass/veto from an AI response. Returns 'pass' | 'veto' | 'unknown'."""
    import re
    t = text.lower()
    if '"veto"' in t or "'veto'" in t or "verdict\": \"veto" in t:
        return "veto"
    if '"pass"' in t or "'pass'" in t or "verdict\": \"pass" in t:
        return "pass"
    # Fallback: look for the word
    match = re.search(r'verdict["\s:]+(pass|veto)', t)
    if match:
        return match.group(1)
    return "unknown"


# Network timeouts (ms) so a slow or blocked call fails fast instead of
# hanging the scheduler or the web dropdown.
_GEN_TIMEOUT_MS = 60_000
_LIST_TIMEOUT_MS = 10_000






def _deepseek(prompt, keys, *, temperature) -> tuple[str, str]:
    model, thinking = migrate_deepseek_model(active_model("deepseek"))
    body: dict = {"model": model, "messages": [{"role": "user", "content": prompt}],
                  "stream": False}
    # Thinking moved from the model name to a request parameter in V4, and it
    # defaults to ON for the pro tier. Every caller in this bot wants a short
    # JSON verdict on a latency budget (the entry path already measured median
    # 42s / p90 99s BEFORE reasoning was in the picture), so state the mode
    # explicitly rather than inheriting a per-model default that can change.
    body["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if temperature is not None:
        body["temperature"] = temperature
    last_err: Exception | None = None
    for key in keys:
        for attempt in range(2):
            try:
                r = requests.post(
                    f"{_DEEPSEEK_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json=body, timeout=60)
                if r.status_code == 429:
                    last_err = RuntimeError("429 rate/quota")
                    break  # next key
                if r.status_code in (500, 503) and attempt == 0:
                    last_err = RuntimeError(f"{r.status_code} transient")
                    time.sleep(4)
                    continue
                r.raise_for_status()
                text = (r.json()["choices"][0]["message"]["content"] or "").strip()
                return text, model
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt == 0:
                    time.sleep(2)
                    continue
                break
    raise RuntimeError(f"DeepSeek exhausted: {last_err}")


# ── live model listing (for the web dropdown) ────────────────────────────────
def list_models(provider: str, *, key: str | None = None) -> list[str]:
    """Fetch the provider's currently-available text models, LIVE. Raises on
    failure so the endpoint can fall back to _FALLBACK_MODELS."""
    provider = provider.strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    key = key or (provider_keys(provider)[0] if provider_keys(provider) else None)
    if not key:
        raise RuntimeError(f"no {PROVIDER_LABELS[provider]} key to list models")
    return _list_deepseek(key)


def fallback_models(provider: str) -> list[str]:
    return list(_FALLBACK_MODELS.get(provider, []))




def _list_deepseek(key: str) -> list[str]:
    r = requests.get(f"{_DEEPSEEK_BASE}/models",
                     headers={"Authorization": f"Bearer {key}"}, timeout=20)
    r.raise_for_status()
    data = r.json().get("data", [])
    return [d["id"] for d in data if d.get("id")]
