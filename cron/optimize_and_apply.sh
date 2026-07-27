#!/bin/bash
#
# optimize_and_apply.sh — param optimization pipeline (v2, auto-apply)
#
# ⚠ SUPERSEDED 2026-07-28 — the scheduler now runs this itself.
#
# src/main.py schedules _grid_sweep_job at the same two slots (Mon 07:00 KL
# quick grid, weekdays 09:00 KL neighborhood walk) with cron_state catchup, and
# the sweep moved to src/optimize_system.py so it ships inside the packaged
# .app. Reasons this stopped being a crontab job:
#   • the crontab line had an unquoted path with a space, so under /bin/sh the
#     `cd` failed and the `&&` chain never ran — it had not executed once since
#     2026-07-07 (no logs/cron_optimize.log, no data/params_before_opt.json);
#   • a crontab is per-machine, so nobody installing the .app ever got the
#     optimization pipeline at all.
# REMOVE the two moomoo-trader lines from your crontab (`crontab -e`) or the
# sweep will run twice. This script is kept only for a manual one-off run.
#
# Usage: optimize_and_apply.sh [daily|weekly]
#   weekly (default) — Mon 07:00 MYT: full quick grid (27 combos, 2 windows)
#   daily            — Tue-Sat 09:00 MYT: incremental cache top-up (only the
#                      bars added since yesterday) + neighborhood walk around
#                      the CURRENT params (±1 step, 1.25× hysteresis, ≤1
#                      param change/day) — every trading day opens on params
#                      re-validated against data through the previous close.
#
# Both run while the US market is CLOSED (the sweep temporarily injects grid
# params into live db-state; the optimizer itself refuses to run mid-session).
#
# Owner directive 2026-07-07: the validated winner is applied DIRECTLY via
# runtime_config.set_param (param_history recorded, Telegram notified) — no
# approval step. The sweep itself always restores pre-sweep params first;
# only the explicit winner application changes live values.
#
set -euo pipefail

MODE="${1:-weekly}"
case "$MODE" in
    daily)  OPT_FLAG="--daily" ;;
    weekly) OPT_FLAG="--quick" ;;
    *) echo "usage: $0 [daily|weekly]"; exit 2 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv/bin/python3"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$MODE] $*"; }

# ── 0. Save current params for potential manual rollback ──
log "Step 0: Saving current params"
$VENV -c "
import json, sys
sys.path.insert(0, '$ROOT')
from src import runtime_config
saved = {k: runtime_config.current(k)
         for k in ['entry_threshold', 'tp_atr_mult', 'sl_atr_mult']}
with open('$ROOT/data/params_before_opt.json', 'w') as f:
    json.dump(saved, f, indent=2)
print('Saved:', saved)
"

# ── 1. Optimization ──
log "Step 1: Running optimizer v2 ($OPT_FLAG)"
(cd "$ROOT" && $VENV -m src.optimize_system "$OPT_FLAG") 2>&1 | tee "$LOG_DIR/optimize_$(date +%Y%m%d_%H%M).log"

# ── 2. Verify: live params must equal EITHER the pre-sweep snapshot (no
#      winner) or the applied best combo — anything else means the restore
#      or the apply half-failed. ──
log "Step 2: Verifying live params are consistent"
$VENV -c "
import json, sys
sys.path.insert(0, '$ROOT')
from src import runtime_config
d = json.load(open('$ROOT/data/optimized_params.json'))
saved = json.load(open('$ROOT/data/params_before_opt.json'))
live = {k: float(runtime_config.current(k))
        for k in ['entry_threshold', 'tp_atr_mult', 'sl_atr_mult']}
if d.get('applied') and d.get('best'):
    b = d['best']
    expect = {'entry_threshold': float(b['th']), 'tp_atr_mult': float(b['tp']),
              'sl_atr_mult': float(b['sl'])}
    src = 'applied best combo'
else:
    expect = {k: float(v) for k, v in saved.items()}
    src = 'pre-sweep snapshot (no change)'
for k, v in expect.items():
    if live[k] != v:
        print(f'CRITICAL: live {k}={live[k]} != expected {v} ({src}) — fix manually!')
        sys.exit(1)
print(f'OK: live params match {src}: {live}')
"

# ── 3. Report. WEEKLY: one-line Telegram summary even when nothing changed.
#      DAILY: stay quiet unless a change was applied (the optimizer already
#      sent its own Telegram on apply) — no daily "no change" spam. ──
if [ "$MODE" = "weekly" ]; then
    log "Step 3: Sending weekly summary"
    $VENV -c "
import json, sys
sys.path.insert(0, '$ROOT')
d = json.load(open('$ROOT/data/optimized_params.json'))
best = d.get('best') or {}
base = d.get('baseline') or {}
msg = (
    '🔧 *Weekly Optimizer v2*\n'
    f\"Applied: {'YES' if d.get('applied') else 'no change'}\n\"
    f\"Best: THR={best.get('th','—')} TP={best.get('tp','—')} SL={best.get('sl','—')} \"
    f\"agg={best.get('agg_score','—')}\n\"
    f\"Baseline agg={base.get('agg_score','—')} | combos={d.get('combos_tested','?')} \"
    f\"| windows={len(d.get('windows', []))} | {d.get('elapsed_sec','?')}s\"
)
from src import notifier
notifier.send(msg)
print(msg)
"
else
    log "Step 3: daily mode — Telegram only fires on an applied change (see Step 1 log)"
fi

# ── 4. Housekeeping: bound every growing artifact this pipeline creates ──
#   • per-run tee logs (the real disk eater: ~200 KB × 6/week) → keep 30 days
#   • cron_optimize.log → keep the last 2000 lines once it passes ~5 MB
#   • hermes .env snapshots → keep 60 days
log "Step 4: Housekeeping"
find "$LOG_DIR" -name "optimize_*.log" -mtime +30 -delete 2>/dev/null || true
CRON_LOG="$LOG_DIR/cron_optimize.log"
if [ -f "$CRON_LOG" ] && [ "$(stat -f%z "$CRON_LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    tail -n 2000 "$CRON_LOG" > "$CRON_LOG.tmp" && mv "$CRON_LOG.tmp" "$CRON_LOG"
    log "cron_optimize.log trimmed to last 2000 lines"
fi
find "$ROOT/data/hermes_snapshots" -mindepth 1 -maxdepth 1 -type d -mtime +60 \
    -exec rm -rf {} + 2>/dev/null || true

log "DONE"
