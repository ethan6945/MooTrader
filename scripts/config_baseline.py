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
    4. README table             — what the documentation claims

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


def collect() -> dict:
    state = db.get_state()
    history = list(state.get("param_history") or [])
    env = read_env_file()
    example = read_env_file(".env.example")
    readme = read_readme_table()

    rows, findings = [], []

    findings += check_budget_baseline(state, env)

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

        # ── Finding 4: README documents something else.
        if readme_val is not None and not _same(effective, readme_val):
            findings.append({
                "severity": "low", "key": key,
                "what": f"README says {env_name}={readme_val}, effective value "
                        f"is {effective}",
                "why": "The README is what a new user (or future you) will "
                       "believe the bot is doing.",
                "action": f"Update the README table to {effective}.",
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
