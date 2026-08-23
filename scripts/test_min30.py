"""Compare HOUR_1 vs MIN_30 timeframe on the new 19-ticker watchlist.

Reuses a single prefetch where possible (different timeframes need different
data so each runs its own prefetch).
"""
from __future__ import annotations

import logging
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backtest import BacktestConfig, _run_live_engine  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s | %(message)s")
    from src.config import settings
    rows = []
    for tf in ("HOUR_1", "MIN_30"):
        cfg = BacktestConfig(
            days=180,
            timeframe=tf,
            threshold=60.0,
            tickers=[],
            account_usd=settings.account_usd,
            risk_per_trade=settings.risk_per_trade,
            max_position_pct=settings.max_position_pct,
            max_hold_days=settings.max_hold_days,
            tp_atr_mult=settings.tp_atr_mult,
            sl_atr_mult=settings.sl_atr_mult,
            max_gap_pct=settings.max_gap_pct,
        )
        print(f"\n=== prefetching {tf} ===")
        cache = prefetch_data(cfg)
        print(f"=== simulating {tf} ({len(cache['per_ticker'])} tickers) ===")
        result = _run_live_engine(cfg)
        m = result["metrics"]
        rows.append({
            "tf": tf,
            "trades": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0),
            "pf": m.get("profit_factor", 0),
            "net": m.get("net_pnl_usd", 0),
            "daily": m.get("net_pnl_usd", 0) / 180,
            "sortino": m.get("sortino_ratio", 0),
            "max_dd_pct": m.get("max_drawdown_pct", 0),
        })

    print("\n" + "=" * 90)
    print("  TIMEFRAME COMPARISON — HOUR_1 vs MIN_30 (all other settings = .env)")
    print("=" * 90)
    print(f"  {'tf':<8} {'trades':>6} {'WR%':>5} {'PF':>5} "
          f"{'net $':>10} {'$/day':>8} {'Sortino':>8} {'MaxDD%':>7}")
    print("  " + "-" * 86)
    for r in rows:
        print(f"  {r['tf']:<8} {r['trades']:>6} {r['wr']:>5.1f} "
              f"{r['pf']:>5.2f} {r['net']:>+10.2f} {r['daily']:>+8.2f} "
              f"{r['sortino']:>+8.2f} {r['max_dd_pct']:>7.2f}")


if __name__ == "__main__":
    main()
