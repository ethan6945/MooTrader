"""Runtime parameter overrides.

An owner-APPROVED param change (from the DeepSeek optimizer via the approval
queue) is written to db-state as `param_<key>` and read here — so it takes
effect on the NEXT scan with no restart, no .env edit. If no override exists,
the frozen .env setting is used (so default behavior is byte-identical until
the owner approves a change).

LIVE-ONLY: the backtest engine uses its own explicit cfg (cfg.threshold,
cfg.tp_atr_mult, …), so these overrides never perturb a backtest measurement.

Two Phase-0 additions (2026-08-10):

  • set_param/revert_param refuse to write while settings.params_frozen. This
    is the single choke point for all seven writer paths — see config.py.

  • push_overrides() gives a parameter sweep an EPHEMERAL, thread-local way to
    say "evaluate as if the params were X" without touching db-state. The grid
    sweep used to inject its combos into live db-state and restore them in a
    finally block; a SIGKILL mid-sweep therefore left a grid combo running the
    live account, which is the most likely origin of the db/param_history
    divergence found in the 2026-08-10 audit. A thread-local cannot outlive the
    process, so that failure mode is now structurally impossible.
"""
from __future__ import annotations

import logging
import threading

from . import db
from .config import settings

log = logging.getLogger(__name__)


class ParamsFrozen(ValueError):
    """Raised by set_param/revert_param while the Phase-0 freeze is active.

    Subclasses ValueError deliberately: every existing caller already catches
    ValueError (approvals, optimizer_ai) or Exception (autopilot, hermes_improve),
    so the freeze degrades them to "queue it / skip it" instead of crashing the
    scheduler.
    """


# Ephemeral, thread-local param overrides — see module docstring. Never
# persisted; a crash or a killed process loses them, which is the point.
_OVERRIDES = threading.local()


def _overrides() -> dict:
    return getattr(_OVERRIDES, "values", None) or {}


def push_overrides(values: dict) -> None:
    """Evaluate as if these params were live, for THIS thread only."""
    _OVERRIDES.values = {str(k): float(v) for k, v in values.items()}


def clear_overrides() -> None:
    _OVERRIDES.values = {}


def frozen() -> bool:
    """True while Phase-0 param freeze is active (PARAMS_FROZEN, default on)."""
    return bool(settings.params_frozen)


# runtime_config's snake_case names, and the parameter file's env-shaped keys.
# Two spellings of one parameter is how this went wrong before; the mapping
# lives here so there is exactly one place that knows both.
_FILE_KEY = {
    "entry_threshold": "ENTRY_SCORE_THRESHOLD", "sl_atr_mult": "SL_ATR_MULT",
    "tp_atr_mult": "TP_ATR_MULT", "risk_per_trade": "RISK_PER_TRADE",
    "max_position_pct": "MAX_POSITION_PCT", "max_hold_days": "MAX_HOLD_DAYS",
    "max_positions": "MAX_POSITIONS", "universe_top_n": "UNIVERSE_TOP_N",
    "tp1_r": "TP1_R", "tp2_r": "TP2_R", "max_gap_pct": "MAX_GAP_PCT",
    "scan_interval_min": "SCAN_INTERVAL_MIN",
}


def params_file():
    from .config import ROOT
    return ROOT / "config" / "parameters.json"


def _read_file() -> dict:
    import json
    try:
        return json.loads(params_file().read_text())
    except (OSError, ValueError):
        return {}


def _param(key: str):
    """The live value of one parameter, or None to fall back to the default.

    Reads config/parameters.json, not db-state. Parameters lived in BOTH for a
    long time — .env plus a param_<key> row — and the row won silently, so
    SL_ATR_MULT=2.8 in .env while the bot traded 3.5. One store now.
    """
    ov = _overrides()
    if key in ov:
        return ov[key]
    fk = _FILE_KEY.get(key)
    if not fk:
        return None
    return (_read_file().get("params") or {}).get(fk)


def entry_threshold() -> float:
    v = _param("entry_threshold")
    try:
        return float(v) if v is not None else settings.entry_threshold
    except (TypeError, ValueError):
        return settings.entry_threshold


def tp_atr_mult() -> float:
    v = _param("tp_atr_mult")
    try:
        return float(v) if v is not None else settings.tp_atr_mult
    except (TypeError, ValueError):
        return settings.tp_atr_mult


def sl_atr_mult() -> float:
    v = _param("sl_atr_mult")
    try:
        return float(v) if v is not None else settings.sl_atr_mult
    except (TypeError, ValueError):
        return settings.sl_atr_mult


def risk_per_trade() -> float:
    v = _param("risk_per_trade")
    try:
        return float(v) if v is not None else settings.risk_per_trade
    except (TypeError, ValueError):
        return settings.risk_per_trade


def breakeven_trigger_r() -> float:
    v = _param("breakeven_trigger_r")
    try:
        return float(v) if v is not None else settings.breakeven_trigger_r
    except (TypeError, ValueError):
        return settings.breakeven_trigger_r


def max_hold_days() -> int:
    v = _param("max_hold_days")
    try:
        return int(float(v)) if v is not None else settings.max_hold_days
    except (TypeError, ValueError):
        return settings.max_hold_days


def universe_top_n() -> int:
    v = _param("universe_top_n")
    try:
        return int(float(v)) if v is not None else settings.universe_top_n
    except (TypeError, ValueError):
        return settings.universe_top_n


def max_position_pct() -> float:
    v = _param("max_position_pct")
    try:
        return float(v) if v is not None else settings.max_position_pct
    except (TypeError, ValueError):
        return settings.max_position_pct


# Whitelist of keys the optimizer is allowed to propose (guards against a bad
# LLM proposal touching something dangerous). Values are (min, max) sanity bounds.
# 2026-06-11: extended beyond threshold/tp/sl/risk — the exit audit measured all
# four at their plateau, so the levers with remaining evidence-backed movement
# (breakeven trigger, hold window, universe breadth) are now reachable too.
# Position cap / budget / regime multiplier stay OWNER-ONLY by design.
ALLOWED_PARAMS = {
    "entry_threshold": (55.0, 85.0),
    "tp_atr_mult": (2.0, 12.0),
    "sl_atr_mult": (2.0, 6.0),
    "risk_per_trade": (0.01, 0.08),
    "breakeven_trigger_r": (0.75, 1.5),
    "max_hold_days": (5.0, 10.0),
    "universe_top_n": (10.0, 20.0),
    # 2026-08-10: floor lowered 0.20 → 0.10 to match the human baseline. With
    # the floor above the baseline, every proposal the optimizer could make
    # would necessarily RAISE single-name concentration — a one-way ratchet
    # away from the value a human just chose. The ablation numbers below came
    # off the same engine that disagrees with the live path by 83.5% of net
    # PnL, so treat them as untested until parity is fixed.
    # 2026-06-12 cap ablation: 30%=$12.8 / 40%=$22.1 / 50%=$17.6 / 70%=$19.2
    # per day — 40-70 statistically flat in-sample, so the optimizer may tune
    # within [0.20, 0.55]. The 0.55 ceiling is deliberate and NOT tunable: a
    # backtest can never price the overnight single-name gap (the window
    # contains no blowup), so the upper bound is the tail-risk guard the
    # in-sample gate structurally cannot provide.
    "max_position_pct": (0.10, 0.55),
}


def is_valid(key: str, value: float) -> bool:
    if key not in ALLOWED_PARAMS:
        return False
    lo, hi = ALLOWED_PARAMS[key]
    try:
        return lo <= float(value) <= hi
    except (TypeError, ValueError):
        return False


def current(key: str):
    """Runtime-effective value of an ALLOWED param (override beats .env)."""
    return {
        "entry_threshold": entry_threshold,
        "tp_atr_mult": tp_atr_mult,
        "sl_atr_mult": sl_atr_mult,
        "risk_per_trade": risk_per_trade,
        "breakeven_trigger_r": breakeven_trigger_r,
        "max_hold_days": max_hold_days,
        "universe_top_n": universe_top_n,
        "max_position_pct": max_position_pct,
    }[key]()


def history_file():
    from .config import ROOT
    return ROOT / "config" / "parameters_history.jsonl"


def history(key: str | None = None) -> list[dict]:
    """The append-only parameter journal, oldest first.

    THE JOURNAL MOVED AND TWO READERS DID NOT. Changes are written to
    config/parameters_history.jsonl by _write_param; the db-state
    `param_history` list is the store they used to live in and has received
    nothing since 2026-08-17. revert_param and autopilot.check_and_rollback
    both still looked there, so the auto-rollback was scanning a log that had
    stopped growing — silently, because an empty scan looks exactly like
    "nothing needs rolling back".
    """
    import json
    f = history_file()
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if key is None or rec.get("key") == key:
            out.append(rec)
    return out


def last_change(key: str) -> dict | None:
    """The newest journalled change for `key` — the one currently in force.

    An append-only log needs no mutable `active` flag: the last entry for a key
    IS the active one. The old db list carried `active` booleans that had to be
    flipped by hand, and two parameters ended up live with no active record at
    all — two of the four contradictions config_baseline used to report.
    """
    recs = history(key)
    return recs[-1] if recs else None


def _write_param(key: str, value: float, rec: dict) -> None:
    """Persist one parameter to the file, and journal the change beside it.

    Written whole and atomically — a half-written parameters.json would take
    every parameter with it, not just the one being changed.

    The journal is a separate append-only file rather than a list inside the
    same document. param_history used to live in db-state next to the values it
    described, capped at fifty entries, and the two drifted: two parameters were
    live with no active record at all, which is two of the four contradictions
    config_baseline reports. A log that cannot be rewritten by the thing it
    audits is worth more than one that can.
    """
    import json, os as _os, tempfile
    from .config import ROOT
    f = params_file()
    # The directory may not exist yet — a fresh checkout, or a run pointed at a
    # new MMT_HOME. The atomic write needs its temp file as a SIBLING (rename
    # is only atomic within one filesystem), so the directory has to be there
    # before the temp file, not after.
    f.parent.mkdir(parents=True, exist_ok=True)
    doc = _read_file() or {"version": 1, "params": {}}
    fk = _FILE_KEY.get(key, key.upper())
    doc.setdefault("params", {})[fk] = str(value)
    tmp = tempfile.NamedTemporaryFile("w", dir=str(f.parent), delete=False,
                                      suffix=".tmp", encoding="utf-8")
    try:
        json.dump(doc, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
        tmp.flush()
        _os.fsync(tmp.fileno())
        tmp.close()
        _os.replace(tmp.name, f)
    except BaseException:
        try:
            _os.unlink(tmp.name)
        except OSError:
            pass
        raise
    # The environment Settings was built from, so a change lands on this scan
    # rather than at the next restart.
    _os.environ[fk] = str(value)
    try:
        with open(ROOT / "config" / "parameters_history.jsonl", "a",
                  encoding="utf-8") as h:
            h.write(json.dumps({**rec, "file_key": fk}, default=str) + "\n")
    except OSError as e:
        log.warning("parameter change applied but not journalled: %s", e)


def set_param(key: str, value: float, source: str, force: bool = False) -> dict:
    """Single write path for runtime param changes (approval executor AND the
    bounded-autonomy auto-apply both come through here). Validates against
    ALLOWED_PARAMS, records the change in the param_history db-state list
    (which powers the autopilot's auto-rollback), and returns the record.
    Raises ValueError on an out-of-bounds/unknown key.

    Raises ParamsFrozen while the Phase-0 freeze is on. `force=True` is for the
    human re-baselining tool only (scripts/config_baseline.py) — no automated
    caller may pass it."""
    if frozen() and not force:
        raise ParamsFrozen(
            f"param freeze active (PARAMS_FROZEN) — refused {key}={value} "
            f"from {source!r}. Parity must pass before automated tuning resumes."
        )
    if not is_valid(key, value):
        raise ValueError(f"param {key}={value} outside ALLOWED_PARAMS bounds")
    old = current(key)
    from datetime import datetime, timezone
    rec = {"key": key, "old": old, "new": float(value), "source": source,
           "applied_at": datetime.now(timezone.utc).isoformat(),
           "active": True}
    _write_param(key, float(value), rec)
    # 2026-07-06: re-validate scale-out guard with runtime SL/TP values
    # (the module-level guard in config.py uses static .env values and
    # can't see runtime overrides — this plugs the gap).
    if key in ("sl_atr_mult", "tp_atr_mult"):
        _recheck_scale_out()
    return rec


def revert_param(key: str, reason: str, force: bool = False) -> dict | None:
    """Auto-rollback: restore the previous value of the most recent ACTIVE
    change for `key`. Marks the history record rolled_back; returns it (or
    None if there was nothing active to revert).

    Also frozen in Phase 0: an automated rollback is still an automated write,
    and with the freeze on there is nothing for it to roll back anyway.

    Reads the append-only journal (see history()). It used to read the
    db-state `param_history` list, which stopped receiving writes when the
    journal moved to a file — so it found nothing to revert and said so, which
    is indistinguishable from there being nothing wrong.

    The write goes through _write_param, so a rollback is journalled like any
    other change and the next reader sees the reverted value as current.
    """
    if frozen() and not force:
        raise ParamsFrozen(
            f"param freeze active (PARAMS_FROZEN) — refused rollback of {key}"
        )
    rec = last_change(key)
    if rec is None:
        return None
    if str(rec.get("source", "")).startswith("rollback:"):
        return None          # already reverted; do not roll back a rollback
    old = rec.get("old")
    if old is None:
        return None
    from datetime import datetime, timezone
    _write_param(key, float(old), {
        "key": key, "old": rec.get("new"), "new": float(old),
        "source": f"rollback: {reason}"[:200],
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "active": True})
    return {**rec, "rolled_back": True, "rollback_reason": reason}


# ── scale-out re-validation (2026-07-06) ──────────────────

def _recheck_scale_out() -> None:
    """Re-validate the scale-out guard with current runtime SL/TP values.
    Called automatically from set_param() when sl_atr_mult or tp_atr_mult
    changes. Uses lazy import to avoid circular dependency at module load."""
    try:
        from .config import recheck_scale_out
        sl = sl_atr_mult()
        tp = tp_atr_mult()
        recheck_scale_out(sl, tp)
    except Exception:
        pass  # scale-out guard is advisory; never crash on it


# ── where a setting actually lives ───────────────────────────────────────────

def _env_path():
    from .config import ROOT
    return ROOT / ".env"


def _env_keys() -> set[str]:
    p = _env_path()
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            out.add(s.split("=", 1)[0].strip())
    return out


def owning_store(key: str) -> str:
    """Which file decides `key` — "parameters" or "env".

    config._load_parameters overlays config/parameters.json onto os.environ
    AFTER load_dotenv, and overwrites. So when a key is in both files, the
    parameter file wins and the .env line is decoration.

    A writer that does not know this writes to the losing file, reports
    success, and changes nothing. That is what happened to the panel's
    strategy-mode selector — the one control the settings page labels as the
    only one that changes the strategy. It wrote STRATEGY_MODE=technical to
    .env while parameters.json said news, the panel showed "restart to apply",
    the restart applied nothing, and the banner explaining why could never
    clear.

    It is also, exactly, the split the parameter migration was carried out to
    end: _load_parameters' own docstring describes SL_ATR_MULT=2.8 sitting in
    .env while the bot traded 3.5. Same bug, new pair of files, introduced by
    the fix for the old pair.
    """
    from .config import ROOT
    import json as _json
    try:
        params = _json.loads((ROOT / "config" / "parameters.json").read_text())
        if key in (params.get("params") or {}):
            return "parameters"
    except (OSError, ValueError):
        pass
    return "env"


def write_setting(key: str, value: str, source: str = "panel") -> str:
    """Write a configuration key to the store that decides it. Returns which.

    The single choke point for every non-credential write. Callers do not get
    to pick the file, because picking it is the thing that goes wrong.

    Writing to the parameter file also strips any stale .env line for the same
    key, so the two cannot disagree again the moment someone reads .env to
    find out what the bot is doing.
    """
    key = str(key)
    value = "" if value is None else str(value)
    store = owning_store(key)

    if store == "parameters":
        _write_param_raw(key, value, source)
        _strip_env_key(key)
        _reset_redaction_if_secret(key)
        return "parameters"

    _write_env_raw(key, value)
    # The parameter branch above updates os.environ; this one must too, or the
    # process that just wrote the value cannot see it. That is not cosmetic:
    # log_redact builds its secret list from the live environment, so a key
    # pasted into the panel would not be redacted until the next restart —
    # while the panel's own preflight probe is already sending it.
    _os_environ()[key] = value
    _reset_redaction_if_secret(key)
    return "env"


def _write_param_raw(file_key: str, value: str, source: str) -> None:
    """Set a parameter BY ITS FILE KEY, bypassing the tunable-bounds path.

    set_param() is for the numeric strategy tunables: it checks ALLOWED_PARAMS
    bounds and honours PARAMS_FROZEN. A mode switch is neither — freezing the
    strategy tunables must not weld the simulate/live and strategy-mode
    controls shut — so this writes the file directly and journals it the same
    way, with the source recorded.
    """
    import json, os as _os, tempfile
    from .config import ROOT
    f = params_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    doc = _read_file() or {"version": 1, "params": {}}
    old = (doc.get("params") or {}).get(file_key)
    doc.setdefault("params", {})[file_key] = value
    tmp = tempfile.NamedTemporaryFile("w", dir=str(f.parent), delete=False,
                                      suffix=".tmp", encoding="utf-8")
    try:
        json.dump(doc, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
        tmp.flush()
        _os.fsync(tmp.fileno())
        tmp.close()
        _os.replace(tmp.name, f)
    except BaseException:
        try:
            _os.unlink(tmp.name)
        except OSError:
            pass
        raise
    _os.environ[file_key] = value
    try:
        with open(ROOT / "config" / "parameters_history.jsonl", "a",
                  encoding="utf-8") as h:
            h.write(json.dumps({"key": file_key, "file_key": file_key,
                                "old": old, "new": value, "source": source,
                                "applied_at": _now_iso(), "active": True}) + "\n")
    except OSError as e:
        log.warning("setting applied but not journalled: %s", e)


def _write_env_raw(key: str, value: str) -> None:
    p = _env_path()
    lines = p.read_text().splitlines() if p.exists() else []
    for i, l in enumerate(lines):
        st = l.strip()
        if st.startswith(key + "=") or st.startswith("#" + key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    p.write_text("\n".join(lines) + "\n")


def _strip_env_key(key: str) -> None:
    """Remove a key's line from .env — it has a home, and this is not it.

    Takes the key's trailing comment block with it. A `.env` line often
    carries an indented rationale on the lines below; removing only the
    assignment left those floating with nothing to explain, and after a dozen
    strips the file was mostly orphaned commentary and blank runs. One of
    those orphans read "the system is unified on Gemini 3.5 Flash" long after
    it was not — which is the file someone opens to find out what the bot
    uses.
    """
    p = _env_path()
    if not p.exists():
        return
    lines = p.read_text().splitlines()
    out, i, hit = [], 0, False
    while i < len(lines):
        st = lines[i].strip()
        if st.startswith(key + "=") or st.startswith("#" + key + "="):
            hit = True
            i += 1
            # Its continuation comments: INDENTED # lines directly below. A
            # comment at column 0 introduces the next thing, not this one.
            while i < len(lines) and lines[i][:1] in (" ", "\t") \
                    and lines[i].strip().startswith("#"):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if not hit:
        return
    # Collapse the blank runs the removals leave behind.
    tidy, blanks = [], 0
    for l in out:
        blanks = blanks + 1 if not l.strip() else 0
        if blanks <= 1:
            tidy.append(l)
    p.write_text("\n".join(tidy).rstrip() + "\n")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _os_environ():
    import os
    return os.environ


def _reset_redaction_if_secret(key: str) -> None:
    """Rebuild the log-redaction secret list when a credential changes.

    log_redact.reset_cache()'s own docstring names this case — "after a
    rotation, or in tests" — and until now only the tests called it. A rotated
    key was therefore unredacted for the life of the process, and the panel
    probes a freshly-pasted key immediately.

    Here rather than in the panel because the panel is not the only writer:
    hermes_improve reaches this function too, and the next writer will not
    read the panel's source to find out what it forgot.
    """
    if not any(w in key.upper() for w in
               ("KEY", "TOKEN", "SECRET", "PASSWORD", "PWD", "ACCOUNT")):
        return
    try:
        from . import log_redact
        log_redact.reset_cache()
    except Exception as e:
        log.warning("could not refresh the log-redaction cache after "
                    "writing %s — a rotated credential may appear in logs "
                    "until restart: %s", key, e)
