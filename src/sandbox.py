#!/usr/bin/env python3
"""
Sandbox v3 — historical replay using the SAME code path as live trading.

Every gate, every filter, every module imported directly from src/.
The ONLY differences from live:
  1. SimClock replaces real-time clock
  2. SimFeed returns no-lookahead historical klines (same OpenD source)
  3. SimBroker simulates fills (limit model against next-bar OHLC)
  4. AI news/veto/sentiment in ADVISORY mode — annotates trades, never blocks
     Results include ai_filtered_pnl so you can measure AI's value-add.

Dynamic universe: when --universe dynamic, selects Top-N point-in-time
every Monday using universe.select_universe() against history before that moment.

Usage:
  python3 src/sandbox.py --quick                          # last 30 days
  python3 src/sandbox.py --from 2026-01-01 --to 2026-06-30
  python3 src/sandbox.py --from 2026-01-01 --to 2026-06-30 --universe dynamic
  python3 src/sandbox.py --quick --output data/my.json
"""

import json, os, sys, time as _time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ET = ZoneInfo("America/New_York")

from src.config import ROOT, settings
from src import indicators, strategy_momentum, strategy_mr, strategy_pattern
from src import regime as regime_mod, breadth, kill_switch, sector, risk_manager, blacklist
from src import runtime_config
from src.moo_client import MooClient
from moomoo import KLType


# ── Config ────────────────────────────────────────────────

@dataclass
class SandboxConfig:
    start: datetime
    end: datetime
    tickers: list[str] = field(default_factory=list)
    universe_mode: str = "static"
    enable_ai: bool = False
    scan_interval_min: int = 30
    data_lookback_days: int = 120
    # Starting capital. 0 → resolved at run time to risk_manager.budget_usd()
    # (db-state budget override, else .env ACCOUNT_USD) — the same deployable
    # capital live sizing uses. Parity fix 2026-07-11: the frozen
    # settings.account_usd ignored the GUI/db budget override, so sandbox and
    # the Web backtest could silently replay two different account sizes.
    account_usd: float = 0.0


# ── SimClock ───────────────────────────────────────────────

class SimClock:
    def __init__(self, start: datetime, end: datetime):
        self._now = start
        self.end = end

    def ny_now(self) -> datetime:
        return self._now

    def advance(self, minutes: int = 30):
        self._now += timedelta(minutes=minutes)
        while self._now.weekday() >= 5:
            self._now += timedelta(days=1)
            self._now = self._now.replace(hour=9, minute=30)

    def in_trade_phase(self) -> bool:
        if self._now.weekday() >= 5:
            return False
        m = self._now.hour * 60 + self._now.minute
        return 9 * 60 + 45 <= m < 15 * 60 + 30

    def done(self) -> bool:
        return self._now >= self.end

    def date_str(self) -> str:
        return self._now.strftime("%Y-%m-%d")

    def is_monday_morning(self) -> bool:
        return self._now.weekday() == 0 and self._now.hour == 9 and self._now.minute == 45


# ── SimFeed ────────────────────────────────────────────────

def _tz_aware(df: pd.DataFrame) -> bool:
    try:
        return isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None
    except Exception:
        return False

class SimFeed:
    """Pre-fetched historical klines, clock-truncated for zero lookahead.
    First run fetches from OpenD → caches to data/sandbox_cache/ as parquet."""
    CACHE_DIR = ROOT / "data" / "sandbox_cache"

    def __init__(self, tickers: list[str], start: datetime, end: datetime,
                 clock: "SimClock", lookback_days: int = 120):
        self._hourly: dict[str, pd.DataFrame] = {}
        self._daily: dict[str, pd.DataFrame] = {}
        self._tickers = tickers
        self.clock = clock
        self._vix: pd.Series | None = None   # real ^VIX daily closes (shifted +1d)
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._fetch_all(start - timedelta(days=lookback_days), end)
        self._load_vix(start - timedelta(days=lookback_days), end)

    def _fetch_all(self, fetch_start: datetime, fetch_end: datetime):
        client = MooClient()
        print(f"  Fetching {len(self._tickers)} tickers + SPY …")
        all_syms = list(self._tickers) + ["SPY"]
        for i, sym in enumerate(all_syms):
            cache_h = self.CACHE_DIR / f"{sym}_60M.parquet"
            cache_d = self.CACHE_DIR / f"{sym}_DAY.parquet"
            tag = f"[{i+1}/{len(all_syms)}] {sym}"

            # Incremental cache (2026-07-07, owner request): the parquet cache
            # is PERMANENT — each run only TOPS UP the bars added since the
            # cache's newest bar and merges them in, so daily reruns fetch ~1
            # day of data per symbol instead of the whole window.
            #   fresh (≤3 trading-ish days behind) → use as-is
            #   ≤15 days behind → fetch just the gap, merge, rewrite cache
            #   older / missing / corrupt → full refetch (fallback)
            def _staleness_days(df: pd.DataFrame) -> int:
                last = df.index.max()
                if getattr(last, "tzinfo", None) is not None:
                    last = last.tz_localize(None)
                end_naive = (fetch_end.replace(tzinfo=None)
                             if fetch_end.tzinfo else fetch_end)
                return (end_naive - last).days

            def _merge(cached: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
                out = pd.concat([cached, fresh])
                out = out[~out.index.duplicated(keep="last")].sort_index()
                return out

            def _load_or_topup(cache_path, ktype, full_bars: int, bpd: int
                               ) -> tuple[pd.DataFrame | None, str]:
                cached = None
                if cache_path.exists():
                    try:
                        cached = pd.read_parquet(cache_path)
                        if cached.empty:
                            cached = None
                    except Exception:
                        cached = None
                if cached is not None:
                    try:
                        stale = _staleness_days(cached)
                    except Exception:
                        stale = 999
                    if stale <= 3:
                        return cached, "cached"
                    if stale <= 15:
                        need = min(full_bars, (stale + 3) * bpd + 5)
                        fresh = client.get_kline(sym, bars=need, ktype=ktype)
                        if fresh is not None and not fresh.empty:
                            merged = _merge(cached, fresh).tail(full_bars * 2)
                            merged.to_parquet(cache_path)
                            return merged, f"topped-up +{stale}d"
                        return cached, "cached (top-up failed)"
                # full refetch
                fresh = client.get_kline(sym, bars=full_bars, ktype=ktype)
                if fresh is not None and not fresh.empty:
                    fresh = fresh.sort_index()
                    fresh.to_parquet(cache_path)
                    return fresh, "full fetch"
                return cached, "no data"

            try:
                df, how_h = _load_or_topup(cache_h, KLType.K_60M, 500, 7)
                if df is not None:
                    if _tz_aware(df):
                        df.index = df.index.tz_convert("US/Eastern")
                    else:
                        df.index = df.index.tz_localize("US/Eastern")
                    self._hourly[sym] = df
                    print(f"    {tag}: {len(df)}h bars ({how_h})")
                else:
                    print(f"    {tag}: no hourly data")
            except Exception as e:
                print(f"    {tag}: hourly ERROR — {e}")

            try:
                df_d, _how_d = _load_or_topup(cache_d, KLType.K_DAY, 250, 1)
                if df_d is not None:
                    if _tz_aware(df_d):
                        df_d.index = df_d.index.tz_convert("US/Eastern")
                    else:
                        df_d.index = df_d.index.tz_localize("US/Eastern")
                    self._daily[sym] = df_d
            except Exception:
                pass
            n_d = len(self._daily.get(sym, pd.DataFrame()))
            n_h = len(self._hourly.get(sym, pd.DataFrame()))
            print(f"    {tag}: {n_h}h, {n_d}d bars")
        print(f"  Done: {len(self._hourly)}/{len(all_syms)} tickers loaded")
        # close() (not just del): the broker SDK's quote context spawns
        # NON-DAEMON threads — a leaked context keeps the whole process alive
        # after main() returns (the 2026-07-07 optimizer hang at exit).
        try:
            client.close()
        except Exception:
            pass

    def get_kline(self, symbol: str, bars: int = 120, ktype=None):
        """Pure clock-driven truncation — no _idx, no advance_all."""
        sim_now = self.clock.ny_now()
        source = self._daily if (ktype is not None and 'DAY' in str(ktype)) else self._hourly
        df = source.get(symbol)
        if df is None or df.empty:
            return None
        idx_aware = _tz_aware(df)
        if sim_now.tzinfo and not idx_aware:
            cutoff = sim_now.replace(tzinfo=None)
        elif not sim_now.tzinfo and idx_aware:
            cutoff = sim_now.replace(tzinfo=getattr(df.index, 'tz', None))
        else:
            cutoff = sim_now
        df = df[df.index <= cutoff]
        return df.tail(bars) if not df.empty else None

    def _load_vix(self, fetch_start: datetime, fetch_end: datetime):
        """Real ^VIX daily closes via yfinance, parquet-cached like the bars.

        Parity 2026-07-11: the fast engines (backtest.prefetch_data) read REAL
        VIX history; the sandbox used a SPY-ATR×15 proxy that is structurally
        different (wrong level AND wrong dynamics), so the VIX size-halving and
        regime layers fired on noise — polluting any sandbox-vs-backtest diff.
        Same series + same 1-day forward shift as prefetch_data (a bar reads
        YESTERDAY's close — no lookahead). On any failure self._vix stays None
        and get_vix() falls back to the old proxy, so a dead network or missing
        yfinance can't kill a replay."""
        cache_v = self.CACHE_DIR / "VIX_DAY.parquet"
        end_naive = fetch_end.replace(tzinfo=None) if fetch_end.tzinfo else fetch_end
        df = None
        if cache_v.exists():
            try:
                cached = pd.read_parquet(cache_v)
                if not cached.empty and (end_naive - cached.index.max()).days <= 3 \
                        and cached.index.min() <= pd.Timestamp(fetch_start.date()) + pd.Timedelta(days=7):
                    df = cached
            except Exception:
                df = None
        if df is None:
            try:
                import yfinance as yf
                start_naive = (fetch_start.replace(tzinfo=None)
                               if fetch_start.tzinfo else fetch_start)
                raw = yf.download("^VIX", start=(start_naive - timedelta(days=10)).date(),
                                  end=(end_naive + timedelta(days=1)).date(),
                                  interval="1d", progress=False, auto_adjust=True)
                if raw is not None and not raw.empty:
                    if isinstance(raw.columns, pd.MultiIndex):
                        raw.columns = [c[0] for c in raw.columns]
                    df = pd.DataFrame({"vix": raw["Close"].astype(float)})
                    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
                    df.to_parquet(cache_v)
            except Exception as e:
                print(f"  VIX history fetch failed: {e} — falling back to SPY-ATR proxy")
        if df is not None and not df.empty:
            s = df["vix"].copy()
            # Shift forward 1 day → sim date D reads the close of D-1.
            s.index = pd.to_datetime(s.index).normalize() + pd.Timedelta(days=1)
            self._vix = s.sort_index()
            print(f"  VIX history: {len(s)} real daily closes "
                  f"({s.index.min().date()} → {s.index.max().date()})")

    @property
    def vix_is_real(self) -> bool:
        return self._vix is not None and not self._vix.empty

    def get_vix(self) -> float:
        if self.vix_is_real:
            cutoff = pd.Timestamp(self.clock.ny_now().date())
            s = self._vix[self._vix.index <= cutoff]
            if not s.empty:
                return round(float(s.iloc[-1]), 1)
        return self._vix_proxy()

    def _vix_proxy(self) -> float:
        """Legacy fallback only: SPY ATR×15 — structurally NOT real VIX."""
        df = self._daily.get("SPY")
        if df is None or len(df) < 20:
            return 15.0
        sim_now = self.clock.ny_now()
        idx_aware = _tz_aware(df)
        if sim_now.tzinfo and not idx_aware:
            cutoff = sim_now.replace(tzinfo=None)
        elif not sim_now.tzinfo and idx_aware:
            cutoff = sim_now.replace(tzinfo=getattr(df.index, 'tz', None))
        else:
            cutoff = sim_now
        recent = df[df.index <= cutoff].tail(20)
        if len(recent) < 5:
            return 15.0
        daily_range = (recent["high"] - recent["low"]) / recent["close"] * 100
        return round(float(daily_range.mean() * 15.0), 1)

    def daily_dict(self) -> dict[str, pd.DataFrame | None]:
        return {sym: self._daily.get(sym) for sym in self._tickers}


def _business_days(start: datetime, end: datetime) -> int:
    """Weekday count between start (exclusive) and end (inclusive) — the same
    hold-age measure the live executor uses (minus NYSE holidays, which over a
    30-60d replay shift at most one day)."""
    if end <= start:
        return 0
    days, d, last = 0, start.date(), end.date()
    while d < last:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


# ── Transaction cost model (broker US rates) ────────────
_COMMISSION_PER_SHARE = 0.0049    # $0.0049/share
_COMMISSION_MIN = 0.99            # min $0.99 per side
_SLIPPAGE_BPS = 5.0               # 5 bps = 0.05% one-way

def _trade_cost(qty: int, entry_px: float, exit_px: float) -> tuple[float, float, float]:
    """Return (total_commission, total_slippage, total_cost) for a round-trip."""
    comm_entry = max(_COMMISSION_MIN, _COMMISSION_PER_SHARE * qty)
    comm_exit = max(_COMMISSION_MIN, _COMMISSION_PER_SHARE * qty)
    total_comm = comm_entry + comm_exit
    slip_entry = entry_px * qty * _SLIPPAGE_BPS / 10000.0
    slip_exit = exit_px * qty * _SLIPPAGE_BPS / 10000.0
    total_slip = slip_entry + slip_exit
    total_cost = round(total_comm + total_slip, 2)
    return round(total_comm, 2), round(total_slip, 2), total_cost


# ── SimBroker ──────────────────────────────────────────────

@dataclass
class SimPos:
    symbol: str; entry: float; stop: float; tp: float; qty: int
    entry_time: datetime; strategy: str
    ai_score: float = 0.0; ai_verdict: str = ""
    high_w: float = 0.0; low_w: float = 999999.0
    def __post_init__(self):
        if self.high_w == 0.0: self.high_w = self.entry
        if self.low_w == 999999.0: self.low_w = self.entry

@dataclass
class SimTrade:
    symbol: str; entry: float; exit: float; qty: int
    pnl: float; pnl_pct: float; r: float; reason: str; strategy: str
    entry_t: datetime; exit_t: datetime
    ai_score: float = 0.0; ai_verdict: str = ""
    commission: float = 0.0; slippage: float = 0.0; net_pnl: float = 0.0
    mfe: float = 0.0; mae: float = 0.0

class SimBroker:
    def __init__(self, feed: SimFeed, clock: SimClock, cfg: SandboxConfig):
        self.feed = feed; self.clock = clock; self.cfg = cfg
        self.positions: dict[str, SimPos] = {}
        self.trades: list[SimTrade] = []
        # cfg.account_usd is resolved by run_sandbox (risk_manager.budget_usd());
        # the settings fallback only covers a directly-constructed broker.
        self.cash = cfg.account_usd or settings.account_usd
        self._pending: list[dict] = []
        self.last_sl_time: dict[str, datetime] = {}  # SL cooldown tracker

    def open_position(self, symbol: str, entry_px: float, stop_px: float,
                      tp_px: float, qty: int, strategy: str = "trend",
                      ai_score: float = 0.0, ai_verdict: str = "") -> bool:
        self._pending.append(dict(sym=symbol, limit=entry_px, stop=stop_px,
                                  tp=tp_px, qty=qty, strat=strategy,
                                  ai_score=ai_score, ai_verdict=ai_verdict))
        return True

    def _latest(self, sym: str):
        k = self.feed.get_kline(sym, bars=1)
        return k.iloc[-1] if k is not None and not k.empty else None

    def process_bar(self):
        keep = []
        for o in self._pending:
            bar = self._latest(o["sym"])
            if bar is None: keep.append(o); continue
            lo = float(bar["low"] if "low" in bar.index else bar.low)
            if lo <= o["limit"]:
                # Deduct entry commission + slippage from cash immediately
                q = o["qty"]; ep = o["limit"]
                comm_entry = max(_COMMISSION_MIN, _COMMISSION_PER_SHARE * q)
                slip_entry = ep * q * _SLIPPAGE_BPS / 10000.0
                self.positions[o["sym"]] = SimPos(
                    o["sym"], ep, o["stop"], o["tp"], q,
                    self.clock.ny_now(), o["strat"],
                    o.get("ai_score", 0.0), o.get("ai_verdict", ""),
                    ep, ep)
                self.cash -= (ep * q + comm_entry + slip_entry)
        self._pending = keep

        for sym, pos in list(self.positions.items()):
            bar = self._latest(sym)
            if bar is None: continue
            hi = float(bar["high"] if "high" in bar.index else bar.high)
            lo = float(bar["low"] if "low" in bar.index else bar.low)
            cl = float(bar["close"] if "close" in bar.index else bar.close)
            op = float(bar["open"] if "open" in bar.index else bar.open)
            pos.high_w = max(pos.high_w, hi)
            pos.low_w = min(pos.low_w, lo)

            exit_px = reason = None
            if lo <= pos.stop:
                exit_px = min(op, pos.stop); reason = "SL"
            elif hi >= pos.tp:
                exit_px = pos.tp; reason = "TP"
            elif _business_days(pos.entry_time, self.clock.ny_now()) >= runtime_config.max_hold_days():
                # Parity 2026-07-07: live counts BUSINESS days against the
                # runtime-effective max_hold_days (executor._business_days_between
                # + runtime_config) — calendar days closed winners ~2 days early.
                exit_px = cl; reason = "MAX_HOLD"

            if exit_px is not None:
                gross = (exit_px - pos.entry) * pos.qty
                comm, slip, tc = _trade_cost(pos.qty, pos.entry, exit_px)
                net = round(gross - tc, 2)
                pnl_pct = (exit_px - pos.entry) / pos.entry * 100
                r_unit = pos.entry - pos.stop
                r_val = (exit_px - pos.entry) / r_unit if r_unit > 0 else 0.0
                self.trades.append(SimTrade(
                    sym, pos.entry, exit_px, pos.qty, round(gross, 2), pnl_pct, r_val,
                    reason, pos.strategy, pos.entry_time, self.clock.ny_now(),
                    pos.ai_score, pos.ai_verdict,
                    comm, slip, net,
                    (pos.high_w - pos.entry) / pos.entry * 100,
                    (pos.low_w - pos.entry) / pos.entry * 100))
                # Add exit proceeds, then deduct exit costs
                self.cash += exit_px * pos.qty
                comm_exit = max(_COMMISSION_MIN, _COMMISSION_PER_SHARE * pos.qty)
                slip_exit = exit_px * pos.qty * _SLIPPAGE_BPS / 10000.0
                self.cash -= (comm_exit + slip_exit)
                # SL cooldown tracking
                if reason == "SL":
                    self.last_sl_time[sym] = self.clock.ny_now()
                del self.positions[sym]

    def held_symbols(self) -> set:
        return set(self.positions.keys())


# ── Universe ───────────────────────────────────────────────

def _load_universe(config: SandboxConfig, feed: SimFeed | None = None) -> list[str]:
    if config.universe_mode == "dynamic":
        try:
            from src.universe import Affordability, load_pool, select_universe
            pool = load_pool()
            if feed is not None:
                daily = feed.daily_dict()
                asof = config.start.date()  # point-in-time for historical replay
                tickers = select_universe(daily, asof=asof,
                                          top_n=runtime_config.universe_top_n(),
                                          afford=Affordability.live())
                print(f"  Universe (dynamic): {len(tickers)} tickers from pool of {len(pool)}")
                return tickers
        except Exception as e:
            print(f"  Universe (dynamic) failed: {e} — falling back to watchlist")
    wf = ROOT / "config" / "watchlist.json"
    if wf.exists():
        return json.loads(wf.read_text()).get("tickers", [])
    return ["AAPL", "MSFT", "NVDA", "AMD", "GOOGL"]


def _refresh_universe(config: SandboxConfig, feed: SimFeed) -> list[str]:
    """Point-in-time dynamic universe selection."""
    from src.universe import Affordability, load_pool, select_universe
    pool = load_pool()
    today_ts = feed.clock.ny_now()
    today_date = today_ts.date()
    daily = feed.daily_dict()
    daily_by_sym = {sym: feed._daily.get(sym) for sym in pool}
    return select_universe(daily_by_sym, asof=today_date,
                           top_n=runtime_config.universe_top_n(),
                           afford=Affordability.live())


# ── Regime ─────────────────────────────────────────────────

def _assess_regime(feed: SimFeed) -> tuple:
    spy_daily = feed.get_kline("SPY", bars=250, ktype=KLType.K_DAY)
    if spy_daily is None or len(spy_daily) < 200:
        return regime_mod.Regime("NEUTRAL", 0, 0, 0, False, False, "no SPY data"), "NEUTRAL", 15.0
    vix = feed.get_vix()
    try:
        reg = regime_mod.assess(spy_daily, vix=vix)
    except Exception:
        reg = regime_mod.Regime("NEUTRAL", 0, 0, 0, False, False, "assess failed")
    effective = reg.confirmed if settings.smart_regime_enabled else reg.label
    return reg, effective, vix


# ── GATE: MTF + Gap ────────────────────────────────────────

def _check_mtf_gap(feed: SimFeed, sig, skip_cb) -> bool:
    sym = sig.symbol
    df_d = feed.get_kline(sym, bars=60, ktype=KLType.K_DAY)
    if df_d is None or df_d.empty:
        return True
    if settings.timeframe == "HOUR_1":
        daily_ok, daily_reason = indicators.daily_trend_bullish(df_d)
        if not daily_ok:
            skip_cb("mtf", daily_reason); return False
    strat = getattr(sig, 'strategy', 'trend')
    block_up = strat not in ('trend', 'momentum_break')
    gap_ok, gap_reason = indicators.check_gap(
        df_d, max_gap_pct=settings.max_gap_pct, block_up_gaps=block_up)
    if not gap_ok:
        skip_cb("gap", gap_reason); return False
    return True


# ── GATE: Earnings (historical, no lookahead) ────────────

_EARNINGS_AVOID_DAYS = 2

def _preload_earnings_calendar(tickers: list[str]) -> dict[str, list[str]]:
    """Pre-fetch historical earnings dates for all tickers in batch mode."""
    from src.earnings import load_earnings_calendar
    try:
        return load_earnings_calendar(tickers)
    except Exception:
        return {t.upper(): [] for t in tickers}

def _check_historical_earnings(symbol: str, sim_now: datetime,
                                earnings_cal: dict[str, list[str]]) -> tuple[bool, str]:
    """Check if earnings within EARNINGS_AVOID_DAYS of sim_now. No lookahead —
    only uses earnings dates relative to simulated time, not date.today()."""
    dates = earnings_cal.get(symbol.upper(), [])
    if not dates:
        return False, "no earnings history"
    sim_date = sim_now.date()
    future_dates = []
    for d in dates:
        try:
            dt = date.fromisoformat(d)
            if dt >= sim_date:
                future_dates.append(dt)
        except ValueError:
            continue
    if not future_dates:
        return False, "no future earnings found in history"
    next_earn = min(future_dates)
    days_until = (next_earn - sim_date).days
    if 0 <= days_until <= _EARNINGS_AVOID_DAYS:
        return True, f"earnings in {days_until}d ({next_earn})"
    return False, f"next earnings {next_earn} ({days_until}d away)"


# ── GATE: Spread (simulated historical proxy) ───────────

_SPREAD_MAX_PCT = 0.5

def _check_spread(symbol: str, feed: SimFeed) -> float:
    """Simulate spread from recent daily range as liquidity proxy.
    Returns spread as percentage. """
    df_d = feed.get_kline(symbol, bars=20, ktype=KLType.K_DAY)
    if df_d is None or df_d.empty:
        return 0.08  # conservative default for liquid names
    try:
        hl_pct = ((df_d["high"] - df_d["low"]) / df_d["close"] * 100).mean()
        # Map daily range to spread: wider range → wider spread
        return round(max(0.03, min(0.5, float(hl_pct) * 0.05)), 2)
    except Exception:
        return 0.08


# ── GATE: Cooldown (SL-triggered temporary exclusion) ───

_COOLDOWN_MINUTES = 30

def _is_cooldown_active(symbol: str, broker, sim_now: datetime) -> bool:
    """Check if this symbol was SL'd within the last 30 minutes."""
    last_sl = broker.last_sl_time.get(symbol)
    if last_sl is not None:
        return (sim_now - last_sl) < timedelta(minutes=_COOLDOWN_MINUTES)
    return False


# ── AI Annotation (skipped for historical parity) ─────────
# AI news/veto/sentiment SKIPPED — prevents lookahead bias from LLM training
# data (Gemini/DeepSeek "knows" the future). ai_score/ai_verdict fields remain
# in SimTrade as placeholders for future offline batch annotation with
# a cutoff-date-locked model.


# ── Main Loop ─────────────────────────────────────────────

def run_sandbox(config: SandboxConfig) -> dict:
    t0 = _time.time()

    # 0. Resolve starting capital to the runtime-effective budget (db-state
    # override beats frozen .env) — the same base live sizing/slot math uses.
    if config.account_usd <= 0:
        config.account_usd = risk_manager.budget_usd()

    # 1. Initial universe
    tickers = _load_universe(config)
    if not tickers:
        tickers = config.tickers or ["AAPL", "MSFT", "NVDA", "AMD", "GOOGL"]

    # For dynamic mode, also pre-fetch the whole pool
    if config.universe_mode == "dynamic":
        try:
            from src.universe import load_pool
            pool_tickers = list(set(load_pool() + tickers))
        except Exception:
            pool_tickers = tickers
    else:
        pool_tickers = tickers

    print(f"Sandbox v3: {config.start.date()} → {config.end.date()}")
    print(f"Universe: {config.universe_mode} | AI: {'ADVISORY' if config.enable_ai else 'OFF'}")
    print(f"Tickers: {len(pool_tickers)} (scan top {runtime_config.universe_top_n()})")
    print(f"Params: THR={runtime_config.entry_threshold()} SL={runtime_config.sl_atr_mult()} "
          f"TP={runtime_config.tp_atr_mult()} | ${config.account_usd:,.0f}")
    print(f"{'='*60}")

    # 2. Init infrastructure
    clock = SimClock(config.start, config.end)
    feed = SimFeed(pool_tickers, config.start, config.end, clock, config.data_lookback_days)
    broker = SimBroker(feed, clock, config)

    # Dynamic universe — select initial Top-N
    if config.universe_mode == "dynamic":
        try:
            tickers = _refresh_universe(config, feed)
            print(f"  Initial universe: {len(tickers)} tickers")
        except Exception as e:
            print(f"  Initial dynamic universe failed: {e} — using static")

    scans = eval_scans = signals_found = 0
    skip_counts = defaultdict(int)
    # Per-signal skip log so scripts/sandbox_vs_backtest.py can attribute a
    # trade missing on the sandbox side to the exact gate that blocked it.
    # Bounded (owner concern: no unbounded growing artifacts).
    _SKIP_EVENTS_MAX = 10_000
    skip_events: list[dict] = []
    max_seen = 0.0
    week_refreshes = 0
    # Parity 2026-07-07: live derives the slot cap from capital
    # (risk_manager.max_positions → derive_max_positions); the static
    # settings.max_positions ignored capital scaling. 2026-07-11: derive from
    # the RESOLVED budget, not frozen .env — mirrors risk_manager.max_positions().
    from src.config import derive_max_positions
    _max_positions_cap = derive_max_positions(config.account_usd)

    # ── Preload historical earnings calendar (no lookahead) ──
    print(f"  Pre-loading earnings calendar …")
    earnings_cal = _preload_earnings_calendar(pool_tickers + tickers)
    print(f"  Earnings calendar: {sum(1 for v in earnings_cal.values() if v)} symbols")

    # ── Preload blacklist (current snapshot, advisory) ──
    _bl_active = blacklist.get_blacklist()
    if _bl_active:
        print(f"  Blacklist: {len(_bl_active)} symbols ({', '.join(list(_bl_active)[:10])})")

    # 3. Main loop
    while not clock.done():
        clock.advance(config.scan_interval_min)
        now = clock.ny_now()

        # Dynamic universe refresh every Monday morning
        if config.universe_mode == "dynamic" and clock.is_monday_morning():
            try:
                new_univ = _refresh_universe(config, feed)
                if new_univ:
                    tickers = new_univ
                    week_refreshes += 1
            except Exception:
                pass

        if not clock.in_trade_phase():
            broker.process_bar()
            continue

        eval_scans += 1
        scans += 1

        # Regime + VIX
        regime, effective_label, vix = _assess_regime(feed)

        # ── Breadth (sandbox-native, zero OpenD dependency) ──
        # breadth.assess(client, vix) requires the SDK OpenQuoteContext.request_history_kline.
        # SimFeed provides get_kline(sym, bars, ktype) — a different interface.
        # Solution: replicate the breadth logic directly against SimFeed SPY dailies.
        # VIX overlay 2026-07-11: SimFeed now carries REAL ^VIX history, so the
        # live VIX_PANIC check applies too. Skipped when the feed fell back to
        # the SPY-ATR proxy (structurally different — would false-trigger).
        try:
            spy_50 = feed.get_kline("SPY", bars=55, ktype=KLType.K_DAY)
            if spy_50 is not None and len(spy_50) >= 50:
                close_ser = pd.Series(spy_50["close"])
                open_ser = pd.Series(spy_50["open"])
                sma50 = float(close_ser.rolling(50).mean().iloc[-1])
                price = float(close_ser.iloc[-1])
                n = len(close_ser)
                days_up = sum(
                    1 for i in range(-10, 0)
                    if i + n >= 0 and close_ser.iloc[i] > open_ser.iloc[i]
                )
                ad_ratio = days_up / 10
                vix_ok = (vix < breadth.VIX_PANIC) if (feed.vix_is_real and vix > 0) else True
                breadth_ok = (ad_ratio >= 0.55) and ((price / sma50 - 1) * 100 > -10) and vix_ok
                breadth_note = (f"AD={ad_ratio:.2f} SPYvs50MA={(price/sma50-1)*100:.1f}%"
                                + ("" if vix_ok else f" VIX {vix:.0f}≥{breadth.VIX_PANIC}"))
            else:
                breadth_ok, breadth_note = True, "insufficient SPY bars — passing"
        except Exception as e:
            breadth_ok, breadth_note = True, f"breadth calc failed: {e} — passing"

        # Kill switch
        if effective_label == "BEAR":
            broker.process_bar(); continue
        # Breadth PARITY FIX (2026-07-07): live treats unhealthy breadth as
        # ADVISORY unless BREADTH_BLOCKING=true (main.py) — the sandbox was
        # hard-blocking regardless, which suppressed ~95% of scans in testing
        # and made every optimizer sweep measure a strategy live doesn't run.
        if not breadth_ok:
            skip_counts["breadth"] += 1
            if settings.breadth_blocking:
                broker.process_bar(); continue
        if now.weekday() == 4 and now.hour * 60 + now.minute >= 14 * 60:
            broker.process_bar(); continue

        # Adaptive threshold
        entry_thr = runtime_config.entry_threshold()
        base_thr = entry_thr
        if effective_label == "BULL":
            base_thr = max(55, entry_thr - 5)
        elif effective_label == "NEUTRAL":
            base_thr = min(85, entry_thr + 5)
        threshold_floor = base_thr

        # Score candidates (same 4 strategies as live)
        ranked = []
        held = broker.held_symbols()
        late_cutoff = (now.hour * 60 + now.minute) >= 14 * 60

        for sym in tickers:
            df = feed.get_kline(sym, bars=120)
            if df is None or df.empty or len(df) < 20:
                continue
            if len(df) >= 2:
                df = df.iloc[:-1]  # drop forming bar (parity with main.py)

            try:
                s = indicators.evaluate(sym, df)
                max_seen = max(max_seen, s.score)
                if s.score >= threshold_floor: ranked.append(s)
            except Exception: pass
            try:
                s = strategy_momentum.evaluate(sym, df)
                max_seen = max(max_seen, s.score)
                if s.score >= threshold_floor: ranked.append(s)
            except Exception: pass
            if settings.mr_enabled:
                try:
                    s = strategy_mr.evaluate(sym, df)
                    if s.score >= threshold_floor: ranked.append(s)
                except Exception: pass
            if settings.pattern_enabled:
                try:
                    s = strategy_pattern.evaluate(sym, df)
                    if s.score >= threshold_floor: ranked.append(s)
                except Exception: pass

        ranked.sort(key=lambda s: s.score, reverse=True)
        signals_found += len(ranked)

        # Candidate funnel
        new_names = 0
        new_limit = settings.max_new_names_per_scan

        def _skip(gate, reason):
            skip_counts[gate] += 1
            try:
                _sym = sig.symbol  # current signal in the funnel loop below
            except NameError:
                _sym = None
            if len(skip_events) < _SKIP_EVENTS_MAX:
                skip_events.append({"t": now.isoformat(), "symbol": _sym,
                                    "gate": gate, "reason": reason})

        for sig in ranked:
            if sig.score < threshold_floor:
                break
            is_stack = sig.symbol in held
            if not is_stack and new_names >= new_limit:
                continue
            if late_cutoff and not is_stack and sig.score < min(88, threshold_floor + 8):
                _skip("late_entry", ""); continue
            if is_stack and sig.score < entry_thr:
                continue
            if not is_stack and len(held) >= _max_positions_cap:
                _skip("max_positions", ""); continue
            if sig.symbol in broker.positions:
                continue

            # MTF + Gap
            if not _check_mtf_gap(feed, sig, _skip):
                continue

            # Cooldown (SL-triggered temporary exclusion)
            if _is_cooldown_active(sig.symbol, broker, now):
                _skip("cooldown", f"SL'd within {_COOLDOWN_MINUTES}min"); continue

            # Blacklist (current snapshot, advisory — point-in-time not feasible)
            if sig.symbol in _bl_active:
                _skip("blacklist", "on adaptive blacklist"); continue

            # Earnings (historical, no lookahead)
            earn_blocked, earn_reason = _check_historical_earnings(sig.symbol, now, earnings_cal)
            if earn_blocked:
                _skip("earnings", earn_reason); continue

            # Spread (simulated liquidity proxy)
            spread_pct = _check_spread(sig.symbol, feed)
            if spread_pct > _SPREAD_MAX_PCT:
                _skip("spread", f"bid-ask {spread_pct:.2f}% > {_SPREAD_MAX_PCT}%"); continue

            # Sector — build mock positions_df matching broker format
            pos_data = []
            for p in broker.positions.values():
                code = f"US.{p.symbol}" if not p.symbol.startswith("US.") else p.symbol
                pos_data.append({"code": code, "qty": p.qty, "position_side": "LONG"})
            positions_df = (pd.DataFrame(pos_data) if pos_data
                           else pd.DataFrame(columns=["code", "qty", "position_side"]))
            pending_syms = {o["sym"] for o in broker._pending}

            try:
                ok, reason = sector.check_sector_exposure(sig.symbol, positions_df, pending_syms)
                if not ok: _skip("sector", reason); continue
            except Exception: pass

            # AI annotation — skipped: historical news feeds would inject
            # lookahead bias from LLM training data (Gemini "knows" the future).
            # ai_score/ai_verdict fields remain in SimTrade for future opt-in
            # (e.g. offline batch-annotate with a cutoff-date-locked model).
            ai_score, ai_verdict = 0.0, "skipped_historical"

            # Size calculation — use this signal's own kline, not stale df
            sig_df = feed.get_kline(sig.symbol, bars=120)
            if sig_df is None or sig_df.empty:
                continue

            # Sizing PARITY (2026-07-07): mirror risk_manager.calc_position_size —
            # runtime risk_per_trade (was static .env), VIX halving layers, and
            # qty=0 → skip (the old max(1, …) forced a share even when the risk
            # math said none, overstating small-account activity).
            atr_val = float(getattr(sig, 'atr', sig_df["close"].iloc[-1] * 0.02))
            entry_px = float(getattr(sig, 'price', None) or sig_df["close"].iloc[-1])
            stop_px = float(getattr(sig, 'stop_loss', None)
                            or (entry_px - runtime_config.sl_atr_mult() * atr_val))
            tp_px = float(getattr(sig, 'take_profit', None)
                          or (entry_px + runtime_config.tp_atr_mult() * atr_val))
            if entry_px <= 0 or stop_px <= 0:
                continue
            stop_distance = entry_px - stop_px
            if stop_distance <= 0:
                continue
            equity = broker.cash + sum(p.entry * p.qty for p in broker.positions.values())
            qty_by_risk = int(equity * runtime_config.risk_per_trade() / stop_distance)
            qty_by_cap = int(equity * runtime_config.max_position_pct() / entry_px)
            qty = max(0, min(qty_by_risk, qty_by_cap))
            if qty <= 0:
                _skip("qty_zero", ""); continue
            if vix > 35:
                qty = max(1, qty // 4)
            elif vix > 25:
                qty = max(1, qty // 2)

            strat = getattr(sig, 'strategy', 'trend')
            broker.open_position(sig.symbol, entry_px, stop_px, tp_px, qty, strat,
                                 ai_score, ai_verdict)
            if not is_stack:
                new_names += 1
                held.add(sig.symbol)

        broker.process_bar()

        if eval_scans % 10 == 0:
            print(f"  {clock.date_str()} {now:%H:%M} | scan {scans} (eval {eval_scans}) "
                  f"| max={max_seen:.0f} (floor={threshold_floor}) "
                  f"| pos={len(broker.positions)} trades={len(broker.trades)} "
                  f"| PnL=${sum(t.net_pnl for t in broker.trades):,.0f} | {effective_label}")

    # ── Results ──
    elapsed = _time.time() - t0
    closed = broker.trades
    wins = [t for t in closed if t.net_pnl > 0]
    losses = [t for t in closed if t.net_pnl <= 0]
    total_net = sum(t.net_pnl for t in closed)
    total_gross = sum(t.pnl for t in closed)
    total_comm = sum(t.commission for t in closed)
    total_slip = sum(t.slippage for t in closed)

    by_strat = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "net_pnl": 0.0})
    for t in closed:
        by_strat[t.strategy]["n"] += 1
        if t.net_pnl > 0: by_strat[t.strategy]["wins"] += 1
        by_strat[t.strategy]["pnl"] += t.pnl
        by_strat[t.strategy]["net_pnl"] += t.net_pnl

    # AI breakdown
    ai_trades = [t for t in closed if t.ai_score > 0]
    ai_bullish = [t for t in ai_trades if t.ai_verdict == "bullish"]
    ai_bearish = [t for t in ai_trades if t.ai_verdict == "bearish"]
    ai_neutral = [t for t in ai_trades if t.ai_verdict == "neutral"]

    result = {
        "version": "v3",
        "config": {
            "start": str(config.start.date()), "end": str(config.end.date()),
            "tickers_n": len(tickers),
            "universe": config.universe_mode,
            "week_refreshes": week_refreshes,
            "entry_threshold": runtime_config.entry_threshold(),   # 2026-07-06: use runtime override, not static .env
            "sl_atr_mult": runtime_config.sl_atr_mult(),
            "tp_atr_mult": runtime_config.tp_atr_mult(),
            "account_usd": config.account_usd,
            "ai_enabled": config.enable_ai,
        },
        "summary": {
            "total_trades": len(closed),
            "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(len(wins) / max(len(closed), 1) * 100, 1),
            "net_pnl_usd": round(total_net, 2),
            "gross_pnl_usd": round(total_gross, 2),
            "total_return_pct": round(total_net / config.account_usd * 100, 2),
            "avg_win_usd": round(sum(t.net_pnl for t in wins) / max(len(wins), 1), 2),
            "avg_loss_usd": round(sum(t.net_pnl for t in losses) / max(len(losses), 1), 2),
            "profit_factor": round(
                sum(t.net_pnl for t in wins) / max(abs(sum(t.net_pnl for t in losses)), 1), 2),
            "avg_r": round(sum(t.r for t in closed) / max(len(closed), 1), 2),
            "scans": scans, "eval_scans": eval_scans,
            "signals_found": signals_found,
            "max_score_seen": round(max_seen, 1),
            "elapsed_sec": round(elapsed, 1),
        },
        "ai_telemetry": {
            "trades_with_ai": len(ai_trades),
            "ai_bullish": len(ai_bullish),
            "ai_bearish": len(ai_bearish),
            "ai_neutral": len(ai_neutral),
            "pnl_ai_bullish": round(sum(t.net_pnl for t in ai_bullish), 2),
            "pnl_ai_bearish": round(sum(t.net_pnl for t in ai_bearish), 2),
            "pnl_ai_neutral": round(sum(t.net_pnl for t in ai_neutral), 2),
        },
        "cost_summary": {
            "total_commission": round(total_comm, 2),
            "total_slippage": round(total_slip, 2),
            "total_cost": round(total_comm + total_slip, 2),
            "cost_pct_of_gross": round((total_comm + total_slip) / max(abs(total_gross), 1) * 100, 1),
        },
        "by_strategy": {
            s: {"n": d["n"], "wins": d["wins"],
                "win_rate": round(d["wins"] / max(d["n"], 1) * 100, 1),
                "pnl": round(d["pnl"], 2), "net_pnl": round(d["net_pnl"], 2)}
            for s, d in by_strat.items()
        },
        "skip_reasons": dict(skip_counts),
        "skip_events": skip_events,
        "trades": [{
            "symbol": t.symbol, "entry": round(t.entry, 2), "exit": round(t.exit, 2),
            "entry_t": t.entry_t.isoformat(), "exit_t": t.exit_t.isoformat(),
            "qty": t.qty,
            "pnl": round(t.pnl, 2), "net_pnl": round(t.net_pnl, 2),
            "commission": round(t.commission, 2), "slippage": round(t.slippage, 2),
            "pnl_pct": round(t.pnl_pct, 2),
            "r": round(t.r, 2), "reason": t.reason, "strategy": t.strategy,
            "ai_score": round(t.ai_score, 1), "ai_verdict": t.ai_verdict,
            "mfe": round(t.mfe, 2), "mae": round(t.mae, 2),
        } for t in closed],
    }
    return result


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Sandbox v3 — live-gate historical replay")
    ap.add_argument("--from", dest="start", default=None, help="Start date YYYY-MM-DD")
    ap.add_argument("--to", dest="end", default=None, help="End date YYYY-MM-DD")
    ap.add_argument("--quick", action="store_true", help="Last 30 days, AI off, static")
    ap.add_argument("--ai", choices=["on", "off"], default="off")
    ap.add_argument("--universe", choices=["static", "dynamic"], default="static")
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    if args.quick:
        end = datetime.now(ET).replace(hour=16, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=30)
        ai = False
    else:
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=ET)
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=ET)
        ai = args.ai == "on"

    cfg = SandboxConfig(
        start=start, end=end,
        tickers=args.tickers or [],
        universe_mode=args.universe,
        enable_ai=ai,
    )
    result = run_sandbox(cfg)

    s = result["summary"]
    c = result.get("cost_summary", {})
    print(f"\n{'='*60}")
    print(f"SANDBOX v3 — {cfg.start.date()} → {cfg.end.date()}")
    print(f"  Trades: {s['total_trades']} | Wins: {s['wins']} ({s['win_rate_pct']}%)")
    print(f"  Gross PnL: ${s.get('gross_pnl_usd', 0):,.2f} | Net PnL: ${s['net_pnl_usd']:,.2f} ({s['total_return_pct']:+.1f}%)")
    if c:
        print(f"  Costs: Comm=${c['total_commission']:,.2f} | Slip=${c['total_slippage']:,.2f} | Total=${c['total_cost']:,.2f} ({c['cost_pct_of_gross']}% of gross)")
    print(f"  Avg Win: ${s['avg_win_usd']:,.0f} | Loss: ${s['avg_loss_usd']:,.0f}")
    print(f"  PF: {s['profit_factor']:.2f} | Avg R: {s['avg_r']:.2f}")
    print(f"  Scans: {s['scans']} (eval {s.get('eval_scans','?')}) | Signals: {s['signals_found']} | {s['elapsed_sec']}s")
    print(f"  Max score: {s.get('max_score_seen','?')}")
    if result["by_strategy"]:
        print(f"\nBy Strategy:")
        for strat, data in result["by_strategy"].items():
            print(f"  {strat}: {data['n']} trades, {data['win_rate']:.0f}% WR, PnL=${data['net_pnl']:,.0f}")
    if result["skip_reasons"]:
        print(f"\nSkip reasons: {result['skip_reasons']}")

    out_path = args.output or str(ROOT / "data" / "sandbox_result.json")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\nSaved to {out_path}")
