#!/bin/bash
# Double-click to run MooTrader.
#
#   OpenD (launch + wait for 11111) → web panel (detached) → browser → close
#   this window.
#
# The trading scheduler is a SEPARATE process: press ▶ in the panel to start
# it. ■ Stop there asks whether to stop just the scheduler or quit everything.
# To stop from the Finder instead, double-click macos-stop-web.command.
#
# Restarts the web server rather than reusing a running one. Flask does not
# reload changed code, so "already running" would silently serve whatever was
# current when it started — and the whole reason to run from this folder is
# that edits here take effect.

cd "$(dirname "$0")" || exit 1
mkdir -p logs

# macOS hands launchd-spawned processes a 256-fd soft limit, and both the panel
# and the scheduler it spawns are long-lived processes that talk to yfinance —
# whose worker threads open a SQLite connection each. The leak itself is fixed
# at the call sites (gc after the batch download), but 256 is a thin margin for
# any all-day process, so raise the ceiling for everything started from here.
# Hard limit on macOS is unlimited, so this needs no privileges.
ulimit -n 4096 2>/dev/null || true

PORT=${WEB_PORT:-8770}
PY=".venv/bin/python3"
say() { printf '  %s\n' "$*"; }

echo
echo "  MooTrader"
echo "  ─────────"

[ -x "$PY" ] || { say "✗ no interpreter at $PY"; say "  run:  uv venv && uv pip install -r requirements.txt"; echo; read -r -p "  Press return to close. "; exit 1; }
[ -f .env ]  || { say "✗ no .env — copy .env.example and fill it in"; echo; read -r -p "  Press return to close. "; exit 1; }

set -a; . ./.env; set +a

# ── OpenD ───────────────────────────────────────────────────────────────────
# 2026-07-09: the OFFICIAL moomoo_OpenD.app, deliberately. The headless
# OpenD-rs gateway (v1.4.122) silently dropped every order — need_op_confirm
# stub with order_id=0, purged after ~30s — so each buy became a fake
# MANUAL_SELL ghost and a re-buy loop. The official app keeps its own login
# session; this only launches it and waits for the port.
if nc -z 127.0.0.1 11111 2>/dev/null; then
    say "✓ OpenD already up on 127.0.0.1:11111"
else
    say "→ launching moomoo_OpenD…"
    open -a moomoo_OpenD 2>/dev/null
    ok=0
    for i in $(seq 1 60); do
        if nc -z 127.0.0.1 11111 2>/dev/null; then say "✓ OpenD ready (${i}s)"; ok=1; break; fi
        sleep 1
    done
    if [ "$ok" -eq 0 ]; then
        say "✗ OpenD did not open port 11111 within 60s"
        say "  finish the login in the OpenD window, then run this again."
        echo; read -r -p "  Press return to close. "; exit 1
    fi
fi

# ── web panel ───────────────────────────────────────────────────────────────
if [ -f logs/web.pid ] && kill -0 "$(cat logs/web.pid 2>/dev/null)" 2>/dev/null; then
    kill "$(cat logs/web.pid)" 2>/dev/null; sleep 1
fi
pkill -f "web/server.py" 2>/dev/null; sleep 1

say "→ starting the panel…"
# setsid where available, so closing this window (two steps below, on purpose)
# cannot take the server with it. nohup alone survives too.
if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$PY" web/server.py >> logs/web.log 2>&1 &
else
    nohup "$PY" web/server.py >> logs/web.log 2>&1 &
fi
WEB_PID=$!
echo "$WEB_PID" > logs/web.pid
disown "$WEB_PID" 2>/dev/null || true

# Wait for it to ANSWER. A pid that is about to die of a port conflict or a bad
# .env looks exactly like a healthy one for the first second, and imports
# (src.ai, src.db) take 3–6s cold.
for i in $(seq 1 40); do
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/favicon.ico" >/dev/null 2>&1; then
        say "✓ panel on http://127.0.0.1:$PORT  (pid $WEB_PID)"
        open "http://127.0.0.1:$PORT"
        sleep 1
        osascript -e 'tell application "Terminal" to close (every window whose name contains "macos-start-web")' >/dev/null 2>&1 &
        exit 0
    fi
    kill -0 "$WEB_PID" 2>/dev/null || break
    sleep 0.5
done

# Failed. Keep the window — showing why is the only reason it exists.
say "✗ the panel did not come up within 20s"
echo
say "last lines of logs/web.log:"
tail -n 15 logs/web.log 2>/dev/null | sed 's/^/      /'
rm -f logs/web.pid
echo
read -r -p "  Press return to close. "
