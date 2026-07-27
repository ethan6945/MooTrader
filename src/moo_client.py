"""Thin wrapper around the moomoo-api SDK.

Centralises connection management so the rest of the bot never touches the raw
SDK types. All methods raise RuntimeError on API errors (RET_OK check is built
into each call) — callers can rely on the return values being valid.
"""
from __future__ import annotations

import collections
import hashlib
import logging
import math
import threading
import time
from contextlib import contextmanager
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

from .config import settings

log = logging.getLogger(__name__)

# US regular session = 6.5h = 390 min → bars per trading day by timeframe.
# Used to size history-kline windows so .tail(bars) always returns RECENT candles.
_BARS_PER_TRADING_DAY = {
    KLType.K_1M: 390, KLType.K_3M: 130, KLType.K_5M: 78, KLType.K_10M: 39,
    KLType.K_15M: 26, KLType.K_30M: 13, KLType.K_60M: 7, KLType.K_DAY: 1,
}


# ---------- process-level sliding-window rate limiter ----------
# the broker's documented limit: 60 history-kline requests per 30 seconds.
# We track a deque of call timestamps and block any 61st caller until the
# oldest call ages out of the window. This is shared across ALL MooClient
# instances in the process (training + scan won't double-spend the quota).
_KLINE_WINDOW_SEC = 30.0
_KLINE_MAX_CALLS = 55       # leave a 5-call safety margin under the hard 60
_kline_call_log: "collections.deque[float]" = collections.deque(maxlen=_KLINE_MAX_CALLS)
_kline_lock = threading.Lock()


# OpenD GUI mode disables programmatic unlock_trade — every trade-context open
# triggers the same warning. Log it once per process instead of spamming.
_OPEND_UNLOCK_WARNED = False

# 2026-07-09: REAL-unlock proof. the broker's trade unlock only gates order ops
# (place/modify/cancel) on REAL accounts — queries never need it and SIMULATE
# has no unlock concept at all. So the ONLY hard evidence that the user really
# clicked Unlock in the OpenD GUI is a gated operation succeeding in REAL env.
# The account snapshot persists this for the web badge (which previously
# inferred "已解锁" from a successful accinfo query — a wrong premise).
_REAL_GATED_OP_OK = False


def real_unlock_confirmed() -> bool:
    """True once a gated trade op (place/cancel) has succeeded in REAL env
    during this process's lifetime. Always False on SIMULATE."""
    return _REAL_GATED_OP_OK


def _note_gated_op_ok() -> None:
    global _REAL_GATED_OP_OK
    if not _REAL_GATED_OP_OK and settings.moo_trade_env == "REAL":
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
    return TrdEnv.SIMULATE if settings.moo_trade_env == "SIMULATE" else TrdEnv.REAL


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
            pwd = settings.moo_trade_pwd
            pwd_md5 = hashlib.md5(pwd.encode()).hexdigest()
            ret, data = self._trade.unlock_trade(password=pwd, password_md5=pwd_md5)
            if ret != RET_OK:
                msg = str(data)
                if "GUI version" in msg or "Unlock button" in msg:
                    # OpenD GUI version disables programmatic unlock BY DESIGN —
                    # this error only proves "GUI build", NOT "unlocked". Queries
                    # work either way; SIMULATE never needs unlock; REAL order ops
                    # fail until the user clicks Unlock in the OpenD window. Log
                    # once per process, env-appropriately.
                    global _OPEND_UNLOCK_WARNED
                    if not _OPEND_UNLOCK_WARNED:
                        if settings.moo_trade_env == "REAL":
                            log.warning(
                                "OpenD connected (GUI version, API unlock disabled) "
                                "— REAL unlock NOT confirmed: order ops will fail "
                                "unless you clicked Unlock in the OpenD window")
                        else:
                            log.info(
                                "OpenD connected (GUI version, API unlock disabled "
                                "— SIMULATE needs no unlock, this is normal)")
                        _OPEND_UNLOCK_WARNED = True
                else:
                    raise RuntimeError(f"unlock_trade failed: {data}")
        return self._trade

    def close(self) -> None:
        if self._quote is not None:
            self._quote.close()
            self._quote = None
        if self._trade is not None:
            self._trade.close()
            self._trade = None

    # ---------- market data ----------

    def get_kline(self, symbol: str, bars: int = 120, ktype: KLType | None = None) -> pd.DataFrame:
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
            ret, data = self.trade.order_list_query(trd_env=_env_enum())
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
            ret, data = self.trade.order_list_query(trd_env=_env_enum())
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
        """Cancel a pending order. Returns True on success."""
        try:
            ret, data = self.trade.modify_order(
                modify_order_op=ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=_env_enum(),
            )
            if ret == RET_OK:
                _note_gated_op_ok()
            return ret == RET_OK
        except Exception as e:
            log.warning("cancel_order(%s) failed: %s", order_id, e)
            return False

    def list_pending_buys(self) -> "pd.DataFrame":
        """Pending BUY orders (with create_time for staleness check)."""
        ret, data = self.trade.order_list_query(trd_env=_env_enum())
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
        ret, data = self.trade.accinfo_query(trd_env=_env_enum())
        if ret != RET_OK:
            raise RuntimeError(f"accinfo_query failed: {data}")
        return float(data.iloc[0]["cash"])

    def get_positions(self) -> pd.DataFrame:
        ret, data = self.trade.position_list_query(trd_env=_env_enum())
        if ret != RET_OK:
            raise RuntimeError(f"position_list_query failed: {data}")
        return data

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
            ret, data = self.trade.deal_list_query(code=code, trd_env=_env_enum())
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
                code=code, start=start, end=end, trd_env=_env_enum())
            if ret == RET_OK:
                hit = _latest_sell(data)
                if hit and hit["price"] > 0:
                    return hit
        except Exception as e:
            log.debug("history_deal_list_query(%s) failed: %s", symbol, e)

        return None

    def get_pending_buy_value(self) -> float:
        """Sum of (price × qty) for BUY orders awaiting fill — counted as
        committed capital so we don't double-spend the budget."""
        ret, data = self.trade.order_list_query(trd_env=_env_enum())
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
        ret, data = self.trade.order_list_query(trd_env=_env_enum())
        if ret != RET_OK or data is None or data.empty:
            return set()
        pending = data[
            data["order_status"].isin(["SUBMITTED", "SUBMITTING", "WAITING_SUBMIT"])
            & (data["trd_side"] == "BUY")
        ]
        return {c.split(".")[-1] for c in pending["code"].tolist()}

    def place_limit_order(
        self, symbol: str, qty: int, price: float, side: TrdSide
    ) -> str:
        # US stocks require 2-decimal precision for price >= $1.
        rounded = round(price, 2) if price >= 1 else round(price, 4)
        ret, data = self.trade.place_order(
            price=rounded,
            qty=qty,
            code=self._format_code(symbol),
            trd_side=side,
            order_type=OrderType.NORMAL,
            trd_env=_env_enum(),
        )
        if ret != RET_OK:
            raise RuntimeError(f"place_order failed for {symbol}: {data}")
        order_id = str(data.iloc[0]["order_id"]).strip()
        # 2026-07-09: an order_id of 0/empty means the broker never actually
        # accepted the order (the OpenD-rs gateway returned a need_op_confirm
        # stub with order_id=0, then silently purged it — every "buy" became a
        # phantom entry that reconcile later booked as a fake MANUAL_SELL).
        # Treat it as a placement failure so no position is ever recorded.
        if order_id in ("", "0", "None", "nan"):
            raise RuntimeError(
                f"place_order for {symbol} returned invalid order_id={order_id!r} "
                "— order not accepted by broker/gateway, treating as failed"
            )
        _note_gated_op_ok()
        log.info("Placed %s %s qty=%s price=%.2f order_id=%s", side, symbol, qty, price, order_id)
        return order_id

    def place_stop_loss(self, symbol: str, qty: int, stop_price: float) -> str:
        """Sell-stop to close a long position."""
        rounded = round(stop_price, 2) if stop_price >= 1 else round(stop_price, 4)
        ret, data = self.trade.place_order(
            price=rounded,
            qty=qty,
            code=self._format_code(symbol),
            trd_side=TrdSide.SELL,
            order_type=OrderType.STOP,
            aux_price=rounded,
            trd_env=_env_enum(),
        )
        if ret != RET_OK:
            raise RuntimeError(f"stop-loss failed for {symbol}: {data}")
        order_id = str(data.iloc[0]["order_id"]).strip()
        if order_id in ("", "0", "None", "nan"):
            raise RuntimeError(
                f"stop-loss for {symbol} returned invalid order_id={order_id!r} "
                "— order not accepted by broker/gateway, treating as failed"
            )
        _note_gated_op_ok()
        return order_id

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
