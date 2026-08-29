"""Portfolio-level risk gates + closed-trade R-multiple log.

Portfolio heat = Σ (entry − stop) × qty across all open trades. We cap this
at PORTFOLIO_HEAT_PCT of ACCOUNT_USD (default 8%) — even if every single
trade hits its stop, total drawdown is bounded to that %.

Every close is appended to data/trades.jsonl with R-multiple = realised
PnL / initial risk. After a few dozen trades this tells you whether you
actually have an edge (avg R > 0.5 is decent, > 1.0 is excellent).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pytz

from . import db
from .config import settings
from .indicators import Signal

log = logging.getLogger(__name__)

TRADE_LOG_FILE = settings.root / "data" / "trades.jsonl"   # legacy mirror
NY = pytz.timezone("America/New_York")

# Was 0.08 when RISK_PER_TRADE=2% — that capped total open risk at $360, which
# blocked the 2nd or 3rd position the moment we moved to 5% risk × 5 positions
# (theoretical max 25% heat). Raised to 0.20 so up to 4 simultaneous full-risk
# positions can coexist. The DD_HALT_PCT in .env (18%) is the real circuit
# breaker — heat cap is a softer "don't go silly" guard.
PORTFOLIO_HEAT_PCT = 0.20   # was 0.08 — scales with new 5% RISK_PER_TRADE


def current_open_risk() -> float:
    """Σ of (entry - stop) × qty across all tracked trades — from SQLite."""
    total = 0.0
    for t in db.load_open_trades().values():
        try:
            risk = (t["entry_price"] - t["stop_loss"]) * t["qty"]
            if risk > 0:
                total += risk
        except (KeyError, ValueError, TypeError):
            pass
    return total


def heat_check(signal: Signal, qty: int) -> tuple[bool, str]:
    """Block entry if portfolio heat would exceed cap.

    NOTE: kept as a primitive but NOT called on the live entry path since
    2026-06-03 (structurally redundant with per-trade risk + the budget cash
    wall — see main.py). The cap now derives from the dynamic budget so it stays
    correct if ever re-enabled, instead of anchoring to the static .env capital."""
    if qty <= 0:
        return False, "qty=0"
    new_risk = (signal.price - signal.stop_loss) * qty
    if new_risk <= 0:
        return True, "no positive risk"
    open_risk = current_open_risk()
    from . import risk_manager   # local import avoids any import-time cycle
    cap = risk_manager.budget_usd() * PORTFOLIO_HEAT_PCT
    total = open_risk + new_risk
    if total > cap:
        return False, (
            f"portfolio heat ${total:.0f} > cap ${cap:.0f} "
            f"(open ${open_risk:.0f} + new ${new_risk:.0f})"
        )
    return True, f"heat OK ${total:.0f}/${cap:.0f}"


def record_close(
    symbol: str,
    qty: int,
    entry: float,
    stop: float,
    exit_price: float,
    exit_reason: str,
    opened_at: str = "",
    mfe_pct: float | None = None,
    mae_pct: float | None = None,
    ml_proba_entry: float | None = None,
    ml_features: list[float] | None = None,
    strategy: str = "trend",
    initial_risk: float | None = None,
) -> dict:
    """Append closed trade with R-multiple, MFE/MAE, ML proba, strategy tag.

    `stop` is the CURRENT stop at close time (may have been ratcheted up by
    breakeven). `initial_risk` (entry − original stop) is used for R-multiple
    so the R metric doesn't distort to 0 when breakeven has raised stop=entry.
    When not provided, falls back to `entry − stop` (legacy behavior).
    """
    pnl = (exit_price - entry) * qty
    pnl_pct = (exit_price - entry) / entry * 100 if entry > 0 else 0.0
    r_unit = initial_risk if (initial_risk is not None and initial_risk > 0) else (entry - stop)
    r_multiple = (exit_price - entry) / r_unit if r_unit > 0 else 0.0
    row = {
        "ts": datetime.now(NY).isoformat(),
        "symbol": symbol,
        "qty": qty,
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "exit": round(exit_price, 2),
        "exit_reason": exit_reason,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "r_multiple": round(r_multiple, 2),
        "opened_at": opened_at,
        "mfe_pct": round(mfe_pct, 2) if mfe_pct is not None else None,
        "mae_pct": round(mae_pct, 2) if mae_pct is not None else None,
        "ml_proba_entry": round(ml_proba_entry, 3) if ml_proba_entry is not None else None,
        "strategy": strategy,
        "extra": json.dumps({"ml_features": ml_features}) if ml_features else None,
    }
    # Persist to SQLite (primary) + legacy JSONL mirror (best-effort).
    # Write SQLite FIRST, and only mirror to JSONL on success — so the two
    # never diverge silently. If SQLite fails, the caller still gets the
    # returned row, but the warning is loud and the trade is re-insertable
    # from the JSONL mirror if needed.
    db_ok = False
    try:
        db.closed_trade_insert(row)
        db_ok = True
    except Exception as e:
        log.warning("closed_trade SQLite insert failed: %s", e)
    try:
        TRADE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TRADE_LOG_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        log.warning("legacy trade log write failed: %s", e)
    if not db_ok:
        # The trade is only in JSONL — flag it so the operator knows.
        log.error(
            "DATA LOSS RISK: trade %s %s pnl=%.2f written to JSONL but NOT DB — "
            "stats/ML/panel will miss it until manually reconciled",
            row.get("symbol"), row.get("exit_reason"), row.get("pnl", 0))
    return row


# Closes the OWNER caused, not the strategy. Two labels because there are two
# routes: "MANUAL" is the owner asking the bot to close a position, "MANUAL_SELL"
# is reconcile finding a sale the owner made in the broker app. Both belong in
# the ops bucket and neither is strategy performance.
#
# This checked "MANUAL" alone, and reconcile has only ever written the other
# one — so an owner's hand-placed sale counted in the headline win rate, which
# is the exact pollution the bucket was added to prevent.
OWNER_CLOSE_REASONS = frozenset({"MANUAL", "MANUAL_SELL"})


def trade_stats(last_n: int = 50) -> dict:
    """Quick summary of recent closed trades — now reads from SQLite.

    HEADLINE numbers measure the strategy only: orphan-recovery adoptions
    (reconcile artifacts with fabricated stops, not strategy decisions) and
    MANUAL owner closes are excluded — they polluted the first 43 live trades
    (8 orphans at 12.5% WR / −$193 plus 4 manual closes). They stay visible in
    the separate `ops` bucket so nothing is hidden."""
    all_rows = db.closed_trades(last_n)
    if not all_rows:
        return {"count": 0}
    ops_rows = [r for r in all_rows
                if (r.get("strategy") == "reconcile_orphan_recovery"
                    or r.get("exit_reason") in OWNER_CLOSE_REASONS)]
    ops = {"n": len(ops_rows), "pnl": round(sum(r["pnl"] for r in ops_rows), 2)}
    rows = [r for r in all_rows if r not in ops_rows]
    if not rows:
        return {"count": 0, "ops": ops}
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    avg_r = sum(r["r_multiple"] for r in rows) / len(rows)

    # Per-strategy breakdown (helps user see if trend vs mean-revert is earning).
    by_strategy: dict = {}
    for r in rows:
        s = r.get("strategy") or "trend"
        agg = by_strategy.setdefault(s, {"n": 0, "wins": 0, "pnl": 0.0, "avg_r": 0.0})
        agg["n"] += 1
        if r["pnl"] > 0:
            agg["wins"] += 1
        agg["pnl"] = round(agg["pnl"] + r["pnl"], 2)
        agg["avg_r"] = round(agg["avg_r"] + r["r_multiple"], 2)
    for s, agg in by_strategy.items():
        if agg["n"] > 0:
            agg["avg_r"] = round(agg["avg_r"] / agg["n"], 2)
            agg["win_rate"] = round(agg["wins"] / agg["n"] * 100, 1)

    # MFE/MAE summary (only over rows that have it; older v1 rows return None)
    mfe_rows = [r for r in rows if r.get("mfe_pct") is not None]
    avg_mfe = round(sum(r["mfe_pct"] for r in mfe_rows) / len(mfe_rows), 2) if mfe_rows else None
    avg_mae = round(sum(r["mae_pct"] for r in mfe_rows) / len(mfe_rows), 2) if mfe_rows else None

    return {
        "count": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(rows) * 100, 1),
        "avg_r_multiple": round(avg_r, 2),
        "total_pnl": round(sum(r["pnl"] for r in rows), 2),
        "best_r": round(max((r["r_multiple"] for r in rows), default=0), 2),
        "worst_r": round(min((r["r_multiple"] for r in rows), default=0), 2),
        "by_strategy": by_strategy,
        "avg_mfe_pct": avg_mfe,
        "avg_mae_pct": avg_mae,
        # Non-strategy closes (orphan adoptions, manual exits) — visible but
        # excluded from the headline win-rate/PnL above.
        "ops": ops,
    }
