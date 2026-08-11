#!/usr/bin/env python3
"""Merge the repo-era ledger into the App ledger — one account, one history.

    .venv/bin/python scripts/merge_ledgers.py --dry-run     # show, change nothing
    .venv/bin/python scripts/merge_ledgers.py --apply

WHY THIS EXISTS
  The same paper account was recorded into two databases. The repo checkout has
  30 closed trades from 2026-06-17 to 2026-07-29; the packaged app has 6 from
  2026-08-03 to 2026-08-07 in
  ~/Library/Application Support/MooMooTrader/data/trader.db. The ranges do not
  overlap: the app took over from the repo and started a fresh file, so this is
  one continuous account split at the handover, not two accounts and not a
  duplicate.

  Split like that, every derived number is wrong. realized_pnl_total reads
  -$148.99 when the account is actually down $774.46; peak_equity is anchored to
  the wrong base; self_review and the win-rate stats see 6 trades where there
  are 36 — and the single largest winner in the account's history (SNDK, +$564)
  sits in the half nothing reads.

AUTHORITATIVE SIDE
  The app database. It is what the running process writes, so anything else is
  a copy that stopped being updated.

WHAT THIS DOES NOT DO
  It does not migrate the app database to schema v4. The running app is a frozen
  v2.4.0 backend that predates that schema; pointing it at a kv_state with a
  composite primary key would have it read global and account-scoped rows as
  duplicate keys. v4 lands when the app is rebuilt from current source.

PROVENANCE
  Imported rows are stamped in `extra` with the source database, the merge time,
  and an attribution confidence: 'exact' when the source row carried the field,
  'inferred' when it was reconstructed here. Nothing is silently invented.
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

REPO_DB = ROOT / "data" / "trader.db"
APP_DB = Path.home() / "Library/Application Support/MooMooTrader/data/trader.db"

MERGE_TAG = "repo-ledger-2026-06-17..2026-07-29"


def _rows(c, table):
    return [dict(r) for r in c.execute(f"SELECT * FROM {table}")]


def _cols(c, table):
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def _stamp(extra_json, source_db, confidence, now):
    """Record where a row came from and how much of it is original."""
    try:
        extra = json.loads(extra_json) if extra_json else {}
        if not isinstance(extra, dict):
            extra = {"_original": extra}
    except (json.JSONDecodeError, TypeError):
        extra = {"_original_raw": str(extra_json)}
    extra["merge_source"] = source_db
    extra["merge_tag"] = MERGE_TAG
    extra["merged_at"] = now
    extra["attribution_confidence"] = confidence
    return json.dumps(extra, default=str)


def batch_state(dst):
    """Has this batch already been imported?

    Without this the script is not safe to re-run. After a successful merge the
    target's earliest row IS an imported one, so the handover cutoff derived
    from MIN(ts) collapses to the start of the archive: every repo row then
    looks "post-handover", the trading-row guard fires a false conflict, and the
    PnL line adds the archive to a total that already contains it — reporting
    -$1,399.93 for an account that is down $774.46.

    Two independent signals, because either alone can be lost: the kv_state
    receipt, and merge_tag stamped into the imported rows themselves.
    """
    receipt = None
    try:
        row = dst.execute("SELECT value FROM kv_state WHERE key='ledger_merge'").fetchone()
        if row:
            receipt = json.loads(row["value"])
    except Exception:
        pass
    tagged = dst.execute(
        "SELECT COUNT(*) FROM closed_trades WHERE extra LIKE ?",
        (f"%{MERGE_TAG}%",)).fetchone()[0]
    return {
        "applied": bool((receipt or {}).get("tag") == MERGE_TAG) or tagged > 0,
        "receipt": receipt, "tagged_rows": tagged,
    }


def plan():
    if not REPO_DB.exists():
        sys.exit(f"repo db missing: {REPO_DB}")
    if not APP_DB.exists():
        sys.exit(f"app db missing: {APP_DB}")

    src = sqlite3.connect(f"file:{REPO_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(f"file:{APP_DB}?mode=ro", uri=True)
    dst.row_factory = sqlite3.Row

    out = {"tables": {}, "conflicts": [], "excluded": {},
           "batch": batch_state(dst)}
    if out["batch"]["applied"]:
        # Nothing further to compute: every derived figure below assumes the
        # target does not already contain the source.
        out["pnl"] = {
            "merged": round(dst.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM closed_trades").fetchone()[0], 2)}
        out["rows"] = dst.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
        src.close(); dst.close()
        return out

    for table, tskey in (("closed_trades", "ts"), ("audit", "ts"),
                         ("history", "ts")):
        s_rows = _rows(src, table)
        d_rows = _rows(dst, table)
        s_max = max((r[tskey] for r in s_rows), default=None)
        d_min = min((r[tskey] for r in d_rows), default=None)

        # The handover is where the app's ledger starts. A repo row at or after
        # that point was written to the archive AFTER the app took over — it is
        # not part of the continuous history and importing it would duplicate or
        # reorder the app's own record.
        cutoff = d_min
        after = [r for r in s_rows if cutoff and r[tskey] >= cutoff]
        out["tables"][table] = {
            "repo": len(s_rows), "app": len(d_rows),
            "repo_range": (min((r[tskey] for r in s_rows), default=None), s_max),
            "app_range": (d_min, max((r[tskey] for r in d_rows), default=None)),
            "cutoff": cutoff, "excluded": len(after),
        }
        if after:
            # Maintenance rows (a budget change recorded on the archive) are
            # fine to drop. A TRADING row after the handover would mean both
            # instances were live at once, which no merge should paper over.
            trading = [r for r in after
                       if r.get("action") in ("buy", "sell", "error")
                       or (r.get("symbol") and r.get("action") != "budget_change")]
            out["excluded"][table] = [
                f"{r[tskey][:19]} {r.get('action', '')} {r.get('symbol') or ''} "
                f"{(r.get('reason') or '')[:50]}".strip() for r in after[:10]]
            if trading:
                out["conflicts"].append(
                    f"{table}: {len(trading)} TRADING row(s) in the repo db after "
                    f"the app took over at {cutoff} — both instances appear to "
                    f"have been live, which this merge cannot resolve")

    # open_trades is keyed by symbol; a symbol held in both is a real conflict.
    # Positions are never imported — see apply_merge. Report both sides so the
    # decision is visible rather than implicit.
    s_open = {r["symbol"] for r in _rows(src, "open_trades")}
    d_open = {r["symbol"] for r in _rows(dst, "open_trades")}
    out["open_repo"], out["open_app"] = sorted(s_open), sorted(d_open)
    closed_later = [
        r["symbol"] for r in dst.execute(
            "SELECT DISTINCT symbol FROM closed_trades")
        if r["symbol"] in s_open]
    out["repo_open_closed_by_app"] = sorted(set(closed_later))

    s_pnl = src.execute("SELECT COALESCE(SUM(pnl),0) FROM closed_trades").fetchone()[0]
    d_pnl = dst.execute("SELECT COALESCE(SUM(pnl),0) FROM closed_trades").fetchone()[0]
    out["pnl"] = {"repo": round(s_pnl, 2), "app": round(d_pnl, 2),
                  "merged": round(s_pnl + d_pnl, 2)}
    src.close(); dst.close()
    return out


def apply_merge():
    now = datetime.now(timezone.utc).isoformat()
    src = sqlite3.connect(f"file:{REPO_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(APP_DB))
    dst.row_factory = sqlite3.Row
    moved = {}

    try:
        # Re-check inside the write path, not just in plan(): two operators, or
        # a retry after a partial failure, must not both get past the check.
        st = batch_state(dst)
        if st["applied"]:
            raise RuntimeError(
                f"batch {MERGE_TAG!r} already applied "
                f"({st['tagged_rows']} tagged rows) — refusing to double-import")
        dst.execute("BEGIN IMMEDIATE")
        for table in ("closed_trades", "audit", "history"):
            s_cols = set(_cols(src, table))
            d_cols = _cols(dst, table)
            # Only columns the destination actually has: the repo db is v4 and
            # the app db is v3, and the identity columns must NOT be forced into
            # a schema whose code cannot read them.
            use = [c for c in d_cols if c in s_cols and c != "id"]
            has_extra = "extra" in use
            placeholders = ",".join("?" for _ in use)
            # Same cutoff the plan reported: import only what predates the app
            # taking over, so nothing duplicates or reorders the app's record.
            row = dst.execute(f"SELECT MIN(ts) FROM {table}").fetchone()
            cutoff = row[0] if row else None
            n = skipped = 0
            for r in src.execute(f"SELECT * FROM {table}"):
                if cutoff and r["ts"] >= cutoff:
                    skipped += 1
                    continue
                vals = []
                for c in use:
                    v = r[c]
                    if c == "extra" and has_extra:
                        v = _stamp(v, "repo/data/trader.db", "exact", now)
                    vals.append(v)
                dst.execute(
                    f"INSERT INTO {table} ({','.join(use)}) VALUES ({placeholders})",
                    vals)
                n += 1
            moved[table] = n
            if skipped:
                moved[f"{table}_skipped_post_handover"] = skipped

        # Positions are deliberately NOT imported.
        #
        # closed_trades is history and merges cleanly. open_trades is live state,
        # and the archive's copy is a snapshot frozen at the handover. Here it
        # still lists FTNT qty=10 and DDOG qty=2 — both of which the app went on
        # to close at a profit (FTNT 08-03 TP +$100.10, DDOG 08-04 TP +$33.55,
        # quantities matching exactly). Importing them would conjure two
        # positions the account does not hold, and the executor would then try
        # to manage stops for them.
        #
        # The authoritative side's open_trades is the answer about what is held,
        # and the broker outranks even that — reconcile() already treats it as
        # the source of truth and adopts or quarantines anything that disagrees.
        moved["open_trades_skipped_stale"] = src.execute(
            "SELECT COUNT(*) FROM open_trades").fetchone()[0]

        # Realized PnL and the drawdown peak must be recomputed from the MERGED
        # ledger — that is the entire point. peak = max(base, base+realized),
        # the one formula risk_manager.compute_peak_equity uses.
        total = dst.execute("SELECT COALESCE(SUM(pnl),0) FROM closed_trades").fetchone()[0]
        row = dst.execute("SELECT value FROM kv_state WHERE key='budget_usd'").fetchone()
        budget = float(json.loads(row["value"])) if row else 10000.0
        peak = max(budget, budget + total, 1.0)
        for k, v in (("realized_pnl_total", total), ("peak_equity", peak)):
            dst.execute("INSERT OR REPLACE INTO kv_state (key, value) VALUES (?,?)",
                        (k, json.dumps(v)))
        dst.execute("INSERT OR REPLACE INTO kv_state (key, value) VALUES (?,?)",
                    ("ledger_merge", json.dumps({
                        "tag": MERGE_TAG, "merged_at": now,
                        "source": "repo/data/trader.db", "rows": moved,
                        "realized_pnl_total": round(total, 2),
                        "peak_equity": round(peak, 2)}, default=str)))
        dst.execute("COMMIT")
    except Exception:
        dst.execute("ROLLBACK")
        raise
    finally:
        src.close(); dst.close()
    return moved, total, peak


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    p = plan()
    print(f"source (repo) : {REPO_DB}")
    print(f"target (app)  : {APP_DB}\n")

    if p["batch"]["applied"]:
        r = p["batch"]["receipt"] or {}
        print(f"  batch {MERGE_TAG!r} is ALREADY applied.")
        print(f"    receipt   : merged_at {r.get('merged_at', '?')}")
        print(f"    taggedrows: {p['batch']['tagged_rows']} carry merge_tag")
        print(f"    ledger now: {p['rows']} trades, ${p['pnl']['merged']}")
        print("\n  Nothing to do. Re-importing would double-count; the archive "
              "is already inside this ledger.")
        return 0

    for t, info in p["tables"].items():
        print(f"  {t:<15} repo {info['repo']:>5}  app {info['app']:>5}   "
              f"repo {str(info['repo_range'][0])[:10]}→{str(info['repo_range'][1])[:10]}  "
              f"app {str(info['app_range'][0])[:10]}→{str(info['app_range'][1])[:10]}"
              + (f"   import {info['repo'] - info['excluded']}"
                 f" (skip {info['excluded']} post-handover)"
                 if info["excluded"] else ""))
    if p["excluded"]:
        print("\n  excluded as post-handover (written to the archive after the "
              "app took over):")
        for t, rows in p["excluded"].items():
            for line in rows:
                print(f"    {t}: {line}")
    print(f"\n  open positions  repo={p['open_repo']}  app={p['open_app']}")
    print("  positions are NOT imported — the archive's copy is a handover-time "
          "snapshot;")
    if p["repo_open_closed_by_app"]:
        print(f"  {p['repo_open_closed_by_app']} were closed by the app afterwards, "
              f"so importing them would create phantom holdings.")
    print(f"  realized PnL    repo ${p['pnl']['repo']}  app ${p['pnl']['app']}  "
          f"-> merged ${p['pnl']['merged']}")

    if p["conflicts"]:
        print("\nCONFLICTS — not safe to merge:")
        for c in p["conflicts"]:
            print(f"  · {c}")
        return 1

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0

    moved, total, peak = apply_merge()
    print("\nmerged:")
    for t, n in moved.items():
        print(f"  {t:<15} +{n}")
    print(f"  realized_pnl_total -> ${total:,.2f}")
    print(f"  peak_equity        -> ${peak:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
