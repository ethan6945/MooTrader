#!/usr/bin/env python3
"""Ledger-quality migration: mark non-trades, recompute derived state, close
stale param history. Idempotent and audited.

    .venv/bin/python scripts/migrate_ledger_quality.py --db <path> --dry-run
    .venv/bin/python scripts/migrate_ledger_quality.py --db <path> --apply

WHY A MIGRATION AND NOT A ONE-OFF
  The first pass at this was typed straight at the database. That is fine right
  up until the same cleanup has to be applied to a second copy — the migration
  rehearsal on a clone, or the app database after a rebuild — and nobody can say
  exactly what was done to the first one. Every step here is declared, checked
  before it is applied, and recorded with a receipt so a second run is a no-op
  that says so.

WHAT IT DOES
  1. Marks records that are in the ledger but are not trades:
       · a synthetic TST row (entry 100.00 / exit 90.00 / qty 10)
       · the second of two identical MRK closes, booked 4 seconds apart
     Marked, never deleted — the raw ledger stays the record of what happened.
     db.closed_trades() filters them from every performance and risk consumer.

  2. Recomputes realized_pnl_total from the EFFECTIVE ledger. It feeds
     current_drawdown_pct(), so a double-counted close there is a risk decision
     made on a trade that did not happen, and peak_equity is re-derived with it
     through the one formula in risk_manager.compute_peak_equity.

  3. Closes param_history records still flagged active after their param_*
     override was cleared. The autopilot's rollback reads that flag, so a stale
     "active" points it at a value nothing is running.
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

RECEIPT_KEY = "ledger_quality_migration"
VERSION = 1


def _find_targets(c) -> list[tuple[int, str, str]]:
    """Locate the rows to mark BY THEIR PROPERTIES, not by hardcoded id.

    Ids differ between the app database and any clone of it made before the
    merge, so an id list would silently mark the wrong rows there.
    """
    out = []
    for r in c.execute(
            "SELECT id, symbol, qty, entry, exit FROM closed_trades "
            "WHERE symbol = 'TST'"):
        out.append((r["id"], "test_record",
                    f"symbol TST, entry {r['entry']:.2f} / exit {r['exit']:.2f} / "
                    f"qty {r['qty']} — a synthetic record from testing, never a "
                    f"broker fill"))

    # Exact duplicates: same symbol, opened_at, qty, entry, exit and pnl. Keep
    # the earliest row of each group, mark the rest.
    for g in c.execute("""
            SELECT symbol, opened_at, qty, entry, exit, ROUND(pnl, 4) p,
                   COUNT(*) n, MIN(id) keep, GROUP_CONCAT(id) ids
            FROM closed_trades
            GROUP BY symbol, opened_at, qty, entry, exit, ROUND(pnl, 4)
            HAVING n > 1"""):
        for i in [int(x) for x in g["ids"].split(",") if int(x) != g["keep"]]:
            out.append((i, "duplicate",
                        f"identical to id={g['keep']} on symbol, opened_at, qty, "
                        f"entry, exit and pnl — one close booked twice"))
    return out


def _mark(c, tid: int, cls: str, why: str, now: str) -> bool:
    row = c.execute("SELECT extra FROM closed_trades WHERE id = ?", (tid,)).fetchone()
    try:
        extra = json.loads(row["extra"]) if row and row["extra"] else {}
        if not isinstance(extra, dict):
            extra = {"_original": extra}
    except (json.JSONDecodeError, TypeError):
        extra = {}
    if (extra.get("ledger_quality") or {}).get("excluded_from_performance"):
        return False        # already marked — idempotent
    extra["ledger_quality"] = {
        "excluded_from_performance": True, "class": cls, "reason": why,
        "marked_at": now, "migration_version": VERSION,
    }
    c.execute("UPDATE closed_trades SET extra = ? WHERE id = ?",
              (json.dumps(extra, default=str), tid))
    return True


def _effective_total(c) -> float:
    tot = 0.0
    for r in c.execute("SELECT pnl, extra FROM closed_trades"):
        try:
            extra = json.loads(r["extra"]) if r["extra"] else {}
        except (json.JSONDecodeError, TypeError):
            extra = {}
        if isinstance(extra, dict) and \
                (extra.get("ledger_quality") or {}).get("excluded_from_performance"):
            continue
        tot += float(r["pnl"] or 0)
    return round(tot, 2)


def _kv(c, key, default=None):
    r = c.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except (json.JSONDecodeError, TypeError):
        return r["value"]


def run(db_path: Path, apply: bool) -> int:
    c = sqlite3.connect(str(db_path) if apply else f"file:{db_path}?mode=ro",
                        uri=not apply)
    c.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    prior = _kv(c, RECEIPT_KEY)
    if prior and prior.get("version") == VERSION:
        print(f"  migration v{VERSION} already applied at {prior.get('at')}")
        print(f"    marked {prior.get('marked')} row(s), "
              f"realized_pnl_total -> {prior.get('realized_pnl_total')}")

    targets = _find_targets(c)
    raw_total = round(c.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM closed_trades").fetchone()[0], 2)

    print(f"\n  db: {db_path}")
    print(f"  rows to mark: {len(targets)}")
    for tid, cls, why in targets:
        r = c.execute("SELECT symbol, ts, pnl FROM closed_trades WHERE id=?",
                      (tid,)).fetchone()
        print(f"    id={tid:<4} {r['symbol']:<6} {r['ts'][:19]} "
              f"${r['pnl']:>8.2f}  [{cls}]")

    if not apply:
        # Simulate to show the resulting numbers without writing.
        would = _effective_total(c)
        excl = {t[0] for t in targets}
        sim = round(sum(float(r["pnl"] or 0) for r in
                        c.execute("SELECT id, pnl, extra FROM closed_trades")
                        if r["id"] not in excl and not _is_marked(r)), 2)
        print(f"\n  realized_pnl_total: raw {raw_total} -> effective {sim}")
        print("  dry run — nothing written")
        c.close()
        return 0

    c.execute("BEGIN IMMEDIATE")
    try:
        marked = sum(1 for tid, cls, why in targets if _mark(c, tid, cls, why, now))

        eff = _effective_total(c)
        budget = float(_kv(c, "budget_usd") or 0) or None
        from src.risk_manager import compute_peak_equity
        base = float(_kv(c, "auto_budget_seed") or 0) or budget
        peak = compute_peak_equity(base, eff, prior_peak=0.0) if base else None

        c.execute("INSERT OR REPLACE INTO kv_state (key,value) VALUES (?,?)",
                  ("realized_pnl_total", json.dumps(eff)))
        if peak is not None:
            c.execute("INSERT OR REPLACE INTO kv_state (key,value) VALUES (?,?)",
                      ("peak_equity", json.dumps(peak)))

        # Close param_history entries still flagged active whose override is gone.
        hist = _kv(c, "param_history", []) or []
        live = {r["key"][len("param_"):] for r in c.execute(
            "SELECT key FROM kv_state WHERE key LIKE 'param%' AND key != 'param_history'")}
        closed = 0
        for h in hist:
            if h.get("active") and h.get("key") not in live:
                h["active"] = False
                h["closed_by"] = "ledger-quality migration: override cleared"
                h["closed_at"] = now
                closed += 1
        c.execute("INSERT OR REPLACE INTO kv_state (key,value) VALUES (?,?)",
                  ("param_history", json.dumps(hist[-50:], default=str)))

        c.execute("INSERT OR REPLACE INTO kv_state (key,value) VALUES (?,?)",
                  (RECEIPT_KEY, json.dumps({
                      "version": VERSION, "at": now, "marked": marked,
                      "raw_total": raw_total, "realized_pnl_total": eff,
                      "peak_equity": peak, "param_history_closed": closed},
                      default=str)))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise

    print(f"\n  marked {marked} new row(s) ({len(targets)-marked} already marked)")
    print(f"  realized_pnl_total: {raw_total} -> {eff}")
    print(f"  peak_equity       : {peak}")
    print(f"  param_history closed: {closed}")
    c.close()
    return 0


def _is_marked(row) -> bool:
    try:
        e = json.loads(row["extra"]) if row["extra"] else {}
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(isinstance(e, dict)
                and (e.get("ledger_quality") or {}).get("excluded_from_performance"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, type=Path)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not a.db.exists():
        sys.exit(f"no such db: {a.db}")
    return run(a.db, apply=a.apply)


if __name__ == "__main__":
    sys.exit(main())
