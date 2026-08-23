"""The auto-rollback: does it read the log that is actually being written?

WHY THIS EXISTS

  The parameter journal moved from a db-state list to an append-only file.
  Two readers did not move with it — runtime_config.revert_param and
  autopilot.check_and_rollback — so for a week the rollback scanned a log
  frozen at 2026-08-17 and found nothing to do.

  That failure is invisible by construction. An empty scan and a healthy
  account produce the same output: no action, no message. Nothing errored,
  nothing looked wrong, and the safety net for a bad parameter change was
  simply off. The only way to know is to assert that a change written the
  normal way is a change the rollback can see.

  Run from repo root: .venv/bin/python scripts/test_param_rollback.py
  No broker, no network.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="mmt-rollback-")
os.environ["MMT_HOME"] = _TMP
os.environ["PARAMS_FROZEN"] = "false"   # the rollback is a write; it needs the gate open
for _d in ("data", "logs", "config"):
    (Path(_TMP) / _d).mkdir(parents=True, exist_ok=True)

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


from src import autopilot, db, runtime_config as rc          # noqa: E402

print("1  the journal is the log the writer writes")
rc.set_param("entry_threshold", 72.0, source="owner-approved")
check("a change lands in the journal file",
      rc.history_file().exists() and rc.history_file().stat().st_size > 0)
last = rc.last_change("entry_threshold")
check("last_change sees it", last is not None and last["new"] == 72.0,
      str(last))
check("the value itself moved", rc.current("entry_threshold") == 72.0,
      str(rc.current("entry_threshold")))
check("the dead db list is NOT what carried it",
      not db.get_state().get("param_history"),
      str(db.get_state().get("param_history")))

print("\n2  revert restores the previous value, through the same writer")
old_before = last["old"]
rec = rc.revert_param("entry_threshold", "test rollback")
check("revert returns the record it reverted", rec is not None)
check("the value went back", rc.current("entry_threshold") == float(old_before),
      f"{rc.current('entry_threshold')} vs {old_before}")
check("the rollback is itself journalled",
      str(rc.last_change("entry_threshold")["source"]).startswith("rollback:"),
      str(rc.last_change("entry_threshold")["source"]))

print("\n3  a rollback is not itself rolled back")
check("reverting again is a no-op",
      rc.revert_param("entry_threshold", "second attempt") is None)

print("\n4  check_and_rollback acts on an OWNER-APPROVED change")
# The scope that mattered: every change is owner-approved now that autopilot,
# optimizer_ai and hermes all queue instead of writing. A rollback that skips
# owner-approved changes skips everything.
rc.set_param("entry_threshold", 60.0, source="owner-approved")
changed_at = datetime.now(timezone.utc)

# Five closed trades AFTER the change, all losing, against a profitable window
# before it — the deterioration the rollback exists to catch.
def _t(offset_min: int, pnl: float) -> dict:
    return {"symbol": "TEST", "pnl": pnl,
            "ts": (changed_at + timedelta(minutes=offset_min)).isoformat()}

rows = ([_t(-600, +50.0), _t(-500, +40.0), _t(-400, +30.0)]
        + [_t(10, -60.0), _t(20, -70.0), _t(30, -50.0), _t(40, -80.0), _t(50, -40.0)])
_real_closed = db.closed_trades
db.closed_trades = lambda limit=200: rows                      # noqa: E731
try:
    notes = autopilot.check_and_rollback()
finally:
    db.closed_trades = _real_closed

check("it rolled the owner-approved change back", bool(notes), str(notes))
check("the value is back to what preceded it",
      rc.current("entry_threshold") != 60.0, str(rc.current("entry_threshold")))
check("and it says so in a message a person will read",
      any("回滚" in n or "rollback" in n.lower() for n in notes), str(notes))

print("\n5  a healthy account is left alone")
rc.set_param("tp_atr_mult", 9.0, source="owner-approved")
good_at = datetime.now(timezone.utc)
good = [{"symbol": "TEST", "pnl": +40.0,
         "ts": (good_at + timedelta(minutes=i * 10)).isoformat()} for i in range(6)]
db.closed_trades = lambda limit=200: good                      # noqa: E731
try:
    notes2 = autopilot.check_and_rollback()
finally:
    db.closed_trades = _real_closed
check("no rollback when results improved", not notes2, str(notes2))
check("the value stayed put", rc.current("tp_atr_mult") == 9.0,
      str(rc.current("tp_atr_mult")))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
