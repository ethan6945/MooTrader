"""Which execution paths have REAL runtime evidence, and which are still claims.

    .venv/bin/python scripts/coverage_ledger.py [--home ~/MooTraderStaging]

WHY

  Twenty-three suites pass and every one of them feeds the code a fixture I
  wrote. That is worth something and it is not the same as the path having run.
  Twice now a path was green in the suites and broken in production — the
  position mirror the settler never wrote, and a live scan priced off a bar it
  had already been handed a fresher version of. Both were found by running, not
  by testing.

  So this reads the database and the logs and answers one question per path:
  has this actually happened, on a real account, and where is the record.

  It asserts nothing about whether the outcomes were GOOD. A stop-loss that
  fired is evidence the stop-loss path works, whatever it cost.

WHAT COUNTS AS EVIDENCE

  A row in the ledger, an order in a terminal state, or a log line emitted by
  the code path itself — never a line I could have written by hand into a test.
  Each finding is printed with the identifier you would use to go and look.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A row carried in from the previous schema is not evidence that THIS code
# ran. All 37 closed trades in the authoritative database arrived through the
# v3 -> v7 migration: migrated_from set, session_id null, written by an
# execution path that no longer exists in this form. Counting them reported
# stop-loss, take-profit and full-close as COVERED — 3/12 — when the current
# executor had never closed a position at all. The honest number was 0/12.
#
# The same principle the scale-out entry already states: a coverage tool that
# accepts configuration as evidence is worse than no coverage tool. History
# inherited from another program is the same mistake wearing a timestamp.
THIS_ERA = ("(coalesce(migrated_from,'') = '' "
            "AND coalesce(session_id,'') <> '')")


def q(db: Path, sql: str, args=()) -> list[tuple]:
    if not db.exists():
        return []
    c = sqlite3.connect(str(db))
    try:
        return list(c.execute(sql, args))
    except sqlite3.Error:
        return []
    finally:
        c.close()


def grep(log: Path, pattern: str, limit: int = 3) -> list[str]:
    if not log.exists():
        return []
    rx = re.compile(pattern, re.I)
    hits = []
    try:
        for line in log.read_text(errors="replace").splitlines():
            if rx.search(line):
                hits.append(line.strip()[:160])
    except OSError:
        return []
    return hits[-limit:]


def _default_home() -> Path:
    """Where the bot actually keeps its data — asked, not assumed.

    This defaulted to a hardcoded ~/MooTraderStaging. That directory was
    deleted when the repo became the home, and the ledger went on reporting
    "0/12 paths have real runtime evidence" against a path that did not
    exist. To a reader that is indistinguishable from "nothing has run yet",
    which is the one thing a coverage report must never be ambiguous about.
    Against the real home the same code said 3/12.
    """
    env = os.getenv("MMT_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return ROOT


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    home = Path(args.home).expanduser() if args.home else _default_home()
    db = home / "data" / "trader.db"
    log = home / "logs" / "trader.log"

    # Refuse rather than measure nothing and call it zero. An absent home is a
    # question about the invocation; an empty one is a fact about the bot.
    if not home.is_dir():
        sys.exit(f"coverage ledger: no such home {home}\n"
                 f"  Nothing was measured. This is NOT the same as 0/12.\n"
                 f"  Pass --home, or set MMT_HOME.")
    if not db.exists():
        sys.exit(f"coverage ledger: {home} has no data/trader.db\n"
                 f"  Nothing was measured. This is NOT the same as 0/12.\n"
                 f"  Is this the home the bot actually writes to?")

    paths = []

    def record(name, why, evidence, note=""):
        paths.append({"path": name, "covered": bool(evidence),
                      "evidence": evidence[:3], "why_it_matters": why,
                      "note": note})

    # ── the six the plan names ──────────────────────────────────────────
    record(
        "stop-loss exit",
        "under soft exits this is the only thing limiting a loss",
        [f"closed_trade #{r[0]} {r[1]} {r[2]} pnl {r[3]}"
         for r in q(db, "SELECT id,symbol,exit_reason,round(pnl,2) FROM "
                        "closed_trades WHERE " + THIS_ERA + " AND "
                        "upper(coalesce(exit_reason,'')) "
                        "LIKE '%SL%' ORDER BY ts DESC LIMIT 3")])

    record(
        "take-profit exit",
        "the other half of the ladder; shares the same code as the stop",
        # A FULL take-profit close, not a tranche: TP1/TP2 are scale-out and
        # are counted below. 'TP%' would have swept them in here and reported
        # both paths covered on one event.
        [f"closed_trade #{r[0]} {r[1]} {r[2]} pnl {r[3]}"
         for r in q(db, "SELECT id,symbol,exit_reason,round(pnl,2) FROM "
                        "closed_trades WHERE " + THIS_ERA + " AND "
                        "upper(coalesce(exit_reason,'')) "
                        "IN ('TP','TAKE_PROFIT','TP_BRACKET') "
                        "ORDER BY ts DESC LIMIT 3")])

    # USE_SCALE_OUT gates the tranche logic entirely. With it off this path
    # cannot be covered no matter how long the bot runs, and reporting it as
    # merely "not yet" invites someone to wait for something that will never
    # arrive.
    try:
        from src.config import settings as _s
        _scale_on = bool(getattr(_s, "use_scale_out", False))
    except Exception:
        _scale_on = None
    record(
        "scale-out / partial exit",
        "a position closed in pieces is where quantity bookkeeping goes wrong"
        + ("" if _scale_on else
           "  — NOTE: USE_SCALE_OUT is off, so this cannot occur at all until "
           "it is switched on; this is not a matter of waiting"),
        # Ledger and terminal state only. An earlier version of this also
        # grepped the log for "scale.?out", which matched the line
        # "soft stop ... (scale-out enabled)" — a statement that the FEATURE is
        # on, printed at every entry, and it reported the path covered before
        # a single tranche had ever sold. A coverage tool that accepts
        # configuration as evidence is worse than no coverage tool.
        [f"closed_trade #{r[0]} {r[1]} {r[2]} (tranche)"
         for r in q(db, "SELECT id,symbol,exit_reason FROM closed_trades WHERE "
                        + THIS_ERA + " AND "
                        "upper(coalesce(exit_reason,'')) IN ('TP1','TP2') "
                        "ORDER BY ts DESC LIMIT 3")]
        + [f"order {r[0][:12]} {r[1]} sold {r[2]} as {r[3]}"
           for r in q(db, "SELECT client_order_id,symbol,filled_qty,kind FROM "
                          "orders WHERE side='SELL' AND kind IN "
                          "('SCALE_OUT','PARTIAL','TP1','TP2') AND "
                          "filled_qty > 0 ORDER BY created_at DESC LIMIT 3")]
        + [f"open_trade {r[0]} half_closed=1" for r in
           q(db, "SELECT symbol FROM open_trades WHERE half_closed = 1")])

    record(
        "partial FILL",
        "the settler must apply the delta, not the request",
        [f"order {r[0][:12]} {r[1]} filled {r[2]} of {r[3]} requested"
         for r in q(db, "SELECT client_order_id,symbol,filled_qty,requested_qty "
                        "FROM orders WHERE filled_qty > 0 AND "
                        "filled_qty < requested_qty ORDER BY created_at DESC "
                        "LIMIT 3")],
        note="a paper account fills in full; this may only ever appear in REAL")

    record(
        "cancel failure",
        "a protective order that will not cancel is a live order nobody owns",
        # The code's own words on the failure path, not the word "cancel".
        grep(log, r"the broker refused the cancellation|could not cancel the "
                  r"orphaned|protective order cancel failed|cancel FAILED"))

    record(
        "late fill after restart",
        "shares that arrived while the process was gone",
        [f"order {r[0][:12]} {r[1]} recovered" for r in
         q(db, "SELECT client_order_id,symbol FROM orders WHERE "
               "extra LIKE '%late_fill%' ORDER BY created_at DESC LIMIT 3")]
        + grep(log, r"late first fill booked|filled while this software was "
                    r"not running"))

    # ── the ones the plan's acceptance criteria depend on ───────────────
    record(
        "cancel (successful)",
        "the ordinary path the failure case is measured against",
        [f"order {r[0][:12]} {r[1]} CANCELLED" for r in
         q(db, "SELECT client_order_id,symbol FROM orders WHERE "
               "state='CANCELLED' ORDER BY created_at DESC LIMIT 3")])

    record(
        "settlement of a real fill",
        "applied_qty moving because a broker filled something",
        [f"order {r[0][:12]} {r[1]} applied {r[2]}" for r in
         q(db, "SELECT client_order_id,symbol,applied_qty FROM orders WHERE "
               "applied_qty > 0 ORDER BY created_at DESC LIMIT 3")])

    record(
        "position fully closed (the mechanism, whatever triggered it)",
        "close -> settler -> closed_trades is the same chain a stop uses; only "
        "what PULLS it differs, so proving the chain and proving the trigger "
        "are two separate things",
        [f"closed_trade #{r[0]} {r[1]} {r[2]} qty {r[3]} pnl {r[4]}" for r in
         q(db, "SELECT id,symbol,exit_reason,qty,round(pnl,2) FROM "
               "closed_trades WHERE " + THIS_ERA + " ORDER BY ts DESC LIMIT 3")],
        note="a triggered SL/TP is tracked separately above and is NOT implied "
             "by this")

    record(
        "stack onto an existing position",
        "extending a position is where the quantity and average-price "
        "arithmetic lives, and 'no broker/local quantity drift' is an "
        "acceptance criterion",
        [f"order {r[0][:12]} {r[1]} +{r[2]} @ {r[3]}" for r in
         q(db, "SELECT client_order_id,symbol,filled_qty,round(avg_fill_price,2)"
               " FROM orders WHERE kind='STACK' AND filled_qty > 0 "
               "ORDER BY created_at DESC LIMIT 3")]
        + grep(log, r"STACK #\d+ on"))

    record(
        "halt raised by the code",
        "every acceptance criterion below assumes halts actually fire",
        grep(log, r"TRADING HALTED"))

    record(
        "supervised restart",
        "four to eight weeks of continuous running depends on it",
        grep(home / "logs" / "supervisor.log", r"restarted as"))

    # ── the invariants, stated as numbers rather than hopes ─────────────
    drift = q(db, "SELECT client_order_id,symbol,filled_qty,applied_qty FROM "
                  "orders WHERE applied_qty <> filled_qty AND state IN "
                  "('FILLED','CANCELLED','EXPIRED','REJECTED','FAILED_LOCAL')")
    unexplained = q(db, "SELECT client_order_id,symbol FROM orders WHERE "
                        "filled_qty > 0 AND applied_qty = 0")
    closed = q(db, "SELECT COUNT(*) FROM closed_trades WHERE " + THIS_ERA)
    inherited = q(db, "SELECT COUNT(*) FROM closed_trades WHERE NOT " + THIS_ERA)

    invariants = {
        "closed_trades": closed[0][0] if closed else 0,
        "closed_trades_inherited": inherited[0][0] if inherited else 0,
        "orders_with_applied_qty_mismatch": len(drift),
        "fills_never_applied": len(unexplained),
        "mismatch_detail": [f"{r[0][:12]} {r[1]} filled={r[2]} applied={r[3]}"
                            for r in drift[:5]],
    }

    covered = [p for p in paths if p["covered"]]
    print(f"\ncoverage ledger — {home}")
    print("=" * 66)
    if invariants["closed_trades_inherited"]:
        print(f"  {invariants['closed_trades_inherited']} closed trade(s) came "
              f"in through a schema migration and are NOT counted: they were\n"
              f"  written by the previous execution path, so they say nothing "
              f"about this one.\n")
    for p in paths:
        mark = "COVERED " if p["covered"] else "  not yet"
        print(f"  {mark}  {p['path']}")
        if p["covered"]:
            for e in p["evidence"]:
                print(f"              {e}")
        else:
            print(f"              why it matters: {p['why_it_matters']}")
            if p["note"]:
                print(f"              note: {p['note']}")
    print("=" * 66)
    print(f"  {len(covered)}/{len(paths)} paths have real runtime evidence")
    print(f"\n  closed trades so far:            {invariants['closed_trades']}")
    print(f"  applied_qty mismatches:          "
          f"{invariants['orders_with_applied_qty_mismatch']}   (must be 0)")
    print(f"  fills never applied:             "
          f"{invariants['fills_never_applied']}   (must be 0)")
    for d in invariants["mismatch_detail"]:
        print(f"      {d}")

    out = {"home": str(home), "paths": paths, "invariants": invariants,
           "covered": len(covered), "total": len(paths)}
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
