"""How the app launches its own worker — the one shape no test ever checked.

Run from repo root: .venv/bin/python scripts/test_worker_cmd.py
No broker, no subprocess.

THE INCIDENT

  2.5.0 installed, and the scheduler would not start. prepare() passed every
  check — schema v7, integrity ok, config hash matching — and then:

      worker_exited: the worker exited with code 1 before reporting ready

  start_protocol.commit() built its own default command,
  [sys.executable, "-m", "src.main", "run"]. Under PyInstaller sys.executable
  is the app's own binary, and packaging/entry.py recognises exactly two
  switches: --import-check and --worker. `-m` is neither, so it fell through to
  the default branch and started a SECOND web server, which lost the race for
  port 8770 and exited 1.

  Every test of start_protocol ran under a real Python interpreter, where the
  wrong branch happens to also be the right one. The frozen shape was reachable
  only by installing the .app, which no suite does.

  web/server.py had this right the whole time, in a function with a docstring
  explaining precisely this. There were two implementations of one rule and
  only one of them knew. That is the same fault as the two daily-rollover
  functions, and it produced the same kind of outage.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ast                                                    # noqa: E402


def code_only(path: Path, func: str | None = None) -> str:
    """Source with comments and docstrings removed.

    Assertions in this file kept matching the PROSE — the comment saying
    'never [sys.executable, "-m", ...]' reads identically to the code it warns
    against, and web/server's docstring names --worker while its body no longer
    does. Unparsing the AST leaves only what runs.
    """
    tree = ast.parse(path.read_text())
    if func:
        tree = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == func)
        if (tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)
                and isinstance(tree.body[0].value.value, str)):
            tree.body = tree.body[1:]        # drop the docstring
    return ast.unparse(tree)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(("  ok  " if cond else " FAIL ") + name + (f"   [{detail}]" if detail else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1


import src.config as cfg                                      # noqa: E402


def cmd_when_frozen(frozen: bool, *a):
    was = cfg.IS_FROZEN
    cfg.IS_FROZEN = frozen
    try:
        return cfg.worker_cmd(*a)
    finally:
        cfg.IS_FROZEN = was


# ── 1. the frozen shape entry.py can actually dispatch ─────────────────────
print("1  a frozen build re-execs itself with --worker")
c = cmd_when_frozen(True, "src.main", "run")
check("the switch is --worker", c[1] == "--worker", " ".join(c[1:]))
check("...never -m, which entry.py does not take", "-m" not in c, " ".join(c))
check("the module comes next", c[2] == "src.main", str(c[2:3]))
check("...and its arguments after it", c[3:] == ["run"], str(c[3:]))
check("it re-execs THIS binary", c[0] == sys.executable, c[0])


# ── 2. the dev shape still uses the repo venv ──────────────────────────────
print("\n2  a source checkout uses its own interpreter")
c = cmd_when_frozen(False, "src.main", "run")
check("-m is used here", c[1] == "-m", " ".join(c[1:]))
check("...with the venv python, not whatever is running",
      c[0].endswith("/.venv/bin/python"), c[0])
check("the module and args are unchanged", c[2:] == ["src.main", "run"], str(c[2:]))


# ── 3. every caller goes through the one definition ────────────────────────
print("\n3  one rule, not one per caller")
sp = code_only(ROOT / "src" / "start_protocol.py", "commit")
check("start_protocol builds no command of its own",
      "'-m'" not in sp and '"-m"' not in sp, "")
check("...it calls the shared builder", "_build_worker_cmd" in sp)

ws = code_only(ROOT / "web" / "server.py", "_worker_cmd")
check("web/server keeps no second copy",
      "IS_FROZEN" not in ws and "--worker" not in ws, ws.replace("\n", " ")[:70])
check("...it delegates too", "worker_cmd" in ws)

cf = code_only(ROOT / "src" / "config.py", "worker_cmd")
check("the shared rule is the only place both shapes appear",
      "--worker" in cf and "-m" in cf and "IS_FROZEN" in cf)


# ── 4. entry.py really only takes those two switches ───────────────────────
print("\n4  the dispatch this has to match")
ep = (ROOT / "packaging" / "entry.py").read_text()
main = ep[ep.index("def main()"):]
switches = {s for s in ("--import-check", "--worker", "-m", "--run", "run")
            if f'argv[0] == "{s}"' in main}
check("entry.py dispatches --import-check", "--import-check" in switches)
check("...and --worker", "--worker" in switches)
check("...and nothing else — so -m WOULD fall through",
      switches == {"--import-check", "--worker"}, str(sorted(switches)))
# The fallthrough is the web server, which is why the failure looked like a
# port conflict rather than a bad argument.
tail = main[main.index('argv[0] == "--worker"'):]
check("the fallthrough starts the web server",
      "web.server" in tail or "server" in tail)


# ── 5. the command survives a round trip through the protocol ──────────────
print("\n5  what commit() would actually spawn")
import src.start_protocol as spm                              # noqa: E402
src_txt = (ROOT / "src" / "start_protocol.py").read_text()
line = [l for l in src_txt.splitlines() if "cmd = worker_cmd or" in l]
check("commit builds its default from the shared rule", len(line) == 1, str(line))
check("...and an explicit worker_cmd still overrides it",
      "worker_cmd or" in line[0], line[0].strip() if line else "")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
