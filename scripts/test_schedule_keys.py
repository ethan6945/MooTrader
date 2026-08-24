"""Every job the scheduler asks about must exist in the schedule tables.

WHY THIS EXISTS

  Renaming a job on 2026-08-23 (sandbox_diff → v4_calibration) updated both
  registration lists in main.py and missed cron_state.WEEKLY_SCHEDULE. The
  first thing the scheduler does on start is ask
  expected_last_fire("v4_calibration") for its catch-up decision, that raised
  KeyError, and the trading loop crash-looped for three days.

  Thirty checks were green through all of it. Every one of them tests behaviour
  inside a module; none of them starts the scheduler, so the one path that runs
  before anything else was the one path nothing covered.

  This reads main.py for the job keys it actually passes to cron_state and
  resolves every one. It is a spelling test, which is exactly what the bug was.

  Run from repo root: .venv/bin/python scripts/test_schedule_keys.py
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MMT_HOME", tempfile.mkdtemp(prefix="mmt-sched-"))
for _d in ("data", "logs", "config"):
    (Path(os.environ["MMT_HOME"]) / _d).mkdir(parents=True, exist_ok=True)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name + (f"   [{detail}]" if detail else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1


from src import cron_state as cs                                  # noqa: E402

# The cron_state entry points that take a job key, and the table each reads.
RESOLVERS = {
    "expected_last_fire": cs.expected_last_fire,
    "expected_last_fire_daily_kl": cs.expected_last_fire_daily_kl,
}

src = (ROOT / "src" / "main.py").read_text()
tree = ast.parse(src)

# Collect every literal job key main.py hands to a cron_state resolver.
asked: dict[str, set[str]] = {k: set() for k in RESOLVERS}
recorded: set[str] = set()
for node in ast.walk(tree):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        continue
    fn = node.func.attr
    if not node.args or not isinstance(node.args[0], ast.Constant):
        continue
    key = node.args[0].value
    if not isinstance(key, str):
        continue
    if fn in RESOLVERS:
        asked[fn].add(key)
    elif fn == "record_run":
        recorded.add(key)

print("1  every key main.py resolves is in a schedule table")
total = sum(len(v) for v in asked.values())
check("main.py asks about at least one job", total > 0, f"{total} keys")
for fn, keys in asked.items():
    for key in sorted(keys):
        try:
            RESOLVERS[fn](key)
            ok, why = True, ""
        except KeyError as e:
            ok, why = False, f"KeyError {e} — missing from the {fn} table"
        except Exception as e:
            ok, why = False, f"{type(e).__name__}: {e}"
        check(f"{fn}({key!r}) resolves", ok, why)

# NOT ASSERTED: that every record_run() key appears in these tables. Jobs like
# monthly_optuna are scheduled through expected_last_fire_monthly(day, hour,
# minute), which takes a date rather than a key — so their bookkeeping name
# legitimately has no table entry. Asserting it produced seven failures about
# perfectly healthy jobs, which is how a check teaches people to ignore it.

print("2  the tables carry no keys nothing asks for")
# A leftover entry is dead weight and, worse, reads as coverage.
scheduled = set().union(*asked.values()) if asked else set()
for table_name, table in (("WEEKLY_SCHEDULE", cs.WEEKLY_SCHEDULE),
                          ("DAILY_KL_SCHEDULE", cs.DAILY_KL_SCHEDULE)):
    for key in sorted(table):
        check(f"{table_name}[{key!r}] is used by main.py", key in scheduled,
              "" if key in scheduled else "orphan — nothing schedules it")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
