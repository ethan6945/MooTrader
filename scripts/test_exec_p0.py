"""The seven execution P0s, each exercised through its failure.

Run from repo root: .venv/bin/python scripts/test_exec_p0.py
No broker.

Each of these was a path where something went wrong and the bot kept trading.
None of them raised in production; all of them logged and continued. The tests
below inject the failure rather than the success, because the success path was
never the thing in doubt.

  1  A late FIRST fill had no position to be added to, so the settler raised —
     forever, every sweep, while the shares sat at the broker in nothing.
  2  A settlement failure was collected into a return value nobody read.
  3  Startup recovery caught its own exception and logged "continuing to
     protective exits, but the order picture may be incomplete".
  4  Two daily-rollover functions wrote the same kv_state key. Only one learned
     to preserve a halt that a new day does not answer; main called the other.
  5  realized_pnl_total was a counter that was incremented and never checked
     against the trades it claims to summarise — and it is the denominator of
     the drawdown breaker.
  6  A half-placed bracket left one live leg at the broker while the caller fell
     back to soft tracking, so the same shares could be sold twice.
  7  READY was written without ever asking the broker who this run is talking
     to; the binding resolved lazily, after GO, inside trading.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-p0-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

from src import db, fill_settler, identity, kill_switch, order_log  # noqa: E402
from src import risk_manager                                        # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name + (f"   [{detail}]" if detail else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1


db._ensure_initialised()
identity._session = None
identity.reset_cache()
identity.start_session("SIMULATE")
ACCT = db._require_account_id("test setup")


def clear():
    risk_manager.release_halt("test-setup")
    with db.conn() as c:
        c.execute("DELETE FROM orders")
        c.execute("DELETE FROM open_trades")
        c.execute("DELETE FROM closed_trades")
        c.commit()
    fill_settler._HALT_AFTER_COMMIT.clear()
    fill_settler._NEEDS_MIRROR.clear()


def make_order(coid, symbol, side, kind, qty, price, extra=None, filled=None):
    """An order row already filled at the broker and not yet applied."""
    with db.conn() as c:
        c.execute(
            "INSERT INTO orders (client_order_id, account_id, session_id, symbol,"
            " side, kind, requested_qty, limit_price, state, filled_qty,"
            " avg_fill_price, applied_qty, applied_notional, created_at, extra)"
            " VALUES (?,?,?,?,?,?,?,?,'FILLED',?,?,0,0,'2026-08-20T00:00:00Z',?)",
            (coid, ACCT, "s1", symbol, side, kind, qty, price,
             qty if filled is None else filled, price,
             json.dumps(extra) if extra else None))
        c.commit()


# ── 1. a late FIRST fill becomes a real position ───────────────────────────
print("1  a fill that arrives after the entry gave up")
clear()
make_order("c-late", "AAPL", "BUY", "ENTRY", 10, 100.0,
           extra={"intended_stop": 95.0, "intended_tp": 120.0, "atr": 2.5,
                  "strategy": "trend"})
out = fill_settler.settle("c-late")
check("it is applied rather than raising", out["applied"] == 10, str(out))
pos = db.load_open_trades().get("AAPL")
check("...and the position now exists", pos is not None)
check("...at the filled quantity", pos and pos["qty"] == 10, str(pos and pos["qty"]))
check("...carrying the stop the ENTRY intended",
      pos and abs(pos["stop_loss"] - 95.0) < 1e-9, str(pos and pos["stop_loss"]))
check("...and its take-profit", pos and abs(pos["take_profit"] - 120.0) < 1e-9)
check("the order is marked fully applied",
      order_log.get("c-late")["applied_qty"] == 10)
check("a protected late fill does NOT halt", not risk_manager.halt_status()["halted"],
      str(risk_manager.halt_status()["reason"]))

out2 = fill_settler.settle("c-late")
check("settling it again applies nothing", out2["applied"] == 0)
check("...and does not duplicate the position",
      db.load_open_trades()["AAPL"]["qty"] == 10)

# The same fill with no recorded protection: booked, then halted.
clear()
make_order("c-bare", "MSFT", "BUY", "ENTRY", 5, 200.0)      # no extra
fill_settler.settle("c-bare")
check("an unprotected late fill is still booked",
      db.load_open_trades().get("MSFT", {}).get("qty") == 5)
st = risk_manager.halt_status()
check("...and halts", st["halted"], str(st["reason"]))
check("...naming the missing protection",
      st["reason"] == "late fill without protection", str(st["reason"]))
check("...requiring a person", st["needs_manual_release"])


# ── 2. a settlement failure halts ──────────────────────────────────────────
print("\n2  a fill the ledger could not accept stops the bot")
clear()
# A SELL of shares that are not held: _apply_sell cannot book it.
make_order("c-bad", "TSLA", "SELL", "EXIT", 7, 300.0)
res = fill_settler.settle_all()
check("the sweep reports the failure", len(res["failures"]) >= 1, str(res["failures"])[:80])
st = risk_manager.halt_status()
check("...and halts rather than returning it to nobody", st["halted"])
# The MORE SPECIFIC reason wins: halt() keeps the first one, and _apply_sell
# has already said "oversold position", which tells an operator what to look
# for. A generic "settlement failed" on top of it would be a worse message.
check("...naming the specific cause, not the generic one",
      st["reason"] == "oversold position", str(st["reason"]))
check("...requiring a person", st["needs_manual_release"])

# And a failure with no more specific reason of its own still halts — the
# sweep's own guard, tested without relying on which inner error occurred.
clear()
make_order("c-boom", "NFLX", "BUY", "ENTRY", 3, 50.0,
           extra={"intended_stop": 45.0, "intended_tp": 60.0})
_real_settle = fill_settler.settle
fill_settler.settle = lambda coid, **k: (_ for _ in ()).throw(
    RuntimeError("the database went away"))
try:
    res = fill_settler.settle_all()
finally:
    fill_settler.settle = _real_settle
st = risk_manager.halt_status()
check("an unclassified settlement failure halts too", st["halted"])
check("...naming settlement", st["reason"] == "fill settlement failed", str(st["reason"]))
check("...and says which order and why",
      "c-boom" in str(st["detail"]) and "database went away" in str(st["detail"]),
      str(st["detail"])[:70])
check("...requiring a person", st["needs_manual_release"])


# ── 3. the halts that a new day does not answer ────────────────────────────
print("\n3  one daily rollover, and it keeps what the clock cannot fix")
check("kill_switch delegates rather than reimplementing",
      "risk_manager.reset_for_new_day" in
      (ROOT / "src" / "kill_switch.py").read_text())

for reason, survives in (("fill settlement failed", True),
                         ("oversold position", True),
                         ("position reconciliation failed", True),
                         ("late fill without protection", True),
                         ("startup recovery failed", True),
                         ("daily drawdown", False)):
    clear()
    risk_manager.halt(reason, "injected")
    db.update_state({"day": "1999-01-01"})          # force a rollover
    kill_switch.reset_for_new_day(10000.0)
    st = risk_manager.halt_status()
    check(f"{reason!r} survives midnight = {survives}",
          st["halted"] is survives, f"halted={st['halted']}")
    if not survives:
        check("...and its reason is cleared with it", st["reason"] is None,
              str(st["reason"]))

clear()
risk_manager.halt("oversold position", "injected")
db.update_state({"day": "1999-01-01"})
kill_switch.reset_for_new_day(10000.0)
check("a surviving halt keeps its reason readable",
      risk_manager.halt_status()["reason"] == "oversold position")
check("...and only release_halt clears it",
      risk_manager.release_halt("tester", "checked the account")["released"])
check("...leaving nothing behind",
      not risk_manager.halt_status()["halted"] and
      risk_manager.halt_status()["reason"] is None)


# ── 4. realized PnL can be rebuilt from the ledger ─────────────────────────
print("\n4  the drawdown denominator is checkable against the trades")
clear()
db.update_state({"budget_usd": 10000.0, "realized_pnl_total": 0.0,
                 "peak_equity": 10000.0})
for i, pnl in enumerate((150.0, -60.0, 25.5)):
    db.record_trade_close_row({
        "ts": f"2026-08-1{i}T10:00:00Z", "symbol": "AAA", "qty": 1,
        "entry": 100.0, "stop": 95.0, "exit": 100.0 + pnl, "pnl": pnl,
    }) if hasattr(db, "record_trade_close_row") else None
with db.conn() as c:
    for i, pnl in enumerate((150.0, -60.0, 25.5)):
        c.execute("INSERT INTO closed_trades (ts,symbol,qty,entry,stop,exit,"
                  "pnl,account_id) VALUES (?,?,?,?,?,?,?,?)",
                  (f"2026-08-1{i}T10:00:00Z", "AAA", 1, 100.0, 95.0,
                   100.0 + pnl, pnl, ACCT))
    c.commit()

check("the ledger totals the closed trades",
      abs(risk_manager.ledger_realized_pnl() - 115.5) < 1e-9,
      f"{risk_manager.ledger_realized_pnl()}")

db.update_state({"realized_pnl_total": 115.5})
d = risk_manager.realized_pnl_drift()
check("an accurate counter shows no drift", abs(d["drift"]) < 1e-9, str(d))
check("...and is left alone", not risk_manager.rebuild_realized_pnl("test")["rebuilt"])

# Drift the counter the dangerous way: too LOW, which pins the drawdown
# breaker open because equity never falls below a peak set from a smaller base.
db.update_state({"realized_pnl_total": 0.0})
out = risk_manager.rebuild_realized_pnl("test")
check("a drifted counter is detected", out["rebuilt"], str(out))
check("...and restored from the ledger",
      abs(float(db.get_state()["realized_pnl_total"]) - 115.5) < 1e-6,
      str(db.get_state()["realized_pnl_total"]))
check("...with the drawdown peak re-anchored to it",
      abs(float(db.get_state()["peak_equity"]) - 10115.5) < 1e-6,
      str(db.get_state()["peak_equity"]))


# ── 5. a bracket is both legs or neither ───────────────────────────────────
print("\n5  a half-placed bracket does not become a double sale")
from src import executor                                      # noqa: E402
from moomoo import TrdSide                                    # noqa: E402


class Broker:
    """Places what it is told to; fails whichever leg the test names."""
    def __init__(self, fail, cancel_ok=True):
        self.fail, self.cancel_ok = fail, cancel_ok
        self.placed, self.cancelled = [], []

    def place_stop_loss(self, symbol, qty, px, *, intent=""):
        if self.fail == "stop":
            raise RuntimeError("broker refused the stop")
        self.placed.append("stop")
        return type("P", (), {"broker_order_id": "STOP-1"})()

    def place_limit_order(self, symbol, qty, px, side, *, kind=None,
                          intent="", extra=None):
        if self.fail == "tp":
            raise RuntimeError("broker refused the take-profit")
        self.placed.append("tp")
        return type("P", (), {"broker_order_id": "TP-1"})()

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return self.cancel_ok


clear()
b = Broker(fail="tp")
s, tp = executor.place_bracket(b, "AAPL", 10, 95.0, 120.0)
check("the surviving STOP leg is withdrawn", b.cancelled == ["STOP-1"], str(b.cancelled))
check("...and the caller is told it has no bracket", (s, tp) == (None, None), f"{s},{tp}")
check("...without halting, because nothing is unaccounted for",
      not risk_manager.halt_status()["halted"])

clear()
b = Broker(fail="stop")
s, tp = executor.place_bracket(b, "AAPL", 10, 95.0, 120.0)
check("a surviving TP leg is withdrawn too", b.cancelled == ["TP-1"], str(b.cancelled))
check("...reported as no bracket", (s, tp) == (None, None))

clear()
b = Broker(fail="tp", cancel_ok=False)
s, tp = executor.place_bracket(b, "AAPL", 10, 95.0, 120.0)
st = risk_manager.halt_status()
check("a leg that cannot be withdrawn halts", st["halted"], str(st["reason"]))
check("...as an uncancellable protective order",
      st["reason"] == "protective order cancel failed", str(st["reason"]))
check("...requiring a person", st["needs_manual_release"])
check("...and the live order is NOT reported as absent", s == "STOP-1", str(s))

clear()
b = Broker(fail=None)
s, tp = executor.place_bracket(b, "AAPL", 10, 95.0, 120.0)
check("a complete bracket is left alone", (s, tp) == ("STOP-1", "TP-1"))
check("...with nothing cancelled", b.cancelled == [])


# ── 6. READY asks the broker who this is ───────────────────────────────────
print("\n6  READY is not written before the broker confirms the account")
sp = (ROOT / "src" / "start_protocol.py").read_text()
body = sp[sp.index("def worker_verify_and_report"):]
body = body[:body.index("\ndef ")]
probe_at = body.find("broker_binding.require")
ready_at = body.find("os.replace(tmp, p)")
check("the broker is consulted inside worker_verify_and_report", probe_at > 0)
check("...BEFORE the READY file is published", 0 < probe_at < ready_at,
      f"probe@{probe_at} ready@{ready_at}")
check("a gateway serving the wrong environment refuses the start",
      "broker_env_mismatch" in body)
check("...and an unreachable broker refuses too, rather than proceeding",
      "broker_unreachable" in body)
check("the session is closed on either refusal",
      body.count("identity.end_session") >= 2)
check("the broker's identity is carried in READY itself",
      '"broker_ref"' in body)


# ── 7. startup recovery fails closed ───────────────────────────────────────
print("\n7  a startup that could not establish the picture does not trade")
mn = (ROOT / "src" / "main.py").read_text()
blk = mn[mn.index("def _startup_recover_orders"):]
blk = blk[:blk.index("\ndef ")]
# Assert on the CODE, not on prose: the new comment quotes the old wording in
# order to record what was removed, so searching the file for that sentence
# finds the explanation rather than the behaviour.
handler = blk[blk.index("except Exception as e:"):]
check("the exception handler halts", "risk_manager.halt(" in handler)
check("...rather than only logging and returning",
      handler.index("risk_manager.halt(") < len(handler),
      "halt present in the handler")
check("...and the halt is not itself allowed to escape unnoticed",
      "could not halt after a failed startup recovery" in handler)
check("...naming startup recovery",
      '"startup recovery failed"' in blk)
check("and that reason needs a person",
      "startup recovery failed" in risk_manager.MANUAL_RELEASE_REASONS)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
