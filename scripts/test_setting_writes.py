"""A saved setting must land in the file that decides it.

Run from repo root: .venv/bin/python scripts/test_setting_writes.py
Temp home throughout. Touches no real .env, no OpenD, no database.

WHY THIS EXISTS

  config._load_parameters overlays config/parameters.json onto os.environ
  AFTER load_dotenv, and overwrites. So for any key in both files there is a
  winner and a decoration, and a writer that does not know which is which will
  pick the wrong one roughly half the time and report success either way.

  On 2026-08-21 the settings panel wrote STRATEGY_MODE=technical and
  NEWS_DRIVEN_ENABLED=false to .env while parameters.json held news/true. Five
  controls were affected — the strategy-mode selector the page itself labels
  as "the only setting here that changes the strategy", plus every news and
  FinBERT toggle. Each wrote successfully, showed "restart to apply", and
  changed nothing. The bot kept selecting trades from news through a restart.

  The migration that moved parameters out of .env caused it, and that
  migration existed to end the identical split between .env and db-state
  (SL_ATR_MULT=2.8 in .env while the bot traded 3.5). Same bug, new pair of
  files. So the assertions here are about the ROUTING RULE, not about the five
  keys that happened to be caught by it.
"""
import ast
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-setwrite-")
os.environ["MMT_HOME"] = _TMP
for _d in ("data", "logs", "config"):
    (Path(_TMP) / _d).mkdir(parents=True, exist_ok=True)
(Path(_TMP) / ".env").write_text(
    "MOO_TRADE_ENV=SIMULATE\nDEEPSEEK_API_KEY=sk-test\nSTRATEGY_MODE=news\n")
(Path(_TMP) / "config" / "parameters.json").write_text(json.dumps(
    {"version": 1, "params": {"STRATEGY_MODE": "news",
                              "NEWS_DRIVEN_ENABLED": "true",
                              "ENTRY_SCORE_THRESHOLD": "70.0"}}, indent=2))

from src import runtime_config as rc                          # noqa: E402

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name)
    if cond: PASS += 1
    else: FAIL += 1


def _params() -> dict:
    return json.loads(
        (Path(_TMP) / "config" / "parameters.json").read_text())["params"]


def _env() -> dict:
    out = {}
    for line in (Path(_TMP) / ".env").read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            out[s.split("=", 1)[0].strip()] = s.split("=", 1)[1].strip()
    return out


# ── 1. the rule ────────────────────────────────────────────────────────────
print("1  a key goes to the store that decides it")
check("a key in parameters.json is owned by parameters.json",
      rc.owning_store("ENTRY_SCORE_THRESHOLD") == "parameters")
check("a key only in .env is owned by .env",
      rc.owning_store("MOO_TRADE_ENV") == "env")
check("an unknown key defaults to .env",
      rc.owning_store("SOMETHING_NEW") == "env")

# The case that broke: present in BOTH. The parameter file wins at runtime,
# so it must be what a write targets.
check("a key in BOTH is owned by parameters.json, because that is what wins",
      rc.owning_store("STRATEGY_MODE") == "parameters")


# ── 2. the write actually lands where the reader looks ─────────────────────
print("\n2  writing the strategy mode changes what the bot would run")
store = rc.write_setting("STRATEGY_MODE", "technical", source="test")
check("the write reports the parameter file", store == "parameters")
check("...and the parameter file holds the new value",
      _params()["STRATEGY_MODE"] == "technical")
check("...and os.environ was updated, so it applies without a restart too",
      os.environ["STRATEGY_MODE"] == "technical")
# The decoration must not survive: someone opening .env to find out what the
# bot does must not be told the old answer.
check("...and the stale .env line is gone", "STRATEGY_MODE" not in _env())

# A genuine .env key still goes to .env — the routing must not swallow
# credentials and connection settings into the parameter file.
store2 = rc.write_setting("MOO_TRADE_ENV", "SIMULATE", source="test")
check("an .env-owned key still goes to .env", store2 == "env")
check("...and is not created in the parameter file",
      "MOO_TRADE_ENV" not in _params())


# ── 3. no key may end up in both again ─────────────────────────────────────
print("\n3  the two stores cannot disagree after a write")
rc.write_setting("NEWS_DRIVEN_ENABLED", "false", source="test")
overlap = set(_env()) & set(_params())
check(f"no key is in both files (overlap {sorted(overlap) or 'none'})",
      not overlap)


# ── 4. no caller may write a settings file directly ────────────────────────
# Structural, not textual: parse the modules and look for a write to a path
# ending in .env or parameters.json outside runtime_config itself. A comment
# saying "always use write_setting" is exactly the thing that does not hold.
print("\n4  every writer goes through the one choke point")
LIVE_STORE_EXPRS = {
    "ENV_FILE", "env_file", "params_file()", "_env_path()",
    "ROOT / '.env'", "ROOT / \".env\"",
}
offenders = []
for f in sorted(list((ROOT / "src").glob("*.py")) + list((ROOT / "web").glob("*.py"))):
    if f.name == "runtime_config.py":
        continue
    try:
        tree = ast.parse(f.read_text())
    except (OSError, SyntaxError):
        continue
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        if n.func.attr not in ("write_text", "writelines"):
            continue
        recv = ast.unparse(n.func.value).strip()
        # The LIVE store only. snapshot_before writes `snap / '.env'`, a
        # redacted backup, which is a different file and a legitimate write —
        # matching on the substring ".env" flagged it and would have taught
        # the next reader to ignore this check.
        if recv in LIVE_STORE_EXPRS:
            fn = next((x.name for x in ast.walk(tree)
                       if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and any(y is n for y in ast.walk(x))), "<module>")
            offenders.append(f"{f.name}:{fn}")
check(f"nothing writes .env or parameters.json directly ({offenders or 'none'})",
      not offenders)


# ── 5. the panel's banner describes the WORKER ─────────────────────────────
# It compared the .env line against settings.strategy_mode — a file that no
# longer decides the mode, against the WEB SERVER's frozen snapshot. Neither
# is the trading worker, so restarting the trading loop could not clear the
# banner and restarting the web server cleared it whether or not the worker
# had changed. Three states that used to collapse into one.
print("\n5  the mode banner reports the running worker")
(Path(_TMP) / ".env").write_text("MOO_TRADE_ENV=SIMULATE\nWEB_PASSWORD=\n")
from src import db                                            # noqa: E402
db._ensure_initialised()
from web.server import app                                    # noqa: E402
app.config["TESTING"] = True
_c = app.test_client()

def _mode_api():
    return _c.get("/api/strategy-mode").get_json()

db.update_state({"worker_strategy_mode": None})
_d = _mode_api()
check("with no worker, mode is null rather than the configured value",
      _d["mode"] is None and _d["pending"] == "technical")

db.update_state({"worker_strategy_mode": "technical"})
_d = _mode_api()
check("a worker on the configured mode reports agreement",
      _d["mode"] == "technical" and _d["mode"] == _d["pending"])

# The case the user hit: saved technical, worker still on news.
db.update_state({"worker_strategy_mode": "news"})
_d = _mode_api()
check("a worker on a stale mode is reported as stale",
      _d["mode"] == "news" and _d["pending"] == "technical")

# And the fix that matters: changing the setting must move `pending`, which
# it could not do while the panel wrote .env and the reader read .env.
rc.write_setting("STRATEGY_MODE", "news", source="test")
_d = _mode_api()
check("saving a mode moves the pending value the banner compares against",
      _d["pending"] == "news")
check("...so once the worker restarts onto it, the banner clears",
      _d["mode"] == _d["pending"])

import shutil                                                  # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
