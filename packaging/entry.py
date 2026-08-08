"""Entry point for the frozen backend — the one binary the packaged .app ships.

In the repo, four kinds of process exist: the Flask web server, the trading
scheduler (`python -m src.main run`), the signal reporter, and the self-review
worker. Each was spawned through `.venv/bin/python -m <module>`. A packaged
install has no venv and no interpreter on PATH, so all four collapse into this
single binary, and argv decides the role:

    MooTraderBackend                          → Flask web server (default)
    MooTraderBackend --worker src.main run    → same as `python -m src.main run`
    MooTraderBackend --import-check           → verify the frozen dependency graph

web.server._worker_cmd() builds the second form from sys.executable, which under
PyInstaller is this binary. runpy hands the target module the exact argv it
would have seen under `python -m`, so src/main.py and src/signal_reporter.py
keep their existing `if __name__ == "__main__"` blocks with no frozen-only
branches of their own.
"""
from __future__ import annotations

import multiprocessing
import runpy
import sys
from pathlib import Path


# Every module the running bot needs that PyInstaller could plausibly miss:
# dynamic importers, lazily-imported engine modules, and the two worker entry
# points nothing statically references. A missing hidden import otherwise shows
# up hours later as a scheduler that dies on its first grid sweep.
_CRITICAL = (
    "pandas", "numpy", "pyarrow", "pyarrow.parquet", "pandas_ta_classic",
    "moomoo", "yfinance", "optuna", "sqlalchemy",
    "apscheduler.schedulers.background", "flask", "dotenv", "requests",
    "google.genai",
    "src.main", "src.signal_reporter", "src.optimize_system", "src.sandbox",
    "src.optimizer", "src.executor", "src.moo_client", "src.ai", "src.db",
    "web.server",
    # News-driven mode and its sources (2026-08-07). All default OFF, but a
    # missing import here would surface as a mode that silently never fires
    # rather than as a build failure — which is the whole point of this check.
    "src.news_driven", "src.news_fetcher", "src.finnhub_news", "src.moo_notices",
    "src.news_score_local",
    # FinBERT's runtime. Imported lazily inside functions so the PyInstaller
    # graph walk cannot see them; they are in the spec's hiddenimports, and
    # this is what proves the spec entry actually worked.
    "onnxruntime", "tokenizers",
)


def _import_check() -> int:
    """Import every critical module and prove parquet actually round-trips.

    Imports only — never runs a module as __main__, which would start a real
    optimisation study or scheduler.
    """
    import importlib
    import traceback

    failed = []
    for name in _CRITICAL:
        try:
            importlib.import_module(name)
            print(f"  ok    {name}")
        except Exception as e:
            failed.append(name)
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")

    # pyarrow can import and still fail to write: the parquet codepath pulls in
    # compiled extensions the analyser resolves separately. src/sandbox.py caches
    # every K-line bar this way, so a broken round-trip breaks the grid sweeps.
    try:
        import tempfile

        import pandas as pd

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.parquet"
            pd.DataFrame({"a": [1.0, 2.0]}).to_parquet(p)
            assert len(pd.read_parquet(p)) == 2
        print("  ok    parquet round-trip")
    except Exception:
        failed.append("parquet round-trip")
        traceback.print_exc()

    print(f"\n{len(_CRITICAL) + 1 - len(failed)}/{len(_CRITICAL) + 1} passed")
    return 1 if failed else 0


def main() -> None:
    # Must run before anything else. PyInstaller starts multiprocessing children
    # by re-executing this binary; without freeze_support() the child re-enters
    # this entry point and boots a second web server instead of doing its job.
    multiprocessing.freeze_support()

    argv = sys.argv[1:]
    if argv and argv[0] == "--import-check":
        sys.exit(_import_check())
    if argv and argv[0] == "--worker":
        if len(argv) < 2:
            print("--worker requires a module name", file=sys.stderr)
            sys.exit(2)
        module, args = argv[1], argv[2:]
        # `python -m <module> <args…>` argv shape. alter_sys=True then rewrites
        # argv[0] to the module's own path, exactly as -m does, so any code
        # reading sys.argv[0] sees what it always has.
        sys.argv = [module, *args]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return

    from web.server import main as server_main

    server_main()


if __name__ == "__main__":
    main()
