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
def check(name, cond, detail=""):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name
          + (f"   [{detail}]" if detail else ""))
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


# ── 6. the parameter console ───────────────────────────────────────────────
# It promises "save and it is in use". That is true of twelve parameters and
# false of thirty-eight: runtime_config._param() re-reads the file on every
# call, while everything else is read through `settings`, evaluated once at
# process start. So the console labels each row and restarts the worker when a
# cold one moves. The label is the promise, so it must be derived from the
# same thing the reader uses — never a second list to be kept in step.
print("\n6  the parameter console tells the truth about what it saved")
(Path(_TMP) / "config" / "parameters.json").write_text(json.dumps(
    {"version": 1, "params": {
        "ENTRY_SCORE_THRESHOLD": "70.0",   # hot,  shared
        "SL_ATR_MULT": "3.5",              # hot,  shared
        "USE_SCALE_OUT": "false",          # cold, shared
        "TIMEFRAME": "HOUR_1",             # cold, shared
        "STRATEGY_MODE": "technical",
        "FINBERT_ENABLED": "false",        # news mode only
        "NEWS_DRIVEN_MIN_SCORE": "65",     # news mode only
        "SENTIMENT_SCORING_ENABLED": "false"}}))   # technical only

from web.server import _param_rows, PARAM_GROUPS                # noqa: E402

# sections → groups → params since the console was regrouped by strategy.
_rows = {pr["key"]: pr for sec in _param_rows()
         for g in sec["groups"] for pr in g["params"]
         if not pr.get("special")}
_file = json.loads(
    (Path(_TMP) / "config" / "parameters.json").read_text())["params"]

check("every parameter in the file is shown — none silently omitted",
      set(_rows) == set(_file), f"{sorted(set(_file) - set(_rows))} missing")

# The label must come from _FILE_KEY, the map _param() actually consults.
check("the 'takes effect now' label matches what _param() can read",
      all(_rows[k]["hot"] == (k in set(rc._FILE_KEY.values())) for k in _rows))
check("...so a hot one is labelled hot", _rows["ENTRY_SCORE_THRESHOLD"]["hot"])
check("...and a cold one is not", not _rows["USE_SCALE_OUT"]["hot"])

# A parameter the console describes but the file has dropped must not appear
# as an editable row with an empty value — that would offer to "save" a key
# into existence with no reader.
_described = {k for _, items in PARAM_GROUPS for k, _, _ in items
              if not k.startswith("__")}
check("described-but-absent parameters are not offered for editing",
      not (set(_rows) - set(_file)))

# Every described row carries BOTH languages, and asking for one gets that one.
# The English UI used to be an English frame around a Chinese page: the frame
# came from data-i18n in the HTML, the page came from here, and only the frame
# had been translated.
def _has_cjk(t):
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(t))

_zh_rows = {pr["key"]: pr for sec in _param_rows("zh")
            for g in sec["groups"] for pr in g["params"]}
_en_rows = {pr["key"]: pr for sec in _param_rows("en")
            for g in sec["groups"] for pr in g["params"]}
check("both languages describe the same set of parameters",
      set(_zh_rows) == set(_en_rows))
_missing_en = [k for k, pr in _en_rows.items()
               if pr["desc"] and _has_cjk(pr["desc"])]
check("no English description is left in Chinese",
      not _missing_en, f"still Chinese: {_missing_en[:6]}")
_missing_zh = [k for k, pr in _zh_rows.items()
               if k in _described and not pr["desc"]]
check("every described parameter still has its Chinese text", not _missing_zh)
check("section headings translate",
      not _has_cjk("".join(sec["label"] for sec in _param_rows("en"))))
check("group headings translate",
      not _has_cjk("".join(g["name"] for sec in _param_rows("en")
                           for g in sec["groups"])))
check("...and the Chinese ones are still Chinese",
      _has_cjk("".join(sec["label"] for sec in _param_rows("zh"))))
check("an unknown lang falls back to Chinese, not to blank",
      _has_cjk("".join(sec["label"] for sec in _param_rows("de"))))

# Saving a hot parameter must move what the strategy reads, with no restart.
_before = rc.entry_threshold()
_c.post("/api/params", json={"params": {"ENTRY_SCORE_THRESHOLD": "72.5"}})
check("saving a hot parameter changes what the strategy reads immediately",
      _before != 72.5 and rc.entry_threshold() == 72.5)

# Saving a cold one must ask for a restart. Stub the protocol — this suite
# must never touch a real worker.
import src.start_protocol as _sp                                # noqa: E402
_calls = []
_sp.stop = lambda src: _calls.append(("stop", src))
_sp.start = lambda src: _calls.append(("start", src)) or {"pid": 1, "fence": 1}
_r = _c.post("/api/params", json={"params": {"USE_SCALE_OUT": "true"}}).get_json()
check("saving a cold parameter reports which one needed a restart",
      _r["needed_restart"] == ["USE_SCALE_OUT"])
check("...and actually restarts the worker",
      _r["restarted"] and [c[0] for c in _calls] == ["stop", "start"])

_calls.clear()
_r2 = _c.post("/api/params", json={"params": {"SL_ATR_MULT": "3.4"}}).get_json()
check("a hot-only save does NOT restart the worker",
      _r2["applied"] == ["SL_ATR_MULT"] and not _r2["restarted"] and not _calls)

_r3 = _c.post("/api/params", json={"params": {"NOT_A_PARAM": "1"}}).get_json()
check("an unknown key is refused rather than created",
      "NOT_A_PARAM" in _r3["rejected"])

# ── 6b. grouped by which strategy the parameter belongs to ─────────────────
# The split is the page's claim: a row under 新闻指标模式 does nothing while
# the mode is technical. If the membership were hand-maintained against the
# code rather than read off it, the claim would rot — so these assert the two
# that are provably mode-scoped, from the branch each reader sits in.
print("\n6b  the strategy split matches where the readers actually are")
_secs = {sec["id"]: sec for sec in _param_rows()}
check("there is a shared section and one per mode",
      {"shared", "technical", "news"} <= set(_secs))

def _keys(sid):
    return {pr["key"] for g in _secs[sid]["groups"] for pr in g["params"]}

# main.py: finbert_crosscheck sits inside `if news_driven.enabled():`
check("FINBERT_ENABLED is filed under the news mode that calls it",
      "FINBERT_ENABLED" in _keys("news"))
# main.py: sentiment scoring sits under `not news_driven.enabled()`
check("SENTIMENT_SCORING_ENABLED is filed under technical",
      "SENTIMENT_SCORING_ENABLED" in _keys("technical"))
# news_driven.threshold() is `entry_threshold + delta`, so the base is shared
check("ENTRY_SCORE_THRESHOLD is shared, because news mode uses it as its base",
      "ENTRY_SCORE_THRESHOLD" in _keys("shared"))
check("the two mode sections do not overlap", not (_keys("news") & _keys("technical")))

# Whichever mode is NOT running is marked inert as a whole.
_mode = json.loads(
    (Path(_TMP) / "config" / "parameters.json").read_text())["params"]["STRATEGY_MODE"]
_other = "news" if _mode == "technical" else "technical"
check(f"the section for the mode NOT running ({_other}) is marked inert",
      _secs[_other]["inert"] and not _secs[_mode]["inert"])
check("the shared section is never inert", not _secs["shared"]["inert"])

# NEWS_DRIVEN_ENABLED was the duplicate: config reads it only when
# STRATEGY_MODE is absent, which it never is. One setting, one row.
check("NEWS_DRIVEN_ENABLED is gone — STRATEGY_MODE is the only mode setting",
      "NEWS_DRIVEN_ENABLED" not in
      set().union(*(_keys(k) for k in _secs)))


# ── 7. the API key field ───────────────────────────────────────────────────
print("\n7  a key is masked in the page and whole behind the eye")
from web.server import _mask                                    # noqa: E402
# Named for what it is. check_no_secrets flags a literal assigned to
# anything called _SECRET — correctly, since it cannot know this one is
# invented, and the first version of this file was refused for it.
_FIXTURE_KEY = "SYNTHETIC-CONSOLE-KEY-ABCDEFGH1234"
_m = _mask(_FIXTURE_KEY)
check("the mask does not contain the key", _FIXTURE_KEY not in _m)
check("...and does not contain its middle",
      _FIXTURE_KEY[4:-4] not in _m and len(_FIXTURE_KEY[4:-4]) > 4)
check("...but keeps enough of both ends to recognise it",
      _m.startswith(_FIXTURE_KEY[:4]) and _m.endswith(_FIXTURE_KEY[-4:]))
check("a short value is masked entirely, not almost entirely",
      _mask("abcd1234") == "•" * 8)
check("an unset key masks to nothing", _mask("") == "")

(Path(_TMP) / ".env").write_text(
    "MOO_TRADE_ENV=SIMULATE\nWEB_PASSWORD=\n"
    f"TAVILY_API_KEY={_FIXTURE_KEY}\n")
_rev = _c.post("/api/settings/key/reveal",
               json={"key": "TAVILY_API_KEY"}).get_json()
check("the eye returns the whole key", _rev["value"] == _FIXTURE_KEY)
check("an unknown key cannot be revealed",
      _c.post("/api/settings/key/reveal",
              json={"key": "SOMETHING_ELSE"}).status_code == 400)

# ── 8  the settings page answers in the language it was asked for ───────────
# Same failure the parameter page had: an English frame around a Chinese page.
print("\n8  the settings page is translated too")
_set_zh = {k["key"]: k["desc"]
           for k in _c.get("/api/settings?lang=zh").get_json()["keys"]}
_set_en = {k["key"]: k["desc"]
           for k in _c.get("/api/settings?lang=en").get_json()["keys"]}
check("both languages describe the same keys", set(_set_zh) == set(_set_en))
_cjk_en = [k for k, d in _set_en.items() if _has_cjk(d)]
check("no English key description is left in Chinese", not _cjk_en,
      f"still Chinese: {_cjk_en}")
check("...and the Chinese ones are still Chinese",
      all(_has_cjk(d) for d in _set_zh.values()))
check("no lang given falls back to Chinese",
      all(_has_cjk(k["desc"])
          for k in _c.get("/api/settings").get_json()["keys"]))

# ── 9  the top bar's one server-rendered pill ───────────────────────────────
# opend_label was Chinese unconditionally, which put one Chinese pill in the
# middle of an otherwise English top bar.
print("\n9  the OpenD pill follows the language too")
import socket as _socket, contextlib as _ctx                   # noqa: E402
from web.server import _opend_status                           # noqa: E402

class _FakeSock:
    def __enter__(self): return self
    def __exit__(self, *a): return False

_real_conn = _socket.create_connection
_labels = {"zh": [], "en": []}
for _reach in (False, True):
    _socket.create_connection = (
        (lambda *a, **k: _FakeSock()) if _reach
        else (lambda *a, **k: (_ for _ in ()).throw(OSError("refused"))))
    for _acct in ({}, {"cash": 1.0, "trade_env": "SIMULATE"},
                  {"cash": 1.0, "trade_env": "REAL"},
                  {"cash": 1.0, "trade_env": "REAL", "real_unlock_confirmed": True}):
        for _sched in (False, True):
            for _lg in ("zh", "en"):
                _labels[_lg].append(_opend_status(dict(_acct), _sched, _lg)[1])
_socket.create_connection = _real_conn

_bad = sorted({l for l in _labels["en"] if _has_cjk(l)})
check("no English OpenD label is left in Chinese", not _bad, f"{_bad[:4]}")
check("...and the Chinese ones are still Chinese",
      any(_has_cjk(l) for l in _labels["zh"]))
check("the two languages cover the same set of states",
      len(set(_labels["zh"])) == len(set(_labels["en"])),
      f"zh={len(set(_labels['zh']))} en={len(set(_labels['en']))}")

import shutil                                                  # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
