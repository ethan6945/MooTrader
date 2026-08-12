#!/usr/bin/env python3
"""Regenerate the legacy JSON mirrors from the database.

    .venv/bin/python scripts/rebuild_json_mirrors.py --db <path> --dry-run
    .venv/bin/python scripts/rebuild_json_mirrors.py --db <path> --apply

One direction only: database -> JSON. Never the reverse.

WHY
  open_trades.json, state.json and account.json predate SQLite and are still
  written for the GUI and for anything that has not been moved over. They are
  copies, and a copy that nobody refreshes becomes a second, contradictory
  answer to "what do we hold".

  On 2026-08-11 that is exactly what happened: the database was corrected to
  remove a position whose order never filled, and open_trades.json went on
  listing HPE 64 for hours. state.json and account.json still carried the
  pre-correction PnL. Any code that fell back to a mirror — reconcile did, on a
  SQLite error — would have compared the broker against a holding that never
  existed.

  So the mirrors are rebuilt from the database here, and the two readers that
  could resurrect them have been pointed at the database instead.

REFUSES TO RUN BACKWARDS
  If the JSON holds a position the database does not, that is not a merge
  candidate. The database is authoritative; the mirror is stale by definition.
  This reports the difference and overwrites it, rather than offering to
  reconcile in the wrong direction.
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


def _kv(c) -> dict:
    scoped = any(r[1] == "account_id"
                 for r in c.execute("PRAGMA table_info(kv_state)"))
    out = {}
    q = ("SELECT key, value FROM kv_state ORDER BY account_id <> '' ASC"
         if scoped else "SELECT key, value FROM kv_state")
    for r in c.execute(q):
        try:
            out[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            out[r["key"]] = r["value"]
    return out


def build(c) -> dict[str, object]:
    """The three mirrors, derived entirely from the database."""
    state = _kv(c)

    open_trades = {}
    for r in c.execute("SELECT * FROM open_trades"):
        d = dict(r)
        extra = d.pop("extra", None)
        if extra:
            try:
                merged = json.loads(extra)
                if isinstance(merged, dict):
                    for k, v in merged.items():
                        d.setdefault(k, v)
            except (json.JSONDecodeError, TypeError):
                pass
        # Identity columns are a v4 concept the legacy consumers do not know.
        for k in ("account_id", "opened_session_id"):
            d.pop(k, None)
        open_trades[d["symbol"]] = d

    # account.json: refresh only the fields the database actually owns. Live
    # cash and market values come from the broker during a scan and are left
    # as-is rather than invented here.
    acct_path = ROOT / "data" / "account.json"
    account = {}
    for candidate in (acct_path,):
        if candidate.exists():
            try:
                account = json.loads(candidate.read_text())
            except (json.JSONDecodeError, OSError):
                account = {}
    realized = float(state.get("realized_pnl_total") or 0.0)
    account.update({
        "realized_pnl_total": realized,
        "realized_pnl_today": float(state.get("realized_pnl_today") or 0.0),
        "positions_count": len(open_trades),
        "symbols": sorted(open_trades),
        "budget": float(state.get("budget_usd") or 0.0),
        "budget_usd": float(state.get("budget_usd") or 0.0),
        "total_pnl": round(realized + float(account.get("unrealized_pnl") or 0.0), 4),
        "mirror_rebuilt_at": datetime.now(timezone.utc).isoformat(),
        "mirror_source": "database",
    })
    return {"open_trades.json": open_trades, "state.json": state,
            "account.json": account}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None,
                    help="directory for the mirrors (default: the db's own)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not a.db.exists():
        sys.exit(f"no such db: {a.db}")
    out_dir = a.out or a.db.parent

    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    mirrors = build(c)
    c.close()

    print(f"  db : {a.db}")
    print(f"  out: {out_dir}\n")
    drift = 0
    for name, data in mirrors.items():
        p = out_dir / name
        old = {}
        if p.exists():
            try:
                old = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                old = {}
        if name == "open_trades.json":
            was, now = sorted(old), sorted(data)
            ghosts = [s for s in was if s not in now]
            print(f"  {name:<18} was {was or '[]'} -> {now or '[]'}"
                  + (f"   removing phantom {ghosts}" if ghosts else ""))
            if was != now:
                drift += 1
        else:
            keys = ("realized_pnl_total", "realized_pnl_today")
            changed = [k for k in keys if old.get(k) != data.get(k)]
            print(f"  {name:<18} " + ("  ".join(
                f"{k}: {old.get(k)} -> {data.get(k)}" for k in changed)
                if changed else "already current"))
            if changed:
                drift += 1
        if a.apply:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2, default=str))

    if not a.apply:
        print(f"\n  {drift} mirror(s) stale — dry run, nothing written")
    else:
        print(f"\n  rebuilt {len(mirrors)} mirror(s) from the database")
    return 0


if __name__ == "__main__":
    sys.exit(main())
