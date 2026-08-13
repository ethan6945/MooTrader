"""The order log: what is written, when, and what a failure is allowed to mean.

Run from repo root: .venv/bin/python scripts/test_order_log.py
Uses a fake trade context. Contacts no OpenD, places nothing.

WHY THIS EXISTS
  Until schema v6 there was no record that an order had been asked for. A
  position row was written the instant place_order() returned — at the
  REQUESTED quantity and the LIMIT price — and that was the only trace.

  The case that record cannot express is the one that matters: a call that
  neither succeeds nor fails, because the connection dropped after the broker
  accepted the order and before the answer came back. Without a prior record
  that looks exactly like an order that was never sent, and the retry buys
  twice. So the row is written and flushed BEFORE the broker is called, and a
  failure that proves nothing is recorded as UNKNOWN rather than as "no".
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-orderlog-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

import pandas as pd                                    # noqa: E402
from moomoo import RET_OK, RET_ERROR, TrdSide, OrderType   # noqa: E402
from src import broker_binding as bb, db, identity, order_gate, order_log  # noqa: E402
from src import moo_client, start_lease                # noqa: E402

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


# ── a world in which an order may be placed ────────────────────────────────
db._ensure_initialised()
identity._session = None
identity.reset_cache()
SESSION = identity.start_session("SIMULATE")
os.environ["MMT_START_FENCE"] = "1"
import json                                            # noqa: E402
(Path(_TMP) / "logs" / "worker.lease").write_text(
    json.dumps({"pid": os.getpid(), "fence": 1, "host": "t"}))
order_gate.permit("test")


class FakeTrade:
    """Records what it was asked to place, and answers however the test says."""
    def __init__(self, behaviour="ok"):
        self.behaviour = behaviour
        self.calls = []
        self.rows = []
    def get_acc_list(self):
        return (RET_OK, [{"acc_id": 1, "trd_env": "SIMULATE",
                          "security_firm": "N/A", "acc_status": "ACTIVE",
                          "acc_type": "CASH"}])
    def place_order(self, **kw):
        self.calls.append(kw)
        if self.behaviour == "raise":
            raise ConnectionError("connection reset by peer")
        if self.behaviour == "reject":
            return (RET_ERROR, "Insufficient buying power")
        if self.behaviour == "timeout_ret":
            return (RET_ERROR, "request timed out waiting for gateway")
        if self.behaviour == "zero_id":
            return (RET_OK, pd.DataFrame([{"order_id": "0"}]))
        self.rows.append({"order_id": f"BRK{len(self.calls)}",
                          "remark": kw.get("remark", ""),
                          "code": kw["code"], "qty": kw["qty"],
                          "dealt_qty": 0, "dealt_avg_price": 0,
                          "order_status": "SUBMITTED"})
        return (RET_OK, pd.DataFrame([{"order_id": f"BRK{len(self.calls)}"}]))
    def order_list_query(self, **kw):
        return (RET_OK, pd.DataFrame(self.rows))
    def history_order_list_query(self, **kw):
        return (RET_OK, pd.DataFrame(self.rows))
    def close(self):
        pass


def fresh_client(behaviour="ok"):
    bb.reset()
    c = moo_client.MooClient()
    ft = FakeTrade(behaviour)
    c._trade = ft
    bb.bind(bb.resolve(ft, trade_env="SIMULATE"))
    return c, ft


# ── 1. the row exists before the order can ─────────────────────────────────
print("1  the record is written before the broker is called")


class ObservingTrade(FakeTrade):
    """Looks at the database from inside place_order, which is the only moment
    that can distinguish "written first" from "written after"."""
    def __init__(self):
        super().__init__("ok")
        self.seen_state = None
        self.seen_remark = None
    def place_order(self, **kw):
        row = order_log.get(kw.get("remark", ""))
        self.seen_state = row["state"] if row else None
        self.seen_remark = kw.get("remark")
        return super().place_order(**kw)


bb.reset()
c = moo_client.MooClient()
obs = ObservingTrade()
c._trade = obs
bb.bind(bb.resolve(obs, trade_env="SIMULATE"))
placed = c.place_limit_order("AAPL", 10, 190.0, TrdSide.BUY)
check("the order was already in the database when the broker was called",
      obs.seen_state == "PENDING_SUBMIT")
check("the broker was given our client_order_id as the remark",
      bool(obs.seen_remark) and obs.seen_remark.startswith("mmt"))
row = order_log.get(obs.seen_remark)
check("and it is SUBMITTED once the broker answered", row["state"] == "SUBMITTED")
check("with the broker's id recorded",
      row["broker_order_id"] == placed.broker_order_id)
# Placement carries BOTH ids and is deliberately not str-able: a call site
# that had not been updated would otherwise keep "working" while dropping
# the client id, which is the half that survives a lost answer.
check("...and the placement carries our id too",
      placed.client_order_id == obs.seen_remark)
check("the requested quantity is recorded", row["requested_qty"] == 10)
check("nothing is filled yet", row["filled_qty"] == 0)
check("it carries the session", row["session_id"] == SESSION["session_id"])


# ── 2. what each kind of failure is allowed to mean ────────────────────────
print("\n2  a failure that proves nothing is recorded as UNKNOWN")

c, ft = fresh_client("raise")
before = len(order_log.recent(limit=500))
try:
    c.place_limit_order("MSFT", 5, 400.0, TrdSide.BUY)
    check("a dropped connection raises to the caller", False)
except RuntimeError:
    check("a dropped connection raises to the caller", True)
latest = order_log.recent(limit=1)[0]
check("...and the order is UNKNOWN, not gone", latest["state"] == "UNKNOWN")
check("...for the symbol that was attempted", latest["symbol"] == "MSFT")

# A rejection is EVIDENCE. Recording it as UNKNOWN would send the recovery
# sweep chasing an order that provably never reached the book.
c, ft = fresh_client("reject")
try:
    c.place_limit_order("NVDA", 5, 100.0, TrdSide.BUY)
except RuntimeError:
    pass
latest = order_log.recent(limit=1)[0]
check("an explicit broker rejection is REJECTED, not UNKNOWN",
      latest["state"] == "REJECTED")
check("...and the reason is kept", "buying power" in (latest["last_error"] or ""))

# A timeout reported through the return value is still a timeout.
c, ft = fresh_client("timeout_ret")
try:
    c.place_limit_order("TSLA", 5, 100.0, TrdSide.BUY)
except RuntimeError:
    pass
latest = order_log.recent(limit=1)[0]
check("a timeout in the return value is UNKNOWN, not a rejection",
      latest["state"] == "UNKNOWN")

# order_id=0 is the gateway answering with something meaningless. It used to be
# treated as a clean failure, which is a guess about the order book.
c, ft = fresh_client("zero_id")
try:
    c.place_limit_order("AMD", 5, 100.0, TrdSide.BUY)
except RuntimeError:
    pass
latest = order_log.recent(limit=1)[0]
check("an order_id of 0 is UNKNOWN rather than assumed failed",
      latest["state"] == "UNKNOWN")


# ── 3. resolving an UNKNOWN by asking, not by reasoning ────────────────────
print("\n3  an UNKNOWN is settled only by the broker")

c, ft = fresh_client("ok")
placed = c.place_limit_order("HPE", 20, 21.5, TrdSide.BUY)
coid = placed.client_order_id
# Pretend we never saw the answer.
order_log.unknown(coid, "connection dropped after submit")
check("the order is UNKNOWN", order_log.get(coid)["state"] == "UNKNOWN")
check("it is listed as live", any(o["client_order_id"] == coid
                                 for o in order_log.live_orders()))

# The broker had it all along, and it filled.
ft.rows[-1].update({"order_status": "FILLED_ALL", "dealt_qty": 20,
                    "dealt_avg_price": 21.48})
summary = order_log.reconcile_live(c)
row = order_log.get(coid)
# The sweep covers every live order, not just this one — earlier sections left
# their own UNKNOWNs behind, and a sweep that skipped them would be the bug.
check("the sweep resolved at least this order", summary["resolved"] >= 1)
check("...and reports the ones it could not find",
      summary["still_unknown"] == len(summary["orders"]))
check("...as FILLED", row["state"] == "FILLED")
check("...at the quantity the BROKER reports", row["filled_qty"] == 20)
check("...and the broker's average price", abs(row["avg_fill_price"] - 21.48) < 1e-9)
check("it is no longer live", not any(o["client_order_id"] == coid
                                      for o in order_log.live_orders()))

# Matching is by remark, not by shape. This is the case a heuristic gets wrong:
# a retry sitting beside the original, identical in symbol, side and quantity.
c, ft = fresh_client("ok")
c.place_limit_order("DELL", 7, 100.0, TrdSide.BUY)
first = ft.calls[-1]["remark"]
c.place_limit_order("DELL", 7, 100.0, TrdSide.BUY)
second = ft.calls[-1]["remark"]
check("two identical orders get different ids", first != second)
ft.rows[0].update({"order_status": "FILLED_ALL", "dealt_qty": 7,
                   "dealt_avg_price": 99.9})
ft.rows[1].update({"order_status": "CANCELLED_ALL", "dealt_qty": 0})
order_log.reconcile_live(c)
check("the filled one is the one that filled",
      order_log.get(first)["state"] == "FILLED")
check("the cancelled one is the one that was cancelled",
      order_log.get(second)["state"] == "CANCELLED")


# ── 4. fills only ever grow ────────────────────────────────────────────────
print("\n4  a fill quantity never goes backwards")
c, ft = fresh_client("ok")
c.place_limit_order("IBM", 100, 200.0, TrdSide.BUY)
coid = ft.calls[-1]["remark"]
order_log.record_fill(coid, filled_qty=60, avg_price=199.5, state="PARTIAL")
check("a partial fill is recorded", order_log.get(coid)["filled_qty"] == 60)
# A stale poll reporting less is a stale view, not an un-fill. Taking the lower
# number would shrink a position we hold, and the correction reads as a sale.
order_log.record_fill(coid, filled_qty=20, avg_price=199.5, state="PARTIAL")
check("a lower figure from a later poll is ignored",
      order_log.get(coid)["filled_qty"] == 60)
order_log.record_fill(coid, filled_qty=100, avg_price=199.6, state="FILLED")
check("a higher figure is taken", order_log.get(coid)["filled_qty"] == 100)
check("the terminal state is recorded", order_log.get(coid)["state"] == "FILLED")
check("and it is stamped resolved", bool(order_log.get(coid)["resolved_at"]))


# ── 5. unknown broker statuses stay live ───────────────────────────────────
print("\n5  an unrecognised status is polled again, not written off")
check("a status this code has never seen maps to a live state",
      order_log.map_broker_status("SOME_NEW_STATUS") in order_log.LIVE_STATES)
check("FILLED_ALL maps to FILLED",
      order_log.map_broker_status("FILLED_ALL") == "FILLED")
check("FILLED_PART maps to PARTIAL",
      order_log.map_broker_status("FILLED_PART") == "PARTIAL")
check("an empty status does not settle an order",
      order_log.map_broker_status(None) in order_log.LIVE_STATES)


# ── 6. orders belong to an account, like everything else ───────────────────
print("\n6  the log is account-scoped")
sim_orders = len(order_log.recent(limit=500))
check("this account has orders", sim_orders > 0)
from src.config import settings                        # noqa: E402
identity._session = None
identity.reset_cache()
object.__setattr__(settings, "moo_trade_env", "REAL")
check("a live account sees none of the paper account's orders",
      order_log.recent(limit=500) == [])
check("...and none of them as live orders", order_log.live_orders() == [])
object.__setattr__(settings, "moo_trade_env", "SIMULATE")
identity.reset_cache()


# ── 7. an order cannot be recorded without a run to attribute it to ────────
print("\n7  no session, no order")
identity._session = None
try:
    order_log.begin(symbol="X", side="BUY", kind="ENTRY", requested_qty=1)
    check("recording an order outside a session refuses", False)
except RuntimeError:
    # A fill that cannot be attributed to a run is the thing that makes a
    # duplicate order impossible to investigate afterwards.
    check("recording an order outside a session refuses", True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
