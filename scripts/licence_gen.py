#!/usr/bin/env python3
"""Issue licence keys. Runs on the ISSUER's machine only.

  python scripts/licence_gen.py --machine <id-from-customer> --id MT-0001
  python scripts/licence_gen.py --machine <id> --id MT-0002 --expires 2027-09-03
  python scripts/licence_gen.py --show-public

The private key lives at ~/.mootrader-licence/signing-private.pem and NOTHING
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
from datetime import date
from pathlib import Path

PRIVATE_KEY = Path.home() / ".mootrader-licence" / "signing-private.pem"


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
    ap.add_argument("--id", help="licence id you will recognise later, e.g. an "
                                 "order number")
    ap.add_argument("--edition", default="perpetual")
    ap.add_argument("--expires", help="YYYY-MM-DD; omit for perpetual")
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

    if not args.id:
        ap.error("--id is required")
    if args.expires:
        try:
            date.fromisoformat(args.expires)
        except ValueError:
            ap.error("--expires must be YYYY-MM-DD")

    payload = {
        "id": args.id,
        "machine": (args.machine or "").strip().lower(),
        "edition": args.edition,
        "issued": date.today().isoformat(),
        "expires": args.expires,
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
