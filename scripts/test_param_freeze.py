"""Self-contained test for the Phase-0 parameter freeze.

Run from repo root: .venv/bin/python scripts/test_param_freeze.py
Uses an in-memory fake of db-state, so it touches NO real SQLite / OpenD.

WHY THIS EXISTS
  The 2026-08-10 audit found SEVEN paths that mutate live strategy params —
  approvals, autopilot, hermes_improve, optimizer_ai, optimize_system (twice),
  lever_recheck — plus auto_budget writing budget_usd. AUTO_APPLY_PARAMS closed
  some of them; one (optimize_system._inject) bypassed set_param entirely, so it
  skipped both the ALLOWED_PARAMS bounds check AND the param_history audit trail.

  The evidence it left in the live db: param_sl_atr_mult=3.0 was active while
  its param_history record said active=false, and param_max_position_pct=0.36
  had no provenance record at all. Effective config and audit trail had already
  diverged before anyone noticed.

  These tests pin the freeze shut. They must keep passing until sandbox↔v3
  parity is reconciled (data/sandbox_vs_backtest.json currently: verdict BREACH,
  20% signal match, 83.5% net-PnL gap).
"""
import sys
import threading

import src.db as db

# ---- in-memory db-state fake (no SQLite) ----
_STATE = {}
db.get_state = lambda: dict(_STATE)
def _update(d):
    for k, v in d.items():
        if v is None:
            _STATE.pop(k, None)
        else:
            _STATE[k] = v
db.update_state = _update

from src import runtime_config as rc          # noqa: E402  (after db is faked)
from src.config import settings               # noqa: E402

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


print(f"settings.params_frozen = {settings.params_frozen}")
rc.frozen = lambda: True        # pin on regardless of the caller's .env

# ── 1. set_param is closed to every automated source ─────────────────────────
_STATE.clear()
_STATE["param_entry_threshold"] = 70.0
for source in ("owner-approved", "autopilot_2026-08-10", "hermes_agent",
               "auto-optimizer", "optimizer_auto"):
    try:
        rc.set_param("entry_threshold", 65.0, source)
        check(f"set_param refused from {source!r}", False)
    except rc.ParamsFrozen:
        check(f"set_param refused from {source!r}", True)
check("db-state untouched by refused writes", _STATE["param_entry_threshold"] == 70.0)

# ParamsFrozen must subclass ValueError: approvals and optimizer_ai catch
# ValueError, autopilot and hermes_improve catch Exception. If it were a bare
# Exception subclass, the two ValueError handlers would let it escape and kill
# a scheduled job instead of degrading to "queue it / skip it".
check("ParamsFrozen is a ValueError", issubclass(rc.ParamsFrozen, ValueError))

# ── 2. revert_param (autopilot auto-rollback) is closed too ──────────────────
_STATE["param_history"] = [{"key": "sl_atr_mult", "old": 2.5, "new": 3.0,
                            "source": "auto-optimizer", "active": True}]
_STATE["param_sl_atr_mult"] = 3.0
try:
    rc.revert_param("sl_atr_mult", "auto-rollback test")
    check("revert_param refused", False)
except rc.ParamsFrozen:
    check("revert_param refused", True)
check("rollback left db-state alone", _STATE["param_sl_atr_mult"] == 3.0)

# ── 3. force=True is the human re-baselining escape hatch ────────────────────
rec = rc.set_param("entry_threshold", 72.0, "human-baseline", force=True)
check("force=True applies", _STATE["param_entry_threshold"] == 72.0)
check("force=True records provenance in param_history",
      any(h.get("source") == "human-baseline" for h in _STATE["param_history"]))

# ── 4. sweep overrides never reach db-state ──────────────────────────────────
# This is the regression guard for optimize_system._inject. It used to write
# param_* into live db-state once per combo and rely on a finally block to put
# them back — a finally block that does not run on SIGKILL.
_STATE.clear()
_STATE["param_entry_threshold"] = 70.0
_STATE["param_tp_atr_mult"] = 8.0
_STATE["param_sl_atr_mult"] = 3.0

from src import optimize_system                # noqa: E402
optimize_system._inject(55.0, 14.0, 4.5)

check("_inject: reader sees the combo", rc.entry_threshold() == 55.0)
check("_inject: tp/sl too", rc.tp_atr_mult() == 14.0 and rc.sl_atr_mult() == 4.5)
check("_inject: db param_entry_threshold untouched", _STATE["param_entry_threshold"] == 70.0)
check("_inject: db param_tp_atr_mult untouched", _STATE["param_tp_atr_mult"] == 8.0)
check("_inject: db param_sl_atr_mult untouched", _STATE["param_sl_atr_mult"] == 3.0)
check("_inject: no param_history pollution", "param_history" not in _STATE)

# The live scan runs on a different thread from a sweep. Mid-sweep it must
# still see the real params — this is what makes a killed sweep harmless.
seen = {}
t = threading.Thread(target=lambda: seen.update(
    th=rc.entry_threshold(), tp=rc.tp_atr_mult(), sl=rc.sl_atr_mult()))
t.start(); t.join()
check("_inject: another thread sees real params, not the combo",
      (seen["th"], seen["tp"], seen["sl"]) == (70.0, 8.0, 3.0))

rc.clear_overrides()
check("clear_overrides restores this thread", rc.entry_threshold() == 70.0)

# ── 5. auto_budget's direct budget_usd write is closed ───────────────────────
# auto_budget never went through set_param, so AUTO_APPLY_PARAMS never covered
# it. Live state on 2026-08-10: armed seed $5,000, base_realized -174.91,
# realized -625.47 → it would have overwritten an owner-set $10,000 with ~$4,549.
from src import auto_budget, risk_manager      # noqa: E402
risk_manager._load_state = lambda: dict(_STATE)

_STATE.clear()
_STATE["budget_usd"] = 10000.0
_STATE["auto_budget_enabled"] = True
_STATE["auto_budget_seed"] = 5000.0
_STATE["auto_budget_base_realized"] = -174.91
_STATE["realized_pnl_total"] = -625.47
r = auto_budget.recompute_and_apply()
check("auto_budget refuses while frozen", r.get("applied") is False)
check("auto_budget names the freeze", "frozen" in str(r.get("reason", "")))
check("auto_budget: owner's $10,000 survives", _STATE["budget_usd"] == 10000.0)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
