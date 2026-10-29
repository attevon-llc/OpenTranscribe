#!/usr/bin/env bash
# Sourced helper: refuse a scan target that is this machine's live OpenTranscribe stack.
#
#   source "$SCRIPT_DIR/lib/refuse_live_stack.sh"
#   refuse_live_stack "$URL" "$ALLOW_LIVE" || exit "$(( $? == 3 ? REFUSE_EXIT : USAGE_EXIT ))"
#
# Returns 0 when allowed, 3 when refused, 2 when the target cannot be judged. The logic
# lives in lib/target_guard.py so both callers (and the unit tests) share one definition.

refuse_live_stack() {
  local url="$1" allow="${2:-0}"
  local guard
  guard="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/target_guard.py"
  local args=("$url")
  [ "$allow" -eq 1 ] && args+=(--allow-live-dev)
  python3 "$guard" "${args[@]}"
}
