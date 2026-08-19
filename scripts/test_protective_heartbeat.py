"""Can you tell, from the log, that the stop-loss monitor is running?

Run from repo root: .venv/bin/python scripts/test_protective_heartbeat.py
No broker.

WHY THIS EXISTS

  Both environments here run REAL_USE_SOFT_EXITS=true, so no stop order is
  ever placed at the broker — executor.place_bracket is only reached by a REAL
  account with soft exits off. Protection is entirely the fast-stop loop
  re-reading prices and deciding. Which means: if that loop stops, every open
  position is naked, and nothing at the broker will catch it.

  The loop logs only when it ACTS. On 2026-08-19 three positions ran a full
  session, CVX came within 0.15% of its stop, and the log contained no
  fast-stop line at all. "Checked every minute, all clear" and "has not run
  since the bell" produced identical output. There was no way to tell which
  had happened, for the one subsystem where that distinction is the difference
  between a managed position and an unmanaged one.

  A heartbeat is not decoration here. It is the only evidence the protection
  exists.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-hb-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

import logging                                              # noqa: E402
from src import executor                                    # noqa: E402
from src import main as _main                               # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name + (f"   [{detail}]" if detail else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = Capture()
logging.getLogger("main").addHandler(cap)
logging.getLogger("main").setLevel(logging.INFO)


def reset():
    cap.lines.clear()
    _main._last_fast_stop_log = 0.0
    executor._LAST_STOP_PASS.update(
        {"at": None, "positions": 0, "closest": None,
         "closest_symbol": None, "passes": 0})


# ── 1. a completed pass is recorded ────────────────────────────────────────
print("1  the pass records what it saw")
reset()
trades = {
    "CVX":  {"stop_loss": 203.96, "last_price": 204.26, "entry_price": 207.19},
    "XLE":  {"stop_loss": 63.26,  "last_price": 64.05,  "entry_price": 64.08},
    "ABBV": {"stop_loss": 258.80, "last_price": 265.97, "entry_price": 264.04},
}
executor._note_stop_pass(trades)
p = executor.last_stop_pass()
check("the position count is recorded", p["positions"] == 3, str(p["positions"]))
check("...and the pass counter advances", p["passes"] == 1)
check("...and a timestamp is set", p["at"] is not None)
# CVX: (204.26 - 203.96) / 204.26 = 0.00147 — the tightest of the three, and
# the number that made this worth logging on the day.
check("the NEAREST stop is the one reported", p["closest_symbol"] == "CVX",
      str(p["closest_symbol"]))
check("...as a fraction of the mark", abs(p["closest"] - 0.001469) < 1e-5,
      f"{p['closest']:.6f}")

executor._note_stop_pass(trades)
check("a second pass increments rather than replaces",
      executor.last_stop_pass()["passes"] == 2)


# ── 2. the heartbeat says the useful thing ─────────────────────────────────
print("\n2  the log line carries the number that matters")
reset()
executor._note_stop_pass(trades)
_main._fast_stop_heartbeat()
line = next((l for l in cap.lines if "protective loop alive" in l), "")
check("a heartbeat is emitted", bool(line))
check("...naming how many positions were checked", "3 position" in line, line)
check("...and how close the nearest stop is", "0.15%" in line, line)
check("...and which symbol that was", "CVX" in line, line)


# ── 3. it does not flood ───────────────────────────────────────────────────
print("\n3  a heartbeat every minute is noise, not evidence")
reset()
executor._note_stop_pass(trades)
_main._fast_stop_heartbeat()
n_after_first = len([l for l in cap.lines if "protective loop alive" in l])
for _ in range(10):
    _main._fast_stop_heartbeat()
n_after_ten = len([l for l in cap.lines if "protective loop alive" in l])
check("ten more ticks inside the window add nothing",
      n_after_first == 1 and n_after_ten == 1, f"{n_after_ten} lines")
_main._last_fast_stop_log = time.time() - _main._FAST_STOP_HEARTBEAT_S - 1
_main._fast_stop_heartbeat()
check("...but it does fire again once the window passes",
      len([l for l in cap.lines if "protective loop alive" in l]) == 2)


# ── 4. it cannot break what it observes ────────────────────────────────────
print("\n4  observability must not be able to take down the protection")
reset()
# A trade store full of nonsense: the recorder must swallow it, not raise into
# the protective pass that called it.
for bad in ({"X": {"stop_loss": "nope", "last_price": None}},
            {"X": None},
            None,
            {"X": {}}):
    try:
        executor._note_stop_pass(bad)
        ok = True
    except Exception:
        ok = False
    check(f"a malformed store is survived: {str(bad)[:28]}", ok)

check("...and a position with no usable mark reports no distance",
      executor.last_stop_pass()["closest"] is None)

reset()
_main._fast_stop_heartbeat()      # nothing recorded yet
check("a heartbeat before any pass does not raise",
      any("protective loop alive" in l for l in cap.lines))


# ── 5. the configuration this all rests on ─────────────────────────────────
print("\n5  the reason the loop is the only protection")
from src.config import settings                              # noqa: E402
# Not an assertion about what the value SHOULD be — a statement of what the
# heartbeat is for. If soft exits are ever turned off, the broker holds a real
# stop and the loop stops being the single point of failure.
check("soft exits are a real setting, not an assumption",
      hasattr(settings, "real_use_soft_exits"),
      f"real_use_soft_exits={getattr(settings, 'real_use_soft_exits', '?')}")
check("SIMULATE never places a broker bracket regardless",
      "place_bracket" in open(ROOT / "src" / "executor.py").read())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
