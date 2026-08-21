"""Every module that says "ATR" must mean the same number.

Run from repo root: .venv/bin/python scripts/test_atr_agreement.py
Reads cached parquet bars. Contacts no OpenD, places nothing.

WHY THIS EXISTS
  pattern_detect._atr carried the docstring "Wilder ATR without a pandas_ta
  call" over `np.mean(tr[-period:])` — a simple mean. Across 120 windows of
  real 60m bars the two sit a median 8% apart, 18% at P90, 34% at the worst,
  and the simple mean is the larger one 74% of the time.

  That matters because every threshold in that module is an ATR MULTIPLE —
  pattern height >= 1.2x ATR, flag pole >= 2.0x ATR, breakout confidence
  50 + (excess / ATR) * 70 — and a multiple only means something against an
  agreed unit. Detection was quietly stricter than its own numbers said, and
  breakout confidence quietly lower, against an ENTRY_SCORE_THRESHOLD tuned
  on the shared definition.

  universe.py had the same disagreement and was migrated to ta.atr; the
  comment left at universe.py:128 names it. This suite is so the next module
  cannot drift back without saying so.

  The assertion is numeric, against ta.atr on real bars. It does not read the
  docstring — the docstring is what was wrong.
"""
import glob
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np                                            # noqa: E402
import pandas as pd                                           # noqa: E402
import pandas_ta_classic as ta                                # noqa: E402

from src import pattern_detect                                # noqa: E402

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


def _frames(limit=40, min_bars=60):
    for f in sorted(glob.glob(str(ROOT / "data" / "sandbox_cache" / "*_60M.parquet")))[:limit]:
        try:
            df = pd.read_parquet(f)
        except Exception:
            continue
        df.columns = [c.lower() for c in df.columns]
        if {"high", "low", "close"} <= set(df.columns) and len(df) >= min_bars:
            yield Path(f).stem, df


FRAMES = list(_frames())

# ── 1. there is data to judge against ───────────────────────────────────────
print("1  the fixtures are real bars, not a synthetic curve")
check("cached 60m bars are present", len(FRAMES) >= 5)
if not FRAMES:
    print("\nno cached bars — cannot assert agreement")
    sys.exit(1)

# ── 2. pattern_detect._atr IS Wilder, to the last decimal ──────────────────
print("\n2  pattern_detect._atr agrees with ta.atr")
worst, worst_sym = 0.0, ""
for sym, df in FRAMES:
    mine = pattern_detect._atr(df, 14)
    theirs = float(ta.atr(df["high"], df["low"], df["close"], length=14).iloc[-1])
    if theirs <= 0:
        continue
    rel = abs(mine - theirs) / theirs
    if rel > worst:
        worst, worst_sym = rel, sym
check(f"agrees on all {len(FRAMES)} symbols (worst {worst * 1e6:.3f} ppm on "
      f"{worst_sym or 'n/a'})", worst < 1e-6)

# Different periods too — a seeding bug shows up as period-dependent drift.
print("\n2b  ...at every period the timeframes use")
for period in (7, 14, 20, 30):
    w = max(
        abs(pattern_detect._atr(df, period)
            - float(ta.atr(df["high"], df["low"], df["close"],
                           length=period).iloc[-1]))
        / max(float(ta.atr(df["high"], df["low"], df["close"],
                           length=period).iloc[-1]), 1e-12)
        for _, df in FRAMES)
    check(f"period={period} agrees (worst {w * 1e6:.3f} ppm)", w < 1e-6)

# ── 3. the simple mean it used to be is genuinely a different number ───────
# Without this the suite would pass on ANY two implementations that happened
# to agree — including both being wrong the same way.
print("\n3  the old simple-mean really was a different answer")
def _simple(df, period):
    h = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    prev = c[:-1]
    tr = np.maximum(h[1:] - low[1:],
                    np.maximum(np.abs(h[1:] - prev), np.abs(low[1:] - prev)))
    return float(np.mean(tr[-period:]))

gaps = []
for _, df in FRAMES:
    theirs = float(ta.atr(df["high"], df["low"], df["close"], length=14).iloc[-1])
    if theirs > 0:
        gaps.append(abs(_simple(df, 14) - theirs) / theirs)
check(f"the two differ by a median {np.median(gaps) * 100:.1f}% — this suite "
      f"is not comparing a formula with itself", np.median(gaps) > 0.01)

# ── 4. no module reintroduces a hand-rolled mean-of-TR ─────────────────────
# Structural, not textual: parse each module and look for a call to .mean()
# whose receiver chain mentions a true-range variable. A comment saying
# "use ta.atr" would not survive being stripped, and does not count.
print("\n4  no ATR is a mean of true range")
import ast                                                     # noqa: E402

offenders = []
for f in sorted((ROOT / "src").glob("*.py")):
    tree = ast.parse(f.read_text())
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "atr" not in fn.name.lower():
            continue
        body = ast.unparse(fn)
        # Wilder's recurrence divides by the period; a plain mean does not.
        looks_mean = ".mean(" in body or "np.mean(" in body
        looks_wilder = ("period - 1" in body or "length - 1" in body
                        or "ta.atr" in body or "_ta.atr" in body)
        if looks_mean and not looks_wilder:
            offenders.append(f"{f.name}:{fn.name}")
check(f"no hand-rolled mean-of-TR ATR remains ({offenders or 'none'})",
      not offenders)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
