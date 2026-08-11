#!/usr/bin/env python3
"""Read-only inventory of every file that holds a credential or an account id.

    .venv/bin/python scripts/audit_credential_files.py

Reads. Never writes, deletes, moves, or rotates anything.

It prints key NAMES, never values. "Matches current" is decided by comparing
SHA-256 of the value against the canonical one — enough to say whether a copy is
stale or live, without the value crossing into the terminal, the scrollback, or
this session's transcript.

WHY
  Credentials accumulate in copies nobody made deliberately: a snapshot taken
  before a config change, a .bak written by an editor, a backup of a backup. Each
  is as sensitive as the original and none of them are being watched. Before
  deciding what to remove, the list has to be complete and the classification has
  to be explicit — a "backup" that turns out to be the only copy of something is
  not a file to delete on a hunch.
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.hermes_improve import is_identifier_key, is_secret_key  # noqa: E402

APP_HOME = Path.home() / "Library/Application Support/MooMooTrader"
CANONICAL = {ROOT / ".env", APP_HOME / ".env"}
REDACTED_MARKERS = ("<redacted", "ref:")


def parse(p: Path) -> dict[str, str]:
    out = {}
    try:
        text = p.read_text(errors="ignore")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.split("#")[0].strip()
    return out


def digest(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()[:12]


def classify(p: Path, keys: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """(classification, plaintext_sensitive_keys, redacted_keys)"""
    plain, red = [], []
    for k, v in keys.items():
        if not v or not (is_secret_key(k) or is_identifier_key(k)):
            continue
        (red if any(v.startswith(m) or m in v for m in REDACTED_MARKERS)
         else plain).append(k)
    if p in CANONICAL:
        return "CANONICAL (live config — keep)", plain, red
    if plain:
        return "REDUNDANT PLAINTEXT", plain, red
    if red:
        return "redacted backup (safe)", plain, red
    return "no sensitive keys", plain, red


def main() -> int:
    candidates = []
    for base in (APP_HOME, ROOT, Path("/Applications/MooTrader.app")):
        if not base.exists():
            continue
        for pat in (".env*", "*env.bak*", "*.env"):
            for p in base.rglob(pat):
                if p.is_file() and ".venv" not in p.parts:
                    candidates.append(p)
    # Anything else on disk that happens to hold a live secret.
    canon_vals = {}
    for c in CANONICAL:
        for k, v in parse(c).items():
            if v and (is_secret_key(k) or is_identifier_key(k)):
                canon_vals.setdefault(k, set()).add(digest(v))

    for extra in ((ROOT / "logs"), (APP_HOME / "logs"),
                  (ROOT / "data"), (ROOT / "packaging" / "dist")):
        if not extra.exists():
            continue
        for p in extra.rglob("*"):
            if not p.is_file() or p.stat().st_size > 40_000_000:
                continue
            if p in candidates or p.suffix in (".db", ".parquet", ".pyc"):
                continue
            try:
                blob = p.read_text(errors="ignore")
            except OSError:
                continue
            hit = [k for k, v in parse(ROOT / ".env").items()
                   if v and len(v) >= 12 and (is_secret_key(k) or is_identifier_key(k))
                   and v in blob]
            if hit:
                candidates.append(p)

    candidates = sorted(set(candidates))
    buckets: dict[str, list] = {}
    print(f"{len(candidates)} file(s) examined\n")
    for p in candidates:
        keys = parse(p)
        cls, plain, red = classify(p, keys)
        if cls == "no sensitive keys":
            continue
        st = p.stat()
        stale = []
        for k in plain:
            d = digest(keys[k])
            if k in canon_vals:
                stale.append(f"{k}{'=live' if d in canon_vals[k] else '=stale'}")
            else:
                stale.append(f"{k}=unknown")
        buckets.setdefault(cls, []).append({
            "path": str(p).replace(str(Path.home()), "~"),
            "mode": stat.filemode(st.st_mode),
            "octal": oct(st.st_mode & 0o777)[2:],
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "plain": stale, "redacted": red,
        })

    order = ["CANONICAL (live config — keep)", "REDUNDANT PLAINTEXT",
             "redacted backup (safe)"]
    for cls in order:
        rows = buckets.get(cls, [])
        if not rows:
            continue
        print(f"── {cls} — {len(rows)} file(s) " + "─" * max(0, 46 - len(cls)))
        for r in rows:
            print(f"  {r['path']}")
            print(f"      {r['mode']} ({r['octal']})  {r['size']:>7} B  {r['mtime']}")
            if r["plain"]:
                print(f"      plaintext sensitive keys ({len(r['plain'])}): "
                      f"{', '.join(sorted(r['plain']))}")
            if r["redacted"]:
                print(f"      already redacted ({len(r['redacted'])}): "
                      f"{', '.join(sorted(r['redacted']))}")
        print()

    plain_files = buckets.get("REDUNDANT PLAINTEXT", [])
    live_keys = sorted({s.split("=")[0] for r in plain_files for s in r["plain"]
                        if s.endswith("=live")})
    print("── rotation assessment " + "─" * 44)
    if live_keys:
        print(f"  {len(live_keys)} credential(s) whose CURRENT value sits in a "
              f"redundant copy:")
        for k in live_keys:
            print(f"      {k}")
        print("  Rotation is warranted only if a copy left this machine — a sync\n"
              "  folder, a shared log, a pushed commit. Local-only copies are a\n"
              "  cleanup job, not a rotation. Assessed separately; nothing rotated.")
    else:
        print("  No live credential found outside the canonical files.")

    print("\n  Nothing was written, moved, or rotated — this script only reads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
