"""Thin wrapper around the moomoo-api SDK.

Centralises connection management so the rest of the bot never touches the raw
SDK types. All methods raise RuntimeError on API errors (RET_OK check is built
into each call) — callers can rely on the return values being valid.
"""
from __future__ import annotations

import collections
import logging
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import pandas as pd
from moomoo import (
    OpenQuoteContext,
    OpenSecTradeContext,
    SecurityFirm,
    TrdMarket,
    TrdEnv,
    TrdSide,
    OrderType,
    KLType,
    SubType,
    ModifyOrderOp,
    RET_OK,
)

from . import broker_binding, order_log
from .config import settings

log = logging.getLogger(__name__)

# US regular session = 6.5h = 390 min → bars per trading day by timeframe.
# Used to size history-kline windows so .tail(bars) always returns RECENT candles.
_BARS_PER_TRADING_DAY = {
    KLType.K_1M: 390, KLType.K_3M: 130, KLType.K_5M: 78, KLType.K_10M: 39,
    KLType.K_15M: 26, KLType.K_30M: 13, KLType.K_60M: 7, KLType.K_DAY: 1,
}

# How many hours each session spans, so the window maths above can be scaled.
# This is not cosmetic: request_history_kline returns the OLDEST max_count rows
# inside [start, end], so a window sized for 6.5-hour days while asking for
# 24-hour days holds ~3.7x the bars it budgeted for, overflows the 1000-row
# return limit, and hands back candles from weeks before the end date — the
# exact stale-candle bug the comment below describes, reintroduced by a session
# argument rather than by a timeframe.
_SESSION_HOURS = {"RTH": 6.5, "ETH": 16.0, "ALL": 24.0}

# How many windows a single get_kline may page through. Twelve hourly windows
# is roughly four and a half years — far past anything this project backtests,
# and a bound so a symbol the broker has no history for cannot spend the whole
# rate-limit budget discovering that.
_MAX_KLINE_CHUNKS = 12


# ---------- process-level sliding-window rate limiter ----------
# the broker's documented limit: 60 history-kline requests per 30 seconds.
# We track a deque of call timestamps and block any 61st caller until the
# oldest call ages out of the window. This is shared across ALL MooClient
# instances in the process (training + scan won't double-spend the quota).
_KLINE_WINDOW_SEC = 30.0
_KLINE_MAX_CALLS = 55       # leave a 5-call safety margin under the hard 60
_kline_call_log: "collections.deque[float]" = collections.deque(maxlen=_KLINE_MAX_CALLS)
_kline_lock = threading.Lock()


# 2026-07-09: REAL-unlock proof. the broker's trade unlock only gates order ops
# (place/modify/cancel) on REAL accounts — queries never need it and SIMULATE
# has no unlock concept at all. So the ONLY hard evidence that the user really
# clicked Unlock in the OpenD GUI is a gated operation succeeding in REAL env.
# The account snapshot persists this for the web badge (which previously
# inferred "已解锁" from a successful accinfo query — a wrong premise).
#
# This remains an OBSERVATION, never an authorization. The protocol has no way
# to read the gateway's lock state — no field in any response reports it — so
# "a gated op worked" can only ever be said afterwards, about an order that has
# already been sent. Nothing may treat it as permission to send one.
_REAL_GATED_OP_OK = False


def real_unlock_confirmed() -> bool:
    """True once a gated trade op (place/cancel) has succeeded in REAL env
    during this process's lifetime. Always False on SIMULATE."""
    return _REAL_GATED_OP_OK


def _note_gated_op_ok() -> None:
    global _REAL_GATED_OP_OK
    binding = broker_binding.current()
    if not _REAL_GATED_OP_OK and binding is not None and binding.trade_env == "REAL":
        _REAL_GATED_OP_OK = True
        log.info("REAL trade unlock confirmed (a gated order op succeeded)")


def _kline_rate_acquire() -> None:
    """Block until making one more call would fit in the past-30s window.
    Always records the timestamp before returning."""
    while True:
        with _kline_lock:
            now = time.monotonic()
            # Drop calls outside the window
            while _kline_call_log and now - _kline_call_log[0] > _KLINE_WINDOW_SEC:
                _kline_call_log.popleft()
            if len(_kline_call_log) < _KLINE_MAX_CALLS:
                _kline_call_log.append(now)
                return
            # Need to wait until the oldest ages out (plus 0.1s buffer)
            sleep_for = _KLINE_WINDOW_SEC - (now - _kline_call_log[0]) + 0.1
        log.info("kline rate limit: pausing %.1fs (%d calls in last %ds)",
                 sleep_for, len(_kline_call_log), int(_KLINE_WINDOW_SEC))
        time.sleep(max(0.5, sleep_for))


def _market_enum() -> TrdMarket:
    return {
        "US": TrdMarket.US,
        "HK": TrdMarket.HK,
        "CN": TrdMarket.CN,
        "SG": TrdMarket.SG,
    }[settings.moo_market]


def _env_enum() -> TrdEnv:
    """The pinned environment — never re-derived from settings.

    This used to be `SIMULATE if settings.moo_trade_env == "SIMULATE" else
    REAL`, read fresh on every call. Two faults in one line: any unrecognised
    value selected real money, and the answer could change mid-run because it
    was read from mutable configuration rather than from the run's own decision.
    """
    return broker_binding.require("a broker call").trd_env


def start_protocol_assert(what: str) -> None:
    """Lease and session only. Separate from the order gate so an order can be
    RECORDED before it is refused — see _place."""
    from . import start_protocol
    start_protocol.assert_may_trade(what)


def _assert_may_trade(what: str) -> None:
    """Refuse a broker mutation unless this process still holds its lease.

    Placed on the three methods that change broker state — place_limit_order,
    place_stop_loss and cancel_order — rather than at the eighteen call sites in
    executor, inverse_sleeve and cash_yield. One of eighteen would eventually be
    added without it, and the failure that check exists for is invisible from
    the call site: this process is healthy, its connection is fine, and it
    simply is not the current worker any more.
    """
    from . import order_gate, start_protocol
    # Two questions, asked in this order. "May this process reach the order book
    # at all?" is answered by the grant the parent issued at GO time and cannot
    # change afterwards. "Is this process still the worker?" is answered by the
    # lease and the session, and can change under it at any moment.
    order_gate.require(what)
    start_protocol.assert_may_trade(what)


def _acc_id() -> int:
    """The pinned account. Passing this is what stops the SDK from choosing.

    With acc_id unset the SDK calls _get_default_acc_id(), which returns the
    first account whose environment matches, in broker-supplied order. That is
    correct exactly when there is one account per environment and silent when
    there is not.
    """
    return broker_binding.require("a broker call").acc_id


@dataclass(frozen=True)
class Placement:
    """What came back from asking for an order: both ids, never just one.

    broker_order_id is what the broker calls it; client_order_id is what we
    called it before it existed. The second is the one that survives a lost
    answer, so returning only the first — as this did — meant the caller could
    not wait on, or later claim, an order whose response never arrived.
    """
    broker_order_id: str
    client_order_id: str


class MooClient:
    """One quote context + one trade context, opened lazily."""

    def __init__(self) -> None:
        self._quote: OpenQuoteContext | None = None
        self._trade: OpenSecTradeContext | None = None

    # ---------- lifecycle ----------
    @property
    def quote(self) -> OpenQuoteContext:
        if self._quote is None:
            self._quote = OpenQuoteContext(
                host=settings.moo_host, port=settings.moo_port
            )
        return self._quote

    @property
    def trade(self) -> OpenSecTradeContext:
        if self._trade is None:
            firm = getattr(SecurityFirm, settings.moo_security_firm)
            self._trade = OpenSecTradeContext(
                filter_trdmarket=_market_enum(),
                host=settings.moo_host,
                port=settings.moo_port,
                security_firm=firm,
            )
            # Resolve and pin the account before this context is usable.
            #
            # What used to be here was an unconditional unlock_trade() with the
            # real password. On SIMULATE that is pure side effect — the broker's
            # documentation is explicit that simulated trading has no unlock —
            # and on REAL it arms order placement across everything sharing that
            # OpenD gateway, from a property that merely opening a connection
            # triggers. Arming real money is a per-start decision made by a
            # person, not a consequence of reading a position list.
            try:
                binding = broker_binding.current()
                if binding is None:
                    binding = broker_binding.resolve(
                        self._trade, trade_env=self._pinned_env(),
                        expected_acc_id=self._expected_acc_id())
                    broker_binding.verify_against_session(binding)
                    broker_binding.bind(binding)
            except Exception:
                self._trade.close()
                self._trade = None
                raise
        return self._trade

    @staticmethod
    def _pinned_env() -> str:
        """The run's environment, from the session when there is one.

        Inside a worker the session decided this before OpenD was contacted and
        it cannot move. Outside one (CLI tools, the web panel) it comes from
        configuration — but through the fail-closed parser, so an unrecognised
        value refuses instead of selecting REAL.
        """
        try:
            from . import identity
            session = identity.current_session()
            if session:
                return broker_binding.parse_trade_env(session["trade_env"],
                                                      where="the session")
        except Exception:
            pass
        return broker_binding.parse_trade_env(settings.moo_trade_env,
                                              where="MOO_TRADE_ENV")

    @staticmethod
    def _expected_acc_id() -> str | None:
        """The broker account these records were written against, if known."""
        try:
            from . import identity
            info = identity.account_info() or {}
            return info.get("broker_acc_id")
        except Exception:
            return None

    def close(self) -> None:
        if self._quote is not None:
            self._quote.close()
            self._quote = None
        if self._trade is not None:
            self._trade.close()
            self._trade = None

    # ---------- market data ----------

    def get_kline(self, symbol: str, bars: int = 120, ktype: KLType | None = None,
                  session: str | None = None) -> pd.DataFrame:
        """Return LATEST `bars` candles. Process-level rate-limited (55/30s) +
        auto-retry once on a high-frequency error.

        Background: pre-fix, throttle was per-instance, so train + scan running
        back-to-back could spend > 60 calls in 30s and trigger the broker's hard
        limit. The new _kline_rate_acquire() is module-global → all clients
        share the same 30-second window.

        IMPORTANT: request_history_kline without start/end returns OLDEST bars.
        We compute an explicit date window ending today and call .tail(bars).
        """
        from datetime import date, timedelta
        from .timeframe import current as _tf

        if ktype is None:
            ktype = _tf().kltype

        end_d = date.today()
        # Size the date window so it holds ~`bars` candles ending TODAY, and keep
        # max_count ABOVE the window's bar count.
        #
        # Why this matters (was a real bug): request_history_kline returns the
        # OLDEST `max_count` candles inside [start, end]. If the window holds more
        # bars than max_count, .tail(bars) lands on STALE candles. The old code
        # used a 15-day window for every intraday tf with max_count=200, so a 5-min
        # request (≈78 bars/day → >1000 bars in 15 days) returned candles from ~2
        # weeks ago. Fix: a tight, timeframe-aware window + generous max_count.
        bpd = _BARS_PER_TRADING_DAY.get(ktype)
        if bpd and ktype != KLType.K_DAY and session in ("ETH", "ALL"):
            bpd = math.ceil(bpd * _SESSION_HOURS[session] / _SESSION_HOURS["RTH"])
        if bpd:
            trading_days = max(1, math.ceil(bars / bpd))
            window_days = math.ceil(trading_days * 7 / 5) + (5 if ktype == KLType.K_DAY else 3)
            # Cap the window so its bar count stays under the API's ~1000-row
            # single-request return limit (else we truncate to the OLDEST 1000 and
            # lose the latest candles). Use trading-day basis (≤ calendar days) so
            # even 1-min (390 bars/day) stays under the cap and ends at the latest bar.
            max_window_days = max(2, int(1000 / bpd))
            window_days = min(window_days, max_window_days)
            max_count = 1000
        else:
            # Coarse timeframes (weekly/monthly/…): few bars, generous window.
            window_days = bars * 10
            max_count = max(bars * 3, 200)
        start_d = end_d - timedelta(days=window_days)
        code = self._format_code(symbol)

        # When the ask is longer than one request can carry, page backwards.
        #
        # window_days is capped at 1000/bpd so a single response cannot overflow
        # the API's ~1000-row return limit — 142 calendar days for hourly bars.
        # That cap used to be the end of it: asking for 2590 hourly bars (a
        # 360-day backtest) returned 686, spanning 141 days, with no error and
        # no warning. The run completed and reported numbers computed on 40% of
        # the window it named.
        #
        # The data is there. Asking the broker for explicit older windows
        # returns ~450 hourly bars per three months, back to at least 2025-03.
        # Only this function's arithmetic stopped at one window.
        if bpd and bars > window_days * bpd * 5 / 7:
            return self._get_kline_chunked(code, symbol, bars, ktype, end_d,
                                           window_days, max_count)

        # Try up to 2 times — on rate-limit error wait for the window to clear.
        last_err = None
        for attempt in (1, 2):
            _kline_rate_acquire()
            ret, df, _ = self.quote.request_history_kline(
                code,
                start=start_d.isoformat(),
                end=end_d.isoformat(),
                ktype=ktype,
                max_count=max_count,
                autype="qfq",
                # Regular hours unless asked otherwise. "RTH" is the broker's
                # default and the only session the strategy has ever been
                # designed or validated on; the others exist so an experiment
                # can ask for them explicitly rather than by changing a default
                # under everything that already depends on it.
                **({"session": session} if session else {}),
            )
            if ret == RET_OK:
                df = df.copy()
                df["time_key"] = pd.to_datetime(df["time_key"])
                df = df.set_index("time_key").sort_index()
                return df.tail(bars)
            last_err = str(df)
            # If Broker rejected for rate, sleep one full window + retry once.
            if "high frequency" in last_err.lower() and attempt == 1:
                log.warning("Broker rejected %s for rate-limit — waiting 32s and retrying",
                            symbol)
                time.sleep(32)
                continue
            break
        raise RuntimeError(f"request_history_kline failed for {symbol}: {last_err}")

    def _get_kline_chunked(self, code: str, symbol: str, bars: int, ktype,
                           end_d, window_days: int, max_count: int):
        """Fetch a long history as consecutive windows, newest first.

        Stops as soon as it has enough rows or the broker returns an empty
        window — the second is the real end of the history, and continuing past
        it would spend the rate-limit budget on nothing.
        """
        from datetime import timedelta
        frames, cursor = [], end_d
        have = 0
        for _ in range(_MAX_KLINE_CHUNKS):
            chunk_start = cursor - timedelta(days=window_days)
            _kline_rate_acquire()
            ret, df, _ = self.quote.request_history_kline(
                code, start=chunk_start.isoformat(), end=cursor.isoformat(),
                ktype=ktype, max_count=max_count, autype="qfq")
            if ret != RET_OK:
                if not frames:
                    raise RuntimeError(
                        f"request_history_kline failed for {symbol}: {df}")
                log.warning("%s: history stops at %s (%s) — returning %d bars",
                            symbol, chunk_start.isoformat(), str(df)[:80], have)
                break
            if df is None or df.empty:
                break                       # the history genuinely ends here
            frames.append(df)
            have += len(df)
            if have >= bars:
                break
            # One day of overlap so a bar on the boundary is never dropped;
            # duplicates are removed below.
            cursor = chunk_start + timedelta(days=1)

        if not frames:
            raise RuntimeError(f"no kline history returned for {symbol}")
        out = pd.concat(frames)
        out["time_key"] = pd.to_datetime(out["time_key"])
        out = (out.set_index("time_key")
                  .sort_index())
        out = out[~out.index.duplicated(keep="last")]
        if len(out) < bars:
            log.info("%s: asked for %d bars, the broker's history holds %d "
                     "(from %s)", symbol, bars, len(out),
                     str(out.index[0])[:10])
        return out.tail(bars)

    def get_vix(self) -> float:
        """Fetch current VIX level from broker snapshot.

        Falls back to SPY realized-vol proxy if the index quote isn't available,
        and returns 15.0 (benign default) if both fail.
        """
        # Try the VIX index directly
        try:
            ret, data = self.quote.get_market_snapshot(["US.VIX"])
            if ret == RET_OK and not data.empty:
                v = float(data.iloc[0].get("last_price", 0))
                if v > 0:
                    return v
        except Exception:
            pass

        # Fallback: SPY 14-day ATR expressed as annualised % ≈ VIX
        try:
            df = self.get_kline("SPY", bars=20, ktype=KLType.K_DAY)
            import pandas_ta_classic as _ta
            atr = float(_ta.atr(df["high"], df["low"], df["close"], length=14).iloc[-1])
            price = float(df["close"].iloc[-1])
            # Daily ATR% × sqrt(252) ≈ annualised vol (rough VIX proxy)
            return round(atr / price * 100 * (252 ** 0.5), 1)
        except Exception:
            pass

        return 15.0   # neutral / benign fallback

    def get_snapshot(self, symbol: str) -> dict:
        ret, data = self.quote.get_market_snapshot([self._format_code(symbol)])
        if ret != RET_OK:
            raise RuntimeError(f"get_market_snapshot failed: {data}")
        return data.iloc[0].to_dict()

    def get_spread_pct(self, symbol: str) -> float:
        """Return (ask - bid) / mid × 100. Returns 0 if quote unavailable
        (e.g. paper trading often lacks live bid/ask) so it doesn't block trades."""
        try:
            ret, data = self.quote.get_market_snapshot([self._format_code(symbol)])
            if ret != RET_OK or data.empty:
                return 0.0
            row = data.iloc[0]
            bid = float(row.get("bid_price", 0) or 0)
            ask = float(row.get("ask_price", 0) or 0)
            if bid <= 0 or ask <= 0 or ask < bid:
                return 0.0
            mid = (bid + ask) / 2
            return (ask - bid) / mid * 100 if mid > 0 else 0.0
        except Exception:
            return 0.0

    def get_order_status(self, order_id: str) -> str:
        """Return SDK OrderStatus string (e.g. 'FILLED_ALL', 'SUBMITTED'),
        or '' if the order can't be found."""
        try:
            ret, data = self.trade.order_list_query(trd_env=_env_enum(), acc_id=_acc_id())
            if ret != RET_OK or data is None or data.empty:
                return ""
            row = data[data["order_id"].astype(str) == str(order_id)]
            if row.empty:
                return ""
            return str(row.iloc[0].get("order_status", ""))
        except Exception as e:
            log.warning("get_order_status(%s) failed: %s", order_id, e)
            return ""

    def is_order_filled(self, order_id: str, include_partial: bool = False) -> bool:
        """True if the order is fully filled. With include_partial=True, a
        PARTIAL fill also counts as 'fired' — used by the OCO check so a
        partially-filled bracket leg still cancels the opposite leg (preventing
        double exposure); reconcile() then re-syncs any residual qty."""
        st = self.get_order_status(order_id)
        if include_partial:
            return st in ("FILLED_ALL", "FILLED_PART")
        return st == "FILLED_ALL"

    def get_order_fill(self, order_id: str) -> dict | None:
        """ACTUAL execution of `order_id` → {price, qty, status}, or None if it
        can't be read / nothing filled yet.

        `price` is the broker's dealt_avg_price — the real volume-weighted fill,
        not the limit we asked for. Added 2026-07-27: protective exits book P&L
        at the pre-order quote (`last`) while placing a marketable limit 3% below
        it, so trades.jsonl recorded an exit price the broker never gave us. That
        file feeds half-Kelly, the optimizer, the blacklist and adaptive sizing,
        so the whole self-improvement loop was reading slippage-free numbers.
        Partial fills return the partial qty — the caller decides what to do."""
        try:
            ret, data = self.trade.order_list_query(trd_env=_env_enum(), acc_id=_acc_id())
            if ret != RET_OK or data is None or data.empty:
                return None
            row = data[data["order_id"].astype(str) == str(order_id)]
            if row.empty:
                return None
            r = row.iloc[0]
            price = float(r.get("dealt_avg_price") or 0)
            qty = int(float(r.get("dealt_qty") or 0))
            if price <= 0 or qty <= 0:
                return None
            return {"price": price, "qty": qty,
                    "status": str(r.get("order_status", ""))}
        except Exception as e:
            log.warning("get_order_fill(%s) failed: %s", order_id, e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Ask the broker to cancel an order. True means the REQUEST was accepted.

        It does not mean the order is gone. The order may have filled between
        the decision to cancel and the request arriving, and the broker will
        happily accept a cancel for something already executed. Callers that
        need to know whether protection is still live must poll, not assume.
        """
        _assert_may_trade(f"cancelling order {order_id}")
        row = order_log.by_broker_id(order_id)
        try:
            ret, data = self.trade.modify_order(
                modify_order_op=ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=_env_enum(), acc_id=_acc_id(),
            )
            if ret == RET_OK:
                _note_gated_op_ok()
            if row:
                order_log.cancel_requested(row["client_order_id"],
                                           accepted=(ret == RET_OK),
                                           detail="" if ret == RET_OK else str(data))
            return ret == RET_OK
        except Exception as e:
            log.warning("cancel_order(%s) failed: %s", order_id, e)
            if row:
                order_log.cancel_requested(row["client_order_id"], accepted=False,
                                           detail=f"{type(e).__name__}: {e}")
            return False

    def list_pending_buys(self) -> "pd.DataFrame":
        """Pending BUY orders (with create_time for staleness check)."""
        ret, data = self.trade.order_list_query(trd_env=_env_enum(), acc_id=_acc_id())
        if ret != RET_OK or data is None or data.empty:
            return pd.DataFrame()
        return data[
            # FILLED_PART included (2026-06-10): a stale partially-filled buy
            # must still be canceled — and its record kept at dealt_qty —
            # otherwise it lingers all day and the leftover becomes an orphan.
            data["order_status"].isin(["SUBMITTED", "SUBMITTING",
                                       "WAITING_SUBMIT", "FILLED_PART"])
            & (data["trd_side"] == "BUY")
        ]

    # ---------- trading ----------
    def get_account_cash(self) -> float:
        ret, data = self.trade.accinfo_query(trd_env=_env_enum(), acc_id=_acc_id())
        if ret != RET_OK:
            raise RuntimeError(f"accinfo_query failed: {data}")
        return float(data.iloc[0]["cash"])

    def get_positions(self) -> pd.DataFrame:
        ret, data = self.trade.position_list_query(trd_env=_env_enum(), acc_id=_acc_id())
        if ret != RET_OK:
            raise RuntimeError(f"position_list_query failed: {data}")
        return data

    def get_short_symbols(self) -> set[str]:
        """Symbols the account is NET SHORT. Buying one of these nets against
        the short instead of opening a long — see reconcile.short_symbols()."""
        try:
            from .reconcile import short_symbols     # lazy: avoids import cycle
            return short_symbols(self.get_positions())
        except Exception as e:
            log.warning("get_short_symbols failed: %s", e)
            return set()

    def get_last_sell_fill(self, symbol: str, lookback_days: int = 7) -> dict | None:
        """Most recent SELL execution for `symbol` → {price, qty, time}, or None.

        Used to BOOK a manual exit: when reconcile finds a tracked position the
        broker no longer holds (GHOST = you sold it in the broker app), this looks
        up the ACTUAL fill so the realised P&L is recorded at the true exit price
        instead of a guess. Checks today's deals first (the common case —
        reconcile runs every scan), then the history deal list over the last
        `lookback_days` (covers a sell detected the next session). Never raises."""
        code = self._format_code(symbol)
        bare = symbol.split(".")[-1]

        def _latest_sell(df) -> dict | None:
            if df is None or getattr(df, "empty", True):
                return None
            d = df.copy()
            if "code" in d.columns:
                d = d[d["code"].astype(str).str.endswith(bare)]
            if "trd_side" in d.columns:
                d = d[d["trd_side"].astype(str).str.upper().str.contains("SELL")]
            if d.empty:
                return None
            if "create_time" in d.columns:
                d = d.sort_values("create_time")
            row = d.iloc[-1]
            try:
                return {"price": float(row.get("price") or 0),
                        "qty": int(float(row.get("qty") or 0)),
                        "time": str(row.get("create_time") or "")}
            except Exception:
                return None

        # 1) today's deals
        try:
            ret, data = self.trade.deal_list_query(code=code, trd_env=_env_enum(), acc_id=_acc_id())
            if ret == RET_OK:
                hit = _latest_sell(data)
                if hit and hit["price"] > 0:
                    return hit
        except Exception as e:
            log.debug("deal_list_query(%s) failed: %s", symbol, e)

        # 2) history fallback
        try:
            from datetime import date, timedelta
            start = (date.today() - timedelta(days=lookback_days)).isoformat()
            end = date.today().isoformat()
            ret, data = self.trade.history_deal_list_query(
                code=code, start=start, end=end, trd_env=_env_enum(), acc_id=_acc_id())
            if ret == RET_OK:
                hit = _latest_sell(data)
                if hit and hit["price"] > 0:
                    return hit
        except Exception as e:
            log.debug("history_deal_list_query(%s) failed: %s", symbol, e)

        return None

    def find_dealt_sell_orders(self, symbol: str, lookback_days: int = 7) -> list[dict]:
        """SELL orders for `symbol` that actually dealt → [{order_id, price,
        qty, time, status}], oldest first. Empty list = the broker has no
        record of anything being sold.

        Deliberately reads the ORDER list rather than the deal list. Paper
        trading refuses deal queries outright ("Paper trading does not support
        deal data"), which is why get_last_sell_fill always returned None under
        SIMULATE and every ghost fell through to an INVENTED manual-sell price
        (2026-07-28: DDOG booked a $253.46 MANUAL_SELL that never happened).
        Orders are queryable in both envs, carry the order_id that tells OUR
        exit from a hand-placed one, and report dealt_avg_price — the real fill.
        """
        code = self._format_code(symbol)
        bare = symbol.split(".")[-1]
        rows: list[dict] = []

        def _collect(df) -> None:
            if df is None or getattr(df, "empty", True):
                return
            d = df.copy()
            if "code" in d.columns:
                d = d[d["code"].astype(str).str.endswith(bare)]
            if "trd_side" in d.columns:
                d = d[d["trd_side"].astype(str).str.upper().str.contains("SELL")]
            for _, r in d.iterrows():
                try:
                    qty = int(float(r.get("dealt_qty") or 0))
                    price = float(r.get("dealt_avg_price") or 0)
                except (TypeError, ValueError):
                    continue
                if qty <= 0 or price <= 0:
                    continue          # cancelled / never filled — not evidence
                rows.append({
                    "order_id": str(r.get("order_id", "")),
                    "price": price,
                    "qty": qty,
                    "time": str(r.get("create_time") or ""),
                    "status": str(r.get("order_status", "")),
                })

        try:
            ret, data = self.trade.order_list_query(code=code, trd_env=_env_enum(), acc_id=_acc_id())
            if ret == RET_OK:
                _collect(data)
        except Exception as e:
            log.debug("order_list_query(%s) failed: %s", symbol, e)

        try:
            from datetime import date, timedelta
            ret, data = self.trade.history_order_list_query(
                code=code,
                start=(date.today() - timedelta(days=lookback_days)).isoformat(),
                end=date.today().isoformat(),
                trd_env=_env_enum(), acc_id=_acc_id())
            if ret == RET_OK:
                _collect(data)
        except Exception as e:
            log.debug("history_order_list_query(%s) failed: %s", symbol, e)

        seen: set[str] = set()
        uniq = []
        for r in sorted(rows, key=lambda x: x["time"]):
            if r["order_id"] and r["order_id"] in seen:
                continue
            seen.add(r["order_id"])
            uniq.append(r)
        return uniq

    def get_pending_buy_value(self) -> float:
        """Sum of (price × qty) for BUY orders awaiting fill — counted as
        committed capital so we don't double-spend the budget."""
        ret, data = self.trade.order_list_query(trd_env=_env_enum(), acc_id=_acc_id())
        if ret != RET_OK:
            return 0.0
        if data is None or data.empty:
            return 0.0
        pending = data[
            data["order_status"].isin(["SUBMITTED", "SUBMITTING", "WAITING_SUBMIT"])
            & (data["trd_side"] == "BUY")
        ]
        if pending.empty:
            return 0.0
        return float((pending["qty"].astype(float) * pending["price"].astype(float)).sum())

    def get_pending_symbols(self) -> set[str]:
        """Symbols of BUY orders awaiting fill — used to skip duplicates."""
        ret, data = self.trade.order_list_query(trd_env=_env_enum(), acc_id=_acc_id())
        if ret != RET_OK or data is None or data.empty:
            return set()
        pending = data[
            data["order_status"].isin(["SUBMITTED", "SUBMITTING", "WAITING_SUBMIT"])
            & (data["trd_side"] == "BUY")
        ]
        return {c.split(".")[-1] for c in pending["code"].tolist()}

    # Messages that mean "we did not get an answer", as opposed to "the answer
    # was no". The distinction decides whether a failure is REJECTED (evidence
    # that nothing reached the order book) or UNKNOWN (no evidence either way).
    # Getting it wrong in the safe direction costs one extra broker query; wrong
    # in the other direction is a duplicate order.
    _AMBIGUOUS = ("timeout", "timed out", "connection", "disconnect", "broken",
                  "reset", "unreachable", "no response", "网络", "超时")

    def _place(self, *, symbol: str, qty: int, price: float, side: TrdSide,
               kind: str, order_type=OrderType.NORMAL,
               aux_price: float | None = None, intent: str = "",
               extra: dict | None = None) -> "Placement":
        """Place one order, recorded before it is sent.

        The order of operations is the point. order_log.begin() commits a row
        and flushes it, and only then is the broker called — so a crash or a
        dropped connection leaves a record saying "this may exist", which can be
        resolved by asking. Recording afterwards cannot express that case at
        all: the call that neither succeeds nor fails looks exactly like the
        call that never happened, and the retry buys twice.
        """
        # Lease and session first: without them there is no run to attribute an
        # order to, so there is nothing to record either.
        start_protocol_assert(f"placing a {side} order for {symbol}")
        rounded = round(price, 2) if price >= 1 else round(price, 4)
        aux = None if aux_price is None else (
            round(aux_price, 2) if aux_price >= 1 else round(aux_price, 4))

        try:
            coid = order_log.begin(symbol=symbol, side=str(side).split(".")[-1],
                                   kind=kind, requested_qty=int(qty),
                                   limit_price=rounded, aux_price=aux,
                                   intent=intent, extra=extra)
        except order_log.DuplicateIntent as e:
            # A live order already serves this intent. Sending a second one is
            # how the same shares get bought — or sold — twice; the first has to
            # reach a terminal state before another can take its place. Callers
            # wrap per-symbol, so this defers rather than crashing the pass.
            raise RuntimeError(
                f"{symbol}: refusing a second {kind} order while "
                f"{e.existing['client_order_id']} is still "
                f"{e.existing['state']} — settle or cancel it first") from e

        # The order gate is checked AFTER the intent is recorded, so a refused
        # order is still written down as FAILED_LOCAL — never sent, but visible.
        #
        # Checking it first meant a staging run left no trace of what the
        # strategy had wanted to do: the whole point of running without order
        # capability is to watch the decisions, and they were being discarded at
        # the last step. FAILED_LOCAL already meant "we refused it ourselves";
        # this is what it is for.
        from . import order_gate
        try:
            order_gate.require(f"placing a {side} order for {symbol}")
        except order_gate.OrdersNotPermitted as e:
            order_log.failed_local(coid, str(e))
            raise
        kwargs = dict(price=rounded, qty=qty, code=self._format_code(symbol),
                      trd_side=side, order_type=order_type,
                      trd_env=_env_enum(), acc_id=_acc_id(),
                      # Our id, carried by the broker and returned on order
                      # queries — so an order whose result we never saw can be
                      # claimed by name instead of guessed at from its shape.
                      remark=coid)
        if aux is not None:
            kwargs["aux_price"] = aux

        try:
            ret, data = self.trade.place_order(**kwargs)
        except Exception as e:
            order_log.unknown(coid, f"{type(e).__name__}: {e}")
            raise RuntimeError(
                f"place_order for {symbol} failed without an answer ({e}) — "
                f"order {coid} recorded UNKNOWN; it will be resolved by "
                f"querying the broker, not by assuming") from e

        if ret != RET_OK:
            msg = str(data)
            if any(m in msg.lower() for m in self._AMBIGUOUS):
                order_log.unknown(coid, msg)
            else:
                order_log.rejected(coid, msg)
            raise RuntimeError(f"place_order failed for {symbol}: {msg}")

        order_id = str(data.iloc[0]["order_id"]).strip()
        # 2026-07-09: an order_id of 0/empty came from an OpenD-rs gateway that
        # returned a need_op_confirm stub and then purged it. This used to be
        # treated as a clean failure, which is a guess — the broker answered
        # with something meaningless, not with "no". UNKNOWN says that, and the
        # recovery sweep settles it.
        if order_id in ("", "0", "None", "nan"):
            order_log.unknown(coid, f"broker returned order_id={order_id!r}")
            raise RuntimeError(
                f"place_order for {symbol} returned invalid order_id={order_id!r} "
                f"— order {coid} recorded UNKNOWN pending a broker query")

        order_log.submitted(coid, order_id)
        _note_gated_op_ok()
        log.info("Placed %s %s qty=%s price=%.2f order_id=%s (%s)",
                 side, symbol, qty, price, order_id, coid)
        return Placement(broker_order_id=order_id, client_order_id=coid)

    def await_fill(self, client_order_id: str, *, timeout: float = 20.0,
                   poll: float = 0.5) -> dict:
        """Poll one order until it settles or the wait runs out. Returns its row.

        The order log is updated on every poll, so whatever this returns is also
        on disk — a crash mid-wait leaves the last known state recorded rather
        than nothing.

        A timeout is not a failure and not a fill. It means the order is still
        working, and the caller must treat the quantity that HAS filled as the
        real one. That is the whole point: the previous code recorded a position
        the size of the request the instant the order was accepted, so a limit
        that filled 40 of 100 produced a 100-share position with a stop covering
        60 shares that were never bought.
        """
        deadline = time.time() + timeout
        row = order_log.get(client_order_id)
        if row is None:
            raise RuntimeError(f"unknown order {client_order_id}")
        broker_id = row.get("broker_order_id")

        while True:
            try:
                ret, data = self.trade.order_list_query(
                    trd_env=_env_enum(), acc_id=_acc_id())
                if ret == RET_OK and data is not None and len(data):
                    hit = order_log.claim_from_broker(client_order_id, data)
                    if hit is None and broker_id:
                        hit = order_log._claim_by_broker_id(broker_id, data)
                    if hit is not None:
                        state = order_log.map_broker_status(hit.get("order_status"))
                        order_log.record_fill(
                            client_order_id,
                            filled_qty=int(float(hit.get("dealt_qty") or 0)),
                            avg_price=float(hit.get("dealt_avg_price") or 0) or None,
                            state=state,
                            broker_order_id=str(hit.get("order_id") or "") or None)
                        if state in order_log.TERMINAL_STATES:
                            return order_log.get(client_order_id)
            except Exception as e:
                # A failed poll is not evidence about the order. Keep trying
                # until the deadline; the log still holds the last known state.
                log.warning("fill poll for %s failed: %s", client_order_id, e)

            if time.time() >= deadline:
                final = order_log.get(client_order_id)
                log.info("order %s still working after %.0fs — %s of %s filled",
                         client_order_id, timeout, final.get("filled_qty"),
                         final.get("requested_qty"))
                return final
            time.sleep(poll)

    def history_orders(self, start: str, end: str):
        """Every order in the window, live and settled, with `remark` intact.

        Both endpoints, concatenated. An order placed minutes ago may not have
        reached the history endpoint yet while sitting plainly in the live one,
        and the recovery sweep asking only history would conclude it never
        existed — about an order that is working at the broker right now.

        Note `end` is INCLUSIVE on this API. That caused a double-count when
        these rows were summed by month; here they are matched by id, so a row
        appearing twice is harmless and the wider window is worth more.
        """
        frames, failures = [], []
        for fn, kwargs in (
            (self.trade.order_list_query, {}),
            (self.trade.history_order_list_query, {"start": start, "end": end}),
        ):
            try:
                ret, data = fn(trd_env=_env_enum(), acc_id=_acc_id(), **kwargs)
                if ret != RET_OK:
                    failures.append(f"{fn.__name__}: {str(data)[:120]}")
                    continue
                if data is not None and len(data):
                    frames.append(data)
            except Exception as e:
                failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")

        # An empty answer and a failed question are not the same thing, and this
        # returned the same object for both. Downstream, "no rows" is read as
        # "the broker does not have this order" — so a transient query failure
        # became evidence that an order never existed, and the next cycle placed
        # it again. That is the duplicate fill this whole log exists to prevent,
        # reintroduced one layer above it.
        #
        # So the completeness of the answer travels WITH the answer, and callers
        # must not conclude anything from an incomplete one.
        out = (pd.concat(frames, ignore_index=True) if frames
               else pd.DataFrame())
        if "order_id" in out.columns:
            out = out.drop_duplicates(subset=["order_id"], keep="last")
        if failures:
            log.error("order query INCOMPLETE — %s", "; ".join(failures))
        out.attrs["complete"] = not failures
        out.attrs["failures"] = failures
        return out

    def place_limit_order(
        self, symbol: str, qty: int, price: float, side: TrdSide,
        *, kind: str | None = None, intent: str = "",
        extra: dict | None = None
    ) -> Placement:
        return self._place(symbol=symbol, qty=qty, price=price, side=side,
                           kind=kind or ("ENTRY" if side == TrdSide.BUY else "EXIT"),
                           intent=intent, extra=extra)

    def place_stop_loss(self, symbol: str, qty: int, stop_price: float,
                        *, intent: str = "") -> Placement:
        """Sell-stop to close a long position."""
        return self._place(symbol=symbol, qty=qty, price=stop_price,
                           side=TrdSide.SELL, kind="STOP",
                           order_type=OrderType.STOP, aux_price=stop_price,
                           intent=intent)

    # ---------- helpers ----------
    @staticmethod
    def _format_code(symbol: str) -> str:
        market = settings.moo_market
        if "." in symbol:
            return symbol
        return f"{market}.{symbol}"


@contextmanager
def client() -> Iterator[MooClient]:
    c = MooClient()
    try:
        yield c
    finally:
        c.close()
