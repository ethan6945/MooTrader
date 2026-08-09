"""Does a news sentiment score actually sort forward returns? (2026-08-07)

THE GATE THE NEWS-DRIVEN SWITCH NEVER CLEARED. `NEWS_DRIVEN_ENABLED` ships OFF
with a preflight warning saying no factor study backs it, unlike the
options-volume factor which had to clear scripts/options_factor_study.py over
5,980 name-days first. This is that study for news. Run it before trusting the
mode with money; the answer may well be "no".

WHY IT IS ONLY POSSIBLE NOW. Two things had to exist together:
  • Finnhub company-news, which answers for a PAST date range (Tavily only
    answers "now", so the input to a past decision was unreconstructable).
  • FinBERT, whose training corpus predates any window tested here. Scoring
    2026 headlines with a frontier LLM would measure its memory of what
    happened, not a signal.
Neither alone is enough, which is why this script arrives with them.

── TIMING, WHICH IS WHERE STUDIES LIE TO THEMSELVES ─────────────────────────
News published on day D at 15:00 "predicting" day D's move is not prediction,
it is a recap — and it is the single easiest way to manufacture a beautiful,
worthless backtest. Daily bars cannot separate intraday timing, so this study
takes the conservative alignment and never the flattering one:

    score  ← headlines published through day D-1's session
    entry  ← day D OPEN
    exit   ← day D CLOSE          (ret_session — what the live mode captures)

ret_session is the honest headline number, because news-driven mode flattens
at the close: a one-session bet is all it can collect. next-day and next-3-day
close-to-close are reported too, for comparability with the options study and
because the documented post-news drift (PEAD) runs for days — but the mode as
built cannot capture them, so do not read them as its expected return.

── WHAT A PASS AND A FAIL MEAN ──────────────────────────────────────────────
PASS = buckets sort MONOTONICALLY on ret_session, the top bucket beats the
baseline on both win rate and mean, and that survives in BOTH halves of the
sample. Anything less: leave NEWS_DRIVEN_ENABLED off. A PASS still only means
"keep collecting forward samples" — one regime, one scorer, and a bucket count
small enough that a couple of names can carry it.

And note what this CANNOT measure: the live path scores with an LLM and demands
a named catalyst, neither of which is modelled here. FinBERT sentiment sorting
returns is evidence that news carries signal in this universe. It is not proof
the live gate captures it. Running the mode on the SIMULATED account collects
that, and `--live` scores it.

Usage (from repo root; needs FINNHUB_API_KEY and a downloaded FinBERT):
    .venv/bin/python -m scripts.news_factor_study --days 180
    .venv/bin/python -m scripts.news_factor_study --symbols NVDA,AMD --days 90
    .venv/bin/python -m scripts.news_factor_study --csv /tmp/newsfactor.csv

Finnhub's free tier is 60 calls/min and ~1 year of history, so a full pool-wide
run is one call per name-day and takes a while. Every fetch is cached to
data/news_study_cache/, so a re-run costs nothing and an interrupted run
resumes.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import finnhub_news, news_score_local, universe
from src.config import ROOT

CACHE_DIR = ROOT / "data" / "news_study_cache"
# Finnhub free tier: 60/min. Stay well under — a 429 mid-run costs the rest of
# the minute and this script makes thousands of calls.
_SLEEP_S = 1.1


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.json"


def _load_cache(symbol: str) -> dict:
    try:
        return json.loads(_cache_path(symbol).read_text())
    except Exception:
        return {}


def _save_cache(symbol: str, data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(symbol).write_text(json.dumps(data))
    except Exception as e:
        print(f"  (cache write failed for {symbol}: {e})")


def fetch_prices(symbol: str, start: date, end: date) -> pd.DataFrame | None:
    """Daily OHLC via yfinance — no broker needed, so this can run unattended."""
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(start=start.isoformat(),
                                       end=(end + timedelta(days=1)).isoformat(),
                                       auto_adjust=False)
    except Exception as e:
        print(f"  skip {symbol}: price fetch failed ({e})")
        return None
    if df is None or df.empty:
        return None
    df = df.rename(columns=str.lower)
    if hasattr(df.index, "tz_localize"):
        try:
            df.index = df.index.tz_localize(None)
        except (TypeError, AttributeError):
            pass
    return df[["open", "high", "low", "close"]].dropna()


def build_panel(symbols: list[str], days: int, news_days: int,
                refresh: bool = False) -> pd.DataFrame:
    """One row per (symbol, tradeable day) that had news in the prior window."""
    end = date.today()
    start = end - timedelta(days=days + 10)      # padding for the forward legs
    rows: list[dict] = []

    for si, sym in enumerate(symbols, 1):
        px = fetch_prices(sym, start, end)
        if px is None or len(px) < 20:
            print(f"  skip {sym}: not enough price history")
            continue
        cache = {} if refresh else _load_cache(sym)
        cache_dirty = False
        sess = [d.date() for d in px.index]
        n_scored = 0

        for i, day in enumerate(sess):
            # Forward legs must exist, else the row is unusable.
            if i + 3 >= len(sess):
                break
            prior = sess[i - 1] if i >= 1 else None
            if prior is None:
                continue

            key = prior.isoformat()
            if key in cache:
                items = cache[key]
            else:
                # POINT-IN-TIME: `until=prior` — nothing published after the
                # previous session can enter this row. finnhub_news re-checks
                # the bound locally rather than trusting the server.
                items = finnhub_news.fetch_company_news(sym, days=news_days,
                                                        until=prior)
                cache[key] = items
                cache_dirty = True
                time.sleep(_SLEEP_S)
            if not items:
                continue

            score, _detail = news_score_local.score_news(items)
            if score is None:
                continue
            n_scored += 1

            o, c = float(px.iloc[i]["open"]), float(px.iloc[i]["close"])
            c1, c3 = float(px.iloc[i + 1]["close"]), float(px.iloc[i + 3]["close"])
            rows.append({
                "sym": sym,
                "day": pd.Timestamp(day),
                "score": score,
                "n_items": len(items),
                # The live mode's actual horizon: in at the open, out at the close.
                "ret_session": (c / o - 1) * 100,
                # For comparability with options_factor_study; NOT capturable by
                # news-driven mode as built, which flattens before the close.
                "ret_next": (c1 / c - 1) * 100,
                "ret_next3": (c3 / c - 1) * 100,
            })

        if cache_dirty:
            _save_cache(sym, cache)
        print(f"  [{si}/{len(symbols)}] {sym}: {n_scored} scored name-days")

    if not rows:
        raise RuntimeError(
            "no scored name-days. Check FINNHUB_ENABLED + FINNHUB_API_KEY, and "
            "that FinBERT is downloaded (Settings → FinBERT, or "
            "FINBERT_AUTO_DOWNLOAD=true).")
    return pd.DataFrame(rows)


def _row(sub: pd.DataFrame, label: str, min_n: int = 30) -> None:
    if len(sub) < min_n:
        print(f"  {label:40s} n={len(sub):5d}   (too few to read)")
        return
    print(f"  {label:40s} n={len(sub):5d}  win%={100 * (sub.ret_session > 0).mean():5.1f}"
          f"  session={sub.ret_session.mean():+6.3f}%"
          f"  next1={sub.ret_next.mean():+6.3f}%"
          f"  next3={sub.ret_next3.mean():+6.3f}%"
          f"  sd={sub.ret_session.std():5.2f}%")


def report(panel: pd.DataFrame, threshold: int) -> bool:
    print(f"\nTOTAL name-days: {len(panel)}   "
          f"span {str(panel.day.min())[:10]} .. {str(panel.day.max())[:10]}   "
          f"names: {panel.sym.nunique()}\n")
    print("win% and `session` are the honest columns — open-to-close, the only")
    print("horizon news-driven mode can capture. next1/next3 are context.\n")

    print("=== BASELINE (every name-day WITH news) ===")
    _row(panel, "all scored name-days")

    print("\n=== FinBERT score buckets (want MONOTONIC on `session`) ===")
    for lo, hi in [(0, 35), (35, 45), (45, 55), (55, 65), (65, 75), (75, 101)]:
        _row(panel[(panel.score >= lo) & (panel.score < hi)],
             f"score {lo}-{hi if hi <= 100 else '100'}")

    print(f"\n=== the live rule's shape: score >= {threshold} ===")
    hot = panel[panel.score >= threshold]
    ctl = panel[panel.score < threshold]
    _row(hot, f"score >= {threshold}   <-- the rule")
    _row(ctl, f"control: score < {threshold}")

    print("\n=== stability: same rule, each half of the sample ===")
    mid = panel.day.min() + (panel.day.max() - panel.day.min()) / 2
    halves_pass = []
    for label, first in [("first half", True), ("second half", False)]:
        mask = panel.day < mid if first else panel.day >= mid
        h, b = hot[mask.reindex(hot.index, fill_value=False)], panel[mask]
        _row(h, f"{label}: rule")
        _row(b, f"{label}: baseline")
        halves_pass.append(len(h) >= 30
                           and h.ret_session.mean() > b.ret_session.mean())

    ok = (len(hot) >= 30 and len(ctl) >= 30
          and (hot.ret_session > 0).mean() > (ctl.ret_session > 0).mean()
          and hot.ret_session.mean() > ctl.ret_session.mean()
          and all(halves_pass))

    print("\n" + ("PASS — the score sorts open-to-close returns and holds up in "
                  "both halves."
                  if ok else
                  "FAIL — the score does not sort returns here. Leave "
                  "NEWS_DRIVEN_ENABLED off."))
    print("\nRead this narrowly either way:")
    print("  • FinBERT scores sentiment, not price impact. A PASS says news")
    print("    carries signal in this universe, not that the LIVE gate (an LLM")
    print("    plus a named-catalyst requirement) captures it — that is what")
    print("    running the mode on the paper account collects — score it")
    print("    with `--live`.")
    print("  • One regime, one scorer, and buckets small enough that a couple")
    print("    of names can carry them. A PASS means 'keep collecting', never")
    print("    'arm it and size up'.")
    print("  • Costs are not modelled. A same-session round trip pays spread")
    print("    plus fees EVERY day; a thin edge here is a losing strategy live.")
    return ok


def report_live() -> int:
    """Score what the mode ACTUALLY did, by joining the news read onto outcomes.

    This is the measurement the offline study above cannot make. Every row is a
    decision the real pipeline reached — LLM read, named-catalyst requirement,
    risk, sizing — and then acted on, so the outcome attached to it is the
    thing that will trade rather than a proxy for it.

    Both halves are already on disk and neither needed a feature to produce:
    audit.jsonl carries the news score and the FinBERT cross-check on every buy
    (audit.record flattens `extra` into the row), and trades.jsonl carries the
    realised P&L when the position closes. Run this against the SIMULATED
    account and it costs nothing but time.

    This replaced a "shadow mode" that logged what the gate WOULD have bought
    without ordering. Paper mode produces the same dataset plus real fills and
    P&L, so shadow was a strictly weaker duplicate — and the parallel code path
    had already grown a bug where it blocked buys but not the 15:45 flatten.
    """
    def _rows(path):
        out = []
        if not path.exists():
            return out
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    audit_p, trades_p = ROOT / "data" / "audit.jsonl", ROOT / "data" / "trades.jsonl"
    buys = [r for r in _rows(audit_p)
            if r.get("action") == "buy" and r.get("news_driven")]
    if not buys:
        print(f"No news-driven buys in {audit_p}.\n"
              "Set NEWS_DRIVEN_ENABLED=true with TRADE_ENV=SIMULATE and let it "
              "run — every paper fill records the news read and the outcome.")
        return 2
    closed = _rows(trades_p)
    if not closed:
        print(f"{len(buys)} news-driven buys recorded, but {trades_p} has no "
              "closed trades yet — outcomes arrive when the positions close.")
        return 2

    def _day(v):
        d = pd.to_datetime(str(v), errors="coerce", utc=True)
        return None if pd.isna(d) else d.normalize()

    # Join on (symbol, day). The mode flattens at the close, so a symbol cannot
    # have two open entries in one session and the pair is unique in practice.
    by_key = {}
    for c in closed:
        k = (str(c.get("symbol", "")).upper(), _day(c.get("opened_at") or c.get("ts")))
        if k[1] is not None:
            by_key.setdefault(k, c)

    out, unmatched = [], 0
    for b in buys:
        k = (str(b.get("symbol", "")).upper(), _day(b.get("ts")))
        c = by_key.get(k)
        if c is None:
            unmatched += 1
            continue
        try:
            ret = float(c.get("pnl_pct"))
        except (TypeError, ValueError):
            continue
        out.append({"symbol": k[0], "day": k[1],
                    "score": b.get("sentiment_score"),
                    "finbert": b.get("finbert_score"),
                    "ret": ret, "exit_reason": c.get("exit_reason") or "?",
                    "mfe": c.get("mfe_pct"), "mae": c.get("mae_pct")})
    if not out:
        print(f"{len(buys)} news-driven buys, none matched to a closed trade "
              "yet — the positions are still open.")
        return 2

    res = pd.DataFrame(out)
    print(f"=== what news-driven mode ACTUALLY did (n={len(res)}"
          + (f", {unmatched} still open" if unmatched else "") + ") ===")
    print(f"  span              : {str(res.day.min())[:10]} .. {str(res.day.max())[:10]}")
    print(f"  names             : {res.symbol.nunique()}")
    print(f"  win rate          : {(res.ret > 0).mean() * 100:.1f}%")
    print(f"  mean per trade    : {res.ret.mean():+.3f}%")
    print(f"  median            : {res.ret.median():+.3f}%")
    print(f"  best / worst      : {res.ret.max():+.2f}% / {res.ret.min():+.2f}%")
    print(f"  total (unweighted): {res.ret.sum():+.2f}%")
    if res["exit_reason"].notna().any():
        print("\n  exits:", ", ".join(f"{k}={v}" for k, v in
                                      res.exit_reason.value_counts().items()))

    if res["score"].notna().any():
        print("\n=== by the LLM's news score ===")
        for lo, hi in [(0, 70), (70, 80), (80, 90), (90, 101)]:
            sub = res[(res.score >= lo) & (res.score < hi)]
            if len(sub):
                print(f"  score {lo}-{min(hi,100):3d}  n={len(sub):4d}  "
                      f"win%={100*(sub.ret>0).mean():5.1f}  "
                      f"mean={sub.ret.mean():+6.3f}%")
    if res["finbert"].notna().any():
        agree = res.dropna(subset=["score", "finbert"])
        if len(agree) >= 20:
            print(f"\n=== LLM vs FinBERT (n={len(agree)}) ===")
            gap = (agree.score - agree.finbert).abs()
            for lbl, sub in [("they agree (gap<=15)", agree[gap <= 15]),
                             ("they disagree (gap>15)", agree[gap > 15])]:
                if len(sub):
                    print(f"  {lbl:24s} n={len(sub):4d}  "
                          f"win%={100*(sub.ret>0).mean():5.1f}  "
                          f"mean={sub.ret.mean():+6.3f}%")
            print("  (if disagreement marks the losers, the cross-check is worth "
                  "promoting from advisory)")

    print("\nP&L here is net of nothing but the simulator's own fill model, which")
    print("is optimistic — a same-session round trip pays spread plus fees every")
    print("time. And a few weeks is not a sample: this tells you whether to keep")
    print("collecting, not whether to go live.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="score what the mode actually traded (audit.jsonl joined "
                         "to trades.jsonl) instead of the offline FinBERT study")
    ap.add_argument("--symbols", help="comma-separated; default = the liquidity pool")
    ap.add_argument("--days", type=int, default=180, help="lookback window (default 180)")
    ap.add_argument("--news-days", type=int, default=None,
                    help="news lookback per row (default: FINNHUB_DAYS)")
    ap.add_argument("--threshold", type=int, default=None,
                    help="score gate (default: NEWS_DRIVEN_MIN_SCORE)")
    ap.add_argument("--refresh", action="store_true", help="ignore the news cache")
    ap.add_argument("--csv", help="write the raw name-day panel here")
    args = ap.parse_args()

    # Live scoring needs neither Finnhub nor FinBERT — it reads what the loop
    # already wrote — so it is handled before those preconditions.
    if args.live:
        return report_live()

    from src.config import settings
    threshold = args.threshold if args.threshold is not None else settings.news_driven_min_score
    news_days = args.news_days if args.news_days is not None else settings.finnhub_days

    # Fail loudly and early rather than after an hour of empty fetches.
    if not finnhub_news.enabled():
        print("FINNHUB_ENABLED / FINNHUB_API_KEY not set — this study needs "
              "point-in-time news. Get a free key at finnhub.io.")
        return 2
    ok_fb, why = news_score_local.available()
    if not ok_fb:
        print(f"FinBERT unavailable ({why}). It is the only look-ahead-free "
              f"scorer here — an LLM would score 2026 headlines from memory.")
        return 2

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        etfs = universe.load_etfs()
        symbols = [s for s in universe.load_pool() if s not in etfs]

    print(f"Building panel: {len(symbols)} names x ~{args.days} days, "
          f"news window {news_days}d, cache {CACHE_DIR}")
    print("(Finnhub free tier is 60/min — first run is slow, re-runs are cached.)\n")
    panel = build_panel(symbols, args.days, news_days, refresh=args.refresh)
    if args.csv:
        panel.to_csv(args.csv, index=False)
        print(f"\nraw panel -> {args.csv}")
    return 0 if report(panel, threshold) else 1


if __name__ == "__main__":
    raise SystemExit(main())
