"""How much of the account one idea is allowed to be.

WHAT WAS MISSING

  calc_position_size caps a SINGLE order at MAX_POSITION_PCT of the budget, and
  _can_stack_onto checks only how many stacks a symbol has had and whether the
  last one is in profit. Neither looks at what is already held. So five stacks
  of 36% each are five separate legal decisions that add up to an illegal
  position, and the only thing that ever stopped them was running out of cash.

  Nothing looked at the portfolio either: portfolio.heat_check() exists and has
  not been called since 2026-06-03, removed because heat and per-trade risk are
  both percentages of the same account and scale together. That reasoning is
  sound about RISK — and says nothing about NOTIONAL, which is what decides how
  much a gap costs.

WHY CORRELATION AND NOT SECTOR
  A sector cap is a proxy for "do not hold five of the same bet". The proxy
  needs a classification, and there is not one: the broker returns no industry
  or sector field for US equities (checked — get_stock_basicinfo has none, and
  the snapshot's plate_* columns are plate counts, not a classification). The
  alternative is a hand-written map, which goes stale silently, and a stale
  risk constraint is worse than none because it reads as protection.

  Daily returns are available for every name already, so the thing the proxy
  was for can be measured directly.
"""
from __future__ import annotations

import logging

import pandas as pd

from . import db, risk_manager
from .config import settings

log = logging.getLogger(__name__)

# Total notional in ONE symbol, as a fraction of deployable budget. Above the
# single-order cap on purpose: a stack is meant to add to a winner, and this
# stops that from becoming the whole account rather than stopping it at all.
MAX_SYMBOL_EXPOSURE_PCT = 0.50

# Total notional across every position. The account is not allowed to be more
# than this invested at once, whatever the per-name arithmetic says.
MAX_GROSS_EXPOSURE_PCT = 1.00

# Two names moving together are one bet. Above this, the candidate counts
# against the exposure of what it correlates with rather than getting its own.
CORRELATION_THRESHOLD = 0.80
CORRELATION_LOOKBACK_DAYS = 60
# The most a single correlated CLUSTER may be. Deliberately tighter than one
# symbol: five names at 0.9 correlation are a 50% position wearing a disguise.
MAX_CLUSTER_EXPOSURE_PCT = 0.60


def _budget() -> float:
    try:
        return max(float(risk_manager.budget_usd()), 1.0)
    except Exception:
        return max(float(settings.account_usd), 1.0)


def holdings_notional() -> dict[str, float]:
    """symbol -> notional at ENTRY price.

    Entry rather than market: this is a limit on how much was committed, and
    marking it to market would loosen the cap as a position falls — exactly
    when it should not loosen.
    """
    out = {}
    try:
        for sym, t in db.load_open_trades().items():
            out[sym] = float(t["qty"]) * float(t["entry_price"])
    except Exception as e:
        log.error("cannot read holdings for the exposure check: %s", e)
        raise
    return out


def symbol_exposure(symbol: str, add_notional: float = 0.0) -> tuple[float, float]:
    """(notional, fraction of budget) for one symbol, including a proposed add."""
    held = holdings_notional().get(symbol.upper(), 0.0) + max(0.0, add_notional)
    return held, held / _budget()


def gross_exposure(add_notional: float = 0.0) -> tuple[float, float]:
    total = sum(holdings_notional().values()) + max(0.0, add_notional)
    return total, total / _budget()


def _daily_returns(client, symbol: str, days: int) -> pd.Series | None:
    try:
        from moomoo import KLType
        df = client.get_kline(symbol, bars=days + 5, ktype=KLType.K_DAY)
    except Exception as e:
        log.debug("no klines for %s (%s)", symbol, e)
        return None
    if df is None or len(df) < 10 or "close" not in df.columns:
        return None
    return df["close"].astype(float).pct_change().dropna().tail(days)


def correlated_cluster(client, symbol: str,
                       threshold: float = CORRELATION_THRESHOLD) -> list[str]:
    """Held symbols whose daily returns move with `symbol` above `threshold`.

    Returns an empty list when the data is not there. That is a deliberate
    fail-OPEN on this one check: refusing every entry whose history could not
    be fetched would make a quote outage a trading halt, and the exposure caps
    above still apply. A correlation that cannot be measured is not evidence of
    correlation — but it is also why this is a cap on top of the others rather
    than the only one.
    """
    held = [s for s in holdings_notional() if s.upper() != symbol.upper()]
    if not held:
        return []
    base = _daily_returns(client, symbol, CORRELATION_LOOKBACK_DAYS)
    if base is None or len(base) < 20:
        return []

    cluster = []
    for other in held:
        series = _daily_returns(client, other, CORRELATION_LOOKBACK_DAYS)
        if series is None or len(series) < 20:
            continue
        joined = pd.concat([base, series], axis=1, join="inner").dropna()
        if len(joined) < 20:
            continue
        corr = joined.iloc[:, 0].corr(joined.iloc[:, 1])
        if pd.notna(corr) and abs(corr) >= threshold:
            cluster.append(other)
            log.info("%s correlates %.2f with held %s over %d days",
                     symbol, corr, other, len(joined))
    return cluster


def check(signal, qty: int, client=None) -> tuple[bool, str]:
    """May this order be placed, given everything already held?

    Called on the entry path alongside the per-order sizing cap. The two answer
    different questions: sizing asks "how big may THIS order be", this asks
    "how big may the position, the cluster and the account become".
    """
    if qty <= 0:
        return False, "qty=0"
    price = float(getattr(signal, "price", 0) or 0)
    if price <= 0:
        return False, "no price"
    symbol = str(signal.symbol).upper()
    add = qty * price
    budget = _budget()

    _, sym_frac = symbol_exposure(symbol, add)
    if sym_frac > MAX_SYMBOL_EXPOSURE_PCT:
        return False, (f"{symbol} would reach {sym_frac:.0%} of budget "
                       f"(cap {MAX_SYMBOL_EXPOSURE_PCT:.0%}) — this is the "
                       f"cumulative limit stacking never had")

    _, gross_frac = gross_exposure(add)
    if gross_frac > MAX_GROSS_EXPOSURE_PCT:
        return False, (f"gross exposure would reach {gross_frac:.0%} of budget "
                       f"(cap {MAX_GROSS_EXPOSURE_PCT:.0%})")

    if client is not None:
        try:
            cluster = correlated_cluster(client, symbol)
        except Exception as e:
            log.warning("correlation check failed for %s (%s) — the exposure "
                        "caps above still applied", symbol, e)
            cluster = []
        if cluster:
            held = holdings_notional()
            cluster_notional = add + sum(held.get(s, 0.0) for s in cluster) \
                + held.get(symbol, 0.0)
            frac = cluster_notional / budget
            if frac > MAX_CLUSTER_EXPOSURE_PCT:
                return False, (
                    f"{symbol} moves with {', '.join(cluster)}; together they "
                    f"would be {frac:.0%} of budget (cap "
                    f"{MAX_CLUSTER_EXPOSURE_PCT:.0%}). Correlated names are one "
                    f"bet, whatever the per-symbol arithmetic says")

    return True, "ok"


def snapshot() -> dict:
    """Current concentration, for the dashboard and the logs."""
    held = holdings_notional()
    budget = _budget()
    total = sum(held.values())
    return {
        "budget": budget,
        "gross_notional": total,
        "gross_pct": total / budget,
        "by_symbol": {s: {"notional": n, "pct": n / budget}
                      for s, n in sorted(held.items(), key=lambda x: -x[1])},
        "caps": {"symbol": MAX_SYMBOL_EXPOSURE_PCT,
                 "gross": MAX_GROSS_EXPOSURE_PCT,
                 "cluster": MAX_CLUSTER_EXPOSURE_PCT},
    }
