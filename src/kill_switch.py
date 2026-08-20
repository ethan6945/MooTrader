"""Centralised "can we open new positions right now?" decision.

Used to live across three places:
  • main.py:  in_trade_phase() check at top of scan_once
  • main.py:  regime.block_new_entries check before candidate loop
  • risk_manager.can_open_new():  halted + drawdown checks

Each had different short-circuit shapes (return / continue / break), different
audit formats, and the daily-reset for `halted` only fired lazily from inside
can_open_new — which left a "ghost halt" if no candidate reached that stage.

This module pulls all four checks into one function so:
  1. The same logic runs at the same point in every scan.
  2. The audit gate name is consistent ("kill_switch").
  3. The GUI can read one snapshot ("can_trade", "reason") instead of guessing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import clock, db
from .config import settings

log = logging.getLogger(__name__)

# Trade-phase boundaries (NY local time, minutes from midnight).
# Skip first 15 min of session (gap noise) + last 30 min (MOC volatility).
TRADE_PHASE_START_MIN = 9 * 60 + 45    # 09:45 ET
TRADE_PHASE_END_MIN = 15 * 60 + 30     # 15:30 ET

# Friday no-new-entries cutoff — weekend gap risk on swing positions.
# After this time on Friday we still MANAGE open positions (stops + tps still
# fire) but refuse to open new names. Existing positions can still close out
# normally. 2026-05-28: added per audit recommendation.
FRIDAY_CUTOFF_MIN = 14 * 60      # 14:00 ET


@dataclass(frozen=True)
class KillSwitchVerdict:
    can_trade: bool
    reason: str
    gate: str               # which check fired, e.g. "trade_phase" | "regime" | "halt"


def in_trade_phase(now: Optional[datetime] = None) -> bool:
    n = now or clock.ny_now()
    if n.weekday() >= 5:
        return False
    minutes = n.hour * 60 + n.minute
    # 2026-06-26 strict-boundary fix: 15:30 is the start of MOC volatility —
    # the bot should NOT open new entries at/after 15:30. Use < instead of <=
    # so the last valid entry minute is 15:29, consistent with the docstring.
    return TRADE_PHASE_START_MIN <= minutes < TRADE_PHASE_END_MIN


def evaluate(regime_block_new: bool, regime_label: str, regime_note: str,
             current_cash: float = 0.0) -> KillSwitchVerdict:
    """Run every "should we even try to open a new position" check.

    Caller still does manage_open_trades + snapshot refresh on `can_trade=False`
    — this only governs new entries.
    """
    now = clock.ny_now()
    if not in_trade_phase(now):
        return KillSwitchVerdict(
            can_trade=False,
            reason=f"outside trade phase ({now:%H:%M} ET, allowed 09:45-15:30)",
            gate="trade_phase",
        )

    # Friday no-new-entries cutoff — avoid weekend gap risk on a 7-day max-hold
    # swing. Existing positions still managed by manage_open_trades; we only
    # block NEW entries here.
    if now.weekday() == 4:   # 4 = Friday
        minutes = now.hour * 60 + now.minute
        if minutes >= FRIDAY_CUTOFF_MIN:
            return KillSwitchVerdict(
                can_trade=False,
                reason=f"Friday after {FRIDAY_CUTOFF_MIN // 60:02d}:00 ET — no new entries (weekend gap protection)",
                gate="friday_cutoff",
            )

    if regime_block_new:
        return KillSwitchVerdict(
            can_trade=False,
            reason=f"regime {regime_label}: {regime_note}",
            gate="regime",
        )

    # Halt / drawdown via SQLite atomic read (no torn data even if scheduler
    # and a manual-close are racing).
    state = db.get_state()
    if state.get("halted"):
        return KillSwitchVerdict(
            can_trade=False,
            reason="halted (daily drawdown — click 'Halted' label to reset)",
            gate="halt",
        )

    # Daily drawdown — measured on TODAY'S REALIZED PnL against the equity
    # baseline (frozen seed while auto-compounding is armed). 2026-07-02 audit
    # P0-3: the previous cash-based measure was a dead fuse in SIMULATE (paper
    # cash ≫ budget → the threshold was mathematically unreachable) and a false
    # trigger in REAL (buying positions reduces cash and read as "drawdown").
    # `current_cash` is no longer consumed here; the parameter stays so call
    # sites don't churn.
    realized_today = float(state.get("realized_pnl_today", 0.0) or 0.0)
    if realized_today < 0:
        try:
            from . import risk_manager   # lazy: avoids any import-order cycle
            base = float(risk_manager.equity_baseline())
        except Exception:
            base = 0.0
        if base > 0:
            loss_frac = -realized_today / base
            if loss_frac >= settings.daily_drawdown_stop:
                # Set the halt flag atomically so subsequent scans short-circuit.
                db.atomic_state(lambda s: {"halted": True})
                return KillSwitchVerdict(
                    can_trade=False,
                    reason=(f"daily drawdown {loss_frac:.1%} of ${base:.0f} "
                            f"≥ {settings.daily_drawdown_stop:.0%} — auto-halted"),
                    gate="drawdown",
                )

    return KillSwitchVerdict(can_trade=True, reason="ok", gate="")


def reset_for_new_day(current_cash: float) -> None:
    """Idempotent daily rollover. Safe to call at the top of every scan.

    Delegates. There were two implementations of this writing the same
    kv_state 'day' key — this one and risk_manager's — and they had already
    caused one P0 (2026-07-07: different date sources, ping-ponging every scan,
    realized_pnl_today wiped and `halted` cleared mid-session). That was fixed
    by aligning the dates, which left the deeper fault untouched: two functions,
    one key.

    The half that mattered was never copied across. risk_manager's version
    learned to preserve a halt that a new day does not answer — an uncancelled
    protective order, an unresolved fill, an oversold position. This one kept
    writing `halted: False` unconditionally, and main calls THIS one on every
    scan, so it won the race: at the first scan after midnight a manual-release
    halt was lifted by the clock and the bot resumed into the exact situation
    the halt existed to prevent. It also left halt_reason behind, so the state
    read "not halted, because: oversold position".

    One implementation now. This is the caller-facing name, kept so main and
    the tests do not have to care which module owns the rollover.
    """
    from . import risk_manager
    risk_manager.reset_for_new_day(current_cash)
