"""Broker research snapshot — options positioning, capital flow, short pressure.

Ported from the Stock Probability Prediction Platform's `companion/opend_worker.py`
and `companion/opend_research.py`, collapsed into one in-process module.

WHY IT COLLAPSED. Over there this ran as a disposable subprocess talking to its
own OpenD connection, because the platform was a web app that had to survive the
broker SDK wedging and had no other reason to hold a quote context open. This
project already owns a supervised, rate-limited `MooClient`; spawning a second
connection to the same OpenD would double the subscription footprint and fight
the same rate limiter from outside it. So the fetches happen here, on the
caller's client.

EVERY FIELD IS OPTIONAL. Options, capital flow and short interest each sit
behind their own broker permission, and a retail account commonly has none of
them. A missing block yields None, never a zero, so `signal_context.broker_scores`
can tell "no evidence" from "evidence says neutral". `data_mode` reports how much
actually arrived: live (all three), partial (some), unavailable (none).

Read-only. Nothing here places, modifies or cancels an order.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from moomoo import RET_OK

log = logging.getLogger(__name__)

# How many near-the-money contracts to snapshot. The broker caps list size and a
# full chain runs to several hundred legs; open interest at the money is what
# the put/call ratio is actually about.
_MAX_CONTRACTS = 160
_STRIKE_BAND = 0.15


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _rows(frame) -> list[dict[str, Any]]:
    if frame is None or not hasattr(frame, "to_dict"):
        return []
    try:
        return frame.to_dict("records")
    except Exception:
        return []


def _query(quote, method: str, *args, **kwargs) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Call one broker endpoint, returning rows and a short failure label.

    Permission errors are the normal case here, not the exception, so they are
    returned as a label for the caller to record rather than raised.
    """
    function = getattr(quote, method, None)
    if function is None:
        return [], f"{method}:unsupported"
    try:
        result = function(*args, **kwargs)
    except Exception as exc:                      # broker SDK raises freely
        return [], f"{method}:{type(exc).__name__}"
    # The paginated endpoints (short interest, daily short volume) return a
    # third element — the next-page key — while the flow endpoints return two.
    # Unpacking a fixed pair here turned working data into a ValueError that
    # looked exactly like a missing broker permission.
    if not isinstance(result, tuple) or len(result) < 2:
        return [], f"{method}:unexpected_return"
    ret, data = result[0], result[1]
    if ret != RET_OK:
        return [], f"{method}:{str(data)[:60]}"
    return _rows(data), None


def _option_factors(quote, code: str, spot: float) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Put/call open-interest ratio across near-the-money contracts."""
    errors: list[str] = []
    chain, error = _query(quote, "get_option_chain", code)
    if error:
        errors.append(error)
    if not chain:
        return None, errors or ["get_option_chain:no_data"]

    codes: list[str] = []
    for row in chain:
        contract = row.get("code")
        strike = _finite(row.get("strike_price") or row.get("option_strike_price"))
        if not contract:
            continue
        if spot > 0 and strike is not None and not (
            spot * (1 - _STRIKE_BAND) <= strike <= spot * (1 + _STRIKE_BAND)
        ):
            continue
        codes.append(str(contract))
        if len(codes) >= _MAX_CONTRACTS:
            break
    if not codes:
        return None, errors + ["get_option_chain:no_near_the_money"]

    snapshots: list[dict[str, Any]] = []
    for start in range(0, len(codes), 200):
        batch, error = _query(quote, "get_market_snapshot", codes[start:start + 200])
        if error:
            errors.append(error)
        snapshots.extend(batch)
    if not snapshots:
        return None, errors + ["get_market_snapshot:no_option_data"]

    call_oi = put_oi = 0.0
    for row in snapshots:
        oi = max(0.0, _finite(row.get("option_open_interest") or row.get("open_interest")) or 0.0)
        if "PUT" in str(row.get("option_type") or "").upper():
            put_oi += oi
        else:
            call_oi += oi
    if call_oi <= 0:
        return None, errors + ["option_open_interest:no_call_side"]
    return {
        "put_call_oi_ratio": round(put_oi / call_oi, 6),
        "call_open_interest": round(call_oi, 2),
        "put_open_interest": round(put_oi, 2),
        "contracts": len(snapshots),
    }, errors


def snapshot(client, symbol: str) -> dict[str, Any]:
    """Fetch the options / flow / short snapshot for one symbol.

    Never raises: a symbol with no permissions returns data_mode=unavailable and
    the reasons why, which is exactly what the context scorer needs to know.
    """
    code = client._format_code(symbol)
    quote = client.quote
    errors: list[str] = []

    spot = 0.0
    try:
        frame = client.get_kline(symbol, bars=2)
        if frame is not None and len(frame):
            spot = float(frame["close"].iloc[-1])
    except Exception as exc:
        errors.append(f"spot:{type(exc).__name__}")

    flow_rows, error = _query(quote, "get_capital_flow", code)
    if error:
        errors.append(error)
    flow_latest = (max(flow_rows, key=lambda row: str(row.get("capital_flow_item_time") or ""))
                   if flow_rows else {})

    distribution_rows, error = _query(quote, "get_capital_distribution", code)
    if error:
        errors.append(error)
    distribution = distribution_rows[0] if distribution_rows else {}

    short_rows, error = _query(quote, "get_short_interest", code, num=20)
    if error:
        errors.append(error)
    short_latest = (max(short_rows, key=lambda row: str(row.get("timestamp_str")
                                                        or row.get("timestamp") or ""))
                    if short_rows else {})

    daily_short_rows, error = _query(quote, "get_daily_short_volume", code, num=20)
    if error:
        errors.append(error)
    daily_short = (max(daily_short_rows, key=lambda row: str(row.get("timestamp_str")
                                                            or row.get("timestamp") or ""))
                   if daily_short_rows else {})

    options, option_errors = (_option_factors(quote, code, spot) if spot
                              else (None, ["underlying_price_unavailable"]))
    errors.extend(option_errors)

    capital_in = sum(_finite(distribution.get(f"capital_in_{size}")) or 0.0
                     for size in ("super", "big", "mid", "small")) if distribution else None
    capital_out = sum(_finite(distribution.get(f"capital_out_{size}")) or 0.0
                      for size in ("super", "big", "mid", "small")) if distribution else None

    flow = {
        "capital_in_total": capital_in,
        "capital_out_total": capital_out,
        "capital_net": (capital_in - capital_out) if (capital_in is not None
                                                     and capital_out is not None) else None,
        "updated_at": str(flow_latest.get("capital_flow_item_time")
                          or distribution.get("update_time") or ""),
    } if distribution else None

    short = {
        "daily_short_percent": _finite(daily_short.get("short_percent")),
        "short_interest_percent": _finite(short_latest.get("short_percent")),
        "shares_short": _finite(short_latest.get("shares_short")),
        "days_to_cover": _finite(short_latest.get("days_to_cover")),
        "updated_at": str(short_latest.get("timestamp_str")
                          or daily_short.get("timestamp_str") or ""),
    } if (short_latest or daily_short) else None

    available = sum(1 for block in (options, flow, short) if block)
    return {
        "symbol": symbol,
        "source": "moomoo_opend",
        "fetched_at": _utc_now(),
        "as_of": str(flow_latest.get("last_valid_time")
                     or (distribution or {}).get("update_time")
                     or (daily_short or {}).get("timestamp_str") or ""),
        "data_mode": "live" if available == 3 else "partial" if available else "unavailable",
        "options": options,
        "flow": flow,
        "short": short,
        "quality": {
            "status": "live" if available == 3 else "partial" if available else "unavailable",
            "blocks_available": available,
            "errors": errors[:12],
        },
    }


__all__ = ["snapshot"]
