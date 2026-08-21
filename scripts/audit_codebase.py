"""Whole-codebase audit that reads structure, never prose.

    .venv/bin/python scripts/audit_codebase.py            # all checks
    .venv/bin/python scripts/audit_codebase.py --only env-shape
    .venv/bin/python scripts/audit_codebase.py --json out.json

WHY IT IGNORES COMMENTS AND DOCSTRINGS

  Every check here parses to an AST and drops docstrings before looking at
  anything. That is not fastidiousness — it is the failure mode this file was
  written after.

  A test asserting `'[sys.executable, "-m"' not in source` passed for the wrong
  reason and then failed for the wrong reason: the comment warning *never write
  this* is character-for-character the code it warns against. Another asserted
  a function "keeps no second copy of --worker" while its docstring still
  described --worker. Prose about code reads exactly like code.

  So: text is evidence of intent, and intent is not behaviour.

WHAT IT LOOKS FOR

  Each check corresponds to a bug this repository actually shipped, not to a
  style guide.

    env-shape     a branch on frozen-vs-source that only one of them exercises.
                  2.5.0 shipped with start_protocol spawning
                  `sys.executable -m src.main run`; frozen, that is the app's
                  own binary, which takes --worker and not -m, so it started a
                  second web server and died on the port. Every test ran under
                  a real interpreter, where the wrong branch is also right.

    twins         one rule implemented twice. kill_switch and risk_manager both
                  had reset_for_new_day writing the same key; only one learned
                  to keep a halt the clock cannot answer, and the scheduler
                  called the other. web/server and start_protocol both built a
                  worker command; only one knew about freezing.

    swallow       an except that logs and continues on a path that changed
                  state or reached the broker. All seven execution P0s were
                  this: settle_all collected failures nothing read, startup
                  recovery logged "continuing to protective exits".

    cap           a literal bound that does not scale with its input.
                  get_kline capped its window and returned 686 bars for 2590;
                  the sandbox asked for a flat 500 whatever the window;
                  the cache called 497 rows fresh enough for a 2435-row ask.

    unread        a dataclass field accepted and never read. SandboxConfig
                  carried `tickers` that _load_universe ignored, so a parity
                  run silently compared two different universes.

    dead          defined and never referenced, anywhere.

    prose-test    a test asserting against raw source text rather than parsed
                  structure — the mistake at the top of this docstring.

  A finding is a QUESTION, not a defect. The output is meant to be triaged.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("src", "web", "packaging", "scripts")
# Vendored and generated trees. Auditing PyInstaller's copy of pandas_ta_classic
# reports sixty modules that each define _detect() and calls it a duplicated
# rule — true, irrelevant, and enough noise to bury everything that matters.
EXCLUDE = re.compile(r'(^|/)(dist|build|_internal|\.build|node_modules|'
                     r'site-packages|__pycache__)(/|$)')
# First-party runtime. `twins` and `dead` only mean something here: two scripts
# both defining load() or report() is how standalone scripts are written, not a
# rule implemented twice.
RUNTIME_DIRS = ("src", "web")


# ── loading ────────────────────────────────────────────────────────────────

def py_files() -> list[Path]:
    out = []
    for d in SCAN_DIRS:
        out += sorted((ROOT / d).rglob("*.py"))
    return [p for p in out if not EXCLUDE.search(str(p.relative_to(ROOT)))]


def strip_docstrings(node: ast.AST) -> ast.AST:
    """Remove every docstring in the tree, in place."""
    for n in ast.walk(node):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef)):
            body = getattr(n, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                n.body = body[1:] or [ast.Pass()]
    return node


def load() -> dict[Path, ast.Module]:
    trees = {}
    for p in py_files():
        try:
            trees[p] = strip_docstrings(ast.parse(p.read_text()))
        except SyntaxError as e:
            print(f"  ! {p.relative_to(ROOT)} does not parse: {e}", file=sys.stderr)
    return trees


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


F = []          # findings


def finding(check, severity, path, line, what, why):
    F.append({"check": check, "severity": severity, "file": rel(path),
              "line": line, "what": what, "why": why})


# ── env-shape ──────────────────────────────────────────────────────────────

FROZEN_MARKERS = {"IS_FROZEN", "frozen", "BUNDLE_DIR", "MEIPASS"}


def check_env_shape(trees):
    """Branches that behave differently frozen, and whether a test drives both."""
    test_src = "\n".join(p.read_text() for p in py_files()
                         if p.name.startswith("test_"))
    for path, tree in trees.items():
        if path.name.startswith("test_"):
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.If):
                continue
            names = {x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)}
            attrs = {x.attr for x in ast.walk(n.test) if isinstance(x, ast.Attribute)}
            if not (names | attrs) & FROZEN_MARKERS:
                continue
            # Both arms present? Then the two shapes really do differ.
            has_else = bool(n.orelse)
            func = _enclosing_func(tree, n)
            covered = bool(func and re.search(rf'\b{re.escape(func)}\b', test_src))
            if not covered:
                finding("env-shape", "high" if has_else else "medium", path,
                        n.lineno,
                        f"frozen/source branch in {func or '<module>'}()",
                        "no test names this function; the shape that only the "
                        "packaged app takes is unexercised — this is how 2.5.0 "
                        "shipped a start protocol that could not start")


def _enclosing_func(tree, node):
    best = None
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if f.lineno <= node.lineno <= (f.end_lineno or f.lineno):
                if best is None or f.lineno > best.lineno:
                    best = f
    return best.name if best else None


# ── twins ──────────────────────────────────────────────────────────────────

def check_twins(trees):
    """One rule, two implementations."""
    by_name = defaultdict(list)
    for path, tree in trees.items():
        if path.name.startswith("test_"):
            continue
        if path.relative_to(ROOT).parts[0] not in RUNTIME_DIRS:
            continue
        for n in tree.body:                       # module level only
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                by_name[n.name].append((path, n))
    for name, defs in sorted(by_name.items()):
        if len(defs) < 2 or name.startswith("_") and len(defs) < 3:
            continue
        mods = {rel(p) for p, _ in defs}
        if len(mods) < 2:
            continue
        # A duplicated RULE has similar bodies. A polymorphic interface —
        # every strategy module defining evaluate(), every source defining
        # probe() — has different ones, and flagging those buries the real
        # finding under the deliberate design.
        bodies = [ast.unparse(n) for _, n in defs]
        delegating = any(other_mod.split("/")[-1][:-3] in b
                         for b in bodies for other_mod in mods)
        import difflib
        sim = 0.0
        for i in range(len(bodies)):
            for j in range(i + 1, len(bodies)):
                sim = max(sim, difflib.SequenceMatcher(
                    None, bodies[i], bodies[j]).ratio())
        if delegating:
            sev = "low"
        elif sim >= 0.55:
            sev = "high"
        elif sim >= 0.35:
            sev = "medium"
        else:
            continue          # different bodies: an interface, not a twin
        finding("twins", sev, defs[0][0], defs[0][1].lineno,
                f"{name}() in {len(mods)} modules (bodies {sim:.0%} alike): "
                f"{', '.join(sorted(mods))}",
                "two implementations of one rule drift apart, and the caller "
                "decides which one is authoritative by accident"
                + (" — one appears to delegate, so this may already be fine"
                   if delegating else ""))


# ── swallow ────────────────────────────────────────────────────────────────

BENIGN = (ast.Pass, ast.Continue, ast.Break)
# Touching one of these means the account, the ledger or the broker moved.
# A swallowed exception here is the shape all seven execution P0s had.
CRITICAL = re.compile(
    r'\b(place_|cancel_|modify_order|settle|halt|'
    r'update_state|atomic_state|set_param|set_budget|record_trade)\b')

# Persistent, but not money: a swallowed failure here loses data or leaves a
# file half-written. Worth reading, rarely urgent.
STATEFUL = re.compile(
    r'\b(execute|commit|write_text|to_parquet|upsert|save_|'
    r'record_|unlink|rename)\b')

# Deliberately NOT in either list: `replace` (str.replace and
# dataclasses.replace both RETURN a new value and mutate nothing), `insert`
# (list.insert is in-memory), `delete` (dict/attr deletion is too). They were
# in STATEFUL and produced false positives that buried the real ones.


def check_swallow(trees):
    """except blocks that log-and-continue over something consequential."""
    for path, tree in trees.items():
        if path.name.startswith("test_"):
            continue
        for h in ast.walk(tree):
            if not isinstance(h, ast.ExceptHandler):
                continue
            body_src = ast.unparse(ast.Module(body=h.body, type_ignores=[]))
            # Does it re-raise, halt, or return a failure? Then it is handled.
            if re.search(r'\b(raise|halt|sys\.exit)\b', body_src):
                continue
            only_noise = all(
                isinstance(s, BENIGN)
                or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Call))
                or isinstance(s, ast.Return)
                for s in h.body)
            if not only_noise:
                continue
            # What was being attempted?
            try_node = _enclosing_try(tree, h)
            attempted = ast.unparse(ast.Module(
                body=try_node.body, type_ignores=[])) if try_node else ""
            crit = bool(CRITICAL.search(attempted))
            if not crit and not STATEFUL.search(attempted):
                continue
            # Handing the caller a failure VALUE is handling it, not swallowing
            # it — `return jsonify({"ok": False}), 400` and `return False` both
            # say so plainly. A bare `return` (or `return None`) does not: that
            # is the degrade-quietly shape, and it stays flagged.
            if any(isinstance(st, ast.Return)
                   and st.value is not None
                   and not (isinstance(st.value, ast.Constant)
                            and st.value.value is None)
                   for st in h.body):
                continue
            func = _enclosing_func(tree, h)
            finding("swallow", "high" if crit else "medium", path, h.lineno,
                    f"except in {func or '<module>'}() logs and continues over "
                    + ("an account/ledger/broker write" if crit
                       else "a state change"),
                    "the seven execution P0s were all this shape: the write "
                    "half-happened, nothing raised, and the next decision was "
                    "made on a record that no longer described the account")


def _enclosing_try(tree, handler):
    for t in ast.walk(tree):
        if isinstance(t, ast.Try) and handler in t.handlers:
            return t
    return None


# ── cap ────────────────────────────────────────────────────────────────────

CAP_KW = {"limit", "max_count", "bars", "maxlen", "top_n", "n", "count",
          "max_bars", "lookback", "days"}


def check_cap(trees):
    """Literal bounds that do not move with their input."""
    for path, tree in trees.items():
        if path.name.startswith("test_"):
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            for kw in n.keywords:
                if kw.arg in CAP_KW and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, int) \
                        and kw.value.value >= 100:
                    func = _enclosing_func(tree, n)
                    finding("cap", "low", path, n.lineno,
                            f"{kw.arg}={kw.value.value} literal in "
                            f"{func or '<module>'}()",
                            "a bound that does not scale with the window is how "
                            "a 360-day backtest ran on 141 days, and how the "
                            "sandbox replayed the same hundred days twice")


# ── unread dataclass fields ────────────────────────────────────────────────

def check_unread(trees):
    """Config fields accepted and never read."""
    all_src = "\n".join(ast.unparse(t) for t in trees.values())
    for path, tree in trees.items():
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            # A dataclass serialised with asdict() has every field read at
            # once, by name-less machinery. preflight.Check does exactly that,
            # and reporting six of its fields as unread is a false positive
            # that buries the real ones.
            cls_src = ast.unparse(cls)
            if "asdict(" in cls_src or "__dict__" in cls_src:
                continue
            if not any(isinstance(d, ast.Name) and d.id == "dataclass"
                       or isinstance(d, ast.Call) and
                       getattr(d.func, "id", "") == "dataclass"
                       for d in cls.decorator_list):
                continue
            for stmt in cls.body:
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                name = getattr(stmt.target, "id", None)
                if not name or name.startswith("_"):
                    continue
                # A field can be read by attribute, by getattr with a literal,
                # or by name through a config dict. All three count.
                reads = (len(re.findall(rf'\.{re.escape(name)}\b', all_src))
                         + len(re.findall(rf'["\']{re.escape(name)}["\']', all_src)))
                if reads == 0:
                    finding("unread", "high", path, stmt.lineno,
                            f"{cls.name}.{name} is never read",
                            "SandboxConfig.tickers was exactly this: accepted, "
                            "documented by its presence, and ignored — so a "
                            "parity run compared two different universes and "
                            "read as total disagreement")


# ── dead ───────────────────────────────────────────────────────────────────

def check_dead(trees):
    all_src = "\n".join(ast.unparse(t) for t in trees.values())
    for path, tree in trees.items():
        if path.name.startswith("test_") or path.parent.name == "scripts":
            continue
        for n in tree.body:
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if n.name.startswith("__") or n.name == "main":
                continue
            uses = len(re.findall(rf'\b{re.escape(n.name)}\b', all_src))
            if uses <= 1:                     # only its own definition
                finding("dead", "low", path, n.lineno,
                        f"{n.name}() has no caller anywhere",
                        "dead code is not free: it is read as behaviour during "
                        "review and it survives refactors that would have "
                        "broken it")


# ── prose-test ─────────────────────────────────────────────────────────────

def check_prose_test(trees):
    """Tests that assert against raw source text."""
    for path, tree in trees.items():
        if not path.name.startswith("test_"):
            continue
        src = ast.unparse(tree)
        if "read_text()" not in src:
            continue
        # ast.parse / ast.unparse present means it reads structure somewhere
        structural = "ast.parse" in src or "ast.unparse" in src or "code_only" in src
        if structural:
            continue
        n_cmp = len(re.findall(r'\bin \w+_?src\b|\bin src\b|\bin body\b|\bin blk\b', src))
        finding("prose-test", "medium" if n_cmp else "low", path, 1,
                f"{path.name} asserts against file text, not parsed structure",
                "a comment warning 'never write X' is character-for-character "
                "the X it warns against; this check exists because two "
                "assertions in this repo failed against correct code for "
                "exactly that reason")


# ── hazards ────────────────────────────────────────────────────────────────

def check_hazards(trees):
    for path, tree in trees.items():
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in n.args.defaults + [k for k in n.args.kw_defaults if k]:
                    if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                        finding("hazard", "high", path, n.lineno,
                                f"{n.name}() has a mutable default argument",
                                "shared across every call; state leaks between "
                                "unrelated invocations")
            if isinstance(n, ast.ExceptHandler) and n.type is None:
                if not path.name.startswith("test_"):
                    finding("hazard", "medium", path, n.lineno,
                            "bare except catches KeyboardInterrupt and SystemExit",
                            "a stop request becomes an ignored error")


CHECKS = {
    "env-shape": check_env_shape, "twins": check_twins, "swallow": check_swallow,
    "cap": check_cap, "unread": check_unread, "dead": check_dead,
    "prose-test": check_prose_test, "hazard": check_hazards,
}
ORDER = {"high": 0, "medium": 1, "low": 2}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", nargs="*", choices=sorted(CHECKS))
    ap.add_argument("--json", default=None)
    ap.add_argument("--severity", choices=["high", "medium", "low"], default="low")
    args = ap.parse_args()

    trees = load()
    print(f"parsed {len(trees)} modules, docstrings stripped\n")
    for name, fn in CHECKS.items():
        if args.only and name not in args.only:
            continue
        fn(trees)

    keep = [f for f in F if ORDER[f["severity"]] <= ORDER[args.severity]]
    keep.sort(key=lambda f: (ORDER[f["severity"]], f["check"], f["file"], f["line"]))

    by = defaultdict(list)
    for f in keep:
        by[(f["severity"], f["check"])].append(f)
    for (sev, chk), items in sorted(by.items(), key=lambda kv: (ORDER[kv[0][0]], kv[0][1])):
        print(f"── {sev.upper():6} {chk}  ({len(items)})")
        for f in items[:12]:
            print(f"   {f['file']}:{f['line']}  {f['what']}")
        if len(items) > 12:
            print(f"   … and {len(items) - 12} more")
        print(f"   ↳ {items[0]['why']}\n")

    counts = defaultdict(int)
    for f in keep:
        counts[f["severity"]] += 1
    print("=" * 70)
    print(f"  {counts['high']} high · {counts['medium']} medium · {counts['low']} low"
          f"   ({len(keep)} findings)")
    print("  A finding is a question, not a defect. Triage before acting.")
    if args.json:
        Path(args.json).write_text(json.dumps(keep, indent=2))
        print(f"  written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
