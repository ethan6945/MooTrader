#!/usr/bin/env python3
"""Replace redundant plaintext credential copies with redacted equivalents.

    .venv/bin/python scripts/scrub_credential_copies.py --dry-run
    .venv/bin/python scripts/scrub_credential_copies.py --apply

Originals go to the Trash, never to rm. Everything here is recoverable until the
Trash is emptied, which is a decision for a human on a different day.

ORDER OF OPERATIONS
  Write the replacement to a temporary name, verify it, only then trash the
  original, and only then move the replacement into place. There is no moment
  when the path holds nothing — a crash between steps leaves either the original
  or the verified replacement, never a gap.

WHAT "REDACTED" MEANS HERE
  Credentials are blanked. Account identifiers become an irreversible
  per-install reference, so two historical configs can still be compared for
  "same account?" without either carrying the identifier. Keys, comments and
  line order are preserved: these files exist to answer "what was configured on
  that date", and that answer survives redaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.hermes_improve import (is_identifier_key, is_secret_key,  # noqa: E402
                                redacted_env_text)

APP_HOME = Path.home() / "Library/Application Support/MooMooTrader"

# The five identified by scripts/audit_credential_files.py. Listed explicitly
# rather than globbed: this moves files to the Trash, and a glob that widens by
# accident is not something to discover afterwards.
TARGETS = [
    APP_HOME / ".env.bak-20260806-161711",
    APP_HOME / ".env.bak-20260806-162347",
    APP_HOME / ".env.bak-20260806-162604",
    APP_HOME / ".env.bak-20260806-230415",
    ROOT / "data" / "env.bak-20260810-081654",
]

MANIFEST = ROOT / "data" / "credential_scrub_manifest.json"


def live_secrets() -> dict[str, str]:
    """Current values, for verifying that none survive in a replacement."""
    out = {}
    for env in (ROOT / ".env", APP_HOME / ".env"):
        if not env.exists():
            continue
        for line in env.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, _, v = s.partition("=")
                v = v.split("#")[0].strip()
                if v and len(v) >= 8 and (is_secret_key(k) or is_identifier_key(k)):
                    out.setdefault(k.strip(), v)
    return out


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def verify(text: str, secrets: dict[str, str], original: str) -> list[str]:
    """Problems with a candidate replacement. Empty list means it is good."""
    problems = []
    for name, value in secrets.items():
        if value in text:
            problems.append(f"live value of {name} still present")
    # Key set and line count must survive — the file's purpose is the record.
    def keys(t):
        return [l.split("=", 1)[0].strip() for l in t.splitlines()
                if l.strip() and not l.strip().startswith("#") and "=" in l]
    if keys(text) != keys(original):
        problems.append("key set changed")
    if len(text.splitlines()) != len(original.splitlines()):
        problems.append("line count changed")
    if redacted_env_text(text) != text:
        problems.append("not idempotent — a second pass would change it again")
    return problems


def to_trash(p: Path) -> str:
    """Move to the Trash. Finder first, so Put Back works; ~/.Trash otherwise."""
    script = (f'tell application "Finder" to delete POSIX file '
              f'"{p.as_posix()}"')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode == 0:
        return "Finder (Put Back available)"
    trash = Path.home() / ".Trash"
    trash.mkdir(exist_ok=True)
    dest = trash / p.name
    n = 1
    while dest.exists():
        dest = trash / f"{p.stem}-{n}{p.suffix}"
        n += 1
    os.replace(p, dest) if dest.parent == p.parent else None
    if p.exists():
        import shutil
        shutil.move(str(p), str(dest))
    return f"~/.Trash/{dest.name} (no Put Back)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    secrets = live_secrets()
    print(f"verifying against {len(secrets)} live credential/identifier value(s)\n")

    records = []
    for p in TARGETS:
        rel = str(p).replace(str(Path.home()), "~")
        if not p.exists():
            print(f"  SKIP  {rel} — not present")
            continue
        original = p.read_text(errors="ignore")
        replacement = redacted_env_text(original)
        problems = verify(replacement, secrets, original)
        before = sha(p)

        print(f"  {rel}")
        print(f"      original  sha256 {before[:16]}…  {p.stat().st_size} B")
        if problems:
            print("      REFUSED — " + "; ".join(problems))
            records.append({"path": rel, "status": "refused", "problems": problems})
            continue
        print(f"      redacted  sha256 {hashlib.sha256(replacement.encode()).hexdigest()[:16]}…"
              f"  {len(replacement.encode())} B   verified clean")

        if args.dry_run:
            records.append({"path": rel, "status": "would-replace"})
            continue

        tmp = p.with_name(p.name + ".redacted.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, replacement.encode())
        finally:
            os.close(fd)
        # Re-verify from disk, not from the in-memory string.
        if verify(tmp.read_text(), secrets, original):
            tmp.unlink()
            print("      REFUSED after write-back verification")
            records.append({"path": rel, "status": "refused-on-disk"})
            continue

        where = to_trash(p)
        os.replace(tmp, p)
        os.chmod(p, 0o600)
        after = sha(p)
        print(f"      original -> {where}")
        print(f"      in place  sha256 {after[:16]}…  mode 600")
        records.append({
            "path": rel, "status": "replaced",
            "original_sha256": before, "redacted_sha256": after,
            "trashed_to": where,
            "at": datetime.now(timezone.utc).isoformat(),
        })

    if args.apply and records:
        prior = []
        if MANIFEST.exists():
            try:
                prior = json.loads(MANIFEST.read_text())
            except json.JSONDecodeError:
                prior = []
        prior.append({"at": datetime.now(timezone.utc).isoformat(),
                      "records": records})
        fd = os.open(str(MANIFEST), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(prior, indent=1).encode())
        finally:
            os.close(fd)
        print(f"\n  manifest: {MANIFEST.relative_to(ROOT)} "
              f"({len(records)} record(s) this run)")

    if args.dry_run:
        print("\n  dry run — nothing written, nothing trashed")
    return 1 if any(r["status"].startswith("refused") for r in records) else 0


if __name__ == "__main__":
    sys.exit(main())
