# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the MooTrader backend.

Build with:  .venv/bin/pyinstaller packaging/mmt-backend.spec --noconfirm
Output:      packaging/dist/backend/  (binary + _internal/), copied by
             macos/build.sh into MooTrader.app/Contents/Resources/backend/.

onedir, not onefile: onefile re-extracts the whole ~400 MB archive to a temp
dir on every launch, and this binary re-execs ITSELF for each worker (scheduler,
signal reporter, self-review) — four processes would mean four extractions.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

# collect_submodules() has to IMPORT a package to enumerate it, and it does so
# in PyInstaller's own interpreter — where `pathex` does not apply, because that
# only feeds the analysis of the target program. Without this line
# collect_submodules("src") silently returns an empty list (it swallows the
# ImportError), the src hidden imports vanish, and the build still succeeds:
# every src module reachable from web.server gets pulled in by static analysis
# anyway, so the only casualties are the handful nothing imports — including
# src.signal_reporter, a worker entry point. That failure surfaces as a
# ModuleNotFoundError the first time the user runs the signal reporter.
sys.path.insert(0, str(ROOT))

# ── bundled resources ────────────────────────────────────────────────────────
# Read-only, reached through src.config.BUNDLE_DIR. Mutable state (.env, data/,
# logs/) is NEVER bundled — it lives in ~/Library/Application Support and, for
# .env and data/, would leak the developer's API keys and live trade history
# into a public release.
datas = [(str(ROOT / "web" / "static"), "web/static")]
# Config templates, seeded into the app home on first run by src/config.py.
# *.json only — it skips watchlist.json.bak.
datas += [(str(p), "config") for p in sorted((ROOT / "config").glob("*.json"))]
# yfinance/moomoo ship certs and .proto-derived data files that no import
# statement mentions.
datas += collect_data_files("yfinance")
datas += collect_data_files("moomoo")
# pandas_ta_classic builds its Category dict at import time by ITERATING ITS OWN
# PACKAGE DIRECTORY (_meta.py:_build_category_dict) — one .py per indicator. In
# a normal PyInstaller build those modules live in the PYZ archive with no
# directory on disk, so the scan raises FileNotFoundError and every indicator
# import in src/ dies with it. Shipping the sources as data gives the scan a
# real tree to walk; imports still resolve from the PYZ.
datas += collect_data_files("pandas_ta_classic", include_py_files=True)

# ── imports the analyser cannot see ──────────────────────────────────────────
hiddenimports = [
    # Worker modules: reached only through runpy at runtime (entry.py), so
    # nothing statically imports them and the graph walk would miss the entire
    # trading engine. Collecting the whole package also covers the lazy
    # in-function imports (src.optimize_system, src.sandbox, src.optimizer).
    *collect_submodules(
        "src",
        # matplotlib's only importer in the repo; excluded below. Nothing
        # imports pattern_vision itself, so dropping it costs no behaviour.
        filter=lambda name: name != "src.pattern_vision",
    ),
    # pandas_ta_classic imports its indicator categories dynamically by name.
    *collect_submodules("pandas_ta_classic"),
    # Broker SDK — protobuf message modules are resolved at runtime.
    *collect_submodules("moomoo"),
    # APScheduler 3.x resolves triggers/executors/jobstores through entry points.
    *collect_submodules("apscheduler"),
    # Optuna picks storage backends at runtime; SQLAlchemy loads the sqlite
    # dialect by string name.
    *collect_submodules("optuna"),
    "sqlalchemy.dialects.sqlite",
    # pyarrow backs the parquet K-line cache in src/sandbox.py, which the daily
    # and weekly grid sweeps run from the scheduler — this is a live path, not
    # an optional extra.
    "pyarrow",
    "pyarrow.parquet",
    "pandas.io.parquet",
    # AI provider (src/ai.py) — `from google import genai` is a namespace pkg.
    "google.genai",
    "google.genai.types",
]

# ── excluded ─────────────────────────────────────────────────────────────────
# ~53 MB of plotting stack whose only consumer is src/pattern_vision.py, which
# nothing in the repo imports (verified: no static import, no importlib call)
# and whose PATTERN_VISION_ENABLED flag defaults to false. pillow and fontTools
# are matplotlib dependencies with no other consumer here.
excludes = [
    "matplotlib",
    "PIL",
    "fontTools",
    # Dev/test tooling that dependencies drag in.
    "tkinter",
    "pytest",
    "IPython",
    "jupyter",
    "notebook",
    "pandas.tests",
    "numpy.f2py",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MooTraderBackend",
    debug=False,
    strip=False,
    upx=False,
    # console=True: this is a background process the Swift shell spawns and
    # redirects to logs/web.log — not a GUI app. The .app wrapper around it is
    # what the user actually launches.
    console=True,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="backend",
)
