"""Which bar the live scan is allowed to score, and why it matters.

Run from repo root: .venv/bin/python scripts/test_forming_bar.py
No broker, no network — every frame here is built in memory.

THE INCIDENT

  2026-08-19. The bot ran a full morning without placing a single order. Every
  candidate cleared the score threshold, passed the AI check, and was then
  refused by executor's 20bps chase gate:

      ABBV: market ran past signal ($264.09 > $258.92 +20.0 bps) — skipped

  None of those refusals was a judgement about the market. $258.92 was the
  previous DAY's close. The current bar — stamped 2026-08-19 10:30, sitting at
  264.85 — had been discarded as "still forming" at 10:38, eight minutes after
  it closed. Verified live: its close, high and volume did not move across 75
  seconds while the quote did.

  _drop_forming_bar dropped the last intraday row unconditionally, on the
  premise that the broker "returns bars up to and including the bar that
  contains now". The broker stamps US intraday bars by their END time. The
  proof is a single day of ABBV hourly bars:

      10:30  11:30  12:30  13:30  14:30  15:30  16:00
                                                  ^ 1,958,350 shares

  Seven bars ending at 16:00, the last carrying the closing auction against a
  typical 400K. Under start-stamping that day would read 09:30 … 15:30, and a
  16:00 bar would begin after the market closed.

  So a bar stamped T covers (T − period, T] and is closed once now >= T. The
  fix drops it only while now < T.

WHY A TEST AND NOT JUST THE FIX

  This function decides which bar every live score is computed on, so it also
  decides how live compares to the backtest — the parity work measures exactly
  that. Getting it wrong in the other direction is worse than the bug it
  replaces: keeping a genuinely forming bar is look-ahead, and look-ahead
  flatters results instead of suppressing them.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-bar-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

import pandas as pd                                          # noqa: E402
from src import main as _main                                # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name + (f"   [{detail}]" if detail else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1


def frame(end_times):
    """An hourly frame stamped by END time, the way the broker sends it."""
    idx = pd.to_datetime(list(end_times))
    return pd.DataFrame(
        {"open": range(len(idx)), "high": range(len(idx)),
         "low": range(len(idx)), "close": range(len(idx)),
         "volume": [1000] * len(idx)},
        index=idx)


class FixedClock:
    def __init__(self, when):
        self.when = when

    def ny_now(self):
        return self.when


def at(when, df, tf="HOUR_1"):
    real = _main.clock
    _main.clock = FixedClock(when)
    try:
        return _main._drop_forming_bar(df, tf)
    finally:
        _main.clock = real


# The morning that produced the incident, reconstructed.
YESTERDAY = ["2026-08-18 14:30", "2026-08-18 15:30", "2026-08-18 16:00"]
TODAY_ONE = YESTERDAY + ["2026-08-19 10:30"]


# ── 1. a closed bar is kept ────────────────────────────────────────────────
print("1  a bar whose period has ended is real data")
df = frame(TODAY_ONE)
out = at(datetime(2026, 8, 19, 10, 38), df)
check("10:38 keeps the bar stamped 10:30", len(out) == len(df),
      str(out.index[-1]))
check("...so the signal is today's bar, not yesterday's",
      str(out.index[-1]).startswith("2026-08-19"), str(out.index[-1]))

out = at(datetime(2026, 8, 19, 10, 30), df)
check("10:30 exactly — the period has ended, keep it", len(out) == len(df))


# ── 2. a forming bar is still dropped ──────────────────────────────────────
print("\n2  a bar whose period has NOT ended is look-ahead")
out = at(datetime(2026, 8, 19, 10, 29, 59), df)
check("10:29:59 drops the 10:30 bar", len(out) == len(df) - 1,
      str(out.index[-1]))
check("...falling back to the previous close",
      str(out.index[-1]) == "2026-08-18 16:00:00", str(out.index[-1]))

out = at(datetime(2026, 8, 19, 9, 45), df)
check("09:45 drops it too", len(out) == len(df) - 1)


# ── 3. before the first bar of the day closes ──────────────────────────────
print("\n3  the state that produced the incident")
# At 10:12 the feed had published nothing for today: the last row was
# yesterday's 16:00, long closed. The old code dropped it anyway, so the
# signal became 15:30 — two periods stale — and every entry read as a chase.
df_pre = frame(YESTERDAY)
out = at(datetime(2026, 8, 19, 10, 12), df_pre)
check("yesterday's last bar is kept, not discarded as forming",
      len(out) == len(df_pre) and str(out.index[-1]) == "2026-08-18 16:00:00",
      str(out.index[-1]))
# The old behaviour, stated so the regression is visible if it returns.
check("...which is one bar fresher than the old unconditional drop gave",
      str(df_pre.iloc[:-1].index[-1]) == "2026-08-18 15:30:00")


# ── 4. daily bars are untouched ────────────────────────────────────────────
print("\n4  daily bars are not intraday")
d = frame(["2026-08-17", "2026-08-18", "2026-08-19"])
out = at(datetime(2026, 8, 19, 10, 38), d, tf="DAY")
check("a DAY frame is returned whole", len(out) == len(d))
out = at(datetime(2026, 8, 19, 10, 38), d, tf="day")
check("...case-insensitively", len(out) == len(d))


# ── 5. every intraday timeframe is covered ─────────────────────────────────
print("\n5  the rule applies to each intraday timeframe")
for tf, closed, forming in (
        ("HOUR_1", datetime(2026, 8, 19, 10, 31), datetime(2026, 8, 19, 10, 29)),
        ("MIN_30", datetime(2026, 8, 19, 10, 31), datetime(2026, 8, 19, 10, 29)),
        ("MIN_10", datetime(2026, 8, 19, 10, 31), datetime(2026, 8, 19, 10, 29))):
    f = frame(["2026-08-19 09:30", "2026-08-19 10:30"])
    check(f"{tf}: kept once closed", len(at(closed, f, tf)) == 2)
    check(f"{tf}: dropped while forming", len(at(forming, f, tf)) == 1)


# ── 6. degenerate frames do not raise ──────────────────────────────────────
print("\n6  it cannot take down a scan")
one = frame(["2026-08-19 10:30"])
check("a single-row frame is returned as-is",
      len(at(datetime(2026, 8, 19, 10, 38), one)) == 1)
empty = frame([])
check("an empty frame is returned as-is",
      len(at(datetime(2026, 8, 19, 10, 38), empty)) == 0)


# ── 7. an unreadable index drops, it does not keep ─────────────────────────
print("\n7  when it cannot tell, it errs toward stale — never toward early")
# Being a bar late is a worse signal. Being a bar early is look-ahead, and only
# one of those two makes the results look better than they were.
broken = frame(["2026-08-19 09:30", "2026-08-19 10:30"])
broken.index = ["not", "times"]
out = at(datetime(2026, 8, 19, 10, 38), broken)
check("an index it cannot compare falls back to dropping",
      len(out) == 1, f"{len(out)} rows")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
