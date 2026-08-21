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
import json
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
check("rollback left the stored value alone", _STATE["param_sl_atr_mult"] == 3.0)

# ── 3. force=True is the human re-baselining escape hatch ────────────────────
# Parameters live in config/parameters.json now, not as param_* rows in
# db-state. They used to live in BOTH that and .env, and db-state won silently:
# SL_ATR_MULT=2.8 sat in .env while the bot traded 3.5. These assertions read
# the file because the file is where the value is.
# Snapshot first: earlier sections put param_* keys into the db fake by hand,
# so "the key is absent" would be asserting about their setup rather than about
# what set_param did. What matters is that this write does not ADD to it.
_db_before = {k: v for k, v in _STATE.items() if k.startswith("param_")}
rec = rc.set_param("entry_threshold", 72.0, "human-baseline", force=True)
check("force=True applies", rc.entry_threshold() == 72.0)
_doc = json.loads(rc.params_file().read_text())
check("...to the parameter file",
      float(_doc["params"]["ENTRY_SCORE_THRESHOLD"]) == 72.0)
check("...and db-state gained nothing",
      {k: v for k, v in _STATE.items() if k.startswith("param_")} == _db_before)
_jl = rc.params_file().parent / "parameters_history.jsonl"
_hist = [json.loads(l) for l in _jl.read_text().splitlines()] if _jl.exists() else []
check("force=True records provenance in the journal",
      any(h.get("source") == "human-baseline" for h in _hist))
check("...naming the parameter and both values",
      any(h.get("key") == "entry_threshold" and h.get("new") == 72.0
          for h in _hist))

# ── 4. sweep overrides never reach db-state ──────────────────────────────────
# This is the regression guard for optimize_system._inject. It used to write
# param_* into live db-state once per combo and rely on a finally block to put
# them back — a finally block that does not run on SIGKILL.
# The baseline is established in the FILE, because that is where a parameter
# lives now. Setting param_* keys in the db fake — which is what this did —
# stopped describing anything the readers consult.
_STATE.clear()
for _k, _v in (("entry_threshold", 70.0), ("tp_atr_mult", 8.0), ("sl_atr_mult", 3.0)):
    rc.set_param(_k, _v, "test-baseline", force=True)
_baseline = json.loads(rc.params_file().read_text())["params"]

from src import optimize_system                # noqa: E402
optimize_system._inject(55.0, 14.0, 4.5)

check("_inject: reader sees the combo", rc.entry_threshold() == 55.0)
check("_inject: tp/sl too", rc.tp_atr_mult() == 14.0 and rc.sl_atr_mult() == 4.5)
# The point of the thread-local: a sweep combo must never be PERSISTED. It used
# to be written into live db-state once per combo and restored in a finally
# block — a finally block that does not run on SIGKILL, which is the most
# likely origin of the 2026-08-10 param divergence. The store it must not touch
# is the file now.
_after = json.loads(rc.params_file().read_text())["params"]
check("_inject: the parameter FILE is untouched", _after == _baseline)
check("_inject: nothing written to db-state either",
      not any(k.startswith("param_") for k in _STATE))

# The live scan runs on a different thread from a sweep. Mid-sweep it must
# still see the real params — this is what makes a killed sweep harmless.
seen = {}
t = threading.Thread(target=lambda: seen.update(
    th=rc.entry_threshold(), tp=rc.tp_atr_mult(), sl=rc.sl_atr_mult()))
t.start(); t.join()
check("_inject: another thread sees real params, not the combo",
      (seen["th"], seen["tp"], seen["sl"]) == (70.0, 8.0, 3.0),
      )

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

# ── 6. the db toggle must not out-vote the freeze ────────────────────────────
# auto_budget.enabled() reads db-state BEFORE .env, so one panel click writing
# auto_budget_enabled=True to db resurrected compounding even with
# AUTO_BUDGET_ENABLED=false in .env. The freeze has to win over both.
_STATE["auto_budget_enabled"] = True
check("db toggle cannot re-enable auto_budget while frozen",
      auto_budget.enabled() is False)

# ── 7. arming is blocked; disarming never is ─────────────────────────────────
# arm() starts the budget moving on its own — the exact autonomy the freeze
# removes. disarm() only ever reduces autonomy, so blocking it would help nobody.
_STATE.pop("auto_budget_seed", None)        # section 5 left one behind
_STATE.pop("auto_budget_base_realized", None)
try:
    auto_budget.arm(5000.0)
    check("arm() refused while frozen", False)
except rc.ParamsFrozen:
    check("arm() refused while frozen", True)
check("arm() wrote no seed", _STATE.get("auto_budget_seed") is None)

_STATE["auto_budget_seed"] = 5000.0
_STATE["auto_budget_base_realized"] = -100.0
auto_budget.disarm()
check("disarm() still works while frozen",
      _STATE.get("auto_budget_seed") is None)

# ── 8. setting the budget re-anchors the drawdown peak ───────────────────────
# Skipping the re-anchor silently disables the DD breaker. Upward it pins
# drawdown at 0% forever (2026-08-10, budget $4,740 → $10,000 left peak at
# $5,000, so DD_HALT_PCT=18 needed a 59% real loss to fire); downward it reads a
# phantom drawdown and halts everything (2026-07-07, $50k → $5k). The logic used
# to live inline in the web handler, which is how a later writer skipped it.
_STATE.clear()
_STATE["budget_usd"] = 4740.82
_STATE["peak_equity"] = 5000.0
_STATE["realized_pnl_total"] = -625.47
_STATE["halt_started_at"] = "2026-08-01T00:00:00"

# atomic_state is the write path set_budget uses — fake it over _STATE.
def _atomic(fn):
    _STATE.update(fn(dict(_STATE)))
    return dict(_STATE)
db.atomic_state = _atomic

res = risk_manager.set_budget(10000.0, source="test")
check("set_budget writes the budget", _STATE["budget_usd"] == 10000.0)
# peak = max(base, base + realized) — the `base` floor matters. An earlier
# version used a bare base+realized, which set peak to $9,374 and reported 0%
# drawdown for an account that had genuinely lost $625 of its $10,000. It was
# then clobbered on the next close anyway, because record_trade_close applies
# the floor. One formula now: risk_manager.compute_peak_equity.
check("set_budget anchors peak at the capital base, not below it",
      abs(_STATE["peak_equity"] - 10000.0) < 0.01)
check("set_budget clears a stale halt", _STATE["halt_started_at"] is None)
check("a real drawdown stays visible after re-anchor (not zeroed)",
      abs(risk_manager.current_drawdown_pct() - 6.2547) < 0.01)

# The breaker must be reachable from the NEW base, not a forgotten smaller one.
_STATE["realized_pnl_total"] = -1900.0              # 19% below the $10,000 base
check("DD breaker fires again after re-anchoring",
      risk_manager.current_drawdown_pct() > 18.0)

try:
    risk_manager.set_budget(0, source="test")
    check("set_budget rejects a non-positive budget", False)
except ValueError:
    check("set_budget rejects a non-positive budget", True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
