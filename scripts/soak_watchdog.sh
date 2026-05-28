#!/bin/bash
# Watchdog for the GPU soak orchestrator. Keeps it running until it completes,
# auto-resuming on a crash (the orchestrator is checkpointed/idempotent, so a
# relaunch skips finished levels). Caps consecutive restarts so a persistent
# failure stops instead of looping forever. Run detached:
#   setsid bash scripts/soak_watchdog.sh >/dev/null 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

STATE=benchmarks/soak_state
ORCH_LOG="$STATE/orchestrate.log"
WLOG="$STATE/watchdog.log"
DONE_MARK="SOAK ORCHESTRATOR DONE"
MAX_RESTARTS=8
restarts=0

mkdir -p "$STATE"
log(){ echo "[$(date '+%F %T')] $*" >> "$WLOG"; }
log "watchdog started (pid $$)"

while true; do
  if [[ -f "$ORCH_LOG" ]] && grep -q "$DONE_MARK" "$ORCH_LOG" 2>/dev/null; then
    log "orchestrator reported DONE — watchdog exiting"; break
  fi
  if ! pgrep -f 'scripts/soak_orchestrate.sh' >/dev/null 2>&1; then
    if (( restarts >= MAX_RESTARTS )); then
      log "orchestrator down and MAX_RESTARTS=$MAX_RESTARTS reached — giving up; manual investigation needed"; break
    fi
    restarts=$((restarts + 1))
    log "orchestrator not running and not DONE — resuming (restart #$restarts)"
    setsid bash scripts/soak_orchestrate.sh >/dev/null 2>&1 &
    sleep 45   # let it boot before the next liveness check
  fi
  sleep 60
done
log "watchdog exiting (restarts used: $restarts)"
