"""Stage A — the first time an order actually goes through the new path.

    .venv/bin/python scripts/stage_a_execution_proof.py

WHAT THIS IS FOR

  Twenty test suites say the execution layer behaves correctly. Every one of
  them feeds it a fixture I wrote. None of them has ever seen a broker.

  The `orders` table in staging has zero rows. So the claim "execution safety
  is done" rests entirely on inputs chosen by the same person who wrote the
  code being tested. That is the gap this closes: one real order, placed
  through moo_client, accepted by a real gateway, recorded by order_log, and
  settled by fill_settler — with the database inspected between every step.

SAFETY

  SIMULATE only, asserted three ways before anything is sent: the resolved
  broker binding, the account's own trd_env as the gateway reports it, and the
  configured environment. Any disagreement aborts before the first order.

  A paper account is the broker's own test environment. No money moves and no
  security changes hands; this is a loopback test of our software against the
  gateway that will one day carry real orders.

  It runs as a genuine protocol-started worker — lease, session, broker
  binding, fencing token, order gate — because a harness that grants itself
  permission proves the harness, not the protocol.

WHAT IT DOES NOT PROVE

  The strategy path. This places orders directly rather than through scan →
  score → size, so risk_manager, concentration and the entry threshold are not
  exercised here. It proves the ORDER path underneath them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STAGING = Path.home() / "MooTraderStaging"
STATE = STAGING / "stage_a_state.json"

PASS, FAIL = 0, 0
LOG: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    mark = "  ok  " if cond else " FAIL "
    line = mark + name + (f"   [{detail}]" if detail else "")
    print(line, flush=True)
    LOG.append(line)
    if cond:
        PASS += 1
    else:
        FAIL += 1
    return cond


def note(msg):
    print("       " + msg, flush=True)
    LOG.append("       " + msg)


# ───────────────────────── worker role ─────────────────────────

def worker(phase: str) -> int:
    from src import start_protocol
    ready = start_protocol.worker_verify_and_report()
    start_protocol.wait_for_go()

    from src import broker_binding, db, fill_settler, order_gate, order_log
    from src.moo_client import MooClient
    from moomoo import TrdSide

    print(f"\n=== worker cleared: fence {ready.get('fence')}, "
          f"session {str(ready.get('session_id'))[:8]} ===\n", flush=True)

    # ── environment assertions, before anything is sent ──
    print("0  this is a paper account, asserted three ways")
    check("the order gate is OPEN (the parent granted it)", order_gate.permitted())
    # Opening the trade context is what resolves and pins the binding — it is
    # deliberately not a separate step anyone could forget or skip.
    client = MooClient()
    client.trade
    binding = broker_binding.require("stage A")
    check("the resolved binding is SIMULATE", binding.trade_env == "SIMULATE",
          binding.trade_env)
    acc = binding.acc_id
    note(f"broker acc_id {acc}, firm_verified={binding.firm_verified}")

    import moomoo
    ret, accs = client.trade.get_acc_list()
    row = accs[accs["acc_id"] == acc]
    gateway_env = str(row.iloc[0]["trd_env"]) if len(row) else "?"
    check("the gateway agrees this account is SIMULATE",
          gateway_env == "SIMULATE", gateway_env)
    if binding.trade_env != "SIMULATE" or gateway_env != "SIMULATE":
        print("\nABORT: not a paper account.", file=sys.stderr)
        return 2

    SYM = os.environ.get("STAGE_A_SYMBOL", "F")
    px = float(client.get_snapshot(SYM).get("last_price") or 0)
    note(f"{SYM} last = {px:.2f}")

    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    # ═══════════════ phase one: A1, A2, A3, and the crash ═══════════════
    if phase == "one":
        # ── start from flat, whatever a previous run left behind ────────
        # A harness that only works on a clean account is a harness that gets
        # run once. Cancel anything resting, sell anything held, and let the
        # settler book it — the cleanup uses the same path it is testing.
        print("0b  flattening whatever the last run left")
        order_log.reconcile_live(client)
        for o in order_log.live_orders():
            if o.get("broker_order_id"):
                client.cancel_order(o["broker_order_id"])
        time.sleep(1.5)
        order_log.reconcile_live(client)
        fill_settler.settle_all()
        ret, pos = client.trade.position_list_query(
            trd_env=moomoo.TrdEnv.SIMULATE, acc_id=acc)
        held_broker = 0
        if ret == moomoo.RET_OK and len(pos):
            row = pos[pos["code"] == f"US.{SYM}"]
            held_broker = int(float(row.iloc[0]["can_sell_qty"])) if len(row) else 0
        note(f"broker holds {held_broker} {SYM} before we start")
        if held_broker:
            # Make the ledger agree with the broker, then close through it.
            if db.load_open_trades().get(SYM, {}).get("qty", 0) != held_broker:
                db.upsert_open_trade({"symbol": SYM, "qty": held_broker,
                                      "entry_price": px,
                                      "stop_loss": round(px * .9, 2),
                                      "take_profit": round(px * 1.2, 2)})
            pf = client.place_limit_order(SYM, held_broker, round(px * 0.98, 2),
                                          TrdSide.SELL, kind="EXIT",
                                          intent="stage-A-flatten")
            rf = client.await_fill(pf.client_order_id, timeout=45.0)
            if int(rf["filled_qty"] or 0):
                fill_settler.settle(pf.client_order_id)
            note(f"flattened {rf['filled_qty']} {SYM}")
        check("starting flat at the broker",
              _broker_qty(client, acc, SYM) == 0, str(_broker_qty(client, acc, SYM)))
        check("...and flat in the ledger", db.load_open_trades().get(SYM) is None,
              str(db.load_open_trades().get(SYM)))

        # ── A1 ──────────────────────────────────────────────────────────
        print("\nA1  a resting limit, far from the market")
        low = round(px * 0.70, 2)
        p1 = client.place_limit_order(SYM, 1, low, TrdSide.BUY,
                                      kind="ENTRY", intent="stage-A1")
        r = order_log.get(p1.client_order_id)
        check("an orders row exists", r is not None)
        check("...in a live state, not terminal",
              r["state"] in ("SUBMITTED", "PENDING_SUBMIT", "PARTIAL"), r["state"])
        check("...carrying the broker's id", bool(r.get("broker_order_id")))
        check("...and nothing has filled", int(r["filled_qty"] or 0) == 0)
        check("...with nothing applied to the ledger",
              int(r["applied_qty"] or 0) == 0)
        note(f"coid={p1.client_order_id}  broker={r.get('broker_order_id')}  @{low}")

        # the client_order_id must survive the round trip as the broker's remark
        ret, live = client.trade.order_list_query(
            trd_env=moomoo.TrdEnv.SIMULATE, acc_id=acc)
        mine = live[live["order_id"].astype(str) == str(r["broker_order_id"])] \
            if ret == moomoo.RET_OK and len(live) else None
        remark = str(mine.iloc[0].get("remark", "")) if mine is not None and len(mine) else ""
        check("the client_order_id round-trips as the broker's remark",
              remark == p1.client_order_id, f"{remark!r}")

        # A second order for the same intent must be refused by the DATABASE —
        # the partial unique index, not a check in Python. Assert the outcome
        # (it raised, and no second row was written), not the wording: an
        # earlier version of this matched on the message text and reported a
        # failure while the code was doing exactly the right thing.
        n_before = len(order_log.recent(limit=200, symbol=SYM))
        raised = None
        try:
            client.place_limit_order(SYM, 1, low, TrdSide.BUY,
                                     kind="ENTRY", intent="stage-A1-dup")
        except Exception as e:
            raised = e
        check("a second live order for the same intent is refused",
              raised is not None, type(raised).__name__ if raised else "no error")
        check("...and no second row reached the orders table",
              len(order_log.recent(limit=200, symbol=SYM)) == n_before)
        note(f"refusal: {str(raised)[:88]}")

        # ── A2 ──────────────────────────────────────────────────────────
        print("\nA2  cancelling it leaves the ledger alone")
        ok = client.cancel_order(r["broker_order_id"])
        check("the broker accepted the cancel", ok)
        for _ in range(20):
            time.sleep(0.5)
            order_log.reconcile_live(client)
            r2 = order_log.get(p1.client_order_id)
            if r2["state"] in ("CANCELLED", "EXPIRED"):
                break
        r2 = order_log.get(p1.client_order_id)
        check("the order reached a terminal cancelled state",
              r2["state"] in ("CANCELLED", "EXPIRED"), r2["state"])
        check("...having filled nothing", int(r2["filled_qty"] or 0) == 0)
        check("...and applied nothing", int(r2["applied_qty"] or 0) == 0)
        check("no position was created", db.load_open_trades().get(SYM) is None)
        s = fill_settler.settle_all()
        check("the settler finds nothing to do on a cancelled order",
              int(s.get("applied", 0)) == 0, json.dumps(s))

        # ── A3 ──────────────────────────────────────────────────────────
        print("\nA3  a marketable order fills, and the settler books it")
        high = round(px * 1.02, 2)
        p3 = client.place_limit_order(SYM, 2, high, TrdSide.BUY,
                                      kind="ENTRY", intent="stage-A3")
        row = client.await_fill(p3.client_order_id, timeout=45.0)
        check("the order filled", int(row["filled_qty"] or 0) > 0,
              f"{row['filled_qty']} @ {row.get('avg_fill_price')} state={row['state']}")
        if int(row["filled_qty"] or 0) == 0:
            note("no fill — cannot continue; cancelling and stopping")
            client.cancel_order(row["broker_order_id"])
            STATE.write_text(json.dumps({"aborted": "A3 did not fill"}))
            return 1
        check("...but nothing is applied yet", int(row["applied_qty"] or 0) == 0)

        qty = int(row["filled_qty"])
        fill_px = float(row["avg_fill_price"] or high)
        trade = {"symbol": SYM, "qty": qty, "entry_price": fill_px,
                 "stop_loss": round(fill_px * 0.90, 2),
                 "take_profit": round(fill_px * 1.20, 2),
                 "strategy": "stage_a"}
        res = fill_settler.open_position(p3.client_order_id, trade, qty, fill_px)
        r3 = order_log.get(p3.client_order_id)
        check("the settler applied exactly the filled quantity",
              int(r3["applied_qty"]) == qty, f"{r3['applied_qty']} of {qty}")
        check("...and the position exists",
              db.load_open_trades().get(SYM, {}).get("qty") == qty)
        check("...with the notional priced from the fill",
              abs(float(r3["applied_notional"]) - qty * fill_px) < 0.01)

        # settling again must be a no-op
        s = fill_settler.settle_all()
        r3b = order_log.get(p3.client_order_id)
        check("a second settlement pass applies nothing more",
              int(r3b["applied_qty"]) == qty, f"{r3b['applied_qty']}")
        check("...and does not duplicate the position",
              db.load_open_trades().get(SYM, {}).get("qty") == qty)

        # ── A4 setup: fill an order, then die before settling ───────────
        print("\nA4  crash between the fill and the settlement")
        p4 = client.place_limit_order(SYM, 1, round(px * 1.02, 2), TrdSide.BUY,
                                      kind="STACK", intent="stage-A4")
        row4 = client.await_fill(p4.client_order_id, timeout=45.0)
        check("the second order filled", int(row4["filled_qty"] or 0) > 0,
              f"{row4['filled_qty']} state={row4['state']}")
        check("...and is recorded as filled but UNAPPLIED",
              int(row4["applied_qty"] or 0) == 0)
        STATE.write_text(json.dumps({
            "symbol": SYM, "a3_coid": p3.client_order_id,
            "a3_qty": qty, "a3_px": fill_px,
            "a4_coid": p4.client_order_id,
            "a4_qty": int(row4["filled_qty"]),
            "a4_px": float(row4["avg_fill_price"] or 0),
            "log": LOG, "pass": PASS, "fail": FAIL,
        }))
        note("killing this process with SIGKILL, mid-flight, on purpose")
        sys.stdout.flush()
        os.kill(os.getpid(), 9)      # no cleanup, no atexit, no commit
        return 0                      # unreachable

    # ═══════════════ phase two: recovery, then close out ═══════════════
    print("\nA4  (continued) recovery after the crash")
    a4 = state.get("a4_coid")
    r4 = order_log.get(a4)
    check("the killed process left the fill recorded", int(r4["filled_qty"]) > 0,
          f"{r4['filled_qty']}")
    check("...and still unapplied", int(r4["applied_qty"] or 0) == 0)
    before = db.load_open_trades().get(state["symbol"], {}).get("qty", 0)

    s = fill_settler.settle_all()
    r4b = order_log.get(a4)
    check("the settler applies the orphaned fill",
          int(r4b["applied_qty"]) == int(r4["filled_qty"]),
          f"{r4b['applied_qty']} of {r4['filled_qty']}")
    after = db.load_open_trades().get(state["symbol"], {}).get("qty", 0)
    check("...extending the position by exactly that many shares",
          after == before + int(r4["filled_qty"]), f"{before} → {after}")

    s2 = fill_settler.settle_all()
    r4c = order_log.get(a4)
    after2 = db.load_open_trades().get(state["symbol"], {}).get("qty", 0)
    check("running recovery a second time applies nothing",
          int(r4c["applied_qty"]) == int(r4b["applied_qty"]) and after2 == after,
          f"applied={r4c['applied_qty']} qty={after2}")

    # ── A6 ──────────────────────────────────────────────────────────────
    print("\nA6  the broker and the order log agree")
    rec = order_log.reconcile_live(client)
    check("reconciliation completed against the broker",
          bool(rec.get("complete")), json.dumps(rec)[:120])
    live = order_log.live_orders()
    check("no order is left in a live state", len(live) == 0,
          f"{[o['client_order_id'][:8] for o in live]}")

    # ── close out: sell what this test bought ───────────────────────────
    print("\nA7  closing the position this test opened")
    SYM = state["symbol"]
    held = db.load_open_trades().get(SYM, {}).get("qty", 0)
    note(f"holding {held} {SYM}")
    if held:
        pxn = float(client.get_snapshot(SYM).get("last_price") or 0)
        pe = client.place_limit_order(SYM, int(held), round(pxn * 0.98, 2),
                                      TrdSide.SELL, kind="EXIT",
                                      intent="stage-A-close")
        rowe = client.await_fill(pe.client_order_id, timeout=45.0)
        check("the closing order filled", int(rowe["filled_qty"] or 0) == held,
              f"{rowe['filled_qty']} of {held} state={rowe['state']}")
        se = fill_settler.settle(pe.client_order_id)
        check("the settler booked the close", int(se.get("applied", 0)) == held,
              json.dumps(se)[:100])
        check("...and the position is gone",
              db.load_open_trades().get(SYM) is None,
              str(db.load_open_trades().get(SYM)))
        check("...leaving a closed trade in the ledger",
              len(db.closed_trades(limit=5)) > 0)

    rec2 = order_log.reconcile_live(client)
    check("a final reconciliation is clean", bool(rec2.get("complete")))
    # Quantity, not row count: the broker keeps a zero-quantity row for a
    # symbol traded today, and counting rows calls a flat account a leftover.
    left = _broker_qty(client, acc, SYM)
    check("the broker holds none of it either", left == 0, f"{left} shares")

    prev = state.get("log", [])
    total_p = state.get("pass", 0) + PASS
    total_f = state.get("fail", 0) + FAIL
    STATE.write_text(json.dumps({"log": prev + LOG, "pass": total_p,
                                 "fail": total_f, "done": True}))
    print(f"\n=== phase two: {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


def _broker_qty(client, acc, sym) -> int:
    import moomoo
    ret, pos = client.trade.position_list_query(
        trd_env=moomoo.TrdEnv.SIMULATE, acc_id=acc)
    if ret != moomoo.RET_OK or not len(pos):
        return 0
    row = pos[pos["code"] == f"US.{sym}"]
    return int(float(row.iloc[0]["can_sell_qty"])) if len(row) else 0


# ───────────────────────── parent role ─────────────────────────

def parent() -> int:
    os.environ["MMT_HOME"] = str(STAGING)
    from src import start_protocol
    me = str(Path(__file__).resolve())

    if STATE.exists():
        STATE.unlink()

    for phase in ("one", "two"):
        print(f"\n{'='*66}\n  starting protocol worker — phase {phase}\n{'='*66}",
              flush=True)
        try:
            r = start_protocol.start(
                "test", home=STAGING,
                worker_cmd=[sys.executable, me, "--worker", phase],
                allow_orders=True)
            print(f"  worker {r.get('pid')} cleared, fence {r.get('fence')}",
                  flush=True)
        except start_protocol.StartRefused as e:
            print(f"  REFUSED [{e.code}] {e.detail}", file=sys.stderr)
            return 3
        # The worker runs to completion (phase one kills itself on purpose).
        pid = r.get("pid")
        while _alive(pid):
            time.sleep(1)
        try:
            start_protocol.stop("test", home=STAGING)
        except Exception as e:
            print(f"  (stop: {e})", flush=True)

    if STATE.exists():
        st = json.loads(STATE.read_text())
        print(f"\n{'='*66}")
        print(f"  STAGE A: {st.get('pass',0)} passed, {st.get('fail',0)} failed")
        print(f"{'='*66}")
        return 1 if st.get("fail") or not st.get("done") else 0
    return 1


def _alive(pid):
    """True while the worker is still running.

    Not os.kill(pid, 0): the worker is spawned with start_new_session, nobody
    reaps it, and a zombie answers signal 0 successfully — so that check waits
    forever on a process that already exited. Ask for the state instead and
    treat Z as gone.
    """
    if not pid:
        return False
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                       capture_output=True, text=True)
    st = r.stdout.strip()
    return bool(st) and not st.startswith("Z")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        sys.exit(worker(sys.argv[sys.argv.index("--worker") + 1]))
    sys.exit(parent())
