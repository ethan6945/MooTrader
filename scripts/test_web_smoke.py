"""Every GET route must return a page, not a stack trace.

Run from repo root: .venv/bin/python scripts/test_web_smoke.py
Temp home, empty password, no OpenD required. Places nothing.

WHY THIS EXISTS

  Removing Gemini deleted this line from setup_state():

      provider = (val("AI_PROVIDER", "deepseek") or "deepseek").lower()

  along with the `if provider == "gemini":` branch below it that was the
  reason for the deletion. `provider` was still used forty lines further on.
  Opening the panel returned "Internal Server Error" — on `/`, the first thing
  anyone loads.

  Twenty-eight suites were green. Not one of them had asked the application
  for a page. Every web test in this repository calls a handler's helpers or
  posts to one specific endpoint; nothing exercised the route table, so a
  NameError in a view was invisible to all of it.

  These assertions are deliberately shallow — status code and content type,
  nothing about what the page says. Depth is what the other suites are for.
  This one exists to make "it imports" stop being mistaken for "it runs".
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-websmoke-")
os.environ["MMT_HOME"] = _TMP
for _d in ("data", "logs", "config"):
    (Path(_TMP) / _d).mkdir(parents=True, exist_ok=True)
(Path(_TMP) / ".env").write_text(
    "MOO_TRADE_ENV=SIMULATE\nWEB_PASSWORD=\nDEEPSEEK_API_KEY=synthetic-fixture-value\n")
(Path(_TMP) / "config" / "parameters.json").write_text(json.dumps(
    {"version": 1, "params": {"ENTRY_SCORE_THRESHOLD": "70.0",
                              "SL_ATR_MULT": "3.5",
                              "USE_SCALE_OUT": "false",
                              "STRATEGY_MODE": "technical"}}, indent=2))

from src import db                                             # noqa: E402
db._ensure_initialised()
from web.server import app                                     # noqa: E402

app.config["TESTING"] = True          # so a view's exception surfaces here
client = app.test_client()

PASS = 0
FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name + (f"   [{detail}]" if detail else ""))
    if cond: PASS += 1
    else: FAIL += 1


# Every parameterless GET the app declares — discovered from the route table,
# not from a list here. A route added next month is covered the day it exists;
# a hand-written list would have to be remembered, and this suite exists
# because something was not.
ROUTES = sorted(
    r.rule for r in app.url_map.iter_rules()
    if r.endpoint != "static" and "GET" in r.methods and "<" not in r.rule)

print(f"1  {len(ROUTES)} parameterless GET route(s), discovered from the app")
check("the route table is not empty", len(ROUTES) >= 20, str(len(ROUTES)))

print("\n2  none of them raises")
broken = []
for rule in ROUTES:
    try:
        r = client.get(rule)
        code = r.status_code
    except Exception as e:
        broken.append(f"{rule} → {type(e).__name__}: {e}")
        continue
    # 2xx and 3xx are both fine — /setup redirects, / may redirect to /setup.
    # 5xx is not, and neither is 4xx on a route that takes no arguments.
    if code >= 400:
        broken.append(f"{rule} → HTTP {code}")
for b in broken:
    print(f"        {b}")
check(f"every route answers ({len(ROUTES) - len(broken)}/{len(ROUTES)})", not broken)

# The two that matter most, called out by name so a failure says which.
print("\n3  the pages a person actually opens")
for rule in ("/", "/setup", "/login"):
    r = client.get(rule)
    check(f"GET {rule}", r.status_code < 400, f"HTTP {r.status_code}")

print("\n4  the JSON endpoints return JSON")
bad_ct = []
for rule in [x for x in ROUTES if x.startswith("/api/")]:
    r = client.get(rule)
    if r.status_code < 400 and "json" not in (r.content_type or ""):
        bad_ct.append(f"{rule} → {r.content_type}")
for b in bad_ct:
    print(f"        {b}")
check("every /api GET is application/json", not bad_ct)

# A route that 500s while TESTING is off returns Flask's error page rather
# than raising — which is exactly how this reached the browser. Prove the
# suite would catch it either way.
print("\n5  the check works with TESTING off, the way the real server runs")
app.config["TESTING"] = False
r = client.get("/")
check("GET / with TESTING off", r.status_code < 400, f"HTTP {r.status_code}")
app.config["TESTING"] = True

# ── 6. the page's own script must not call a function nobody defines ───────
# Twice now a deletion bounded by two comment markers has taken code that had
# nothing to do with the block being removed. The first cost `provider` and
# returned Internal Server Error on every page; the second took loadTradeEnv,
# loadWebAccess, saveKey, showRestart and toggleKey, and the settings dialog
# stopped opening because loadSettings() calls loadWebAccess() on its last
# line. Neither was a syntax error, so nothing caught either.
#
# A range chosen by two comments is a range nobody has read. This asserts on
# the reachable call graph instead: every plain `name(...)` call in the page
# script must resolve to something the script itself declares, or to a browser
# global. It is a linter's job, and there is no linter here.
print("\n6  every function the page calls is defined")
import re                                                      # noqa: E402

_html = (ROOT / "web" / "static" / "index.html").read_text()
_raw = _html[_html.index("<script>") + 8:_html.rindex("</script>")]


def _strip_js(src: str) -> str:
    """Comments and string literals out, so a word in prose is not a call.

    The first version of this check scanned the raw text and reported
    OUTAGE(), approval(), gradient() — English inside comments, and CSS
    inside template strings. Which is the same mistake this repository keeps
    finding in its own assertions: matching text where structure was meant.
    There is no JS parser here, so this is the smallest thing that is
    actually right — a scanner that knows what it is inside of.
    """
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            i = src.find("\n", i)
            if i < 0:
                break
            continue
        if c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "\"'`":
            quote, i = c, i + 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                # ${...} inside a template literal is real code — keep it.
                if quote == "`" and src[i] == "$" and src[i + 1:i + 2] == "{":
                    depth, k = 1, i + 2
                    while k < n and depth:
                        depth += (src[k] == "{") - (src[k] == "}")
                        k += 1
                    out.append(" " + src[i + 2:k - 1] + " ")
                    i = k
                    continue
                i += 1
            out.append(" ")
            continue
        out.append(c)
        i += 1
    return "".join(out)


_script = _strip_js(_raw)

# Declared: function foo(), const foo = ..., let foo = ..., var foo = ...
_declared = set(re.findall(r'(?:async\s+)?function\s+([A-Za-z_$][\w$]*)', _script))
_declared |= set(re.findall(r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=', _script))
_declared |= set(re.findall(r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(', _script))

# Browser and language globals the script legitimately reaches for.
_GLOBALS = {
    "Array", "Boolean", "Date", "Error", "Infinity", "Intl", "JSON", "Map",
    "Math", "Number", "Object", "Promise", "RegExp", "Set", "String", "Symbol",
    "URL", "URLSearchParams", "WeakMap", "alert", "atob", "btoa", "clearInterval",
    "clearTimeout", "confirm", "console", "decodeURIComponent", "document",
    "encodeURIComponent", "fetch", "history", "isFinite", "isNaN", "localStorage",
    "location", "navigator", "parseFloat", "parseInt", "prompt", "requestAnimationFrame",
    "setInterval", "setTimeout", "window", "Event", "CustomEvent", "Image",
    "FormData", "Blob", "AbortController", "structuredClone", "queueMicrotask",
    "if", "for", "while", "switch", "catch", "return", "function", "typeof",
    "new", "await", "do", "else", "try", "throw", "case", "delete", "in", "of",
    "async", "yield", "void", "instanceof",
    "translateX", "translateY", "rgba", "rgb", "var", "url", "calc", "scale",
    "linear", "cubic", "matrix", "translate", "rotate", "blur", "constructor",
}

# Only PLAIN calls — `foo(` not preceded by a dot (those are methods) and not
# part of a declaration or a property key.
_called = set()
for m in re.finditer(r'(?<![.\w$"\'])([a-zA-Z_$][\w$]*)\s*\(', _script):
    _called.add(m.group(1))

_missing = sorted(n for n in _called - _declared - _GLOBALS)
# Anything reached through an inline onclick="" in the HTML must be global too.
_inline = set(re.findall(r'on(?:click|change|input)="([a-zA-Z_$][\w$]*)\(', _html))
_missing_inline = sorted(_inline - _declared)

for n in _missing:
    print(f"        calls {n}() — not declared in the page script")
for n in _missing_inline:
    print(f"        inline handler calls {n}() — not declared")
check(f"no call to an undeclared function ({_missing or 'none'})", not _missing)
check(f"every inline on* handler resolves ({_missing_inline or 'none'})",
      not _missing_inline)

shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
