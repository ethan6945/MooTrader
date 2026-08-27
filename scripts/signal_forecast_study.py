#!/usr/bin/env python3
"""Measure whether the signal desk's forecast model has any skill, and when.

    .venv/bin/python scripts/signal_forecast_study.py                 # whole pool
    .venv/bin/python scripts/signal_forecast_study.py --symbols NVDA,MU
    .venv/bin/python scripts/signal_forecast_study.py --refetch       # ignore cache

WHY THIS EXISTS
  The forecast merged in from the Stock Probability Prediction Platform reports
  a walk-forward accuracy alongside every prediction, but that report is capped
  at 32 samples from one symbol over roughly two months. That is enough to say
  "not demonstrated here" and not enough to say "this model is useless" — or to
  say the opposite, that it works in some regimes and not others.

  This script answers the larger question: across the whole trading pool, over
  as much history as the broker will serve, does the model beat the baseline it
  has to beat, and does that answer change by period?

WHAT IT MEASURES
  DIRECTION vs BASELINE. The baseline is the expanding majority class — always
  predict whichever of down/flat/up has been most common so far. A model that
  cannot beat that is not adding information, however plausible its output.

  CALIBRATION. Whether a stated probability means anything: reliability by
  confidence bucket, plus Brier score. A model can pick directions well and
  still be useless if it says 99% when it means 55%.

  INTERVAL COVERAGE. q10/q90 is an 80% band by construction, so the fraction of
  outcomes landing inside it should be near 80%. Far below means the intervals
  are too narrow — the same disease as overconfident probabilities.

  EXTRAPOLATION RATE. How often the prediction falls outside the range of
  targets the model was fitted on. Those predictions are the model guessing past
  its own evidence, and they are where its confidence is least earned.

It reads the broker and writes nothing but its own cache and report. No orders.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import signal_bars, signal_forecast as fe    # noqa: E402
from src.signal_calendar import NEW_YORK              # noqa: E402

DEFAULT_SESSIONS = 249          # the broker's ceiling for 30-minute ETH history
MAX_SAMPLES = 999               # study cap; the shipped forecast stops at 32
CLASS_NAMES = ("down", "flat", "up")


# ── data ──────────────────────────────────────────────────────────────────────
def load_pool(spec: str) -> list[str]:
    if spec == "pool":
        return json.loads((ROOT / "config" / "universe_pool.json").read_text())["tickers"]
    if spec == "watchlist":
        return json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]
    return [item.strip().upper() for item in spec.split(",") if item.strip()]


def bars_for(client, symbol: str, sessions: int, cache: Path, refetch: bool) -> list[dict]:
    """Fetch (and cache) one symbol's history. The cache makes re-runs free.

    Re-running the aggregation is the common case while iterating on the
    analysis, and re-fetching 82 symbols each time spends the broker's shared
    rate-limit budget on data that has not changed.
    """
    path = cache / f"{symbol}-{sessions}.json.gz"
    if path.exists() and not refetch:
        with gzip.open(path, "rt") as handle:
            return json.load(handle)
    bars = signal_bars.fetch_30m(client, symbol, sessions=sessions)
    cache.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as handle:
        json.dump(bars, handle)
    return bars


def records_for(symbol: str, bars: list[dict]) -> list[dict]:
    """Every non-overlapping purged walk-forward sample this history supports."""
    as_of = signal_bars.market_as_of(bars)
    data = fe._validate_input(symbol, "3D", bars, as_of, trim_latest_sessions=False)
    features = fe._build_features(data.frame)
    out = []
    for record in fe.walk_forward_records(data, features, max_samples=MAX_SAMPLES):
        prediction = float(record["prediction"])
        low, high = record["train_target_min"], record["train_target_max"]
        width = high - low
        excess = max(0.0, low - prediction, prediction - high)
        out.append({
            "symbol": symbol,
            "origin_at": record["origin_at"],
            "month": record["origin_at"][:7],
            "prediction": prediction,
            "actual": float(record["actual"]),
            "error": float(record["actual"]) - prediction,
            "predicted_class": int(record["predicted_class"]),
            "actual_class": int(record["actual_class"]),
            "baseline_class": int(record["baseline_class"]),
            "max_probability": float(max(record["probabilities"])),
            "brier": float(record["brier"]),
            "interval_hit": bool(record["lower"] <= record["actual"] <= record["upper"]),
            "extrapolated": excess > 0.0,
            "excess_widths": (excess / width) if width > 1e-12 else 0.0,
            "residual_sigma_in_sample": float(record["residual_sigma_in_sample"]),
        })
    return out


# ── aggregation ───────────────────────────────────────────────────────────────
def summarize(rows: list[dict], label: str) -> dict:
    if not rows:
        return {"label": label, "samples": 0}
    hits = [r["predicted_class"] == r["actual_class"] for r in rows]
    base = [r["baseline_class"] == r["actual_class"] for r in rows]
    accuracy = statistics.fmean(hits) * 100.0
    baseline = statistics.fmean(base) * 100.0
    n = len(rows)
    # Binomial standard error on the paired difference, so a margin can be read
    # against the noise it has to clear rather than as a point estimate.
    disagree = sum(1 for h, b in zip(hits, base) if h != b)
    se = (math.sqrt(disagree) / n * 100.0) if disagree else 0.0
    return {
        "label": label,
        "samples": n,
        "symbols": len({r["symbol"] for r in rows}),
        "direction_percent": round(accuracy, 2),
        "baseline_percent": round(baseline, 2),
        "margin_pp": round(accuracy - baseline, 2),
        "margin_se_pp": round(se, 2),
        "brier": round(statistics.fmean(r["brier"] for r in rows), 4),
        "interval_coverage_percent": round(statistics.fmean(r["interval_hit"] for r in rows) * 100.0, 2),
        "mae_percent": round(statistics.fmean(abs(r["error"]) for r in rows) * 100.0, 3),
        "extrapolated_percent": round(statistics.fmean(r["extrapolated"] for r in rows) * 100.0, 2),
        "mean_max_probability": round(statistics.fmean(r["max_probability"] for r in rows) * 100.0, 2),
    }


def reliability(rows: list[dict]) -> list[dict]:
    """Stated confidence vs realized accuracy, bucketed.

    This is the table that decides whether a probability is a probability. If
    the 90-100% bucket lands at 40% accuracy, the number is not a forecast, it
    is a formatting artifact.
    """
    buckets = [(0.33, 0.50), (0.50, 0.70), (0.70, 0.90), (0.90, 1.01)]
    out = []
    for low, high in buckets:
        chosen = [r for r in rows if low <= r["max_probability"] < high]
        if not chosen:
            out.append({"bucket": f"{low*100:.0f}-{high*100:.0f}%", "samples": 0})
            continue
        out.append({
            "bucket": f"{low*100:.0f}-{high*100:.0f}%",
            "samples": len(chosen),
            "stated_percent": round(statistics.fmean(r["max_probability"] for r in chosen) * 100.0, 2),
            "realized_percent": round(statistics.fmean(
                r["predicted_class"] == r["actual_class"] for r in chosen) * 100.0, 2),
        })
    return out


def table(rows: list[dict], columns: list[tuple[str, str, int]]) -> str:
    head = "  ".join(title.rjust(width) if align == ">" else title.ljust(width)
                     for title, align, width in columns)
    lines = [head, "  ".join("-" * width for _t, _a, width in columns)]
    for row in rows:
        cells = []
        for title, align, width in columns:
            key = title.strip().lower().replace(" ", "_").replace("%", "percent")
            value = row.get(key, row.get(title, ""))
            text = "" if value is None else str(value)
            cells.append(text.rjust(width) if align == ">" else text.ljust(width))
        lines.append("  ".join(cells))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", default="pool",
                        help="pool | watchlist | comma-separated list")
    parser.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS)
    parser.add_argument("--refetch", action="store_true")
    parser.add_argument("--cache", default=None)
    parser.add_argument("--out", default=None, help="write the full report as JSON")
    args = parser.parse_args()

    cache = Path(args.cache) if args.cache else ROOT / "data" / "signal_study_cache"
    symbols = load_pool(args.symbols)
    print(f"symbols: {len(symbols)}  sessions: {args.sessions}  cache: {cache}")

    from src.moo_client import client as _mc
    rows: list[dict] = []
    failures: list[tuple[str, str]] = []
    started = time.time()
    with _mc() as broker:
        for index, symbol in enumerate(symbols, 1):
            try:
                bars = bars_for(broker, symbol, args.sessions, cache, args.refetch)
                got = records_for(symbol, bars)
                rows.extend(got)
                print(f"  [{index:>3}/{len(symbols)}] {symbol:<6} {len(bars):>5} bars  "
                      f"{len(got):>3} samples", flush=True)
            except fe.ForecastInputError as exc:
                failures.append((symbol, f"{exc.code}: {exc.message[:60]}"))
                print(f"  [{index:>3}/{len(symbols)}] {symbol:<6} REFUSED {exc.code}", flush=True)
            except Exception as exc:                       # noqa: BLE001 - study boundary
                failures.append((symbol, f"{type(exc).__name__}: {exc}"))
                print(f"  [{index:>3}/{len(symbols)}] {symbol:<6} FAILED {type(exc).__name__}", flush=True)

    if not rows:
        print("\nno samples — nothing to measure")
        return 1

    overall = summarize(rows, "ALL")
    by_month = [summarize([r for r in rows if r["month"] == month], month)
                for month in sorted({r["month"] for r in rows})]
    by_symbol = sorted(
        (summarize([r for r in rows if r["symbol"] == symbol], symbol)
         for symbol in sorted({r["symbol"] for r in rows})),
        key=lambda item: -item["margin_pp"])
    interpolated = summarize([r for r in rows if not r["extrapolated"]], "in-range")
    extrapolated = summarize([r for r in rows if r["extrapolated"]], "extrapolated")

    elapsed = time.time() - started
    print(f"\n{'='*94}\nOVERALL  ({len(rows)} samples, {overall['symbols']} symbols, {elapsed:.0f}s)\n{'='*94}")
    cols = [("label", "<", 10), ("samples", ">", 7), ("direction_percent", ">", 9),
            ("baseline_percent", ">", 9), ("margin_pp", ">", 9), ("margin_se_pp", ">", 7),
            ("brier", ">", 7), ("interval_coverage_percent", ">", 9),
            ("mae_percent", ">", 8), ("extrapolated_percent", ">", 7)]
    header = ("label      samples  direction   baseline    margin   ±se    brier  coverage"
              "      mae  extrap%")
    def line(item: dict) -> str:
        return ("%-10s %8d %10s %10s %9s %6s %8s %9s %8s %8s" % (
            item["label"], item["samples"], item.get("direction_percent"),
            item.get("baseline_percent"), item.get("margin_pp"), item.get("margin_se_pp"),
            item.get("brier"), item.get("interval_coverage_percent"),
            item.get("mae_percent"), item.get("extrapolated_percent")))
    print(header)
    print("-" * 94)
    print(line(overall))

    print(f"\nBY MONTH (does the answer change by regime?)\n{'-'*94}")
    print(header); print("-" * 94)
    for item in by_month:
        print(line(item))

    print(f"\nIN-RANGE vs EXTRAPOLATED\n{'-'*94}")
    print(header); print("-" * 94)
    for item in (interpolated, extrapolated):
        print(line(item))

    print(f"\nRELIABILITY — stated confidence vs realized accuracy\n{'-'*94}")
    print("bucket        samples   stated%   realized%   gap")
    print("-" * 94)
    for item in reliability(rows):
        if not item["samples"]:
            print("%-12s %8d         —           —      —" % (item["bucket"], 0)); continue
        gap = item["stated_percent"] - item["realized_percent"]
        print("%-12s %8d %9s %11s %+7.2f" % (item["bucket"], item["samples"],
                                             item["stated_percent"], item["realized_percent"], gap))

    ranked = [item for item in by_symbol if item["samples"] >= 10]
    # With few symbols the two ends are the same rows; show the list once rather
    # than printing every symbol twice under a "best/worst" heading.
    if len(ranked) <= 10:
        shown, title = ranked, "EVERY SYMBOL BY MARGIN (>=10 samples)"
    else:
        shown = ranked[:5] + [{"label": "…", "samples": 0}] + ranked[-5:]
        title = "BEST 5 / WORST 5 SYMBOLS BY MARGIN (>=10 samples)"
    print(f"\n{title}\n{'-'*94}")
    print(header); print("-" * 94)
    for item in shown:
        if item["samples"] == 0 and item["label"] == "…":
            print("…"); continue
        print(line(item))

    winners = sum(1 for item in by_symbol if item["samples"] >= 10 and item["margin_pp"] > 0)
    counted = sum(1 for item in by_symbol if item["samples"] >= 10)
    print(f"\nsymbols beating baseline: {winners}/{counted}"
          f"   (coin-flip expectation ≈ {counted/2:.0f})")
    if failures:
        print(f"\n{len(failures)} symbol(s) produced no samples:")
        for symbol, why in failures[:10]:
            print(f"  {symbol:<6} {why}")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "overall": overall, "by_month": by_month, "by_symbol": by_symbol,
            "in_range": interpolated, "extrapolated": extrapolated,
            "reliability": reliability(rows), "failures": failures,
            "sessions": args.sessions, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, indent=2, ensure_ascii=False))
        print(f"\nreport → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
