"""Did the advisory AI verdict predict anything? Answered from what already ran.

    .venv/bin/python scripts/ai_shadow_eval.py [--db PATH]

WHY THIS IS NOT A BACKTEST, AND WHY THAT IS THE POINT

  The plan asks for an out-of-sample ablation of the AI layer. A backtest
  cannot give one: there is no point-in-time news archive, and a frontier model
  asked about March already knows how March ended.

  None of that applies here. Every entry records the advisory verdict at the
  moment it was made — main.py persists ai_score and ai_pass into the audit
  row, with a note from 2026-06-11 saying exactly why: "without it no
  AI-vs-outcome calibration is possible". The verdict was written before the
  outcome existed, by a consult that runs AFTER the order and cannot change it.

  So there is no look-ahead (the model saw only what was published then), and
  no selection effect (it could not veto, so the sample is every trade, not the
  ones it liked). This is a forward test that has already been running.

WHAT IT CANNOT SAY

  Sample size. Below about fifty scored, closed trades any split is noise, and
  the honest output is "not enough yet" rather than a number with a decimal
  point. It prints n on every line so that is never hidden.

  Two score values are not verdicts and are excluded: 50 is what main.py
  writes when the AI budget is exhausted, and a null is what it writes when no
  key is configured.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

MIN_N = 50          # below this, report the shortfall rather than a verdict


def parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def load(db: Path):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    buys = []
    for r in c.execute("SELECT ts, symbol, extra FROM audit WHERE action='buy' "
                       "ORDER BY ts"):
        try:
            e = json.loads(r["extra"] or "{}")
        except Exception:
            e = {}
        buys.append({"ts": parse_ts(r["ts"]), "symbol": r["symbol"],
                     "ai_score": e.get("ai_score"), "ai_pass": e.get("ai_pass"),
                     "rule_score": e.get("score"), "regime": e.get("regime"),
                     "sentiment": e.get("sentiment_score")})
    closes = []
    for r in c.execute("SELECT ts, symbol, pnl, r_multiple, exit_reason "
                       "FROM closed_trades ORDER BY ts"):
        closes.append({"ts": parse_ts(r["ts"]), "symbol": r["symbol"],
                       "pnl": r["pnl"], "r": r["r_multiple"],
                       "reason": r["exit_reason"]})
    c.close()
    return buys, closes


def join(buys, closes):
    """Each buy to the first close of the same symbol after it. Greedy, so one
    close is never claimed by two buys."""
    taken = set()
    out = []
    for b in buys:
        if b["ts"] is None:
            continue
        best = None
        for i, cl in enumerate(closes):
            if i in taken or cl["symbol"] != b["symbol"] or cl["ts"] is None:
                continue
            if cl["ts"] >= b["ts"]:
                best = i
                break
        if best is not None:
            taken.add(best)
            out.append({**b, "pnl": closes[best]["pnl"],
                        "r": closes[best]["r"], "reason": closes[best]["reason"],
                        "held_h": (closes[best]["ts"] - b["ts"]).total_seconds() / 3600})
    return out


def describe(rows, label):
    if not rows:
        return f"  {label:22} n=0"
    pnl = [float(r["pnl"] or 0) for r in rows]
    wins = sum(1 for p in pnl if p > 0)
    rs = [float(r["r"]) for r in rows if r["r"] is not None]
    return (f"  {label:22} n={len(rows):3}  net ${sum(pnl):>9,.2f}  "
            f"avg ${statistics.mean(pnl):>8,.2f}  win {wins / len(rows) * 100:4.0f}%"
            + (f"  avgR {statistics.mean(rs):+.2f}" if rs else ""))


def evaluate(db_path=None, min_n: int = MIN_N) -> dict:
    """The same analysis, as data — for the weekly review to report on its own.

    Returns enough to decide whether anything can be said yet: the sample
    sizes, the split each way, and `ready`, which is False until there are
    min_n scored+closed trades. A caller that ignores `ready` and quotes the
    numbers anyway is quoting noise.
    """
    db = Path(db_path or os.path.expanduser(
        "~/Library/Application Support/MooMooTrader/data/trader.db"))
    if not db.exists():
        return {"ready": False, "error": f"no database at {db}"}
    buys, closes = load(db)
    matched = join(buys, closes)
    scored = [r for r in matched
              if r["ai_score"] is not None and float(r["ai_score"]) != 50.0]
    out = {"db": str(db), "n_buys": len(buys), "n_matched": len(matched),
           "n_scored": len(scored), "min_n": min_n,
           "ready": len(scored) >= min_n}
    if scored:
        vals = sorted(float(r["ai_score"]) for r in scored)
        med = statistics.median(vals)
        hi = [r for r in scored if float(r["ai_score"]) >= med]
        lo = [r for r in scored if float(r["ai_score"]) < med]

        def agg(rows):
            if not rows:
                return None
            pnl = [float(r["pnl"] or 0) for r in rows]
            return {"n": len(rows), "net": round(sum(pnl), 2),
                    "avg": round(statistics.mean(pnl), 2),
                    "win_pct": round(sum(1 for p in pnl if p > 0) / len(rows) * 100, 1)}
        out.update({"median_score": med, "above_median": agg(hi),
                    "below_median": agg(lo), "all": agg(scored)})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.path.expanduser(
        "~/Library/Application Support/MooMooTrader/data/trader.db"))
    ap.add_argument("--min-n", type=int, default=MIN_N)
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 2

    buys, closes = load(db)
    matched = join(buys, closes)

    # 50 is "AI budget exhausted — neutral" and null is "no key configured".
    # Neither is a verdict about the trade.
    scored = [r for r in matched
              if r["ai_score"] is not None and float(r["ai_score"]) != 50.0]

    print(f"\nAI advisory verdict vs realised outcome — {db.name}")
    print("=" * 74)
    print(f"  buys recorded          {len(buys)}")
    print(f"  matched to a close     {len(matched)}")
    print(f"  carrying a real score  {len(scored)}"
          f"   (excluded: {len(matched) - len(scored)} neutral/absent)")
    print()

    if not scored:
        print("  Nothing scored has closed yet.")
        return 0

    vals = sorted(float(r["ai_score"]) for r in scored)
    med = statistics.median(vals)
    print(f"  score range {vals[0]:.0f}–{vals[-1]:.0f}, median {med:.0f}")
    print()
    print(describe(scored, "all scored"))
    print(describe([r for r in scored if float(r["ai_score"]) >= med],
                   f"score >= {med:.0f}"))
    print(describe([r for r in scored if float(r["ai_score"]) < med],
                   f"score <  {med:.0f}"))
    print()
    print(describe([r for r in scored if float(r["ai_score"]) >= 90], "score >= 90"))
    print(describe([r for r in scored if float(r["ai_score"]) <= 10], "score <= 10"))
    print()
    print(describe([r for r in scored if r["ai_pass"]], "ai_pass = True"))
    print(describe([r for r in scored if not r["ai_pass"]], "ai_pass = False"))

    # Does the score track the outcome at all? Spearman needs scipy; rank
    # correlation by hand keeps this dependency-free.
    try:
        import math
        n = len(scored)
        sx = sorted(range(n), key=lambda i: float(scored[i]["ai_score"]))
        sy = sorted(range(n), key=lambda i: float(scored[i]["pnl"] or 0))
        rx = {v: i for i, v in enumerate(sx)}
        ry = {v: i for i, v in enumerate(sy)}
        d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
        rho = 1 - 6 * d2 / (n * (n * n - 1)) if n > 2 else float("nan")
        print(f"\n  rank correlation, score vs PnL:  rho = {rho:+.3f}   (n={n})")
        if n > 3 and not math.isnan(rho):
            t = abs(rho) * math.sqrt((n - 2) / max(1e-9, 1 - rho * rho))
            print(f"  |t| = {t:.2f}   (about 2.0 is the usual bar at this n)")
    except Exception as e:
        print(f"  correlation failed: {e}")

    print()
    print("=" * 74)
    if len(scored) < args.min_n:
        print(f"  VERDICT: not enough. {len(scored)} scored+closed trades against "
              f"{args.min_n} needed.")
        print("  Every split above is inside the noise at this sample size, and")
        print("  no decision about keeping or dropping the layer should rest on it.")
    else:
        print(f"  {len(scored)} scored+closed trades — the splits above can be read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
