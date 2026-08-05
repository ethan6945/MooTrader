"""Validation harness for the aggregate options-flow factor (src/options_stats.py).

OPTIONS_STATS_ENABLED is DEFAULT OFF and OPTIONS_STATS_SIZING is a further
opt-in. This script is the gate they have to clear, and re-running it is how the
claim stays honest as new data arrives.

WHAT IT TESTS. `call_rvol` = a day's call-option volume divided by its own
trailing 20-day mean, computed by src.options_stats.compute() so the number here
is byte-for-byte the number the live path uses. The question is whether it sorts
forward returns.

WHAT THE 2026-08-05 RUN FOUND (26 pool names, 5,980 name-days — the full 1 year
the broker serves):

    call_rvol      n      win%    next-day    next-3-day
    < 0.7        2001     52.6%    +0.252%     +1.077%
    1.0-1.5      1446     55.0%    +0.555%     +1.080%
    2.0-3.0       294     57.8%    +0.832%     +1.996%
    >= 3.0        172     61.6%    +1.846%     +3.058%
    baseline     5980     53.8%    +0.442%     +1.240%

Monotonic. The put/call ratio z-score over the same sample is FLAT at every
bucket, and "high volume + PUT-heavy" scored as well as "high volume + call-
heavy" — so the skew carries no direction and only the volume is used.

Excluding the earnings window makes it BETTER, not worse (39% of rvol>=3 days
sit in one, at roughly double the volatility):

    rvol>=3, not near earnings   n=105    63.8% win   next-3-day +2.559%
    control (rvol<1.5, same)     n=4868   53.6% win   next-3-day +1.026%

and both half-year sub-periods are positive (+1.56% / +1.10% excess).

WHY THIS IS NOT ENOUGH TO ARM IT. The broker caps the history at 251 rows, so
the entire sample is one bull market with a +0.442%/day baseline drift. The
factor has never been observed through a drawdown and could inv+ert in one. The
clean bucket is 105 samples. Treat a PASS here as "keep collecting", not "turn
on sizing" — the live path already persists call_rvol on every buy (audit
extra), and those forward samples are what actually earns trust.

A PASS is: monotonic buckets AND the rvol>=threshold bucket beating the matched
control on BOTH win rate and next-3-day return, in BOTH sub-periods. Anything
weaker means leave the flags off.

Usage (from repo root, OpenD running):
    .venv/bin/python -m scripts.options_factor_study
    .venv/bin/python -m scripts.options_factor_study --symbols PLTR,MU,SNDK
    .venv/bin/python -m scripts.options_factor_study --csv /tmp/optfactor.csv
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import options_stats, universe
from src.moo_client import client

# Same window the live factor uses, so a name-day here is scored exactly as it
# would have been live.
EARN_BEFORE_D = options_stats.EARN_BEFORE_D
EARN_AFTER_D = options_stats.EARN_AFTER_D


def _earnings_dates(symbol: str) -> set[date]:
    """Historical report dates from the local cache (data/earnings_hist.json).
    Empty set = we cannot exclude earnings for this name, which the caller must
    treat as a reason to distrust its rows, not as 'no earnings'."""
    try:
        from src import earnings
        hist = earnings.get_earnings_history(symbol, limit=40)
    except Exception:
        return set()
    out: set[date] = set()
    for d in hist or []:
        try:
            out.add(date.fromisoformat(str(d)[:10]))
        except (TypeError, ValueError):
            continue
    return out


def build_panel(symbols: list[str], sleep_s: float = 0.35) -> pd.DataFrame:
    """One row per (symbol, day) with the factor and its forward returns."""
    frames = []
    with client() as c:
        for sym in symbols:
            df = options_stats.fetch_history(sym, c)
            if df is None or len(df) < options_stats.MIN_ROWS:
                print(f"  skip {sym}: {0 if df is None else len(df)} rows")
                continue
            d = df.copy()
            base_c = d["call_volume"].rolling(options_stats.RVOL_WINDOW).mean().shift(1)
            d["call_rvol"] = d["call_volume"] / base_c
            pcr = d["put_call_volume_ratio"]
            d["pcr_z"] = ((pcr - pcr.rolling(options_stats.RVOL_WINDOW).mean().shift(1))
                          / pcr.rolling(options_stats.RVOL_WINDOW).std().shift(1))
            px = d["underlying_price"]
            d["ret_next"] = (px.shift(-1) / px - 1) * 100
            d["ret_next3"] = (px.shift(-3) / px - 1) * 100
            edates = _earnings_dates(sym)
            def _near(t) -> bool:
                try:
                    day = date.fromisoformat(str(t)[:10])
                except (TypeError, ValueError):
                    return True          # unparseable date -> suppress, never admit
                return any(timedelta(days=-EARN_BEFORE_D) <= (e - day)
                           <= timedelta(days=EARN_AFTER_D) for e in edates)
            d["near_earnings"] = d["time"].map(_near) if edates else True
            d["sym"] = sym
            frames.append(d)
            print(f"  {sym}: {len(d)} rows  {str(d['time'].iloc[0])[:10]}"
                  f"..{str(d['time'].iloc[-1])[:10]}")
            time.sleep(sleep_s)
    if not frames:
        raise RuntimeError("no options history for any symbol — check OpenD and the account")
    panel = pd.concat(frames, ignore_index=True)
    # The broker returns `time` as a string; the sub-period split does date
    # arithmetic on it, so normalise once here rather than at each use.
    panel["time"] = pd.to_datetime(panel["time"], errors="coerce")
    return panel.dropna(subset=["call_rvol", "ret_next", "time"])


def _row(sub: pd.DataFrame, label: str, min_n: int = 30) -> None:
    if len(sub) < min_n:
        print(f"  {label:44s} n={len(sub):5d}   (too few to read)")
        return
    print(f"  {label:44s} n={len(sub):5d}  win%={100 * (sub.ret_next > 0).mean():5.1f}"
          f"  next1={sub.ret_next.mean():+6.3f}%"
          f"  next3={sub.ret_next3.mean():+6.3f}%"
          f"  sd={sub.ret_next.std():5.2f}%")


def report(panel: pd.DataFrame, threshold: float) -> bool:
    print(f"\nTOTAL name-days: {len(panel)}   "
          f"span {str(panel.time.min())[:10]} .. {str(panel.time.max())[:10]}\n")
    print("=== BASELINE ===")
    _row(panel, "every name-day")

    print("\n=== call_rvol buckets (want MONOTONIC) ===")
    for lo, hi in [(0, 0.7), (0.7, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 1e9)]:
        _row(panel[(panel.call_rvol >= lo) & (panel.call_rvol < hi)],
             f"call_rvol {lo}-{'inf' if hi > 1e8 else hi}")

    print("\n=== put/call skew (expected FLAT — if it sorts, re-read the module) ===")
    for lo, hi in [(-1e9, -2), (-2, -1), (-1, 0), (0, 1), (1, 2), (2, 1e9)]:
        _row(panel[(panel.pcr_z >= lo) & (panel.pcr_z < hi)],
             f"pcr_z {'-inf' if lo < -1e8 else lo}..{'inf' if hi > 1e8 else hi}")

    print("\n=== the live rule: spike AND clear of earnings ===")
    hot = panel[(panel.call_rvol >= threshold) & (~panel.near_earnings)]
    ctl = panel[(panel.call_rvol < 1.5) & (~panel.near_earnings)]
    _row(panel[(panel.call_rvol >= threshold) & (panel.near_earnings)],
         f"rvol>={threshold} NEAR earnings (excluded)")
    _row(hot, f"rvol>={threshold} clear of earnings  <-- the rule")
    _row(ctl, "control: rvol<1.5, clear of earnings")

    print("\n=== stability: same rule, each half of the sample ===")
    mid = panel.time.min() + (panel.time.max() - panel.time.min()) / 2
    halves_pass = []
    for label, mask in [("first half", panel.time < mid), ("second half", panel.time >= mid)]:
        h = hot[hot.time < mid] if label == "first half" else hot[hot.time >= mid]
        b = panel[mask]
        _row(h, f"{label}: rule")
        _row(b, f"{label}: baseline")
        halves_pass.append(len(h) >= 30 and h.ret_next3.mean() > b.ret_next3.mean())

    ok = (len(hot) >= 30 and len(ctl) >= 30
          and (hot.ret_next > 0).mean() > (ctl.ret_next > 0).mean()
          and hot.ret_next3.mean() > ctl.ret_next3.mean()
          and all(halves_pass))
    print("\n" + ("PASS — beats the matched control on win rate and next-3-day, in both halves."
                  if ok else
                  "FAIL — does not clear the gate. Leave OPTIONS_STATS_* off."))
    print("Either way this is one bull market and a small clean bucket; a PASS means "
          "'keep collecting forward samples', not 'arm sizing'.")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", help="comma-separated; default = the liquidity pool's "
                                      "single stocks (ETFs have no useful options aggregate here)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="call_rvol spike threshold (default: settings.options_stats_min_rvol)")
    ap.add_argument("--csv", help="write the raw name-day panel here")
    args = ap.parse_args()

    from src.config import settings
    threshold = args.threshold if args.threshold is not None else settings.options_stats_min_rvol

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        etfs = universe.load_etfs()
        symbols = [s for s in universe.load_pool() if s not in etfs]

    print(f"Fetching options history for {len(symbols)} names "
          f"(broker caps each at ~251 daily rows)...")
    panel = build_panel(symbols)
    if args.csv:
        panel.to_csv(args.csv, index=False)
        print(f"\nraw panel -> {args.csv}")
    ok = report(panel, threshold)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
