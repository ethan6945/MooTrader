# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for MooMooTrader.app.

Build with:  scripts/build_app.command   (repo root)

Notes:
  * All of src/ + web/ are collected wholesale as hidden imports because the
    app re-execs itself with `--worker src.main run` etc. — runpy targets are
    invisible to PyInstaller's static analysis.
  * web/static and config templates ship as read-only bundle data; mutable
    state lives in ~/Library/Application Support/MooMooTrader (src/config.py).
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

REPO = os.path.abspath(os.path.join(SPECPATH, ".."))

hiddenimports = collect_submodules("src") + ["web", "web.server"]

datas = [
    (os.path.join(REPO, "web", "static"), os.path.join("web", "static")),
    (os.path.join(REPO, "config", "watchlist.json"), "config"),
    (os.path.join(REPO, "config", "signal_watchlist.json"), "config"),
    (os.path.join(REPO, "config", "universe_pool.json"), "config"),
]
binaries = []

# Packages with data files / dynamic imports that static analysis misses.
for _pkg in ("moomoo", "pandas_ta_classic", "yfinance"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    [os.path.join(REPO, "packaging", "app_launcher.py")],
    pathex=[REPO],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MooMooTrader",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="MooMooTrader")

app = BUNDLE(
    coll,
    name="MooMooTrader.app",
    icon=os.path.join(REPO, "packaging", "MooMooTrader.icns"),
    bundle_identifier="com.moomootrader.desktop",
    info_plist={
        "CFBundleDisplayName": "MooMoo Trader",
        "CFBundleShortVersionString": "1.0.0",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.finance",
        # The window loads http://127.0.0.1:<port> (local Flask) inside WKWebView.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
