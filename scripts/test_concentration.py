"""How much of the account one idea is allowed to be.

Run from repo root: .venv/bin/python scripts/test_concentration.py
No broker. Klines come from a stub so the correlation maths is deterministic.

WHAT WAS MISSING
  calc_position_size caps a SINGLE order at MAX_POSITION_PCT of the budget, and
  _can_stack_onto checks only the stack count and whether the last one is in
  profit. Neither looks at what is already held, so five stacks of 36% are five
  separate legal decisions that add up to an illegal position — and the only
  thing that ever stopped them was running out of cash.

  Nothing capped the portfolio either: portfolio.heat_check() has not been
  called since 2026-06-03, removed as structurally non-binding because heat and
  per-trade risk are both percentages of the same account. True about RISK, and
  silent about NOTIONAL, which is what decides how much a gap costs.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-conc-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

import numpy as np                                       # noqa: E402
import pandas as pd                                      # noqa: E402
from src import concentration, db, identity              # noqa: E402
from src.indicators import Signal                        # noqa: E402

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


db._ensure_initialised()
identity._session = None
identity.reset_cache()
identity.start_session("SIMULATE")
db.update_state({"budget_usd": 10000.0})


def reset():
    for s in list(db.load_open_trades()):
        db.delete_open_trade(s)


def hold(symbol, qty, entry):
    db.upsert_open_trade({"symbol": symbol, "qty": qty, "entry_price": entry,
                          "stop_loss": entry * 0.95, "take_profit": entry * 1.1})


def sig(symbol, price=100.0):
    return Signal(symbol=symbol, price=price, atr=2.0, score=80.0)


class KlineClient:
    """Returns series with a controlled correlation structure."""
    def __init__(self, groups):
        # groups: {"A": [syms...], "B": [syms...]} — same group => correlated
        self.groups = groups
        rng = np.random.default_rng(7)
        self._factors = {g: rng.normal(0, 0.01, 200) for g in groups}
        self._noise = rng.normal(0, 0.0015, (50, 200))
        self._idx = {}
    def _group_of(self, sym):
        for g, syms in self.groups.items():
            if sym in syms:
                return g
        return None
    def get_kline(self, symbol, bars=120, ktype=None):
        g = self._group_of(symbol)
        if g is None:
            raise RuntimeError(f"no data for {symbol}")
        i = self._idx.setdefault(symbol, len(self._idx))
        rets = self._factors[g] + self._noise[i % 50]
        close = 100 * np.cumprod(1 + rets)
        return pd.DataFrame({"close": close[-bars:]})


# ── 1. cumulative per-symbol exposure ──────────────────────────────────────
print("1  one symbol cannot become the account by stacking")
reset()
# 10,000 budget. A single order capped at MAX_POSITION_PCT is legal; five of
# them were five legal decisions with no cumulative check between them.
ok, why = concentration.check(sig("AAPL"), 30)        # 30 * 100 = 3,000 = 30%
check("a first position within the cap is allowed", ok)

hold("AAPL", 45, 100.0)                               # 4,500 held = 45%
ok, why = concentration.check(sig("AAPL"), 10)        # +1,000 -> 55%
check("a stack that would breach the cumulative cap is refused", not ok)
check("...and the reason names the cumulative limit",
      "cumulative" in why or "of budget" in why)

ok, _ = concentration.check(sig("AAPL"), 4)           # +400 -> 49%
check("a stack that stays inside it is allowed", ok)


# ── 2. gross exposure across every position ────────────────────────────────
print("\n2  the account as a whole has a limit")
reset()
for s in ("A", "B", "C", "D"):
    hold(s, 24, 100.0)                                # 4 * 2,400 = 9,600 = 96%
ok, why = concentration.check(sig("E"), 10)           # +1,000 -> 106%
check("an entry past the gross cap is refused", not ok)
check("...naming gross exposure", "gross" in why)
ok, _ = concentration.check(sig("E"), 3)              # +300 -> 99%
check("one that fits is allowed", ok)


# ── 3. correlated names are one bet ────────────────────────────────────────
print("\n3  names that move together count together")
reset()
client = KlineClient({"tech": ["NVDA", "AMD", "AVGO", "MU"],
                      "other": ["KO", "PG"]})
hold("NVDA", 25, 100.0)      # 2,500
hold("AMD", 25, 100.0)       # 2,500
hold("AVGO", 10, 100.0)      # 1,000  -> cluster 6,000 = 60%

# MU correlates with all three. 60% + the new 10% breaches the cluster cap
# while every single-symbol figure stays comfortably legal.
ok, why = concentration.check(sig("MU"), 10, client=client)
check("a correlated addition is refused", not ok)
check("...naming the names it moves with",
      "NVDA" in why or "AMD" in why or "AVGO" in why)
check("...and calling them one bet", "one bet" in why)

# Each symbol on its own is far inside the per-symbol cap — which is exactly
# why the cluster check has to exist.
_, mu_frac = concentration.symbol_exposure("MU", 1000.0)
check("the per-symbol cap alone would have allowed it",
      mu_frac < concentration.MAX_SYMBOL_EXPOSURE_PCT)

# An uncorrelated name of the same size is fine.
ok, why = concentration.check(sig("KO"), 10, client=client)
check("an uncorrelated addition is allowed", ok)


# ── 4. missing history does not become a trading halt ──────────────────────
print("\n4  a correlation that cannot be measured is not correlation")


class NoData:
    def get_kline(self, *a, **k):
        raise RuntimeError("quote feed down")


reset()
hold("NVDA", 25, 100.0)
ok, why = concentration.check(sig("AMD"), 10, client=NoData())
# Fail-open on THIS check only: refusing every entry whose history could not be
# fetched turns a quote outage into a halt, and the exposure caps still applied.
check("an entry is not refused because the data was unavailable", ok)

# ...but the hard caps still bite with no data at all.
reset()
hold("AAPL", 48, 100.0)
ok, why = concentration.check(sig("AAPL"), 10, client=NoData())
check("the exposure caps still apply without any kline data", not ok)


# ── 5. the snapshot reports what is actually held ──────────────────────────
print("\n5  concentration is reportable")
reset()
hold("AAPL", 20, 100.0)
hold("MSFT", 10, 100.0)
snap = concentration.snapshot()
check("gross notional is the sum of positions", abs(snap["gross_notional"] - 3000) < 1e-6)
check("as a fraction of budget", abs(snap["gross_pct"] - 0.30) < 1e-9)
check("with a per-symbol breakdown", set(snap["by_symbol"]) == {"AAPL", "MSFT"})
check("largest first", list(snap["by_symbol"]) == ["AAPL", "MSFT"])
check("and the caps it is measured against", set(snap["caps"]) ==
      {"symbol", "gross", "cluster"})


# ── 6. exposure is measured at entry, not at market ────────────────────────
print("\n6  a falling position does not free up room")
reset()
hold("AAPL", 45, 100.0)      # committed 4,500
# Marking to market would shrink this as the price falls and quietly allow a
# bigger add — loosening the cap exactly when it should not loosen.
notional, frac = concentration.symbol_exposure("AAPL")
check("exposure is the committed cost", abs(notional - 4500) < 1e-6)
check("...regardless of where the price is now", abs(frac - 0.45) < 1e-9)


# ── 7. open losses count toward the drawdown ───────────────────────────────
#
# current_drawdown_pct measured realized equity only, so a portfolio down 30%
# on everything it held reported 0% drawdown until something was sold. The
# circuit breaker could not fire in the situation it exists for, and could
# only fire once the damage was already booked.
print("\n7  unrealised losses reach the drawdown breaker")
from src import risk_manager                                  # noqa: E402

reset()
db.update_state({"budget_usd": 10000.0, "realized_pnl_total": 0.0,
                 "peak_equity": 10000.0})
risk_manager.set_live_equity(None)
check("flat and even is 0% drawdown",
      abs(risk_manager.current_drawdown_pct()) < 1e-9)

# Holding a 25% loss, nothing sold.
risk_manager.set_live_equity(7500.0)
dd = risk_manager.current_drawdown_pct()
check("a 25% open loss shows as a 25% drawdown", abs(dd - 25.0) < 1e-6)

# The asymmetry: paper PROFIT does not raise the high-water mark, because a
# gain that has not been sold is not a level to measure future losses from.
risk_manager.set_live_equity(13000.0)
check("a 30% open GAIN does not show a negative drawdown",
      risk_manager.current_drawdown_pct() == 0.0)
peak_before = float(db.get_state().get("peak_equity") or 0)
risk_manager.current_drawdown_pct()
check("...and does not move the peak",
      abs(float(db.get_state().get("peak_equity") or 0) - peak_before) < 1e-9)

# Realized losses still count when live equity is unknown.
risk_manager.set_live_equity(None)
db.update_state({"realized_pnl_total": -2000.0})
check("a realized loss still shows without a live mark",
      abs(risk_manager.current_drawdown_pct() - 20.0) < 1e-6)

# And the worse of the two is what counts: realized -2,000 with the remaining
# book down further should not report the smaller number.
risk_manager.set_live_equity(7000.0)
check("the worse of realized and marked equity is used",
      abs(risk_manager.current_drawdown_pct() - 30.0) < 1e-6)
risk_manager.set_live_equity(None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
