"""Aggregate options-flow statistics (2026-08-05).

WHY THIS EXISTS ALONGSIDE options_flow.py. That module is written against the
option CHAIN. This account has US options TRADING permission but NOT US options
QUOTE permission, so `get_option_chain` / `get_option_expiration_date` are
denied ("No permission to get quotes ... Please check US MarketOptions quote
permissions") and the chain scaffold cannot run. Three broker endpoints answer
WITHOUT that subscription, and this module uses only those:

  • get_option_underlying_his_statistic  daily call/put volume + OI, 1y history
  • get_option_underlying_overview       live IV / iv_rank / iv_percentile / HV
  • get_derivative_unusual               7-day large-trade text feed WITH side

WHAT THE FACTOR IS, AND WHY IT IS *NOT* THE PUT/CALL RATIO
Measured 2026-08-05 over 5,980 name-days (26 pool names, the full 1y the broker
serves). `call_rvol` = a day's call volume / its own trailing 20-day mean:

    call_rvol      n      win%    next-day    next-3-day
    < 0.7        2001     52.6%    +0.252%     +1.077%
    0.7-1.0      1619     53.2%    +0.343%     +1.030%
    1.0-1.5      1446     55.0%    +0.555%     +1.080%
    2.0-3.0       294     57.8%    +0.832%     +1.996%
    >= 3.0        172     61.6%    +1.846%     +3.058%
    (baseline)   5980     53.8%    +0.442%     +1.240%

Monotonic. The put/call ratio z-score over the same sample is FLAT at every
bucket (51.4 / 55.0 / 53.9 / 53.1 / 53.4 / 55.3 win%), and the decisive control:
"rvol>=1.5 AND put-heavy (z>=+1)" wins 63.2% with +1.864% next-3-day — as good
as "rvol>=1.5 AND call-heavy". The skew carries NO direction. The volume carries
"a large move is coming"; in a bull tape that move leans up.

EARNINGS MUST BE EXCLUDED — this is not optional. 39% of call_rvol>=3 days fall
in an earnings window vs a 6% base rate, and those days are a different and much
fatter distribution (next-day sd 9.08% vs 4.51%). Removing them makes the signal
BETTER, not worse:

    call_rvol>=3, NOT near earnings   n=105   63.8% win   next-3-day +2.559%
    matched control (rvol<1.5, same)  n=4868  53.6% win   next-3-day +1.026%

and it holds in both half-year sub-periods (+1.56% and +1.10% excess next-3-day).
Keeping earnings in would just be betting the print, which the gap sentinel and
the earnings gate already refuse to do on purpose.

LIMITS — read these before trusting the number.
  • The broker caps history at 251 rows (1 year) and that year is a bull market
    (baseline drift +0.442%/day). This factor has NEVER been observed through a
    drawdown and may invert in one.
  • The clean bucket is 105 samples. The confidence interval is wide.
  • Therefore ADVISORY by default: recorded and displayed, never changes which
    trade fires, so live <-> backtest parity is preserved. Arming it is an
    explicit owner decision (OPTIONS_STATS_SIZING).

Every public function is fail-safe: any permission/SDK/data error degrades to a
neutral result and never raises into the scan loop.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import settings

log = logging.getLogger(__name__)

try:
    from moomoo import RET_OK
except Exception:                      # SDK shape guard (keeps import safe)
    RET_OK = 0

# The broker serves at most ~251 daily rows; ask for a comfortable window and
# let it truncate. 20 is the rvol baseline, so anything under ~40 rows is too
# thin to compute a trustworthy mean.
HISTORY_DAYS = 400
RVOL_WINDOW = 20
MIN_ROWS = 40

# Earnings exclusion window, in calendar days around the report date. -1 covers
# a report that lands the evening before the signal day; +4 covers the drift
# days after it. Matches the window used in the 2026-08-05 study.
EARN_BEFORE_D = 1
EARN_AFTER_D = 4

# Per-symbol cache. The scan runs every 15 min over ~15 names and this endpoint
# is rate-limited alongside every other quote call, so a same-session refetch
# buys nothing: the underlying row only changes once a day.
_CACHE_TTL_S = 3600.0
_cache: dict[str, tuple[float, "OptionsStats"]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class OptionsStats:
    """One symbol's aggregate options read. `ok=False` means we have no usable
    data and every caller must treat it as no-opinion, not as bearish."""
    symbol: str
    ok: bool = False
    detail: str = "no data"
    call_rvol: float | None = None
    put_rvol: float | None = None
    put_call_ratio: float | None = None
    call_volume: float | None = None
    rows: int = 0
    asof: str | None = None
    near_earnings: bool = False
    earnings_date: str | None = None

    @property
    def elevated(self) -> bool:
        """The tradeable condition from the study: volume spike AND clear of the
        earnings window. Both halves are required — a spike inside an earnings
        window is the fat-tailed distribution we deliberately exclude."""
        return bool(
            self.ok
            and self.call_rvol is not None
            and self.call_rvol >= settings.options_stats_min_rvol
            and not self.near_earnings
        )

    @property
    def label(self) -> str:
        if not self.ok:
            return "n/a"
        if self.near_earnings and (self.call_rvol or 0) >= settings.options_stats_min_rvol:
            return "spike(earnings—excluded)"
        return "spike" if self.elevated else "normal"

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "ok": self.ok, "detail": self.detail,
            "call_rvol": self.call_rvol, "put_rvol": self.put_rvol,
            "put_call_ratio": self.put_call_ratio, "call_volume": self.call_volume,
            "rows": self.rows, "asof": self.asof, "near_earnings": self.near_earnings,
            "earnings_date": self.earnings_date, "elevated": self.elevated,
            "label": self.label,
        }


def _neutral(symbol: str, detail: str) -> OptionsStats:
    return OptionsStats(symbol=symbol, ok=False, detail=detail)


# ─────────────────────────── history + factor ────────────────────────────────

def fetch_history(symbol: str, client, days: int = HISTORY_DAYS):
    """Raw daily options aggregate for `symbol`, oldest-first, or None.

    Returns a DataFrame with time / call_volume / put_volume /
    put_call_volume_ratio / underlying_price. Never raises.
    """
    try:
        import pandas as pd
    except Exception:
        return None
    try:
        code = client._format_code(symbol)
        end = date.today()
        start = end - timedelta(days=days)
        # This endpoint returns a 3-tuple (ret, data, page_key) on some SDK
        # builds and a 2-tuple on others — normalise instead of unpacking.
        res = client.quote.get_option_underlying_his_statistic(
            code, begin_time=start.isoformat(), end_time=end.isoformat())
        ret, data = res[0], res[1]
        if ret != RET_OK or data is None or not hasattr(data, "columns"):
            return None
        need = ["time", "call_volume", "put_volume",
                "put_call_volume_ratio", "underlying_price"]
        if any(c not in data.columns for c in need):
            return None
        df = data[need].copy()
        for c in need[1:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["call_volume"]).sort_values("time").reset_index(drop=True)
        return df if len(df) else None
    except Exception as e:
        log.debug("options_stats: history fetch failed for %s: %s", symbol, e)
        return None


def compute(df, symbol: str = "", window: int = RVOL_WINDOW) -> dict:
    """Pure factor math on a history frame — no I/O, so the research script and
    the live path compute the identical number. `df` must be oldest-first.

    rvol uses the mean of the window ENDING ON THE PREVIOUS ROW, so the current
    day never contributes to its own baseline (that would damp every spike).
    """
    out = {"ok": False, "detail": "insufficient rows", "rows": 0 if df is None else len(df)}
    if df is None or len(df) < MIN_ROWS:
        return out
    call = df["call_volume"]
    put = df["put_volume"]
    base_c = float(call.iloc[-(window + 1):-1].mean())
    base_p = float(put.iloc[-(window + 1):-1].mean())
    if not base_c or base_c <= 0:
        out["detail"] = "zero call-volume baseline"
        return out
    last_call = float(call.iloc[-1])
    last_put = float(put.iloc[-1])
    pcr = df["put_call_volume_ratio"].iloc[-1]
    return {
        "ok": True,
        "detail": f"{len(df)} rows, 20d call base {base_c:,.0f}",
        "rows": len(df),
        "call_rvol": round(last_call / base_c, 2),
        "put_rvol": round(last_put / base_p, 2) if base_p > 0 else None,
        "put_call_ratio": round(float(pcr), 3) if pcr == pcr else None,
        "call_volume": last_call,
        "asof": str(df["time"].iloc[-1])[:10],
    }


def _earnings_window(symbol: str) -> tuple[bool, str | None]:
    """(inside window, next earnings date). Fail-safe: on any lookup error we
    report TRUE — an unknown earnings date must suppress the signal, never let
    it through, because the earnings subset is exactly what the study excludes."""
    try:
        from . import earnings
        nxt = earnings.get_next_earnings(symbol)
        if not nxt:
            return False, None
        d = date.fromisoformat(str(nxt)[:10])
        today = date.today()
        return (-EARN_BEFORE_D <= (d - today).days <= EARN_AFTER_D), str(nxt)[:10]
    except Exception as e:
        log.debug("options_stats: earnings lookup failed for %s: %s", symbol, e)
        return True, None


def assess(symbol: str, client, use_cache: bool = True) -> OptionsStats:
    """The live entry point. ADVISORY — callers must not let this change which
    trade fires unless the owner armed OPTIONS_STATS_SIZING. Never raises."""
    if not settings.options_stats_enabled:
        return _neutral(symbol, "disabled")
    sym = symbol.upper()
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            hit = _cache.get(sym)
            if hit and (now - hit[0]) < _CACHE_TTL_S:
                return hit[1]
    try:
        df = fetch_history(sym, client)
        c = compute(df, sym)
        if not c.get("ok"):
            res = _neutral(sym, c.get("detail", "no data"))
        else:
            near, edate = _earnings_window(sym)
            res = OptionsStats(
                symbol=sym, ok=True, detail=c["detail"], rows=c["rows"],
                call_rvol=c.get("call_rvol"), put_rvol=c.get("put_rvol"),
                put_call_ratio=c.get("put_call_ratio"),
                call_volume=c.get("call_volume"), asof=c.get("asof"),
                near_earnings=near, earnings_date=edate,
            )
    except Exception as e:
        res = _neutral(sym, f"options stats error ({e})")
    with _cache_lock:
        _cache[sym] = (now, res)
    return res


def conviction_multiplier(stats: OptionsStats) -> float:
    """Sizing-channel multiplier, used ONLY when OPTIONS_STATS_SIZING is armed.

    Deliberately one-sided and small: the study supports "spike -> larger
    forward move", it does NOT support "quiet -> avoid", so a quiet name gets
    1.0 rather than a penalty. Cap mirrors the sentiment channel (1.25).
    """
    if not settings.options_stats_sizing or not stats.elevated:
        return 1.0
    return min(settings.options_stats_max_mult, max(1.0, settings.options_stats_max_mult))


# ────────────────────────── unusual-flow text feed ───────────────────────────
# get_derivative_unusual returns a Chinese narrative blob, e.g.
#   "8.5 01:45，出现一笔买入看涨期权交易，成交量为2023张，未平仓数为3950张，
#    V/OI值为13.8，交易金额为404668USD，合约行权价是170，到期日为2026/08/07。"
# Only the last 7 calendar days are served and there is NO history, so this
# CANNOT be backtested. It is exposed read-only for the owner to eyeball; it is
# wired to nothing that places orders, and it must stay that way until enough
# forward samples exist to test it.

_UNUSUAL_RE = re.compile(
    r"(?P<when>\d+\.\d+\s+\d+:\d+)[^出]*出现一笔(?P<side>买入看涨|卖出看涨|买入看跌|卖出看跌)"
    r"期权交易[^0-9]*成交量为(?P<vol>[\d,]+)张[^0-9]*未平仓数为(?P<oi>[\d,]+)张"
    r"[^0-9]*V/OI值为(?P<voi>[\d.]+)[^0-9]*交易金额为(?P<usd>[\d,]+)USD"
    r"[^0-9]*行权价是(?P<strike>[\d.]+)[^0-9]*到期日为(?P<expiry>[\d/]+)"
)

_SIDE_EN = {"买入看涨": "buy_call", "卖出看涨": "sell_call",
            "买入看跌": "buy_put", "卖出看跌": "sell_put"}


def _num(s: str) -> float:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def parse_unusual(content: str) -> list[dict]:
    """Parse the broker's narrative blob into structured trades. Unparseable
    lines are dropped rather than guessed at — a half-read strike is worse than
    a missing one."""
    out: list[dict] = []
    for m in _UNUSUAL_RE.finditer(content or ""):
        side = m.group("side")
        out.append({
            "when": m.group("when"),
            "side": _SIDE_EN.get(side, side),
            "side_zh": side,
            "is_open": _num(m.group("voi")) >= 1.0,   # V/OI>=1 => new position
            "volume": _num(m.group("vol")),
            "open_interest": _num(m.group("oi")),
            "vol_oi": _num(m.group("voi")),
            "notional_usd": _num(m.group("usd")),
            "strike": _num(m.group("strike")),
            "expiry": m.group("expiry").replace("/", "-"),
        })
    return out


def unusual_flow(symbol: str, client, timeout_s: float = 12.0) -> dict:
    """READ-ONLY large-option-trade feed for `symbol` (last ~7 days).

    Returns {"ok", "detail", "trades": [...], "summary": {...}}. NEVER call this
    from an order path — there is no history behind it, so nothing it says has
    been validated. Bounded by a worker-thread timeout so a wedged OpenD cannot
    stall the caller. Never raises.
    """
    import concurrent.futures

    def _do() -> dict:
        code = client._format_code(symbol)
        res = client.quote.get_derivative_unusual(code)
        ret, data = res[0], res[1]
        if ret != RET_OK:
            return {"ok": False, "detail": str(data)[:160], "trades": [], "summary": {}}
        content = ""
        if isinstance(data, dict):
            content = str(data.get("content", ""))
            window = str(data.get("time_range", ""))
        else:
            content, window = str(data), ""
        trades = parse_unusual(content)
        opens = [t for t in trades if t["is_open"]]
        summary = {
            "window": window,
            "n_trades": len(trades),
            "n_opening": len(opens),
            # Only OPENING trades (V/OI >= 1) count toward the directional tally.
            # A large print against a large existing OI is as likely a close or a
            # spread leg as a fresh bet, and counting it inflates both sides.
            "buy_call_usd": sum(t["notional_usd"] for t in opens if t["side"] == "buy_call"),
            "buy_put_usd": sum(t["notional_usd"] for t in opens if t["side"] == "buy_put"),
            "sell_call_usd": sum(t["notional_usd"] for t in opens if t["side"] == "sell_call"),
            "sell_put_usd": sum(t["notional_usd"] for t in opens if t["side"] == "sell_put"),
        }
        return {"ok": True, "detail": f"{len(trades)} large trades ({window})",
                "trades": trades, "summary": summary}

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_do).result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        return {"ok": False, "detail": f"timeout (>{timeout_s:.0f}s)", "trades": [], "summary": {}}
    except Exception as e:
        return {"ok": False, "detail": f"unusual-flow error ({e})", "trades": [], "summary": {}}
    finally:
        ex.shutdown(wait=False)


def overview(symbol: str, client) -> dict:
    """Live IV / iv_rank / iv_percentile / HV for `symbol`. Read-only context —
    nothing in the live path decides on it. Never raises."""
    try:
        res = client.quote.get_option_underlying_overview([client._format_code(symbol)])
        ret, data = res[0], res[1]
        if ret != RET_OK or data is None or not len(data):
            return {"ok": False, "detail": str(data)[:140]}
        row = data.iloc[0]
        keep = ("call_volume", "put_volume", "call_open_interest", "put_open_interest",
                "iv", "iv_rank", "iv_percentile", "hv_30d")
        out = {k: (float(row[k]) if k in data.columns and row[k] == row[k] else None)
               for k in keep}
        out.update(ok=True, detail="ok")
        return out
    except Exception as e:
        return {"ok": False, "detail": f"overview error ({e})"}


def probe(symbol: str, client) -> tuple[bool, str]:
    """Health probe (bypasses the enabled flag): can we reach the aggregate
    endpoints at all? Used by health_check to catch a silently lapsed feed."""
    df = fetch_history(symbol, client, days=120)
    if df is None:
        return False, "get_option_underlying_his_statistic returned no data"
    if len(df) < MIN_ROWS:
        return False, f"only {len(df)} rows (need {MIN_ROWS})"
    return True, f"ok — {len(df)} daily rows through {str(df['time'].iloc[-1])[:10]}"
