"""Durable research archive for the signal desk's forecasting stack.

Ported from the Stock Probability Prediction Platform's `companion/local_store.py`.
The schema and the settlement maths are unchanged; what changed is where the
database lives (this project's `data/`, not the platform's `.local-data/`) and
the class name.

WHAT IT IS FOR. A forecast that is never scored against what happened is not
research, it is decoration. This module is the part that makes the loop close:

  record_forecast()          freeze one forecast per symbol per NY trading date
  record_actual_bars()       store the bars that later arrived
  settle_forecasts()         join the two and write down what the error was
  learning_summary()         aggregate those errors into hit rates and MAE
  generate_parameter_proposal()  once a week, propose explicit from-to changes
  decide_parameter_proposal()    apply them only when the user approves

IMMUTABILITY IS THE POINT. A forecast may be revised freely while its New York
market date is still current. Once the next calendar day starts, that row is
frozen — reopening the dashboard on a Sunday must not quietly rewrite Friday's
prediction into something that looks better against Friday's outcome. Without
that rule every accuracy number this module reports would be unfalsifiable.

NOTHING HERE CHANGES A PARAMETER ON ITS OWN. The weekly optimizer writes a
proposal with an explicit from-to for each weight and stops. `decide_parameter_
proposal` is the only path that activates a new version, and only a user calls
it. The diagnostic labels it reasons from are correlational hypotheses and are
recorded as such — `causal_claim` is stored False on every settled run.

Standard library only, and no execution path imports this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .signal_calendar import NEW_YORK


SCHEMA_VERSION = "1.0"
OPTIMIZATION_INTERVAL_DAYS = 7
DEFAULT_CONTEXT_PARAMETERS = {
    "five_minute_weight_pp": 0.35,
    "news_weight_pp": 0.20,
    "options_weight_pp": 0.10,
    "flow_weight_pp": 0.20,
    "short_weight_pp": 0.10,
    "max_terminal_adjustment_pp": 1.50,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def canonical_url(url: str) -> str:
    """Canonical enough for local versioning without hiding distinct stories."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url.strip())
    ignored = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in ignored])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", query, ""))


class SignalResearchStore:
    """Thread-safe SQLite archive for forecasts, their outcomes, and news."""

    def __init__(self, root: Path, database_path: Path | None = None):
        configured = os.getenv("SIGNAL_RESEARCH_DB", "").strip()
        if database_path is not None:
            selected = database_path
        elif configured:
            candidate = Path(configured).expanduser()
            selected = candidate if candidate.is_absolute() else root / candidate
        else:
            # Alongside the signal desk's other state (signal_monitor_state.json,
            # signal_alerts.json) rather than in a hidden directory of its own.
            selected = root / "data" / "signal_research.sqlite3"
        self.path = selected.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _migrate(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS actual_bars (
                    symbol TEXT NOT NULL,
                    bar_time TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    PRIMARY KEY(symbol, bar_time)
                );
                CREATE TABLE IF NOT EXISTS forecast_runs (
                    run_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    forecast_day TEXT,
                    market_as_of TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    terminal_at TEXT NOT NULL,
                    terminal_q10 REAL NOT NULL,
                    terminal_q50 REAL NOT NULL,
                    terminal_q90 REAL NOT NULL,
                    model_name TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    model_json TEXT NOT NULL,
                    probabilities_json TEXT NOT NULL,
                    backtest_json TEXT NOT NULL,
                    quality_json TEXT NOT NULL,
                    forecast_json TEXT NOT NULL,
                    learning_json TEXT,
                    settled_at TEXT,
                    UNIQUE(symbol, horizon, market_as_of, model_name, model_version)
                );
                CREATE INDEX IF NOT EXISTS forecast_runs_lookup
                    ON forecast_runs(symbol, horizon, generated_at DESC);
                CREATE TABLE IF NOT EXISTS news_items (
                    news_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_domain TEXT,
                    published_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    finbert_json TEXT,
                    scores_json TEXT,
                    UNIQUE(symbol, canonical_url, content_hash)
                );
                CREATE INDEX IF NOT EXISTS news_items_pit
                    ON news_items(symbol, available_at DESC);
                CREATE TABLE IF NOT EXISTS forecast_news_links (
                    run_id TEXT NOT NULL REFERENCES forecast_runs(run_id) ON DELETE CASCADE,
                    news_id TEXT NOT NULL REFERENCES news_items(news_id) ON DELETE CASCADE,
                    PRIMARY KEY(run_id, news_id)
                );
                CREATE TABLE IF NOT EXISTS watchlist (
                    symbol TEXT PRIMARY KEY,
                    company TEXT NOT NULL,
                    starred_at TEXT NOT NULL,
                    last_refresh_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS refresh_state (
                    task_key TEXT PRIMARY KEY,
                    completed_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scanner_daily_runs (
                    scanner_day TEXT PRIMARY KEY,
                    generated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_versions (
                    version_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    approved_at TEXT,
                    source_proposal_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
                    UNIQUE(symbol,horizon,model_version)
                );
                CREATE INDEX IF NOT EXISTS model_versions_active
                    ON model_versions(symbol,horizon,status);
                CREATE TABLE IF NOT EXISTS parameter_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    next_allowed_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
                    current_version TEXT NOT NULL,
                    suggested_version TEXT NOT NULL,
                    current_json TEXT NOT NULL,
                    suggested_json TEXT NOT NULL,
                    reasoning_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS parameter_proposals_lookup
                    ON parameter_proposals(symbol,horizon,created_at DESC);
                CREATE TABLE IF NOT EXISTS news_selections (
                    symbol TEXT NOT NULL,
                    news_id TEXT NOT NULL REFERENCES news_items(news_id) ON DELETE CASCADE,
                    included INTEGER NOT NULL CHECK(included IN (0,1)),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(symbol,news_id)
                );
                CREATE TABLE IF NOT EXISTS news_analyses (
                    analysis_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    cutoff_at TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    analysis_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS news_analyses_lookup
                    ON news_analyses(symbol,cutoff_at DESC);
                """
            )
            columns = {row[1] for row in self._connection.execute("PRAGMA table_info(forecast_runs)")}
            if "context_json" not in columns:
                self._connection.execute("ALTER TABLE forecast_runs ADD COLUMN context_json TEXT")
            if "forecast_day" not in columns:
                self._connection.execute("ALTER TABLE forecast_runs ADD COLUMN forecast_day TEXT")
            watchlist_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(watchlist)")}
            if "exchange" not in watchlist_columns:
                self._connection.execute("ALTER TABLE watchlist ADD COLUMN exchange TEXT")
            if "base_price" not in watchlist_columns:
                self._connection.execute("ALTER TABLE watchlist ADD COLUMN base_price REAL")
            run_dates = self._connection.execute(
                "SELECT run_id,market_as_of,generated_at FROM forecast_runs"
            ).fetchall()
            for row in run_dates:
                parsed = _parse_time(row["market_as_of"]) or _parse_time(row["generated_at"])
                if parsed is not None:
                    self._connection.execute(
                        "UPDATE forecast_runs SET forecast_day=? WHERE run_id=?",
                        (parsed.astimezone(NEW_YORK).date().isoformat(), row["run_id"]),
                    )
            duplicates = self._connection.execute(
                """SELECT symbol,horizon,forecast_day FROM forecast_runs
                   WHERE forecast_day IS NOT NULL
                   GROUP BY symbol,horizon,forecast_day HAVING COUNT(*)>1"""
            ).fetchall()
            for duplicate in duplicates:
                rows = self._connection.execute(
                    """SELECT run_id FROM forecast_runs
                       WHERE symbol=? AND horizon=? AND forecast_day=?
                       ORDER BY generated_at DESC, rowid DESC""",
                    (duplicate["symbol"], duplicate["horizon"], duplicate["forecast_day"]),
                ).fetchall()
                self._connection.executemany(
                    "DELETE FROM forecast_runs WHERE run_id=?",
                    [(row["run_id"],) for row in rows[1:]],
                )
            self._connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_forecast_runs_daily_latest
                   ON forecast_runs(symbol,horizon,forecast_day)
                   WHERE forecast_day IS NOT NULL"""
            )
            self._connection.execute("PRAGMA optimize")

    def health(self) -> dict[str, Any]:
        with self._lock:
            counts = {}
            for table in ("forecast_runs", "actual_bars", "news_items", "watchlist", "parameter_proposals", "model_versions"):
                counts[table] = int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return {
            "path": str(self.path), "counts": counts,
            "daily_latest_forecasts": True, "immutable_after_trading_day": True,
        }

    def record_actual_bars(self, symbol: str, bars: Iterable[dict[str, Any]], observed_at: str | None = None) -> int:
        observed = observed_at or utc_now()
        rows: list[tuple[Any, ...]] = []
        for bar in bars:
            if not isinstance(bar, dict) or bar.get("is_complete") is not True:
                continue
            values = [_finite(bar.get(key)) for key in ("open", "high", "low", "close", "volume")]
            bar_time = str(bar.get("time") or "")
            if any(value is None for value in values) or _parse_time(bar_time) is None:
                continue
            open_price, high, low, close, volume = values
            if min(open_price, high, low, close) <= 0 or volume < 0 or low > min(open_price, close) or high < max(open_price, close):
                continue
            rows.append((symbol, bar_time, observed, open_price, high, low, close, volume))
        if not rows:
            return 0
        with self._lock, self._connection:
            self._connection.executemany(
                """INSERT INTO actual_bars(symbol,bar_time,observed_at,open,high,low,close,volume)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,bar_time) DO UPDATE SET
                     observed_at=excluded.observed_at, open=excluded.open, high=excluded.high,
                     low=excluded.low, close=excluded.close, volume=excluded.volume""",
                rows,
            )
        return len(rows)

    def record_forecast(self, payload: dict[str, Any]) -> str | None:
        bars = payload.get("forecast_30m")
        model = payload.get("model")
        if not isinstance(bars, list) or not bars or not isinstance(model, dict):
            return None
        terminal = bars[-1]
        market_time = _parse_time(payload.get("market_as_of"))
        generated_time = _parse_time(payload.get("generated_at"))
        if market_time is None or generated_time is None:
            return None
        forecast_day = market_time.astimezone(NEW_YORK).date().isoformat()
        generated_day = generated_time.astimezone(NEW_YORK).date().isoformat()
        daily_identity = "|".join([str(payload.get("symbol", "")), str(payload.get("horizon", "")), forecast_day])
        default_run_id = hashlib.sha256(daily_identity.encode("utf-8")).hexdigest()[:32]
        with self._lock:
            existing = self._connection.execute(
                "SELECT run_id FROM forecast_runs WHERE symbol=? AND horizon=? AND forecast_day=?",
                (payload["symbol"], payload["horizon"], forecast_day),
            ).fetchone()
        run_id = str(existing["run_id"]) if existing is not None else default_run_id
        # A forecast may be revised repeatedly while its New York market date
        # is still current.  Once a later calendar day begins, an existing row
        # for that market date is immutable (for example, reopening on a
        # weekend must not rewrite Friday's frozen version).
        if existing is not None and generated_day > forecast_day:
            return run_id
        row = (
            run_id, payload["symbol"], payload["horizon"], forecast_day, payload["market_as_of"], payload["generated_at"],
            terminal["time"], float(terminal["q10"]), float(terminal["q50"]), float(terminal["q90"]),
            str(model.get("name", "unknown")), str(model.get("version", "unknown")), _json(model),
            _json(payload.get("probabilities")), _json(payload.get("backtest")), _json(payload.get("quality")), _json(bars),
            _json(payload.get("context")) if payload.get("context") is not None else None,
        )
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO forecast_runs(
                   run_id,symbol,horizon,forecast_day,market_as_of,generated_at,terminal_at,terminal_q10,terminal_q50,terminal_q90,
                   model_name,model_version,model_json,probabilities_json,backtest_json,quality_json,forecast_json,context_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     market_as_of=excluded.market_as_of, generated_at=excluded.generated_at,
                     terminal_at=excluded.terminal_at, terminal_q10=excluded.terminal_q10,
                     terminal_q50=excluded.terminal_q50, terminal_q90=excluded.terminal_q90,
                     model_name=excluded.model_name, model_version=excluded.model_version,
                     model_json=excluded.model_json, probabilities_json=excluded.probabilities_json,
                     backtest_json=excluded.backtest_json, quality_json=excluded.quality_json,
                     forecast_json=excluded.forecast_json, context_json=excluded.context_json,
                     learning_json=NULL, settled_at=NULL""",
                row,
            )
            self._connection.execute("DELETE FROM forecast_news_links WHERE run_id=?", (run_id,))
            self._connection.execute(
                """INSERT OR IGNORE INTO forecast_news_links(run_id,news_id)
                   SELECT ?, n.news_id FROM news_items n
                   LEFT JOIN news_selections s ON s.symbol=n.symbol AND s.news_id=n.news_id
                   WHERE n.symbol=? AND n.available_at<=? AND n.published_at>=datetime(?, '-3 days')
                     AND COALESCE(s.included,1)=1""",
                (run_id, payload["symbol"], payload["generated_at"], payload["generated_at"]),
            )
        return run_id

    def settle_forecasts(self, symbol: str) -> int:
        with self._lock:
            pending = self._connection.execute(
                """SELECT r.*, origin.close AS origin_close, terminal.close AS actual_close,
                          (SELECT COUNT(*) FROM forecast_news_links l WHERE l.run_id=r.run_id) AS news_count,
                          (SELECT COUNT(*) FROM news_items n WHERE n.symbol=r.symbol
                             AND n.available_at>r.generated_at AND n.available_at<=r.terminal_at) AS late_news_count
                   FROM forecast_runs r
                   JOIN actual_bars origin ON origin.symbol=r.symbol AND origin.bar_time=r.market_as_of
                   JOIN actual_bars terminal ON terminal.symbol=r.symbol AND terminal.bar_time=r.terminal_at
                   WHERE r.symbol=? AND r.settled_at IS NULL""",
                (symbol,),
            ).fetchall()
        updates: list[tuple[str, str, str]] = []
        for row in pending:
            origin = float(row["origin_close"])
            actual = float(row["actual_close"])
            predicted = float(row["terminal_q50"])
            predicted_return = predicted / origin - 1.0
            actual_return = actual / origin - 1.0
            threshold = 0.0025
            predicted_direction = 1 if predicted_return > threshold else -1 if predicted_return < -threshold else 0
            actual_direction = 1 if actual_return > threshold else -1 if actual_return < -threshold else 0
            error_pp = (predicted_return - actual_return) * 100.0
            interval_hit = float(row["terminal_q10"]) <= actual <= float(row["terminal_q90"])
            reasons: list[str] = []
            if predicted_direction != actual_direction:
                reasons.append("direction_miss")
            if not interval_hit:
                reasons.append("outside_prediction_interval")
            if abs(error_pp) >= 2.0:
                reasons.append("magnitude_miss")
            if int(row["late_news_count"]) > 0 and (predicted_direction != actual_direction or abs(error_pp) >= 1.0):
                reasons.append("post_forecast_news_possible_driver")
            if not reasons:
                reasons.append("within_expected_error")
            forecast_path = _loads(row["forecast_json"], [])
            forecast_times = [str(item.get("time") or "") for item in forecast_path if isinstance(item, dict)]
            actual_path: list[dict[str, Any]] = []
            if forecast_times:
                placeholders = ",".join("?" for _ in forecast_times)
                with self._lock:
                    actual_rows = self._connection.execute(
                        f"SELECT bar_time,close FROM actual_bars WHERE symbol=? AND bar_time IN ({placeholders})",
                        (row["symbol"], *forecast_times),
                    ).fetchall()
                actual_by_time = {item["bar_time"]: float(item["close"]) for item in actual_rows}
                for point in forecast_path:
                    if not isinstance(point, dict) or str(point.get("time") or "") not in actual_by_time:
                        continue
                    point_time = str(point["time"])
                    point_actual = actual_by_time[point_time]
                    actual_path.append({
                        "time": point_time,
                        "actual_close": round(point_actual, 6),
                        "predicted_q50": round(float(point["q50"]), 6),
                        "absolute_error_percent": round(abs(float(point["q50"]) / point_actual - 1.0) * 100.0, 4),
                        "interval_hit": float(point["q10"]) <= point_actual <= float(point["q90"]),
                    })
            path_mae = (
                sum(item["absolute_error_percent"] for item in actual_path) / len(actual_path)
                if actual_path else None
            )
            context = _loads(row["context_json"], {})
            contributions = context.get("contributions_pp") if isinstance(context, dict) else {}
            factor_diagnosis: list[dict[str, Any]] = []
            if isinstance(contributions, dict):
                for factor, raw_contribution in contributions.items():
                    contribution = _finite(raw_contribution)
                    if contribution is None or abs(contribution) < 0.02:
                        continue
                    aligned_with_error = (error_pp > 0 and contribution > 0) or (error_pp < 0 and contribution < 0)
                    factor_diagnosis.append({
                        "factor": str(factor), "contribution_pp": round(contribution, 6),
                        "hypothesis": "possible_overweight" if aligned_with_error else "possible_underweight_or_offset",
                        "confidence": "low" if abs(error_pp) < 1 else "medium",
                    })
            learning = {
                "status": "settled",
                "origin_close": round(origin, 6),
                "actual_terminal_close": round(actual, 6),
                "predicted_terminal_close": round(predicted, 6),
                "predicted_return_percent": round(predicted_return * 100, 4),
                "actual_return_percent": round(actual_return * 100, 4),
                "signed_error_percentage_points": round(error_pp, 4),
                "absolute_error_percentage_points": round(abs(error_pp), 4),
                "direction_hit": predicted_direction == actual_direction,
                "interval_hit": interval_hit,
                "diagnostic_hypotheses": reasons,
                "news_context_count": int(row["news_count"]),
                "post_forecast_news_count": int(row["late_news_count"]),
                "actual_path": actual_path,
                "path_points_settled": len(actual_path),
                "path_mae_percent": round(path_mae, 4) if path_mae is not None else None,
                "error_analysis": {
                    "method": "local_explainable_factor_error_model_v1",
                    "factor_hypotheses": factor_diagnosis,
                    "late_news_possible_driver": int(row["late_news_count"]) > 0,
                    "interpretation": "相关性诊断，用于生成每周参数建议；不宣称因果。",
                },
                "causal_claim": False,
            }
            updates.append((_json(learning), utc_now(), row["run_id"]))
        if updates:
            with self._lock, self._connection:
                self._connection.executemany(
                    "UPDATE forecast_runs SET learning_json=?, settled_at=? WHERE run_id=? AND settled_at IS NULL",
                    updates,
                )
        return len(updates)

    def forecast_history(self, symbol: str, horizon: str, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._connection.execute(
                """SELECT run_id,symbol,horizon,forecast_day,market_as_of,generated_at,terminal_at,model_json,
                          probabilities_json,backtest_json,quality_json,forecast_json,context_json,learning_json,settled_at,
                          (SELECT COUNT(*) FROM forecast_news_links l WHERE l.run_id=r.run_id) AS news_count
                   FROM forecast_runs r WHERE symbol=? AND horizon=?
                   ORDER BY generated_at DESC LIMIT ?""",
                (symbol, horizon, limit),
            ).fetchall()
        return [{
            "run_id": row["run_id"], "symbol": row["symbol"], "horizon": row["horizon"],
            "forecast_day": row["forecast_day"],
            "market_as_of": row["market_as_of"], "generated_at": row["generated_at"], "terminal_at": row["terminal_at"],
            "model": _loads(row["model_json"], {}), "probabilities": _loads(row["probabilities_json"], {}),
            "backtest": _loads(row["backtest_json"], {}), "quality": _loads(row["quality_json"], {}),
            "context": _loads(row["context_json"], None),
            "forecast_30m": _loads(row["forecast_json"], []), "learning": _loads(row["learning_json"], None),
            "settled_at": row["settled_at"], "news_count": int(row["news_count"]),
        } for row in rows]

    def actual_history(self, symbol: str, limit: int = 2600) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT bar_time,open,high,low,close,volume FROM actual_bars
                   WHERE symbol=? ORDER BY bar_time DESC LIMIT ?""",
                (symbol, max(1, min(int(limit), 5000))),
            ).fetchall()
        return [{"time": item["bar_time"], "open": item["open"], "high": item["high"],
                 "low": item["low"], "close": item["close"], "volume": item["volume"]}
                for item in reversed(rows)]

    def learning_summary(self, symbol: str, horizon: str) -> dict[str, Any]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT learning_json FROM forecast_runs WHERE symbol=? AND horizon=? AND learning_json IS NOT NULL ORDER BY settled_at",
                (symbol, horizon),
            ).fetchall()
        samples = [_loads(row["learning_json"], {}) for row in rows]
        errors = [item.get("absolute_error_percentage_points") for item in samples]
        valid_errors = [float(value) for value in errors if _finite(value) is not None]
        direction_hits = [bool(item.get("direction_hit")) for item in samples]
        interval_hits = [bool(item.get("interval_hit")) for item in samples]
        minimum = 20
        raw_oos: list[float] = []
        corrected_oos: list[float] = []
        signed_errors = [
            float(item["signed_error_percentage_points"])
            for item in samples if _finite(item.get("signed_error_percentage_points")) is not None
        ]
        minimum_training = 10
        for index in range(minimum_training, len(signed_errors)):
            historical = signed_errors[:index]
            correction = -sum(historical[-20:]) / min(20, len(historical))
            cap = 3.0
            correction = max(-cap, min(cap, correction))
            raw_oos.append(abs(signed_errors[index]))
            corrected_oos.append(abs(signed_errors[index] + correction))
        raw_oos_mae = sum(raw_oos) / len(raw_oos) if raw_oos else None
        corrected_oos_mae = sum(corrected_oos) / len(corrected_oos) if corrected_oos else None
        improvement = (
            (raw_oos_mae - corrected_oos_mae) / raw_oos_mae * 100.0
            if raw_oos_mae and corrected_oos_mae is not None else None
        )
        candidate_ready = len(samples) >= minimum and improvement is not None and improvement >= 2.0
        cap = 3.0
        recommended_adjustment = (
            max(-cap, min(cap, -sum(signed_errors[-20:]) / min(20, len(signed_errors))))
            if signed_errors else 0.0
        )
        pending = self.latest_parameter_proposal(symbol, horizon)
        active_version = self.active_model_version(symbol, horizon)
        optimizer_active = bool(active_version.get("source_proposal_id"))
        status = "pending_approval" if pending and pending.get("status") == "pending" else "active" if optimizer_active else "monitoring" if len(samples) < minimum else "candidate_ready" if candidate_ready else "rejected_by_walk_forward"
        return {
            "status": status,
            "samples": len(samples),
            "minimum_samples_for_optimizer": minimum,
            "mae_percentage_points": round(sum(valid_errors) / len(valid_errors), 4) if valid_errors else None,
            "direction_accuracy_percent": round(sum(direction_hits) / len(direction_hits) * 100, 2) if direction_hits else None,
            "interval_coverage_percent": round(sum(interval_hits) / len(interval_hits) * 100, 2) if interval_hits else None,
            "optimizer_active": optimizer_active,
            "candidate_ready": candidate_ready,
            "active_model_version": active_version.get("model_version"),
            "pending_proposal_id": pending.get("proposal_id") if pending and pending.get("status") == "pending" else None,
            "recommended_terminal_adjustment_percentage_points": round(recommended_adjustment, 4),
            "raw_oos_mae_percentage_points": round(raw_oos_mae, 4) if raw_oos_mae is not None else None,
            "corrected_oos_mae_percentage_points": round(corrected_oos_mae, 4) if corrected_oos_mae is not None else None,
            "oos_mae_improvement_percent": round(improvement, 3) if improvement is not None else None,
            "adjustment_cap_percentage_points": cap,
            "activation_rule": "expanding walk-forward MAE improvement >= 2% with at least 20 settled samples",
            "note": "诊断是假设标签，不是因果证明；当前不会自动改写历史预测。",
        }

    @staticmethod
    def _next_model_version(current: str) -> str:
        prefix, separator, tail = current.rpartition(".")
        if separator and tail.isdigit():
            return f"{prefix}.{int(tail) + 1}"
        return f"{current}.1"

    def active_model_version(self, symbol: str, horizon: str) -> dict[str, Any]:
        with self._lock, self._connection:
            row = self._connection.execute(
                """SELECT * FROM model_versions WHERE symbol=? AND horizon=? AND status='active'
                   ORDER BY created_at DESC LIMIT 1""", (symbol, horizon),
            ).fetchone()
            if row is None:
                created = utc_now()
                # "SIG" for the signal desk; the platform shipped this as
                # "PL-CTX" (PulseLens) and the label is user-visible in the
                # optimizer panel, so it should name where it actually lives.
                version = "SIG-CTX-1.0.0"
                version_id = hashlib.sha256(f"{symbol}|{horizon}|{version}".encode()).hexdigest()[:32]
                self._connection.execute(
                    """INSERT INTO model_versions(version_id,symbol,horizon,model_version,parameters_json,
                       created_at,approved_at,source_proposal_id,status) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (version_id, symbol, horizon, version, _json(DEFAULT_CONTEXT_PARAMETERS), created, created, None, "active"),
                )
                row = self._connection.execute("SELECT * FROM model_versions WHERE version_id=?", (version_id,)).fetchone()
        result = dict(row)
        result["parameters"] = _loads(result.pop("parameters_json"), dict(DEFAULT_CONTEXT_PARAMETERS))
        return result

    def latest_parameter_proposal(self, symbol: str, horizon: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM parameter_proposals WHERE symbol=? AND horizon=?
                   ORDER BY created_at DESC LIMIT 1""", (symbol, horizon),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for source, target in (("current_json", "current_parameters"), ("suggested_json", "suggested_parameters"),
                               ("reasoning_json", "reasoning"), ("metrics_json", "metrics")):
            result[target] = _loads(result.pop(source), {})
        return result

    def optimization_status(self, symbol: str, horizon: str) -> dict[str, Any]:
        latest = self.latest_parameter_proposal(symbol, horizon)
        now = datetime.now(timezone.utc)
        proposal_next = _parse_time(latest.get("next_allowed_at")) if latest else None
        task_key = f"optimizer-weekly:{symbol}:{horizon}"
        with self._lock:
            refresh = self._connection.execute(
                "SELECT completed_at FROM refresh_state WHERE task_key=?", (task_key,),
            ).fetchone()
        last_check = _parse_time(refresh["completed_at"]) if refresh else None
        check_next = last_check + timedelta(days=OPTIMIZATION_INTERVAL_DAYS) if last_check else None
        candidates = [item for item in (proposal_next, check_next) if item is not None]
        next_allowed = max(candidates) if candidates else now
        due = now >= next_allowed
        return {
            "due": due,
            "interval_days": OPTIMIZATION_INTERVAL_DAYS,
            "last_proposal_at": latest.get("created_at") if latest else None,
            "next_allowed_at": next_allowed.isoformat() if next_allowed else now.isoformat(),
            "last_check_at": last_check.isoformat() if last_check else None,
            "proposal": latest,
            "active_model": self.active_model_version(symbol, horizon),
        }

    def generate_parameter_proposal(self, symbol: str, horizon: str, *, force: bool = False) -> dict[str, Any]:
        status = self.optimization_status(symbol, horizon)
        if (status.get("proposal") or {}).get("status") == "pending":
            return {**status, "generated": False, "reason": "已有待用户审批的参数提案。"}
        if not force and not status["due"]:
            return status
        with self._lock:
            rows = self._connection.execute(
                """SELECT learning_json,context_json FROM forecast_runs
                   WHERE symbol=? AND horizon=? AND learning_json IS NOT NULL AND context_json IS NOT NULL
                   ORDER BY settled_at""", (symbol, horizon),
            ).fetchall()
        observations: list[tuple[float, dict[str, float]]] = []
        score_keys = {
            "five_minute_weight_pp": "five_minute_score",
            "news_weight_pp": "news_score",
            "options_weight_pp": "options_score",
            "flow_weight_pp": "flow_score",
            "short_weight_pp": "short_score",
        }
        for row in rows:
            learning = _loads(row["learning_json"], {})
            context = _loads(row["context_json"], {})
            error = _finite(learning.get("signed_error_percentage_points"))
            raw_scores = context.get("scores") if isinstance(context, dict) else None
            if error is None or not isinstance(raw_scores, dict):
                continue
            scores = {key: value for key, source in score_keys.items() if (value := _finite(raw_scores.get(source))) is not None}
            observations.append((error, scores))
        active = self.active_model_version(symbol, horizon)
        current = dict(active["parameters"])
        minimum = 12
        if len(observations) < minimum:
            reason = f"需要至少 {minimum} 个包含因子快照的已结算预测；当前 {len(observations)} 个。"
            self.mark_task_complete(f"optimizer-weekly:{symbol}:{horizon}", {"generated": False, "reason": reason})
            return {**self.optimization_status(symbol, horizon), "generated": False, "reason": reason}

        suggested = dict(current)
        reasoning: list[dict[str, Any]] = []
        for parameter, score_name in score_keys.items():
            pairs = [(error, scores.get(parameter)) for error, scores in observations if parameter in scores]
            if len(pairs) < 8:
                continue
            errors = [item[0] for item in pairs]
            features = [float(item[1]) for item in pairs]
            feature_mean = sum(features) / len(features)
            error_mean = sum(errors) / len(errors)
            variance = sum((value - feature_mean) ** 2 for value in features)
            covariance = sum((feature - feature_mean) * (error - error_mean) for error, feature in pairs)
            learned_delta = -covariance / variance if variance > 1e-9 else 0.0
            bounded_delta = max(-0.20, min(0.20, learned_delta))
            old = float(current.get(parameter, 0.0))
            new = max(-1.0, min(1.0, old + bounded_delta))
            suggested[parameter] = round(new, 4)
            reasoning.append({
                "parameter": parameter, "score": score_name,
                "from": round(old, 4), "to": round(new, 4),
                "sample_count": len(pairs),
                "error_correlation_direction": "positive" if covariance > 0 else "negative" if covariance < 0 else "flat",
            })
        raw_mae = sum(abs(error) for error, _scores in observations) / len(observations)
        corrected_errors: list[float] = []
        for error, scores in observations:
            correction = sum((float(suggested.get(parameter, 0.0)) - float(current.get(parameter, 0.0))) * scores.get(parameter, 0.0) for parameter in score_keys)
            corrected_errors.append(abs(error + correction))
        corrected_mae = sum(corrected_errors) / len(corrected_errors)
        improvement = (raw_mae - corrected_mae) / raw_mae * 100.0 if raw_mae > 0 else 0.0
        if improvement < 2.0 or all(float(suggested.get(key, 0.0)) == float(current.get(key, 0.0)) for key in score_keys):
            reason = f"本周样本外估计改善 {improvement:.2f}%，未达到 2% 提案门槛。"
            self.mark_task_complete(f"optimizer-weekly:{symbol}:{horizon}", {"generated": False, "reason": reason})
            return {**self.optimization_status(symbol, horizon), "generated": False, "reason": reason}

        created = datetime.now(timezone.utc)
        next_allowed = created + timedelta(days=OPTIMIZATION_INTERVAL_DAYS)
        suggested_version = self._next_model_version(str(active["model_version"]))
        identity = f"{symbol}|{horizon}|{created.isoformat()}|{suggested_version}"
        proposal_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
        metrics = {
            "samples": len(observations), "raw_mae_pp": round(raw_mae, 4),
            "estimated_mae_pp": round(corrected_mae, 4), "estimated_improvement_percent": round(improvement, 3),
            "method": "expanding_factor_error_regression",
        }
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO parameter_proposals(proposal_id,symbol,horizon,created_at,next_allowed_at,status,
                   current_version,suggested_version,current_json,suggested_json,reasoning_json,metrics_json,decided_at)
                   VALUES(?,?,?,?,?,'pending',?,?,?,?,?,?,NULL)""",
                (proposal_id, symbol, horizon, created.isoformat(), next_allowed.isoformat(), active["model_version"],
                 suggested_version, _json(current), _json(suggested), _json(reasoning), _json(metrics)),
            )
        self.mark_task_complete(f"optimizer-weekly:{symbol}:{horizon}", {"generated": True, "proposal_id": proposal_id})
        return self.optimization_status(symbol, horizon) | {"generated": True}

    def decide_parameter_proposal(self, proposal_id: str, approve: bool) -> dict[str, Any]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM parameter_proposals WHERE proposal_id=?", (proposal_id,),
            ).fetchone()
            if row is None:
                raise ValueError("proposal_not_found")
            if row["status"] != "pending":
                raise ValueError("proposal_already_decided")
            decided = utc_now()
            new_status = "approved" if approve else "rejected"
            if approve:
                self._connection.execute(
                    "UPDATE model_versions SET status='superseded' WHERE symbol=? AND horizon=? AND status='active'",
                    (row["symbol"], row["horizon"]),
                )
                version_id = hashlib.sha256(
                    f"{row['symbol']}|{row['horizon']}|{row['suggested_version']}".encode()
                ).hexdigest()[:32]
                self._connection.execute(
                    """INSERT INTO model_versions(version_id,symbol,horizon,model_version,parameters_json,created_at,
                       approved_at,source_proposal_id,status) VALUES(?,?,?,?,?,?,?,?, 'active')""",
                    (version_id, row["symbol"], row["horizon"], row["suggested_version"], row["suggested_json"],
                     decided, decided, proposal_id),
                )
            self._connection.execute(
                "UPDATE parameter_proposals SET status=?,decided_at=? WHERE proposal_id=?",
                (new_status, decided, proposal_id),
            )
        return self.optimization_status(row["symbol"], row["horizon"])

    def apply_active_context(self, payload: dict[str, Any], origin_close: float, context: dict[str, Any]) -> dict[str, Any]:
        if not payload.get("forecast_30m") or origin_close <= 0:
            return payload
        active = self.active_model_version(str(payload.get("symbol")), str(payload.get("horizon")))
        parameters = active["parameters"]
        scores = context.get("scores") if isinstance(context.get("scores"), dict) else {}
        mapping = {
            "five_minute_weight_pp": "five_minute_score", "news_weight_pp": "news_score",
            "options_weight_pp": "options_score", "flow_weight_pp": "flow_score", "short_weight_pp": "short_score",
        }
        contributions: dict[str, float] = {}
        for parameter, score_name in mapping.items():
            score = _finite(scores.get(score_name))
            weight = _finite(parameters.get(parameter))
            if score is not None and weight is not None:
                contributions[score_name] = round(score * weight, 6)
        raw_adjustment = sum(contributions.values())
        cap = abs(_finite(parameters.get("max_terminal_adjustment_pp")) or 1.5)
        adjustment_pp = max(-cap, min(cap, raw_adjustment))
        result = copy.deepcopy(payload)
        bars = result["forecast_30m"]
        terminal_delta = origin_close * adjustment_pp / 100.0
        for index, bar in enumerate(bars):
            delta = terminal_delta * (index + 1) / len(bars)
            for key in ("open", "high", "low", "close", "q10", "q50", "q90"):
                bar[key] = round(float(bar[key]) + delta, 6)
        probabilities = dict(result.get("probabilities") or {})
        if probabilities:
            up = max(0.0, float(probabilities.get("up_percent", 0.0)) + adjustment_pp * 4.0)
            down = max(0.0, float(probabilities.get("down_percent", 0.0)) - adjustment_pp * 4.0)
            flat = max(0.0, float(probabilities.get("flat_percent", 0.0)))
            total = up + down + flat
            if total > 0:
                up, flat = round(up / total * 100.0, 2), round(flat / total * 100.0, 2)
                down = round(100.0 - up - flat, 2)
                probabilities.update({"up_percent": up, "flat_percent": flat, "down_percent": down})
                result["probabilities"] = probabilities
        model = dict(result.get("model") or {})
        base_version = str(model.get("version") or "unknown")
        model.update({
            "base_version": base_version,
            "version": f"{base_version}+{active['model_version']}",
            "context_model_version": active["model_version"],
            "context_parameters": parameters,
        })
        result["model"] = model
        result["context"] = {
            **context, "scores": scores, "contributions_pp": contributions,
            "terminal_adjustment_pp": round(adjustment_pp, 6),
            "model_version": active["model_version"], "user_approved_version": bool(active.get("source_proposal_id")),
        }
        quality = dict(result.get("quality") or {})
        quality["warnings"] = list(quality.get("warnings") or []) + [
            "5分钟、新闻与OpenD因子使用有界研究融合；只有用户批准的新参数版本才会替换当前参数。",
        ]
        result["quality"] = quality
        return result

    def apply_validated_calibration(self, payload: dict[str, Any], origin_close: float) -> dict[str, Any]:
        """Legacy compatibility path; automatic calibration is prohibited."""
        result = copy.deepcopy(payload)
        result["calibration"] = {
            **self.learning_summary(str(payload.get("symbol")), str(payload.get("horizon"))),
            "applied": False,
            "reason": "参数变更必须先生成 from-to 提案，并由用户明确批准模型版本。",
        }
        return result

    def archive_news(self, symbol: str, provider: str, items: Iterable[dict[str, Any]], fetched_at: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        with self._lock, self._connection:
            for item in items:
                url = canonical_url(str(item.get("url") or ""))
                title = str(item.get("title") or "").strip()[:500]
                summary = str(item.get("summary") or "").strip()[:4000]
                published = str(item.get("published_at") or "")
                if not url.startswith("https://") or not title or _parse_time(published) is None:
                    continue
                content_hash = hashlib.sha256(f"{title}\n{summary}".encode("utf-8")).hexdigest()
                news_id = hashlib.sha256(f"{symbol}|{url}|{content_hash}".encode("utf-8")).hexdigest()[:32]
                existing = self._connection.execute(
                    "SELECT available_at FROM news_items WHERE news_id=?", (news_id,),
                ).fetchone()
                available_at = existing["available_at"] if existing else fetched_at
                self._connection.execute(
                    """INSERT OR IGNORE INTO news_items(news_id,symbol,provider,canonical_url,content_hash,title,summary,
                       source_name,source_domain,published_at,available_at,fetched_at,finbert_json,scores_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (news_id, symbol, provider, url, content_hash, title, summary,
                     str(item.get("source_name") or provider)[:160], item.get("source_domain"),
                     published, available_at, fetched_at, _json(item.get("finbert")) if item.get("finbert") is not None else None,
                     _json(item.get("scores")) if item.get("scores") is not None else None),
                )
                stored = dict(item)
                stored.update({
                    "id": news_id, "url": url, "available_at": available_at,
                    "pit_eligible": available_at <= fetched_at, "selected": True,
                })
                output.append(stored)
        return output

    def update_news_analysis(self, news_id: str, finbert: Any, scores: Any) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE news_items SET finbert_json=?, scores_json=? WHERE news_id=?",
                (_json(finbert) if finbert is not None else None, _json(scores) if scores is not None else None, news_id),
            )

    def news_snapshot(self, symbol: str, cutoff_at: str | None = None, limit: int = 30, window_hours: int = 72) -> list[dict[str, Any]]:
        cutoff = cutoff_at or utc_now()
        parsed_cutoff = _parse_time(cutoff) or datetime.now(timezone.utc)
        lower = (parsed_cutoff - timedelta(hours=max(1, min(window_hours, 720)))).isoformat()
        with self._lock:
            rows = self._connection.execute(
                """SELECT n.*, COALESCE(s.included,1) AS selected FROM news_items n
                   LEFT JOIN news_selections s ON s.symbol=n.symbol AND s.news_id=n.news_id
                   WHERE n.symbol=? AND n.available_at<=? AND n.published_at>=?
                   ORDER BY n.published_at DESC LIMIT ?""", (symbol, cutoff, lower, max(1, min(limit, 60))),
            ).fetchall()
        return [{
            "id": row["news_id"], "title": row["title"], "url": row["canonical_url"],
            "source_name": row["source_name"], "source_domain": row["source_domain"],
            "published_at": row["published_at"], "available_at": row["available_at"],
            "pit_eligible": row["available_at"] <= cutoff,
            "selected": bool(row["selected"]),
            "summary": row["summary"], "finbert": _loads(row["finbert_json"], None),
            "scores": _loads(row["scores_json"], {"catalyst": None, "source_quality": None, "relevance": None, "overall": None}),
            "provider": row["provider"],
        } for row in rows]

    def news_history(self, symbol: str, cutoff_at: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        cutoff = cutoff_at or utc_now()
        with self._lock:
            rows = self._connection.execute(
                """SELECT n.*, COALESCE(s.included,1) AS selected FROM news_items n
                   LEFT JOIN news_selections s ON s.symbol=n.symbol AND s.news_id=n.news_id
                   WHERE n.symbol=? AND n.available_at<=? ORDER BY n.available_at LIMIT ?""",
                (symbol, cutoff, max(1, min(limit, 5000))),
            ).fetchall()
        return [{
            "id": row["news_id"], "title": row["title"], "url": row["canonical_url"],
            "source_name": row["source_name"], "source_domain": row["source_domain"],
            "published_at": row["published_at"], "available_at": row["available_at"],
            "pit_eligible": True, "selected": bool(row["selected"]), "summary": row["summary"],
            "finbert": _loads(row["finbert_json"], None), "scores": _loads(row["scores_json"], {}),
        } for row in rows]

    def set_news_selection(self, symbol: str, news_id: str, included: bool) -> None:
        with self._lock, self._connection:
            exists = self._connection.execute(
                "SELECT 1 FROM news_items WHERE symbol=? AND news_id=?", (symbol, news_id),
            ).fetchone()
            if exists is None:
                raise ValueError("news_not_found")
            self._connection.execute(
                """INSERT INTO news_selections(symbol,news_id,included,updated_at) VALUES(?,?,?,?)
                   ON CONFLICT(symbol,news_id) DO UPDATE SET included=excluded.included,updated_at=excluded.updated_at""",
                (symbol, news_id, 1 if included else 0, utc_now()),
            )

    def save_news_analysis(self, symbol: str, cutoff_at: str, analysis: dict[str, Any]) -> str:
        identity = f"{symbol}|{cutoff_at}|{analysis.get('generated_at')}"
        analysis_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO news_analyses(analysis_id,symbol,cutoff_at,generated_at,analysis_json)
                   VALUES(?,?,?,?,?)""",
                (analysis_id, symbol, cutoff_at, str(analysis.get("generated_at") or utc_now()), _json(analysis)),
            )
        return analysis_id

    def latest_news_analysis(self, symbol: str, cutoff_at: str | None = None) -> dict[str, Any] | None:
        cutoff = cutoff_at or utc_now()
        with self._lock:
            row = self._connection.execute(
                """SELECT analysis_json FROM news_analyses WHERE symbol=? AND cutoff_at<=?
                   ORDER BY cutoff_at DESC LIMIT 1""", (symbol, cutoff),
            ).fetchone()
        return _loads(row["analysis_json"], None) if row else None

    def set_star(
        self, symbol: str, company: str, starred: bool,
        exchange: str | None = None, base_price: float | None = None,
    ) -> None:
        with self._lock, self._connection:
            if starred:
                clean_exchange = exchange.strip()[:48] if isinstance(exchange, str) and exchange.strip() else None
                clean_price = _finite(base_price)
                if clean_price is not None and clean_price <= 0:
                    clean_price = None
                self._connection.execute(
                    """INSERT INTO watchlist(symbol,company,starred_at,exchange,base_price) VALUES(?,?,?,?,?)
                       ON CONFLICT(symbol) DO UPDATE SET
                         company=excluded.company,
                         exchange=COALESCE(excluded.exchange,watchlist.exchange),
                         base_price=COALESCE(excluded.base_price,watchlist.base_price)""",
                    (symbol, company[:160] or symbol, utc_now(), clean_exchange, clean_price),
                )
            else:
                self._connection.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))

    def watchlist(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT symbol,company,exchange,base_price,starred_at,last_refresh_at,last_error
                   FROM watchlist ORDER BY starred_at""",
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_watchlist_refresh(self, symbol: str, error: str | None = None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE watchlist SET last_refresh_at=?, last_error=? WHERE symbol=?",
                (utc_now(), error[:240] if error else None, symbol),
            )

    def scanner_snapshot(self, scanner_day: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM scanner_daily_runs WHERE scanner_day=?", (scanner_day,),
            ).fetchone()
        payload = _loads(row["payload_json"], None) if row else None
        return payload if isinstance(payload, dict) else None

    def save_scanner_snapshot(self, scanner_day: str, payload: dict[str, Any]) -> None:
        generated_at = str(payload.get("generated_at") or utc_now())
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO scanner_daily_runs(scanner_day,generated_at,payload_json) VALUES(?,?,?)
                   ON CONFLICT(scanner_day) DO UPDATE SET
                     generated_at=excluded.generated_at,payload_json=excluded.payload_json""",
                (scanner_day, generated_at, _json(payload)),
            )

    def task_due(self, task_key: str, interval_hours: int) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT completed_at FROM refresh_state WHERE task_key=?", (task_key,),
            ).fetchone()
        completed = _parse_time(row["completed_at"]) if row else None
        return completed is None or datetime.now(timezone.utc) - completed >= timedelta(hours=max(1, interval_hours))

    def mark_task_complete(self, task_key: str, details: dict[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO refresh_state(task_key,completed_at,details_json) VALUES(?,?,?)
                   ON CONFLICT(task_key) DO UPDATE SET completed_at=excluded.completed_at,details_json=excluded.details_json""",
                (task_key, utc_now(), _json(details)),
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()
