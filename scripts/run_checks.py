#!/usr/bin/env python3
"""Run every Phase-0 check. One command, no environment setup.

    .venv/bin/python scripts/run_checks.py           # everything
    .venv/bin/python scripts/run_checks.py -v        # show each check's output
    .venv/bin/python scripts/run_checks.py --only freeze redaction

Exit 0 = all green. Exit 1 = something failed; that is the CI signal.

WHY THIS EXISTS
  The suites each needed PYTHONPATH and a throwaway MMT_HOME set by hand, and
  had to be run one at a time. A check that is awkward to run is a check that
  stops being run. This sets both up itself and points the state-touching tests
  at a temporary directory, so running them can never write to data/trader.db.

WHAT IT COVERS
  test_param_freeze      — the guards themselves refuse writes
  test_freeze_callchains — the REAL entry points (approvals, hermes, auto_budget,
                           record_trade_close, the Flask endpoints) actually go
                           through those guards. Unit-testing one function with
                           five different `source` strings did not catch three
                           holes that this found.
  test_param_rollback    — the auto-rollback reads the log that is actually
                           being written. The journal moved to a file and two
                           readers did not follow, so for a week the rollback
                           scanned a frozen log and found nothing — which looks
                           exactly like a healthy account
  test_schedule_keys     — every job key main.py hands to cron_state resolves.
                           A rename that missed one table crash-looped the
                           trading loop for three days while all 30 other
                           checks stayed green — none of them starts the
                           scheduler
  test_sessions          — extended trading hours: the cache cannot be shared
                           across sessions, the fetch window is scaled to the
                           session's length, and regular hours does not move
  test_migrations        — every schema version this project has written can
                           still be upgraded to current, from real captured
                           DDL, with the data intact and no half-landed state
  test_forming_bar       — the live scan scores the freshest CLOSED bar: never
                           one that is still forming (look-ahead), never one
                           period staler than it needs to be
  test_protective_heartbeat — under soft exits the fast-stop loop IS the
                           protection; it must be possible to tell from the log
                           that it ran, and how close the nearest stop came
  test_exec_p0           — the seven execution P0s, each exercised through its
                           FAILURE: late first fill, settlement failure, fail-
                           closed startup, halts that midnight must not clear,
                           PnL rebuilt from the ledger, half-placed bracket,
                           broker identity before READY
  test_advisory_pin      — news and AI stay advisory: the veto is non-blocking
                           by default, the consult happens AFTER the order, and
                           no engine imports a scorer it cannot backtest
  test_shadow_eval       — the forward evaluation of the AI layer: a verdict is
                           joined to its OWN outcome, placeholders are not
                           counted as opinions, and too small a sample is
                           reported as such rather than as a result
  test_worker_cmd        — how the app launches its own worker. The frozen
                           shape takes --worker, not -m; getting it wrong
                           starts a second web server instead of a trading loop
  test_macos_parity      — the macOS app calls the endpoints this server still
                           serves, and its own Codable types decode what they
                           return. 0f6b502 deleted five signal-* routes; the
                           SwiftUI app kept calling all five for a week and
                           showed an empty panel instead of an error
  test_env_redaction     — no credential reaches a snapshot or a log line
  test_auto_budget       — compounding math, and the freeze over it
  config_baseline        — .env / .env.example / db / README / CODE DEFAULTS
                           agree; risk switches ship safe; DD peak is sane
  check_no_secrets       — nothing publishable contains a live credential

  config_baseline and check_no_secrets read the REAL repo state on purpose:
  they are about this machine's configuration, not about code behaviour, and a
  sandboxed copy would make them pass vacuously.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# name → (script, needs_sandboxed_home, ci_safe, timeout_seconds)
#
# ci_safe=False means the check reads THIS MACHINE's real configuration — the
# live .env, the values actually in use. Those are local checks by nature: on a
# runner there is no .env to compare against, so they would either fail or, far
# worse, pass vacuously and report "clean" about a file that was not there.
#
# Every suite carries a timeout. test_merge_concurrency spawns two processes
# that race for a SQLite write lock; if the lock logic regresses into a deadlock
# the suite does not fail, it hangs, and a hung CI job is an unattended one.
CHECKS = {
    "freeze":     ("scripts/test_param_freeze.py",       True,  True,  120),
    "callchains": ("scripts/test_freeze_callchains.py",  True,  True,  120),
    "identity":   ("scripts/test_identity.py",           True,  True,  120),
    "quadrants":  ("scripts/test_schema_quadrants.py",   True,  True,  300),
    "mergerace":  ("scripts/test_merge_concurrency.py",  True,  True,  180),
    "qualmig":    ("scripts/test_quality_migration.py",  True,  True,  180),
    "startproto": ("scripts/test_start_protocol.py",   True,  True,  420),
    "binding":    ("scripts/test_broker_binding.py",   True,  True,  120),
    "orderlog":   ("scripts/test_order_log.py",       True,  True,  180),
    "partials":   ("scripts/test_partial_fills.py",    True,  True,  240),
    "cancels":    ("scripts/test_protective_cancel.py", True,  True,  240),
    "recovery":   ("scripts/test_startup_recovery.py", True,  True,  240),
    "settler":    ("scripts/test_fill_settler.py",     True,  True,  240),
    "concentr":   ("scripts/test_concentration.py",   True,  True,  180),
    "rollback":   ("scripts/test_param_rollback.py",  True,  True,  120),
    "schedkeys":  ("scripts/test_schedule_keys.py",   True,  True,   60),
    "sessions":   ("scripts/test_sessions.py",           True,  True,  120),
    "migrations": ("scripts/test_migrations.py",         True,  True,  240),
    "formingbar": ("scripts/test_forming_bar.py",        True,  True,  120),
    "heartbeat":  ("scripts/test_protective_heartbeat.py", True, True,  120),
    "execp0":     ("scripts/test_exec_p0.py",            True,  True,  240),
    "advisory":   ("scripts/test_advisory_pin.py",       True,  True,  120),
    "shadow":     ("scripts/test_shadow_eval.py",        True,  True,  120),
    "workercmd":  ("scripts/test_worker_cmd.py",         True,  True,  120),
    "atr":        ("scripts/test_atr_agreement.py",       True,  True,  120),
    "setwrite":   ("scripts/test_setting_writes.py",     True,  True,  120),
    "websmoke":   ("scripts/test_web_smoke.py",         True,  True,  120),
    "macparity":  ("scripts/test_macos_parity.py",       True,  True,  240),
    "redaction":  ("scripts/test_env_redaction.py",      True,  True,  120),
    "budget":     ("scripts/test_auto_budget.py",        True,  True,  120),
    "baseline":   ("scripts/config_baseline.py",         False, False, 120),
    "secrets":    ("scripts/check_no_secrets.py",        False, False, 180),
}


def run_one(name: str, script: str, sandboxed: bool, tmp: Path,
            verbose: bool, timeout: int) -> tuple[bool, str]:
    # Blank credentials, so a suite that reaches a notify path CANNOT message
    # the owner. notifier.send() no-ops (and logs) with either of these empty.
    #
    # 2026-08-27: added after a test of /api/param-tune/apply — an endpoint that
    # notifies on a successful apply, correctly — put a dozen "你确认应用了
    # sl_atr_mult 3.5 → 3.0" messages on the owner's phone across its runs. The
    # writes themselves were stubbed and nothing changed; only the notification
    # was real, which is the worst shape for this bug: it looks exactly like the
    # bot acting on its own. This is the MMT_HOME sandbox below, applied to the
    # other thing a test can reach out and touch.
    env = dict(os.environ, PYTHONPATH=str(ROOT),
               TELEGRAM_TOKEN="", TELEGRAM_CHAT_ID="")
    if sandboxed:
        # A throwaway MMT_HOME so a test can never touch the live database.
        home = tmp / name
        (home / "data").mkdir(parents=True, exist_ok=True)
        (home / "logs").mkdir(parents=True, exist_ok=True)
        env["MMT_HOME"] = str(home)
    try:
        proc = subprocess.run([PY, script], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"TIMED OUT after {timeout}s — treat as a failure, not a flake"
    out = (proc.stdout + proc.stderr).strip()
    if verbose and out:
        print("\n".join("      " + ln for ln in out.splitlines()))
    # Prefer the suite's own summary line; fall back to the last line.
    summary = next((ln.strip() for ln in reversed(out.splitlines())
                    if "passed" in ln or "contradiction" in ln.lower()
                    or "clean" in ln.lower()), "")
    return proc.returncode == 0, summary or out.splitlines()[-1] if out else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print each check's full output")
    ap.add_argument("--only", nargs="+", metavar="NAME", choices=list(CHECKS),
                    help=f"run a subset: {', '.join(CHECKS)}")
    ap.add_argument("--ci", action="store_true",
                    help="only the suites that need no real .env, no OpenD and "
                         "no network")
    args = ap.parse_args()

    selected = args.only or [n for n, c in CHECKS.items()
                             if c[2] or not args.ci]
    results: list[tuple[str, bool, str]] = []

    mode = " (CI subset — isolated only)" if args.ci else ""
    print(f"Phase-0 checks — {len(selected)} suite(s){mode}\n")
    with tempfile.TemporaryDirectory(prefix="mmt-checks-") as td:
        tmp = Path(td)
        for name in selected:
            script, sandboxed, _ci, timeout = CHECKS[name]
            print(f"  {name:<12} ", end="", flush=True)
            ok, summary = run_one(name, script, sandboxed, tmp, args.verbose,
                                  timeout)
            print(("PASS  " if ok else "FAIL  ") + summary)
            results.append((name, ok, summary))

    failed = [n for n, ok, _ in results if not ok]
    print()
    if failed:
        print(f"{len(failed)} of {len(results)} failed: {', '.join(failed)}")
        print("Re-run one with output:  "
              f"{Path(PY).name} scripts/run_checks.py -v --only {failed[0]}")
        return 1
    print(f"all {len(results)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
