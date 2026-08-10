#!/usr/bin/env python3
"""Pre-upload check: does any file git would publish contain a live credential?

Read-only. Run from repo root before pushing, especially the first push to a
fresh remote:

    .venv/bin/python scripts/check_no_secrets.py

Exit 0 = clean. Exit 1 = something sensitive would be published.

WHY THIS EXISTS
  .gitignore already excludes .env, data/ and logs/, and that has held — a
  `git grep` over every commit in this repo's history finds no credential. The
  gap it does not cover is a secret pasted into a file that SHOULD be tracked.

  That is not hypothetical: on 2026-08-10 a test file written to verify
  credential redaction was itself seeded with the live DeepSeek API key and the
  real Telegram bot id, copied from terminal output as "realistic-looking
  examples". The test passed. .gitignore was irrelevant. Only comparing file
  contents against the actual values in .env caught it.

WHAT IT CHECKS
  Every file `git ls-files -co --exclude-standard` reports — i.e. exactly the
  set a `git add -A` would stage — against:
    · every populated secret-shaped value in .env
    · the Telegram bot id alone (identifying even without its secret half)
    · anything with the shape of a Telegram token, in case .env has rotated
      since the leak was written

  Values are compared, never printed. Findings name the file and which
  credential matched, not the credential itself.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

SECRET_MARKERS = ("KEY", "SECRET", "PASSWORD", "TOKEN", "PWD")
# Below this length a value is not a credential, and blanket-matching it would
# fire on prices, quantities and timestamps. WEB_PASSWORD=5566 is the reason
# this bound exists — such a password is a problem, but not one grep can fix.
MIN_LEN = 12
MAX_FILE_BYTES = 5_000_000
TELEGRAM_SHAPE = re.compile(r"\bbot\d{8,}:[A-Za-z0-9_-]{20,}")

# A file that legitimately needs credential-shaped fixtures (the redaction
# tests) may declare this marker to opt out of the SHAPE heuristic. It does not
# opt out of anything else: every comparison against a real value in .env still
# runs, so the marker cannot be used to smuggle a live credential past this
# check — only to silence a pattern match on a value known to be invented.
SYNTHETIC_MARKER = "SYNTHETIC-CREDENTIALS-OK"


def live_secrets() -> dict[str, str]:
    """Populated secret-shaped values from .env. Returns {key: value}."""
    out: dict[str, str] = {}
    env = ROOT / ".env"
    if not env.exists():
        return out
    for line in env.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.split("#")[0].strip()
        if v and len(v) >= MIN_LEN and any(m in k.upper() for m in SECRET_MARKERS):
            out[k] = v
    return out


def uploadable_files() -> list[pathlib.Path]:
    """Exactly what `git add -A` would stage: tracked + untracked, minus ignored."""
    res = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / f for f in res.stdout.split("\n") if f.strip()]


def main() -> int:
    secrets = live_secrets()
    files = uploadable_files()
    bot_id = secrets.get("TELEGRAM_TOKEN", "").split(":")[0]

    print(f"checking {len(files)} uploadable files against "
          f"{len(secrets)} live credential(s)")

    findings: list[tuple[str, str]] = []
    for p in files:
        if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        rel = p.relative_to(ROOT).as_posix()
        for name, value in secrets.items():
            if value in text:
                findings.append((rel, name))
        if len(bot_id) >= 8 and bot_id in text:
            findings.append((rel, "TELEGRAM bot id"))
        if TELEGRAM_SHAPE.search(text) and SYNTHETIC_MARKER not in text:
            findings.append((rel, "a Telegram-token-shaped string"))

    if not findings:
        print("clean — nothing sensitive in any file git would publish")
        return 0

    print(f"\n{len(findings)} finding(s) — DO NOT PUSH:\n")
    for rel, what in dict.fromkeys(findings):
        print(f"  {rel}\n      contains: {what}")
    print("\nReplace the value with a synthetic one. If it was already pushed, "
          "rotate the credential — removing the file does not un-publish it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
