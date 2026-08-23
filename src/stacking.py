"""Pyramiding — when to add to a winner, and what the merged lot becomes.

WHY THIS EXISTS

  Live stacks add-on entries onto a position it already holds: up to
  MAX_STACKS_PER_SYMBOL lots, each one only once the trade is at least
  STACK_MIN_R_MULTIPLE in unrealised profit. Both are on today, so a
  meaningful share of live's deployed capital goes into names it is pressing
  rather than into new ones.

  backtest_v3 reimplemented the gate and got the R unit wrong: it measured
  profit against `entry − current_stop`. The breakeven ratchet raises the stop
  to entry, which drives that denominator to zero, so every add-on after
  breakeven armed was refused — in exactly the trades that had run far enough
  to qualify. Live measures against `init_risk_per_share`, the ORIGINAL risk
  at entry, precisely because it is stable under a moving stop.

  sandbox did not model stacking at all, and neither did backtest_v4 until
  this module existed. An engine that takes one lot where live takes five is
  not a conservative version of live; it is a different allocation, and it is
  the engine the optimizer proposes live parameters from.

  So the rule is here once, with the position state named rather than read.
  Live resolves it from db.load_open_trades(); a replay resolves it from its
  own book; neither can drift from the other.

THE R UNIT IS RE-ANCHORED ON EVERY MERGE
  After a stack, live sets init_risk_per_share = new_avg_entry − new_stop.
  Stacks only happen in profit, so the combined lot's risk is genuinely
  smaller than the original lot's was, and the next add-on should be measured
  against the position that now exists rather than the one that used to.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StackDecision:
    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class MergedLot:
    """The combined position after an add-on fills."""
    qty: int
    entry: float
    stop: float
    take_profit: float
    init_risk_per_share: float
    high_water: float
    stacks: int
    cash_debit: float          # what the add-on costs — the cash wall still bites


def r_unit_of(*, entry: float, init_risk_per_share: float,
              current_stop: float) -> float:
    """The per-share risk a stack decision is measured against.

    `init_risk_per_share` when it is known — it is the thesis risk at entry and
    does not move when the stop ratchets. `entry − current_stop` only as the
    legacy fallback, for positions opened before that field existed.
    """
    if init_risk_per_share and init_risk_per_share > 0:
        return float(init_risk_per_share)
    return float(entry) - float(current_stop)


def can_stack(*, stacks: int, entry: float, last_px: float,
              init_risk_per_share: float = 0.0, current_stop: float = 0.0,
              max_stacks: int, min_r: float) -> StackDecision:
    """Whether a held position qualifies for another lot right now."""
    if max_stacks <= 1:
        return StackDecision(False, "stacking disabled (MAX_STACKS_PER_SYMBOL <= 1)")
    if stacks >= max_stacks:
        return StackDecision(False, f"max stacks ({max_stacks}) reached")
    r_unit = r_unit_of(entry=entry, init_risk_per_share=init_risk_per_share,
                       current_stop=current_stop)
    if r_unit <= 0:
        return StackDecision(False, f"invalid R unit (r_unit={r_unit})")
    r_now = (float(last_px) - float(entry)) / r_unit
    if r_now < min_r:
        return StackDecision(False, f"unrealised {r_now:.2f}R < {min_r}R "
                                    f"(need profit to stack)")
    return StackDecision(True, f"{r_now:.2f}R >= {min_r}R")


def merge(*, old_qty: int, old_entry: float, old_stop: float, old_tp: float,
          old_high_water: float, old_stacks: int,
          add_qty: int, add_entry: float, add_stop: float,
          add_tp: float) -> MergedLot:
    """The combined lot, mirroring executor.open_position's stack branch.

      entry  → share-weighted average of what was actually PAID (not the limit:
               using the limit drifted the recorded basis toward a price the
               broker never charged)
      stop   → max(old, add-on). Trails UP only; a stack must never weaken the
               protection already on the original lot.
      TP     → max(old, add-on). Raised only.
      qty_initial and init_risk_per_share re-anchor on the combined lot, so
      scale-out tranches and the next stack decision both re-base.
    """
    new_qty = int(old_qty) + int(add_qty)
    new_entry = round(
        (old_qty * float(old_entry) + add_qty * float(add_entry)) / new_qty, 2)
    new_stop = max(float(old_stop), float(add_stop))
    new_tp = max(float(old_tp), float(add_tp))
    return MergedLot(
        qty=new_qty,
        entry=new_entry,
        stop=new_stop,
        take_profit=new_tp,
        init_risk_per_share=max(new_entry - new_stop, 0.0),
        high_water=max(float(old_high_water or add_entry), float(add_entry)),
        stacks=int(old_stacks) + 1,
        cash_debit=add_qty * float(add_entry),
    )
