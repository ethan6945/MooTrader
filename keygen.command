#!/bin/bash
# Double-click to open the licence keygen.
#
# This window OWNS the server. Close the window, or press ⌃C, and the keygen
# stops with it — the opposite of start-web.command, which deliberately detaches
# so the panel survives. That difference is the point: this thing can mint
# licences, and a forgotten copy of it left listening after the window is gone
# is exactly what should not happen. Nothing here is nohup'd, setsid'd or
# disowned, and the trap below covers ⌃C, a TERM, and the HUP that closing the
# window sends.
#
# It binds 127.0.0.1 only. keygen.py refuses any other address.

cd "$(dirname "$0")" || exit 1

PY=".venv/bin/python3"
DIR="Keygen Activator"
say() { printf '  %s\n' "$*"; }

echo
echo "  Moo Trader — 发码 Keygen"
echo "  ───────────────────────"

[ -x "$PY" ] || { say "✗ no interpreter at $PY"; echo; read -r -p "  Press return to close. "; exit 1; }
[ -d "$DIR" ] || { say "✗ no \"$DIR\" folder"; echo; read -r -p "  Press return to close. "; exit 1; }

if [ ! -f "$DIR/signing-private.pem" ]; then
    say "✗ no signing key in \"$DIR\""
    say "  Without it nothing can be issued. If you have a backup, restore it —"
    say "  a NEW key would invalidate every licence already sold."
    echo; read -r -p "  Press return to close. "; exit 1
fi

# Port 8765 was already taken by something else on this machine once, and the
# failure looked like a broken keygen rather than a busy port. Walk up instead.
PORT=${KEYGEN_PORT:-8765}
for _ in 1 2 3 4 5 6 7 8 9 10; do
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1 || break
    PORT=$((PORT + 1))
done

cd "$DIR" || exit 1
KEYGEN_PORT="$PORT" "../$PY" keygen.py &
SRV=$!

# Closing the Terminal window sends HUP; ⌃C sends INT. Either way the server
# goes with us, and EXIT catches every other way this script ends.
# HUP and EXIT both fire when the window closes, so guard it — the first run
# said "keygen stopped." twice.
CLEANED=0
cleanup() {
    [ "$CLEANED" -eq 1 ] && return
    CLEANED=1
    kill "$SRV" 2>/dev/null
    wait "$SRV" 2>/dev/null
    echo
    printf '  %s\n' "keygen stopped."
}
trap cleanup EXIT INT TERM HUP

for _ in $(seq 1 40); do
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/info" >/dev/null 2>&1; then
        say "✓ http://127.0.0.1:$PORT"
        say "  Close this window to stop the keygen."
        echo
        open "http://127.0.0.1:$PORT"
        wait "$SRV"
        exit 0
    fi
    kill -0 "$SRV" 2>/dev/null || break
    sleep 0.5
done

say "✗ the keygen did not come up"
echo
read -r -p "  Press return to close. "
