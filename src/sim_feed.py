"""The replay data layer: a simulated clock, and bars truncated to what had
CLOSED by it.

Lifted out of sandbox.py so the engine that replaces it does not have to import
the engine it replaces. Nothing here decides anything about a trade — it is the
part of the old sandbox that was always right, and the reason a replay can
claim to be free of look-ahead:

  • bars carry their START time, so a scan inside a forming bar must NOT see
    that bar's finished close (see get_kline — this was wrong until 2026-08-15
    and every sandbox result before that date was computed with up to an hour
    of future price)
  • real ^VIX history, shifted so a scan reads YESTERDAY's close
  • parquet cache, keyed per session, so an extended-hours experiment cannot
    poison a regular-hours replay
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from moomoo import KLType

from .config import ROOT, settings
from .moo_client import MooClient

ET = ZoneInfo("America/New_York")


# Session windows in ET, by bar START minute. Pre-market opens at 04:00 and
# after-hours runs to 20:00; the overnight session is everything else.
_SESSION_WINDOWS = {
    "RTH": (9 * 60 + 45, 15 * 60 + 30),
    "ETH": (4 * 60, 20 * 60),
    "ALL": (0, 24 * 60),
}


# The most hourly bars one replay will ask for. Four thousand is about four
# and a half years of regular hours — past anything backtested here, and a
# bound so a mis-specified window cannot ask the broker for everything.
_MAX_HOURLY_BARS = 4000


def _normalise_sessions(sessions: str) -> str:
    """An unrecognised session name means regular hours.

    Failing toward the narrow, validated session is the only safe direction: a
    typo that opened the overnight book would be a config error that starts
    trading a session nothing has been tested against.
    """
    return sessions if sessions in _SESSION_WINDOWS else "RTH"


class SimClock:
    def __init__(self, start: datetime, end: datetime, sessions: str = "RTH"):
        self._now = start
        self.end = end
        self.sessions = _normalise_sessions(sessions)

    def ny_now(self) -> datetime:
        return self._now

    def advance(self, minutes: int = 30):
        self._now += timedelta(minutes=minutes)
        while self._now.weekday() >= 5:
            self._now += timedelta(days=1)
            lo, _ = _SESSION_WINDOWS[self.sessions]
            self._now = self._now.replace(hour=lo // 60, minute=lo % 60)

    def in_trade_phase(self) -> bool:
        if self._now.weekday() >= 5:
            return False
        lo, hi = _SESSION_WINDOWS[self.sessions]
        m = self._now.hour * 60 + self._now.minute
        return lo <= m < hi

    def done(self) -> bool:
        return self._now >= self.end

    def date_str(self) -> str:
        return self._now.strftime("%Y-%m-%d")

    def is_monday_morning(self) -> bool:
        return self._now.weekday() == 0 and self._now.hour == 9 and self._now.minute == 45

    def is_universe_refresh(self) -> bool:
        """Fires when the LIVE refresh would fire — weekly on Monday, or every
        session when UNIVERSE_REFRESH_FREQ=daily. The sandbox exists to catch
        live/backtest drift, so this must track the live schedule rather than
        assume one."""
        from src.config import settings as _s
        if _s.universe_refresh_freq == "daily":
            return (self._now.weekday() < 5 and self._now.hour == 9
                    and self._now.minute == 45)
        return self.is_monday_morning()


# ── SimFeed ────────────────────────────────────────────────

def _tz_aware(df: pd.DataFrame) -> bool:
    try:
        return isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None
    except Exception:
        return False

class SimFeed:
    """Pre-fetched historical klines, truncated to bars that have CLOSED.
    First run fetches from OpenD → caches to data/sandbox_cache/ as parquet.

    "Clock-truncated for zero lookahead" is what this used to say and it was
    not true — see get_kline. Bars carry their START time, so truncating on
    `index <= now` handed over the bar currently forming, complete, with a
    close from the future.
    """
    CACHE_DIR = ROOT / "data" / "sandbox_cache"

    def __init__(self, tickers: list[str], start: datetime, end: datetime,
                 clock: "SimClock", lookback_days: int = 120,
                 sessions: str = "RTH"):
        # Cached PER SESSION. An RTH cache and an all-hours cache are different
        # series for the same symbol, and sharing one file would let an
        # extended-hours experiment quietly poison every regular-hours run that
        # came after it.
        self.sessions = _normalise_sessions(sessions)
        self._hourly: dict[str, pd.DataFrame] = {}
        self._daily: dict[str, pd.DataFrame] = {}
        self._tickers = tickers
        self.clock = clock
        self._vix: pd.Series | None = None   # real ^VIX daily closes (shifted +1d)
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._fetch_start = start - timedelta(days=lookback_days)
        self._fetch_end = end
        self._fetch_all(self._fetch_start, self._fetch_end)
        self._load_vix(start - timedelta(days=lookback_days), end)

    def cache_name(self, sym: str, ktype) -> str:
        """The parquet filename for this symbol, timeframe and session.

        Session-suffixed for anything but regular hours. A shared file would
        let an extended-hours experiment overwrite the cache every later
        regular-hours replay reads — including the parity runs, which is a
        corruption that outlives the run that caused it and shows up in the
        output as nothing at all.
        """
        tf = "DAY" if ktype == KLType.K_DAY else "60M"
        # Dailies are session-independent, so they keep one file.
        suffix = "" if (self.sessions == "RTH" or ktype == KLType.K_DAY) \
            else f"_{self.sessions}"
        return f"{sym}_{tf}{suffix}.parquet"

    def _session_arg(self, ktype):
        """The session to ask the broker for, or None to take its default.

        Daily bars are one row a day in every session, so they are always left
        alone — asking for extended hours there would change what the row
        contains rather than how many there are, and every daily-derived
        indicator in the strategy was fitted on regular-hours dailies.
        """
        if ktype == KLType.K_DAY or self.sessions == "RTH":
            return None
        return self.sessions

    def _hourly_budget(self) -> tuple[int, int]:
        """(bars, bars-per-day) for the hourly fetch — enough for THIS replay.

        Scaled two ways, and it needs both.

        By session, because 500 bars over 7-bar days is ten weeks of history
        and the same 500 over 24-bar days is three, which will not seed a
        50-day average.

        By WINDOW, because the number used to be a flat 500 whatever range the
        replay covered. A 500-bar hourly fetch is about a hundred trading days,
        so a 180-day sandbox run and a 360-day one both replayed the same
        hundred days and reported it as the window they were asked for. It
        showed up in a parity sweep as the sandbox freezing at 153 trades while
        v3 went 127 -> 271 over the same two windows: not two engines
        disagreeing, one engine answering a question it had not been asked.
        """
        bpd = {"RTH": 7, "ETH": 16}.get(self.sessions, 24)
        # Never fetch LESS than this — a short window still needs enough
        # history to seed a 50-day average.
        floor = 500 if self.sessions == "RTH" else min(500 * bpd // 7, 2400)
        start = getattr(self, "_fetch_start", None)
        end = getattr(self, "_fetch_end", None)
        if start is None or end is None:
            return floor, bpd       # no window known: the floor is all we can say
        # Calendar span of everything this feed was told to cover, warm-up
        # included, converted to trading days.
        span_days = max(1, (end - start).days)
        need = int(span_days * 5 / 7 * bpd) + 5 * bpd
        return max(floor, min(need, _MAX_HOURLY_BARS)), bpd

    def _fetch_all(self, fetch_start: datetime, fetch_end: datetime):
        client = MooClient()
        print(f"  Fetching {len(self._tickers)} tickers + SPY …")
        all_syms = list(self._tickers) + ["SPY"]
        for i, sym in enumerate(all_syms):
            cache_h = self.CACHE_DIR / self.cache_name(sym, KLType.K_60M)
            cache_d = self.CACHE_DIR / self.cache_name(sym, KLType.K_DAY)
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
                    # Fresh by DATE is not the same as deep enough. This asked
                    # only "how old is the newest bar", so a 497-bar cache
                    # satisfied a request for 2435 and the replay silently ran
                    # on a hundred days of a three-hundred-day window — the
                    # same truncation as the flat bar budget and the get_kline
                    # window cap, one layer further down, and the reason fixing
                    # those two did not move the numbers.
                    deep_enough = len(cached) >= full_bars * 0.95
                    if not deep_enough:
                        # Straight to the full refetch below. A top-up asks for
                        # the last few DAYS and merges — it answers staleness,
                        # not depth, so routing a shallow cache through it
                        # returned the same 504 bars with a fresher last row.
                        print(f"    {tag}: cache holds {len(cached)} bars, this "
                              f"run needs {full_bars} — refetching in full")
                    elif stale <= 3:
                        return cached, "cached"
                    elif stale <= 15:
                        need = min(full_bars, (stale + 3) * bpd + 5)
                        fresh = client.get_kline(sym, bars=need, ktype=ktype,
                                                 session=self._session_arg(ktype))
                        if fresh is not None and not fresh.empty:
                            merged = _merge(cached, fresh).tail(full_bars * 2)
                            merged.to_parquet(cache_path)
                            return merged, f"topped-up +{stale}d"
                        return cached, "cached (top-up failed)"
                # full refetch
                fresh = client.get_kline(sym, bars=full_bars, ktype=ktype,
                                         session=self._session_arg(ktype))
                if fresh is not None and not fresh.empty:
                    fresh = fresh.sort_index()
                    fresh.to_parquet(cache_path)
                    return fresh, "full fetch"
                return cached, "no data"

            try:
                h_bars, h_bpd = self._hourly_budget()
                df, how_h = _load_or_topup(cache_h, KLType.K_60M, h_bars, h_bpd)
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

        # Only bars that have CLOSED by now.
        #
        # This was `df.index <= cutoff`, and bars are indexed by their START
        # (time_key 10:30 covers 10:30–11:30). So at a sim time of 11:00 the
        # 10:30 bar was included WITH ITS 11:30 CLOSE — thirty minutes of future
        # price, on every scan that landed inside a forming bar. At the 15-minute
        # interval live actually uses, that is three scans in four.
        #
        # Live does not get that. Live's get_kline at 11:00 returns the 10:30 bar
        # with the price AS OF 11:00; the sandbox's cache holds the finished bar
        # and handed over the finished close. So the replay was not merely
        # different from live, it was optimistic in a way live cannot reproduce —
        # in the engine whose docstring promises no lookahead.
        #
        # Dropping the forming bar makes the sandbox slightly MORE conservative
        # than live, which sees partial data for it. That is the right direction
        # to be wrong in: without intra-hour bars the alternative is inventing a
        # partial close, and a replay that guesses at prices is not evidence.
        if len(df.index) >= 2 and cutoff is not None:
            step = df.index.to_series().diff().median()
            if pd.notna(step) and step > pd.Timedelta(0):
                df = df[df.index + step <= cutoff]
            else:
                df = df[df.index <= cutoff]
        else:
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



# ── Per-session fill telemetry ────────────────────────────
# Which sessions a replay's fills actually came from. Lives here with the
# session windows rather than in the engine, because it answers a question
# about the DATA — an overnight bar that trades 1,841 shares an hour cannot
# fill a 170-share order, and a replay that says it did is inventing liquidity.

_FILL_STATS: dict = {"attempted": 0, "capped": 0, "blocked": 0, "by_session": {}}


def _bar_session(dt) -> str:
    m = dt.hour * 60 + dt.minute
    if 9 * 60 + 30 <= m < 16 * 60:
        return "RTH"
    if 4 * 60 <= m < 9 * 60 + 30:
        return "pre-market"
    if 16 * 60 <= m < 20 * 60:
        return "after-hours"
    return "overnight"


def _note_fill(sess: str, outcome: str) -> None:
    _FILL_STATS[outcome] += 1
    per = _FILL_STATS["by_session"].setdefault(
        sess, {"attempted": 0, "capped": 0, "blocked": 0})
    per[outcome] += 1


def reset_fill_stats() -> None:
    _FILL_STATS.update({"attempted": 0, "capped": 0, "blocked": 0, "by_session": {}})


def fill_stats() -> dict:
    import copy
    return copy.deepcopy(_FILL_STATS)
