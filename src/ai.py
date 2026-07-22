"""Unified AI provider layer — Gemini OR DeepSeek, switchable at runtime.

WHY: Gemini occasionally goes unavailable (quota/outage). This module is the one
place that talks to an LLM, so the owner can flip the WHOLE system to DeepSeek
(or back) from the web panel with NO restart, and pick the exact model from a
live dropdown (so a newly released model is usable without a code change — the
model is no longer hardcoded to gemini-3.5-flash).

Provider + model are RUNTIME overrides in db-state (`ai_provider` / `ai_model`),
falling back to the frozen .env defaults (AI_PROVIDER / GEMINI_MODEL /
DEEPSEEK_MODEL). The running scheduler reads them per AI call, so a switch made
in the web UI takes effect on the next scan — same cross-process db-state
mechanism runtime_config.py uses for param overrides.

Keys stay in .env (GEMINI_API_KEYS comma-rotated; DEEPSEEK_API_KEYS/_KEY).

DeepSeek is text-only (no vision, no google_search grounding). Callers that need
those gate on `supports_vision()` / pass `search=` (honoured only on Gemini).
"""
from __future__ import annotations

import json
import logging
import time

import requests

from . import db
from .config import settings, GEMINI_FREE_CASCADE

log = logging.getLogger(__name__)

# DeepSeek-only (2026-07-22): Gemini removed as a selectable engine. The
# helper functions (_gemini, _list_gemini, the ensemble's gemini branch) stay
# defined but unreachable — keeping them makes re-adding Gemini a one-line
# revert. Consequences of the switch: the entry validator runs single-engine
# (DeepSeek), and chart-pattern vision (Gemini-only) no longer runs.
PROVIDERS = ("deepseek",)
PROVIDER_LABELS = {"deepseek": "DeepSeek"}

# Static fallbacks for the model dropdown when a live fetch fails (offline / no
# key). Kept tiny and current; the live fetch is the source of truth.
_FALLBACK_MODELS = {
    "gemini": ["gemini-3.5-flash", "gemini-3.5-pro", "gemini-2.5-flash"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
}

_DEEPSEEK_BASE = "https://api.deepseek.com"


# ── active provider / model (runtime override beats .env) ────────────────────
def _state() -> dict:
    try:
        return db.get_state()
    except Exception:
        return {}


def active_provider() -> str:
    """Effective provider — db-state override else .env default. Always one of
    PROVIDERS (an unknown/typo value falls back to gemini)."""
    p = (_state().get("ai_provider") or settings.ai_provider or "deepseek")
    p = str(p).strip().lower()
    return p if p in PROVIDERS else "deepseek"


def default_model(provider: str) -> str:
    return settings.deepseek_model if provider == "deepseek" else settings.gemini_model


def active_model(provider: str | None = None) -> str:
    """Effective model for the active provider — db-state `ai_model` override
    else the provider's .env default. The db override is provider-scoped: it is
    ignored if it doesn't look like it belongs to the current provider, so a
    leftover Gemini model never leaks into a DeepSeek run."""
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
    return list(settings.deepseek_keys if provider == "deepseek" else settings.gemini_keys)


def has_key(provider: str | None = None) -> bool:
    return bool(provider_keys(provider))


def supports_vision(provider: str | None = None) -> bool:
    """Only Gemini is multimodal. Vision callers must skip when this is False."""
    return (provider or active_provider()) == "gemini"


def model_cascade(provider: str | None = None) -> list[str]:
    """Models tried in order for one call. Gemini: active model first, then the
    free-tier floor (so a transient 429 on the chosen model still falls through
    to a working one). DeepSeek: just the active model (no cascade)."""
    provider = provider or active_provider()
    primary = active_model(provider)
    if provider == "deepseek":
        return [primary]
    out, seen = [], set()
    for m in [primary] + list(GEMINI_FREE_CASCADE):
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


# ── generation ───────────────────────────────────────────────────────────────
def generate(prompt: str, *, temperature: float | None = None,
             image: bytes | None = None, search: bool = False) -> tuple[str, str]:
    """Run one prompt on the ACTIVE provider. Returns (text, model_used).

    Encapsulates key rotation + model cascade + 429→next-key / 503→retry — the
    machinery that used to be copy-pasted into every caller. Raises RuntimeError
    if no key is configured or every key/model is exhausted; callers keep their
    own try/except neutral defaults.

    image    — PNG bytes for a vision call (Gemini only; ignored on DeepSeek).
    search   — enable google_search grounding (Gemini only; ignored elsewhere).
    """
    provider = active_provider()
    keys = provider_keys(provider)
    if not keys:
        raise RuntimeError(f"no {PROVIDER_LABELS[provider]} key configured")
    if provider == "deepseek":
        return _deepseek(prompt, keys, temperature=temperature)
    return _gemini(prompt, keys, temperature=temperature, image=image, search=search)


def generate_text(prompt: str, **kw) -> str:
    """Convenience wrapper — just the text (drops the model-used tag)."""
    return generate(prompt, **kw)[0]


# ── P1-1 (2026-06-26): Dual-provider ensemble voting ─────────────────────────
def generate_ensemble(prompt: str, *,
                      temperature: float | None = None) -> dict:
    """Call BOTH Gemini AND DeepSeek in parallel, return a consensus result.

    Returns:
      {
        "text": str,                     # merged or dominant text
        "verdict": "consensus" | "single" | "conflict" | "unavailable",
        "gemini": {"text": str, "model": str} | None,
        "deepseek": {"text": str, "model": str} | None,
        "confidence": 0-100,            # 100=consensus, 60=single, 30=conflict
      }

    When only one provider has keys, falls back to single-provider (verdict=
    'single', confidence=60). On total failure returns verdict='unavailable'.
    """
    import concurrent.futures

    # "gemini" is no longer a provider → the ensemble collapses to single-engine
    # DeepSeek (verdict="single", confidence 60). Gate on PROVIDERS so re-adding
    # Gemini restores the dual-engine consensus automatically.
    gemini_ok = "gemini" in PROVIDERS and has_key("gemini")
    deepseek_ok = has_key("deepseek")
    result: dict = {
        "text": "",
        "verdict": "unavailable",
        "gemini": None,
        "deepseek": None,
        "confidence": 0,
    }

    # ── Single-provider fallback ──
    if not gemini_ok and not deepseek_ok:
        result["text"] = "no AI keys configured"
        return result

    if gemini_ok and not deepseek_ok:
        try:
            text, model = _gemini(prompt, provider_keys("gemini"),
                                  temperature=temperature, image=None, search=False)
            result.update(text=text, verdict="single", gemini={"text": text, "model": model},
                          confidence=60)
        except Exception as e:
            result["text"] = f"Gemini unavailable: {e}"
        return result

    if deepseek_ok and not gemini_ok:
        try:
            text, model = _deepseek(prompt, provider_keys("deepseek"),
                                    temperature=temperature)
            result.update(text=text, verdict="single",
                          deepseek={"text": text, "model": model}, confidence=60)
        except Exception as e:
            result["text"] = f"DeepSeek unavailable: {e}"
        return result

    # ── Both available → parallel calls ──
    def _call_gemini():
        try:
            t, m = _gemini(prompt, provider_keys("gemini"),
                           temperature=temperature, image=None, search=False)
            return {"text": t, "model": m}
        except Exception as e:
            log.warning("Gemini ensemble call failed: %s", e)
            return None

    def _call_deepseek():
        try:
            t, m = _deepseek(prompt, provider_keys("deepseek"),
                             temperature=temperature)
            return {"text": t, "model": m}
        except Exception as e:
            log.warning("DeepSeek ensemble call failed: %s", e)
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_g = ex.submit(_call_gemini)
        f_d = ex.submit(_call_deepseek)
        g_res = f_g.result(timeout=75)
        d_res = f_d.result(timeout=75)

    result["gemini"] = g_res
    result["deepseek"] = d_res

    # ── Consensus logic ──
    if g_res and d_res:
        # Both succeeded — try to extract verdicts and compare
        g_verdict = _extract_verdict(g_res["text"])
        d_verdict = _extract_verdict(d_res["text"])
        if g_verdict == d_verdict:
            result.update(text=g_res["text"], verdict="consensus", confidence=90)
        else:
            # Conflict — default to pass but flag it
            result.update(
                text=(f"CONFLICT: Gemini={g_verdict}, DeepSeek={d_verdict}. "
                      f"Gemini: {g_res['text'][:200]}\nDeepSeek: {d_res['text'][:200]}"),
                verdict="conflict", confidence=30)
    elif g_res:
        result.update(text=g_res["text"], verdict="single", confidence=60)
    elif d_res:
        result.update(text=d_res["text"], verdict="single", confidence=60)
    else:
        result.update(text="both providers unavailable", verdict="unavailable",
                      confidence=0)

    return result


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


# Network timeouts (ms) so a slow/geo-blocked Gemini call fails fast instead of
# hanging the scheduler or the web dropdown.
_GEN_TIMEOUT_MS = 60_000
_LIST_TIMEOUT_MS = 10_000


def _gemini_client(key: str, timeout_ms: int):
    from google import genai
    from google.genai import types
    return genai.Client(api_key=key,
                        http_options=types.HttpOptions(timeout=timeout_ms))


def _gemini(prompt, keys, *, temperature, image, search) -> tuple[str, str]:
    from google.genai import types

    contents = prompt
    if image is not None:
        part = types.Part.from_bytes(data=image, mime_type="image/png")
        contents = [part, prompt]

    # Build the per-call config(s). When google_search grounding is requested we
    # try WITH tools first, then fall back to a plain call (older models reject
    # tools) — mirrors the old signal_reporter behaviour.
    base_cfg: dict = {}
    if temperature is not None:
        base_cfg["temperature"] = temperature
    cfgs: list[dict | None] = []
    if search:
        sc = dict(base_cfg)
        sc["tools"] = [{"google_search": {}}]
        cfgs.append(sc)
    cfgs.append(base_cfg or None)

    def _one(client, model_name, cfg) -> tuple[str | None, bool, Exception | None]:
        """One (model, key, cfg) attempt with a single 503 retry.
        Returns (text | None, is_quota, err)."""
        for attempt in range(2):
            try:
                resp = client.models.generate_content(
                    model=model_name, contents=contents,
                    **({"config": cfg} if cfg else {}))
                return (resp.text or "").strip(), False, None
            except Exception as e:  # noqa: BLE001
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    return None, True, e            # quota → caller tries next key
                if attempt == 0 and any(
                        x in err for x in ("503", "500", "UNAVAILABLE")):
                    time.sleep(4)
                    continue
                return None, False, e               # other error → next cfg/key
        return None, False, None

    last_err: Exception | None = None
    for model_name in model_cascade("gemini"):
        for key in keys:
            client = _gemini_client(key, _GEN_TIMEOUT_MS)
            for cfg in cfgs:                        # search-on cfg first, then plain
                text, is_quota, err = _one(client, model_name, cfg)
                if text:
                    return text, model_name
                last_err = err or last_err
                if is_quota:
                    break                           # next key (skip remaining cfgs)
    raise RuntimeError(f"Gemini exhausted: {last_err}")


def _deepseek(prompt, keys, *, temperature) -> tuple[str, str]:
    model = active_model("deepseek")
    body: dict = {"model": model, "messages": [{"role": "user", "content": prompt}],
                  "stream": False}
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
    return _list_gemini(key) if provider == "gemini" else _list_deepseek(key)


def fallback_models(provider: str) -> list[str]:
    return list(_FALLBACK_MODELS.get(provider, []))


def _list_gemini(key: str) -> list[str]:
    client = _gemini_client(key, _LIST_TIMEOUT_MS)
    out = []
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if "generateContent" not in actions:
            continue
        name = (getattr(m, "name", "") or "").split("/")[-1]
        if name:
            out.append(name)
    # Newest-looking first, deduped.
    return sorted(set(out), reverse=True)


def _list_deepseek(key: str) -> list[str]:
    r = requests.get(f"{_DEEPSEEK_BASE}/models",
                     headers={"Authorization": f"Bearer {key}"}, timeout=20)
    r.raise_for_status()
    data = r.json().get("data", [])
    return [d["id"] for d in data if d.get("id")]
