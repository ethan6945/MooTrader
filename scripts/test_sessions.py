"""Extended trading hours: the plumbing, not the strategy.

Run from repo root: .venv/bin/python scripts/test_sessions.py
No broker, no network — nothing here fetches a bar.

WHAT GOES WRONG SILENTLY HERE

  Every fault this file checks produces a run that COMPLETES and reports
  numbers. None of them raise.

  1. A shared cache. `AAPL_60M.parquet` holding all-hours bars poisons every
     regular-hours replay that reads it afterwards — including the parity runs
     the whole engine-agreement effort is measured by. The corrupted state
     outlives the experiment that created it and there is nothing in the output
     to say so.

  2. A window sized for 6.5-hour days while asking for 24-hour days. The broker
     returns the OLDEST max_count rows in [start, end], so overflowing the
     window hands back candles from weeks before the end date. That is a bug
     this repo has already had once, for timeframes rather than sessions, and
     it presented as a strategy that mysteriously stopped working.

  3. A lookback that silently shrinks. 500 hourly bars is ten weeks of regular
     hours and three weeks of all-hours — not enough for a 50-day average, so
     the indicators quietly degrade rather than fail.

  4. RTH drifting. Adding a feature must not change the default path. The
     regular-hours run has to be identical, by construction, to what it was
     before any of this existed.
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-sess-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

from moomoo import KLType                                    # noqa: E402
from src import sandbox                                      # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond:
        PASS += 1
    else:
        FAIL += 1


ET = datetime(2026, 8, 3, 9, 45)


def feed_for(sessions):
    """A SimFeed that has not fetched anything — only its plumbing is examined."""
    f = sandbox.SimFeed.__new__(sandbox.SimFeed)
    f.sessions = sessions
    return f


# ── 1. the default path does not move ──────────────────────────────────────
print("1  regular hours is untouched")
clk = sandbox.SimClock(ET, datetime(2026, 9, 1), sessions="RTH")
clk._now = datetime(2026, 8, 3, 9, 44)
check("09:44 is before the regular-hours phase", not clk.in_trade_phase())
clk._now = datetime(2026, 8, 3, 9, 45)
check("09:45 is inside it", clk.in_trade_phase())
clk._now = datetime(2026, 8, 3, 15, 29)
check("15:29 is inside it", clk.in_trade_phase())
clk._now = datetime(2026, 8, 3, 15, 30)
check("15:30 is past it", not clk.in_trade_phase())
check("the default config asks for regular hours",
      sandbox.SandboxConfig(start=ET, end=ET).sessions == "RTH")
check("...and passes no session to the broker at all",
      feed_for("RTH")._session_arg(KLType.K_60M) is None)
check("...keeping the unsuffixed cache name",
      feed_for("RTH").cache_name("AAPL", KLType.K_60M) == "AAPL_60M.parquet")
# The budget is a FLOOR plus whatever the replay's own window needs. It used
# to be a flat 500 whatever range was asked for — about a hundred trading days
# — so a 180-day run and a 360-day run replayed the same hundred days and
# reported them as the window requested.
check("with no window known, regular hours keeps the 500-bar floor",
      feed_for("RTH")._hourly_budget() == (500, 7))
_f = feed_for("RTH")
_f._fetch_start, _f._fetch_end = datetime(2025, 8, 20), datetime(2026, 8, 20)
_bars, _bpd = _f._hourly_budget()
# A calendar year is about 252 trading days; asserting a round bar count
# instead was how the first version of this got it wrong.
check("...and a year-long window asks for about a year of TRADING days",
      240 <= _bars / _bpd <= 300 and _bpd == 7)
_f._fetch_start, _f._fetch_end = datetime(2026, 8, 1), datetime(2026, 8, 20)
check("...while a short window never drops below the floor",
      _f._hourly_budget() == (500, 7))


# ── 2. the wider sessions actually widen ───────────────────────────────────
print("\n2  extended sessions open the hours they claim")
eth = sandbox.SimClock(ET, datetime(2026, 9, 1), sessions="ETH")
for hh, mm, want in ((3, 59, False), (4, 0, True), (8, 0, True),
                     (19, 59, True), (20, 0, False), (2, 0, False)):
    eth._now = datetime(2026, 8, 3, hh, mm)
    check(f"ETH {hh:02d}:{mm:02d} tradeable={want}", eth.in_trade_phase() is want)

alls = sandbox.SimClock(ET, datetime(2026, 9, 1), sessions="ALL")
for hh, want in ((0, True), (3, True), (12, True), (23, True)):
    alls._now = datetime(2026, 8, 3, hh, 0)
    check(f"ALL {hh:02d}:00 tradeable={want}", alls.in_trade_phase() is want)
alls._now = datetime(2026, 8, 8, 23, 0)      # Saturday
check("...but never on a weekend", not alls.in_trade_phase())


# ── 3. the weekend rollover lands on the session open ──────────────────────
print("\n3  skipping a weekend does not skip the session")
for sess, want in (("RTH", (9, 45)), ("ETH", (4, 0)), ("ALL", (0, 0))):
    c = sandbox.SimClock(ET, datetime(2026, 9, 1), sessions=sess)
    c._now = datetime(2026, 8, 7, 23, 30)     # Friday night
    c.advance(60)
    check(f"{sess} resumes at {want[0]:02d}:{want[1]:02d}",
          (c._now.hour, c._now.minute) == want and c._now.weekday() == 0)


# ── 4. caches cannot cross-contaminate ─────────────────────────────────────
print("\n4  each session keeps its own cache")
names = {s: feed_for(s).cache_name("AAPL", KLType.K_60M)
         for s in ("RTH", "ETH", "ALL")}
check("regular hours keeps the historical filename", names["RTH"] == "AAPL_60M.parquet")
check("all three filenames are distinct", len(set(names.values())) == 3)
check("no extended file can be read as the RTH one",
      names["RTH"] not in (names["ETH"], names["ALL"]))


# ── 5. daily bars are never fetched extended ───────────────────────────────
print("\n5  daily bars stay regular-hours in every mode")
# One row a day whichever session is asked for — extended hours would change
# what the row CONTAINS, and every daily-derived indicator was fitted on
# regular-hours dailies.
for sess in ("RTH", "ETH", "ALL"):
    f = feed_for(sess)
    check(f"{sess}: daily asks for no session",
          f._session_arg(KLType.K_DAY) is None)
    check(f"{sess}: ...and all three share one daily cache",
          f.cache_name("AAPL", KLType.K_DAY) == "AAPL_DAY.parquet")
check("ETH: hourly does ask", feed_for("ETH")._session_arg(KLType.K_60M) == "ETH")
check("ALL: hourly does ask", feed_for("ALL")._session_arg(KLType.K_60M) == "ALL")


# ── 6. the fetch window is scaled, not overflowed ───────────────────────────
print("\n6  a 24-hour day is not requested with a 6.5-hour window")
from src import moo_client                                    # noqa: E402

bpd_rth = moo_client._BARS_PER_TRADING_DAY[KLType.K_60M]
for sess, hours in (("ETH", 16.0), ("ALL", 24.0)):
    import math
    scaled = math.ceil(bpd_rth * moo_client._SESSION_HOURS[sess]
                       / moo_client._SESSION_HOURS["RTH"])
    check(f"{sess}: bars-per-day scales {bpd_rth}→{scaled}", scaled > bpd_rth)
    # The window cap is 1000/bpd trading days. Scaled, that window holds at
    # most 1000 bars — under the API's single-request return limit, which is
    # the whole point.
    check(f"{sess}: the capped window still fits under 1000 rows",
          max(2, int(1000 / scaled)) * scaled <= 1000)

# And the lookback in calendar terms does not shrink.
for sess in ("ETH", "ALL"):
    bars, bpd = feed_for(sess)._hourly_budget()
    rth_bars, rth_bpd = feed_for("RTH")._hourly_budget()
    check(f"{sess}: history stays ≥ the regular-hours lookback in DAYS",
          bars / bpd >= rth_bars / rth_bpd * 0.95)
    # And the window scaling applies to every session, not only RTH.
    _g = feed_for(sess)
    _g._fetch_start, _g._fetch_end = datetime(2025, 8, 20), datetime(2026, 8, 20)
    _wide, _ = _g._hourly_budget()
    check(f"{sess}: a year-long window asks for more than the floor",
          _wide > bars)


# ── 7. an unknown session falls back to regular hours ──────────────────────
print("\n7  an unrecognised session is not a wide-open one")
# Fail toward the narrow, validated session. The opposite default would let a
# typo trade the overnight book.
c = sandbox.SimClock(ET, datetime(2026, 9, 1), sessions="24H")
check("the clock falls back to RTH", c.sessions == "RTH")
c._now = datetime(2026, 8, 3, 3, 0)
check("...and 03:00 is not tradeable", not c.in_trade_phase())
check("the same fallback applies wherever a session is named",
      sandbox._normalise_sessions("24H") == "RTH"
      and sandbox._normalise_sessions("") == "RTH"
      and sandbox._normalise_sessions("rth") == "RTH")
check("...and the recognised ones survive it",
      [sandbox._normalise_sessions(s) for s in ("RTH", "ETH", "ALL")]
      == ["RTH", "ETH", "ALL"])
check("a fallen-back feed writes the RTH cache, not a '_24H' one",
      sandbox.SimFeed.cache_name(feed_for(sandbox._normalise_sessions("24H")),
                                 "AAPL", KLType.K_60M) == "AAPL_60M.parquet")


# ── 8. the participation cap is counted, not assumed ───────────────────────
print("\n8  how often the fill cap bit is recorded")
sandbox.reset_fill_stats()
check("stats start empty", sandbox.fill_stats()["attempted"] == 0)
sandbox._note_fill("overnight", "attempted")
sandbox._note_fill("overnight", "blocked")
sandbox._note_fill("RTH", "attempted")
st = sandbox.fill_stats()
check("attempts are totalled", st["attempted"] == 2)
check("...and split by session", st["by_session"]["overnight"]["blocked"] == 1)
check("...with regular hours separate", st["by_session"]["RTH"]["blocked"] == 0)
check("the returned stats are a copy, not the live dict",
      (st["by_session"]["RTH"].update({"blocked": 99}) or
       sandbox.fill_stats()["by_session"]["RTH"]["blocked"] == 0))

# Bar-start minute → session, the labelling every number above depends on.
for hh, mm, want in ((9, 30, "RTH"), (15, 30, "RTH"), (16, 0, "after-hours"),
                     (19, 30, "after-hours"), (4, 0, "pre-market"),
                     (9, 0, "pre-market"), (20, 0, "overnight"),
                     (3, 30, "overnight")):
    check(f"{hh:02d}:{mm:02d} is {want}",
          sandbox._bar_session(datetime(2026, 8, 3, hh, mm)) == want)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
