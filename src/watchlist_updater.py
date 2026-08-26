"""Auto-refresh watchlist using yfinance — no API key required.

Pulls S&P 500 constituents from Wikipedia, screens by liquidity / ATR% /
momentum, writes the top tickers to config/watchlist.json. The previous
file is backed up to watchlist.json.bak before each write.

A small "anchor" set (mega-caps + index ETFs) is ALWAYS included so the
core day-trade vehicles never disappear after a quiet week.

Run weekly via scheduler (Sun 22:00 ET) or manually:
    .venv/bin/python -m src.watchlist_updater
"""
from __future__ import annotations

import gc
import io
import json
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from .config import settings

log = logging.getLogger(__name__)

WATCHLIST_FILE = settings.root / "config" / "watchlist.json"
BACKUP_FILE = settings.root / "config" / "watchlist.json.bak"

# Anchor tickers — always in the watchlist regardless of screen results.
# 2026-05-30: tightened to the top-10 winners only. The robustness sweep showed
# top-10 had a Sortino edge (8.60 vs 7.85 full) and 3pp lower DD on 360 days.
# The 9 previously-anchored names (TSLA/NVDA/MSFT/GOOGL/AAPL etc) all
# under-traded or under-performed on per-symbol PnL.
ANCHORS = [
    "SNDK", "MU", "INTC", "LRCX", "DDOG",
    "AMD",  "WDC", "SWKS", "PANW", "MCHP",
]

# Screen filters — env-overridable via WATCHLIST_* settings.
MIN_AVG_VOLUME = 5_000_000   # 5M shares/day average
MIN_PRICE = 15.0             # avoid sub-$15 stocks (ATR too small in $)
MIN_ATR_PCT = 1.5            # daily ATR must be >= 1.5% of price
LOOKBACK_DAYS = 30           # how much history to pull for the screen
# Phase 2c: PINNED to the 10 anchors. Backtests on the honest cash engine showed
# expanding to 15/25 names only diluted signal quality (the $5k cash wall locks at
# ~2 positions). Capping TARGET_SIZE at the anchor count makes TOP_FROM_SCREEN = 0,
# so a refresh re-writes the same 10 anchors instead of padding with screen tail.
# To re-enable auto-expansion, bump TARGET_SIZE back to 30.
TARGET_SIZE = len(ANCHORS)   # final watchlist size — pinned to anchors only
TOP_FROM_SCREEN = max(0, TARGET_SIZE - len(ANCHORS))

# Recent-performance filters — added after the 142-day backtest revealed that
# 12 of 30 watchlist symbols had negative cumulative PnL (~$-750). The common
# pattern was decent ATR + decent volume but recently-declining price. These
# filters drop names whose own price action has rolled over before they reach
# the strategy's signal engine.
MIN_RET_5D_PCT = 0.0         # require positive 5-day return
MIN_RET_20D_PCT = -3.0       # allow shallow 20-day pullback but not deep decline


# Fallback S&P 500 list — used only if Wikipedia is unreachable. Kept short:
# the actual screen will gracefully run on whatever subset yfinance returns.
_SP500_FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "BRK-B",
    "JPM", "WMT", "LLY", "V", "UNH", "ORCL", "MA", "XOM", "COST", "HD", "JNJ",
    "PG", "BAC", "ABBV", "NFLX", "CVX", "KO", "AMD", "CRM", "PEP", "TMO",
    "ACN", "WFC", "MRK", "MCD", "ADBE", "LIN", "ABT", "CSCO", "DIS", "GE",
    "INTC", "MU", "QCOM", "PLTR", "COIN", "RDDT", "UBER", "NFLX", "PYPL",
    "BA", "CAT", "F", "GM", "NKE", "SBUX", "TGT", "LOW", "IBM", "TXN", "NOW",
    "MRNA", "STX", "SNDK", "TPL", "ASML", "TSM", "ARM",
    "XLE", "XLF", "XLK", "XLI", "XLV", "XLU", "XLP", "XLY", "XLB", "XLRE",
    "IWM", "DIA", "VTI", "EFA",
]


# ---------- universe ----------

def fetch_sp500_tickers() -> list[str]:
    """Pull current S&P 500 constituents from Wikipedia. Falls back on error.

    pd.read_html sends a default urllib UA that Wikipedia blocks with 403, so
    we fetch with requests + a real browser UA and feed the HTML to pandas.
    """
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        df = tables[0]
        tickers = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
        log.info("S&P 500 universe: %d tickers from Wikipedia", len(tickers))
        return tickers
    except Exception as e:
        log.warning("Wikipedia fetch failed (%s) — using fallback list (%d tickers)",
                    e, len(_SP500_FALLBACK))
        return list(_SP500_FALLBACK)


# ---------- screen ----------

def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> float:
    """Wilder ATR — the definition the rest of the system uses.

    This was a rolling mean of true range, which runs a median 8% above
    ta.atr (P90 18%, worst 34%) and reacts faster to a volatility spike.
    atr_pct built from it is both a filter (>= MIN_ATR_PCT) and 40% of the
    ranking score, so the watchlist was being selected and ordered on a
    volatility measure the trader does not use.
    """
    import pandas_ta_classic as _ta
    v = _ta.atr(high, low, close, length=length)
    return float(v.iloc[-1]) if v is not None and len(v) else float("nan")


def screen_universe(tickers: list[str]) -> pd.DataFrame:
    """Batch-download OHLCV via yfinance, compute per-ticker metrics, return ranked df."""
    log.info("Downloading %d tickers via yfinance...", len(tickers))
    t0 = time.time()
    data = yf.download(
        tickers,
        period=f"{LOOKBACK_DAYS}d",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    log.info("yfinance batch done in %.1fs", time.time() - t0)

    # threads=True stays — 500 tickers serially is not an option. The cost is
    # that every yfinance worker thread opens its own SQLite connection to the
    # tz cache (peewee keeps connection state in a threading.local) and those
    # objects sit in reference cycles until a gen-2 GC pass breaks them. The
    # scheduler is a long-lived process under macOS's default 256-fd soft
    # limit, so waiting for that pass is how you get EMFILE a few refreshes in.
    # Collecting here keeps the fd count flat. `data` stays referenced, so only
    # garbage is freed.
    gc.collect()

    # SPY anchor for relative strength calc.
    try:
        spy_close = data["SPY"]["Close"].dropna()
        spy_5d_return = (spy_close.iloc[-1] / spy_close.iloc[-6] - 1) * 100 if len(spy_close) >= 6 else 0
    except (KeyError, IndexError):
        spy_5d_return = 0.0

    rows = []
    for sym in tickers:
        try:
            df = data[sym].dropna()
        except KeyError:
            continue
        if len(df) < 15:
            continue
        try:
            avg_vol = float(df["Volume"].tail(20).mean())
            last_price = float(df["Close"].iloc[-1])
            atr14 = _atr(df["High"], df["Low"], df["Close"], length=14)
            if pd.isna(atr14) or last_price <= 0:
                continue
            atr_pct = atr14 / last_price * 100
            ret_5d = (df["Close"].iloc[-1] / df["Close"].iloc[-6] - 1) * 100 if len(df) >= 6 else 0
            ret_20d = (df["Close"].iloc[-1] / df["Close"].iloc[0] - 1) * 100
            rs = ret_5d - spy_5d_return   # relative strength vs SPY (5d)
        except (KeyError, IndexError, ValueError):
            continue

        rows.append({
            "symbol": sym,
            "price": last_price,
            "avg_vol": avg_vol,
            "atr_pct": atr_pct,
            "ret_5d": ret_5d,
            "ret_20d": ret_20d,
            "rs_5d": rs,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Apply hard filters — liquidity, price floor, vol envelope, AND recent
    # performance (so we don't admit names already rolling over).
    filtered = df[
        (df["avg_vol"] >= MIN_AVG_VOLUME)
        & (df["price"] >= MIN_PRICE)
        & (df["atr_pct"] >= MIN_ATR_PCT)
        & (df["ret_5d"] >= MIN_RET_5D_PCT)
        & (df["ret_20d"] >= MIN_RET_20D_PCT)
    ].copy()

    # Composite score — 60% momentum/RS, 40% volatility quality.
    if not filtered.empty:
        filtered["score"] = (
            0.4 * filtered["rs_5d"]
            + 0.2 * filtered["ret_20d"]
            + 0.4 * filtered["atr_pct"]
        )
        filtered = filtered.sort_values("score", ascending=False).reset_index(drop=True)

    log.info("Screen: %d → %d after filters", len(df), len(filtered))
    return filtered


# ---------- write ----------

def build_watchlist(ranked: pd.DataFrame) -> list[str]:
    """Combine top-ranked tickers with the anchor set, preserving order.

    Symbols currently on the adaptive blacklist are excluded from BOTH the
    anchor set and the ranked tail — blacklist > yfinance momentum, since
    the blacklist reflects this account's real PnL history.
    """
    # Lazy import: keeps the watchlist module standalone-runnable for tests
    # that don't have the app DB schema bootstrapped.
    try:
        from . import blacklist as _bl
        banned = set(_bl.get_blacklist().keys())
    except Exception as e:
        log.warning("blacklist lookup failed (%s) — proceeding without it", e)
        banned = set()

    if banned:
        log.info("Watchlist build: %d blacklisted symbols excluded: %s",
                 len(banned), sorted(banned))

    top = ranked["symbol"].head(TOP_FROM_SCREEN + len(banned)).tolist() \
        if not ranked.empty else []
    seen = set()
    out: list[str] = []
    for sym in ANCHORS + top:
        s = sym.upper()
        if s in banned:
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= TARGET_SIZE:
            break
    return out


def write_watchlist(tickers: list[str], ranked: pd.DataFrame) -> None:
    """Backup old file, write the new one with metadata for audit."""
    if WATCHLIST_FILE.exists():
        shutil.copy2(WATCHLIST_FILE, BACKUP_FILE)

    # Build a per-ticker note so we can see why each is on the list.
    notes = {}
    if not ranked.empty:
        for _, row in ranked.iterrows():
            notes[row["symbol"]] = (
                f"vol={row['avg_vol']/1e6:.1f}M "
                f"atr={row['atr_pct']:.1f}% "
                f"ret5d={row['ret_5d']:+.1f}% "
                f"rs={row['rs_5d']:+.1f}"
            )

    payload = {
        "_comment": (
            f"Auto-refreshed {datetime.utcnow().isoformat()}Z via watchlist_updater. "
            f"Anchors always included; rest is top-{TOP_FROM_SCREEN} by composite score "
            f"(RS_5d + 20d momentum + ATR%). Run `python -m src.watchlist_updater` to refresh."
        ),
        "tickers": tickers,
        "notes": {t: notes.get(t, "anchor") for t in tickers},
    }
    WATCHLIST_FILE.write_text(json.dumps(payload, indent=2))
    log.info("Watchlist written: %d tickers → %s", len(tickers), WATCHLIST_FILE)


# ---------- entrypoint ----------

def refresh() -> list[str]:
    """End-to-end refresh. Returns the new list of tickers."""
    log.info("=== watchlist refresh start ===")
    universe = fetch_sp500_tickers()
    # Always merge anchors into the universe so they get metrics too (some
    # ETFs aren't in S&P 500).
    universe = sorted(set(universe) | set(ANCHORS))
    ranked = screen_universe(universe)
    final = build_watchlist(ranked)
    write_watchlist(final, ranked)
    log.info("=== watchlist refresh done: %d tickers ===", len(final))
    return final


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    tickers = refresh()
    print(f"\nNew watchlist ({len(tickers)}):")
    print(", ".join(tickers))


if __name__ == "__main__":
    main()
