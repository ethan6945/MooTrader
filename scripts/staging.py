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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
}
# Which keys are dropped is decided BY PATTERN, not by a list of names.
#
# The first version of this listed them exactly, and immediately proved why
# that does not work: it named GEMINI_API_KEY and the real file holds
# GEMINI_API_KEYS, so a live 79-character key was copied into the staging .env.
# WEB_SECRET and OPEND_LOGIN_ACCOUNT were not on the list at all. An exact-name
# list is a list of the credentials someone remembered.
#
# src/hermes_improve.py already classifies these by substring for the snapshot
# redaction, and its is_identifier_key docstring names OPEND_LOGIN_ACCOUNT as
# the reason it exists. Reusing it means there is one answer to "is this
# sensitive?" rather than a third list to keep in sync.
from src.hermes_improve import is_identifier_key, is_secret_key   # noqa: E402


def _is_sensitive(key: str) -> bool:
    return is_secret_key(key) or is_identifier_key(key)


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

    # config/ is copied, not left empty. The first staging run died in
    # load_watchlist() on a missing config/watchlist.json — before it reached
    # OpenD, so the run proved the handshake and nothing about the broker.
    # These files are watchlists and universe pools: they carry no credentials
    # and staging must score the same names as production or it is exercising a
    # different bot.
    cfg = STAGING_HOME / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sorted((ROOT / "config").glob("*.json")):
        if src.name.endswith(".bak"):
            continue
        shutil.copy2(src, cfg / src.name)
        copied += 1

    # The .env is DERIVED from the real one rather than copied: staging must
    # match production's risk settings (otherwise it validates nothing) while
    # holding none of its credentials and reaching none of its channels.
    src_env = ROOT / ".env"
    lines, seen, dropped = [], set(), []
    if src_env.exists():
        for raw in src_env.read_text().splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k = s.split("=", 1)[0].strip()
            if _is_sensitive(k):
                dropped.append(k)
                continue
            seen.add(k)
            lines.append(f"{k}={STAGING_OVERRIDES.get(k, s.split('=', 1)[1].strip())}")
    for k, v in STAGING_OVERRIDES.items():
        if k not in seen:
            lines.append(f"{k}={v}")

    # Written with O_EXCL at 0600 rather than write_text: the default mode would
    # briefly leave it world-readable, and this file is derived from one that
    # holds credentials — being wrong about that once is enough.
    env_path = STAGING_HOME / ".env"
    env_path.unlink(missing_ok=True)
    fd = os.open(str(env_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, ("\n".join(sorted(lines)) + "\n").encode())
    finally:
        os.close(fd)

    # Verified, not assumed. The check that matters is on the file that was
    # actually written, because the bug this replaces was a name that did not
    # match the pattern someone had in mind.
    leaked = [ln.split("=", 1)[0] for ln in env_path.read_text().splitlines()
              if "=" in ln and _is_sensitive(ln.split("=", 1)[0])
              and ln.split("=", 1)[1].strip()]
    if leaked:
        env_path.unlink(missing_ok=True)
        print(f"REFUSING: {leaked} carried values into the staging .env")
        return 1

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
    print(f"dropped      : {len(dropped)} sensitive key(s) — "
          f"{', '.join(sorted(dropped)) or 'none'}")
    print(f"config       : {copied} file(s) copied")
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
