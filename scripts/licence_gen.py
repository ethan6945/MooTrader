#!/usr/bin/env python3
"""Issue licence keys. Runs on the ISSUER's machine only.

  python scripts/licence_gen.py --machine <id-from-customer> --id MT-0001
  python scripts/licence_gen.py --machine <id> --id MT-0002 --years 2
  python scripts/licence_gen.py --machine <id> --id MT-0003 --perpetual
  python scripts/licence_gen.py --show-public

There is a web page for the same thing, which is the one to use day to day:
  cd "Keygen Activator" && ../.venv/bin/python keygen.py

Licences run for ONE YEAR unless told otherwise, and that default is doing real
work. This scheme is offline: a signature handed to a customer cannot be taken
back by another one, so a refund, a chargeback or a key that turns up on a forum
has no technical answer at all. A licence that lapses is the only one there is.
--perpetual is for your own machines, and for customers you would not want to
have to chase.

The private key lives in "Keygen Activator/" beside this repo — visible, not
hidden, so it is one folder an owner can see and remember to back up. NOTHING
else may ever hold it — not the repo, not a build, not a backup that leaves this
machine. Anyone with it can mint licences, which makes every issued licence
worthless. src/licence.py carries only the public half.

scripts/ is not in packaging/mmt-backend.spec's datas, so this file is not in a
frozen build. Check that again if the spec ever grows a scripts/ glob.

The customer's machine id comes from their own installation — the licence panel
shows it, or `python -c "from src.licence import machine_id; print(machine_id())"`.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date, timedelta
from pathlib import Path

KEYGEN_DIR = Path(__file__).resolve().parent.parent / "Keygen Activator"
PRIVATE_KEY = KEYGEN_DIR / "signing-private.pem"


def _next_id() -> str:
    """Same number the web keygen would assign — the log and the counter, whichever
    is higher. Two tools that number independently would collide on the first
    licence issued from the other one."""
    import re
    high = 0
    try:
        for r in json.loads((KEYGEN_DIR / "issued.json").read_text()):
            m = re.fullmatch(r"MT-(\d+)", str(r.get("id", "")))
            if m:
                high = max(high, int(m.group(1)))
    except (OSError, ValueError):
        pass
    try:
        high = max(high, int((KEYGEN_DIR / "next-id.txt").read_text().strip()))
    except (OSError, ValueError):
        pass
    return f"MT-{high + 1:04d}"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _load_key():
    from cryptography.hazmat.primitives import serialization
    try:
        return serialization.load_pem_private_key(
            PRIVATE_KEY.read_bytes(), password=None)
    except OSError:
        sys.exit(f"no signing key at {PRIVATE_KEY} — this is not the issuer's "
                 f"machine, or the key was lost. There is no recovery: a new "
                 f"key invalidates every licence already issued.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Issue a Moo Trader licence key.")
    ap.add_argument("--machine", help="customer's machine id (16 hex chars). "
                                      "Omit to issue an UNBOUND licence, which "
                                      "works on every machine — rarely what you "
                                      "want.")
    ap.add_argument("--id", help="override the auto-assigned MT-NNNN. The web "
                                 "keygen assigns numbers from the same log and "
                                 "counter; pass this only to reissue a known id.")
    ap.add_argument("--years", type=float, default=1.0, metavar="N",
                    help="licence runs for N years from today (default 1). A "
                         "dated licence is the only answer this scheme has to a "
                         "refund: nothing offline can withdraw a key that has "
                         "already been handed over, but one that lapses stops "
                         "on its own.")
    ap.add_argument("--perpetual", action="store_true",
                    help="never expires. Sells better and is unrevocable — use "
                         "it for your own machines and for customers you would "
                         "not want to chase.")
    ap.add_argument("--expires", help="YYYY-MM-DD, overriding --years")
    ap.add_argument("--edition", help="label shown in the panel; derived from "
                                      "the term when omitted")
    ap.add_argument("--show-public", action="store_true",
                    help="print the public key, to paste into src/licence.py")
    args = ap.parse_args()

    key = _load_key()

    if args.show_public:
        from cryptography.hazmat.primitives import serialization
        pub = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        print(pub.hex())
        return 0

    lic_id = args.id or _next_id()

    if args.perpetual and args.expires:
        ap.error("--perpetual and --expires contradict each other")
    if args.expires:
        try:
            date.fromisoformat(args.expires)
        except ValueError:
            ap.error("--expires must be YYYY-MM-DD")
        expires = args.expires
    elif args.perpetual:
        expires = None
    else:
        expires = (date.today() + timedelta(days=round(args.years * 365))
                   ).isoformat()

    if args.edition:
        edition = args.edition
    elif expires is None:
        edition = "perpetual"
    elif abs(args.years - 1.0) < 1e-9 and not args.expires:
        edition = "1 year"
    else:
        edition = f"until {expires}"

    payload = {
        "id": lic_id,
        "machine": (args.machine or "").strip().lower(),
        "edition": edition,
        "issued": date.today().isoformat(),
        "expires": expires,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    licence = f"MT1.{_b64(raw)}.{_b64(key.sign(raw))}"

    print(licence)
    print(f"\n  id      {payload['id']}", file=sys.stderr)
    print(f"  machine {payload['machine'] or 'UNBOUND — runs anywhere'}",
          file=sys.stderr)
    print(f"  expires {payload['expires'] or 'never'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
