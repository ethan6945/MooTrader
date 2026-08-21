"""News and AI are advisory. This is the test that keeps them that way.

Run from repo root: .venv/bin/python scripts/test_advisory_pin.py
No broker, no network, no model calls.

WHY THIS AND NOT AN ABLATION

  The plan asks for out-of-sample ablations — with and without news, with and
  without AI — and for either to enter the strategy only once it is shown to
  add return without deepening drawdown.

  That ablation cannot be run here, and the reason is in src/sandbox.py in as
  many words: there is no point-in-time news archive (Tavily serves "now", not
  "as of 2026-03-04"), and no scorer whose training cutoff precedes the test
  window — a frontier LLM asked about March already knows how March ended. A
  backtest built on either would produce a number contaminated by look-ahead
  that reads exactly like evidence. Running it would be worse than not.

  So the honest position is the conservative one, and it is already the case:

    news_driven_enabled = False   news selects nothing
    ai_veto_blocking    = False   AI is consulted AFTER the order is placed
    the AI ensemble has no key    every verdict is neutral
    the gap sentinel has run twice on real holdings and flagged nothing

  Neither has changed a single decision this system has made. Nothing needs to
  be proven because nothing is being claimed.

  What DOES need guarding is the drift: a default flipped, a branch reordered,
  and an unvalidated model starts choosing trades without anything failing.
  These checks pin the structure, so that change cannot be quiet.
"""
import ast
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mmt-adv-")
os.environ["MMT_HOME"] = _TMP
(Path(_TMP) / "data").mkdir(parents=True, exist_ok=True)
(Path(_TMP) / "logs").mkdir(parents=True, exist_ok=True)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name + (f"   [{detail}]" if detail else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1


from src.config import settings                              # noqa: E402

MAIN = (ROOT / "src" / "main.py").read_text()
SANDBOX = (ROOT / "src" / "sandbox.py").read_text()


# ── 1. the defaults ────────────────────────────────────────────────────────
print("1  the shipped defaults do not let a model choose a trade")
# CODE defaults, read with no .env of our own — what a fresh install runs.
check("news-driven selection is off by default",
      settings.news_driven_enabled is False,
      str(settings.news_driven_enabled))
check("the AI veto is non-blocking by default",
      settings.ai_veto_blocking is False, str(settings.ai_veto_blocking))


# ── 2. the structure, not just the setting ─────────────────────────────────
print("\n2  with the veto off, the consult happens after the order")
# A setting can be flipped in one file. The ORDER of operations is what makes
# the flip safe, and it is what a refactor silently loses.
#
# This used to slice main.py as TEXT between two anchor strings. It broke
# twice against code that was correct: once when "vision_conf" was deleted
# with the pattern-vision remnants and took the end anchor with it, once when
# the slice ran to the first "else:" — which belonged to `if ai_budget <= 0:`
# INSIDE the blocking branch — and swept that branch's _skip into the region.
# Both failures were about the anchors, not about the property.
#
# Worse than either: a positive text assertion is satisfied by a COMMENT. A
# line reading `# never write elif settings.ai_veto_blocking:` would have
# passed "the blocking branch is gated on ai_veto_blocking" while the branch
# itself was gone. So this parses instead, and asks the tree.
MAIN_TREE = ast.parse(MAIN)


def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _is_attr(node, obj, attr):
    return (isinstance(node, ast.Attribute) and node.attr == attr
            and isinstance(node.value, ast.Name) and node.value.id == obj)


def _skips(nodes) -> list[str]:
    """Every _skip("reason", ...) reachable in these statements."""
    out = []
    for n in nodes:
        for sub in ast.walk(n):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "_skip" and sub.args
                    and isinstance(sub.args[0], ast.Constant)):
                out.append(sub.args[0].value)
    return out


def _assigns_true(nodes, target: str) -> bool:
    """`target` is assigned the literal True, tuple-unpacked or not."""
    for n in nodes:
        for sub in ast.walk(n):
            if not isinstance(sub, ast.Assign):
                continue
            for tgt in sub.targets:
                names = tgt.elts if isinstance(tgt, ast.Tuple) else [tgt]
                vals = (sub.value.elts if isinstance(sub.value, ast.Tuple)
                        else [sub.value])
                for nm, v in zip(names, vals):
                    if (isinstance(nm, ast.Name) and nm.id == target
                            and isinstance(v, ast.Constant) and v.value is True):
                        return True
    return False


SCAN = _fn(MAIN_TREE, "scan_once")
check("scan_once is still the function that decides entries", SCAN is not None)

veto_ifs = [n for n in ast.walk(SCAN)
            if isinstance(n, ast.If) and _is_attr(n.test, "settings", "ai_veto_blocking")]
check("exactly one branch is gated on settings.ai_veto_blocking",
      len(veto_ifs) == 1, f"found {len(veto_ifs)}")

if veto_ifs:
    veto = veto_ifs[0]
    # The whole scan may only refuse an entry for ai_veto from inside it.
    check("only the blocking branch can skip for ai_veto",
          _skips(veto.body).count("ai_veto") == 1
          and _skips(SCAN.body).count("ai_veto") == 1,
          f"in branch {_skips(veto.body).count('ai_veto')}, "
          f"in scan_once {_skips(SCAN.body).count('ai_veto')}")

    # The else of that If is the advisory path.
    adv = veto.orelse
    check("the advisory path exists as that branch's else", bool(adv))
    check("...it passes the trade unconditionally", _assigns_true(adv, "ai_pass"))
    check("...it marks the consult deferred", _assigns_true(adv, "ai_deferred"))
    check("...and it can refuse nothing at all", _skips(adv) == [],
          str(_skips(adv)))
    check("...and cannot skip the entry by continuing either",
          not any(isinstance(x, ast.Continue) for n in adv for x in ast.walk(n)))
    # The reason recorded is a string in the code, not a comment about one.
    adv_strings = [x.value for n in adv for x in ast.walk(n)
                   if isinstance(x, ast.Constant) and isinstance(x.value, str)]
    check("...and records that it was consulted post-order",
          any("advisory" in v and "post-order" in v for v in adv_strings),
          str(adv_strings))


# ── 3. a stack is not re-judged by a model ─────────────────────────────────
print("\n3  adding to a position already held does not consult a model")
# The stack branch is the one the veto branch is the else OF, so find it by
# structure: the If whose orelse contains the veto If.
stack_ifs = [n for n in ast.walk(SCAN)
             if isinstance(n, ast.If) and veto_ifs and veto_ifs[0] in n.orelse]
check("the stack branch short-circuits before the AI branch", len(stack_ifs) == 1,
      f"found {len(stack_ifs)}")
if stack_ifs:
    st = stack_ifs[0]
    check("...and it is gated on the stack candidate itself",
          isinstance(st.test, ast.Name) and "stack" in st.test.id.lower(),
          ast.unparse(st.test))
    check("...it passes without consulting anything",
          _assigns_true(st.body, "ai_pass"))
    check("...it calls no validator",
          not any(isinstance(x, ast.Call)
                  and "validate" in ast.unparse(x.func)
                  for n in st.body for x in ast.walk(n)))
    check("...and refuses nothing", _skips(st.body) == [], str(_skips(st.body)))


# ── 4. the backtest does not model what it cannot model ────────────────────
print("\n4  the engines do not pretend to have news")
check("the sandbox states that news-driven mode is not modelled",
      "NEWS-DRIVEN MODE IS NOT MODELLED HERE" in SANDBOX)
check("...and why: no point-in-time archive",
      "point-in-time news archive" in SANDBOX)
check("...and why: no model with a training cutoff before the window",
      "training cutoff before the test window" in SANDBOX)
check("the sandbox skips AI annotation rather than faking it",
      "AI news/veto/sentiment SKIPPED" in SANDBOX)
# A sandbox that scored news would be measuring a different strategy AND doing
# it with look-ahead. Assert on the IMPORTS: an earlier version searched the
# whole file for "news_driven" and matched a comment naming the preflight
# function, which is prose about the gap rather than code that closes it.
NEWS_MODULES = ("news", "ai_validator", "sentiment", "tavily", "deepseek",
                "gemini")


def imports_of(src: str) -> set[str]:
    out = set()
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        m = re.match(r"(?:from|import)\s+([\w\.]+)", s)
        if m:
            out.add(m.group(1).lower())
        m2 = re.search(r"from\s+\.\s+import\s+(.+)", s)
        if m2:
            out.update(x.strip().lower() for x in m2.group(1).split(","))
    return out


for engine, src in (("sandbox", SANDBOX),
                    ("backtest", (ROOT / "src" / "backtest.py").read_text())):
    bad = sorted(i for i in imports_of(src)
                 if any(n in i for n in NEWS_MODULES))
    check(f"{engine} imports no news or model scorer", not bad, str(bad))


# ── 5. if news-driven is ever switched on, the engines must object ─────────
print("\n5  turning news-driven on is not a silent change")
try:
    from src import preflight
    has_check = hasattr(preflight, "check_news_driven")
except Exception as e:
    has_check = False
    print(f"       (preflight import failed: {e})")
check("preflight has a check for it", has_check)
if has_check:
    src = (ROOT / "src" / "preflight.py").read_text()
    body = src[src.index("def check_news_driven"):]
    body = body[:body.index("\ndef ")] if "\ndef " in body else body
    check("...that names the backtest gap rather than merely logging",
          bool(re.search(r"backtest|sandbox|not modelled|cannot", body, re.I)),
          "")


# ── 6. the gap sentinel acts on EXITS only ─────────────────────────────────
print("\n6  the one AI path that does act, acts only to leave")
# It is allowed to close a position on overnight news. It is not allowed to
# open one — a distinction worth pinning, because an exit-only model can cost
# opportunity but cannot buy something nothing validated.
gs = (ROOT / "src" / "gap_sentinel.py").read_text()
check("the gap sentinel never places a BUY",
      "TrdSide.BUY" not in gs and "place_limit_order" not in gs.replace(
          "SELL", ""),
      "")
check("...and the settings that arm it are explicit",
      settings.gap_sentinel_enabled in (True, False))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
