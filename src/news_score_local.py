"""FinBERT — a local, deterministic, look-ahead-free news scorer. DEFAULT OFF.

WHAT IT BUYS YOU, precisely. Not better live entries — see the honest limits
below. The prize is that FinBERT's training corpus predates any window you
would test on, so unlike a frontier LLM it does NOT know what happened next.
Together with Finnhub's point-in-time company news (src/finnhub_news.py), that
is what makes a news strategy testable at all rather than only forward-collected.

Today it earns its place as a deterministic second opinion on the same
headlines the LLM reads, logged and persisted so LLM-vs-FinBERT disagreement
becomes measurable rather than a hunch. It is ADVISORY: it never gates a trade.

HONEST LIMITS. FinBERT scores SENTENCE SENTIMENT, not price impact. "Company
reports record profit" is positive to FinBERT even when the stock drops on a
whisper-number miss. It cannot reason, cannot weigh a catalyst against a
technical setup, and cannot tell a fresh event from a recap. Sentiment is not
alpha; treating this score as a signal on its own would be a mistake.

── WHERE THE MODEL LIVES ────────────────────────────────────────────────────
ONE shared directory, deliberately NOT config.ROOT. ROOT is the repo when you
run from source and ~/Library/Application Support/… when frozen, so keying the
model to it would download the same 100-400 MB twice for a user who runs both.
The weights are machine-scoped user data, not per-install state, so they get a
stable per-OS home (see model_home()) that both installs resolve to.

── WHY ONNX RUNTIME AND NOT TORCH ───────────────────────────────────────────
This ships as a standalone .app that excludes matplotlib to save 53 MB. torch +
transformers would add 1-2 GB to that bundle. onnxruntime + tokenizers add
roughly 25 MB, which is what makes "works in the .app too" affordable. If a
source install already has torch + transformers, that path is used instead —
same scores, no second copy of the weights.

── NOTHING DOWNLOADS BY ITSELF ──────────────────────────────────────────────
Setting FINBERT_ENABLED does not start a download. ensure_model() only runs
when something explicitly asks it to (the web panel's confirm dialog, or
FINBERT_AUTO_DOWNLOAD=true for a headless box), because spending a few hundred
MB of someone's disk without asking is not a decision this code gets to make.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

_MODEL_ID = "ProsusAI/finbert"
# The same weights, re-exported to ONNX. ProsusAI publishes the torch checkpoint
# and nothing else — no onnx/ directory and no tokenizer.json — so the ONNX path
# has to pull from the export repo or it 404s on every file it needs. Verified
# 2026-08-08: identical id2label ({0: positive, 1: negative, 2: neutral}) and
# architecture, so the two runtimes score the same sentence the same way.
_ONNX_MODEL_ID = "Xenova/finbert"
# FinBERT's label order. Read from the model config when available rather than
# assumed — a fine-tune with a different ordering would silently invert every
# score, and that bug looks like "the factor doesn't work" rather than a bug.
_FALLBACK_LABELS = {0: "positive", 1: "negative", 2: "neutral"}

# What ensure_model() pulls for the ONNX path. The quantised graph is ~1/4 the
# size of fp32 for a sentiment head whose output we bucket into 0-100 anyway.
_ONNX_FILES = ("onnx/model_quantized.onnx", "tokenizer.json",
               "tokenizer_config.json", "config.json", "vocab.txt")
_ONNX_FALLBACK = "onnx/model.onnx"          # if the quantised graph isn't published
_HF_BASE = "https://huggingface.co/{repo}/resolve/main/{path}"

# Rough, for the consent prompt. Better to state an honest approximation than
# to make someone accept an unbounded download.
EST_DOWNLOAD_MB = 120

_lock = threading.Lock()
_runtime = None          # ("onnx", session, tokenizer, id2label) | ("torch", ...)
_load_failed = ""


# ── where the weights live ───────────────────────────────────────────────────
def model_home() -> Path:
    """One shared location per machine, resolved identically from the .app and
    from a source checkout. FINBERT_HOME overrides for tests / odd setups."""
    env = (os.getenv("FINBERT_HOME", "") or "").strip()
    if env:
        return Path(env).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "MooMooTrader" / "models" / "finbert"
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "MooMooTrader" / "models" / "finbert"
    return home / ".local" / "share" / "MooMooTrader" / "models" / "finbert"


def enabled() -> bool:
    return bool(getattr(settings, "finbert_enabled", False))


def _repo(onnx: bool) -> str:
    """Which HF repo to pull from. The two runtimes need different ones (see
    _ONNX_MODEL_ID), so FINBERT_MODEL only takes over when it has actually been
    pointed somewhere else — it defaults to the torch id, and treating that
    default as a deliberate choice is what sent the ONNX path to a 404."""
    override = (getattr(settings, "finbert_model", "") or "").strip()
    if override and override != _MODEL_ID:
        return override
    return _ONNX_MODEL_ID if onnx else _MODEL_ID


def _onnx_path() -> Path:
    return model_home() / "model.onnx"


def is_downloaded() -> bool:
    p = _onnx_path()
    tok = model_home() / "tokenizer.json"
    return p.exists() and p.stat().st_size > 1_000_000 and tok.exists()


def disk_usage_mb() -> float:
    try:
        return round(sum(f.stat().st_size for f in model_home().rglob("*") if f.is_file())
                     / 1e6, 1)
    except Exception:
        return 0.0


def _torch_available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        return True
    except Exception:
        return False


def _onnx_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        return True
    except Exception:
        return False


def status() -> dict:
    """Everything the UI and preflight need, without triggering a download."""
    onnx_ok, torch_ok = _onnx_available(), _torch_available()
    return {
        "enabled": enabled(),
        "downloaded": is_downloaded(),
        "path": str(model_home()),
        "disk_mb": disk_usage_mb(),
        "est_download_mb": EST_DOWNLOAD_MB,
        "runtime": ("torch" if torch_ok else ("onnx" if onnx_ok else "none")),
        "can_run": torch_ok or onnx_ok,
        # torch installs pull their own weights via transformers' cache, so the
        # explicit download only applies to the ONNX path.
        "needs_download": onnx_ok and not torch_ok and not is_downloaded(),
        "load_error": _load_failed,
    }


def ensure_model(progress=None) -> tuple[bool, str]:
    """Download the ONNX weights + tokenizer into model_home(). (ok, detail).

    NEVER called implicitly — see the module docstring. Downloads to .part
    files and renames on success, so an interrupted run can't leave a truncated
    graph that loads and scores garbage.
    """
    if _torch_available():
        return True, "torch/transformers present — no separate download needed"
    if not _onnx_available():
        return False, ("onnxruntime/tokenizers not installed — "
                       "pip install onnxruntime tokenizers")
    if is_downloaded():
        return True, f"already present ({disk_usage_mb()} MB)"

    import requests
    repo = _repo(onnx=True)
    dest = model_home()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"cannot create {dest}: {e}"

    def _pull(remote: str, local: Path) -> bool:
        url = _HF_BASE.format(repo=repo, path=remote)
        tmp = local.with_suffix(local.suffix + ".part")
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                if r.status_code == 404:
                    return False
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                done = 0
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                        done += len(chunk)
                        if progress:
                            try:
                                progress(local.name, done, total)
                            except Exception:
                                pass
            tmp.replace(local)
            return True
        except Exception as e:
            log.warning("FinBERT download failed for %s: %s", remote, e)
            tmp.unlink(missing_ok=True)
            return False

    # The graph: quantised if published, else the fp32 export.
    if not _pull(_ONNX_FILES[0], _onnx_path()):
        log.info("FinBERT: quantised graph not found, trying %s", _ONNX_FALLBACK)
        if not _pull(_ONNX_FALLBACK, _onnx_path()):
            return False, f"could not download an ONNX graph from {repo}"
    for remote in _ONNX_FILES[1:]:
        # Only tokenizer.json is load-bearing for the tokenizers path; the rest
        # are nice to have (config.json carries id2label), so a miss is not fatal.
        _pull(remote, dest / Path(remote).name)
    if not (dest / "tokenizer.json").exists():
        return False, "tokenizer.json missing — cannot tokenise"
    return True, f"downloaded ({disk_usage_mb()} MB) to {dest}"


def remove_model() -> tuple[bool, str]:
    """Give back the disk. The UI asked for consent to take it; it should be
    able to hand it back without the user hunting for a hidden folder."""
    d = model_home()
    if not d.exists():
        return True, "nothing to remove"
    mb = disk_usage_mb()
    try:
        shutil.rmtree(d)
    except Exception as e:
        return False, f"remove failed: {e}"
    global _runtime, _load_failed
    _runtime, _load_failed = None, ""
    return True, f"removed {mb} MB from {d}"


# ── loading ──────────────────────────────────────────────────────────────────
def _id2label_from_disk() -> dict[int, str]:
    try:
        import json
        cfg = json.loads((model_home() / "config.json").read_text())
        raw = cfg.get("id2label") or {}
        if raw:
            return {int(k): str(v).lower() for k, v in raw.items()}
    except Exception:
        pass
    return dict(_FALLBACK_LABELS)


def _load():
    """Lazy singleton over whichever runtime is present. None on failure."""
    global _runtime, _load_failed
    if _runtime is not None:
        return _runtime
    if _load_failed:
        return None
    with _lock:
        if _runtime is not None:
            return _runtime
        if _load_failed:
            return None
        try:
            if _torch_available():
                import torch
                from transformers import AutoModelForSequenceClassification, AutoTokenizer
                repo = _repo(onnx=False)
                log.info("FinBERT: loading %s via torch", repo)
                tok = AutoTokenizer.from_pretrained(repo)
                mdl = AutoModelForSequenceClassification.from_pretrained(repo)
                mdl.eval()
                torch.set_num_threads(max(1, int(getattr(settings, "finbert_threads", 2))))
                raw = getattr(mdl.config, "id2label", None) or _FALLBACK_LABELS
                _runtime = ("torch", mdl, tok,
                            {int(k): str(v).lower() for k, v in raw.items()})
                log.info("FinBERT ready (torch) — labels %s", _runtime[3])
                return _runtime

            if not _onnx_available():
                _load_failed = ("no runtime — pip install onnxruntime tokenizers "
                                "(or transformers torch)")
                return None
            if not is_downloaded():
                _load_failed = ("model not downloaded — confirm the download in "
                                "Settings, or set FINBERT_AUTO_DOWNLOAD=true")
                return None
            import onnxruntime as ort
            from tokenizers import Tokenizer
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, int(getattr(settings, "finbert_threads", 2)))
            sess = ort.InferenceSession(str(_onnx_path()), opts,
                                        providers=["CPUExecutionProvider"])
            tok = Tokenizer.from_file(str(model_home() / "tokenizer.json"))
            tok.enable_truncation(max_length=256)
            tok.enable_padding()
            _runtime = ("onnx", sess, tok, _id2label_from_disk())
            log.info("FinBERT ready (onnx) — labels %s", _runtime[3])
            return _runtime
        except Exception as e:
            _load_failed = f"load failed: {e}"
            log.warning("FinBERT unavailable (%s) — scoring disabled for this "
                        "process", _load_failed)
            return None


def available() -> tuple[bool, str]:
    """(usable, detail) without loading or downloading anything."""
    if not enabled():
        return False, "off"
    if _load_failed:
        return False, _load_failed
    st = status()
    if not st["can_run"]:
        return False, "no runtime — pip install onnxruntime tokenizers"
    if st["needs_download"]:
        return False, "model not downloaded yet"
    return True, st["runtime"]


# ── scoring ──────────────────────────────────────────────────────────────────
def _softmax_rows(logits) -> list[list[float]]:
    import math
    out = []
    for row in logits:
        m = max(row)
        exps = [math.exp(v - m) for v in row]
        s = sum(exps) or 1.0
        out.append([e / s for e in exps])
    return out


def score_texts(texts: list[str]) -> tuple[int | None, str]:
    """Aggregate FinBERT bullishness over `texts` → (0-100 score, detail).

    Returns (None, why) whenever it cannot produce a real number — never a
    neutral 50, because a caller must be able to tell "FinBERT says neutral"
    from "FinBERT did not run". Those are different facts and only one of them
    is evidence.

    The mapping is mean(P(positive) - P(negative)) rescaled from [-1, 1] to
    [0, 100]. Averaging probabilities rather than voting on labels keeps a weak
    signal weak instead of rounding it into a confident-looking verdict.
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
    kind, model, tok, id2label = loaded

    pos_i = next((i for i, l in id2label.items() if l.startswith("pos")), None)
    neg_i = next((i for i, l in id2label.items() if l.startswith("neg")), None)
    if pos_i is None or neg_i is None:
        return None, f"unexpected label set {id2label}"

    try:
        if kind == "torch":
            import torch
            with torch.no_grad():
                enc = tok(texts, return_tensors="pt", padding=True,
                          truncation=True, max_length=256)
                probs = torch.softmax(model(**enc).logits, dim=-1)
            net = (probs[:, pos_i] - probs[:, neg_i]).mean().item()
        else:
            encs = tok.encode_batch(texts)
            feed = {"input_ids": [e.ids for e in encs],
                    "attention_mask": [e.attention_mask for e in encs]}
            names = {i.name for i in model.get_inputs()}
            if "token_type_ids" in names:
                feed["token_type_ids"] = [e.type_ids for e in encs]
            feed = {k: v for k, v in feed.items() if k in names}
            try:
                import numpy as np
                feed = {k: np.asarray(v, dtype=np.int64) for k, v in feed.items()}
            except Exception:
                pass
            logits = model.run(None, feed)[0]
            rows = _softmax_rows([list(map(float, r)) for r in logits])
            net = sum(r[pos_i] - r[neg_i] for r in rows) / len(rows)
        score = max(0, min(100, int(round((net + 1.0) * 50.0))))
        return score, f"finbert/{kind} n={len(texts)} net={net:+.3f}"
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


def maybe_auto_download() -> None:
    """Headless opt-in path (FINBERT_AUTO_DOWNLOAD=true). Called once at
    start-up so a server install doesn't need someone to click a dialog."""
    if not (enabled() and getattr(settings, "finbert_auto_download", False)):
        return
    if is_downloaded() or _torch_available():
        return
    log.info("FINBERT_AUTO_DOWNLOAD is on — fetching ~%d MB to %s",
             EST_DOWNLOAD_MB, model_home())
    ok, detail = ensure_model()
    log.info("FinBERT auto-download: %s (%s)", "ok" if ok else "FAILED", detail)
