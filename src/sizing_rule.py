"""How many shares, defined once.

WHY THIS EXISTS

  Three engines sized positions three ways.

  live (risk_manager.calc_position_size) composes five layers, takes
  min(qty_by_risk, qty_by_cap), and THEN applies the VIX divisor — returning 0
  rather than a position larger than the vol regime allows.

  The sandbox reimplemented it and got the VIX layer wrong in the specific way
  live had already been fixed for: `max(1, qty // 4)` hands a name whose base
  size is one share its FULL size in exactly the tape the cut exists for — 4x
  the intended risk. It also omitted the regime up-scaler entirely, so in a
  confirmed bull it sized every entry at 1.0x while live sized at 1.4x.

  backtest_v3 composed the multipliers in a different ORDER (regime and VIX as
  post-multipliers, then a re-clamp) which agrees with live only when the cap
  binds.

  So the two engines that propose live's parameters were measuring a strategy
  that sizes differently from the one that runs. This is that rule, once, with
  the account state named rather than read — a backtest supplies simulated
  state, live supplies its own, and neither can drift from the other.

THE VIX LAYER RETURNS ZERO ON PURPOSE
  Integer share counts cannot express a partial de-risk. At VIX > 35 a base of
  one share wants 0.25 shares; the honest answer is to skip the name, not to
  round up to 4x the intended risk. Any caller that "fixes" this with a floor
  of 1 has re-introduced the bug.
"""
from __future__ import annotations

from dataclasses import dataclass

# Composite floor on the account-state multipliers (dd × adaptive). Signal
# quality (conviction) and the regime tailwind compose ON TOP and are not
# floored — a marginal signal in a deep drawdown SHOULD still be half-sized.
STATE_MULT_FLOOR = 0.25

VIX_HALVE_ABOVE = 25.0
VIX_QUARTER_ABOVE = 35.0


@dataclass(frozen=True)
class Size:
    """A share count, and why it is what it is."""
    qty: int
    reason: str

    def __bool__(self) -> bool:
        return self.qty > 0


def vix_divisor(vix: float) -> int:
    """The vol-regime de-risking divisor. 1 = no cut."""
    if vix > VIX_QUARTER_ABOVE:
        return 4
    if vix > VIX_HALVE_ABOVE:
        return 2
    return 1


def resolve(*, capital: float, risk_per_trade: float, max_position_pct: float,
            price: float, stop_loss: float, vix: float = 15.0,
            conviction: float = 1.0, regime_mult: float = 1.0,
            state_mult: float = 1.0) -> Size:
    """Shares to buy, in live's exact order of operations.

    capital          sizing capital (live: risk_manager.sizing_capital();
                     a replay: its own mark-to-market equity)
    risk_per_trade   fraction of capital risked to the stop
    max_position_pct per-name notional cap as a fraction of capital
    price/stop_loss  the entry and its protective level
    vix              volatility regime input for the divisor layer
    conviction       0.0–1.0 signal-quality multiplier
    regime_mult      >= 1.0 confirmed-bull tailwind (values < 1 are ignored:
                     this layer is an UP-scaler only)
    state_mult       account-state multiplier (drawdown cut x adaptive sizing),
                     floored at STATE_MULT_FLOOR
    """
    if conviction <= 0:
        return Size(0, "conviction 0")
    if price <= 0:
        return Size(0, "no price")

    # An UP-scaler only — a stray < 1 must never secretly shrink risk. That is
    # what the state/VIX/conviction layers are for.
    regime_mult = max(1.0, float(regime_mult))
    state_mult = max(STATE_MULT_FLOOR, float(state_mult))

    stop_distance = price - stop_loss
    if stop_distance <= 0:
        return Size(0, "stop at or above entry")

    risk_dollars = capital * risk_per_trade * state_mult * conviction * regime_mult
    qty_by_risk = int(risk_dollars / stop_distance)
    qty_by_cap = int(capital * max_position_pct / price)
    base = max(0, min(qty_by_risk, qty_by_cap))
    if base <= 0:
        return Size(0, f"not sizeable — by_risk={qty_by_risk} "
                       f"(${risk_dollars:.0f} / ${stop_distance:.2f}), "
                       f"by_cap={qty_by_cap} "
                       f"(${capital * max_position_pct:.0f} / ${price:.2f})")

    divisor = vix_divisor(vix)
    if divisor == 1:
        return Size(base, "full size")
    scaled = base // divisor
    if scaled <= 0:
        # See the module docstring: skipping is the honest answer.
        return Size(0, f"VIX {vix:.1f} wants 1/{divisor} size but base is "
                       f"only {base} share(s) — skipping rather than taking "
                       f"{divisor}x the intended risk")
    return Size(scaled, f"VIX {vix:.1f} → 1/{divisor} size")
