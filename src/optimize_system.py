#!/usr/bin/env python3
"""
Auto-optimizer v2 — grid-search best params via sandbox backtests, cross-window
aggregation, and DIRECT auto-apply (owner directive 2026-07-07: no approval
step — "确认是最优化了就直接套用").

RUNS: Weekly via cron (before the Monday 20:00 KL autopilot chain).
MECHANISM: params are injected into db-state param_* keys (sandbox reads
runtime_config); the winner is applied through runtime_config.set_param() so
param_history records the change and Telegram is notified (铁律: never silent).

ARCHITECTURE (v2, 2026-07-07 audit rewrite):
  1. Rolling 30-day windows over the last 60 days (matches the 2026-07-02
     owner decision: automated tuning validates on recent 60d OpenD data).
  2. Grid: entry_threshold × tp_atr_mult × sl_atr_mult, COMBO-OUTER loop —
     every combo is scored on EVERY window and aggregated (mean), instead of
     the old best-single-window pick that selected on luck.
  3. The CURRENT live params run as a baseline combo through the identical
     pipeline; a challenger must beat the baseline aggregate to be applied.
  4. Best combo auto-applied via set_param(source="optimizer_auto") + Telegram.

SAFETY:
  - Sweep injections write param_* directly (no param_history pollution);
    try/finally ALWAYS restores pre-sweep params, crash or not.
  - Values clamped to ALLOWED_PARAMS bounds.
  - A combo needs ≥3 trades in ≥2 windows to be eligible; the winner must
    also show positive mean net PnL. Otherwise: no change.
"""

import json, sys, time as _time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# 2026-07-28: moved from scripts/ into src/ so the grid sweep ships inside the
# packaged .app. scripts/ is NOT bundled (see _weekly_sandbox_diff_job's
# IS_FROZEN guard), so while this lived there the whole optimization pipeline
# existed only on a machine with the repo checked out AND a working crontab —
# i.e. never for anyone who installs the app. It is now an ordinary module that
# src/main.py schedules directly.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))   # keeps `python -m src.optimize_system` working
ET = ZoneInfo("America/New_York")

from . import runtime_config
from . import db as _db
from .sandbox import run_sandbox, SandboxConfig
from .config import settings

# ── Search grid ──────────────────────────────────────────
GRID_QUICK = {
    "entry_threshold": [65, 70, 75],
    "tp_atr_mult":     [8.0, 10.0, 12.0],
    "sl_atr_mult":     [2.5, 3.5, 4.5],
}   # 27 combos

GRID_FULL = {
    "entry_threshold": [55, 60, 65, 70, 75, 80],
    "tp_atr_mult":     [6.0, 8.0, 10.0, 12.0, 14.0],
    "sl_atr_mult":     [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0],
}   # 210 combos

# Validation geometry — 60d lookback keeps every window inside the ~100
# calendar days of hourly history the sandbox feed holds (500 bars), so no
# window silently replays on missing data.
LOOKBACK_DAYS = 60
WINDOW_DAYS = 30
STEP_DAYS = 15
MIN_TRADES_PER_WINDOW = 3
MIN_VALID_WINDOWS = 2

PARAM_KEYS = ("entry_threshold", "tp_atr_mult", "sl_atr_mult")

# ── Daily mode (2026-07-07, owner request) ──────────────────────────────────
# Every morning (09:00 MYT, market closed) the incremental cache tops up
# yesterday's bars and a NEIGHBORHOOD walk re-validates the CURRENT params ±1
# step per axis. This is deliberately NOT a daily full-grid re-pick — daily
# global re-optimization on ~10-trade samples is noise-chasing. Hill-climb
# with hysteresis instead: a challenger replaces the incumbent only when it
# beats it by DAILY_HYSTERESIS on the aggregate score, and at most ONE param
# moves per day (single best axis step). The weekly Monday full grid remains
# the broad search that can escape local optima.
DAILY_STEPS = {"entry_threshold": 5.0, "tp_atr_mult": 1.0, "sl_atr_mult": 0.3}
DAILY_HYSTERESIS = 1.25          # challenger agg must be ≥ 1.25× baseline agg
DAILY_LOG = ROOT / "data" / "optimizer_daily_log.jsonl"


def _neighbor_combos(saved: dict) -> list[tuple[float, float, float]]:
    """Axis-wise ±1-step neighbors of the current params (each differs from
    the baseline in exactly ONE parameter), bounds-checked and deduped."""
    th0, tp0, sl0 = (saved["entry_threshold"], saved["tp_atr_mult"],
                     saved["sl_atr_mult"])
    out: list[tuple[float, float, float]] = []
    for key, step in DAILY_STEPS.items():
        for sign in (-1.0, +1.0):
            th, tp, sl = th0, tp0, sl0
            if key == "entry_threshold":
                th = round(th0 + sign * step)
            elif key == "tp_atr_mult":
                tp = round(tp0 + sign * step, 1)
            else:
                sl = round(sl0 + sign * step, 1)
            c = (float(th), float(tp), float(sl))
            if c == (th0, tp0, sl0) or c in out:
                continue
            if all(runtime_config.is_valid(k, v)
                   for k, v in zip(PARAM_KEYS, c)):
                out.append(c)
    return out


DAILY_LOG_MAX_RECORDS = 1000   # ≈3 years of daily+weekly runs (~350 B each)


def _append_daily_log(record: dict) -> None:
    """Per-run archive (owner: 每天跑的数据都保留起来，不会忘记).

    Self-bounding: keeps the most recent DAILY_LOG_MAX_RECORDS lines
    (~350 KB ceiling) so the archive can never grow without limit."""
    try:
        DAILY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DAILY_LOG, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        lines = DAILY_LOG.read_text().splitlines()
        if len(lines) > DAILY_LOG_MAX_RECORDS:
            tmp = DAILY_LOG.with_suffix(".tmp")
            tmp.write_text("\n".join(lines[-DAILY_LOG_MAX_RECORDS:]) + "\n")
            tmp.replace(DAILY_LOG)
    except Exception as e:
        print(f"WARN: daily log append failed: {e}")


# ── Scoring ─────────────────────────────────────────────

def _score(net_pnl: float, win_rate: float, n_trades: int, avg_loss: float) -> float | None:
    """Per-window score; None = window not valid for this combo (too few trades)."""
    if n_trades < MIN_TRADES_PER_WINDOW:
        return None
    actual_avg_loss = abs(avg_loss) if abs(avg_loss) > 1 else 1.0
    stability = win_rate / actual_avg_loss
    activity = min(n_trades / 6.0, 2.0)   # saturates at 12 trades
    return max(0.0, net_pnl) * stability * activity


# ── Rolling windows ─────────────────────────────────────

def _windows(end: datetime) -> list[tuple[datetime, datetime]]:
    start = end - timedelta(days=LOOKBACK_DAYS)
    wins, w_end = [], end
    while True:
        w_start = max(w_end - timedelta(days=WINDOW_DAYS), start)
        if w_start < w_end:
            wins.append((w_start, w_end))
        w_end = w_end - timedelta(days=STEP_DAYS)
        if w_start <= start:
            break
    return wins


def _snap_weekdays(w_start: datetime, w_end: datetime) -> tuple[datetime, datetime]:
    while w_start.weekday() >= 5:
        w_start += timedelta(days=1)
    while w_end.weekday() >= 5:
        w_end -= timedelta(days=1)
    return w_start, w_end


# ── Combo evaluation ────────────────────────────────────

def _inject(th: float, tp: float, sl: float) -> None:
    """Direct param_* write — bypasses set_param so the sweep doesn't flood
    param_history (which powers the autopilot's evidence-based rollback)."""
    _db.update_state({
        "param_entry_threshold": float(th),
        "param_tp_atr_mult": float(tp),
        "param_sl_atr_mult": float(sl),
    })


def _eval_combo(th: float, tp: float, sl: float,
                windows: list, pool: list[str], quiet: bool) -> dict:
    """Run one combo across ALL windows; return the aggregate record."""
    _inject(th, tp, sl)
    per_window, scores, pnls = [], [], []
    for w_start, w_end in windows:
        cfg = SandboxConfig(
            start=w_start if w_start.tzinfo else w_start.replace(tzinfo=ET),
            end=w_end if w_end.tzinfo else w_end.replace(tzinfo=ET),
            tickers=pool,
            universe_mode="dynamic",
            enable_ai=False,
        )
        try:
            s = run_sandbox(cfg).get("summary", {})
        except Exception as e:
            if not quiet:
                print(f"    FAIL window {w_start.date()} th={th} tp={tp} sl={sl}: {e}")
            per_window.append({"window": f"{w_start.date()}→{w_end.date()}", "error": str(e)})
            continue
        sc = _score(s.get("net_pnl_usd", 0.0), s.get("win_rate_pct", 0.0),
                    s.get("total_trades", 0), s.get("avg_loss_usd", 0.0))
        per_window.append({
            "window": f"{w_start.date()}→{w_end.date()}",
            "pnl": s.get("net_pnl_usd", 0.0), "trades": s.get("total_trades", 0),
            "win_rate": s.get("win_rate_pct", 0.0),
            "pf": s.get("profit_factor", 0),
            "score": round(sc, 2) if sc is not None else None,
        })
        if sc is not None:
            scores.append(sc)
            pnls.append(s.get("net_pnl_usd", 0.0))
    eligible = len(scores) >= MIN_VALID_WINDOWS
    return {
        "th": float(th), "tp": float(tp), "sl": float(sl),
        "agg_score": round(sum(scores) / len(scores), 2) if eligible else -1.0,
        "mean_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "valid_windows": len(scores), "windows": per_window,
        "eligible": eligible,
    }


# ── Main ────────────────────────────────────────────────

def optimize(quick: bool = False, quiet: bool = False,
             daily: bool = False, force: bool = False) -> dict:
    """Grid search with cross-window aggregation.

    quick: 27-combo grid on the 2 most recent windows (~20 min cold cache).
    daily: NEIGHBORHOOD walk (current params ±1 step per axis, ≤7 combos, 2
           windows, ~2-3 min warm cache) with 1.25× hysteresis and at most
           one param change — the every-morning re-validation.
    full (default): 210 combos on all windows.
    The winner must beat the CURRENT live params run through the identical
    pipeline, then it is auto-applied (owner directive 2026-07-07)."""
    now = datetime.now(ET)

    # ── Safety interlock: the sweep INJECTS grid params into live db-state.
    # A concurrently-scanning scheduler would trade on them. Only run when
    # the US market is closed (all crons are scheduled accordingly).
    if not force:
        try:
            from src import clock
            sess = clock.market_session(clock.ny_now())
        except Exception:
            sess = "unknown"
        if sess == "open":
            msg = ("REFUSED: US market is OPEN — the sweep injects live params "
                   "and a scanning scheduler would trade on grid values. "
                   "Run while closed, or --force after stopping the scheduler.")
            print(msg)
            return {"refused": "market_open", "note": msg}

    end_date = now.replace(hour=16, minute=0, second=0, microsecond=0)

    try:
        from .universe import load_pool
        pool = load_pool()
    except Exception:
        pool = ["AAPL", "MSFT", "NVDA", "AMD", "GOOGL"]

    windows = [_snap_weekdays(a, b) for a, b in _windows(end_date)]
    windows = [(a, b) for a, b in windows if a < b]
    if quick or daily:
        windows = windows[:2]   # two most recent windows

    saved = {k: float(runtime_config.current(k)) for k in PARAM_KEYS}
    baseline_combo = (saved["entry_threshold"], saved["tp_atr_mult"], saved["sl_atr_mult"])

    combos: list[tuple[float, float, float]] = [baseline_combo]
    if daily:
        combos += _neighbor_combos(saved)
    else:
        grid = GRID_QUICK if quick else GRID_FULL
        for th in grid["entry_threshold"]:
            for tp in grid["tp_atr_mult"]:
                for sl in grid["sl_atr_mult"]:
                    c = (float(th), float(tp), float(sl))
                    if c != baseline_combo and all(
                        runtime_config.is_valid(k, v)
                        for k, v in zip(PARAM_KEYS, c)
                    ):
                        combos.append(c)

    mode_name = "daily" if daily else ("quick" if quick else "full")
    if not quiet:
        print(f"Optimizer v2 [{mode_name}]: {len(combos)} combos (incl. baseline) × {len(windows)} windows")
        print(f"Pool: {len(pool)} tickers | Baseline: th={saved['entry_threshold']} "
              f"tp={saved['tp_atr_mult']} sl={saved['sl_atr_mult']}")

    t0 = _time.time()
    results: list[dict] = []
    try:
        for i, (th, tp, sl) in enumerate(combos):
            rec = _eval_combo(th, tp, sl, windows, pool, quiet)
            rec["is_baseline"] = (th, tp, sl) == baseline_combo
            results.append(rec)
            if not quiet and rec["eligible"]:
                tag = "BASELINE" if rec["is_baseline"] else f"combo {i}"
                print(f"  [{tag}] th={th:g} tp={tp:g} sl={sl:g} → "
                      f"agg={rec['agg_score']:.1f} meanPnL=${rec['mean_pnl']:.0f} "
                      f"({rec['valid_windows']}/{len(windows)} windows)")
    finally:
        # ALWAYS restore pre-sweep params — the live scheduler must never be
        # left running on a grid combo, crash or not.
        try:
            _db.update_state({f"param_{k}": v for k, v in saved.items()})
        except Exception as e:
            print(f"CRITICAL: failed to restore pre-sweep params {saved}: {e}")

    baseline = next((r for r in results if r.get("is_baseline")), None)
    baseline_score = baseline["agg_score"] if (baseline and baseline["eligible"]) else 0.0
    challengers = [r for r in results
                   if not r.get("is_baseline") and r["eligible"] and r["mean_pnl"] > 0]
    best = max(challengers, key=lambda r: r["agg_score"], default=None)

    # Apply bar: weekly/full modes require a strict beat; DAILY mode requires
    # the 1.25× hysteresis margin so one new day of data can't thrash params.
    required = (max(baseline_score, 0.0) * DAILY_HYSTERESIS
                if daily and baseline_score > 0 else max(baseline_score, 0.0))

    applied = False
    if best and best["agg_score"] > required:
        # Winner beats the incumbent on the identical pipeline → APPLY.
        # (Owner directive 2026-07-07: validated best is applied directly,
        # no approval step.) set_param records param_history + bounds-checks.
        changes = []
        for key, val in zip(PARAM_KEYS, (best["th"], best["tp"], best["sl"])):
            if float(val) != saved[key]:
                runtime_config.set_param(key, float(val), "optimizer_auto")
                changes.append(f"{key}: {saved[key]:g} → {val:g}")
        applied = bool(changes)
        if applied:
            title = "每日优化器" if daily else "Weekly optimizer"
            msg = (f"🔧 *{title} AUTO-APPLIED*\n"
                   + "\n".join(f"  • {c}" for c in changes)
                   + f"\n  agg score {best['agg_score']:.1f} vs baseline {baseline_score:.1f}"
                   + (f" (需 ≥{required:.1f})" if daily else "")
                   + f" | mean PnL ${best['mean_pnl']:.0f}"
                   f" ({best['valid_windows']}/{len(windows)} windows)"
                   "\n  下一次扫描生效（runtime 覆盖，无需重启）")
            try:
                from src import notifier
                notifier.send(msg)
            except Exception as e:
                print(f"WARN: telegram notify failed: {e}")
            if not quiet:
                print(msg)
    elif not quiet:
        why = ("no eligible challenger beat the baseline"
               if challengers else "no combo produced ≥3 trades in ≥2 windows with positive PnL")
        print(f"No change applied — {why} (baseline agg={baseline_score:.1f})")

    elapsed = _time.time() - t0
    results.sort(key=lambda r: r["agg_score"], reverse=True)
    output = {
        "generated_at": now.isoformat(),
        "version": "v2-aggregated",
        "mode": mode_name,
        "lookback_days": LOOKBACK_DAYS,
        "windows": [f"{a.date()}→{b.date()}" for a, b in windows],
        "combos_tested": len(results),
        "elapsed_sec": round(elapsed, 1),
        "baseline": baseline,
        "best": best,
        "applied": applied,
        "saved_params": saved,
        "top5": results[:5],
    }
    out_path = ROOT / "data" / "optimized_params.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=str))

    # Permanent per-run archive (compact — no window detail; the full detail
    # lives in optimized_params.json until the next run overwrites it).
    _append_daily_log({
        "ts": now.isoformat(timespec="seconds"),
        "mode": mode_name,
        "baseline": {k: saved[k] for k in PARAM_KEYS} | {
            "agg": baseline_score,
            "mean_pnl": (baseline or {}).get("mean_pnl")},
        "best": ({"th": best["th"], "tp": best["tp"], "sl": best["sl"],
                  "agg": best["agg_score"], "mean_pnl": best["mean_pnl"]}
                 if best else None),
        "applied": applied,
        "combos": len(results),
        "windows": len(windows),
        "elapsed_sec": round(elapsed, 1),
    })

    if not quiet:
        print(f"\n{'='*60}")
        print(f"OPTIMIZER v2 DONE — {elapsed:.0f}s | {len(results)} combos × {len(windows)} windows")
        if applied and best:
            print(f"APPLIED: th={best['th']:g} tp={best['tp']:g} sl={best['sl']:g} "
                  f"(agg {best['agg_score']:.1f} > baseline {baseline_score:.1f})")
        else:
            print("Params unchanged (baseline holds)")
        print(f"Saved: {out_path}")
    return output


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="27-combo grid on the 2 most recent windows")
    ap.add_argument("--daily", action="store_true",
                    help="neighborhood walk around current params (±1 step, "
                         "1.25x hysteresis, ≤1 param change) — the 09:00 cron")
    ap.add_argument("--force", action="store_true",
                    help="override the market-open safety interlock (make "
                         "sure the scheduler is STOPPED first)")
    ap.add_argument("--quiet", action="store_true", help="Less console output")
    args = ap.parse_args()
    optimize(quick=args.quick, quiet=args.quiet, daily=args.daily, force=args.force)
