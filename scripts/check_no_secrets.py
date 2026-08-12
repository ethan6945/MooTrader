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
# fire on prices, quantities and timestamps. A four-digit WEB_PASSWORD is the
# reason this bound exists — such a password is a problem, but not one grep
# can fix, and quoting it here would publish it.
MIN_LEN = 12
MAX_FILE_BYTES = 5_000_000
TELEGRAM_SHAPE = re.compile(r"\bbot\d{8,}:[A-Za-z0-9_-]{20,}")

# Comparing against the CURRENT .env catches exactly one thing: today's
# credential copied somewhere it should not be. It cannot see
#   · a credential rotated last month that is still live at the vendor,
#   · someone else's token pasted in while debugging,
#   · a key for a service this project does not even use.
# All three are as publishable as the current one. So values are matched by
# SHAPE as well — using vendor prefixes rather than generic entropy, because a
# codebase is full of git SHAs and hex constants and a detector that fires on
# those gets muted within a week.
SHAPES = [
    ("Telegram bot token",  re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}")),
    ("OpenAI/DeepSeek key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("Tavily key",          re.compile(r"\btvly-[A-Za-z0-9]{16,}")),
    ("GitHub token",        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("AWS access key id",   re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Google API key",      re.compile(r"\bAIza[A-Za-z0-9_-]{30,}")),
    ("Slack token",         re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private key block",   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# A credential-shaped ASSIGNMENT whose value is neither empty nor a placeholder.
# This is what catches a key for a vendor with no recognisable prefix.
#
# Whitespace classes here are HORIZONTAL only ([^\S\n]). Using \s* around the
# separator lets the match run past the end of the line — `KEY=` with an empty
# value then swallows the newline and matches whatever the next line starts
# with, which is how the first version of this reported two empty placeholders
# in .env.example as real credentials.
ASSIGN = re.compile(
    r"(?im)^[^\S\n]*(?:export[^\S\n]+)?"
    r"([A-Z0-9_]*(?:API_KEY|SECRET|PASSWORD|TOKEN|PWD)[A-Z0-9_]*)"
    r"[^\S\n]*[=:][^\S\n]*[\"']?([^\s\"'#]{16,})[\"']?[^\S\n]*(?:#.*)?$")

# `pwd = settings.moo_trade_pwd` is a lookup, not a literal. Attribute access
# and calls mean the value lives somewhere else — which is the correct pattern,
# not a leak.
CODE_EXPR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.|[(\[]")
PLACEHOLDER = re.compile(
    r"^(your|my|xxx|<|\{|\$\{|placeholder|changeme|change_me|todo|example|dummy|"
    r"fake|sample|test|redacted|ref:|\.\.\.|abc123|123456|none|null|n/?a|"
    r"0{8,}|1234567890)", re.I)

# Per-LINE exemption. A file-level marker exempted the whole file from the shape
# heuristics, so one comment at the top let any number of real, rotated or
# third-party credentials ride along underneath it. Now the marker must sit on
# the same line as the value, or on the line immediately above it, and it
# exempts only that line.
#
# It never exempts a comparison against a real value from .env — that check runs
# on every line of every file regardless.
SYNTHETIC_MARKER = "SYNTHETIC-CREDENTIALS-OK"


def _exempt_lines(text: str) -> set[int]:
    """1-indexed lines the marker covers: its own, and the one after it."""
    out: set[int] = set()
    for i, line in enumerate(text.splitlines(), start=1):
        if SYNTHETIC_MARKER in line:
            out.add(i)
            out.add(i + 1)
    return out


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

        # Shape matching, line by line, so an exemption covers one fixture and
        # not everything that happens to share the file with it.
        exempt = _exempt_lines(text)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if lineno in exempt:
                continue
            if TELEGRAM_SHAPE.search(line):
                findings.append((rel, f"line {lineno}: a Telegram-token-shaped string"))
            for label, pat in SHAPES:
                if pat.search(line):
                    findings.append((rel, f"line {lineno}: {label} (by shape — may "
                                          f"be a rotated or third-party credential)"))
            for m in ASSIGN.finditer(line):
                key, value = m.group(1), m.group(2)
                if PLACEHOLDER.match(value) or CODE_EXPR.search(value):
                    continue
                # A reference or an already-redacted marker is the desired state.
                if value.startswith(("ref:", "<redacted", "$(", "${")):
                    continue
                findings.append((rel, f"line {lineno}: {key} assigned a "
                                      f"real-looking literal"))

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
