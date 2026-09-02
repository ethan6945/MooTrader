"""A bot sale is not a manual buy — the in-flight-close grace, armed from the settler.

Run from repo root: .venv/bin/python scripts/test_close_tombstone.py
No OpenD, no orders placed. Drives fill_settler and reconcile directly.

WHAT WAS WRONG (2026-08-15 → 2026-09-02)

  Reconcile compares the broker's positions against ours. A sale that has
  filled but whose position feed has not caught up therefore reads as "broker
  has it, we don't" — an ORPHAN — and orphans are adopted as positions the
  OWNER bought by hand: user_managed, pending_review, and a 检测到手动持仓
  Telegram plus a takeover-approval card for a trade the bot had just exited.

  That was already known (2026-07-16, AMAT: stop sell placed, adopted 2s
  later), and the defence was RECENT_CLOSE_GRACE_S — a tombstone written by
  executor._close_and_log on every close, which the orphan and mismatch scans
  both check before classifying.

  Then every exit moved through fill_settler (12f7056, "one settler for every
  path"). _close_and_log stopped being the close path, and the settler wrote no
  tombstone — so nothing armed the grace. The evidence sat in the ledger:
  `recent_closes` held ONE entry, MRK 2026-08-28, and only because reconcile's
  ghost branch still books through the executor. DELL 08-31, HPE 09-01 and
  FTNT 09-01 all closed and left nothing. The map is pruned only inside the
  writer, which is why a five-day-old entry was still sitting in it.

  Both graces died together:
    * full close  → ORPHAN  → re-adopted as a manual buy (the alert).
    * scale-out   → MISMATCH → "adopt the broker's qty" re-inflates the record,
                    and the next stop sells shares that are already gone.

  The settler is the one writer for closes, so the tombstone belongs there —
  including for the late fills the sweep settles, which have the widest lag and
  never touch the executor at all.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-tomb-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

import pandas as pd                                          # noqa: E402
from src import db, identity, order_gate, order_log          # noqa: E402
from src import executor, fill_settler, reconcile            # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond:
        PASS += 1
    else:
        FAIL += 1


db._ensure_initialised()
identity._session = None
identity.reset_cache()
identity.start_session("SIMULATE")
os.environ["MMT_START_FENCE"] = "1"
(Path(_TMP) / "logs" / "worker.lease").write_text(
    json.dumps({"pid": os.getpid(), "fence": 1, "host": "t"}))
order_gate.permit("test")


def hold(symbol, qty, entry=100.0):
    """A position on our books, shaped the way the executor writes one."""
    db.upsert_open_trade({
        "symbol": symbol, "qty": qty, "entry_price": entry,
        "stop_loss": entry - 4, "take_profit": entry + 10, "atr": 2.0,
        "half_closed": False, "buy_order_id": "b-" + symbol,
        "stop_order_id": None, "tp_order_id": None,
        "opened_at": "2026-09-01T13:00:00", "high_water": entry,
        "low_water": entry, "ml_proba_entry": None, "strategy": "trend",
        "qty_initial": qty, "init_risk_per_share": 4.0,
    })


def sell_through_settler(symbol, qty, price, kind="EXIT", intent="SL"):
    """The bot's own sale, settled the way every exit path settles it."""
    coid = order_log.begin(symbol=symbol, side="SELL", kind=kind,
                           requested_qty=qty, limit_price=price, intent=intent)
    order_log.submitted(coid, "9" + symbol)
    order_log.record_fill(coid, filled_qty=qty, avg_price=price, state="FILLED")
    return fill_settler.settle(coid)


def broker_still_shows(symbol, qty, cost=100.0):
    """The position feed lagging the fill — the window the grace covers."""
    return pd.DataFrame([{"code": f"US.{symbol}", "qty": qty,
                          "cost_price": cost}])


def tombstones():
    return db.get_state().get("recent_closes") or {}


# ── 1. a full close, settled, while the broker still shows the shares ──────
print("\n1  the bot's own completed sale is not adopted as a manual buy")

hold("FTNT", 5)
out = sell_through_settler("FTNT", 5, 96.0)
check("the settler booked the close", out["applied"] == 5)
check("the position is off our books", "FTNT" not in db.load_open_trades())
check("the close is tombstoned by the settler itself", "FTNT" in tombstones())

res = reconcile.reconcile(broker_still_shows("FTNT", 5), auto_fix=True,
                          client=None)
check("reconcile does not call it an orphan",
      [o["symbol"] for o in res["orphans"]] == [])
check("...and adopts nothing",
      [f for f in res["fixes_applied"] if f["type"] == "ORPHAN_ADOPTED"] == [])
check("no manual-adoption record was written",
      db.load_open_trades().get("FTNT") is None)


# ── 2. a scale-out tranche, where the re-inflated qty would oversell ───────
print("\n2  a partial sale is not re-inflated to the broker's stale qty")

hold("BAC", 15)
out = sell_through_settler("BAC", 5, 105.0, kind="TP", intent="TP1")
check("five sold, ten held", out["position_qty"] == 10)
check("the partial is tombstoned too", "BAC" in tombstones())

res = reconcile.reconcile(broker_still_shows("BAC", 15), auto_fix=True,
                          client=None)
check("reconcile does not call it a qty mismatch", res["mismatches"] == [])
check("...and leaves the position at what is actually held",
      int(db.load_open_trades()["BAC"]["qty"]) == 10)


# ── 3. the grace is a window, not an amnesty ───────────────────────────────
print("\n3  a holding that outlives the window is still adopted")

hold("GE", 4)
sell_through_settler("GE", 4, 90.0)
stale = {"GE": "2026-01-01T00:00:00"}          # older than RECENT_CLOSE_GRACE_S
db.update_state({"recent_closes": stale})
res = reconcile.reconcile(broker_still_shows("GE", 4), auto_fix=True,
                          client=None)
check("an aged tombstone does not suppress a real orphan",
      [o["symbol"] for o in res["orphans"]] == ["GE"])
check("...and it is adopted for review",
      bool(db.load_open_trades().get("GE", {}).get("manual_adopted")))


# ── 4. the executor's own path still arms it (reconcile ghost + legacy) ────
print("\n4  the executor's close path keeps arming the grace")

db.update_state({"recent_closes": {}})
trade = {"symbol": "HPE", "qty": 7, "entry_price": 100.0, "stop_loss": 96.0,
         "take_profit": 110.0, "opened_at": "2026-09-01T13:00:00",
         "high_water": 100.0, "low_water": 100.0, "strategy": "trend",
         "init_risk_per_share": 4.0}
hold("HPE", 7)
executor._close_and_log("HPE", trade, 7, 97.0, "SL")
check("_close_and_log tombstones as it always did", "HPE" in tombstones())


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
