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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import optuna

from .backtest import BacktestConfig  # config shape only — v4 does the scoring
from .config import settings  # noqa: F401 (re-exported)

# Suppress Optuna's experimental warnings — TPE is fine.
warnings.filterwarnings("ignore", category=optuna.exceptions.ExperimentalWarning)

log = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent.parent / "data" / "optimizer"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------- walk-forward evaluator ----------

def _fold_windows(days: int, n_folds: int) -> list[tuple[datetime, datetime]]:
    """Split the lookback into n_folds NON-OVERLAPPING date ranges.

    This used to partition the finished TRADE LIST into K slices, which is not
    walk-forward — every slice was produced by one run over the whole window,
    so a parameter set was never actually evaluated on a period it had not
    already seen. Date folds cost n_folds runs instead of one; that is the
    price of the claim.
    """
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
    end = datetime.now(_ET).replace(hour=16, minute=0, second=0, microsecond=0)
    span = max(1, days // n_folds)
    out = []
    for k in range(n_folds):
        f_end = end - timedelta(days=span * k)
        out.append((f_end - timedelta(days=span), f_end))
    return list(reversed(out))


def _evaluate_params(
    base_cfg: BacktestConfig,
    params: dict,
    n_folds: int,
    cache: dict,
) -> dict:
    """Score one parameter set on backtest_v4, fold by fold.

    THE ENGINE CHANGED (2026-08-23). This scored with `_run_live_engine`
    (backtest_v3) against a prefetched bar cache — pure CPU, ~30s for a whole
    study. v4 rebuilds its replay feed per run, so a study now costs tens of
    minutes rather than tens of seconds. That is the accepted trade: v3's
    entry chain never had the blacklist, spread or sector gates and its gap
    filter was inverted, so a Sortino it reported was a Sortino for a strategy
    live does not run. `cache` is accepted and ignored, so callers built for
    the old signature keep working.

    Params reach the engine through runtime_config's THREAD-LOCAL override —
    the same mechanism the grid sweep uses — so a trial can never leak into
    db-state and a killed process cannot leave live on a trial's parameters.
    """
    import io, contextlib
    from .backtest_v4 import V4Config, run_v4
    from . import runtime_config as _rc
    from .metrics import compute_full_metrics

    cfg = replace(base_cfg, **params)
    overrides = {
        "entry_threshold": float(cfg.threshold),
        "tp_atr_mult": float(cfg.tp_atr_mult),
        "sl_atr_mult": float(cfg.sl_atr_mult),
    }

    fold_sortinos: list[float] = []
    n_total = 0
    try:
        _rc.push_overrides(overrides)
        for f_start, f_end in _fold_windows(cfg.days, n_folds):
            v4cfg = V4Config(start=f_start, end=f_end,
                             tickers=list(cfg.tickers),
                             universe_mode="static",
                             account_usd=cfg.account_usd)
            try:
                # The engine narrates its data load; a study runs it dozens of
                # times and the log is not the product.
                with contextlib.redirect_stdout(io.StringIO()):
                    res = run_v4(v4cfg)
            except Exception as e:
                log.warning("[optuna] fold %s→%s failed: %s",
                            f_start.date(), f_end.date(), e)
                continue
            trades = res.get("trades", [])
            n_total += len(trades)
            if len(trades) < 5:
                continue
            # compute_full_metrics reads the incumbent trade shape; v4 reports
            # net_pnl (after costs) where that layer expects pnl.
            shaped = [dict(t, pnl=t["net_pnl"],
                           exit_date=t["exit_t"][:10],
                           entry_date=t["entry_t"][:10]) for t in trades]
            m = compute_full_metrics(shaped, cfg.account_usd,
                                     max(cfg.days // n_folds, 30), n_sims=0)
            # Clamp so one thin/no-loss fold (which the metrics layer reports
            # with the downside-unmeasurable sentinel) cannot dominate the mean
            # and let the optimizer overfit to a lucky window.
            fold_sortinos.append(max(-10.0, min(m.get("sortino_ratio", 0.0), 8.0)))
    finally:
        _rc.clear_overrides()

    if not fold_sortinos:
        return {"sortino_mean": -10.0, "n_trades": n_total, "fold_sortinos": [],
                "sortino_min": -10.0}
    return {
        "sortino_mean": sum(fold_sortinos) / len(fold_sortinos),
        "sortino_min": min(fold_sortinos),
        "n_trades": n_total,
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

    # No prefetch step any more: v4 owns its own replay feed (parquet-cached
    # per symbol in data/sandbox_cache), so the first fold warms the cache and
    # every later one reads it. A study is now minutes rather than seconds —
    # see _evaluate_params for why that trade was taken.
    cache: dict = {}

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
