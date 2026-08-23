"""v4_vs_live — score the engine against REAL FILLS, not against another engine.

WHY THIS REPLACES sandbox_vs_backtest.py

  The old check asked "do the two engines agree". Two engines can agree and
  both be wrong, and on 2026-08-21 they disagreed by 94% of net PnL while the
  optimistic one was the one wired to change live parameters. Worse, 62% of
  that gap turned out to be the comparison harness itself: it started the
  sandbox at 16:00 ET on day one — after the close — so the first session was
  structurally missing on one side only.

  There is exactly one comparison that can be wrong in a way that costs money:
  the engine against what the account actually did. db.closed_trades is the
  record of what the broker actually filled. That is the yardstick.

WHAT IT MEASURES
  1. AGGREGATE — trade count, win rate, net PnL, and the R distribution, over
     the window live actually traded (not a window the engine picked).
  2. STOP REALISM — live's stops average worse than −1R because they are soft:
     checked at scan boundaries and filled at market. An engine whose worst
     stop is −1.0R is not modelling live's exits, it is modelling a limit
     order that does not exist.
  3. NAME OVERLAP — which symbols the engine traded that live did not, and
     which live traded that the engine missed. A false negative on a name live
     really bought is the expensive kind: it means the optimizer is tuning
     against a strategy that would have skipped a real trade.

WHAT A PASS LOOKS LIKE
  Tolerances are deliberately loose on PnL and tight on the SHAPE of the
  result. Matching a 13-trade sample's dollar total to the cent would be
  overfitting; reproducing its win rate, its stop distribution, and which
  names it traded is evidence the engine models the same strategy.

Run:
  .venv/bin/python3 scripts/v4_vs_live.py
  .venv/bin/python3 scripts/v4_vs_live.py --from 2026-07-22 --to 2026-08-11
  .venv/bin/python3 scripts/v4_vs_live.py --compare-legacy   # also score v3 + sandbox
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")

DEFAULT_OUTPUT = ROOT / "data" / "v4_vs_live.json"

# Tolerances. See "WHAT A PASS LOOKS LIKE".
MAX_NET_GAP_PCT = 35.0        # engine net PnL vs live net PnL
MAX_WIN_RATE_GAP_PP = 20.0    # percentage points
MAX_SL_R_GAP = 0.40           # |engine median stop R − live median stop R|
MIN_NAME_RECALL_PCT = 50.0    # share of live-traded names the engine also took


# ── live truth ─────────────────────────────────────────────

def live_trades(start: str, end: str) -> list[dict]:
    """Real closed trades in [start, end], from the trading database."""
    from src import db
    rows = db.closed_trades(limit=10_000)
    out = []
    for r in rows:
        ts = str(r.get("ts", ""))[:10]
        if not ts or ts < start or ts > end:
            continue
        out.append({
            "symbol": r["symbol"],
            "opened_at": str(r.get("opened_at") or "")[:10],
            "exit_date": ts,
            "entry": float(r.get("entry") or 0),
            "exit": float(r.get("exit") or 0),
            "reason": r.get("exit_reason") or "",
            "pnl": float(r.get("pnl") or 0),
            "r": float(r.get("r_multiple") or 0),
        })
    return sorted(out, key=lambda t: (t["opened_at"], t["symbol"]))


# ── engines ────────────────────────────────────────────────

def run_v4(start: datetime, end: datetime, tickers: list[str]) -> dict:
    from src.backtest_v4 import V4Config, run_v4 as _run
    cfg = V4Config(start=start, end=end, tickers=tickers, universe_mode="static")
    res = _run(cfg)
    return {
        "name": "v4",
        "trades": [{"symbol": t["symbol"], "opened_at": t["entry_t"][:10],
                    "exit_date": t["exit_t"][:10], "reason": t["reason"],
                    "pnl": t["net_pnl"], "r": t["r"]} for t in res["trades"]],
        "extra": {k: res["metrics"][k] for k in
                  ("fill_rate_pct", "ttl_expired", "max_dd_mtm_pct",
                   "orders_attempted", "orders_filled")},
        "elapsed_sec": res["elapsed_sec"],
    }


def run_v3(start: datetime, end: datetime, tickers: list[str]) -> dict:
    """The outgoing honest engine, for the record."""
    from src import runtime_config as rc
    from src.backtest import BacktestConfig, prefetch_data, _run_live_engine
    from src.config import settings
    from src.risk_manager import budget_usd
    days = (end - start).days
    cfg = BacktestConfig(
        days=days, timeframe="HOUR_1", threshold=rc.entry_threshold(),
        tickers=tickers, account_usd=budget_usd(),
        risk_per_trade=rc.risk_per_trade(), max_position_pct=rc.max_position_pct(),
        max_hold_days=rc.max_hold_days(), tp_atr_mult=rc.tp_atr_mult(),
        sl_atr_mult=rc.sl_atr_mult(), max_gap_pct=settings.max_gap_pct,
        apply_mr_strategy=settings.mr_enabled)
    res = _run_live_engine(cfg, prefetch_data(cfg))
    lo, hi = start.date().isoformat(), end.date().isoformat()
    tr = []
    for t in res["trades"]:
        if not (lo <= t["entry_date"] <= hi):
            continue
        risk = t["entry_price"] - t["stop_loss"]
        tr.append({"symbol": t["symbol"], "opened_at": t["entry_date"],
                   "exit_date": t["exit_date"], "reason": t["exit_reason"],
                   "pnl": t["pnl"],
                   "r": (t["exit_price"] - t["entry_price"]) / risk if risk > 0 else 0.0})
    return {"name": "v3", "trades": tr, "extra": {}, "elapsed_sec": None}


def run_sandbox(start: datetime, end: datetime, tickers: list[str]) -> dict:
    """The outgoing replay engine, for the record."""
    from src.sandbox import SandboxConfig, run_sandbox as _run
    res = _run(SandboxConfig(start=start, end=end, tickers=tickers,
                             universe_mode="static", enable_ai=False))
    return {
        "name": "sandbox",
        "trades": [{"symbol": t["symbol"], "opened_at": t["entry_t"][:10],
                    "exit_date": t["exit_t"][:10], "reason": t["reason"],
                    "pnl": t["net_pnl"], "r": t["r"]} for t in res["trades"]],
        "extra": {}, "elapsed_sec": res.get("elapsed_sec"),
    }


# ── scoring ────────────────────────────────────────────────

def _shape(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "win_rate_pct": 0.0, "net_pnl": 0.0,
                "median_r": None, "worst_r": None, "median_sl_r": None,
                "symbols": []}
    wins = [t for t in trades if t["pnl"] > 0]
    rs = sorted(t["r"] for t in trades)
    sl_rs = sorted(t["r"] for t in trades
                   if str(t["reason"]).upper().startswith(("SL", "BREAKEVEN")))
    return {
        "n": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1),
        "net_pnl": round(sum(t["pnl"] for t in trades), 2),
        "median_r": round(statistics.median(rs), 2),
        "worst_r": round(rs[0], 2),
        "median_sl_r": round(statistics.median(sl_rs), 2) if sl_rs else None,
        "symbols": sorted({t["symbol"] for t in trades}),
    }


def score(engine: dict, truth: list[dict]) -> dict:
    e_shape, t_shape = _shape(engine["trades"]), _shape(truth)
    e_syms, t_syms = set(e_shape["symbols"]), set(t_shape["symbols"])

    denom = max(abs(t_shape["net_pnl"]), abs(e_shape["net_pnl"]), 1.0)
    net_gap = abs(e_shape["net_pnl"] - t_shape["net_pnl"]) / denom * 100
    wr_gap = abs(e_shape["win_rate_pct"] - t_shape["win_rate_pct"])
    sl_gap = (abs(e_shape["median_sl_r"] - t_shape["median_sl_r"])
              if e_shape["median_sl_r"] is not None
              and t_shape["median_sl_r"] is not None else None)
    recall = 100 * len(e_syms & t_syms) / len(t_syms) if t_syms else 0.0

    fails = []
    if net_gap > MAX_NET_GAP_PCT:
        fails.append(f"net PnL gap {net_gap:.0f}% > {MAX_NET_GAP_PCT:.0f}%")
    if wr_gap > MAX_WIN_RATE_GAP_PP:
        fails.append(f"win-rate gap {wr_gap:.1f}pp > {MAX_WIN_RATE_GAP_PP:.0f}pp")
    if sl_gap is not None and sl_gap > MAX_SL_R_GAP:
        fails.append(f"stop-R gap {sl_gap:.2f} > {MAX_SL_R_GAP:.2f}")
    if recall < MIN_NAME_RECALL_PCT:
        fails.append(f"name recall {recall:.0f}% < {MIN_NAME_RECALL_PCT:.0f}%")

    return {
        "engine": engine["name"],
        "verdict": "PASS" if not fails else "FAIL",
        "fails": fails,
        "engine_shape": e_shape,
        "net_gap_pct": round(net_gap, 1),
        "win_rate_gap_pp": round(wr_gap, 1),
        "stop_r_gap": round(sl_gap, 2) if sl_gap is not None else None,
        "name_recall_pct": round(recall, 1),
        "names_engine_only": sorted(e_syms - t_syms),
        "names_live_only": sorted(t_syms - e_syms),
        "extra": engine.get("extra", {}),
        "elapsed_sec": engine.get("elapsed_sec"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default=None)
    ap.add_argument("--to", dest="end", default=None)
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--compare-legacy", action="store_true",
                    help="also score backtest_v3 and sandbox on the same window")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s | %(message)s")

    # Default window: the span live actually traded, so the comparison is not
    # scoring the engine over sessions the account sat out.
    from src import db
    rows = db.closed_trades(limit=10_000)
    if not rows:
        print("no closed trades in the database — nothing to calibrate against")
        return 1
    all_dates = sorted(str(r.get("opened_at") or r.get("ts") or "")[:10]
                       for r in rows if r.get("ts"))
    start_s = args.start or all_dates[0]
    end_s = args.end or all_dates[-1]

    truth = live_trades(start_s, end_s)
    if not truth:
        print(f"no live trades in {start_s} → {end_s}")
        return 1

    tickers = args.tickers or json.loads(
        (ROOT / "config" / "watchlist.json").read_text())["tickers"]
    # Every name live actually traded must be in the engine's universe, or a
    # miss is a universe artifact rather than a strategy disagreement.
    tickers = sorted(set(tickers) | {t["symbol"] for t in truth})

    start = datetime.fromisoformat(start_s).replace(tzinfo=ET)
    end = datetime.fromisoformat(end_s).replace(hour=16, tzinfo=ET) + timedelta(days=1)

    engines = [run_v4(start, end, tickers)]
    if args.compare_legacy:
        for fn in (run_v3, run_sandbox):
            try:
                engines.append(fn(start, end, tickers))
            except Exception as e:
                print(f"  ({fn.__name__} failed: {e})")

    t_shape = _shape(truth)
    results = [score(e, truth) for e in engines]

    print(f"\n{'='*72}")
    print(f"v4 vs LIVE   {start_s} → {end_s}   ({len(tickers)} tickers)")
    print(f"{'='*72}")
    print(f"\nLIVE (real fills, {t_shape['n']} closed trades)")
    print(f"  win rate {t_shape['win_rate_pct']}%   net ${t_shape['net_pnl']:+,.2f}   "
          f"R median {t_shape['median_r']}  worst {t_shape['worst_r']}  "
          f"stop-median {t_shape['median_sl_r']}")
    print(f"  names: {', '.join(t_shape['symbols'])}")

    hdr = (f"\n{'engine':10} {'n':>4} {'WR%':>7} {'net $':>11} {'netΔ%':>7} "
           f"{'WRΔpp':>7} {'stopR':>7} {'stopΔ':>7} {'recall':>7}  verdict")
    print(hdr)
    print("-" * len(hdr.strip()))
    for r in results:
        s = r["engine_shape"]
        print(f"{r['engine']:10} {s['n']:>4} {s['win_rate_pct']:>7} "
              f"{s['net_pnl']:>11,.2f} {r['net_gap_pct']:>7} "
              f"{r['win_rate_gap_pp']:>7} "
              f"{str(s['median_sl_r']):>7} {str(r['stop_r_gap']):>7} "
              f"{r['name_recall_pct']:>6}%  {r['verdict']}")

    for r in results:
        print(f"\n── {r['engine']} ──")
        if r["fails"]:
            for f in r["fails"]:
                print(f"  FAIL  {f}")
        else:
            print("  PASS  within every tolerance")
        if r["names_live_only"]:
            print(f"  missed names live traded: {', '.join(r['names_live_only'])}")
        if r["names_engine_only"]:
            print(f"  traded names live did not: {', '.join(r['names_engine_only'])}")
        if r["extra"]:
            print(f"  {r['extra']}")

    report = {
        "generated_at": datetime.now(ET).isoformat(),
        "window": {"start": start_s, "end": end_s},
        "tickers": tickers,
        "live": t_shape,
        "live_trades": truth,
        "tolerances": {
            "max_net_gap_pct": MAX_NET_GAP_PCT,
            "max_win_rate_gap_pp": MAX_WIN_RATE_GAP_PP,
            "max_sl_r_gap": MAX_SL_R_GAP,
            "min_name_recall_pct": MIN_NAME_RECALL_PCT,
        },
        "results": results,
    }
    Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    print(f"\nSaved → {args.output}")

    v4 = next((r for r in results if r["engine"] == "v4"), None)
    return 0 if v4 and v4["verdict"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
