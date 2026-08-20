"""Engine parity, at four windows, under a definition that means something.

    .venv/bin/python scripts/parity_runs.py --windows 30 90 180 360
    .venv/bin/python scripts/parity_runs.py --walk-forward 90 --step 30

WHY THE OLD NUMBER DID NOT MEAN AGREEMENT

  sandbox_vs_backtest counts a trade "matched" when the two engines bought the
  SAME SYMBOL within ONE BUSINESS DAY of each other. Nothing about quantity,
  entry price, why it was closed, or what it made. Two engines can post that
  match on trades that entered a day apart at different prices and left for
  different reasons with opposite results.

  So a headline match rate under that rule is not a claim about agreement, and
  it should not be used as one in either direction — neither 31.6% nor 68.2%.

WHAT IS MEASURED HERE

  Four nested tiers. Each adds one condition, and every tier is a subset of the
  one above, so the drop between them says which KIND of disagreement is left.

    T0  same symbol, entry within 1 business day        (the old definition)
    T1  + entered on the SAME bar, entry price within ENTRY_BPS
    T2  + closed for the same reason
    T3  + exit price within EXIT_BPS, and the same sign of PnL

  T3 is the one to quote. It is the tier that means "these two engines took the
  same trade and got the same answer".

  Trades on only one side are reported by which engine had them, because the
  two failure modes are different: a trade only the sandbox took is a gate v3
  does not model, and a trade only v3 took is a gate the sandbox applies and v3
  does not.

WHAT IS HELD CONSTANT

  Both engines run the same window, the same universe and the same
  runtime-effective config, at one git HEAD, and the HEAD is recorded in the
  output. This exists because the forming-bar fix changed which bar live scores
  on, so every parity number measured before it describes a program that no
  longer exists.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")

ENTRY_BPS = 25.0        # same bar, and within a quarter of a percent
EXIT_BPS = 50.0         # exits diverge more: ladders, partial closes


def head() -> str:
    """The commit these numbers describe — and whether that is the whole truth.

    A run started from a working tree with uncommitted changes is not described
    by its HEAD. The first sweep recorded 20aa2fe while running a cache fix that
    was still unstaged, so the output named a commit that did not contain the
    code that produced it. A parity number whose provenance is wrong is worse
    than no parity number.
    """
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=ROOT, capture_output=True, text=True
                             ).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:
        return "unknown"


def _d(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)[:19]).date()
    except Exception:
        return None


def _dt(v):
    try:
        return datetime.fromisoformat(str(v)[:19])
    except Exception:
        return None


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalise(t: dict, engine: str) -> dict:
    """One shape for a trade, whichever engine produced it."""
    entry_t = t.get("entry_t") or t.get("entry_time") or t.get("entry_date")
    exit_t = t.get("exit_t") or t.get("exit_time") or t.get("exit_date")
    return {
        "engine": engine,
        "symbol": t.get("symbol"),
        "entry_dt": _dt(entry_t),
        "entry_date": _d(entry_t),
        "entry_px": _f(t.get("entry") or t.get("entry_price")),
        "exit_px": _f(t.get("exit") or t.get("exit_price")),
        "exit_date": _d(exit_t),
        "reason": str(t.get("exit_reason") or t.get("reason") or "?").upper(),
        "pnl": _f(t.get("net_pnl") if t.get("net_pnl") is not None else t.get("pnl")),
        "qty": _f(t.get("qty")),
    }


def bps(a: float, b: float) -> float:
    if not b:
        return float("inf")
    return abs(a - b) / abs(b) * 1e4


def busdays(a: date, b: date) -> int:
    lo, hi = min(a, b), max(a, b)
    return int(np.busday_count(lo, hi))


def tier_of(s: dict, v: dict) -> int:
    """The highest tier this pair satisfies. 0 = matched only by the old rule."""
    if s["entry_dt"] and v["entry_dt"] and s["entry_dt"] != v["entry_dt"]:
        # Same bar is the T1 condition; hourly engines stamp the bar they used.
        if s["entry_date"] != v["entry_date"]:
            return 0
    if bps(s["entry_px"], v["entry_px"]) > ENTRY_BPS:
        return 0
    if s["reason"] != v["reason"]:
        return 1
    if bps(s["exit_px"], v["exit_px"]) > EXIT_BPS:
        return 2
    if (s["pnl"] >= 0) != (v["pnl"] >= 0):
        return 2
    return 3


def align(sb: list[dict], v3: list[dict], max_busdays: int = 1):
    """Greedy nearest-entry matching per symbol — the T0 rule, kept as-is so
    the tiers are measured on the same population the old number was."""
    free = list(range(len(v3)))
    pairs, sb_only = [], []
    for s in sorted(sb, key=lambda t: (t["entry_date"] or date.min)):
        best, gap_best = None, None
        for j in free:
            if v3[j]["symbol"] != s["symbol"]:
                continue
            if not (s["entry_date"] and v3[j]["entry_date"]):
                continue
            gap = busdays(s["entry_date"], v3[j]["entry_date"])
            if gap <= max_busdays and (gap_best is None or gap < gap_best):
                best, gap_best = j, gap
        if best is None:
            sb_only.append(s)
        else:
            free.remove(best)
            pairs.append((s, v3[best]))
    return pairs, sb_only, [v3[j] for j in free]


def _truncate(data: dict, end: datetime) -> dict:
    """Drop every bar after `end` from a prefetched bundle. Never mutates it."""
    import pandas as pd

    cut = pd.Timestamp(end.date())

    def clip(df):
        if df is None or not hasattr(df, "index") or df.empty:
            return df
        idx = df.index
        try:
            if getattr(idx, "tz", None) is not None:
                return df[idx <= cut.tz_localize(idx.tz)]
            return df[idx <= cut]
        except Exception:
            return df

    out = dict(data)
    out["spy_daily"] = clip(data.get("spy_daily"))
    out["soxx_daily"] = clip(data.get("soxx_daily"))
    per = {}
    for sym, bundle in (data.get("per_ticker") or {}).items():
        b = dict(bundle)
        for k, v in bundle.items():
            if hasattr(v, "index"):
                b[k] = clip(v)
        per[sym] = b
    out["per_ticker"] = per
    return out


def run_window(days: int, end: datetime | None = None) -> dict:
    from src import runtime_config as rc
    from src.backtest import BacktestConfig, prefetch_data, _run_live_engine
    from src.config import settings
    from src.risk_manager import budget_usd
    from src.sandbox import SandboxConfig, run_sandbox
    end = end or datetime.now(ET).replace(hour=16, minute=0, second=0,
                                          microsecond=0)
    start = end - timedelta(days=days)
    # The SAME list to both engines, and the watchlist rather than the pool:
    # it is what the live bot actually scans, so a parity number measured on it
    # is a statement about the program that runs.
    tickers = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]

    print(f"    sandbox {days}d …", flush=True)
    sb_raw = run_sandbox(SandboxConfig(start=start, end=end, tickers=tickers,
                                       universe_mode="static", enable_ai=False))
    print(f"    v3 {days}d …", flush=True)
    cfg = BacktestConfig(
        days=days, timeframe="HOUR_1", threshold=rc.entry_threshold(),
        tickers=tickers, account_usd=budget_usd(),
        risk_per_trade=rc.risk_per_trade(),
        max_position_pct=rc.max_position_pct(),
        max_hold_days=rc.max_hold_days(),
        tp_atr_mult=rc.tp_atr_mult(), sl_atr_mult=rc.sl_atr_mult(),
        max_gap_pct=settings.max_gap_pct)
    data = prefetch_data(cfg)
    # Point v3 at a historical window by TRUNCATING what it was given.
    #
    # BacktestConfig carries `days` and no end date, so v3 can only ever run
    # "the last N days" — which made every walk-forward fold compare a
    # historical sandbox window against v3's most recent one. Folds ending
    # 2026-04-07 and earlier reported v3=0 trades and a 0% match, which is not
    # a measurement of anything.
    #
    # Cutting each frame at the fold's end date makes the engine replay as if
    # that date were today: it slices the last days*bars-per-day rows of what it
    # holds, and what it holds now stops there.
    if end.date() < datetime.now(ET).date():
        data = _truncate(data, end)
    v3_raw = _run_live_engine(cfg, data)

    sb_all = [normalise(t, "sandbox") for t in (sb_raw.get("trades") or [])]
    v3_all = [normalise(t, "v3") for t in (v3_raw.get("trades") or [])]

    # Clip both sides to the SAME calendar window before comparing.
    #
    # `days` does not mean the same thing to the two engines. The sandbox walks
    # a calendar range; v3 fetches days*bars-per-day plus warm-up and runs over
    # everything it fetched, so days=30 gave it roughly thirty TRADING days —
    # 2026-07-07 to 08-18 against the sandbox's 07-22 to 08-14. Aligning trades
    # between two different windows produced zero pairs and would have read as
    # total disagreement between the engines, which is not what it was.
    lo, hi = start.date(), end.date()

    def inside(t):
        return t["entry_date"] is not None and lo <= t["entry_date"] <= hi

    sb = [t for t in sb_all if inside(t)]
    v3 = [t for t in v3_all if inside(t)]
    pairs, sb_only, v3_only = align(sb, v3)

    tiers = [0, 0, 0, 0]
    for s, v in pairs:
        tiers[tier_of(s, v)] += 1
    # Nested: a T3 pair also satisfies T2, T1, T0.
    cum = [sum(tiers[i:]) for i in range(4)]
    denom = max(len(sb), len(v3)) or 1

    def reason_cov(rows):
        n = sum(1 for r in rows if r["reason"] not in ("?", "NONE", ""))
        return f"{n}/{len(rows)}"

    return {
        "days": days,
        "window": [start.date().isoformat(), end.date().isoformat()],
        "n_sandbox": len(sb), "n_v3": len(v3),
        "clipped_out": {"sandbox": len(sb_all) - len(sb),
                        "v3": len(v3_all) - len(v3)},
        # If a side does not record why it closed, T2 cannot be satisfied and
        # the drop from T1 to T2 says nothing about the engines.
        "exit_reason_coverage": {"sandbox": reason_cov(sb), "v3": reason_cov(v3)},
        "n_paired": len(pairs),
        "sandbox_only": len(sb_only), "v3_only": len(v3_only),
        "tier_counts": {"T0_only": tiers[0], "T1_only": tiers[1],
                        "T2_only": tiers[2], "T3": tiers[3]},
        "tier_rate_pct": {f"T{i}": round(cum[i] / denom * 100, 1)
                          for i in range(4)},
        "reason_mismatches": sorted({
            f"{s['reason']}/{v['reason']}" for s, v in pairs
            if s["reason"] != v["reason"]}),
        "sb_net_pnl": round(sum(t["pnl"] for t in sb), 2),
        "v3_net_pnl": round(sum(t["pnl"] for t in v3), 2),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--windows", nargs="*", type=int, default=[30, 90, 180, 360])
    ap.add_argument("--walk-forward", type=int, default=0,
                    help="window length in days for a rolling comparison")
    ap.add_argument("--step", type=int, default=30)
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--output", default=str(ROOT / "data" / "parity_runs.json"))
    args = ap.parse_args()

    out = {"head": head(), "run_at": datetime.now(ET).isoformat(),
           "entry_bps": ENTRY_BPS, "exit_bps": EXIT_BPS,
           "windows": [], "walk_forward": []}

    for d in args.windows:
        print(f"\n=== window {d}d ===", flush=True)
        try:
            r = run_window(d)
            out["windows"].append(r)
            print(f"    sandbox {r['n_sandbox']} / v3 {r['n_v3']} / paired "
                  f"{r['n_paired']} → T3 {r['tier_rate_pct']['T3']}%", flush=True)
        except Exception as e:
            print(f"    FAILED: {e}", flush=True)
            out["windows"].append({"days": d, "error": str(e)[:300]})

    if args.walk_forward:
        base = datetime.now(ET).replace(hour=16, minute=0, second=0, microsecond=0)
        for k in range(args.folds):
            end = base - timedelta(days=args.step * k)
            print(f"\n=== walk-forward fold {k + 1}/{args.folds} "
                  f"(ending {end.date()}) ===", flush=True)
            try:
                r = run_window(args.walk_forward, end=end)
                r["fold"] = k + 1
                out["walk_forward"].append(r)
                print(f"    T3 {r['tier_rate_pct']['T3']}%", flush=True)
            except Exception as e:
                print(f"    FAILED: {e}", flush=True)
                out["walk_forward"].append({"fold": k + 1, "error": str(e)[:300]})

    Path(args.output).write_text(json.dumps(out, indent=2, default=str))
    print(f"\nwritten to {args.output}")
    print(json.dumps(out, indent=2, default=str)[:3000])


if __name__ == "__main__":
    main()
