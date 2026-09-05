#!/bin/bash
# Double-click to stop MooTrader completely.
#
# Order matters and it is not arbitrary:
#
#   1. the trading worker, through the start protocol — it closes the session
#      and releases the lease together. Killing the process alone leaves a
#      session with no ended_at and a lease naming a dead pid, which the next
#      start has to break before it can proceed.
#   2. OpenD, if this software started it
#   3. the web server itself
#
# Stopping the web server first would remove the thing that knows how to stop
# the worker properly.
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO" || exit 1

PORT="${WEB_PORT:-8770}"
PY="$REPO/.venv/bin/python3"
PIDFILE="$REPO/logs/web.pid"

say() { printf '  %s\n' "$*"; }
echo
echo "  MooTrader — stopping everything"
echo "  ───────────────────────────────"

# ── 1. the worker, through the protocol ────────────────────────────────────
if [ -x "$PY" ]; then
    say "stopping the trading worker…"
    MMT_HOME="$REPO" PYTHONPATH="$REPO" "$PY" - <<'PY' 2>/dev/null | sed 's/^/  /'
import sys
sys.path.insert(0, ".")
try:
    from src import start_protocol
    r = start_protocol.stop("stop-command")
    print("worker: stopped" if r.get("stopped") else "worker: was not running")
except Exception as e:
    print(f"worker: could not stop through the protocol ({e})")
PY
fi

# ── 2. OpenD, only if it is ours to stop ───────────────────────────────────
if pgrep -f "moomoo_OpenD" >/dev/null 2>&1; then
    say "leaving OpenD running (start it and stop it yourself)"
fi

# ── 3. the web server ──────────────────────────────────────────────────────
stopped_web=0
if [ -f "$PIDFILE" ]; then
    WEB_PID="$(cat "$PIDFILE" 2>/dev/null)"
    if [ -n "$WEB_PID" ] && kill -0 "$WEB_PID" 2>/dev/null; then
        kill -TERM "$WEB_PID" 2>/dev/null
        for _ in $(seq 1 20); do
            kill -0 "$WEB_PID" 2>/dev/null || break
            sleep 0.25
        done
        kill -0 "$WEB_PID" 2>/dev/null && kill -9 "$WEB_PID" 2>/dev/null
        stopped_web=1
    fi
    rm -f "$PIDFILE"
fi

# Anything still on the port that we did not start — a server from an earlier
# session, or one launched by hand. Named rather than killed silently.
LEFTOVER="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -5)"
if [ -n "$LEFTOVER" ]; then
    for p in $LEFTOVER; do
        CMD="$(ps -o command= -p "$p" 2>/dev/null | cut -c1-60)"
        case "$CMD" in
            *web/server.py*|*MooTraderBackend*)
                say "stopping web server on port $PORT (pid $p)"
                kill -TERM "$p" 2>/dev/null
                sleep 1
                kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null
                stopped_web=1 ;;
            *)
                say "⚠ pid $p holds port $PORT and is not ours — left alone:"
                say "   $CMD" ;;
        esac
    done
fi

[ "$stopped_web" -eq 1 ] && say "✓ web server stopped" || say "web server: was not running"

# ── what is left ───────────────────────────────────────────────────────────
echo
# Check FACTS, not command lines. pgrep -f matches the whole command line, so
# any shell that merely mentions these patterns — including the one running
# this script, and any sibling that was launched with them in its arguments —
# counts as a hit. The first version of this reported "1 process still alive"
# and then printed nothing, because the thing it had found was itself.
#
# The port and the lease are what actually say whether the software is up.
LEFT=""
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
    LEFT="$LEFT web-server(port $PORT)"
fi
if [ -f "$REPO/logs/worker.lease" ]; then
    LPID="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1])).get('pid',''))" \
            "$REPO/logs/worker.lease" 2>/dev/null)"
    if [ -n "$LPID" ] && kill -0 "$LPID" 2>/dev/null; then
        LEFT="$LEFT worker(pid $LPID)"
    fi
fi
if [ -z "$LEFT" ]; then
    say "✓ nothing of MooTrader is running"
else
    say "⚠ still alive:$LEFT"
fi

echo
sleep 2
osascript -e 'tell application "Terminal" to close (every window whose name contains "macos-stop-web")' >/dev/null 2>&1 &
