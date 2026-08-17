"""Does trading pre-market, after-hours or overnight add anything?

Run from repo root:  .venv/bin/python scripts/session_hours_experiment.py
Needs OpenD for the first run of each session (the parquet cache is per
session, so an extended-hours fetch cannot overwrite the RTH one).

WHAT IS BEING ASKED
  The strategy has only ever been replayed on regular hours. Extending it is
  attractive for an obvious reason — more bars, more chances — and the reason
  it has not been done is that "more chances" is only true if those bars can
  actually be traded.

  Measured before this ran, median hourly volume over 30 days across four
  large caps (AAPL/MSFT/NVDA/HPE) — the OPTIMISTIC end of the pool, since the
  full cached universe is thinner still (overnight median 1,555 shares):

      regular hours   3,216,097   1.000x
      after-hours     1,393,117   0.433x
      pre-market         60,011   0.019x
      overnight           8,531   0.003x

  HPE overnight trades 1,841 shares an hour. A $3,600 position at $21 is 170
  shares — nine percent of everything that trades in that hour, from one order.
  A backtest that fills it at the touch is not modelling a trade; it is
  modelling being the only participant.

  So the fill model got a volume participation cap (SimBroker,
  _MAX_VOLUME_PARTICIPATION = 0.10) BEFORE this experiment was allowed to run.
  It does not bind in regular hours — the 45-day parity run returns byte-
  identical numbers with and without it.

  It turned out not to bind anywhere else either, at this account size. That
  is recorded below rather than quietly dropped: the cap was the safeguard this
  experiment was gated on, and it did almost nothing, which changes how much
  the overnight column can be trusted.

WHAT IS HELD CONSTANT
  Same window, same universe, same parameters, same scan interval. The only
  difference between the three runs is which bars exist.

WHAT TO LOOK AT
  Not the PnL first. The cap-bind rate first: if most extended-hours entries
  were trimmed by the participation cap, the extra trades are not opportunities
  the account could have taken, and the PnL attached to them is fiction that
  happens to be signed.

FIRST READING — 45 days to 2026-08-18, and it does not say what it looks like

    sessions   trades   net PnL   win rate      entries by session
    RTH            29   -469.15      17.2%      RTH 29
    ETH            49   -293.90      18.4%      RTH 42, pre 6, after 1
    ALL            51   -175.76      21.6%      RTH 37, pre 5, after 3, night 6

  Read as "opening more hours cut the loss by 63%", that is wrong, for a reason
  visible in the same table: the REGULAR-HOURS entry count moved. 29, then 42,
  then 37 — from the identical regular-hours bars. Slot contention cannot
  explain it; overnight positions occupy slots, which would push the count
  DOWN, and it went up 45%.

  The cause is that indicators are computed over the bar SERIES, and the series
  changed. At shared timestamps the prices are byte-identical (max close
  difference 0.0000 across every cached symbol), but interleaving near-empty
  overnight bars changes every rolling statistic computed over them. Measured
  on the twelve symbols cached in both forms, at the same regular-hours bar:

      ATR(14)   median −33.7%   (range −18.8% to −45.7%)
      RSI(14)   swings up to ±28 points

  ATR is not decorative here. stop = price − SL_ATR_MULT × ATR, and
  risk_manager sizes qty = risk_dollars / (price − stop). A 33.7% smaller ATR
  is a 34% tighter stop and a ~51% larger position. So the three rows above are
  not one strategy on three schedules; they are three different risk models,
  and the "improvement" is mostly stops being cut sooner and smaller.

  The extended-hours trades, taken on their own terms, all lost:

      pre-market   −91.15      after-hours   −41.18      overnight   −2.38

  So nothing here supports trading extended hours, and the comparison is not
  clean enough to have supported it even if the totals had been positive.

WHAT THE PARTICIPATION CAP ACTUALLY CAUGHT
  Almost nothing, and that is worth stating plainly rather than leaving as an
  implied safeguard. Across all three runs it trimmed ZERO fills. It blocked 10
  of 16 overnight attempts, and only because those bars traded under ten shares
  — the cap was refusing dead bars, not sizing against a thin book.

  The reason is account size. Ten percent of a 1,555-share overnight hour is
  155 shares; a $2,000 position is often fewer. Participation is not this
  account's binding constraint, so the cap did not make the overnight column
  trustworthy — it only stopped the most obvious fiction.

  What IS unmodelled is the spread: the fill model charges a flat 5bps one-way
  in every session. A high/low proxy does not settle it either way — median
  bar-to-bar price movement is actually SMALLER outside regular hours (RTH
  49.5bps, pre 26.0, after 20.0, night 19.5), because less happens, which is
  not the same as being cheap to cross. Historical klines cannot answer what
  the spread was. Until something can, extended-hours PnL carries an unbounded
  unmeasured cost, and that is the honest state of it.
"""
import collections
import json
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/Users/ethan/Desktop/moomoo trader")

from src import sandbox                                    # noqa: E402
from src.sandbox import SandboxConfig, run_sandbox         # noqa: E402

DAYS = 45
end = datetime.now().replace(hour=16, minute=0, second=0, microsecond=0)
start = end - timedelta(days=DAYS)

# Bar-start minute → which session that bar belongs to. Hourly US bars start on
# the half hour inside regular trading, so 9:30 is the first RTH bar and 15:30
# is the last.
def session_of(dt: datetime) -> str:
    m = dt.hour * 60 + dt.minute
    if 9 * 60 + 30 <= m < 16 * 60:
        return "RTH"
    if 4 * 60 <= m < 9 * 60 + 30:
        return "pre-market"
    if 16 * 60 <= m < 20 * 60:
        return "after-hours"
    return "overnight"


results = {}
for sessions in ("RTH", "ETH", "ALL"):
    print(f"\n{'='*60}\n  sessions = {sessions}\n{'='*60}", flush=True)
    sandbox.reset_fill_stats()
    cfg = SandboxConfig(start=start, end=end, sessions=sessions)
    r = run_sandbox(cfg)
    trades = r.get("trades", [])
    s = r.get("summary", {})

    by_session = collections.Counter()
    pnl_by_session = collections.defaultdict(float)
    for t in trades:
        try:
            d = datetime.fromisoformat(str(t.get("entry_t")))
        except Exception:
            continue
        sess = session_of(d)
        by_session[sess] += 1
        pnl_by_session[sess] += float(t.get("net_pnl") or t.get("pnl") or 0.0)

    results[sessions] = {
        "n_trades": len(trades),
        "net_pnl": s.get("net_pnl_usd"),
        "win_rate_pct": s.get("win_rate_pct"),
        "return_pct": s.get("total_return_pct"),
        "scans": s.get("scans"),
        "entries_by_session": dict(by_session),
        "net_pnl_by_session": {k: round(v, 2) for k, v in pnl_by_session.items()},
        "fill_stats": sandbox.fill_stats(),
    }
    fs = results[sessions]["fill_stats"]
    print(f"\n  {sessions}: {len(trades)} trades  net ${s.get('net_pnl_usd')}  "
          f"WR {s.get('win_rate_pct')}%", flush=True)
    print(f"  entries by session: {dict(by_session)}", flush=True)
    print(f"  fills trimmed by the volume cap: {fs['capped']}/{fs['attempted']} "
          f"({fs['blocked']} blocked outright)", flush=True)

print()
print(json.dumps(results, indent=2, default=str))
