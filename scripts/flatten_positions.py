"""Close every open position, through the code that normally closes them.

    .venv/bin/python scripts/flatten_positions.py            # asks first
    .venv/bin/python scripts/flatten_positions.py --confirm

Runs as a real protocol-started worker — lease, session, broker binding,
fencing token, order gate — and calls executor.close_position(), the same
function the gap sentinel uses. Not a hand-written sell: routing around the
executor would leave the ledger, the settler and closed_trades to be patched up
by hand afterwards, which is the thing every halt in this system exists to
prevent.

SIMULATE only. The binding and the gateway are both checked before anything is
sent, and a REAL account aborts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STAGING = Path(os.environ.get("MMT_HOME", Path.home() / "MooTraderStaging"))
RESULT = STAGING / "flatten_result.json"
_CLIENT: list = [None]


def worker(reason: str) -> int:
    try:
        return _worker(reason)
    finally:
        try:
            _CLIENT[0].close()
        except Exception:
            pass


def _worker(reason: str) -> int:
    from src import start_protocol
    start_protocol.worker_verify_and_report()
    start_protocol.wait_for_go()

    from src import broker_binding, db, executor, order_gate, order_log
    from src.moo_client import MooClient
    import moomoo

    out = {"closed": [], "failed": [], "reason": reason}

    if not order_gate.permitted():
        print("order gate is shut — nothing can be sent", file=sys.stderr)
        RESULT.write_text(json.dumps({"error": "order gate shut"}))
        return 2

    client = MooClient()
    _CLIENT[0] = client
    client.trade
    binding = broker_binding.require("flattening positions")
    if binding.trade_env != "SIMULATE":
        print(f"ABORT: binding is {binding.trade_env}, not SIMULATE",
              file=sys.stderr)
        RESULT.write_text(json.dumps({"error": f"env {binding.trade_env}"}))
        return 2
    ret, accs = client.trade.get_acc_list()
    row = accs[accs["acc_id"] == binding.acc_id]
    gw = str(row.iloc[0]["trd_env"]) if len(row) else "?"
    if gw != "SIMULATE":
        print(f"ABORT: gateway says {gw}", file=sys.stderr)
        RESULT.write_text(json.dumps({"error": f"gateway {gw}"}))
        return 2

    held = dict(db.load_open_trades())
    print(f"\nflattening {len(held)} position(s) as {reason}: "
          f"{ {s: t['qty'] for s, t in held.items()} }\n", flush=True)

    for sym in list(held):
        try:
            action = executor.close_position(client, sym, reason)
            print(f"  {sym}: closed {action.get('qty', '?')} @ "
                  f"${action.get('price', 0):.2f}  pnl "
                  f"${action.get('pnl', 0):+.2f}", flush=True)
            out["closed"].append({"symbol": sym, **{
                k: action.get(k) for k in ("qty", "price", "pnl", "reason")}})
        except Exception as e:
            print(f"  {sym}: FAILED — {e}", flush=True)
            out["failed"].append({"symbol": sym, "error": str(e)[:300]})

    # Whatever the broker still shows, after everything has settled.
    order_log.reconcile_live(client)
    ret, pos = client.trade.position_list_query(
        trd_env=moomoo.TrdEnv.SIMULATE, acc_id=binding.acc_id)
    leftover = {}
    if ret == moomoo.RET_OK and len(pos):
        for _, r in pos.iterrows():
            n = int(float(r["can_sell_qty"]))
            if n:
                leftover[str(r["code"])] = n
    out["broker_leftover"] = leftover
    out["ledger_leftover"] = {s: t["qty"] for s, t in db.load_open_trades().items()}
    # db.conn() is a context manager, not a connection — an earlier version
    # called .execute() straight on it and crashed AFTER every position had
    # already been closed, so the work was done and the record was not.
    with db.conn() as c:
        out["closed_trades_total"] = c.execute(
            "SELECT COUNT(*) FROM closed_trades").fetchone()[0]
    RESULT.write_text(json.dumps(out, indent=2, default=str))
    print(f"\n  broker still holds: {leftover or 'nothing'}")
    print(f"  ledger still holds: {out['ledger_leftover'] or 'nothing'}")
    return 1 if out["failed"] else 0


def parent(reason: str) -> int:
    os.environ["MMT_HOME"] = str(STAGING)
    from src import start_protocol
    if RESULT.exists():
        RESULT.unlink()
    try:
        r = start_protocol.start(
            "test", home=STAGING, allow_orders=True,
            worker_cmd=[sys.executable, str(Path(__file__).resolve()),
                        "--worker", reason])
    except start_protocol.StartRefused as e:
        print(f"refused ({e.code}): {e.detail}", file=sys.stderr)
        return 3
    pid = r.get("pid")
    print(f"  worker {pid} cleared, fence {r.get('fence')}", flush=True)
    import time
    while pid:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True).stdout.strip()
        if not st or st.startswith("Z"):
            break
        time.sleep(1)
    try:
        start_protocol.stop("test", home=STAGING)
    except Exception as e:
        print(f"  (stop: {e})")
    if RESULT.exists():
        print("\n" + RESULT.read_text())
    return 0


if __name__ == "__main__":
    if "--worker" in sys.argv:
        sys.exit(worker(sys.argv[sys.argv.index("--worker") + 1]))
    if "--confirm" not in sys.argv:
        print(__doc__)
        print("Refusing without --confirm.")
        sys.exit(1)
    sys.exit(parent("MANUAL_FLATTEN"))
