#!/usr/bin/env python3
"""Build and sign the revocation list. Runs on the ISSUER's machine only.

  python scripts/licence_crl.py --revoke MT-0007 --revoke MT-0031 > revoked.txt
  python scripts/licence_crl.py --show          # what the current list holds

Then publish revoked.txt anywhere that serves a file over HTTPS — GitHub Pages,
Cloudflare Pages, a gist — and point installations at it:

  MMT_LICENCE_CRL=https://you.github.io/licences/revoked.txt

Revocation only ever needs to be READ, which is why this needs no server and no
money. The list is signed with the same key as the licences, so whoever hosts
the file cannot edit it, forge one, or swap in an empty list.

SERIAL

  Kept in ~/.mootrader-licence/crl-serial and incremented on every build. An
  installation remembers the highest serial it has seen and refuses anything
  lower, so an old list cannot be replayed to un-revoke a key. Never reset it —
  a list numbered below what customers have already seen will be ignored by
  every one of them.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HOME = Path.home() / ".mootrader-licence"
PRIVATE_KEY = HOME / "signing-private.pem"
SERIAL_FILE = HOME / "crl-serial"
LIST_FILE = HOME / "crl-revoked.json"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a signed revocation list.")
    ap.add_argument("--revoke", action="append", default=[], metavar="ID",
                    help="licence id to add (repeatable)")
    ap.add_argument("--unrevoke", action="append", default=[], metavar="ID",
                    help="licence id to remove (repeatable)")
    ap.add_argument("--show", action="store_true",
                    help="print the current list and exit")
    args = ap.parse_args()

    try:
        current = sorted(set(json.loads(LIST_FILE.read_text())))
    except (OSError, ValueError):
        current = []

    if args.show:
        print(f"{len(current)} revoked:", file=sys.stderr)
        for i in current:
            print(f"  {i}")
        return 0

    ids = sorted((set(current) | set(args.revoke)) - set(args.unrevoke))

    from cryptography.hazmat.primitives import serialization
    try:
        key = serialization.load_pem_private_key(
            PRIVATE_KEY.read_bytes(), password=None)
    except OSError:
        sys.exit(f"no signing key at {PRIVATE_KEY} — this is not the issuer's "
                 f"machine.")

    try:
        serial = int(SERIAL_FILE.read_text().strip()) + 1
    except (OSError, ValueError):
        serial = 1

    payload = {"revoked": ids,
               "issued": datetime.now(timezone.utc).date().isoformat(),
               "serial": serial}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    print(f"MTCRL1.{_b64(raw)}.{_b64(key.sign(raw))}")

    HOME.mkdir(parents=True, exist_ok=True)
    LIST_FILE.write_text(json.dumps(ids))
    SERIAL_FILE.write_text(str(serial))

    print(f"\n  serial  {serial}", file=sys.stderr)
    print(f"  issued  {payload['issued']}  (installations stop trusting it "
          f"after 30 days — republish monthly)", file=sys.stderr)
    print(f"  revoked {len(ids)}: {', '.join(ids) or '(none)'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
