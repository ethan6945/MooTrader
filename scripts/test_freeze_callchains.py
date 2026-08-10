"""End-to-end tests for the Phase-0 freeze, through the REAL entry points.

Run from repo root: .venv/bin/python scripts/test_freeze_callchains.py
In-memory db-state fake — touches no SQLite, no OpenD, no .env.

WHY THIS EXISTS, SEPARATELY FROM test_param_freeze.py
  That suite calls runtime_config.set_param() five times with five different
  `source` strings and asserts each is refused. That is one function tested five
  times wearing different hats — it proves the guard exists, and proves nothing
  about whether the paths that reach production actually go through it.

  The review that prompted this file found three holes underneath exactly that
  blind spot:

    · approvals caught the refusal and then marked the item executed=True, so
      the owner was told "✅ 已执行" for a change that never happened.
    · hermes_improve routed anything not in ALLOWED_PARAMS straight into .env
      with no whitelist — so PARAMS_FROZEN=false, or MOO_TRADE_ENV=REAL, was an
      ordinary LLM proposal away.
    · disarm() cleared the seed, and recompute_and_apply() read "no seed" as
      "first run" and re-armed itself.

  Every guard was in place. Every one of those paths walked past it. So these
  tests start where a caller really starts — apply_approved(), apply_params(),
  recompute_and_apply(), record_trade_close(), and the two Flask endpoints —
  and assert on the observable outcome, not on the guard being called.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.db as db

_STATE = {}
db.get_state = lambda: dict(_STATE)
def _update(d):
    for k, v in d.items():
        if v is None:
            _STATE.pop(k, None)
        else:
            _STATE[k] = v
db.update_state = _update
def _atomic(fn):
    _STATE.update(fn(dict(_STATE)))
    return dict(_STATE)
db.atomic_state = _atomic

from src import approvals, auto_budget, hermes_improve, risk_manager  # noqa: E402
from src import runtime_config as rc                                   # noqa: E402

risk_manager._load_state = lambda: dict(_STATE)
rc.frozen = lambda: True

# Capture notifications instead of sending them.
SENT = []
from src import notifier                                              # noqa: E402
notifier.send = lambda msg: SENT.append(msg)
approvals.notifier = notifier

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


# ── 1. approvals.apply_approved(): a refused item must not read as applied ───
print("approvals.apply_approved()")
_STATE.clear()
_STATE[approvals.QUEUE_KEY] = [{
    "id": "t1", "kind": "param_change", "status": "approved",
    "detail": "raise max_position_pct to 0.36",
    "payload": {"key": "max_position_pct", "value": 0.36},
    "created_at": "2026-08-10T00:00:00",
}]
SENT.clear()
applied = approvals.apply_approved()
item = _STATE[approvals.QUEUE_KEY][0]

check("refused item is NOT returned as applied", applied == [])
check("refused item is not marked executed", not item.get("executed"))
check("refused item is marked refused", item.get("refused") is True)
check("refusal reason names the freeze", "PARAMS_FROZEN" in str(item.get("refused_reason")))
check("owner is told it did not run", any("未能执行" in m for m in SENT))
check("no param was written", "param_max_position_pct" not in _STATE)

# Terminal, not a retry loop: a second pass must not pick it up again.
SENT.clear()
again = approvals.apply_approved()
check("refused item is not retried next cycle", again == [] and SENT == [])

# A non-param approval still works — the freeze must not jam the whole queue.
_STATE[approvals.QUEUE_KEY].append({
    "id": "t2", "kind": "strategy_flag", "status": "approved",
    "detail": "watch trend strategy", "payload": {}, "created_at": "x"})
applied = approvals.apply_approved()
check("unrelated approvals still execute", [a["id"] for a in applied] == ["t2"])


# ── 2. hermes_improve.apply_params(): the .env branch must refuse risk keys ──
print("\nhermes_improve.apply_params()")
_STATE.clear()
written = {}
hermes_improve.snapshot_before = lambda: Path("/dev/null")
# Intercept the real .env write so the test never touches the file.
_real_env = ROOT / ".env"
_env_before = _real_env.read_text() if _real_env.exists() else None

res = hermes_improve.apply_params(
    {"PARAMS_FROZEN": "false",          # would unfreeze everything
     "MOO_TRADE_ENV": "REAL",           # would move to live money
     "RISK_PER_TRADE": "0.08",          # tunable, but frozen
     "DEEPSEEK_API_KEY": "sk-leak",     # credential
     "MAX_POSITION_PCT": "0.55"},       # tunable, but frozen
    reason="test", pnl_estimate="n/a")

rejected = res.get("rejected", {})
check("PARAMS_FROZEN write refused", "PARAMS_FROZEN" in rejected)
check("MOO_TRADE_ENV write refused", "MOO_TRADE_ENV" in rejected)
check("credential write refused", "DEEPSEEK_API_KEY" in rejected)
check("frozen tunable refused (RISK_PER_TRADE)", "RISK_PER_TRADE" in rejected)
check("frozen tunable refused (MAX_POSITION_PCT)", "MAX_POSITION_PCT" in rejected)
check("nothing was applied to .env", not res.get("applied_env"))
check("nothing was applied at runtime", not res.get("applied_runtime"))
if _env_before is not None:
    check("the real .env is byte-identical", _real_env.read_text() == _env_before)

# The denylist must not swallow ordinary feature flags.
check("an ordinary flag is still allowed", hermes_improve.env_write_denied("FINNHUB_ENABLED") == "")
check("denies by substring too", hermes_improve.env_write_denied("SOME_NEW_API_KEY") != "")


# ── 3. auto_budget: disarm must survive a later run ─────────────────────────
print("\nauto_budget disarm persistence")
_STATE.clear()
_STATE["budget_usd"] = 10000.0
rc.frozen = lambda: False          # prove disarm holds on its own, not via the freeze
_STATE["auto_budget_enabled"] = True
auto_budget.disarm()
check("disarm sets the sticky flag", auto_budget.is_disarmed())
r = auto_budget.recompute_and_apply()
check("a later run stays disarmed", r.get("applied") is False)
check("run reports the disarm, not 'first run'", "disarm" in str(r.get("reason")))
check("it did NOT silently re-arm", auto_budget.is_armed() is False)
check("budget untouched by the disarmed run", _STATE["budget_usd"] == 10000.0)

auto_budget.arm(10000.0)
check("an explicit arm() clears the sticky flag", not auto_budget.is_disarmed())
check("arm() actually armed", auto_budget.is_armed())
rc.frozen = lambda: True


# ── 4. peak_equity: a close must not undo a budget re-anchor ────────────────
print("\nrisk_manager: set_budget() then record_trade_close()")
_STATE.clear()
_STATE["budget_usd"] = 4740.82
_STATE["peak_equity"] = 5000.0
_STATE["realized_pnl_total"] = -625.47

risk_manager.set_budget(10000.0, source="test")
peak_after_anchor = _STATE["peak_equity"]
check("re-anchor keeps a real drawdown visible (peak = new base)",
      abs(peak_after_anchor - 10000.0) < 0.01)
check("drawdown reports the real 6.25%, not 0%",
      abs(risk_manager.current_drawdown_pct() - 6.2547) < 0.01)

# The bug: record_trade_close used its own formula and clobbered the anchor.
risk_manager.record_trade_close(-100.0, account_usd=None)
check("a close does not move the peak back",
      abs(_STATE["peak_equity"] - peak_after_anchor) < 0.01)
check("drawdown grew with the loss",
      risk_manager.current_drawdown_pct() > 7.0)

# And a genuine new high still advances the peak.
risk_manager.record_trade_close(2000.0, account_usd=None)
check("a new equity high advances the peak", _STATE["peak_equity"] > 10000.0)
check("drawdown back to zero at a new high",
      abs(risk_manager.current_drawdown_pct()) < 1e-6)

check("both writers share one formula",
      risk_manager.compute_peak_equity(10000.0, -625.47, 0.0) ==
      risk_manager.compute_peak_equity(10000.0, -625.47, 5000.0))


# ── 5. the Flask endpoints ──────────────────────────────────────────────────
print("\nweb endpoints")
try:
    import os
    os.environ["WEB_PASSWORD"] = ""          # no auth gate in-process
    from web.server import app
    app.config["TESTING"] = True
    client = app.test_client()

    _STATE.clear()
    _STATE["budget_usd"] = 4740.82
    _STATE["realized_pnl_total"] = -625.47
    r = client.post("/api/budget", json={"value": 10000})
    check("POST /api/budget succeeds", r.status_code == 200 and r.get_json()["ok"])
    check("POST /api/budget re-anchors the peak via set_budget",
          abs(_STATE["peak_equity"] - 10000.0) < 0.01)

    r = client.post("/api/budget", json={"value": -5})
    check("POST /api/budget rejects a negative budget", r.status_code == 400)

    r = client.post("/api/auto-budget", json={"action": "arm", "seed": 10000})
    check("POST /api/auto-budget arm is refused while frozen", r.status_code == 409)
    check("arm refusal explains the freeze", "PARAMS_FROZEN" in r.get_json()["error"])
    check("no seed was written", _STATE.get("auto_budget_seed") is None)

    r = client.post("/api/auto-budget", json={"enabled": True})
    check("POST /api/auto-budget enable is refused while frozen", r.status_code == 409)
    check("db toggle was not written", _STATE.get("auto_budget_enabled") is not True)

    r = client.post("/api/auto-budget", json={"action": "disarm"})
    check("disarm is still allowed while frozen", r.status_code == 200)
except Exception as e:
    check(f"web endpoints reachable ({type(e).__name__}: {str(e)[:60]})", False)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
