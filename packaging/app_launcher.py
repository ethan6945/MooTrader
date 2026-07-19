"""PyInstaller entry script for MooMooTrader.app.

Must stay tiny: freeze_support() first (frozen multiprocessing children
re-exec this binary), then hand over to the real entry point which also
handles `--worker <module>` re-execs.
"""
import multiprocessing

multiprocessing.freeze_support()

from src.desktop_app import main  # noqa: E402

main()
