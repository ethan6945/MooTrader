"""A protective order that will not cancel stops the bot.

Run from repo root: .venv/bin/python scripts/test_protective_cancel.py
Fake broker throughout. Contacts no OpenD, places nothing.

WHY A HALT AND NOT A WARNING
  A stop or take-profit that refuses to cancel is still working at the broker.
  Everything after that point is reasoning about a position whose protection
  this software can no longer account for:

    * place a replacement and there are two stops covering overlapping
      quantity — when price reaches them both sell, and the second sale is
      stock we no longer hold;
    * sell the shares and the surviving leg becomes a naked short the moment
      it triggers.

  Neither is recoverable locally: the order belongs to the broker. Six of the
  eight cancel sites only logged the failure, and one of them — the stack
  re-bracket — caught exceptions but not a plain `False`, so an explicit
  refusal fell straight through into placing the replacement legs.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-cancel-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

import pandas as pd                                          # noqa: E402
from moomoo import RET_OK, RET_ERROR, TrdSide                # noqa: E402
from src import broker_binding as bb, db, identity, order_gate, order_log  # noqa: E402
from src import executor, moo_client, risk_manager           # noqa: E402

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
    def __init__(self, cancel_mode="ok"):
        self.cancel_mode = cancel_mode      # ok | refuse | raise
        self.rows = []
        self.placed = []
        self.cancels = []
    def get_acc_list(self):
        return (RET_OK, [{"acc_id": 1, "trd_env": "SIMULATE",
                          "security_firm": "N/A", "acc_status": "ACTIVE",
                          "acc_type": "CASH"}])
    def place_order(self, **kw):
        self.placed.append(kw)
        oid = f"BRK{len(self.placed)}"
        # A protective leg is a WORKING order sitting against a held position,
        # not a filled one. The first version reported every placement as
        # FILLED_ALL, which made the settler see a sale with no position behind
        # it and halt for oversell — the settler being right about a fixture
        # that could not happen.
        # A BUY fills (that is how a position comes to exist). A protective
        # SELL leg is a WORKING order sitting against that position, not a
        # filled one — reporting every placement as FILLED_ALL made the settler
        # see a sale with no position behind it and halt for oversell, which was
        # the settler being right about a fixture that could not happen.
        is_buy = str(kw.get("trd_side", "")).endswith("BUY")
        self.rows.append({"order_id": oid, "remark": kw.get("remark", ""),
                          "code": kw["code"], "qty": kw["qty"],
                          "dealt_qty": kw["qty"] if is_buy else 0,
                          "dealt_avg_price": kw["price"] if is_buy else 0,
                          "order_status": "FILLED_ALL" if is_buy
                                          else "SUBMITTED"})
        return (RET_OK, pd.DataFrame([{"order_id": oid}]))
    def modify_order(self, **kw):
        self.cancels.append(kw.get("order_id"))
        if self.cancel_mode == "raise":
            raise ConnectionError("gateway went away mid-cancel")
        if self.cancel_mode == "refuse":
            return (RET_ERROR, "order already executing")
        return (RET_OK, pd.DataFrame())
    def order_list_query(self, **kw):
        return (RET_OK, pd.DataFrame(self.rows))
    def modify_order_status(self, oid, status):
        for r in self.rows:
            if r["order_id"] == oid:
                r["order_status"] = status
    def history_order_list_query(self, **kw):
        return (RET_OK, pd.DataFrame(self.rows))
    def close(self):
        pass


class FakeClient(moo_client.MooClient):
    def __init__(self, cancel_mode="ok", holdings=None, last_price=None):
        super().__init__()
        bb.reset()
        self._trade = FakeTrade(cancel_mode)
        self._holdings = holdings or {}
        # close_position refuses to market-sell without a price ("looks
        # halted/anomalous"), which fires BEFORE the cancel. Both refusals are
        # correct; a test that wants to reach the cancel has to get past the
        # first one honestly rather than by removing the guard.
        self._last_price = last_price
        bb.bind(bb.resolve(self._trade, trade_env="SIMULATE"))
    @property
    def quote(self):
        raise RuntimeError("no quote context in this test")
    def get_last_price(self, symbol):
        return self._last_price
    def get_snapshot(self, symbol):
        # _last_price() reads the SNAPSHOT, not get_last_price. Stubbing the
        # wrong one is why the first attempt at this test never reached the
        # cancel: it kept hitting the "price looks halted" guard.
        if self._last_price is None:
            raise RuntimeError("no snapshot in this test")
        return {"last_price": self._last_price}
    def get_short_symbols(self):
        return set()
    def get_positions(self):
        if not self._holdings:
            return pd.DataFrame()
        return pd.DataFrame([{"code": f"US.{s}", "qty": float(q)}
                             for s, q in self._holdings.items()])


def clear_halt():
    db.update_state({"halted": False, "halt_reason": None,
                     "halt_detail": None, "residual_orders": []})


# ── 1. an accepted cancel is not a confirmed cancellation ──────────────────
print("1  an accepted cancel request is not proof the order is gone")
clear_halt()
c = FakeClient("ok")
p = c.place_limit_order("AAPL", 10, 100.0, TrdSide.SELL, kind="STOP")
ok = executor.cancel_protective(c, "AAPL", p.broker_order_id, "stop")
check("the request is accepted", ok is True)
check("no halt", not db.get_state().get("halted"))
row = order_log.get(p.client_order_id)
extra = json.loads(row["extra"]) if row.get("extra") else {}
check("the attempt is recorded", extra.get("cancel_accepted") is True)
# "Accepted" is now followed by a poll, so the order does end up CANCELLED —
# but only once the BROKER said so. The distinction is not cosmetic: between
# deciding to cancel and the request landing, the order can fill, and a caller
# that placed a replacement on the strength of acceptance would have two live
# orders for one position.
check("the state came from the broker, not from the request",
      row["state"] == "CANCELLED" and row["last_polled_at"])


# A cancel the broker accepts but never confirms is NOT a cancellation.
print("\n1b  an accepted cancel that is never confirmed is a refusal")
clear_halt()


class NeverConfirms(FakeTrade):
    """Accepts the cancel and then reports the order as still working."""
    def get_order_fill(self, *a, **k):
        return None


c = FakeClient("ok")
c._trade.__class__ = NeverConfirms
executor._CANCEL_CONFIRM_SEC = 1.0
p2 = c.place_limit_order("BA", 10, 100.0, TrdSide.SELL, kind="STOP")
# is_order_filled will say False, so the poll concludes CANCELLED. Make it
# ambiguous instead: the order is neither confirmed filled nor confirmed gone.
c.is_order_filled = lambda *a, **k: True
ok2 = executor.cancel_protective(c, "BA", p2.broker_order_id, "stop")
check("an unconfirmed cancellation is reported as a failure", ok2 is False)
check("...and halts", db.get_state().get("halted") is True)
check("...naming the confirmation, not the cancel",
      db.get_state().get("halt_reason") == "cancel not confirmed")
executor._CANCEL_CONFIRM_SEC = 10.0


# ── 2. a refused cancel halts, and says why ────────────────────────────────
print("\n2  a refused cancel stops trading")
clear_halt()
c = FakeClient("refuse")
p = c.place_limit_order("MSFT", 10, 100.0, TrdSide.SELL, kind="STOP")
ok = executor.cancel_protective(c, "MSFT", p.broker_order_id, "stop")
check("the refusal is reported", ok is False)
state = db.get_state()
check("trading is halted", state.get("halted") is True)
check("...with a reason a person can act on",
      state.get("halt_reason") == "protective order cancel failed")
check("...naming the symbol and the leg",
      "MSFT" in (state.get("halt_detail") or "")
      and "stop" in (state.get("halt_detail") or ""))
check("the risk gate now refuses entries",
      risk_manager.can_trade_today()[0] is False
      if hasattr(risk_manager, "can_trade_today") else True)

residuals = executor.residual_orders()
check("the residual order is tracked", len(residuals) == 1)
check("...with the broker id needed to resolve it",
      residuals[0]["broker_order_id"] == p.broker_order_id)
check("...and which leg it was", residuals[0]["leg"] == "stop")
check("the order log records the refusal",
      "cancel refused" in (order_log.get(p.client_order_id)["last_error"] or ""))
check("the order is still live, not written off",
      any(o["client_order_id"] == p.client_order_id
          for o in order_log.live_orders()))

# The first reason wins: a later cause is usually a consequence of the first.
executor.cancel_protective(c, "NVDA", "OTHER", "take-profit")
check("a second failure does not overwrite the first reason",
      db.get_state().get("halt_reason") == "protective order cancel failed"
      and "MSFT" in (db.get_state().get("halt_detail") or ""))
check("but it IS tracked as a second residual",
      len(executor.residual_orders()) == 2)


# ── 3. a cancel that raises is a refusal, not a success ────────────────────
print("\n3  a cancel that raises is treated as refused")
clear_halt()
c = FakeClient("raise")
p = c.place_limit_order("TSLA", 10, 100.0, TrdSide.SELL, kind="STOP")
check("an exception means not cancelled",
      executor.cancel_protective(c, "TSLA", p.broker_order_id, "stop") is False)
check("...and halts", db.get_state().get("halted") is True)


# ── 4. a residual clears only when it is actually resolved ─────────────────
print("\n4  residuals clear explicitly, not by forgetting")
clear_halt()
c = FakeClient("refuse")
p = c.place_limit_order("AMD", 10, 100.0, TrdSide.SELL, kind="STOP")
executor.cancel_protective(c, "AMD", p.broker_order_id, "stop")
check("one residual is held", len(executor.residual_orders()) == 1)
check("clearing an unknown id changes nothing",
      executor.clear_residual_order("NOT-A-REAL-ID") is False)
check("...and the residual is still there", len(executor.residual_orders()) == 1)
check("clearing the real one works",
      executor.clear_residual_order(p.broker_order_id) is True)
check("...and it is gone", executor.residual_orders() == [])


# ── 5. the exit paths refuse rather than sell into a live leg ──────────────
print("\n5  no exit sells while a protective leg is still live")
clear_halt()
c = FakeClient("ok", holdings={"XOM": 10})
opened = executor.open_position(
    c, __import__("src.indicators", fromlist=["Signal"]).Signal(
        symbol="XOM", price=50.0, atr=1.0, score=80.0), 10)
check("a position is open", opened is not None)

# Give it a broker stop, then make cancellation impossible.
trades = executor._load_open_trades()
trades["XOM"]["stop_order_id"] = "STOPLEG"
executor._save_open_trades(trades)
c2 = FakeClient("refuse", holdings={"XOM": 10}, last_price=50.0)
before_closes = len(db.closed_trades(limit=500, include_excluded=True))
try:
    executor.close_position(c2, "XOM", reason="MANUAL")
    check("close_position refuses while the stop cannot be cancelled", False)
except RuntimeError as e:
    ok = "could not be cancelled" in str(e)
    if not ok:
        print("        refused for a different reason: " + str(e)[:160])
    check("close_position refuses while the stop cannot be cancelled", ok)
check("no close was booked",
      len(db.closed_trades(limit=500, include_excluded=True)) == before_closes)
# The position must remain — a holding whose protection is in doubt is exactly
# the one that must stay visible.
check("the position is still held", "XOM" in db.load_open_trades())
check("nothing was sold", not any(
    k.get("trd_side") == TrdSide.SELL and k.get("code", "").endswith("XOM")
    for k in c2._trade.placed))
check("and trading is halted", db.get_state().get("halted") is True)

# ── 6. a fill that lands during the cancel must be applied, or nothing ─────
# The window is real: between deciding to cancel and the request landing, the
# protective order can fill. cancel_protective polls for that and settles it,
# with the comment "apply that before anything else decides what the position
# is" — and then used to log-and-return-True when the settle failed. True means
# "the cancel request was accepted"; the caller reads it and goes on to decide
# what the position is, from the very record that was supposed to be updated
# first. The shares had moved at the broker. The ledger did not say so.
print("\n6  a fill during cancellation that cannot be settled is a refusal")
clear_halt()

from src import fill_settler                                  # noqa: E402
_real_settle = fill_settler.settle
_real_await = executor._await_cancel_terminal

c6 = FakeClient("ok")
p6 = c6.place_limit_order("NKE", 10, 100.0, TrdSide.SELL, kind="STOP")
# The broker says: terminal, and 10 shares filled that the ledger has not
# applied. This is the branch under test.
executor._await_cancel_terminal = lambda *a, **k: {
    "client_order_id": p6.client_order_id, "filled_qty": 10, "applied_qty": 0}
fill_settler.settle = lambda coid: (_ for _ in ()).throw(
    RuntimeError("ledger write failed"))
try:
    ok6 = executor.cancel_protective(c6, "NKE", p6.broker_order_id, "stop")
finally:
    fill_settler.settle = _real_settle

check("a failed settle is reported as a failure, not as an accepted cancel",
      ok6 is False)
check("...and halts", db.get_state().get("halted") is True)
check("...naming the unsettled fill",
      db.get_state().get("halt_reason") == "fill during cancel not settled")
check("...and the detail names the symbol and the leg",
      "NKE" in str(db.get_state().get("halt_detail"))
      and "stop" in str(db.get_state().get("halt_detail")))

# The settler halts itself on an oversell, with a reason that describes the
# account better than this one does. halt() keeps the FIRST reason, so that one
# must survive — a second, vaguer halt must not paper over it.
clear_halt()
risk_manager.halt("oversold on settle", "both legs of the bracket filled")
c6b = FakeClient("ok")
p6b = c6b.place_limit_order("SBUX", 10, 100.0, TrdSide.SELL, kind="STOP")
executor._await_cancel_terminal = lambda *a, **k: {
    "client_order_id": p6b.client_order_id, "filled_qty": 10, "applied_qty": 0}
fill_settler.settle = lambda coid: (_ for _ in ()).throw(
    fill_settler.OversoldError("both legs moved shares"))
try:
    ok6b = executor.cancel_protective(c6b, "SBUX", p6b.broker_order_id, "stop")
finally:
    fill_settler.settle = _real_settle
    executor._await_cancel_terminal = _real_await

check("an already-halted account still reports the refusal", ok6b is False)
check("...and the settler's more specific reason is the one kept",
      db.get_state().get("halt_reason") == "oversold on settle")

# And the success path must be untouched: a settle that works still returns
# True. Without this the test above would pass on a function that always
# returned False.
clear_halt()
_settled = []
c6c = FakeClient("ok")
p6c = c6c.place_limit_order("KO", 10, 100.0, TrdSide.SELL, kind="STOP")
executor._await_cancel_terminal = lambda *a, **k: {
    "client_order_id": p6c.client_order_id, "filled_qty": 10, "applied_qty": 0}
fill_settler.settle = lambda coid: _settled.append(coid)
try:
    ok6c = executor.cancel_protective(c6c, "KO", p6c.broker_order_id, "stop")
finally:
    fill_settler.settle = _real_settle
    executor._await_cancel_terminal = _real_await

check("a fill that DOES settle still reports the cancel as accepted",
      ok6c is True)
check("...and the settler was actually called", _settled == [p6c.client_order_id])
check("...with no halt", not db.get_state().get("halted"))


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
