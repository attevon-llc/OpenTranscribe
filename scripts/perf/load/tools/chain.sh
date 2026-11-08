#!/bin/bash
# Run several profiles back to back, each with the pg probe beside it, on a quiet host.
#
# Usage: chain.sh FRESH_NAME OUT_ROOT "NAME|probe_seconds|run.sh args..." ...
# e.g.   chain.sh perf1337 ~/runs "F01-smoke-read-only|120|--profile smoke --mix read-only"
# Each run records uptime before it, then waits 60 s so the next one starts from a settled stack.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRESH="${1:?fresh deployment name}"; OUT_ROOT="${2:?runs directory}"; shift 2
for spec in "$@"; do
  IFS='|' read -r N DUR ARGS <<< "$spec"
  mkdir -p "$OUT_ROOT/$N"; uptime > "$OUT_ROOT/$N/uptime-before.txt"
  "$HERE/pg_probe.sh" "$FRESH" "$OUT_ROOT/$N/pgprobe.csv" "$DUR" &
  PROBE=$!
  # shellcheck disable=SC2086  # ARGS is a word list by design
  "$HERE/run_profile.sh" "$FRESH" "$OUT_ROOT" "$N" $ARGS
  wait "$PROBE"
  uptime > "$OUT_ROOT/$N/uptime-after.txt"
  sed -n 3p "$OUT_ROOT/$N/summary.md"
  sleep 60
done
