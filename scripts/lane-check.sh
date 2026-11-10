#!/usr/bin/env bash
# lane-check.sh: the fast whole-tree guards, in one command, before a lane reports.
#
# Runs every FAST whole-tree ratchet/auditor this repo gates on, in parallel, and prints one
# PASS/FAIL line per guard plus the tail of each failing guard's output. Exits 1 if any guard
# failed, 2 on misuse or a missing prerequisite.
#
# Each pre-commit-backed guard runs the hook's OWN `entry`, read from .pre-commit-config.yaml by
# scripts/lib/precommit_entry.py, never a copy of it: a copied command keeps passing after the
# hook changes. `language: python` hooks run under backend/venv/bin/python, which holds the same
# pinned ruff/vulture/mypy (each guard refuses any other version, so a mismatch fails, it does
# not pass). Not included: builds, `bandit -r`, test suites, pre-commit itself (it stashes the
# worktree; see CLAUDE.md "Pre-commit / lint hooks").
#
# Usage:
#   scripts/lane-check.sh                 # every guard
#   scripts/lane-check.sh --only a,b      # a subset, by guard name
#   scripts/lane-check.sh --skip a,b      # every guard except these
#   scripts/lane-check.sh --list          # guard names and the command each runs
#   scripts/lane-check.sh --config FILE   # read hook entries from another pre-commit config
#
# Env: LANE_CHECK_PYTHON (default backend/venv/bin/python), LANE_CHECK_TAIL (failure lines, 25).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${LANE_CHECK_PYTHON:-$ROOT/backend/venv/bin/python}"
TAIL_LINES="${LANE_CHECK_TAIL:-25}"

# name -> pre-commit hook id. Order is the report order.
HOOK_GUARDS=(
  "duplicates=guard-duplicates"
  "hidden-mypy=guard-hidden-mypy"
  "style-literals=guard-style-literals"
  "unused-css=guard-unused-css"
  "suppressions=guard-suppressions"
  "vulture=guard-vulture"
  "svelte-unused=guard-svelte-unused"
  "nuia=guard-nuia"
  "api-boundary=guard-api-boundary"
  "raw-env=guard-raw-env"
  "ruff-ratchet=guard-ruff-ratchet"
  "check-i18n=i18n-parity"
  "audit-tests=audit-tests"
)
# Guards with no pre-commit hook (CI and scripts/frontend-check.sh run them).
declare -A EXTRA_GUARDS=(
  ["test-audit"]="cd frontend && npm run -s test:audit"
)
EXTRA_ORDER=("test-audit")

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; }

only="" skip="" list=false config="$ROOT/.pre-commit-config.yaml"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --only) only="${2:?--only needs a list}"; shift 2 ;;
    --skip) skip="${2:?--skip needs a list}"; shift 2 ;;
    --list) list=true; shift ;;
    --config) config="${2:?--config needs a file}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "lane-check: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ! -x "$PY" ]]; then
  echo "lane-check: $PY not found; create backend/venv (CLAUDE.md, Backend / venv) or set LANE_CHECK_PYTHON" >&2
  exit 2
fi
if [[ ! -x frontend/node_modules/.bin/svelte-check ]]; then
  echo "lane-check: frontend/node_modules is missing; run 'cd frontend && npm ci' first" >&2
  exit 2
fi

declare -A CMD=()
NAMES=()
hook_ids=()
for pair in "${HOOK_GUARDS[@]}"; do hook_ids+=("${pair#*=}"); done
# One resolver call for every hook: an unknown or renamed hook id fails here, before anything runs.
resolved="$("$PY" scripts/lib/precommit_entry.py --config "$config" --python "$PY" "${hook_ids[@]}")" || {
  echo "lane-check: could not resolve the hook entries (above); fix HOOK_GUARDS" >&2
  exit 2
}
for pair in "${HOOK_GUARDS[@]}"; do
  name="${pair%%=*}" hook="${pair#*=}"
  CMD[$name]="$(awk -F'\t' -v h="$hook" '$1 == h { sub(/^[^\t]*\t/, ""); print; exit }' <<<"$resolved")"
  NAMES+=("$name")
done
for name in "${EXTRA_ORDER[@]}"; do CMD[$name]="${EXTRA_GUARDS[$name]}"; NAMES+=("$name"); done

in_list() { [[ ",$2," == *",$1,"* ]]; }
for given in ${only//,/ } ${skip//,/ }; do
  [[ -n "${CMD[$given]+x}" ]] || { echo "lane-check: unknown guard '$given' (see --list)" >&2; exit 2; }
done

selected=()
for name in "${NAMES[@]}"; do
  if [[ -n "$only" ]] && ! in_list "$name" "$only"; then continue; fi
  if [[ -n "$skip" ]] && in_list "$name" "$skip"; then continue; fi
  selected+=("$name")
done

if $list; then
  for name in "${selected[@]}"; do printf '%s\t%s\n' "$name" "${CMD[$name]}"; done
  exit 0
fi

LOGS="$(mktemp -d -t ot-lane-check.XXXXXX)"
export PATH="$ROOT/frontend/node_modules/.bin:$PATH"
start_all=$SECONDS
declare -A PIDS=()
for name in "${selected[@]}"; do
  (
    t0=$(date +%s.%N)
    rc=0
    nice -n 10 bash -c "${CMD[$name]}" >"$LOGS/$name.log" 2>&1 </dev/null || rc=$?
    t1=$(date +%s.%N)
    printf '%s %s\n' "$rc" "$(awk -v a="$t0" -v b="$t1" 'BEGIN { printf "%.1f", b - a }')" >"$LOGS/$name.status"
  ) &
  PIDS[$name]=$!
done

failed=0
for name in "${selected[@]}"; do wait "${PIDS[$name]}" || true; done
for name in "${selected[@]}"; do
  read -r rc secs <"$LOGS/$name.status"
  if [[ "$rc" == 0 ]]; then
    printf 'PASS  %-15s %6ss\n' "$name" "$secs"
  else
    printf 'FAIL  %-15s %6ss  (exit %s, log %s)\n' "$name" "$secs" "$rc" "$LOGS/$name.log"
    failed=$((failed + 1))
  fi
done
for name in "${selected[@]}"; do
  read -r rc _ <"$LOGS/$name.status"
  [[ "$rc" == 0 ]] && continue
  echo
  echo "---- $name (last $TAIL_LINES lines) ----"
  tail -n "$TAIL_LINES" "$LOGS/$name.log"
done
echo
if [[ $failed -eq 0 ]]; then
  rm -f "$LOGS"/*.log "$LOGS"/*.status && rmdir "$LOGS"
  echo "lane-check: ${#selected[@]} guard(s), 0 failed, $((SECONDS - start_all))s wall"
  exit 0
fi
echo "lane-check: ${#selected[@]} guard(s), $failed failed, $((SECONDS - start_all))s wall (logs: $LOGS)"
exit 1
