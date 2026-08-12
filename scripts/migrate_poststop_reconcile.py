#!/usr/bin/env python3
"""Reconcile the database with the broker after the 2026-08-11 unattended run.

    .venv/bin/python scripts/migrate_poststop_reconcile.py --db <path> --dry-run
    .venv/bin/python scripts/migrate_poststop_reconcile.py --db <path> --apply

Idempotent, receipted, and it refuses rather than guesses. Nothing here is a
hand-edit: the same corrections have to be applicable to a clone during the
staging rehearsal, and "what exactly was changed" has to be answerable later.

WHAT WENT WRONG
  The scheduler restarted on its own at 20:23 local, ran for two hours, and left
  the database disagreeing with the broker in four ways:

  1. param_* overrides reappeared. The packaged backend predates PARAMS_FROZEN,
     so it rewrote entry_threshold, tp_atr_mult and sl_atr_mult from its own
     logic. .env is supposed to be the only source of truth.

  2. A phantom position. open_trades held a position whose broker order was
     SUBMITTED with dealt_qty 0 and never filled — since cancelled. This is executor.py writing the position the moment
     place_limit_order returns an id, without waiting for a fill.

  3. A close was booked at LIMIT prices rather than fill prices — both ends
     wrong, PnL understated.

  4. realized_pnl_today stayed 0.0 through a +$62 close.

  Corrections 2 and 3 come from the broker, which outranks our records. The
  fills are read from data/broker_fills_cache or the snapshot taken at stop, not
  re-queried, so this migration needs no OpenD connection and produces the same
  result every time it runs.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RECEIPT_KEY = "poststop_reconcile"
VERSION = 1

# Broker evidence is NOT embedded here. Order ids, fill prices and timestamps
# are facts about a real brokerage account, and this file is tracked and
# publishable; the reconciliation is generic, the evidence is local.
#
# The file is written by whoever captured the fills (0600, gitignored) and has
# the shape produced by scripts/reconcile_broker_ledger.py:
#
#   {"fills":    {"SYM": {"buy":  {"order_id","qty","px","t"},
#                         "sell": {"order_id","qty","px","t"}}},
#    "unfilled": {"SYM": {"order_id","qty","limit","status","dealt"}}}
EVIDENCE = ROOT / "data" / "broker_evidence.json"


def load_evidence(path: Path) -> tuple[dict, dict]:
    """Fills and never-filled orders, from the local evidence file."""
    if not path.exists():
        sys.exit(
            f"REFUSED: no broker evidence at {path}.\n"
            f"  This migration corrects records against what the broker actually\n"
            f"  did, so it will not run on assumptions. Capture the fills first\n"
            f"  (scripts/reconcile_broker_ledger.py) and write them there, 0600.")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"REFUSED: cannot read {path}: {e}")
    fills = data.get("fills") or {}
    unfilled = data.get("unfilled") or {}
    for sym, f in fills.items():
        for side in ("buy", "sell"):
            if side not in f or "px" not in f[side] or "order_id" not in f[side]:
                sys.exit(f"REFUSED: evidence for {sym} is missing {side} "
                         f"order_id/px")
    return fills, unfilled


def _kv_scoped(c) -> bool:
    return any(r[1] == "account_id" for r in c.execute("PRAGMA table_info(kv_state)"))


def _kv(c, key, default=None):
    if _kv_scoped(c):
        rows = c.execute("SELECT account_id, value FROM kv_state WHERE key = ?",
                         (key,)).fetchall()
        if len(rows) > 1:
            raise SystemExit(f"REFUSED: {key!r} exists in {len(rows)} scopes")
        r = rows[0] if rows else None
        v = r["value"] if r else None
    else:
        r = c.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
        v = r["value"] if r else None
    if v is None:
        return default
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return v


def _kv_set(c, key, value) -> None:
    payload = json.dumps(value, default=str)
    if _kv_scoped(c):
        r = c.execute("SELECT account_id FROM kv_state WHERE key = ?",
                      (key,)).fetchone()
        scope = r["account_id"] if r else ""
        c.execute("INSERT OR REPLACE INTO kv_state (account_id, key, value) "
                  "VALUES (?, ?, ?)", (scope, key, payload))
    else:
        c.execute("INSERT OR REPLACE INTO kv_state (key, value) VALUES (?, ?)",
                  (key, payload))


def plan(c, BROKER_FILLS: dict, UNFILLED: dict) -> dict:
    """Everything this run would change, decided before anything is written."""
    out = {"params": [], "phantom": [], "fills": [], "today": None, "notes": []}

    for r in c.execute("SELECT key, value FROM kv_state WHERE key LIKE 'param%' "
                       "AND key != 'param_history'"):
        out["params"].append((r["key"], r["value"]))

    for sym, info in UNFILLED.items():
        row = c.execute("SELECT * FROM open_trades WHERE symbol = ?", (sym,)).fetchone()
        if row:
            out["phantom"].append({
                "symbol": sym, "qty": int(row["qty"]),
                "entry": float(row["entry_price"]),
                "order_id": info["order_id"], "dealt": info["dealt"]})

    for sym, f in BROKER_FILLS.items():
        row = c.execute(
            "SELECT * FROM closed_trades WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
            (sym,)).fetchone()
        if not row:
            out["notes"].append(f"{sym}: no closed_trade to correct")
            continue
        real_pnl = round((f["sell"]["px"] - f["buy"]["px"]) * f["sell"]["qty"], 4)
        if (abs(float(row["entry"]) - f["buy"]["px"]) > 1e-9
                or abs(float(row["exit"]) - f["sell"]["px"]) > 1e-9):
            out["fills"].append({
                "id": row["id"], "symbol": sym,
                "entry": (float(row["entry"]), f["buy"]["px"]),
                "exit": (float(row["exit"]), f["sell"]["px"]),
                "pnl": (round(float(row["pnl"] or 0), 4), real_pnl),
                "order_ids": [f["buy"]["order_id"], f["sell"]["order_id"]],
            })

    # realized_pnl_today: sum of today's effective closes, from the ledger.
    today = max((r["ts"][:10] for r in c.execute("SELECT ts FROM closed_trades")),
                default=None)
    if today:
        tot = 0.0
        for r in c.execute("SELECT pnl, extra FROM closed_trades WHERE ts LIKE ?",
                           (today + "%",)):
            try:
                extra = json.loads(r["extra"]) if r["extra"] else {}
            except (json.JSONDecodeError, TypeError):
                extra = {}
            if isinstance(extra, dict) and (extra.get("ledger_quality") or {}).get(
                    "excluded_from_performance"):
                continue
            tot += float(r["pnl"] or 0)
        # Apply the fill correction to the same figure.
        for f in out["fills"]:
            if f["symbol"] in {r["symbol"] for r in []}:
                pass
        for f in out["fills"]:
            row = c.execute("SELECT ts FROM closed_trades WHERE id = ?",
                            (f["id"],)).fetchone()
            if row and row["ts"][:10] == today:
                tot += f["pnl"][1] - f["pnl"][0]
        cur = float(_kv(c, "realized_pnl_today", 0.0) or 0.0)
        if abs(cur - tot) > 1e-6:
            out["today"] = {"date": today, "from": cur, "to": round(tot, 4)}
    return out


def run(db: Path, apply: bool, evidence: Path) -> int:
    c = sqlite3.connect(str(db) if apply else f"file:{db}?mode=ro", uri=not apply)
    c.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    prior = _kv(c, RECEIPT_KEY)
    entries = prior if isinstance(prior, list) else ([prior] if prior else [])
    if any(e.get("version") == VERSION for e in entries if isinstance(e, dict)):
        print(f"  migration v{VERSION} already applied "
              f"({len(entries)} receipt(s)); re-checking for drift\n")

    BROKER_FILLS, UNFILLED = load_evidence(evidence)
    p = plan(c, BROKER_FILLS, UNFILLED)
    print(f"  db: {db}\n")
    print(f"  param_* overrides to clear: {len(p['params'])}")
    for k, v in p["params"]:
        print(f"      {k} = {v}")
    print(f"  phantom positions to remove: {len(p['phantom'])}")
    for ph in p["phantom"]:
        print(f"      {ph['symbol']} qty={ph['qty']} @ {ph['entry']} — broker "
              f"order {ph['order_id']} filled {ph['dealt']}")
    print(f"  fills to correct: {len(p['fills'])}")
    for f in p["fills"]:
        print(f"      {f['symbol']} id={f['id']}  entry {f['entry'][0]} -> {f['entry'][1]}"
              f"   exit {f['exit'][0]} -> {f['exit'][1]}"
              f"   pnl {f['pnl'][0]} -> {f['pnl'][1]}")
    if p["today"]:
        t = p["today"]
        print(f"  realized_pnl_today ({t['date']}): {t['from']} -> {t['to']}")
    else:
        print("  realized_pnl_today: already correct")
    for n in p["notes"]:
        print(f"  note: {n}")

    changes = (len(p["params"]) + len(p["phantom"]) + len(p["fills"])
               + (1 if p["today"] else 0))
    if not apply:
        print(f"\n  {changes} change(s) pending — dry run, nothing written")
        c.close()
        return 0
    if not changes:
        print("\n  nothing to do")
        c.close()
        return 0

    c.execute("BEGIN IMMEDIATE")
    try:
        hist = _kv(c, "param_history", []) or []
        for k, v in p["params"]:
            c.execute("DELETE FROM kv_state WHERE key = ?", (k,))
            hist.append({"key": k[len("param_"):], "old": json.loads(v),
                         "new": None, "source": "poststop-reconcile",
                         "applied_at": now, "active": False,
                         "cleared_to_env": True,
                         "reason": "rewritten by the packaged backend during the "
                                   "2026-08-11 unattended run; .env is the only "
                                   "source of truth"})
        if p["params"]:
            _kv_set(c, "param_history", hist[-50:])

        for ph in p["phantom"]:
            # Deleted, not archived to closed_trades: nothing was ever bought,
            # so there is no trade to record. The broker order and its
            # cancellation are the history.
            c.execute("DELETE FROM open_trades WHERE symbol = ?", (ph["symbol"],))

        for f in p["fills"]:
            row = c.execute("SELECT extra FROM closed_trades WHERE id = ?",
                            (f["id"],)).fetchone()
            try:
                extra = json.loads(row["extra"]) if row and row["extra"] else {}
                if not isinstance(extra, dict):
                    extra = {"_original": extra}
            except (json.JSONDecodeError, TypeError):
                extra = {}
            extra["broker_corrected"] = {
                "at": now, "order_ids": f["order_ids"],
                "was": {"entry": f["entry"][0], "exit": f["exit"][0],
                        "pnl": f["pnl"][0]},
                "reason": "booked at limit prices; corrected to broker dealt "
                          "average prices",
            }
            pnl_pct = ((f["exit"][1] - f["entry"][1]) / f["entry"][1] * 100
                       if f["entry"][1] else 0)
            c.execute("UPDATE closed_trades SET entry=?, exit=?, pnl=?, "
                      "pnl_pct=?, extra=? WHERE id=?",
                      (f["entry"][1], f["exit"][1], f["pnl"][1],
                       round(pnl_pct, 4), json.dumps(extra, default=str), f["id"]))

        # Totals are recomputed from the corrected ledger, never adjusted by a delta.
        tot = 0.0
        for r in c.execute("SELECT pnl, extra FROM closed_trades"):
            try:
                extra = json.loads(r["extra"]) if r["extra"] else {}
            except (json.JSONDecodeError, TypeError):
                extra = {}
            if isinstance(extra, dict) and (extra.get("ledger_quality") or {}).get(
                    "excluded_from_performance"):
                continue
            tot += float(r["pnl"] or 0)
        _kv_set(c, "realized_pnl_total", round(tot, 4))
        if p["today"]:
            _kv_set(c, "realized_pnl_today", p["today"]["to"])

        entries.append({"version": VERSION, "at": now,
                        "params_cleared": [k for k, _ in p["params"]],
                        "phantoms_removed": [x["symbol"] for x in p["phantom"]],
                        "fills_corrected": [x["symbol"] for x in p["fills"]],
                        "realized_pnl_total": round(tot, 4),
                        "realized_pnl_today": (p["today"] or {}).get("to")})
        _kv_set(c, RECEIPT_KEY, entries)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise

    print(f"\n  applied {changes} change(s)")
    print(f"  realized_pnl_total -> {round(tot, 4)}")
    c.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--evidence", type=Path, default=None,
                    help="broker fill evidence (default: data/broker_evidence.json)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not a.db.exists():
        sys.exit(f"no such db: {a.db}")
    return run(a.db, apply=a.apply, evidence=a.evidence or EVIDENCE)


if __name__ == "__main__":
    sys.exit(main())
