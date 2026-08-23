#!/usr/bin/env python3
"""Phase-0 config baseline — what is ACTUALLY in effect, and where it came from.

Read-only. Writes nothing, changes nothing. Run from repo root:

    .venv/bin/python scripts/config_baseline.py
    .venv/bin/python scripts/config_baseline.py --json    # machine-readable

WHY THIS EXISTS
  A strategy param can be set in four places, and they disagree today:

    1. db-state `param_<key>`   — runtime override, beats everything
    2. `.env`                   — what the owner thinks is configured
    3. code default (config.py) — what a fresh install runs
    4. README table             — what a NEW USER is told they get. Compared
                                  against .env.example, never against this
                                  machine's effective values: those are one
                                  owner's weekly tuning and are supposed to
                                  diverge from the shipped reference.

  The 2026-08-10 audit found `param_sl_atr_mult=3.0` live in db while its
  `param_history` record says `active: false`, and `param_max_position_pct=0.36`
  live with NO provenance record at all. The cause was optimize_system._inject
  writing `param_*` directly, bypassing set_param's audit trail (now fixed —
  see scripts/test_param_freeze.py), but the divergence it left behind is still
  in the database.

  A human baseline cannot be established on top of two records that contradict
  each other. This script names every contradiction so they can be resolved one
  by one, in the open.

EXIT CODE
  0 = effective config is internally consistent
  1 = at least one contradiction needs a human decision
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import db, runtime_config                       # noqa: E402
from src.config import settings                          # noqa: E402
from src.hermes_improve import _RUNTIME_KEY_MAP          # noqa: E402


# runtime key → (.env var name, settings attribute)
TRACKED = {
    "entry_threshold":     ("ENTRY_SCORE_THRESHOLD", "entry_threshold"),
    "tp_atr_mult":         ("TP_ATR_MULT", "tp_atr_mult"),
    "sl_atr_mult":         ("SL_ATR_MULT", "sl_atr_mult"),
    "risk_per_trade":      ("RISK_PER_TRADE", "risk_per_trade"),
    "breakeven_trigger_r": ("BREAKEVEN_TRIGGER_R", "breakeven_trigger_r"),
    "max_hold_days":       ("MAX_HOLD_DAYS", "max_hold_days"),
    "universe_top_n":      ("UNIVERSE_TOP_N", "universe_top_n"),
    "max_position_pct":    ("MAX_POSITION_PCT", "max_position_pct"),
}


def _num(v):
    """Best-effort float, else None — comparisons must not crash on prose."""
    try:
        return float(str(v).split("#")[0].strip())
    except (TypeError, ValueError):
        return None


def _same(a, b) -> bool:
    fa, fb = _num(a), _num(b)
    if fa is None or fb is None:
        return str(a).strip() == str(b).strip()
    return abs(fa - fb) < 1e-9


def read_env_file(name: str = ".env") -> dict:
    """Parse an env file directly. settings.* already folds in code defaults, so
    it cannot tell 'owner set this' apart from 'nobody set it'."""
    out = {}
    p = ROOT / name
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.split("#")[0].strip()
    return out


def read_readme_table() -> dict:
    """Pull the 'Key numbers' table out of README.md — two param/value pairs
    per row: | `KEY` | val | `KEY` | val |"""
    out = {}
    p = ROOT / "README.md"
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        if not line.startswith("|"):
            continue
        for k, v in re.findall(r"`([A-Z_]+)`\s*\|\s*([^|]+?)\s*\|", line):
            # Values are sometimes code-quoted (`15`) and sometimes bare (15) —
            # strip the backticks so 15 and `15` don't read as a disagreement.
            out[k] = v.strip().strip("`").strip()
    return out


def provenance(history: list, key: str) -> dict:
    """What param_history says about this key: the active record, plus counts."""
    recs = [h for h in history if h.get("key") == key]
    active = [h for h in recs if h.get("active")]
    return {
        "records": len(recs),
        "active_record": active[-1] if active else None,
        "last_record": recs[-1] if recs else None,
    }


# Keys that must not drift between .env and .env.example. A fresh install (and
# the packaged .app on first run) gets .env.example, so any disagreement here
# means a new user silently runs a different strategy than the one that was
# tested. On 2026-08-10, 13 of these 15 disagreed: threshold 60 vs 70, TP 3.0
# vs 10.0, 10 concurrent positions vs 5, single-name cap 40% vs 10%.
EXAMPLE_MUST_MATCH = [
    "ACCOUNT_USD", "RISK_PER_TRADE", "MAX_POSITIONS", "MAX_POSITION_PCT",
    "DAILY_DRAWDOWN_STOP", "DD_HALT_PCT", "ENTRY_SCORE_THRESHOLD",
    "SCAN_INTERVAL_MIN", "MAX_HOLD_DAYS", "TP_ATR_MULT", "SL_ATR_MULT",
    "PARAMS_FROZEN", "AUTO_APPLY_PARAMS", "AUTO_BUDGET_ENABLED",
    "MAX_POSITIONS_AUTOSCALE",
]


# Risk / safety switches that are NOT runtime-tunable, so nothing above tracks
# them — yet each one changes how much the bot can lose. For these we check the
# CODE DEFAULT too: the value a packaged install runs when .env is absent, which
# is the case the .env-vs-.env.example comparison cannot see at all.
# (env var, settings attribute, expected-safe default)
RISK_SWITCHES = [
    # PARAMS_FROZEN's job moved (2026-08-23). It was the global choke point that
    # stopped every automated write; it is now off for this deployment, and what
    # replaced it is structural: approvals.py is the ONLY caller of set_param,
    # so nothing reaches live without an approval. check_sole_param_writer()
    # below guards that, and it is a stronger property than the flag ever was —
    # the flag could be turned off, the call sites cannot appear by accident.
    # Listed with expected False so this file states the current design rather
    # than failing forever against a decision that was made deliberately.
    ("PARAMS_FROZEN",            "params_frozen",            False),
    ("AUTO_APPLY_PARAMS",        "auto_apply_params",        False),
    ("AUTO_BUDGET_ENABLED",      "auto_budget_enabled",      False),
    ("MAX_POSITIONS_AUTOSCALE",  "max_positions_autoscale",  False),
    ("MR_ENABLED",               "mr_enabled",               False),
    ("PATTERN_ENABLED",          "pattern_enabled",          False),
    ("SMART_EXIT_ENABLED",       "smart_exit_enabled",       False),
    ("STALL_OUT_ENABLED",        "stall_out_enabled",        False),
    ("INVERSE_SLEEVE_ENABLED",   "inverse_sleeve_enabled",   False),
    ("CASH_YIELD_ENABLED",       "cash_yield_enabled",       False),
    ("OPTIONS_FLOW_ENABLED",     "options_flow_enabled",     False),
    ("SENTIMENT_SIZING",         "sentiment_sizing",         False),
    ("OPTIONS_STATS_SIZING",     "options_stats_sizing",     False),
    ("USE_SCALE_OUT",            "use_scale_out",            False),
]


def code_default(attr: str):
    """The value settings.<attr> takes with the env var absent — i.e. what a
    fresh packaged install actually runs. Re-imports config with that one
    variable stripped, so it reads the real dataclass default rather than a
    hand-maintained copy of it that could itself drift."""
    import importlib
    import os as _os
    env_name = next((e for e, a, _ in RISK_SWITCHES if a == attr), None)
    saved = _os.environ.pop(env_name, None) if env_name else None
    try:
        import src.config as _cfg
        reloaded = importlib.reload(_cfg)
        return getattr(reloaded.settings, attr, None)
    finally:
        if saved is not None:
            _os.environ[env_name] = saved
        import src.config as _cfg2
        importlib.reload(_cfg2)


# Modules allowed to call runtime_config.set_param(). Exactly one: the
# approval executor. Anything else is an automated path to live.
_ALLOWED_PARAM_WRITERS = {"src/approvals.py"}


def check_sole_param_writer() -> list[dict]:
    """Only the approval executor may write a live parameter.

    This replaces PARAMS_FROZEN as the thing that makes "live never changes by
    itself" true. Three modules used to call set_param directly — autopilot
    (DeepSeek, inside hardcoded guardrails), optimizer_ai (gated on a config
    flag), and hermes_improve. The freeze was what actually stopped them, so
    lifting it without moving them would have re-armed all three at once.
    """
    # Parsed, not grepped. A docstring that MENTIONS set_param is prose about
    # the design; only a call node is a path to live.
    import ast as _ast
    out, offenders = [], []
    for path in sorted((ROOT / "src").glob("*.py")):
        rel = f"src/{path.name}"
        if rel in _ALLOWED_PARAM_WRITERS:
            continue
        try:
            tree = _ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            fn = node.func
            if (isinstance(fn, _ast.Attribute) and fn.attr == "set_param"
                    and isinstance(fn.value, _ast.Name)
                    and fn.value.id in ("runtime_config", "rc")):
                offenders.append(f"{rel}:{node.lineno}")
    if offenders:
        out.append({
            "severity": "high", "key": "PARAM_WRITE_PATH",
            "what": f"set_param called outside the approval executor: "
                    f"{', '.join(offenders)}",
            "why": "With PARAMS_FROZEN off, a direct set_param call changes live "
                   "parameters with nobody approving it. Owner requirement: "
                   "every parameter change waits for an approval.",
            "action": "route it through approvals.enqueue(kind='param_change') "
                      "instead, or add it to _ALLOWED_PARAM_WRITERS deliberately",
        })
    return out


def check_risk_switches(env: dict, example: dict) -> list[dict]:
    """Every risk switch: is the CODE DEFAULT safe, and do the two env files
    agree? A switch missing from .env runs its code default silently."""
    out = []
    for env_name, attr, safe_default in RISK_SWITCHES:
        try:
            actual_default = code_default(attr)
        except Exception as e:
            out.append({"severity": "low", "key": env_name,
                        "what": f"could not read the code default ({e})",
                        "why": "cannot confirm what a fresh install runs",
                        "action": f"check settings.{attr} in src/config.py"})
            continue
        if actual_default is not None and bool(actual_default) != bool(safe_default):
            out.append({
                "severity": "high", "key": env_name,
                "what": f"CODE DEFAULT for {env_name} is {actual_default}, "
                        f"expected {safe_default}",
                "why": "This is what a packaged install with no .env runs. A "
                       "risk switch defaulting on ships enabled to people who "
                       "never chose it.",
                "action": f"change the default of settings.{attr} in src/config.py "
                          f"to {safe_default}",
            })
        have, want = env.get(env_name), example.get(env_name)
        if have is not None and want is not None and not _same(have, want):
            out.append({
                "severity": "medium", "key": env_name,
                "what": f"{env_name}: .env has {have}, .env.example has {want}",
                "why": "A fresh install would run with a different risk switch.",
                "action": f"set {env_name}={have} in .env.example",
            })
    return out


def check_budget_baseline(state: dict, env: dict) -> list[dict]:
    """The drawdown breaker measures equity against `peak_equity`. If the budget
    moves and the peak does not, the breaker silently stops working — upward it
    pins drawdown at 0% forever, downward it reads a phantom drawdown and halts
    everything. Both have happened here."""
    out = []
    try:
        from src import risk_manager
        peak = float(state.get("peak_equity") or 0.0)
        budget = risk_manager.budget_usd()
        realized = float(state.get("realized_pnl_total") or 0.0)
        equity = risk_manager.equity_baseline() + realized
    except Exception as e:
        return [{"severity": "medium", "key": "peak_equity",
                 "what": f"could not evaluate the drawdown baseline ({e})",
                 "why": "the DD breaker cannot be verified",
                 "action": "investigate before trading"}]

    if peak <= 0:
        out.append({
            "severity": "medium", "key": "peak_equity",
            "what": "peak_equity is unset",
            "why": "current_drawdown_pct() returns 0 with no peak, so the DD "
                   "breaker cannot fire at all.",
            "action": "run risk_manager.set_budget(<budget>, 'baseline') to anchor it",
        })
    elif equity > peak * 1.001:
        out.append({
            "severity": "high", "key": "peak_equity",
            "what": f"equity ${equity:,.0f} already exceeds peak_equity "
                    f"${peak:,.0f} — the peak is stale, from a smaller budget",
            "why": "Drawdown is measured against this peak, so it pins at 0.0% "
                   "and the breaker can never trip until the account falls "
                   "below the OLD, smaller base.",
            "action": "risk_manager.set_budget(<budget>, 'reanchor') — it "
                      "re-anchors the peak as part of setting the budget",
        })

    # ACCOUNT_USD is the fallback when the db key is missing. If they disagree,
    # losing or resetting db-state silently changes deployable capital.
    env_budget = _num(env.get("ACCOUNT_USD"))
    if env_budget is not None and abs(env_budget - budget) > 1e-6:
        out.append({
            "severity": "medium", "key": "ACCOUNT_USD",
            "what": f"ACCOUNT_USD={env_budget:,.0f} in .env but live budget is "
                    f"${budget:,.0f} (db-state budget_usd)",
            "why": "ACCOUNT_USD is the fallback if the db key is ever missing; "
                   "while they disagree, a db reset silently changes capital.",
            "action": f"set ACCOUNT_USD={budget:,.0f} in .env",
        })
    return out


def check_no_constant_false_gates(_env: dict) -> list[dict]:
    """A parameter whose readers all sit behind a constant-false gate.

    check_parameter_file_is_live asks whether anything READS a key. That is
    too weak, and GEMINI_MODEL walked through it: settings.gemini_model was
    read by ai.default_model(), on the branch `provider == "deepseek" else
    gemini_model` — a branch active_provider() can never take, because
    ai.PROVIDERS is ("deepseek",). Read, and unreachable. It then appeared in
    the parameter console as a live setting, which is what the owner saw.

    "Something reads it" is not "changing it does something".

    Detecting reachability in general is not on the table. Detecting THIS
    shape is: a module-level constant collection, and a membership test
    against it that decides whether a parameter's reader runs. So this finds
    the constants and reports the literals that can never be members.
    """
    out = []
    for pyf in sorted((ROOT / "src").glob("*.py")):
        try:
            tree = ast.parse(pyf.read_text())
        except (OSError, SyntaxError):
            continue
        consts = {}
        for n in tree.body:
            if not isinstance(n, ast.Assign) or len(n.targets) != 1:
                continue
            t = n.targets[0]
            if not (isinstance(t, ast.Name) and t.id.isupper()):
                continue
            if isinstance(n.value, (ast.Tuple, ast.List, ast.Set)):
                vals = [e.value for e in n.value.elts
                        if isinstance(e, ast.Constant)]
                if vals and all(isinstance(v, str) for v in vals):
                    consts[t.id] = set(vals)
        if not consts:
            continue
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Compare) and len(n.ops) == 1
                    and isinstance(n.ops[0], ast.In)):
                continue
            right = n.comparators[0]
            if not (isinstance(right, ast.Name) and right.id in consts):
                continue
            left = n.left
            if not (isinstance(left, ast.Constant) and isinstance(left.value, str)):
                continue
            if left.value in consts[right.id]:
                continue
            out.append({
                "severity": "high", "key": f"{pyf.name}:{n.lineno}",
                "what": (f'`"{left.value}" in {right.id}` is always False — '
                         f"{right.id} holds {sorted(consts[right.id])}"),
                "why": ("Everything this gate protects is unreachable, "
                        "including any setting only read inside it. That is "
                        "how GEMINI_MODEL survived as an editable parameter "
                        "for a provider that could not be selected."),
                "action": ("delete the branch and whatever only it reads, or "
                           f"put {left.value!r} back in {right.id}"),
            })
    return out


def check_one_store_per_key(env: dict) -> list[dict]:
    """No key may live in both .env and config/parameters.json.

    config._load_parameters overlays the parameter file onto os.environ AFTER
    load_dotenv and OVERWRITES. A key in both files therefore has a winner and
    a decoration, and nothing on either line says which is which.

    This is not hypothetical and it is not old. On 2026-08-21 the settings
    panel wrote STRATEGY_MODE=technical and NEWS_DRIVEN_ENABLED=false into
    .env while parameters.json held news/true. The panel reported success and
    said "restart to apply". The restart applied nothing, the bot went on
    selecting trades from news, and the banner explaining the discrepancy
    could never clear because the file it compared against was the losing one.

    It is the same split the parameter migration existed to end — .env said
    SL_ATR_MULT=2.8 while the bot traded 3.5 — reintroduced by that migration
    between a new pair of files. Which is why this asserts the invariant
    rather than any particular key.
    """
    try:
        params = json.loads(
            (ROOT / "config" / "parameters.json").read_text()).get("params") or {}
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for key in sorted(set(env) & set(params)):
        same = str(env[key]).strip().lower() == str(params[key]).strip().lower()
        out.append({
            "severity": "medium" if same else "high", "key": key,
            "what": (f"{key} is in BOTH .env ({env[key]}) and parameters.json "
                     f"({params[key]})" + ("" if same else " — AND THEY DISAGREE")),
            "why": ("parameters.json is overlaid after load_dotenv and wins. "
                    + ("The values agree today, so nothing is broken yet — but "
                       "editing the .env line will look like it worked and do "
                       "nothing."
                       if same else
                       "The bot is running the parameters.json value. Anyone "
                       "reading .env to find out what it does is being told "
                       "the wrong thing.")),
            "action": (f"remove {key} from .env — runtime_config.write_setting "
                       f"routes writes to the owning store, so the panel will "
                       f"keep working"),
        })
    return out


def check_parameter_file_is_live(_env: dict) -> list[dict]:
    """Every key in config/parameters.json must be read by something.

    The 2026-08-21 migration seeded the parameter file from the EFFECTIVE
    environment. That environment still carried NEWS_DRIVEN_SHADOW=true — a
    setting deleted from the code a release earlier, whose own CHANGELOG entry
    warns that "a configuration relying on it to hold orders back will start
    placing orders". The migration wrote it into the new file, where it read as
    a live safety switch set to the safe value, and was nothing.

    That is the shape this repository keeps finding: apply_sector_gate naming a
    MAX_PER_SECTOR that never existed, apply_ml_gate logged as search-engine
    fidelity with no ML behind it, SandboxConfig.tickers accepted and ignored.
    A retired setting resurrected into the file people READ to learn what the
    bot does is the same fault with better placement.
    """
    f = ROOT / "config" / "parameters.json"
    try:
        params = json.loads(f.read_text()).get("params") or {}
    except (OSError, json.JSONDecodeError) as e:
        return [{"severity": "high", "key": "parameters.json",
                 "what": f"could not be read ({e})",
                 "why": "it is where every strategy parameter now lives",
                 "action": f"repair {f}"}]

    # Strip docstrings and comments before looking: a key named only in prose
    # is not a key anything consults.
    code = []
    for d in ("src", "web", "packaging"):
        for pyf in (ROOT / d).rglob("*.py"):
            try:
                t = ast.parse(pyf.read_text())
            except (OSError, SyntaxError):
                continue
            for n in ast.walk(t):
                if (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                        and isinstance(n.value.value, str)):
                    n.value.value = ""
            code.append("\n".join(l.split("#")[0] for l in ast.unparse(t).splitlines()))
    blob = "\n".join(code)

    out = []
    for key in sorted(params):
        snake = key.lower()
        if re.search(rf'\b{re.escape(key)}\b', blob) or \
           re.search(rf'\bsettings\.{snake}\b', blob):
            continue
        out.append({
            "severity": "high", "key": key,
            "what": f"{key}={params[key]} is in parameters.json and NOTHING reads it",
            "why": "A parameter file is the document people consult to learn "
                   "what the bot is configured to do. A key in it that no code "
                   "consults reads as a setting and is a comment — and it reads "
                   "loudest when its name promises safety.",
            "action": f"delete {key} from config/parameters.json, or wire it up "
                      f"if the behaviour it names is wanted",
        })
    return out


def collect() -> dict:
    state = db.get_state()
    history = list(state.get("param_history") or [])
    env = read_env_file()
    example = read_env_file(".env.example")
    readme = read_readme_table()

    rows, findings = [], []

    findings += check_budget_baseline(state, env)
    findings += check_risk_switches(env, example)
    findings += check_sole_param_writer()
    findings += check_parameter_file_is_live(env)
    findings += check_one_store_per_key(env)
    findings += check_no_constant_false_gates(env)

    for key in EXAMPLE_MUST_MATCH:
        have, want = env.get(key), example.get(key)
        if want is None:
            findings.append({
                "severity": "medium", "key": key,
                "what": f"{key} is missing from .env.example",
                "why": "A fresh install gets .env.example. An undocumented key "
                       "means new users silently run the code default.",
                "action": f"add {key}={have if have is not None else '<value>'} "
                          f"to .env.example",
            })
        elif have is not None and not _same(have, want):
            findings.append({
                "severity": "medium", "key": key,
                "what": f"{key}: .env has {have}, .env.example has {want}",
                "why": "A fresh install would run a different strategy than the "
                       "one being tested here.",
                "action": f"set {key}={have} in .env.example",
            })

    for key, (env_name, attr) in TRACKED.items():
        db_val = state.get(f"param_{key}")
        env_val = env.get(env_name)
        settings_val = getattr(settings, attr, None)
        readme_val = readme.get(env_name)
        effective = runtime_config.current(key)
        prov = provenance(history, key)

        source = ("db override" if db_val is not None else
                  ".env" if env_val is not None else "code default")

        rows.append({
            "key": key, "env_name": env_name, "effective": effective,
            "source": source, "db": db_val, "env": env_val,
            "settings": settings_val, "readme": readme_val,
            "history_records": prov["records"],
            "history_active": bool(prov["active_record"]),
        })

        # ── Finding 1: live override with no provenance at all.
        if db_val is not None and prov["records"] == 0:
            findings.append({
                "severity": "high", "key": key,
                "what": f"param_{key}={db_val} is live but param_history has no "
                        f"record for it — provenance unknown",
                "why": "A value nobody can attribute cannot be reasoned about. "
                       "Do not guess which optimizer wrote it.",
                "action": f"Decide {key} by hand, then set it with "
                          f"runtime_config.set_param('{key}', <value>, "
                          f"'human-baseline', force=True)",
            })

        # ── Finding 2: live override whose history record says it is not active.
        elif db_val is not None and not prov["active_record"]:
            last = prov["last_record"] or {}
            findings.append({
                "severity": "high", "key": key,
                "what": f"param_{key}={db_val} is live, but its most recent "
                        f"history record (new={last.get('new')}, "
                        f"source={last.get('source')}) is marked active=false"
                        + (", rolled_back=true" if last.get("rolled_back") else ""),
                "why": "db-state and the audit trail disagree about what is "
                       "running. The rollback machinery reads the audit trail.",
                "action": f"Reconcile by hand: either restore the value the "
                          f"history implies, or re-apply {db_val} with "
                          f"source='human-baseline'.",
            })

        # ── Finding 3: an .env edit masked by a live override.
        if db_val is not None and env_val is not None and not _same(db_val, env_val):
            findings.append({
                "severity": "medium", "key": key,
                "what": f"{env_name}={env_val} in .env, but db override "
                        f"param_{key}={db_val} wins — the .env value is inert",
                "why": "Editing .env looks like it worked and does nothing. "
                       "This is how the 2026-07-01 drift went unnoticed.",
                "action": f"Clear the db override, or update .env to match "
                          f"so the two agree.",
            })

        # ── Finding 4: the README documents something a fresh install does
        # not get.
        #
        # This compared the README against THIS MACHINE's effective value, and
        # that is a category error. The README is reference material for a new
        # user; the effective value is one owner's accumulated tuning, re-tuned
        # weekly by the grid sweep and applied on approval. Coupling them made
        # the check fail after every approved change and demanded a README edit
        # to document a number that is specific to one account and stale by the
        # next Monday.
        #
        # What the README can genuinely be wrong about is what a fresh install
        # runs, which is .env.example. That is the comparison worth keeping.
        example_val = example.get(env_name)
        if (readme_val is not None and example_val is not None
                and not _same(example_val, readme_val)):
            findings.append({
                "severity": "low", "key": key,
                "what": f"README says {env_name}={readme_val}, but a fresh "
                        f"install gets {example_val} from .env.example",
                "why": "The README is what a new user reads before they have "
                       "run anything. It should describe the shipped default, "
                       "not any one account's tuning.",
                "action": f"Update the README table to {example_val}, or change "
                          f".env.example if the shipped default is meant to move.",
            })

    # ── Orphan param_* keys nobody tracks.
    for k in state:
        if k.startswith("param_") and k != "param_history":
            rk = k[len("param_"):]
            if rk not in TRACKED:
                findings.append({
                    "severity": "medium", "key": rk,
                    "what": f"db-state has {k}={state[k]} but it is not in "
                            f"runtime_config.ALLOWED_PARAMS",
                    "why": "An override no bounds check can validate.",
                    "action": f"Delete {k} from db-state, or add it to "
                              f"ALLOWED_PARAMS with explicit bounds.",
                })

    return {"rows": rows, "findings": findings,
            "frozen": runtime_config.frozen(),
            "history_len": len(history)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    result = collect()
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 1 if result["findings"] else 0

    print("═══ EFFECTIVE CONFIG ═══")
    print(f"  param freeze: {'ON (PARAMS_FROZEN)' if result['frozen'] else 'OFF'}"
          f"   |   param_history: {result['history_len']} records\n")
    hdr = f"  {'param':<20} {'effective':>10}  {'source':<13} {'.env':>8} {'README':>8}  history"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for r in result["rows"]:
        hist = (f"{r['history_records']} rec"
                + ("" if r["history_active"] else ", none active")) \
            if r["history_records"] else "—"
        print(f"  {r['key']:<20} {str(r['effective']):>10}  {r['source']:<13} "
              f"{str(r['env'] if r['env'] is not None else '—'):>8} "
              f"{str(r['readme'] if r['readme'] is not None else '—'):>8}  {hist}")

    findings = result["findings"]
    if not findings:
        print("\n✓ No contradictions. Effective config is internally consistent.")
        return 0

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    print(f"\n═══ {len(findings)} CONTRADICTION(S) — each needs a human decision ═══")
    for i, f in enumerate(findings, 1):
        print(f"\n  [{i}] {f['severity'].upper()}  ({f['key']})")
        print(f"      what  : {f['what']}")
        print(f"      why   : {f['why']}")
        print(f"      action: {f['action']}")
    print("\n  Nothing was changed — this script is read-only.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
