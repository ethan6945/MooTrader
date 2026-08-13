#!/usr/bin/env python3
"""Build and run an isolated staging installation. Places no orders, ever.

    .venv/bin/python scripts/staging.py init      # create ~/MooTraderStaging
    .venv/bin/python scripts/staging.py start     # start a worker, no orders
    .venv/bin/python scripts/staging.py status
    .venv/bin/python scripts/staging.py stop

WHAT IS ISOLATED, AND WHAT IS NOT

  Isolated: MMT_HOME, the database, the logs, the lease, the web port. Nothing
  staging does can reach the authoritative installation's files.

  NOT isolated: OpenD. There is one gateway on 127.0.0.1:11111 and behind it one
  moomoo paper account — the same one the authoritative ledger describes. A
  staging worker that placed an order would place it in that account, and the
  authoritative database would know nothing about it. At the next reconcile the
  position would appear as a phantom holding nobody could account for.

  That is why staging runs with the order gate shut rather than merely pointed
  somewhere else. Separate files are not separation when the broker is shared;
  the only real boundary is not sending the order.

WHAT THIS IS FOR
  Exercising Start -> GO -> session -> lease -> Stop against the real src/main.py
  and a real OpenD connection, which is the one thing the test suites cannot do.
  It is not for evaluating the strategy.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING_HOME = Path(os.environ.get("MMT_STAGING_HOME",
                                   Path.home() / "MooTraderStaging"))
STAGING_PORT = "8771"          # the authoritative web panel is on 8770

# Everything staging must NOT inherit from the real .env, with the value it gets
# instead. Anything that could reach the outside world is blanked: a staging run
# that sent a Telegram message would be indistinguishable from the real one to
# whoever read it.
STAGING_OVERRIDES = {
    "MOO_TRADE_ENV": "SIMULATE",
    "WEB_PORT": STAGING_PORT,
    "PARAMS_FROZEN": "true",
    "AUTO_APPLY_PARAMS": "false",
    "AUTO_BUDGET_ENABLED": "false",
    "MAX_POSITIONS_AUTOSCALE": "false",
    "TELEGRAM_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
}
# Keys never copied at all. Credentials a staging run has no business holding.
STAGING_DROP = ("DEEPSEEK_API_KEY", "TAVILY_API_KEY", "GEMINI_API_KEY",
                "FINNHUB_KEY", "OPENAI_API_KEY", "WEB_PASSWORD",
                "MOO_TRADE_PWD")


def _staging_env() -> dict:
    """The environment a staging command runs under."""
    env = dict(os.environ)
    env["MMT_HOME"] = str(STAGING_HOME)
    env["PYTHONPATH"] = str(ROOT)
    env.pop("MMT_ALLOW_SCHEMA_UPGRADE", None)
    return env


def cmd_init(args) -> int:
    if STAGING_HOME.exists() and not args.force:
        print(f"{STAGING_HOME} already exists — pass --force to rebuild it")
        return 1
    if STAGING_HOME.exists():
        shutil.rmtree(STAGING_HOME)
    (STAGING_HOME / "data").mkdir(parents=True)
    (STAGING_HOME / "logs").mkdir(parents=True)

    # The .env is DERIVED from the real one rather than copied: staging must
    # match production's risk settings (otherwise it validates nothing) while
    # holding none of its credentials and reaching none of its channels.
    src_env = ROOT / ".env"
    lines, seen = [], set()
    if src_env.exists():
        for raw in src_env.read_text().splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k = s.split("=", 1)[0].strip()
            if k in STAGING_DROP:
                continue
            seen.add(k)
            lines.append(f"{k}={STAGING_OVERRIDES.get(k, s.split('=', 1)[1].strip())}")
    for k, v in STAGING_OVERRIDES.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    (STAGING_HOME / ".env").write_text("\n".join(sorted(lines)) + "\n")
    os.chmod(STAGING_HOME / ".env", 0o600)

    # A fresh database at the current schema. Never a copy of the authoritative
    # one: staging writing into a copy of real history produces a ledger that
    # looks real and is not, and the two become easy to confuse in a backup.
    r = subprocess.run(
        [sys.executable, "-c",
         "from src import db, identity; db._ensure_initialised(); "
         "print(identity.get_or_create_account('SIMULATE'))"],
        cwd=ROOT, env=dict(_staging_env(), MMT_ALLOW_SCHEMA_UPGRADE="1"),
        capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr.strip()[-800:])
        return 1

    print(f"staging home : {STAGING_HOME}")
    print(f"database     : {STAGING_HOME / 'data' / 'trader.db'} (fresh)")
    print(f"web port     : {STAGING_PORT}")
    print(f"account      : {r.stdout.strip().splitlines()[-1]}")
    print("\norders       : PERMANENTLY DISABLED for this installation")
    print("OpenD        : SHARED with the authoritative install — which is why")
    print("               the order gate, not the file layout, is the boundary")
    return 0


def cmd_start(args) -> int:
    if not (STAGING_HOME / ".env").exists():
        print(f"no staging installation at {STAGING_HOME} — run `init` first")
        return 1
    r = subprocess.run(
        [sys.executable, "-c",
         "import json;from src import start_protocol as sp;"
         "print(json.dumps(sp.start('cli', allow_orders=False)))"],
        cwd=ROOT, env=_staging_env(), capture_output=True, text=True)
    out = [l for l in r.stdout.splitlines() if l.startswith("{")]
    if r.returncode != 0 or not out:
        print((r.stderr.strip() or r.stdout.strip())[-800:])
        return 1
    res = json.loads(out[0])
    print(f"worker pid   : {res['pid']}")
    print(f"session      : {res.get('session_id')}")
    print(f"fence        : {res.get('fence')}")
    print("orders       : DENIED (the GO withheld the trading grant)")
    return 0


def cmd_stop(args) -> int:
    r = subprocess.run(
        [sys.executable, "-c",
         "import json;from src import start_protocol as sp;"
         "print(json.dumps(sp.stop('cli')))"],
        cwd=ROOT, env=_staging_env(), capture_output=True, text=True)
    print((r.stdout or r.stderr).strip()[-800:])
    return r.returncode


def cmd_status(args) -> int:
    r = subprocess.run(
        [sys.executable, "-c", """
import json
from src import start_lease, identity, db
lease = start_lease.read() or {}
try:
    sessions = identity.open_sessions()
except Exception:
    sessions = []
print(json.dumps({"lease": {k: lease.get(k) for k in ("pid","fence","host")},
                  "open_sessions": len(sessions),
                  "positions": len(db.load_open_trades())}, indent=2))
"""], cwd=ROOT, env=_staging_env(), capture_output=True, text=True)
    print((r.stdout or r.stderr).strip()[-1500:])
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init"); p_init.add_argument("--force", action="store_true")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    a = ap.parse_args()
    return {"init": cmd_init, "start": cmd_start,
            "stop": cmd_stop, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
