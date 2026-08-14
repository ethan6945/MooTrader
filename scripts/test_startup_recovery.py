"""What a restart has to settle before it is allowed to decide anything.

Run from repo root: .venv/bin/python scripts/test_startup_recovery.py
Fake broker throughout. Contacts no OpenD, places nothing.

THE SITUATION
  A worker that restarts inherits an account that kept going without it. Orders
  it left working may have filled, been cancelled, or still be sitting there.

  The first thing run_loop does is place protective exits — a decision about a
  position — and it makes that decision from the local record, which until
  every unfinished order is settled describes the account as it was when this
  software last saw it. A stop placed on that basis can cover shares that were
  sold while we were down, or miss shares that were bought.

  The tempting shortcut is to compare broker positions against ours and correct
  the difference. That answers "what do we hold" and not "what did each order
  do", and the entry price, PnL, R-multiple and holding period all come from
  the second question.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-recover-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

import pandas as pd                                          # noqa: E402
from moomoo import RET_OK, TrdSide                           # noqa: E402
from src import broker_binding as bb, db, identity, order_gate, order_log  # noqa: E402
from src import moo_client, risk_manager, startup_recovery   # noqa: E402

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


db._ensure_initialised()
identity._session = None
identity.reset_cache()
identity.start_session("SIMULATE")
os.environ["MMT_START_FENCE"] = "1"
(Path(_TMP) / "logs" / "worker.lease").write_text(
    json.dumps({"pid": os.getpid(), "fence": 1, "host": "t"}))
order_gate.permit("test")


class FakeTrade:
    def __init__(self):
        self.rows = []
        self.placed = []
        self.positions = {}
    def get_acc_list(self):
        return (RET_OK, [{"acc_id": 1, "trd_env": "SIMULATE",
                          "security_firm": "N/A", "acc_status": "ACTIVE",
                          "acc_type": "CASH"}])
    def place_order(self, **kw):
        self.placed.append(kw)
        oid = f"BRK{len(self.placed)}"
        self.rows.append({"order_id": oid, "remark": kw.get("remark", ""),
                          "code": kw["code"], "qty": kw["qty"],
                          "dealt_qty": 0, "dealt_avg_price": 0,
                          "order_status": "SUBMITTED"})
        return (RET_OK, pd.DataFrame([{"order_id": oid}]))
    def order_list_query(self, **kw):
        return (RET_OK, pd.DataFrame(self.rows))
    def history_order_list_query(self, **kw):
        return (RET_OK, pd.DataFrame(self.rows))
    def close(self):
        pass


class FakeClient(moo_client.MooClient):
    def __init__(self):
        super().__init__()
        bb.reset()
        self._trade = FakeTrade()
        bb.bind(bb.resolve(self._trade, trade_env="SIMULATE"))
    @property
    def quote(self):
        raise RuntimeError("no quote context in this test")
    def get_positions(self):
        pos = self._trade.positions
        if not pos:
            return pd.DataFrame()
        return pd.DataFrame([{"code": f"US.{s}", "qty": float(q)}
                             for s, q in pos.items()])


def age_order(coid, minutes):
    """Backdate an order so the settle grace period has passed."""
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with db.transaction() as c:
        c.execute("UPDATE orders SET created_at = ? WHERE client_order_id = ?",
                  (when, coid))


def clear_halt():
    db.update_state({"halted": False, "halt_reason": None, "halt_detail": None})


def drop_all_orders():
    with db.transaction() as c:
        c.execute("DELETE FROM orders")


# ── 1. nothing outstanding is a fast no-op ─────────────────────────────────
print("1  a clean restart settles nothing and halts nothing")
clear_halt(); drop_all_orders()
c = FakeClient()
r = startup_recovery.recover(c)
check("nothing was checked", r["checked"] == 0)
check("no halt", not db.get_state().get("halted"))


# ── 2. an order that filled while we were down ─────────────────────────────
print("\n2  an order that filled during the downtime is settled from the broker")
clear_halt(); drop_all_orders()
c = FakeClient()
p = c.place_limit_order("AAPL", 100, 190.0, TrdSide.BUY)
# The process died here. The broker went on and filled it.
c._trade.rows[-1].update({"order_status": "FILLED_ALL", "dealt_qty": 100,
                          "dealt_avg_price": 189.80})
age_order(p.client_order_id, 30)

r = startup_recovery.recover(c)
row = order_log.get(p.client_order_id)
check("the order is settled", row["state"] == "FILLED")
check("...at the quantity the broker filled", row["filled_qty"] == 100)
check("...and the price it filled at", abs(row["avg_fill_price"] - 189.80) < 1e-9)
check("it is no longer live", not order_log.live_orders())
check("no halt — everything is accounted for", not db.get_state().get("halted"))
check("the summary counts it", r["resolved"] >= 1)


# ── 3. an order that never reached the order book ──────────────────────────
print("\n3  an order the broker never saw is concluded, not left forever")
clear_halt(); drop_all_orders()
c = FakeClient()
coid = order_log.begin(symbol="GHOST", side="BUY", kind="ENTRY",
                       requested_qty=10, limit_price=1.0)
# PENDING_SUBMIT: written, then the process stopped before the call went out.
age_order(coid, 30)
r = startup_recovery.recover(c)
check("it is concluded as never submitted",
      order_log.get(coid)["state"] == "REJECTED")
check("...with a reason that says so",
      "never submitted" in (order_log.get(coid)["last_error"] or ""))
check("it is counted as never landed", r["never_landed"] == 1)
check("no halt — absence from the broker's own list IS an answer",
      not db.get_state().get("halted"))
check("nothing is left live", not order_log.live_orders())

# An UNKNOWN absent from a week of broker history reaches the same conclusion.
clear_halt(); drop_all_orders()
coid = order_log.begin(symbol="LOST", side="BUY", kind="ENTRY",
                       requested_qty=5, limit_price=1.0)
order_log.unknown(coid, "connection dropped")
age_order(coid, 30)
startup_recovery.recover(FakeClient())
check("an UNKNOWN the broker does not have never landed",
      order_log.get(coid)["state"] == "REJECTED")


# ── 4. too soon to conclude anything ───────────────────────────────────────
print("\n4  a just-placed order is not judged by its absence")
clear_halt(); drop_all_orders()
c = FakeClient()
coid = order_log.begin(symbol="FRESH", side="BUY", kind="ENTRY",
                       requested_qty=5, limit_price=1.0)
# No backdating: seconds old. The order book and the query endpoint do not
# update in the same instant, so absence here means nothing yet.
r = startup_recovery.recover(c, halt_on_unresolved=False)
check("it is NOT written off", order_log.get(coid)["state"] == "PENDING_SUBMIT")
check("it stays live for the next sweep",
      any(o["client_order_id"] == coid for o in order_log.live_orders()))


# ── 5. an order the broker accepted and then lost sight of ─────────────────
print("\n5  an order that cannot be accounted for stops trading")
clear_halt(); drop_all_orders()
c = FakeClient()
p = c.place_limit_order("MSFT", 50, 400.0, TrdSide.BUY)
# The broker gave us an id — so it existed — and now does not return it.
c._trade.rows.clear()
age_order(p.client_order_id, 30)

r = startup_recovery.recover(c)
check("it is reported unresolved", r["unresolved"] == [p.client_order_id])
# It is NOT written off. The broker acknowledged this order; not finding it now
# is a gap in what we can see, and deciding it never existed would be inventing
# an answer about a claim that may still fire.
check("it is not concluded to have never landed",
      order_log.get(p.client_order_id)["state"] != "REJECTED")
check("trading is halted", db.get_state().get("halted") is True)
check("...with a reason naming the cause",
      db.get_state().get("halt_reason") == "unresolved orders after restart")
check("...and the order id needed to chase it",
      p.client_order_id in (db.get_state().get("halt_detail") or ""))


# ── 6. positions are compared, not corrected ───────────────────────────────
print("\n6  a position disagreement is reported, never silently adopted")
clear_halt(); drop_all_orders()
for s in list(db.load_open_trades()):
    db.delete_open_trade(s)
c = FakeClient()
c._trade.positions = {"NVDA": 25}          # the broker holds something we do not
r = startup_recovery.reconcile_positions_from_orders(c)
check("the comparison ran", r["ok"] is True)
check("the disagreement is reported", len(r["differences"]) == 1)
check("...with both sides", r["differences"][0]["broker"] == 25
      and r["differences"][0]["ours"] == 0)
# Adopting it would put a position with invented entry, stop and R-multiple
# into the risk calculation — the 2026-07 "orphan adopted with fabricated
# levels" failure.
check("nothing was adopted", db.load_open_trades() == {})
# Halting, not warning. Every order has been settled by this point, so a
# remaining disagreement means shares moved that this software never asked for
# — and sizing the next position against that is how the difference gets
# adopted or overwritten by whatever touches it next.
check("trading is halted", db.get_state().get("halted") is True)
check("...with a reason a person can act on",
      db.get_state().get("halt_reason") == "position reconciliation failed")
check("...naming both sides",
      "NVDA" in (db.get_state().get("halt_detail") or ""))
check("...and it needs a person to clear",
      risk_manager.halt_status()["needs_manual_release"] is True)


# ── 7. an incomplete answer settles nothing ────────────────────────────────
#
# history_orders() used to swallow both query failures and return an empty
# frame, which is indistinguishable from "the broker has no such order". The
# recovery then wrote UNKNOWN orders off as never having landed, and the next
# cycle placed them again:
#
#     broker accepts the order -> query fails -> software sees an empty list
#     -> declares the order non-existent -> re-places it -> duplicate fill
#
# A failed question is not an empty answer.
print("\n7  a broker query that failed is not evidence of anything")


class BrokenQueryTrade(FakeTrade):
    """Both order queries fail, exactly as a dropped gateway would."""
    def order_list_query(self, **kw):
        raise ConnectionError("gateway unreachable")
    def history_order_list_query(self, **kw):
        raise ConnectionError("gateway unreachable")


clear_halt(); drop_all_orders()
c = FakeClient()
p = c.place_limit_order("KO", 40, 60.0, TrdSide.BUY)
order_log.unknown(p.client_order_id, "connection dropped after submit")
age_order(p.client_order_id, 45)          # well past the settle grace
c._trade.__class__ = BrokenQueryTrade     # now the queries fail

r = startup_recovery.recover(c)
row = order_log.get(p.client_order_id)
check("the answer is reported incomplete", r["complete"] is False)
check("the order is NOT written off", row["state"] == "UNKNOWN")
check("...and is still live", any(o["client_order_id"] == p.client_order_id
                                  for o in order_log.live_orders()))
check("trading is halted instead", db.get_state().get("halted") is True)
check("...with a reason naming the query, not the order",
      db.get_state().get("halt_reason") == "broker order query incomplete")

# And the frame itself carries the completeness, so nothing downstream has to
# infer it from emptiness.
df = c.history_orders("2026-08-01", "2026-08-14")
check("the frame says it is incomplete", df.attrs.get("complete") is False)
check("...and why", bool(df.attrs.get("failures")))


# ── 8. a safety halt does not expire overnight ─────────────────────────────
#
# reset_for_new_day() wrote halted=False unconditionally. That is right for a
# daily-drawdown halt — a new day genuinely answers it — and wrong for a stop
# order at the broker that could not be cancelled, which is not less true
# tomorrow. Those were being lifted overnight.
print("\n8  a halt that needs a person is not cleared by the calendar")
from src import risk_manager                                  # noqa: E402

for reason, survives in (("protective order cancel failed", True),
                         ("unresolved orders after restart", True),
                         ("broker order query incomplete", True),
                         ("daily drawdown", False)):
    clear_halt()
    db.update_state({"day": "1999-01-01"})
    risk_manager.halt(reason, "detail")
    risk_manager.reset_for_new_day(10000.0)
    still = bool(db.get_state().get("halted"))
    check(f"{reason!r} {'survives' if survives else 'is cleared by'} the rollover",
          still is survives)

# The seven-day auto-release re-anchors peak equity, which answers a DRAWDOWN.
# Applied to an order halt it would resume trading a week after a discrepancy
# nobody looked at.
clear_halt()
risk_manager.halt("protective order cancel failed", "a live stop at the broker")
old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
db.update_state({"halt_started_at": old})
risk_manager.check_drawdown_halt(db.get_state()) \
    if hasattr(risk_manager, "check_drawdown_halt") else None
check("the 7-day auto-release does not touch an order halt",
      db.get_state().get("halt_started_at") == old)

# It is cleared by a person, and who did it is recorded.
res = risk_manager.release_halt("ethan", "cancelled the stray stop by hand")
check("a person can release it", res["released"] is True)
check("...and trading resumes", not db.get_state().get("halted"))
check("...and the release is audited",
      any(a.get("action") == "halt_released" for a in db.audit_recent(limit=20)))

status = risk_manager.halt_status()
check("halt_status reports a clean state", status["halted"] is False)
clear_halt()
risk_manager.halt("daily drawdown", "6%")
check("a drawdown halt is not flagged as needing a person",
      risk_manager.halt_status()["needs_manual_release"] is False)
clear_halt()
risk_manager.halt("protective order cancel failed", "x")
check("an order halt is", 
      risk_manager.halt_status()["needs_manual_release"] is True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
