"""lever_recheck — monthly drift-check on the data-derived exit levers.

Born from the 2026-06-25 MAE/MFE → lever sweep (`scripts/optimize_levers.py`),
which proved most single-trade heuristics don't survive portfolio validation and
that only widening TP (8→10) helped. This module re-runs the *survivors'* check
each month so a regime change that shifts the optimum is flagged early — same
philosophy as the weekly backtest and monthly Optuna: **suggestion only, never
auto-applies `.env`** (blindly chasing last month's optimum is overfitting).

Scope is deliberately narrow + fast (a 3 a.m. job on the dedicated host must be
reliable): the two levers most exposed to regime drift — TP_ATR_MULT and
SL_ATR_MULT — swept around their current live values on the pinned watchlist.
Because it's a RELATIVE A-vs-B comparison on identical bars, the pinned-watchlist
survivorship bias the weekly Phase-1 backtest worries about cancels out (it hits
every variant equally), so we trade that nuance for speed + no flaky 72-name pool
prefetch. The judge is the project's iron rule: a variant only "wins" if it lifts
$/day AND does not worsen max MTM drawdown.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

from .config import settings
from . import runtime_config as rc

log = logging.getLogger(__name__)
_ROOT = settings.root

# How much a different value must beat the current one (on $/day) before we
# bother the owner with a suggestion — keeps month-to-month noise quiet.
SUGGEST_MIN_DELTA_USD = 1.5
DD_TOLERANCE_PP = 0.2   # a variant may not raise max MTM DD by more than this


def _live_cfg(days: int, tickers: list[str]):
    """The faithful current-live config — mirrors backtest._run_live_engine.

    Reads tunable params from runtime_config (override beats .env), NOT raw
    settings, so the recheck compares against what the bot ACTUALLY runs after
    any prior auto-applied / owner-approved change — same parity rule as
    optimizer_ai._base_cfg."""
    from .backtest import BacktestConfig
    cfg = BacktestConfig(
        days=days, timeframe=str(settings.timeframe), threshold=float(rc.entry_threshold()),
        tickers=tickers,
        account_usd=float(settings.account_usd), risk_per_trade=float(rc.risk_per_trade()),
        max_position_pct=float(rc.max_position_pct()), max_hold_days=int(rc.max_hold_days()),
        tp_atr_mult=float(rc.tp_atr_mult()), sl_atr_mult=float(rc.sl_atr_mult()),
        max_gap_pct=float(settings.max_gap_pct),
        dd_size_cut_pct=float(settings.dd_size_cut_pct), dd_halt_pct=float(settings.dd_halt_pct),
        apply_mr_strategy=False,
    )
    return replace(
        cfg, apply_vix_sizing=True, apply_earnings_gate=True, use_realistic_commission=True,
        apply_momentum_strategy=True, use_regime_scaling=True,
        regime_bull_mult=settings.regime_bull_mult, regime_vix_calm=settings.regime_vix_calm,
        scan_grid_exits=True, entry_fill_open_only=True, no_same_day_daily=True,
        reclamp_position_cap=True, apply_trade_windows=True,
        max_new_names_per_scan=settings.max_new_names_per_scan,
        use_breakeven_stop=settings.use_breakeven_stop,
        breakeven_trigger_r=rc.breakeven_trigger_r(),
        use_scale_out=False,
    )


def _row(cfg, cache) -> dict:
    from .backtest_v3 import simulate_v3
    m = simulate_v3(cfg, cache, enforce_cash=True)["metrics"]
    return {"daily": m.get("daily_pnl_usd", 0.0), "ddm": m.get("max_dd_mtm_pct", 0.0),
            "pf": m.get("profit_factor", 0.0), "wr": m.get("win_rate_pct", 0.0),
            "trd": m.get("total_trades", 0)}


def _bounded_grid(key: str, candidates: list[float]) -> list[float]:
    """Round, dedupe, and keep only candidates inside ALLOWED_PARAMS[key]."""
    lo, hi = rc.ALLOWED_PARAMS[key]
    return sorted({round(x, 1) for x in candidates if lo <= round(x, 1) <= hi})


def _best(base_cfg, cache, field: str, grid: list[float], current: float, base_row: dict) -> dict:
    """Sweep one lever; return the DD-safe variant with the highest $/day."""
    best_val, best_row, best_delta = current, base_row, 0.0
    rows = {}
    for v in grid:
        r = base_row if abs(v - current) < 1e-9 else _row(replace(base_cfg, **{field: v}), cache)
        rows[v] = r
        delta = r["daily"] - base_row["daily"]
        dd_ok = r["ddm"] <= base_row["ddm"] + DD_TOLERANCE_PP
        if dd_ok and delta > best_delta + 1e-9:
            best_val, best_row, best_delta = v, r, delta
    return {"current": current, "best": best_val, "delta": round(best_delta, 1),
            "best_dd": best_row["ddm"], "grid": {k: round(v["daily"], 1) for k, v in rows.items()}}


def monthly_recheck(days: int = 180, tickers: list[str] | None = None) -> dict:
    """Run the TP/SL drift check. Returns a structured result (no side effects)."""
    from .backtest import prefetch_data
    if tickers is None:
        tickers = json.loads((_ROOT / "config" / "watchlist.json").read_text())["tickers"]

    base_cfg = _live_cfg(days, tickers)
    cache = prefetch_data(base_cfg)
    if not cache.get("per_ticker"):
        raise RuntimeError("prefetch returned 0 tickers (OpenD down?) — recheck skipped")
    base_row = _row(base_cfg, cache)

    cur_tp, cur_sl = float(rc.tp_atr_mult()), float(rc.sl_atr_mult())
    # Grids are clamped to runtime_config.ALLOWED_PARAMS bounds so a "best" value
    # is always auto-applicable / approvable (never rejected at execution time).
    tp_grid = _bounded_grid("tp_atr_mult", [cur_tp - 2, cur_tp - 1, cur_tp, cur_tp + 1, cur_tp + 2])
    sl_grid = _bounded_grid("sl_atr_mult", [cur_sl - 1, cur_sl - 0.5, cur_sl, cur_sl + 0.5, cur_sl + 1])

    tp = _best(base_cfg, cache, "tp_atr_mult", tp_grid, cur_tp, base_row)
    sl = _best(base_cfg, cache, "sl_atr_mult", sl_grid, cur_sl, base_row)

    suggestions = []
    for label, r, key in [("TP", tp, "tp_atr_mult"), ("SL", sl, "sl_atr_mult")]:
        if (r["best"] != r["current"] and r["delta"] >= SUGGEST_MIN_DELTA_USD
                and rc.is_valid(key, r["best"])):
            suggestions.append({"param": key, "label": label, "from": r["current"],
                                "to": r["best"], "delta_daily": r["delta"], "dd": r["best_dd"]})

    return {"universe": "watchlist", "days": days, "n_tickers": len(tickers),
            "baseline": base_row, "tp": tp, "sl": sl, "suggestions": suggestions}


def format_telegram(res: dict) -> str:
    b = res["baseline"]
    lines = [f"🔧 *Monthly lever recheck* (watchlist, {res['days']}d, "
             f"{res['n_tickers']} names)",
             f"  baseline: ${b['daily']:+.1f}/day · DD {b['ddm']:.1f}% · "
             f"PF {b['pf']} · {b['trd']} trd"]
    for key, r in [("TP", res["tp"]), ("SL", res["sl"])]:
        if r["best"] == r["current"]:
            lines.append(f"  {key}={r['current']}: still optimal ✓")
        else:
            lines.append(f"  {key}: best={r['best']} (now {r['current']}) "
                         f"→ +${r['delta']}/day, DD {r['best_dd']:.1f}%")
    if res["suggestions"]:
        s = "; ".join(f"{x['param']} {x['from']}→{x['to']} (+${x['delta_daily']}/day)"
                      for x in res["suggestions"])
        lines.append(f"\n💡 *Consider* (一键审批即生效): {s}")
    else:
        lines.append("\nNo change suggested — current exits still win.")
    return "\n".join(lines)


def apply_suggestions(res: dict) -> list[dict]:
    """Enqueue each in-bounds winning lever as a one-click `param_change` approval
    — the SAME queue + runtime_config.set_param + autopilot auto-rollback path the
    weekly optimizer uses. Returns the suggestions that were enqueued.

    We ENQUEUE for a one-tap approval rather than silently auto-applying (even when
    AUTO_APPLY_PARAMS is on): optimizer_ai earns silent apply via a 180d+360d
    DUAL-window gate, but the HOUR_1 data cap (~150d) gives this recheck only ONE
    window, so the owner confirms. Approval applies it live (no restart) with the
    rollback watcher armed. approvals.enqueue de-dupes, so re-suggesting the same
    key month-over-month won't pile up duplicate cards.
    """
    from . import approvals
    enqueued: list[dict] = []
    for s in res.get("suggestions", []):
        key, value = s["param"], float(s["to"])
        gain = (f"回测 +${s['delta_daily']}/day (DD {s['dd']:.1f}%, watchlist {res['days']}d, "
                f"单窗口—HOUR_1 数据上限无第二时间窗)")
        approvals.enqueue(
            kind="param_change",
            detail=f"月度 lever 复验(验证过): {key} {s['from']} → {value} — {gain}",
            action=f"Set {key} = {value} (live, no restart)",
            payload={"key": key, "value": value},
        )
        enqueued.append(s)
    return enqueued
