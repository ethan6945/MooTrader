#!/usr/bin/env python3
"""Ask which TARGET the 30-minute features can actually predict.

    .venv/bin/python scripts/signal_target_lab.py

WHY THIS EXISTS
  `signal_estimator_lab.py` established that no estimator predicts three-day
  DIRECTION from these features: every candidate landed at negative
  out-of-sample R^2 and below the majority-class baseline, on dev and holdout
  alike. That is a statement about the target, not about the estimator, so the
  remaining question is whether some other target is predictable from the same
  bars.

  The candidates here are the quantities the desk already prints. The scanner's
  plan block sets a stop at 1.15x ATR and a target at 1.8x R. Those multiples
  are assumptions. If a model forecasts the realized range, or the maximum
  favourable and adverse excursions, better than trailing volatility does, the
  improvement lands directly on numbers the system already shows.

THE BASELINE IS THE POINT
  Volatility is strongly autocorrelated, so a model that "predicts" it will look
  impressive against an unconditional mean while adding nothing over "tomorrow
  resembles today". Every target here is therefore scored against a PERSISTENCE
  baseline that is given its own best fit: an OLS on the single trailing
  statistic, trained on exactly the same rows as the model. `incr` in the output
  is the incremental R^2 over that baseline, and it is the only column that
  answers whether the other 24 features earn their place.

PROTOCOL
  Monthly retraining, purged non-overlapping test origins, dev/holdout split —
  identical to `signal_estimator_lab.py`, for the same reasons.

Reads only the study cache written by `scripts/signal_forecast_study.py`.
No broker calls, no orders, no writes outside stdout.
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import signal_bars, signal_forecast as fe    # noqa: E402

CACHE = Path(__file__).resolve().parent.parent / "data" / "signal_study_cache"
H = 96
HOLDOUT_FROM = "2026-05"
ALPHA = 6.0
MIN_TRAIN = 200


# ── targets and their persistence baselines ───────────────────────────────────
# Each entry returns (target_at_origin, persistence_feature_at_origin), both
# arrays aligned to the frame index, with NaN where undefined.
def build_targets(frame) -> dict[str, tuple[np.ndarray, np.ndarray, str]]:
    close = frame["close"].to_numpy(float)
    high = frame["high"].to_numpy(float)
    low = frame["low"].to_numpy(float)
    n = len(close)
    log_close = np.log(close)
    step = np.diff(log_close, prepend=log_close[0])

    def roll_max(values, window, forward):
        out = np.full(n, np.nan)
        for i in range(n):
            a, b = (i + 1, i + 1 + window) if forward else (i + 1 - window, i + 1)
            if a < 0 or b > n or b <= a:
                continue
            out[i] = values[a:b].max()
        return out

    def roll_min(values, window, forward):
        out = np.full(n, np.nan)
        for i in range(n):
            a, b = (i + 1, i + 1 + window) if forward else (i + 1 - window, i + 1)
            if a < 0 or b > n or b <= a:
                continue
            out[i] = values[a:b].min()
        return out

    def roll_std(values, window, forward):
        out = np.full(n, np.nan)
        for i in range(n):
            a, b = (i + 1, i + 1 + window) if forward else (i + 1 - window, i + 1)
            if a < 0 or b > n or b - a < 2:
                continue
            out[i] = values[a:b].std(ddof=1)
        return out

    fwd_hi, fwd_lo = roll_max(high, H, True), roll_min(low, H, True)
    trl_hi, trl_lo = roll_max(high, H, False), roll_min(low, H, False)
    fwd_vol, trl_vol = roll_std(step, H, True), roll_std(step, H, False)

    with np.errstate(invalid="ignore", divide="ignore"):
        return {
            # log volatility: positive and right-skewed, so modelled in logs
            "vol96 (log realized vol)": (
                np.log(fwd_vol), np.log(trl_vol), "log trailing vol"),
            "range96 (high-low / close)": (
                (fwd_hi - fwd_lo) / close, (trl_hi - trl_lo) / close, "trailing range"),
            "mfe96 (max favourable)": (
                fwd_hi / close - 1.0, (trl_hi - trl_lo) / close, "trailing range"),
            "mae96 (max adverse)": (
                fwd_lo / close - 1.0, (trl_hi - trl_lo) / close, "trailing range"),
            "dir96 (log return) [control]": (
                np.concatenate([log_close[H:] - log_close[:-H], np.full(H, np.nan)]),
                np.concatenate([np.full(H, np.nan), log_close[H:] - log_close[:-H]]),
                "trailing return"),
        }


def ridge(X, y, alpha):
    xm = X.mean(0); xs = X.std(0, ddof=0); xs = np.where(xs > 1e-12, xs, 1.0)
    Z = np.clip((X - xm) / xs, -8, 8)
    ym = y.mean()
    coef = np.linalg.solve(Z.T @ Z + alpha * np.eye(Z.shape[1]), Z.T @ (y - ym))
    return xm, xs, ym, coef


def apply(model, X):
    xm, xs, ym, coef = model
    return ym + np.clip((X - xm) / xs, -8, 8) @ coef


print("loading + building features …", flush=True)
DATA, SYMS = {}, []
for path in sorted(CACHE.glob("*-249.json.gz")):
    sym = path.name.split("-")[0]
    with gzip.open(path, "rt") as handle:
        bars = json.load(handle)
    try:
        d = fe._validate_input(sym, "3D", bars, signal_bars.market_as_of(bars),
                               trim_latest_sessions=False)
    except fe.ForecastInputError:
        continue
    features = fe._build_features(d.frame)
    DATA[sym] = dict(V=features.to_numpy(float), ok=fe._feature_origins(features),
                     times=d.frame.index, targets=build_targets(d.frame),
                     n=len(d.frame))
    SYMS.append(sym)
print(f"  {len(SYMS)} symbols", flush=True)

TARGET_NAMES = list(next(iter(DATA.values()))["targets"])

# purged, non-overlapping test origins
samples = []
for sym in SYMS:
    d = DATA[sym]
    sched, nxt = [], d["n"]
    for t in reversed([int(o) for o in d["ok"] if o + H < d["n"]]):
        if t + H + 1 < nxt:
            sched.append(t); nxt = t
    for t in reversed(sched):
        samples.append((sym, t, str(d["times"][t])[:7], d["times"][t + H]))
months = sorted({m for _s, _t, m, _st in samples})
print(f"  {len(samples)} test origins over {len(months)} months", flush=True)

results = {name: {"model": [], "persist": [], "actual": [], "month": []}
           for name in TARGET_NAMES}

for month in months:
    tests = [s for s in samples if s[2] == month]
    cutoff = min(st for _s, _t, _m, st in tests)
    for name in TARGET_NAMES:
        rows_x, rows_p, rows_y = [], [], []
        for sym in SYMS:
            d = DATA[sym]
            tgt, per, _lbl = d["targets"][name]
            idx = np.array([t for t in d["ok"]
                            if t + H < d["n"] and d["times"][t + H] < cutoff], dtype=int)
            if len(idx) == 0:
                continue
            good = np.isfinite(tgt[idx]) & np.isfinite(per[idx]) & np.isfinite(d["V"][idx]).all(1)
            idx = idx[good]
            if len(idx) == 0:
                continue
            rows_x.append(d["V"][idx]); rows_p.append(per[idx]); rows_y.append(tgt[idx])
        if not rows_x:
            continue
        X = np.vstack(rows_x); P = np.concatenate(rows_p); Y = np.concatenate(rows_y)
        if len(Y) < MIN_TRAIN:
            continue
        model = ridge(X, Y, ALPHA)
        base = ridge(P.reshape(-1, 1), Y, 1e-6)     # persistence, best-fitted
        for sym, t, _m, _st in tests:
            d = DATA[sym]
            tgt, per, _lbl = d["targets"][name]
            x, p, y = d["V"][t], per[t], tgt[t]
            if not (np.isfinite(x).all() and np.isfinite(p) and np.isfinite(y)):
                continue
            results[name]["model"].append(float(apply(model, x)))
            results[name]["persist"].append(float(apply(base, np.array([p]))))
            results[name]["actual"].append(float(y))
            results[name]["month"].append(_m)
    print(f"  {month}", flush=True)


def r2(pred, actual):
    return 1 - float(np.sum((actual - pred) ** 2)) / float(np.sum((actual - actual.mean()) ** 2))


hdr = "%-30s %7s %9s %9s %9s %9s" % ("target", "n", "R2 model", "R2 persist", "incr R2", "corr")
for split, keep in (("DEV  (2025-09 .. 2026-04)", lambda m: m < HOLDOUT_FROM),
                    ("HOLDOUT (2026-05 .. 2026-08)", lambda m: m >= HOLDOUT_FROM)):
    print(f"\n{split}\n{'-'*80}\n{hdr}\n{'-'*80}")
    for name in TARGET_NAMES:
        r = results[name]
        mask = np.array([keep(m) for m in r["month"]], dtype=bool)
        if mask.sum() < 50:
            continue
        M = np.array(r["model"])[mask]; B = np.array(r["persist"])[mask]
        A = np.array(r["actual"])[mask]
        rm, rb = r2(M, A), r2(B, A)
        corr = float(np.corrcoef(M, A)[0, 1]) if M.std() > 1e-12 else 0.0
        print("%-30s %7d %9.4f %9.4f %+9.4f %9.4f" % (name, len(A), rm, rb, rm - rb, corr))
