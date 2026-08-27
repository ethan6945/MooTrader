"""Compare candidate estimators for the 3-day forecast on identical test origins.

    .venv/bin/python scripts/signal_estimator_lab.py

WHY THIS EXISTS
  When the shipped ridge turned out to have no measurable skill, the obvious
  next move was to swap it for something else. This is the harness that answers
  whether any given "something else" is actually better, rather than better on
  the stretch of tape it was chosen on.

PROTOCOL, AND WHY EACH PIECE IS THERE
  MONTHLY RETRAINING. At test month M every candidate is fitted only on samples
  whose 96-bar target had SETTLED before M's first test origin. No candidate can
  see its own future, and all of them see exactly the same information, so a
  difference between two rows is a difference between the estimators.

  PURGED, NON-OVERLAPPING TEST ORIGINS. Spacing of 96+1 bars within each symbol,
  the same schedule `signal_forecast.walk_forward_records` uses. Overlapping
  test windows would count one lucky stretch of tape as thirty independent
  successes.

  DEV / HOLDOUT. Candidates are chosen on 2025-09..2026-04 and the choice is
  reported once on 2026-05..2026-08. This is not ceremony. On the first run the
  pooled candidate showed a dev correlation of 0.094 — six standard errors, and
  it looked like real signal. On holdout it was 0.020, under one standard error.
  Without the split that would have shipped as a discovery.

READING THE OUTPUT
  `margin` is direction accuracy minus the expanding-majority baseline, and it
  is the number that decides whether a candidate is worth anything. Beware of
  reading it alone: it rewards bold calls, so a well-calibrated candidate with a
  BETTER R^2 usually scores WORSE on margin, because shrinking predictions moves
  them into the flat band and flat is the rarest class. Read R^2 and margin
  together, and treat any margin under about two standard errors as noise.

Reads only the study cache written by `scripts/signal_forecast_study.py`.
No broker calls, no orders, no writes outside stdout.
"""
import gzip, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import signal_forecast as fe, signal_bars

CACHE = Path(__file__).resolve().parent.parent / "data" / "signal_study_cache"
H = 96
HOLDOUT_FROM = "2026-05"

print("loading + building features …", flush=True)
SYMS, DATA = [], {}
for path in sorted(CACHE.glob("*-249.json.gz")):
    sym = path.name.split("-")[0]
    with gzip.open(path, "rt") as fh:
        bars = json.load(fh)
    try:
        d = fe._validate_input(sym, "3D", bars, signal_bars.market_as_of(bars),
                               trim_latest_sessions=False)
    except fe.ForecastInputError:
        continue
    F = fe._build_features(d.frame)
    V = F.to_numpy(float)
    lc = np.log(d.frame["close"].to_numpy(float))
    ok = fe._feature_origins(F)
    times = d.frame.index
    DATA[sym] = dict(V=V, lc=lc, ok=ok, times=times)
    SYMS.append(sym)
print(f"  {len(SYMS)} symbols", flush=True)

# ── cross-sectional feature: this symbol's trailing return ranked against the
#    whole pool at the same bar. This is information NO per-symbol model can
#    see, which is the point of testing it.
print("building cross-sectional ranks …", flush=True)
by_time = defaultdict(dict)
for sym in SYMS:
    d = DATA[sym]
    lc = d["lc"]
    for lag in (16, 64):
        r = np.full(len(lc), np.nan)
        r[lag:] = lc[lag:] - lc[:-lag]
        d[f"ret{lag}"] = r
    for i, t in enumerate(d["times"]):
        by_time[t][sym] = i
RANK = {sym: np.full((len(DATA[sym]["lc"]), 2), np.nan) for sym in SYMS}
for t, members in by_time.items():
    if len(members) < 10:
        continue
    for col, lag in enumerate((16, 64)):
        vals = [(sym, DATA[sym][f"ret{lag}"][i]) for sym, i in members.items()]
        vals = [(s, v) for s, v in vals if np.isfinite(v)]
        if len(vals) < 10:
            continue
        order = sorted(vals, key=lambda kv: kv[1])
        n = len(order)
        for pos, (sym, _v) in enumerate(order):
            RANK[sym][members[sym], col] = pos / (n - 1) - 0.5
for sym in SYMS:
    DATA[sym]["V_x"] = np.hstack([DATA[sym]["V"], RANK[sym]])

# ── test schedule ────────────────────────────────────────────────────────────
samples = []            # (sym, origin_index, month, settle_time)
for sym in SYMS:
    d = DATA[sym]
    ok, n = d["ok"], len(d["lc"])
    sched, nxt = [], n
    for t in reversed([int(o) for o in ok if o + H < n]):
        if t + H + 1 < nxt:
            sched.append(t); nxt = t
    for t in reversed(sched):
        samples.append((sym, t, str(d["times"][t])[:7], d["times"][t + H]))
print(f"  {len(samples)} test origins", flush=True)


def ridge(X, y, alpha):
    xm = X.mean(0); xs = X.std(0, ddof=0); xs = np.where(xs > 1e-12, xs, 1.0)
    Z = np.clip((X - xm) / xs, -8, 8)
    yc = np.clip(y, -0.5, 0.5); ym = yc.mean()
    coef = np.linalg.solve(Z.T @ Z + alpha * np.eye(Z.shape[1]), Z.T @ (yc - ym))
    return xm, xs, ym, coef


def apply(model, X):
    xm, xs, ym, coef = model
    return ym + np.clip((X - xm) / xs, -8, 8) @ coef


CANDIDATES = {
    "A per-symbol 10d": dict(pooled=False, window=320, alpha=6.0, cross=False),
    "B per-symbol full": dict(pooled=False, window=None, alpha=6.0, cross=False),
    "C pooled full": dict(pooled=True, window=None, alpha=6.0, cross=False),
    "D pooled + xsec": dict(pooled=True, window=None, alpha=6.0, cross=True),
    "E pooled a=2000": dict(pooled=True, window=None, alpha=2000.0, cross=False),
}
results = {name: [] for name in CANDIDATES}
months = sorted({m for _s, _t, m, _st in samples})

for month in months:
    tests = [s for s in samples if s[2] == month]
    if not tests:
        continue
    cutoff = min(st for _s, _t, _m, st in tests)
    # pooled training rows: every settled sample from every symbol, before cutoff
    pool_rows, pool_rows_x, pool_y = [], [], []
    for sym in SYMS:
        d = DATA[sym]
        ok, lc, times = d["ok"], d["lc"], d["times"]
        tr = ok[(ok + H < len(lc))]
        tr = np.array([t for t in tr if times[t + H] < cutoff], dtype=int)
        if len(tr) == 0:
            continue
        pool_rows.append(d["V"][tr]); pool_rows_x.append(d["V_x"][tr])
        pool_y.append(lc[tr + H] - lc[tr])
    if not pool_rows:
        continue
    PX = np.vstack(pool_rows); PXx = np.vstack(pool_rows_x); PY = np.concatenate(pool_y)
    good = np.isfinite(PX).all(1) & np.isfinite(PY)
    goodx = np.isfinite(PXx).all(1) & np.isfinite(PY)
    models = {}
    for name, cfg in CANDIDATES.items():
        if not cfg["pooled"]:
            continue
        if cfg["cross"]:
            models[name] = ridge(PXx[goodx], PY[goodx], cfg["alpha"])
        else:
            models[name] = ridge(PX[good], PY[good], cfg["alpha"])

    for sym, t, _m, _st in tests:
        d = DATA[sym]
        lc, ok, V, Vx, times = d["lc"], d["ok"], d["V"], d["V_x"], d["times"]
        actual = float(lc[t + H] - lc[t])
        sig = max(0.0005, float(np.std(np.diff(lc[max(0, t - 64):t + 1]), ddof=0)))
        th = min(0.03, max(0.0025, 0.35 * sig * math.sqrt(H)))
        for name, cfg in CANDIDATES.items():
            if cfg["pooled"]:
                x = Vx[t] if cfg["cross"] else V[t]
                if not np.isfinite(x).all():
                    continue
                pred = float(apply(models[name], x))
            else:
                tr = np.array([j for j in ok if j + H <= t
                               and (cfg["window"] is None or j >= t - cfg["window"])], dtype=int)
                if len(tr) < 50:
                    continue
                y = lc[tr + H] - lc[tr]
                pred = float(apply(ridge(V[tr], y, cfg["alpha"]), V[t]))
            results[name].append((np.clip(pred, -0.35, 0.35), actual, th, _m))
    print(f"  {month}: {len(tests)} origins", flush=True)


def report(rows, label):
    P = np.array([r[0] for r in rows]); A = np.array([r[1] for r in rows])
    T = np.array([r[2] for r in rows])
    pc = np.where(P > T, 2, np.where(P < -T, 0, 1))
    ac = np.where(A > T, 2, np.where(A < -T, 0, 1))
    acc = float(np.mean(pc == ac)) * 100
    base = float(np.bincount(ac, minlength=3).max()) / len(ac) * 100
    r2 = 1 - float(np.sum((A - P) ** 2)) / float(np.sum((A - A.mean()) ** 2))
    corr = float(np.corrcoef(P, A)[0, 1]) if P.std() > 1e-12 else 0.0
    se = math.sqrt(acc / 100 * (1 - acc / 100) / len(ac)) * 100
    return "%-19s %6d %9.4f %8.4f %8.2f %8.2f %+8.2f %6.2f" % (
        label, len(ac), r2, corr, acc, base, acc - base, se)


hdr = "%-19s %6s %9s %8s %8s %8s %8s %6s" % (
    "candidate", "n", "OOS R^2", "corr", "dir%", "base%", "margin", "±se")
for split, keep in (("DEV  (2025-09 .. 2026-04)", lambda m: m < HOLDOUT_FROM),
                    ("HOLDOUT (2026-05 .. 2026-08)", lambda m: m >= HOLDOUT_FROM)):
    print(f"\n{split}\n{'-'*80}\n{hdr}\n{'-'*80}")
    for name in CANDIDATES:
        rows = [r for r in results[name] if keep(r[3])]
        if len(rows) >= 30:
            print(report(rows, name))
