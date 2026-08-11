#!/usr/bin/env python3
"""Reconcile our closed-trade ledger against moomoo's own record.

    .venv/bin/python scripts/reconcile_broker_ledger.py            # use cache
    .venv/bin/python scripts/reconcile_broker_ledger.py --refresh  # re-query

Read-only against both the broker and the database. Writes only its own cache.

WHY THIS IS NOT A ONE-LINER
  OpenD rate-limits `history_order_list_query` hard — a handful of consecutive
  month-sized calls start returning "request failed due to high frequency", and
  a partial answer here is worse than none: a query window that starts after a
  position was opened shows the closing sell with no matching buy and reports a
  naked short that never existed. That is exactly what a first pass at this
  suggested for XLF, until a wider window showed the 2026-07-29 buy of 17 was
  covering a short opened before the window began.

  So months are fetched with pacing and cached to disk. Re-running is cheap and
  the picture only gets more complete.

WHAT IT CAN AND CANNOT SETTLE
  It can confirm the CURRENT position against `position_list_query`, which is
  authoritative and needs no history.

  It cannot, on its own, settle cumulative realized PnL. The broker's position
  list and its order history disagree here: the list holds JNJ alone, while the
  order arithmetic over the fetched window nets -30 shares across ten symbols.
  One of those is wrong, or the account was reset at some point and only the
  orders survived. Until that is explained, treat our ledger's PnL as our
  ledger's, not as verified.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_HOME = Path.home() / "Library/Application Support/MooMooTrader"
APP_DB = APP_HOME / "data" / "trader.db"
CACHE = ROOT / "data" / "broker_fills_cache.json"

PACE_SECONDS = 3.0        # between month queries; below this OpenD starts refusing
RETRIES = 3


def _env():
    out = {}
    p = APP_HOME / ".env"
    if not p.exists():
        p = ROOT / ".env"
    for line in p.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, _, v = s.partition("=")
            out[k.strip()] = v.split("#")[0].strip()
    return out


def _months(start: date, end: date):
    cur = date(start.year, start.month, 1)
    while cur < end:
        nxt = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
        yield cur.isoformat(), min(nxt, end).isoformat()
        cur = nxt


def fetch(refresh: bool) -> dict:
    cache = {}
    if CACHE.exists() and not refresh:
        cache = json.loads(CACHE.read_text())

    from moomoo import (RET_OK, OpenSecTradeContext, SecurityFirm, TrdEnv,
                        TrdMarket)
    env = _env()
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US, host=env.get("MOO_HOST", "127.0.0.1"),
        port=int(env.get("MOO_PORT", 11111)),
        security_firm=getattr(SecurityFirm, env.get("MOO_SECURITY_FIRM", "FUTUMY")))
    try:
        ret, accs = ctx.get_acc_list()
        if ret != RET_OK:
            sys.exit(f"get_acc_list failed: {accs}")
        sim = accs[accs["trd_env"] == "SIMULATE"]
        if sim.empty:
            sys.exit("no SIMULATE account")
        acc_id = int(sim.iloc[0]["acc_id"])

        ret, pos = ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=acc_id)
        positions = ([] if ret != RET_OK or pos.empty else
                     [{"code": r["code"], "qty": float(r["qty"]),
                       "cost": float(r.get("cost_price") or 0),
                       "pl": float(r.get("pl_val") or 0)}
                      for _, r in pos.iterrows()])

        fills = dict(cache.get("fills", {}))
        misses = []
        for a, z in _months(date(2025, 1, 1), date.today() + timedelta(days=1)):
            if a in fills and not refresh:
                continue
            got = None
            for attempt in range(RETRIES):
                ret, od = ctx.history_order_list_query(
                    trd_env=TrdEnv.SIMULATE, acc_id=acc_id, start=a, end=z)
                if ret == RET_OK:
                    f = od[od["order_status"].astype(str)
                           .str.contains("FILLED_ALL", na=False)]
                    got = [{"t": str(r["create_time"]),
                            "sym": str(r["code"]).split(".")[-1],
                            "side": str(r["trd_side"]),
                            "qty": int(r["dealt_qty"]),
                            "px": float(r.get("dealt_avg_price") or 0)}
                           for _, r in f.iterrows()]
                    break
                time.sleep(PACE_SECONDS * (attempt + 2))   # backoff, then retry
            if got is None:
                misses.append(a)
            else:
                fills[a] = got
            time.sleep(PACE_SECONDS)

        out = {"acc_id_present": True, "positions": positions,
               "fills": fills, "missing_months": misses,
               "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(out, indent=1))
        return out
    finally:
        ctx.close()


def report(data: dict) -> int:
    all_fills = [f for month in data["fills"].values() for f in month]
    buys, sells = Counter(), Counter()
    for f in all_fills:
        (buys if f["side"] == "BUY" else sells)[f["sym"]] += f["qty"]

    months = sorted(data["fills"])
    print(f"broker fills cached: {len(all_fills)} across {len(months)} month(s) "
          f"({months[0] if months else '—'} … {months[-1] if months else '—'})")
    if data["missing_months"]:
        print(f"  INCOMPLETE — {len(data['missing_months'])} month(s) could not "
              f"be fetched: {', '.join(data['missing_months'][:6])}"
              + (" …" if len(data["missing_months"]) > 6 else ""))
        print("  Re-run to fill them in; conclusions below are provisional.")

    print("\ncurrent positions (authoritative — no history needed):")
    for p in data["positions"] or []:
        print(f"  {p['code']:<10} qty={p['qty']:<8g} cost={p['cost']:<10g} pl={p['pl']:+.2f}")
    if not data["positions"]:
        print("  flat")

    db = sqlite3.connect(f"file:{APP_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    ours = {r["symbol"]: r for r in db.execute("SELECT * FROM open_trades")}
    print("\n  our open_trades vs broker:")
    broker_syms = {p["code"].split(".")[-1]: p for p in data["positions"] or []}
    for sym in sorted(set(ours) | set(broker_syms)):
        o, b = ours.get(sym), broker_syms.get(sym)
        if o and b and int(o["qty"]) == int(b["qty"]):
            print(f"    {sym:<6} MATCH qty={int(o['qty'])}")
        elif o and b:
            print(f"    {sym:<6} QTY MISMATCH ours={int(o['qty'])} broker={int(b['qty'])}")
        elif b:
            print(f"    {sym:<6} BROKER ONLY qty={int(b['qty'])} — unrecorded position")
        else:
            print(f"    {sym:<6} OURS ONLY qty={int(o['qty'])} — phantom holding")

    print("\n  order arithmetic (net per symbol, non-zero only):")
    nz = {s: buys[s] - sells[s] for s in set(buys) | set(sells)
          if buys[s] - sells[s] != 0}
    for s in sorted(nz):
        print(f"    {s:<6} buy {buys[s]:>4} sell {sells[s]:>4} net {nz[s]:>+5}")
    if not nz:
        print("    all symbols net flat")

    implied = {s: n for s, n in nz.items() if n > 0}
    listed = {s: int(p["qty"]) for s, p in broker_syms.items()}
    if implied != listed:
        print("\n  ⚠ the broker's own two views disagree:")
        print(f"      position list : {listed}")
        print(f"      orders imply  : {implied}")
        print("    A window that opens after a position did shows its closing")
        print("    sell with no buy and reports a short that never existed, so")
        print("    do NOT treat the negatives as real until every month is")
        print("    fetched. Cumulative PnL stays unverified while this holds.")

    n_raw = db.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM closed_trades").fetchone()
    n_eff = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM closed_trades WHERE extra IS NULL "
        "OR extra NOT LIKE '%\"excluded_from_performance\": true%'").fetchone()
    print(f"\n  our ledger: raw {n_raw[0]} trades ${n_raw[1]:.2f} | "
          f"effective {n_eff[0]} trades ${n_eff[1]:.2f}")
    print("  (effective excludes records marked as test/duplicate — see ledger_quality)")
    return 1 if data["missing_months"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true",
                    help="re-query every month instead of using the cache")
    args = ap.parse_args()
    if args.refresh or not CACHE.exists():
        data = fetch(args.refresh)
    else:
        data = json.loads(CACHE.read_text())
        print(f"(cache from {data.get('fetched_at')} — --refresh to re-query)\n")
    return report(data)


if __name__ == "__main__":
    sys.exit(main())
