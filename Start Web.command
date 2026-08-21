#!/bin/bash
# Double-click to run MooTrader's web panel.
#
# Starts the server DETACHED, waits until it actually answers, opens the
# browser, then closes this Terminal window. The server keeps running.
#
# Detached matters: a server that is a child of this Terminal dies when the
# window closes, and the window is closed on purpose two lines later. nohup +
# setsid + disown means the only thing that can stop it is "Stop Web.command".
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO" || exit 1

PORT="${WEB_PORT:-8770}"
PY="$REPO/.venv/bin/python3"
LOG="$REPO/logs/web.log"
PIDFILE="$REPO/logs/web.pid"

say() { printf '  %s\n' "$*"; }
echo
echo "  MooTrader — starting the web panel"
echo "  ──────────────────────────────────"

[ -x "$PY" ] || { say "✗ no interpreter at .venv/bin/python3"; say "  run:  uv venv && uv pip install -r requirements.txt"; echo; read -r -p "  Press return to close. "; exit 1; }
[ -f "$REPO/.env" ] || { say "✗ no .env — copy .env.example and fill it in"; echo; read -r -p "  Press return to close. "; exit 1; }

# Already up? Then this is a no-op, not a second server fighting for the port.
if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/favicon.ico" >/dev/null 2>&1 \
   || nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; then
    say "already running on port $PORT — opening it"
    open "http://127.0.0.1:$PORT"
    sleep 1
    osascript -e 'tell application "Terminal" to close (every window whose name contains "Start Web")' >/dev/null 2>&1 &
    exit 0
fi

mkdir -p "$REPO/logs"
say "launching…"
# setsid detaches from this terminal's session so closing the window cannot
# take the server with it. It is not on macOS by default, so fall back to a
# plain background nohup, which survives too.
if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$PY" web/server.py >> "$LOG" 2>&1 &
else
    nohup "$PY" web/server.py >> "$LOG" 2>&1 &
fi
SERVER_PID=$!
echo "$SERVER_PID" > "$PIDFILE"
disown "$SERVER_PID" 2>/dev/null || true

# Wait for it to ANSWER, not merely to exist. A pid that is about to die of a
# port conflict or a bad .env would otherwise look like success.
for _ in $(seq 1 40); do
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/favicon.ico" >/dev/null 2>&1 \
       || nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; then
        say "✓ running on http://127.0.0.1:$PORT  (pid $SERVER_PID)"
        open "http://127.0.0.1:$PORT"
        sleep 1
        osascript -e 'tell application "Terminal" to close (every window whose name contains "Start Web")' >/dev/null 2>&1 &
        exit 0
    fi
    kill -0 "$SERVER_PID" 2>/dev/null || break
    sleep 0.5
done

# Did not come up. Keep the window open — the reason is in the log and the
# whole point of this window is to show it.
say "✗ it did not come up within 20s"
echo
say "last lines of logs/web.log:"
tail -n 15 "$LOG" 2>/dev/null | sed 's/^/      /'
rm -f "$PIDFILE"
echo
read -r -p "  Press return to close. "
