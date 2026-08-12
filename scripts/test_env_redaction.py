"""Self-contained test for .env redaction in Hermes snapshots.

Run from repo root: .venv/bin/python scripts/test_env_redaction.py
Uses a synthetic .env — never reads the real one, never prints a credential.

WHY THIS EXISTS
  hermes_improve.snapshot_before() used to `shutil.copy` the whole .env into
  data/hermes_snapshots/<ts>/.env before every parameter change. The 2026-08-10
  audit found three such snapshots, each holding SEVEN populated credentials —
  DEEPSEEK_API_KEY, GEMINI_API_KEYS, TAVILY_API_KEY, TELEGRAM_TOKEN,
  WEB_PASSWORD, WEB_SECRET and MOOMOO_TRADE_PWD, the last of which is what
  unlocks real-money orders at the broker.

  data/ is gitignored and `git grep` over all 92 commits found nothing, so none
  of it reached the public repo. But one credential living in four files is a
  blast radius nobody chose, and the module already had a secret-skip predicate
  for the LLM payload — it simply was not applied on the snapshot path.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.hermes_improve import is_secret_key, redacted_env_text, _REDACTED  # noqa: E402

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


SAMPLE = """\
# Broker
MOO_HOST=127.0.0.1
MOOMOO_TRADE_PWD=hunter2secret
MOO_TRADE_ENV=SIMULATE

# AI
DEEPSEEK_API_KEY=sk-deadbeefcafe
GEMINI_API_KEYS=aaa,bbb,ccc
TAVILY_API_KEY=tvly-1234

# Notifications
TELEGRAM_TOKEN=1234567890:AAHsyntheticvalue
TELEGRAM_CHAT_ID=12345

# Web
WEB_PASSWORD=5566
WEB_SECRET=s3cr3t

# Strategy — these MUST survive, rollback() reads them back
ENTRY_SCORE_THRESHOLD=70   # tuned
MAX_POSITION_PCT=0.40
EMPTY_KEY=
"""

SECRET_VALUES = ["hunter2secret", "sk-deadbeefcafe", "aaa,bbb,ccc", "tvly-1234",
                 "1234567890:AAHsyntheticvalue", "5566", "s3cr3t"]

out = redacted_env_text(SAMPLE)

# ── 1. every credential value is gone ────────────────────────────────────────
for v in SECRET_VALUES:
    check(f"value removed: {v[:6]}…", v not in out)

# ── 2. the broker trade password specifically — this one moves real money ────
check("MOOMOO_TRADE_PWD redacted",
      f"MOOMOO_TRADE_PWD={_REDACTED}" in out)

# ── 3. non-secret settings survive, so rollback() still works ────────────────
check("ENTRY_SCORE_THRESHOLD survives", "ENTRY_SCORE_THRESHOLD=70   # tuned" in out)
check("MAX_POSITION_PCT survives", "MAX_POSITION_PCT=0.40" in out)
check("MOO_TRADE_ENV survives (not a secret despite the module)",
      "MOO_TRADE_ENV=SIMULATE" in out)
# TELEGRAM_CHAT_ID used to be asserted to pass through untouched, on the
# reasoning that an id is not a credential. It survived three rounds of
# "redaction" that way and sat in plaintext in every backup — and anyone holding
# it plus a bot token can message the owner directly. It is not authentication,
# but it identifies a person, so it becomes an irreversible per-install
# reference: still comparable between two backups, no longer usable.
check("chat id is referenced, not left in the clear",
      "TELEGRAM_CHAT_ID=12345" not in out)
check("chat id becomes a ref", "TELEGRAM_CHAT_ID=ref:" in out)

# ── 4. shape is preserved — a snapshot is still a readable record ────────────
check("line count preserved",
      len(out.splitlines()) == len(SAMPLE.splitlines()))
check("comments preserved", out.count("#") == SAMPLE.count("#"))
check("keys preserved", all(
    line.partition("=")[0] in out
    for line in SAMPLE.splitlines() if "=" in line and not line.startswith("#")))

# ── 5. an empty secret is left alone, not given a fake value ─────────────────
check("empty value untouched", "EMPTY_KEY=" in out and f"EMPTY_KEY={_REDACTED}" not in out)

# ── 6. the predicate itself ──────────────────────────────────────────────────
for k in ("DEEPSEEK_API_KEY", "WEB_PASSWORD", "TELEGRAM_TOKEN", "WEB_SECRET",
          "MOOMOO_TRADE_PWD", "GEMINI_API_KEYS"):
    check(f"is_secret_key({k})", is_secret_key(k))
for k in ("ENTRY_SCORE_THRESHOLD", "MAX_POSITION_PCT", "MOO_HOST", "TELEGRAM_CHAT_ID"):
    check(f"not secret: {k}", not is_secret_key(k))

# ── 7. idempotent — redacting a redacted file changes nothing further ────────
check("redaction is idempotent", redacted_env_text(out) == out)

# ── 8. log scrubbing ─────────────────────────────────────────────────────────
# Same class of leak, different file. notifier.send() logs the requests
# exception on a network error; requests puts the failing URL in that message
# and Telegram puts the bot token in the URL path. Result: the live token in
# trader.log 14× and scheduler.log 6×. Nothing was misconfigured — a normal
# error path did it — so the fix has to sit at the logging layer.
import io                                                       # noqa: E402
import logging                                                  # noqa: E402

from src import log_redact                                      # noqa: E402

# SYNTHETIC-CREDENTIALS-OK — the fixtures below are invented, and this marker
# tells scripts/check_no_secrets.py to skip its token-SHAPE heuristic here. It
# does not exempt this file from comparison against the real values in .env.
#
# Never paste a real credential in here "as a realistic example". This file was
# first written with the live DeepSeek key and the real Telegram bot id copied
# out of terminal output; every test passed, .gitignore was irrelevant, and only
# check_no_secrets.py caught it.
FAKE_TOKEN = "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
FAKE_KEY = "sk-00000000000000000000000000000000"
log_redact._cache = [FAKE_TOKEN, FAKE_KEY]      # don't touch the real .env

check("scrub removes a bare token", FAKE_TOKEN not in log_redact.scrub(
    f"telegram_token={FAKE_TOKEN}"))
check("scrub removes an api key", FAKE_KEY not in log_redact.scrub(
    f"calling deepseek with {FAKE_KEY}"))
check("scrub leaves ordinary text alone",
      log_redact.scrub("bought 12 NVDA @ 178.40") == "bought 12 NVDA @ 178.40")

# The URL-path pattern must catch a token even when it is NOT in the value list
# — e.g. after a rotation, when the log still carries the previous one.
log_redact._cache = []
url_line = ("HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries "
            "exceeded with url: /bot9999999999:ZZunknownrotatedtoken/sendMessage")
check("scrub catches /bot<token>/ by shape, not just by value",
      "ZZunknownrotatedtoken" not in log_redact.scrub(url_line))

# Short values must NOT be blanket-replaced: WEB_PASSWORD=5566 is real, and
# replacing "5566" everywhere would corrupt prices and quantities in the log.
log_redact._cache = [s for s in ["5566"] if len(s) >= log_redact._MIN_SECRET_LEN]
check("short values are not treated as secrets",
      log_redact.scrub("filled 5566 shares") == "filled 5566 shares")

# End to end: a third-party logger this codebase never calls directly.
log_redact._cache = [FAKE_TOKEN]
buf = io.StringIO()
handler = logging.StreamHandler(buf)
handler.setFormatter(logging.Formatter("%(message)s"))
root = logging.getLogger()
saved_handlers, saved_level = root.handlers, root.level
root.handlers, root.filters = [handler], []
root.setLevel(logging.INFO)
log_redact.install()
logging.getLogger("urllib3.connectionpool").warning(
    "telegram send failed: %s", f"url: /bot{FAKE_TOKEN}/sendMessage")
logged = buf.getvalue()
root.handlers, root.filters = saved_handlers, []
root.setLevel(saved_level)

check("installed filter scrubs a third-party logger", FAKE_TOKEN not in logged)
check("installed filter keeps the rest of the message",
      "telegram send failed" in logged and "sendMessage" in logged)

log_redact.reset_cache()

# ── 9. the publish check sees more than today's credential ──────────────────
# Comparing only against the current .env catches one thing: today's value in
# the wrong place. It cannot see a credential rotated last month that is still
# live at the vendor, someone else's token pasted in while debugging, or a key
# for a service this project does not use. All three publish just as badly.
import importlib.util                                          # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "cns", ROOT / "scripts" / "check_no_secrets.py")
cns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cns)

def shapes_in(text):
    out = [label for label, pat in cns.SHAPES if pat.search(text)]
    for m in cns.ASSIGN.finditer(text):
        v = m.group(2)
        if not cns.PLACEHOLDER.match(v) and not cns.CODE_EXPR.search(v):
            out.append(f"assign:{m.group(1)}")
    return out

check("a rotated OpenAI-style key is caught",
      "OpenAI/DeepSeek key" in shapes_in('K = "sk-a1b2c3d4e5f6a7b8c9d0e1f2a3b4"'))
check("a third-party bot token is caught",
      "Telegram bot token" in shapes_in('T = "9988776655:AAG-notOursAtAll1234567890abcd"'))
check("an AWS key id is caught",
      "AWS access key id" in shapes_in('A = "AKIAIOSFODNN7EXAMPLE"'))
check("a vendorless key assignment is caught",
      any(s.startswith("assign:") for s in
          shapes_in('SOME_API_KEY = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"')))

# …without firing on the things a codebase is full of.
check("a git SHA is not a credential",
      shapes_in("commit 89d078825abd4f969994f292b65121ad32fee276") == [])
check("a config lookup is not a literal",
      shapes_in("pwd = settings.moo_trade_pwd") == [])
check("an md5 call is not a literal",
      shapes_in("pwd_md5 = hashlib.md5(pwd.encode()).hexdigest()") == [])
check("an empty placeholder does not swallow the next line",
      shapes_in("TELEGRAM_TOKEN=\nSOME_OTHER_LINE=abcdefghijklmnopqrst") == [])
check("a documented placeholder is not a finding",
      shapes_in('API_KEY = "your_api_key_here_please"') == [])
check("a redacted value is the desired state, not a finding",
      shapes_in("TELEGRAM_TOKEN=<redacted-by-snapshot>") == [])
check("a reference is not a finding",
      shapes_in("TELEGRAM_CHAT_ID=ref:telegram_chat_id:0123456789abcdef0123") == [])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
