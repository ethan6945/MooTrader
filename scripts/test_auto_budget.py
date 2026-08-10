"""Self-contained test for src.auto_budget — math + DD decoupling.

Run from repo root: .venv/bin/python <this file>
Uses an in-memory fake of db-state so it touches NO real SQLite / OpenD.
"""
import sys, types

import src.db as db
from src import auto_budget, risk_manager
from src.config import settings

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
# auto_budget caches no module refs to db functions (calls db.get_state each
# time), and risk_manager.budget_usd reads db via its own _load_state → patch
# that too so budget_usd reflects our fake state.
risk_manager._load_state = lambda: dict(_STATE)

# Sections 1-4 exercise the compounding MECHANISM (arm, hysteresis, clamps,
# equity baseline). The Phase-0 freeze blocks all of it, so lift the freeze for
# them; section 5 turns it back on and asserts it actually holds. Patched here
# rather than mid-file because arm() is first reached in section 2.
from src import runtime_config as _rc
_rc.frozen = lambda: False

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1

print("settings:", dict(reinvest=settings.auto_budget_reinvest_frac,
      min_frac=settings.auto_budget_min_frac, max_mult=settings.auto_budget_max_mult,
      step_usd=settings.auto_budget_min_step_usd, step_pct=settings.auto_budget_min_step_pct))

# ── 1. pure compute_target ───────────────────────────────────────────────────
seed = 4500.0
# arm-neutral: no profit since arm → target == seed
t, d = auto_budget.compute_target(seed, base_real=0.0, realized=0.0, live_equity=None)
check("arm is neutral (target==seed)", abs(t - seed) < 1e-6)

# +$1000 earned since arm, full reinvest → +$1000 budget
t, d = auto_budget.compute_target(seed, 0.0, 1000.0, live_equity=1e9)
check("compounds up: +1000 -> 5500", abs(t - 5500.0) < 1e-6)

# −$800 since arm → give-back to 3700 (above floor 2250)
t, d = auto_budget.compute_target(seed, 0.0, -800.0, live_equity=1e9)
check("gives back: -800 -> 3700", abs(t - 3700.0) < 1e-6)

# huge loss clamped at floor seed*0.5 = 2250
t, d = auto_budget.compute_target(seed, 0.0, -9999.0, live_equity=1e9)
check("floor clamp at 2250", abs(t - seed * settings.auto_budget_min_frac) < 1e-6)

# huge gain clamped at ceil seed*5 = 22500
t, d = auto_budget.compute_target(seed, 0.0, 99999.0, live_equity=1e9)
check("ceil clamp at 22500", abs(t - seed * settings.auto_budget_max_mult) < 1e-6)

# live-equity cap: target wants 5500 but account only has 5000
t, d = auto_budget.compute_target(seed, 0.0, 1000.0, live_equity=5000.0)
check("live-equity caps target to 5000", abs(t - 5000.0) < 1e-6 and d["capped_by_equity"])

# base_realized offset: pre-arm profit excluded. realized=1800, base=800 → since=1000
t, d = auto_budget.compute_target(seed, 800.0, 1800.0, live_equity=1e9)
check("base_realized excludes pre-arm profit (->5500)", abs(t - 5500.0) < 1e-6)

# ── 2. DD-breaker decoupling (the load-bearing invariant) ────────────────────
_STATE.clear()
_STATE["budget_usd"] = 4500.0
_STATE["auto_budget_enabled"] = True
# Not armed yet → equity_baseline falls back to live budget
check("unarmed: equity_baseline == budget", abs(risk_manager.equity_baseline() - 4500.0) < 1e-6)

# Arm, then simulate compounding having grown the deployable budget to 6000
auto_budget.arm(4500.0)
_STATE["budget_usd"] = 6000.0           # pretend compounding grew it
_STATE["realized_pnl_total"] = 1500.0   # the profit that drove it
# equity_baseline MUST stay frozen at the seed (4500), NOT the grown budget
check("armed: equity_baseline frozen at seed 4500", abs(risk_manager.equity_baseline() - 4500.0) < 1e-6)
# So equity = baseline + realized = 4500 + 1500 = 6000 (NOT 6000+1500=7500 double-count)
equity = risk_manager.equity_baseline() + _STATE["realized_pnl_total"]
check("equity = seed + realized = 6000 (no double-count)", abs(equity - 6000.0) < 1e-6)
# current_drawdown_pct must read 0 at a new high (peak<=equity), not a phantom DD
_STATE["peak_equity"] = 6000.0
check("current_drawdown_pct == 0 at new high", abs(risk_manager.current_drawdown_pct()) < 1e-6)

# ── 3. recompute_and_apply hysteresis + apply ────────────────────────────────
# Freeze already lifted at the top of the file — see the note there.
_STATE.clear()
_STATE["budget_usd"] = 4500.0
_STATE["auto_budget_enabled"] = True
risk_manager._live_equity_cache["value"] = None
# first run arms (neutral)
r = auto_budget.recompute_and_apply()
check("first run arms", r.get("reason") == "armed")
check("seed stored == 4500", abs((auto_budget.seed_usd() or 0) - 4500.0) < 1e-6)
# tiny profit below step → no apply
_STATE["realized_pnl_total"] = 100.0   # < max($250, 5%*4500=225) → hold
r = auto_budget.recompute_and_apply()
check("below-step change held", r.get("applied") is False and r.get("reason") == "below_step")
check("budget unchanged at 4500", abs(_STATE["budget_usd"] - 4500.0) < 1e-6)
# profit above step → apply, budget grows
_STATE["realized_pnl_total"] = 1200.0  # target 5700, Δ1200 > 285 step
r = auto_budget.recompute_and_apply()
check("above-step change applied", r.get("applied") is True)
check("budget grew to 5700", abs(_STATE["budget_usd"] - 5700.0) < 1e-6)
check("history recorded", len(_STATE.get("auto_budget_history", [])) >= 2)

# ── 4. disabled = pure no-op ─────────────────────────────────────────────────
_STATE.clear()
_STATE["budget_usd"] = 4500.0
_STATE["auto_budget_enabled"] = False
r = auto_budget.recompute_and_apply()
check("disabled is no-op", r.get("reason") == "disabled")
check("disabled: equity_baseline == budget (legacy)", abs(risk_manager.equity_baseline() - 4500.0) < 1e-6)

# ── 5. Phase-0 freeze blocks the budget write ────────────────────────────────
# Regression guard for the 2026-08-10 audit finding: auto_budget writes
# budget_usd straight to db-state, NOT through runtime_config.set_param, so
# AUTO_APPLY_PARAMS never covered it. An armed seed of $5,000 would have walked
# an owner-set $10,000 budget back to ~$4,549 at the next daily close. The
# freeze must hold even with AUTO_BUDGET_ENABLED back on.
_rc.frozen = lambda: True
_STATE.clear()
_STATE["budget_usd"] = 10000.0          # owner just set this by hand
_STATE["auto_budget_enabled"] = True
_STATE["auto_budget_seed"] = 5000.0     # stale armed seed from before
_STATE["auto_budget_base_realized"] = -174.91
_STATE["realized_pnl_total"] = -625.47  # target would be ~4549
r = auto_budget.recompute_and_apply()
check("frozen: refuses to apply", r.get("applied") is False)
check("frozen: reason names the freeze", "frozen" in str(r.get("reason", "")))
check("frozen: owner's $10,000 survives", abs(_STATE["budget_usd"] - 10000.0) < 1e-6)

# ── 6. budget must not silently change concurrency ───────────────────────────
# derive_max_positions() used to scale slots with capital: round(capital/1000),
# floored at MAX_POSITIONS, capped at MAX_POSITIONS_CAP. At $4,740 that resolved
# to 5 so it never visibly moved — but raising the budget to $10,000 would have
# taken the bot from 5 concurrent names to 10. Double the names, half the average
# position, sector caps binding at a different fraction: a strategy change
# arriving through a capital field, with no backtest behind it.
from src.config import derive_max_positions   # noqa: E402

check("slots stay at MAX_POSITIONS when budget doubles",
      derive_max_positions(10000.0) == settings.max_positions)
check("slots unchanged at the old budget (backtest parity)",
      derive_max_positions(4740.82) == settings.max_positions)
check("slots unchanged at an absurd budget",
      derive_max_positions(1_000_000.0) == settings.max_positions)
check("autoscale is off by default", settings.max_positions_autoscale is False)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
