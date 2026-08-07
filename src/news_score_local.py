"""FinBERT — a local, deterministic, look-ahead-free news scorer. DEFAULT OFF.

WHAT IT BUYS YOU, precisely. Not better live entries — see the honest limits
below. The prize is that FinBERT's training cutoff predates any window you
would test on, so unlike a frontier LLM it does NOT know what happened next.
That is the missing half of making news-driven mode backtestable:

    look-ahead-free scorer     ← this module
    point-in-time news archive ← STILL MISSING (Tavily answers "now", never
                                 "as of 2026-03-04"). Until that exists, a
                                 backtest of this mode is not possible, only
                                 a forward-collected factor study.

So today this earns its place as (a) a deterministic second opinion on the same
headlines the LLM reads, logged so LLM-vs-FinBERT disagreement becomes a
measurable thing rather than a hunch, and (b) the scorer a future factor study
will run offline over collected news. It is ADVISORY: it never gates a trade.

HONEST LIMITS. FinBERT scores SENTENCE SENTIMENT, not price impact. "Company
reports record profit" is positive to FinBERT even when the stock drops on a
whisper-number miss. It cannot reason, cannot weigh a catalyst against a
technical setup, and cannot tell a fresh event from a recap. Sentiment is not
alpha; treating this score as a signal on its own would be a mistake.

WHY IT IS NOT A PACKAGED DEPENDENCY. torch + transformers add roughly 1-2 GB to
a frozen bundle. This app ships as a standalone .app that deliberately excludes
matplotlib to save 53 MB, so bundling a deep-learning stack for an advisory
cross-check is not a trade worth making. The import is lazy and every failure
path degrades to "unavailable" — a source install can `pip install
transformers torch` to switch it on, and the .app simply never has it.
"""
from __future__ import annotations

import logging
import threading

from .config import settings

log = logging.getLogger(__name__)

_MODEL_ID = "ProsusAI/finbert"
# FinBERT's label order. Read from the model config at load time rather than
# assumed — a fine-tune with a different ordering would otherwise silently
# invert every score, which is the kind of bug that looks like bad alpha.
_FALLBACK_LABELS = {0: "positive", 1: "negative", 2: "neutral"}

_lock = threading.Lock()
_pipe = None            # (tokenizer, model, id2label) once loaded
_load_failed = ""       # non-empty once we've tried and failed; stops retry storms


def enabled() -> bool:
    return bool(getattr(settings, "finbert_enabled", False))


def available() -> tuple[bool, str]:
    """(usable, detail) without triggering a model download."""
    if not enabled():
        return False, "off"
    if _load_failed:
        return False, _load_failed
    try:
        import transformers  # noqa: F401
        import torch  # noqa: F401
    except Exception as e:
        return False, (f"transformers/torch not installed ({type(e).__name__}) — "
                       "pip install transformers torch")
    return True, "importable"


def _load():
    """Lazy singleton. Returns (tokenizer, model, id2label) or None.

    First call downloads ~440 MB from HuggingFace and caches it under
    ~/.cache/huggingface. That is a deliberate one-time cost the user opted
    into by setting FINBERT_ENABLED; it never happens implicitly.
    """
    global _pipe, _load_failed
    if _pipe is not None:
        return _pipe
    if _load_failed:
        return None
    with _lock:
        if _pipe is not None:
            return _pipe
        if _load_failed:
            return None
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
            model_id = getattr(settings, "finbert_model", "") or _MODEL_ID
            log.info("FinBERT: loading %s (first run downloads ~440MB)", model_id)
            tok = AutoTokenizer.from_pretrained(model_id)
            mdl = AutoModelForSequenceClassification.from_pretrained(model_id)
            mdl.eval()
            torch.set_num_threads(max(1, int(getattr(settings, "finbert_threads", 2))))
            id2label = getattr(mdl.config, "id2label", None) or _FALLBACK_LABELS
            id2label = {int(k): str(v).lower() for k, v in id2label.items()}
            _pipe = (tok, mdl, id2label)
            log.info("FinBERT ready — labels %s", id2label)
            return _pipe
        except Exception as e:
            _load_failed = f"load failed: {e}"
            log.warning("FinBERT unavailable (%s) — scoring disabled for this "
                        "process", _load_failed)
            return None


def score_texts(texts: list[str]) -> tuple[int | None, str]:
    """Aggregate FinBERT bullishness over `texts` → (0-100 score, detail).

    Returns (None, why) whenever it cannot produce a real number — never a
    neutral 50, because a caller must be able to tell "FinBERT says neutral"
    from "FinBERT did not run". Those are different facts and only one of them
    is evidence.

    The 0-100 mapping is mean(P(positive) - P(negative)) over the headlines,
    rescaled from [-1, 1] to [0, 100]. Averaging probabilities rather than
    voting on labels keeps a weak signal weak instead of rounding it to a
    confident-looking verdict.
    """
    texts = [t.strip() for t in (texts or []) if t and t.strip()]
    if not texts:
        return None, "no text to score"
    ok, why = available()
    if not ok:
        return None, why
    loaded = _load()
    if loaded is None:
        return None, _load_failed or "load failed"
    tok, mdl, id2label = loaded
    try:
        import torch
        with torch.no_grad():
            enc = tok(texts, return_tensors="pt", padding=True,
                      truncation=True, max_length=256)
            probs = torch.softmax(mdl(**enc).logits, dim=-1)
        pos_i = next((i for i, l in id2label.items() if l.startswith("pos")), None)
        neg_i = next((i for i, l in id2label.items() if l.startswith("neg")), None)
        if pos_i is None or neg_i is None:
            return None, f"unexpected label set {id2label}"
        net = (probs[:, pos_i] - probs[:, neg_i]).mean().item()
        score = int(round((net + 1.0) * 50.0))
        score = max(0, min(100, score))
        return score, f"finbert n={len(texts)} net={net:+.3f}"
    except Exception as e:
        log.warning("FinBERT scoring failed: %s", e)
        return None, f"scoring failed: {e}"


def score_news(items: list[dict]) -> tuple[int | None, str]:
    """Score already-fetched news dicts (news_fetcher shape: title + content)."""
    texts = []
    for it in items or []:
        title = (it.get("title") or "").strip()
        body = (it.get("content") or "").strip()
        if title or body:
            texts.append(f"{title}. {body}".strip())
    return score_texts(texts)
