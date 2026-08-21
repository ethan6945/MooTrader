#!/bin/bash
# Keeps the staging worker alive, and says so.
#
#   scripts/supervise.sh ~/MooTraderStaging
#
# WHY
#   2026-08-20: the worker ran 9.5 hours and then stopped. No traceback, no
#   crash report, no exit line — it was signalled, most likely as collateral
#   from a parent's teardown. It stayed dead for 6.5 hours. Three positions
#   were open the whole time, and because both environments run soft exits
#   there was no stop order at the broker to catch them. Nothing noticed and
#   nothing restarted it.
#
#   A run of four to eight weeks is not a thing you can ask of a process that
#   can vanish without anyone finding out.
#
# WHAT IT DOES NOT DO
#   It does not decide anything about trading. It starts the worker through the
#   normal protocol — same lease, same GO, same order gate — and if the
#   protocol refuses, that refusal stands. A supervisor that could talk its way
#   past a refusal would be a way around every check the protocol exists to
#   make.
#
#   It also will not hammer a broken start: repeated failures back off, and
#   after enough of them it stops trying and leaves the reason in the log,
#   because a bot that cannot start is a person's problem, not a retry's.
set -u

HOME_DIR="${1:-$HOME/MooTraderStaging}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python3"
LOG="$HOME_DIR/logs/supervisor.log"
LEASE="$HOME_DIR/logs/worker.lease"
POLL="${SUPERVISE_POLL:-30}"
MAX_CONSECUTIVE_FAILURES="${SUPERVISE_MAX_FAILS:-5}"

say() { echo "$(date '+%F %H:%M:%S') $*" >> "$LOG"; }

worker_pid() {
  [ -f "$LEASE" ] || { echo ""; return; }
  "$PY" - "$LEASE" <<'PYEOF' 2>/dev/null
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("pid", ""))
except Exception:
    print("")
PYEOF
}

alive() {
  local pid="$1"
  [ -n "$pid" ] || return 1
  # Not kill -0: a zombie answers it. Ask for the state instead.
  local st
  st=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')
  [ -n "$st" ] && [ "${st:0:1}" != "Z" ]
}

start_worker() {
  say "starting worker"
  MMT_HOME="$HOME_DIR" PYTHONPATH="$REPO" "$PY" -m src.main start >> "$LOG" 2>&1
}

say "=== supervisor up (home=$HOME_DIR, poll=${POLL}s) ==="
fails=0
while true; do
  pid="$(worker_pid)"
  if alive "$pid"; then
    [ "$fails" -ne 0 ] && say "worker $pid healthy again"
    fails=0
  else
    say "worker not running (lease pid='${pid:-none}') — restarting"
    if start_worker; then
      newpid="$(worker_pid)"
      say "restarted as ${newpid:-unknown}"
      fails=0
      # Hold the machine awake for as long as THIS worker lives. Bound to the
      # pid so it cannot outlive what it was protecting.
      [ -n "$newpid" ] && nohup caffeinate -is -w "$newpid" >/dev/null 2>&1 &
    else
      fails=$((fails + 1))
      say "start FAILED ($fails/$MAX_CONSECUTIVE_FAILURES)"
      if [ "$fails" -ge "$MAX_CONSECUTIVE_FAILURES" ]; then
        say "giving up after $fails consecutive failures — a start that keeps"
        say "being refused is a person's problem; the last refusal is above"
        exit 1
      fi
      sleep $((POLL * fails))     # back off rather than hammer
      continue
    fi
  fi
  sleep "$POLL"
done
