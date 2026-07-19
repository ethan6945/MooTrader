"""MooMooTrader.app entry point — native window hosting the web dashboard.

One binary, two modes:

  MooMooTrader                      → GUI: start the Flask dashboard in a
                                      thread, auto-start OpenD, then open a
                                      native (WKWebView) window on it. Closing
                                      the window quits the GUI only — the
                                      trading scheduler / menu-bar icon are
                                      separate detached processes and keep
                                      running, exactly like the dev setup.

  MooMooTrader --worker <mod> [a…]  → re-exec as a background worker
                                      (src.main run, src.menubar_app,
                                      src.signal_reporter …). Used by
                                      web/server.py's _worker_cmd(), replacing
                                      dev's `.venv/bin/python -m <mod>`.

In dev this file also works: `python -m src.desktop_app` gives the same
windowed experience on top of the repo checkout.
"""
from __future__ import annotations

import os
import runpy
import socket
import sys
import threading
import time
import webbrowser


def _dispatch_worker() -> bool:
    """`<binary> --worker <module> [args…]` → become that module."""
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        module, args = sys.argv[2], sys.argv[3:]
        sys.argv = [module, *args]
        runpy.run_module(module, run_name="__main__")
        return True
    return False


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    if _dispatch_worker():
        return

    from src.config import ROOT  # noqa: F401 — sets up app home / .env / dirs
    from web import server as web_server

    port = int(os.getenv("WEB_PORT", "8770"))
    if not _port_open(port):
        # Same boot rules as web/server.py main(): env override → .env → local;
        # never expose beyond localhost without a password.
        host = os.getenv("WEB_HOST") or web_server._read_env().get("WEB_HOST") or "127.0.0.1"
        if host != "127.0.0.1" and not web_server._web_password():
            host = "127.0.0.1"
        try:
            (web_server.ROOT / "logs").mkdir(parents=True, exist_ok=True)
            # Register as "the web server" so the existing Exit-UI /pid logic
            # (which kills logs/web.pid) also quits this GUI process.
            (web_server.ROOT / "logs" / "web.pid").write_text(str(os.getpid()))
        except Exception:
            pass
        for boot_step in (web_server._self_review_catchup_on_boot,
                          lambda: web_server._launch_menubar()
                          if web_server._scheduler_running() else None,
                          web_server._start_opend):
            try:
                boot_step()
            except Exception as e:
                print(f"boot step skipped: {e}", file=sys.stderr)
        threading.Thread(
            # load_dotenv=False: Flask would otherwise pull .env from the CWD
            # into os.environ — env loading is owned by src/config.py alone.
            target=lambda: web_server.app.run(host=host, port=port, threaded=True,
                                              load_dotenv=False),
            daemon=True,
        ).start()
        for _ in range(100):                      # wait ≤10s for the server
            if _port_open(port):
                break
            time.sleep(0.1)

    url = f"http://127.0.0.1:{port}"
    try:
        import webview  # pywebview — native WKWebView window
        webview.create_window(
            "MooMoo Trader", url,
            width=1300, height=880, min_size=(940, 620),
        )
        webview.start()
    except Exception as e:
        # No pywebview (or it failed) → plain browser tab; keep the process
        # alive so the embedded server keeps serving.
        print(f"pywebview unavailable ({e}) — falling back to the browser", file=sys.stderr)
        webbrowser.open(url)
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
