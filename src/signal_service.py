"""Orchestration for the signal desk's forecasting stack.

This is the module that turns the ported pieces into one operation. It replaces
the Stock Probability Prediction Platform's `ForecastService` + `ScannerService`
pair, minus their process supervision: over there each forecast ran in a
disposable subprocess so a wedged model could not take the web app down with it.
Here the model is a few hundred milliseconds of numpy in the same process, and
the thing worth protecting is the broker connection, which `MooClient` already
supervises. So the subprocess machinery did not come across.

ONE FETCH, TWO WINDOWS. The engine trains on the last 10 sessions but scores
itself on a much longer one, and those used to be two separate broker requests.
`_validate_input(trim_latest_sessions=True)` already trims whatever it is handed
down to the last 10 sessions, so a single ~42-session fetch serves as both the
training input and the walk-forward input. That halves the request count, which
matters: the kline rate limiter is shared process-wide with the live trading
loop, and a scan that spends the budget is a scan that starves execution.

WHAT RUNS ON EVERY FORECAST, IN ORDER:
    fetch bars → run the engine → build the five context scores →
    apply the user-approved context weights → store the run (frozen per NY
    trading date) → settle whatever the newly-arrived bars now make settleable →
    once a week, propose parameter changes for the user to approve or reject.

Read-only with respect to the account. Nothing here places, modifies or cancels
an order, and no execution module imports it.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Optional

from .config import settings
from . import signal_bars, signal_context, signal_excursion, signal_forecast
from . import signal_news, signal_research, signal_scanner
from .signal_calendar import NEW_YORK, is_session
from .signal_store import SignalResearchStore

log = logging.getLogger(__name__)

HORIZON = "3D"
# Sessions fetched per symbol — the broker's ceiling for 30-minute ETH history.
#
# This is now the TRAINING window too, not just the walk-forward window. Ten
# sessions gave the model ~160 training origins overlapping by 95 of 96 bars,
# and it responded by memorising them: out-of-sample R^2 of -1.84, three-day
# predictions of -11%, probabilities of 99%. The same estimator on the full
# window scores -0.10 and predicts -1%. Depth is doing more for this model than
# any change of algorithm did — see scripts/signal_forecast_study.py.
#
# Cost is one chunked kline request per symbol (~1.3s) and ~2.5s of numpy, and
# the walk-forward still stops at MAX_BACKTEST_SAMPLES origins.
BACKTEST_SESSIONS = 249
TRAINING_SESSIONS = 10
# Discovery names scanned beyond the signal watchlist.
DISCOVERY_LIMIT = 12


def _watchlist() -> list[str]:
    """The desk's starred symbols, read straight from the config file.

    This used to go through `signal_reporter.load_watchlist()`. That module —
    the old technical watch station and its Telegram pushes — has been removed,
    but the watchlist itself is still the list a user curates, and the web
    editor at /api/signal-watchlist still writes it. Reading the file directly
    is all the coupling that was ever needed.
    """
    import json
    path = settings.root / "config" / "signal_watchlist.json"
    try:
        raw = json.loads(path.read_text()).get("tickers") or []
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("signal_service: signal watchlist unreadable: %s", exc)
        return []
    seen, out = set(), []
    for item in raw:
        symbol = str(item).strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def _discovery_pool(limit: int) -> tuple[list[str], str]:
    """Names to scan beyond the watchlist, from the live momentum universe."""
    import json
    path = settings.root / "config" / "watchlist.json"
    try:
        tickers = json.loads(path.read_text()).get("tickers") or []
        return [str(item).upper() for item in tickers][:limit], "live_universe_watchlist"
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("signal_service: discovery pool unreadable: %s", exc)
        return [], "unavailable"


class SignalForecastService:
    """Forecasts, scans and the learning loop, over one broker client."""

    def __init__(self, store: Optional[SignalResearchStore] = None):
        self.store = store or SignalResearchStore(settings.root)
        self._lock = threading.RLock()
        self._cache: dict[tuple, dict[str, Any]] = {}
        self._scan_lock = threading.Lock()

    # ── forecast ────────────────────────────────────────────────────────────
    def forecast(self, client, symbol: str, *, include_news: bool = True,
                 include_broker: bool = True, force: bool = False,
                 bars: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        """One symbol's three-day forecast, with context, stored and settled.

        Never raises on a data problem: a refusal comes back as a payload with
        `ok=False` and the engine's machine-readable code, because "we will not
        forecast this" is a result the panel has to render, not an exception.

        `bars` lets a caller that has already paid for this symbol's history hand
        it over instead of fetching it again. The scanner does exactly that: the
        kline rate limiter is shared with the live trading loop, so a fourteen-
        symbol scan re-fetching the same 1300 bars twice per symbol is budget
        taken directly out of execution.
        """
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return self._refusal(symbol, "invalid_symbol", "股票代码为空。")

        if bars is None:
            try:
                bars = signal_bars.fetch_30m(client, symbol, sessions=BACKTEST_SESSIONS)
            except Exception as exc:
                log.warning("signal_service: kline fetch failed for %s: %s", symbol, exc)
                return self._refusal(symbol, "market_unavailable", f"行情获取失败：{exc}")

        as_of = signal_bars.market_as_of(bars)
        if not as_of:
            return self._refusal(symbol, "insufficient_data", "没有已完成的30分钟K线。",
                                 coverage=signal_bars.session_coverage(bars))

        key = (symbol, HORIZON, as_of, include_news, include_broker)
        if not force:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                return {**cached, "cache": {"hit": True, "market_as_of": as_of}}

        # Store the bars first: settlement joins on them, so a forecast made now
        # can be scored later even if nothing else runs in between.
        try:
            self.store.record_actual_bars(symbol, bars)
        except Exception as exc:
            log.warning("signal_service: record_actual_bars failed for %s: %s", symbol, exc)

        try:
            payload = signal_forecast.generate_forecast(
                symbol=symbol, horizon=HORIZON,
                bars_30m=bars,                 # trimmed to the last 10 sessions
                market_as_of=as_of,
                backtest_bars_30m=bars,        # scored over the whole span
            )
        except signal_forecast.ForecastInputError as exc:
            return self._refusal(symbol, exc.code, exc.message,
                                 coverage=signal_bars.session_coverage(bars))
        except Exception as exc:
            log.exception("signal_service: forecast failed for %s", symbol)
            return self._refusal(symbol, "model_failure", f"预测计算失败：{exc}")

        context = self._context(client, symbol, include_news, include_broker)
        origin_close = float(bars[-1]["close"])
        try:
            payload = self.store.apply_active_context(payload, origin_close, context)
        except Exception as exc:
            log.warning("signal_service: context weighting failed for %s: %s", symbol, exc)

        run_id = None
        try:
            run_id = self.store.record_forecast(payload)
            self.store.settle_forecasts(symbol)
        except Exception as exc:
            log.warning("signal_service: store write failed for %s: %s", symbol, exc)

        # The last three sessions of real bars travel with the forecast so the
        # panel can draw one continuous line — history into projection — instead
        # of a cone floating with nothing to anchor it. Three sessions is the
        # same span the forecast covers, which keeps the two halves of the chart
        # at a comparable scale.
        recent = [
            {"time": bar["time"], "close": bar["close"],
             "high": bar["high"], "low": bar["low"]}
            for bar in bars[-96:]
        ]
        result = {
            "ok": True,
            **payload,
            "recent_30m": recent,
            "run_id": run_id,
            "coverage": signal_bars.session_coverage(bars),
            "learning": self._safe(lambda: self.store.learning_summary(symbol, HORIZON), {}),
            "cache": {"hit": False, "market_as_of": as_of},
        }
        with self._lock:
            self._cache[key] = result
            if len(self._cache) > 64:
                self._cache.pop(next(iter(self._cache)))
        return result

    def _context(self, client, symbol: str, include_news: bool,
                 include_broker: bool) -> dict[str, Any]:
        bars_5m: list[dict[str, Any]] = []
        try:
            bars_5m = signal_bars.fetch_5m(client, symbol, sessions=2)
        except Exception as exc:
            log.warning("signal_service: 5m fetch failed for %s: %s", symbol, exc)

        news_items: list[dict[str, Any]] = []
        if include_news:
            try:
                news_items = signal_news.analyze(symbol)["items"]
            except Exception as exc:
                log.warning("signal_service: news failed for %s: %s", symbol, exc)

        broker_snapshot = None
        if include_broker:
            try:
                broker_snapshot = signal_research.snapshot(client, symbol)
            except Exception as exc:
                log.warning("signal_service: broker snapshot failed for %s: %s", symbol, exc)

        return signal_context.build_context(
            bars_5m=bars_5m, news_items=news_items, broker_snapshot=broker_snapshot,
            include_news=include_news, include_broker=include_broker,
        )

    @staticmethod
    def _refusal(symbol: str, code: str, message: str, **extra) -> dict[str, Any]:
        return {
            "ok": False, "symbol": symbol, "horizon": HORIZON,
            "error": {"code": code, "message": message},
            "quality": {"status": "unavailable", "actionable": False,
                        "reasons": [message]},
            **extra,
        }

    @staticmethod
    def _safe(call, fallback):
        try:
            return call()
        except Exception:
            return fallback

    # ── scanner ─────────────────────────────────────────────────────────────
    def scan(self, client, *, force: bool = False) -> dict[str, Any]:
        """Rank the watchlist and a bounded discovery pool. Cached once daily."""
        scanner_day = datetime.now(NEW_YORK).date().isoformat()
        if not force:
            cached = self._safe(lambda: self.store.scanner_snapshot(scanner_day), None)
            if isinstance(cached, dict):
                return {**cached, "daily_cache": {"hit": True, "scanner_day": scanner_day,
                                                  "policy": "once_daily_unless_manual"}}
        with self._scan_lock:
            return self._compute_scan(client, scanner_day, force)

    def _compute_scan(self, client, scanner_day: str, forced: bool) -> dict[str, Any]:
        starred = _watchlist()
        discovery, source = _discovery_pool(DISCOVERY_LIMIT)
        discovery = [item for item in discovery if item not in starred]
        warnings: list[str] = []
        if not is_session(datetime.now(NEW_YORK).date()):
            warnings.append("今天不是美股交易日；行情停留在最近一个交易日。")
        if not starred:
            warnings.append("signal watchlist 为空；只扫描了动态候选。")

        rows: list[dict[str, Any]] = []
        for symbol in starred + discovery:
            origin = "starred" if symbol in starred else "discovery"
            try:
                bars = signal_bars.fetch_30m(client, symbol, sessions=BACKTEST_SESSIONS)
            except Exception as exc:
                warnings.append(f"{symbol}：行情获取失败 {exc}")
                rows.append({"symbol": symbol, "origin": origin, "status": "NO_TRADE",
                             "score": 0.0, "indicators": {}, "plan": {},
                             "reasons": [f"行情获取失败：{exc}"]})
                continue
            # News and the broker research snapshot are deliberately OFF here.
            # The platform did the same: a scan touches a dozen symbols and each
            # news call is a network round trip, so the scan reads price only and
            # the detail panel is where the full context gets built.
            forecast = self.forecast(client, symbol, include_news=False,
                                     include_broker=False, bars=bars)
            if not forecast.get("ok"):
                warnings.append(f"{symbol}：{forecast.get('error', {}).get('message', '预测不可用')}")
                rows.append({"symbol": symbol, "origin": origin, "status": "NO_TRADE",
                             "score": 0.0, "indicators": {}, "plan": {},
                             "reasons": [forecast.get("error", {}).get("message", "预测不可用")]})
                continue
            # The excursion estimate reads the SAME long history — it needs the
            # full span, because its quantiles come from non-overlapping
            # three-day windows and ten sessions hold barely three of them.
            excursion = None
            try:
                excursion = signal_excursion.estimate(
                    symbol=symbol, bars_30m=bars,
                    market_as_of=signal_bars.market_as_of(bars))
            except Exception as exc:
                log.warning("signal_service: excursion failed for %s: %s", symbol, exc)
            # `analyze_bars` keeps only the newest three sessions itself, so the
            # long history already in hand is the right input.
            rows.append(signal_scanner.score_row(symbol, bars, forecast, origin,
                                                 excursion=excursion))

        payload = signal_scanner.build_payload(
            rows, scanner_day=scanner_day,
            universe_size=len(starred) + len(discovery),
            discovery_screened=len(discovery), warnings=warnings[:20],
            forced=forced, discovery_source=source,
        )
        self._safe(lambda: self.store.save_scanner_snapshot(scanner_day, payload), None)
        return payload

    # ── learning loop ───────────────────────────────────────────────────────
    def settle(self, client, symbol: str) -> dict[str, Any]:
        """Pull the latest bars and score every forecast they now make settleable."""
        symbol = str(symbol or "").strip().upper()
        try:
            bars = signal_bars.fetch_30m(client, symbol, sessions=TRAINING_SESSIONS)
            recorded = self.store.record_actual_bars(symbol, bars)
        except Exception as exc:
            return {"ok": False, "symbol": symbol, "error": str(exc)}
        settled = self.store.settle_forecasts(symbol)
        return {"ok": True, "symbol": symbol, "bars_recorded": recorded,
                "runs_settled": settled,
                "learning": self.store.learning_summary(symbol, HORIZON)}

    def optimizer_status(self, symbol: str) -> dict[str, Any]:
        return self.store.optimization_status(str(symbol).upper(), HORIZON)

    def propose(self, symbol: str, *, force: bool = False) -> dict[str, Any]:
        return self.store.generate_parameter_proposal(str(symbol).upper(), HORIZON,
                                                      force=force)

    def decide(self, proposal_id: str, approve: bool) -> dict[str, Any]:
        return self.store.decide_parameter_proposal(str(proposal_id), bool(approve))

    def history(self, symbol: str, limit: int = 60) -> list[dict[str, Any]]:
        return self.store.forecast_history(str(symbol).upper(), HORIZON, limit=limit)

    def health(self) -> dict[str, Any]:
        from . import news_score_local
        finbert_ok, finbert_detail = news_score_local.available()
        return {
            "store": self._safe(self.store.health, {}),
            "horizon": HORIZON,
            "backtest_sessions": BACKTEST_SESSIONS,
            "training_sessions": TRAINING_SESSIONS,
            "finbert": {"available": finbert_ok, "detail": finbert_detail},
            "tavily_configured": bool(settings.tavily_key),
            "model": {"name": signal_forecast.MODEL_NAME,
                      "version": signal_forecast.MODEL_VERSION},
            "actionable": False,
        }

    def close(self) -> None:
        self._safe(self.store.close, None)


_service: Optional[SignalForecastService] = None
_service_lock = threading.Lock()


def service() -> SignalForecastService:
    """Process-wide singleton — the SQLite store should have one owner."""
    global _service
    with _service_lock:
        if _service is None:
            _service = SignalForecastService()
        return _service


__all__ = ["HORIZON", "SignalForecastService", "service"]
