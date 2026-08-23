#!/usr/bin/env python3
"""
Hermes Autonomous Trading Loop — core engine.
Reads bot state, runs backtest, applies validated improvements.

Called by the Hermes cron job. All decision-making happens in the AI agent
(the cron prompt); this module is the read/validate/apply toolkit.

Usage:
  python3 src/hermes_improve.py diagnose   → JSON dump of current state
  python3 src/hermes_improve.py backtest    → run backtest with current params
  python3 src/hermes_improve.py apply KEY=VALUE [...] → apply param changes
  python3 src/hermes_improve.py rollback    → revert last week's changes
"""

import json, os, subprocess, sys, shutil
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
CHANGELOG = DATA / "hermes_changelog.jsonl"
SNAPSHOT_DIR = DATA / "hermes_snapshots"
BACKTEST_OUT = DATA / "hermes_backtest_result.json"

# Use project venv Python for backtest (not Hermes venv — numpy mismatch)
_VENV_PYTHON = ROOT / ".venv" / "bin" / "python3"
_PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _cmd(args: list, cwd=ROOT, timeout=300) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, cwd=str(cwd), timeout=timeout)


# ── DIAGNOSE ──────────────────────────────────────────────

_SECRET_MARKERS = ("KEY", "SECRET", "PASSWORD", "TOKEN", "PWD")
_REDACTED = "<redacted-by-snapshot>"

# Values that are not credentials but DO identify a person or an account. A
# secret gets blanked; these get an irreversible per-install reference instead,
# so a backup still answers "is this the same account as the other backup?"
# without carrying the identifier itself.
#
# TELEGRAM_CHAT_ID is the one that made this necessary: it survived three
# rounds of "redaction" because it contains none of the secret markers, and
# anyone holding it plus a bot token can message the owner directly.
_IDENTIFIER_KEYS = {
    "TELEGRAM_CHAT_ID",
    "MOO_ACC_ID", "MOOMOO_ACC_ID", "ACC_ID", "ACCOUNT_ID",
    "MOO_LOGIN", "MOOMOO_LOGIN", "OPEND_LOGIN", "MOO_USER", "MOOMOO_USER",
    "OPEND_LOGIN_ACCOUNT", "OPEND_ACCOUNT", "MOO_LOGIN_ACCOUNT",
    "MOO_SECURITY_FIRM",
}

# Substring rules, so a key added later is covered without editing this file.
_IDENTIFIER_MARKERS = ("LOGIN", "ACC_ID", "CHAT_ID")

# …with an explicit exemption list, because the substring rules are blunt.
# ACCOUNT_USD is the trading budget. Referencing it would replace a number the
# config check compares across .env files with an opaque hash, and every
# baseline comparison would start failing for no reason.
_NOT_IDENTIFIERS = {"ACCOUNT_USD", "MAX_POSITION_PCT"}


def is_secret_key(name: str) -> bool:
    """True for .env keys whose VALUE must never be copied anywhere."""
    return any(s in name.upper() for s in _SECRET_MARKERS)


def is_identifier_key(name: str) -> bool:
    """True for keys that identify an account or person rather than authenticate.

    OPEND_LOGIN_ACCOUNT is the reason the substring rules exist: it sat in both
    .env files and in every backup, matched none of the credential markers, and
    was not in the exact-name list either. It names the brokerage login.
    """
    k = name.strip().upper()
    if k in _NOT_IDENTIFIERS:
        return False
    return k in _IDENTIFIER_KEYS or any(m in k for m in _IDENTIFIER_MARKERS)


def redacted_env_text(text: str) -> str:
    """.env content with every secret VALUE replaced, keys and layout intact.

    Keys and comments are kept so a snapshot still answers "what was configured
    at the time" and so rollback() can still read back non-secret values; only
    the credentials are removed.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = line.partition("=")
            val = v.split("#")[0].strip()
            if val and is_secret_key(k):
                out.append(f"{k}={_REDACTED}")
                continue
            if val and is_identifier_key(k):
                # Reference, not blank: two snapshots of the same account still
                # compare equal, which is what makes a backup diffable.
                from . import account_ref
                # Already a reference — re-hashing would produce a DIFFERENT
                # one each pass, so a backup re-redacted twice would stop
                # matching itself. Backups do get re-redacted (this file's own
                # predicate has widened once already), so idempotency is load
                # bearing, not tidiness.
                if not account_ref.is_ref(val):
                    val = account_ref.ref(val, kind=k.strip().lower())
                out.append(f"{k}={val}")
                continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def diagnose() -> dict:
    """Collect full bot state for AI analysis. Returns JSON-serializable dict."""
    account = _load_json(DATA / "account.json")
    mae = _load_json(DATA / "mae_mfe_diagnostic.json")
    backtest = _load_json(DATA / "backtest_results.json")

    # Read .env non-secret params
    env = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if is_secret_key(k):
                    continue
                env[k.strip()] = v.split("#")[0].strip()

    # Recent closed trades — via db.closed_trades(), not raw SQL.
    #
    # This payload is what the LLM is told is recent performance. Reading the
    # table directly bypassed both corrections that live in that function: the
    # ts ordering (id stopped tracking time at the ledger merge, so "the last
    # 30" were the oldest 30) and the effective-ledger filter (a synthetic test
    # row and a double-booked close were being presented as real trades).
    trades = []
    db_path = DATA / "trader.db"
    if db_path.exists():
        try:
            sys.path.insert(0, str(ROOT))
            from src import db as _db
            trades = [
                {"symbol": r.get("symbol"), "pnl": r.get("pnl"),
                 "pnl_pct": r.get("pnl_pct"), "r_multiple": r.get("r_multiple"),
                 "exit_reason": r.get("exit_reason"), "strategy": r.get("strategy"),
                 "ts": r.get("ts")}
                for r in _db.closed_trades(limit=30)
            ]
        except Exception as e:
            print(f"diagnose: closed_trades unavailable ({e})", file=sys.stderr)
    # Self-review latest
    sr_dir = DATA / "self_review"
    latest_sr = None
    if sr_dir.exists():
        srs = sorted(sr_dir.glob("*.json"), reverse=True)
        if srs:
            latest_sr = _load_json(srs[0])

    return {
        "ts": datetime.now().isoformat(),
        "account": account,
        "env": env,
        "recent_trades": trades[:20],
        "mae_mfe": mae,
        "backtest_metrics": backtest.get("metrics", {}) if backtest else {},
        "latest_self_review": latest_sr,
        "posture": _analyze_posture(trades[:20], backtest.get("metrics", {}) if backtest else {}),
    }


# ── POSTURE — conservative vs aggressive ──────────────────

def _analyze_posture(trades: list, backtest_metrics: dict) -> dict:
    """Determine if the bot should tighten (too many losers) or loosen (earning too little).

    Returns a posture recommendation with concrete parameter directions.
    """
    if not trades:
        return {"mode": "unknown", "reason": "no trades yet", "directions": []}

    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins) / n * 100
    avg_r = sum(t["r_multiple"] for t in trades) / n
    total_pnl = sum(t["pnl"] for t in trades)

    # Backtest reference
    bt_wr = backtest_metrics.get("win_rate_pct", 45)
    bt_pf = backtest_metrics.get("profit_factor", 1.5)

    directions = []

    # ── CONSERVATIVE triggers ──
    if wr < 35:
        directions.append({
            "direction": "tighten",
            "reason": f"win rate {wr:.0f}% well below backtest {bt_wr:.0f}%",
            "actions": [
                "raise ENTRY_SCORE_THRESHOLD (+5 to +10) = fewer but higher-quality signals",
                "reduce MAX_POSITIONS (-1) = force concentration on best ideas",
                "tighten MAX_HOLD_DAYS (-1 to -2) = cut losers faster",
            ],
        })
    if avg_r < -0.3:
        directions.append({
            "direction": "tighten",
            "reason": f"avg R {avg_r:.2f} is negative — losing more per trade than winning",
            "actions": [
                "tighten SL_ATR_MULT (reduce by 0.3-0.5) = smaller losses per trade",
                "raise ENTRY_SCORE_THRESHOLD (+5) = higher conviction entries",
            ],
        })

    # ── AGGRESSIVE triggers ──
    if wr > 50 and total_pnl > 0 and avg_r > 0.3:
        directions.append({
            "direction": "loosen",
            "reason": f"win rate {wr:.0f}% strong, avg R {avg_r:.2f} positive — earning but could earn MORE",
            "actions": [
                "widen TP_ATR_MULT (+2 to +4) = let winners run further",
                "lower ENTRY_SCORE_THRESHOLD (-5) = capture more signals while edge is hot",
                "increase RISK_PER_TRADE (+1-2pp) = scale up proven edge",
                "increase MAX_POSITIONS (+1) = more concurrent bets",
            ],
        })
    if avg_r > 0.5 and wr > 40:
        directions.append({
            "direction": "loosen_size",
            "reason": f"strong expectancy ({avg_r:.2f}R) with decent win rate — sizing too small",
            "actions": [
                "increase RISK_PER_TRADE by 1-3 percentage points",
                "widen TP_ATR_MULT to capture higher R-multiple exits",
            ],
        })

    # ── NEUTRAL / MIXED ──
    mode = "neutral"
    if directions:
        tight = [d for d in directions if d["direction"].startswith("tighten")]
        loose = [d for d in directions if d["direction"].startswith("loosen")]
        if tight and not loose:
            mode = "conservative"
        elif loose and not tight:
            mode = "aggressive"
        else:
            mode = "mixed"

    return {
        "mode": mode,
        "summary": f"{n} trades: WR={wr:.0f}%, avg_R={avg_r:.2f}, PnL=${total_pnl:.0f}",
        "backtest_ref": f"WR={bt_wr:.0f}%, PF={bt_pf:.2f}",
        "directions": directions,
    }


# ── BACKTEST ──────────────────────────────────────────────

def run_backtest(ticker_count: int = 15) -> dict:
    """Run backtest_v3 with current .env params. Returns metrics dict."""
    # backtest_v3 uses relative imports — run via module import
    # Read live settings to inject into backtest config
    sys.path.insert(0, str(ROOT))
    from src.config import settings as _bt_settings

    code = (
        "import sys, json; sys.path.insert(0, '" + str(ROOT) + "'); "
        "from src.config import settings; "
        "from src import runtime_config as rc; "   # runtime-EFFECTIVE values (db overrides win)
        "from src.backtest import BacktestConfig, run_backtest; "
        "cfg = BacktestConfig("
        "days=180, threshold=rc.entry_threshold(), "
        "account_usd=settings.account_usd, "
        "risk_per_trade=rc.risk_per_trade(), "
        "sl_atr_mult=rc.sl_atr_mult(), "
        "tp_atr_mult=rc.tp_atr_mult(), "
        "max_hold_days=rc.max_hold_days(), "
        "max_position_pct=rc.max_position_pct(), "
        "apply_momentum_strategy=True, "
        "apply_mr_strategy=" + str(_bt_settings.mr_enabled) + ", "
        "use_breakeven_stop=" + str(_bt_settings.use_breakeven_stop) + ", "
        "breakeven_trigger_r=" + str(_bt_settings.breakeven_trigger_r) + ", "
        "use_scale_out=" + str(_bt_settings.use_scale_out) + ", "
        "tp1_r=" + str(_bt_settings.tp1_r) + ", tp2_r=" + str(_bt_settings.tp2_r) + ", "
        "max_gap_pct=" + str(_bt_settings.max_gap_pct) + ", "
        "sl_cooldown_hours=6, "
        "apply_max_positions=True, "
        "apply_dd_breaker=True, "
        "realistic_limit_fills=True"
        "); "
        "result = run_backtest(cfg); "
        "print(json.dumps(result.get('metrics', result), default=str))"
    )
    rc = _cmd([_PYTHON, "-c", code], timeout=600)
    if rc.returncode != 0:
        return {"error": rc.stderr[:500], "stdout": rc.stdout[:500]}
    try:
        return json.loads(rc.stdout.strip().split("\n")[-1])
    except json.JSONDecodeError:
        return {"error": "parse failed", "stdout": rc.stdout[:500]}


def compare_backtests(before: dict, after: dict) -> dict:
    """Compare two backtest metric dicts, return delta."""
    delta = {}
    for k in ["net_pnl_usd", "win_rate_pct", "profit_factor", "sortino_ratio",
              "max_drawdown_pct", "total_return_pct", "expectancy_per_trade_usd"]:
        b = before.get(k, 0) or 0
        a = after.get(k, 0) or 0
        delta[k] = {"before": b, "after": a, "change_pct": ((a - b) / abs(b) * 100) if b != 0 else 0}
    return delta


# ── APPLY ─────────────────────────────────────────────────
# 2026-07-07 redesign: the effective config of the RUNNING bot is
# runtime_config db-state overrides, NOT .env — an .env edit is masked by any
# existing override and needs a scheduler restart anyway (the 2026-07-01 audit
# found exactly this drift). So tunable params now route through
# runtime_config.set_param(): hot-effective next scan, recorded in
# param_history (autopilot rollback/cooldown see it), Telegram-notified (铁律).
# Only non-tunable keys (feature flags etc.) still fall back to .env, loudly.

# Hermes/env-style name → runtime_config key (ALLOWED_PARAMS whitelist).
_RUNTIME_KEY_MAP = {
    "ENTRY_THRESHOLD": "entry_threshold",
    "ENTRY_SCORE_THRESHOLD": "entry_threshold",   # legacy alias in posture text
    "TP_ATR_MULT": "tp_atr_mult",
    "SL_ATR_MULT": "sl_atr_mult",
    "RISK_PER_TRADE": "risk_per_trade",
    "MAX_HOLD_DAYS": "max_hold_days",
    "MAX_POSITION_PCT": "max_position_pct",
    "UNIVERSE_TOP_N": "universe_top_n",
    "BREAKEVEN_TRIGGER_R": "breakeven_trigger_r",
}
# runtime-style names map to themselves
_RUNTIME_KEY_MAP.update({v: v for v in set(_RUNTIME_KEY_MAP.values())})

# Keys an AI agent may NEVER write to .env, whatever it proposes.
#
# apply_params used to send every non-tunable key straight into .env with no
# whitelist at all. That made the param freeze decorative: a proposal of
# PARAMS_FROZEN=false unfreezes the bot, and MOO_TRADE_ENV=REAL moves it to live
# money — both from an LLM suggestion, both without an approval step, because
# the .env branch was the "boring leftovers" path nobody threatened-modelled.
#
# Matched case-insensitively by exact name or prefix. Denylist rather than
# allowlist deliberately: a new feature flag showing up here should still be
# writable, but a new SECRET/RISK/ENV key must fail closed. Anything not listed
# and not a known tunable still requires the key to already exist in .env, so a
# typo cannot invent a setting.
_ENV_DENY_EXACT = {
    "PARAMS_FROZEN",            # would unfreeze everything below
    "AUTO_APPLY_PARAMS",
    "AUTO_BUDGET_ENABLED",
    "MAX_POSITIONS_AUTOSCALE",
    "MOO_TRADE_ENV",            # SIMULATE -> REAL is real money
    "MOO_TRADE_PWD",
    "MOO_HOST", "MOO_PORT",
    "ACCOUNT_USD",              # capital; goes through risk_manager.set_budget
    "RISK_PER_TRADE", "MAX_POSITION_PCT", "MAX_POSITIONS",
    "DAILY_DRAWDOWN_STOP", "DD_HALT_PCT", "DD_SIZE_CUT_PCT",
    "MAX_STACKS_PER_SYMBOL", "MAX_NEW_NAMES_PER_SCAN",
    "WEB_PASSWORD", "WEB_SECRET",
}
_ENV_DENY_SUBSTRING = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PWD")


def env_write_denied(key: str) -> str:
    """Reason this key may not be written to .env by an agent, or "" if allowed."""
    k = (key or "").strip().upper()
    if k in _ENV_DENY_EXACT:
        return "risk/safety setting — owner-only"
    if any(s in k for s in _ENV_DENY_SUBSTRING):
        return "credential — never agent-writable"
    return ""


def snapshot_before() -> Path:
    """Save current .env (REDACTED) and account.json for rollback.

    2026-08-10: this used to shutil.copy the .env verbatim, so every snapshot
    was a second plaintext copy of DEEPSEEK_API_KEY, TAVILY_API_KEY,
    TELEGRAM_TOKEN and WEB_PASSWORD sitting in data/hermes_snapshots/. Three
    such copies existed. data/ is gitignored so none reached the public repo,
    but "the credential is in four files instead of one" is not a property
    anybody chose. rollback() only ever reads back non-secret keys (tunable
    params route through runtime_config, not .env), so redacting costs nothing.
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = SNAPSHOT_DIR / ts
    snap.mkdir()
    env_file = ROOT / ".env"
    if env_file.exists():
        (snap / ".env").write_text(redacted_env_text(env_file.read_text()))
    account_file = DATA / "account.json"
    if account_file.exists():
        shutil.copy(account_file, snap / "account.json")
    return snap


def apply_params(changes: dict, reason: str, pnl_estimate: str) -> dict:
    """Apply parameter changes. Tunable params → runtime_config (hot, validated,
    history-recorded, Telegram-notified). Everything else → .env with a loud
    restart-required warning. Returns what happened per key."""
    sys.path.insert(0, str(ROOT))
    from src import runtime_config

    snapshot = snapshot_before()
    applied_runtime: dict = {}
    applied_env: dict = {}
    rejected: dict = {}
    env_changes: dict = {}

    for key, new_val in changes.items():
        rk = _RUNTIME_KEY_MAP.get(key) or _RUNTIME_KEY_MAP.get(key.upper())
        if rk:
            # Queued, not applied (2026-08-23). An agent-proposed parameter is
            # still a decision about real money; PARAMS_FROZEN used to be what
            # stopped this writing, and the freeze is off.
            try:
                val = float(new_val)
                if not runtime_config.is_valid(rk, val):
                    raise ValueError("outside ALLOWED_PARAMS bounds")
                from . import approvals as _approvals
                _approvals.enqueue(
                    kind="param_change",
                    detail=f"Hermes agent: {rk} {runtime_config.current(rk)} → {val}",
                    action=f"set {rk} = {val}",
                    payload={"key": rk, "value": val, "source": "hermes_agent"})
                applied_runtime[rk] = {"old": runtime_config.current(rk),
                                       "new": val, "status": "queued for approval"}
            except (ValueError, TypeError) as e:
                rejected[key] = f"out of ALLOWED_PARAMS bounds / not numeric: {e}"
        else:
            denied = env_write_denied(key)
            if denied:
                rejected[key] = f"refused .env write: {denied}"
            else:
                env_changes[key] = new_val

    # Non-tunable keys (feature flags, etc.) — loudly flagged, and written to
    # whichever file actually decides them.
    #
    # This edited .env directly. Since parameters moved to
    # config/parameters.json — which is overlaid onto the environment after
    # load_dotenv and overwrites — a flag living there would have been written
    # to .env, reported as applied, and silently lost. The web panel had this
    # exact bug and it cost the strategy-mode selector. One choke point now:
    # runtime_config.write_setting picks the file.
    if env_changes:
        from . import runtime_config as _rc
        known = _rc._env_keys() | set(
            (_rc._read_file() or {}).get("params") or {})
        for key, new_val in env_changes.items():
            if key not in known:
                # Unchanged rule: an unknown key is not created blindly, because
                # a typo would otherwise become a permanent line nothing reads.
                rejected[key] = ("key not found in .env or parameters.json "
                                 "(refusing to append blindly)")
                continue
            store = _rc.write_setting(key, str(new_val), source="hermes")
            applied_env[key] = new_val
            log.warning("hermes wrote %s=%s to %s", key, new_val, store)

    # 铁律: parameter changes must never be silent.
    if applied_runtime or applied_env:
        try:
            from src import notifier
            lines = ["🤖 *Hermes agent 调参*"]
            for k, v in applied_runtime.items():
                lines.append(f"  • {k}: {v['old']} → {v['new']} (runtime, 下一次扫描生效)")
            for k, v in applied_env.items():
                lines.append(f"  • {k}={v} (.env — 需重启调度器生效)")
            lines.append(f"  理由: {reason}")
            notifier.send("\n".join(lines))
        except Exception:
            pass

    entry = {
        "ts": datetime.now().isoformat(),
        "snapshot": str(snapshot),
        "applied_runtime": applied_runtime,
        "applied_env": applied_env,
        "rejected": rejected,
        "reason": reason,
        "pnl_estimate": pnl_estimate,
    }
    with open(CHANGELOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

    out = {"applied_runtime": applied_runtime, "applied_env": applied_env,
           "rejected": rejected, "snapshot": str(snapshot)}
    if applied_env:
        out["warning"] = (".env keys need a scheduler restart AND are masked by "
                          "any runtime db override — prefer runtime keys: "
                          + ", ".join(sorted(set(_RUNTIME_KEY_MAP.values()))))
    return out


def rollback() -> dict:
    """Revert the LAST apply_params changelog entry.

    Runtime params revert via runtime_config.revert_param (history-recorded);
    .env keys are restored FROM the entry's snapshot — key-by-key, never a
    whole-file copy (the old version clobbered every .env edit made since the
    snapshot, e.g. a freshly-generated WEB_SECRET)."""
    sys.path.insert(0, str(ROOT))
    from src import runtime_config

    entries = []
    if CHANGELOG.exists():
        for line in CHANGELOG.read_text().splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not entries:
        return {"error": "no changelog entries to roll back"}
    last = entries[-1]

    reverted_runtime = {}
    for rk in (last.get("applied_runtime") or {}):
        rec = runtime_config.revert_param(rk, reason="hermes rollback")
        if rec:
            reverted_runtime[rk] = {"restored": rec.get("old")}

    restored_env = {}
    env_keys = list((last.get("applied_env") or {}).keys())
    snap_env = Path(last.get("snapshot", "")) / ".env"
    if env_keys and snap_env.exists():
        old_vals = {}
        for line in snap_env.read_text().splitlines():
            s = line.strip()
            for k in env_keys:
                if s.startswith(f"{k}="):
                    val = s.split("=", 1)[1]
                    # Snapshots redact secret values (see snapshot_before). If
                    # one somehow reached applied_env, restoring the placeholder
                    # would overwrite a live credential with literal text —
                    # worse than not rolling that key back at all. Skip it.
                    if is_secret_key(k) or val.strip() == _REDACTED:
                        print(f"rollback: refusing to restore {k} from a "
                              f"redacted snapshot — set it by hand",
                              file=sys.stderr)
                        continue
                    old_vals[k] = val
        if old_vals:
            # Through the choke point, for the same reason apply_params is:
            # a key that now lives in config/parameters.json would be restored
            # into .env, reported as rolled back, and overwritten on the next
            # start by the value we were rolling back FROM.
            from . import runtime_config as _rc
            for k, v in old_vals.items():
                store = _rc.write_setting(k, str(v), source="hermes-rollback")
                restored_env[k] = v
                log.warning("hermes rollback restored %s to %s", k, store)

    try:
        from src import notifier
        if reverted_runtime or restored_env:
            notifier.send("↩️ Hermes agent 回滚: "
                          + ", ".join(list(reverted_runtime) + list(restored_env)))
    except Exception:
        pass

    return {"reverted_runtime": reverted_runtime, "restored_env": restored_env,
            "entry_ts": last.get("ts")}


# ── MAIN ──────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: hermes_improve.py <diagnose|backtest|apply|rollback> [args]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "diagnose":
        print(json.dumps(diagnose(), indent=2, default=str))

    elif cmd == "backtest":
        baseline = run_backtest()
        print(json.dumps(baseline, indent=2, default=str))

    elif cmd == "apply":
        changes = {}
        for arg in sys.argv[2:]:
            if "=" in arg:
                k, v = arg.split("=", 1)
                changes[k] = v
        if not changes:
            print(json.dumps({"error": "no KEY=VALUE pairs provided"}))
            sys.exit(1)
        result = apply_params(changes, reason="auto", pnl_estimate="TBD")
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "rollback":
        result = rollback()
        print(json.dumps(result, indent=2, default=str))
