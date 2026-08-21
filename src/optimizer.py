"""Walk-forward parameter optimization via Optuna (Bayesian/TPE search).

Why this exists:
  The live system has ~6 magic numbers that were picked by feel
  (entry threshold, ATR multiples, max gap %, slippage). Without
  walk-forward + out-of-sample evaluation, "tuning" by hand is
  guaranteed to overfit. This module:

    1. Splits the lookback window into K non-overlapping test folds.
    2. For each Optuna trial (param set), runs the backtest on EACH fold.
    3. Reports the AVERAGE out-of-sample Sortino across folds.
    4. Optuna's TPE sampler then proposes new param sets that maximise OOS.

Why Sortino, not PnL:
  PnL rewards luck. Sortino penalises downside vol — a parameter set with
  smooth equity will beat one with a few big spikes, which is what you
  actually want to deploy with real money.

Usage:
  python -m src.optimizer --trials 30 --days 180 --folds 3
  python -m src.optimizer --tickers AAPL NVDA TSLA --trials 20
"""
from __future__ import annotations

import argparse
import json
import logging
import warnings
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Optional

import optuna

from .backtest import (BacktestConfig, prefetch_data, simulate_with_cache,  # noqa: F401
                       _run_live_engine)
from .config import settings  # noqa: F401 (re-exported)

# Suppress Optuna's experimental warnings — TPE is fine.
warnings.filterwarnings("ignore", category=optuna.exceptions.ExperimentalWarning)

log = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent.parent / "data" / "optimizer"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------- walk-forward evaluator ----------

def _evaluate_params(
    base_cfg: BacktestConfig,
    params: dict,
    n_folds: int,
    cache: dict,
) -> dict:
    """Re-simulate with new params on the cached data. NO OpenD calls here.

    `cache` is what prefetch_data() returned — passed in so we don't refetch
    historical bars on every Optuna trial. This is the speedup that turns a
    25-trial study from ~2 hours into ~30 seconds.
    """
    from .metrics import compute_full_metrics

    cfg = replace(base_cfg, **params)
    # Phase 2c (2026-06-01): optimise on the HONEST cash-walled engine, not the
    # leveraged oracle. _run_live_engine applies the real $5k cash wall + VIX
    # sizing + earnings gate + MY commissions (enforce_cash=True), so the param
    # optimum Optuna finds is the one that actually deploys on the account. The
    # incumbent tp=6.0/sl=3.25 was tuned on simulate_with_cache, which can hold
    # unlimited positions — a different, more aggressive optimum than a cash
    # account wants. rich_metrics off: we only need the trade list for the
    # per-fold Sortino below (skips the Monte-Carlo cost on every trial).
    result = _run_live_engine(cfg, cache, rich_metrics=False)
    trades = result.get("trades", [])
    if not trades:
        return {"sortino_mean": -10.0, "n_trades": 0, "fold_sortinos": [],
                "sortino_min": -10.0}

    # Sort by exit_date and split into K folds.
    sorted_t = sorted(trades, key=lambda t: t.get("exit_date", ""))
    n = len(sorted_t)
    if n < n_folds:
        n_folds = max(1, n // 5) or 1   # avoid empty folds
    fold_size = max(1, n // n_folds)

    fold_sortinos: list[float] = []
    for k in range(n_folds):
        start = k * fold_size
        end = (k + 1) * fold_size if k < n_folds - 1 else n
        fold = sorted_t[start:end]
        if len(fold) < 5:
            continue
        m = compute_full_metrics(fold, cfg.account_usd, max(cfg.days // n_folds, 30),
                                 n_sims=0)
        # 2026-06-03: clamp the fold Sortino so a single thin/no-loss fold (which
        # the metrics layer reports as the "downside-unmeasurable" sentinel) can't
        # dominate the mean and let the optimizer overfit to a lucky window.
        fold_sortinos.append(max(-10.0, min(m.get("sortino_ratio", 0.0), 8.0)))

    if not fold_sortinos:
        return {"sortino_mean": -10.0, "n_trades": n, "fold_sortinos": [],
                "sortino_min": -10.0}

    return {
        "sortino_mean": sum(fold_sortinos) / len(fold_sortinos),
        "sortino_min": min(fold_sortinos),   # worst-fold robustness check
        "n_trades": n,
        "fold_sortinos": [round(x, 3) for x in fold_sortinos],
    }


# ---------- Optuna objective ----------

def _make_objective(base_cfg: BacktestConfig, n_folds: int, min_trades: int, cache: dict):
    def objective(trial: optuna.Trial) -> float:
        # 2026-05-28: widened ranges to span the new W3 aggressive regime.
        # Old bounds (tp 1-3 / sl 1.5-3.5) couldn't reach the actual optimum
        # of tp=6.0 / sl=3.25 — Optuna was searching the wrong neighbourhood.
        params = {
            # 2026-06-01 (Phase 2c honest retune): widened the upper bound 72→80.
            # The honest $5k cash wall makes SELECTIVITY more valuable than under
            # the leveraged oracle — fewer, higher-conviction entries leave cash
            # for the best signals instead of starving slots 3-5. Let Optuna reach
            # a higher threshold if the cash-constrained optimum lives there.
            "threshold":       trial.suggest_int("threshold", 55, 80, step=1),
            # 2026-06-11: upper bound 8.0 → 11.0. The live value IS 8.0 — with
            # the range capped at it, the tuner could never test the upward
            # neighbourhood needed to confirm (or move) the plateau.
            "tp_atr_mult":     trial.suggest_float("tp_atr_mult", 3.5, 11.0, step=0.5),
            "sl_atr_mult":     trial.suggest_float("sl_atr_mult", 2.5, 4.0, step=0.25),
            "max_gap_pct":     trial.suggest_float("max_gap_pct", 2.0, 5.0, step=0.5),
            # 2026-06-11: base_slip_bp REMOVED from the search space. It is a
            # friction ASSUMPTION, not a strategy parameter — trials paired
            # with optimistic slippage scored higher, so "best params" came
            # systematically attached to slip≈1.0 and overstated expectancy.
            # All trials now pay the same fixed friction (cfg default 2.0).
        }
        try:
            stats = _evaluate_params(base_cfg, params, n_folds, cache)
        except Exception as e:
            import traceback
            log.warning("trial failed: %s\n%s", e, traceback.format_exc())
            return -10.0

        # Tell Optuna why we like / don't like this trial.
        trial.set_user_attr("n_trades", stats["n_trades"])
        trial.set_user_attr("fold_sortinos", stats["fold_sortinos"])
        trial.set_user_attr("sortino_min", round(stats["sortino_min"], 3))

        # Reject trials that just got lucky on a few trades.
        if stats["n_trades"] < min_trades:
            return -10.0 + stats["n_trades"] / max(min_trades, 1)
        # Combined objective: 70% mean Sortino + 30% worst-fold Sortino —
        # penalises strategies that look good on average but blow up in one
        # window.
        score = 0.7 * stats["sortino_mean"] + 0.3 * stats["sortino_min"]
        return float(score)

    return objective


# ---------- CLI ----------

def run_study(
    n_trials: int,
    days: int,
    n_folds: int,
    min_trades: int,
    tickers: Optional[list[str]] = None,
    timeframe: Optional[str] = None,
    fast_mode: bool = True,
    apply_mr_strategy: Optional[bool] = None,
) -> dict:
    """Run the Optuna study.

    `fast_mode=True` disables the mean-revert gate during the search. That gate
    is NOT a pure filter — it ADDS signals at different score levels, so
    toggling it shifts the score distribution and therefore the threshold
    optimum. To tune the engine production actually runs, pass the flag
    EXPLICITLY (it overrides the fast_mode-derived default): production =
    `apply_mr_strategy=False`. Leaving it None preserves the legacy fast_mode
    behaviour.

    There used to be an `apply_ml_gate` here too, described as a pure veto and
    reported in the fidelity log below. Nothing read it — there is no ML in
    this codebase at all — so the log asserted a fidelity it had not checked.
    """
    mr_on = (not fast_mode) if apply_mr_strategy is None else apply_mr_strategy
    # 2026-06-11: base config comes from optimizer_ai._base_cfg — the single
    # source of "the strategy the bot actually runs" (runtime-effective params,
    # dynamic universe walk-forward when enabled). Tuning on the live watchlist
    # file would optimize against a hindsight-selected list.
    from dataclasses import replace as _replace
    from .optimizer_ai import _base_cfg
    base_cfg = _base_cfg(days=days)
    base_cfg = _replace(
        base_cfg,
        timeframe=timeframe or base_cfg.timeframe,
        tickers=tickers or base_cfg.tickers,
        apply_mr_strategy=mr_on,
    )
    log.info("[optuna] search engine fidelity: apply_mr_strategy=%s "
             "(production = False)", mr_on)

    # Pre-fetch all kline data ONCE. Every trial after this is pure CPU
    # (no OpenD calls), making 25 trials take ~30s instead of ~2 hours.
    log.info("Prefetching market data for %s tickers, %d days @ %s...",
             len(base_cfg.tickers) or "watchlist", base_cfg.days, base_cfg.timeframe)
    t0 = datetime.utcnow()
    cache = prefetch_data(base_cfg)
    log.info("Prefetch done in %.1fs — cached %d tickers",
             (datetime.utcnow() - t0).total_seconds(), len(cache["per_ticker"]))

    # TPE sampler with deterministic seed → reproducible search.
    sampler = optuna.samplers.TPESampler(seed=42)
    pruner = optuna.pruners.MedianPruner(n_warmup_steps=5)
    study = optuna.create_study(
        direction="maximize", sampler=sampler, pruner=pruner,
        study_name=f"moo-trader-{datetime.utcnow().strftime('%Y%m%d-%H%M')}",
    )

    objective = _make_objective(base_cfg, n_folds, min_trades, cache)
    log.info("Starting Optuna study: %d trials, %d folds, min_trades=%d",
             n_trials, n_folds, min_trades)
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    # Pull best trial + a summary of the top-5 for inspection.
    best = study.best_trial
    top5 = sorted(study.trials, key=lambda t: (t.value or -99), reverse=True)[:5]
    summary = {
        "study_name": study.study_name,
        "n_trials": n_trials,
        "n_folds": n_folds,
        "min_trades": min_trades,
        "base_config": {
            "days": base_cfg.days,
            "timeframe": base_cfg.timeframe,
            "tickers": tickers or "watchlist",
        },
        "best_value_sortino": round(best.value, 3) if best.value else None,
        "best_params": best.params,
        "best_user_attrs": dict(best.user_attrs),
        "top_5_trials": [
            {
                "value": round(t.value, 3) if t.value else None,
                "params": t.params,
                "user_attrs": dict(t.user_attrs),
            }
            for t in top5
        ],
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    out = RESULTS_DIR / f"{study.study_name}.json"
    out.write_text(json.dumps(summary, indent=2, default=str))
    log.info("Study saved to %s", out)
    return summary


def print_summary(s: dict) -> None:
    print("\n" + "=" * 64)
    print(f"  OPTUNA WALK-FORWARD STUDY  |  {s['study_name']}")
    print("=" * 64)
    print(f"  Trials             : {s['n_trials']}  |  Folds: {s['n_folds']}")
    print(f"  Timeframe          : {s['base_config']['timeframe']}  |  "
          f"Days: {s['base_config']['days']}")
    print(f"  Min trades per fold: {s['min_trades']}")
    print()
    print(f"  ★ Best Sortino (combined OOS): {s['best_value_sortino']}")
    print(f"    n_trades        : {s['best_user_attrs'].get('n_trades', '?')}")
    print(f"    fold sortinos   : {s['best_user_attrs'].get('fold_sortinos', [])}")
    print(f"    worst-fold      : {s['best_user_attrs'].get('sortino_min', '?')}")
    print()
    print("  ★ Best parameters (paste into .env or backtest CLI):")
    for k, v in s["best_params"].items():
        env_key = {
            "threshold": "ENTRY_SCORE_THRESHOLD",
            "tp_atr_mult": "(backtest --tp-atr)",
            "sl_atr_mult": "(backtest --sl-atr)",
            "max_gap_pct": "(backtest --max-gap)",
            "base_slip_bp": "(backtest --slip-bp)",
        }.get(k, k)
        print(f"    {k:<18} = {v:<8}    →  {env_key}")
    print()
    print("  Top 5 trials:")
    for i, t in enumerate(s["top_5_trials"], 1):
        params_str = ", ".join(f"{k}={v}" for k, v in t["params"].items())
        n_tr = t["user_attrs"].get("n_trades", "?")
        worst = t["user_attrs"].get("sortino_min", "?")
        print(f"    #{i}  Sortino={t['value']:<7}  n_trades={n_tr:<4}  worst={worst}")
        print(f"        {params_str}")
    print("=" * 64 + "\n")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="Walk-forward Optuna optimizer")
    ap.add_argument("--trials", type=int, default=30, help="Optuna trials")
    ap.add_argument("--days", type=int, default=180, help="Backtest history")
    ap.add_argument("--folds", type=int, default=3, help="OOS test folds")
    ap.add_argument("--min-trades", type=int, default=30,
                    help="Trials with fewer trades than this are penalised")
    ap.add_argument("--tickers", nargs="*", help="Specific tickers (default: watchlist)")
    ap.add_argument("--timeframe", default=None)
    ap.add_argument("--full", action="store_true",
                    help="Enable ML + MR gates (slower; default is fast mode)")
    args = ap.parse_args()

    summary = run_study(
        n_trials=args.trials,
        days=args.days,
        n_folds=args.folds,
        min_trades=args.min_trades,
        tickers=args.tickers,
        timeframe=args.timeframe,
        fast_mode=not args.full,
    )
    print_summary(summary)


if __name__ == "__main__":
    main()
