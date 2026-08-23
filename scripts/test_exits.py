"""Compare breakeven stop + scale-out exit variants on 180d and 360d windows."""
from __future__ import annotations
import logging, sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, _run_live_engine

logging.basicConfig(level=logging.WARNING, format="%(message)s")
import builtins as _b
_orig = _b.print
def print(*a, **k):
    k.setdefault("flush", True); _orig(*a, **k)

from src.config import settings

TOP_10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]

def base(days):
    return BacktestConfig(
        days=days, timeframe="HOUR_1", threshold=70.0,
        tickers=TOP_10,   # match active watchlist
        account_usd=settings.account_usd, risk_per_trade=settings.risk_per_trade,
        max_position_pct=0.50, max_hold_days=settings.max_hold_days,
        tp_atr_mult=8.0, sl_atr_mult=3.5,
        max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
        apply_mr_strategy=False,
    )

variants = [
    ("baseline (no extras)", {}),
    ("breakeven @ +1R",      {"use_breakeven_stop": True, "breakeven_trigger_r": 1.0}),
    ("breakeven @ +1.5R",    {"use_breakeven_stop": True, "breakeven_trigger_r": 1.5}),
    ("scale-out (2R/4R)",    {"use_scale_out": True, "tp1_r": 2.0, "tp2_r": 4.0}),
    ("scale-out (3R/5R)",    {"use_scale_out": True, "tp1_r": 3.0, "tp2_r": 5.0}),
    ("BE@1R + scale 2R/4R",  {"use_breakeven_stop": True, "breakeven_trigger_r": 1.0,
                              "use_scale_out": True, "tp1_r": 2.0, "tp2_r": 4.0}),
    ("BE@1.5R + scale 3R/5R",{"use_breakeven_stop": True, "breakeven_trigger_r": 1.5,
                              "use_scale_out": True, "tp1_r": 3.0, "tp2_r": 5.0}),
]

for days in [180, 360]:
    print(f"\n=== {days}-day window (top-10 watchlist, mpp=0.50, tp=8/sl=3.5) ===")
    print(f"{'variant':<26} {'trades':>6} {'WR%':>5} {'PF':>5} {'$/day':>8} {'Sortino':>8} {'MaxDD%':>7}")
    print("-" * 80)
    cache = prefetch_data(base(days))
    narrowed = {**cache, "per_ticker":
                {k: v for k, v in cache["per_ticker"].items() if k in TOP_10}}
    for name, overrides in variants:
        cfg = replace(base(days), **overrides)
        r = _run_live_engine(cfg)
        m = r["metrics"]
        print(f"{name:<26} {m.get('total_trades', 0):>6} "
              f"{m.get('win_rate_pct', 0):>5.1f} {m.get('profit_factor', 0):>5.2f} "
              f"{m.get('net_pnl_usd', 0)/days:>+8.2f} {m.get('sortino_ratio', 0):>+8.2f} "
              f"{m.get('max_drawdown_pct', 0):>7.2f}")
