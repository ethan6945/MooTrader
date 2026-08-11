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

# name → (script, needs_sandboxed_home)
CHECKS = {
    "freeze":     ("scripts/test_param_freeze.py", True),
    "callchains": ("scripts/test_freeze_callchains.py", True),
    "identity":   ("scripts/test_identity.py", True),
    "mergerace":  ("scripts/test_merge_concurrency.py", True),
    "redaction":  ("scripts/test_env_redaction.py", True),
    "budget":     ("scripts/test_auto_budget.py", True),
    "baseline":   ("scripts/config_baseline.py", False),
    "secrets":    ("scripts/check_no_secrets.py", False),
}


def run_one(name: str, script: str, sandboxed: bool, tmp: Path,
            verbose: bool) -> tuple[bool, str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    if sandboxed:
        # A throwaway MMT_HOME so a test can never touch the live database.
        home = tmp / name
        (home / "data").mkdir(parents=True, exist_ok=True)
        (home / "logs").mkdir(parents=True, exist_ok=True)
        env["MMT_HOME"] = str(home)
    proc = subprocess.run([PY, script], cwd=ROOT, env=env,
                          capture_output=True, text=True)
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
    args = ap.parse_args()

    selected = args.only or list(CHECKS)
    results: list[tuple[str, bool, str]] = []

    print(f"Phase-0 checks — {len(selected)} suite(s)\n")
    with tempfile.TemporaryDirectory(prefix="mmt-checks-") as td:
        tmp = Path(td)
        for name in selected:
            script, sandboxed = CHECKS[name]
            print(f"  {name:<12} ", end="", flush=True)
            ok, summary = run_one(name, script, sandboxed, tmp, args.verbose)
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
