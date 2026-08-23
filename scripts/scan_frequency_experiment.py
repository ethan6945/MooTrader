"""Does scanning faster still help, now that the replay cannot see the future?

    .venv/bin/python scripts/scan_frequency_experiment.py [--days 45]

One engine, one window, one variable: scan_interval_min. Everything else — code
path, data, config — is identical, so any difference IS the frequency. That is
why this is more trustworthy than any cross-engine comparison.

WHY IT HAD TO BE RE-ASKED
  SCAN_INTERVAL_MIN=15 rests on a "2026-07-04 timing audit" that survives only
  as a comment: no document, no data, no git history. And until 2026-08-15 the
  sandbox truncated bars on `index <= now` while bars carry their START time,
  so a scan inside a forming bar received that bar's FINISHED close. Faster
  scanning would look better under that bug for a reason that has nothing to do
  with trading: more scans landing mid-bar means more chances to read a price
  from the future.

FIRST CLEAN RESULT (45 days to 2026-08-17, 15 tickers)
      15min   28 trades   net -$437.98   WR 17.9%
      30min   27 trades   net -$410.63   WR 18.5%
      60min   22 trades   net -$270.60   WR 22.7%

  Monotonic: faster is worse. And the entry times say why — at 60min all 22
  entries land on the bar boundary (:30); the extra ones at 15min land at :00,
  :15 and :45, where the scan sees NO new information and simply acts later at
  a worse price.

  Caveat that matters: every setting LOSES money here. This is evidence that 15
  is worse than 60, not that 60 is good. One window, 22-28 trades. Confirm on
  more windows before changing a live parameter.
"""
import json, sys, collections
from datetime import datetime, timedelta
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest_v4 import V4Config, run_v4
from src.config import ROOT

DAYS = 45
end = datetime.now().replace(hour=16, minute=0, second=0, microsecond=0)
start = end - timedelta(days=DAYS)

results = {}
for interval in (15, 30, 60):
    cfg = V4Config(start=start, end=end, scan_interval_min=interval)
    r = run_v4(cfg)
    trades = r.get("trades", [])
    s = r.get("summary", {})
    # Where in the hour did entries land? A signal that only exists because of
    # a mid-bar scan would cluster off the hour boundary.
    minutes = collections.Counter()
    for t in trades:
        ts = t.get("entry_t") or ""
        try:
            m = datetime.fromisoformat(str(ts)).minute
            minutes[m] += 1
        except Exception:
            pass
    results[interval] = {
        "n_trades": len(trades),
        "net_pnl": s.get("net_pnl_usd"),
        "gross_pnl": s.get("gross_pnl_usd"),
        "win_rate_pct": s.get("win_rate_pct"),
        "return_pct": s.get("total_return_pct"),
        "entry_minutes": dict(sorted(minutes.items())),
    }
    print(f"  {interval:>2}min: {len(trades)} trades  net ${s.get('net_pnl_usd')}  "
          f"WR {s.get('win_rate_pct')}%", flush=True)

print()
print(json.dumps(results, indent=2, default=str))

