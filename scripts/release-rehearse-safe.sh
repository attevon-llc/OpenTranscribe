#!/bin/bash
# Autonomous-safe entry point for the rehearsal-only portion of the release
# pipeline. Exists so an agent (or an operator) can iterate on
# preflight/verify/test/build/scan/rehearse without a permission prompt on
# every invocation, while making it STRUCTURALLY impossible for this specific
# entry point to reach tag/publish/promote/finish -- those stages are not
# accepted as arguments here at all, never mind executed.
#
# scripts/release.sh's own EXTERNAL_STAGES gate (tag/publish/promote/finish
# require --yes, checked in code) is the real safety net and is untouched by
# this wrapper. This script is a second, independent layer: it cannot ask for
# --yes because it never accepts an external stage in the first place.
#
# Usage: ./scripts/release-rehearse-safe.sh <version> [stages] [force-scan-reason]
#
# [stages]: comma-separated subset to resume just the remaining stages (e.g.
# "scan,rehearse" after test/build already passed) instead of re-running
# everything -- ALWAYS intersected against SAFE_STAGES below, so a typo or a
# copy-pasted dangerous stage name can never slip through; if the intersection
# is empty, every safe stage runs (the original default behavior).
#
# [force-scan-reason]: if given, passed as --force-scan "<reason>" -- for a
# scan finding that is real but has no available fix (e.g. a base-image OS
# package CVE with no upstream patch yet). It can ONLY ever affect the `scan`
# stage, which is already in SAFE_STAGES; it cannot be used to force tag/
# publish/promote/finish, since those are never in --only here regardless.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VERSION="${1:?usage: release-rehearse-safe.sh vX.Y.Z [stages] [force-scan-reason]}"
REQUESTED_STAGES="${2:-}"
FORCE_SCAN_REASON="${3:-}"

SAFE_STAGES="preflight,verify,test,build,scan,rehearse"

STAGES_TO_RUN="$SAFE_STAGES"
if [[ -n "$REQUESTED_STAGES" ]]; then
  intersected=""
  IFS=',' read -ra _requested <<< "$REQUESTED_STAGES"
  IFS=',' read -ra _safe <<< "$SAFE_STAGES"
  for r in "${_requested[@]}"; do
    for s in "${_safe[@]}"; do
      [[ "$r" == "$s" ]] && intersected="${intersected:+$intersected,}$r"
    done
  done
  [[ -n "$intersected" ]] && STAGES_TO_RUN="$intersected"
fi

if [[ -n "$FORCE_SCAN_REASON" ]]; then
  exec "$SCRIPT_DIR/release.sh" run "$VERSION" --only "$STAGES_TO_RUN" --yes --force-scan "$FORCE_SCAN_REASON"
fi

exec "$SCRIPT_DIR/release.sh" run "$VERSION" --only "$STAGES_TO_RUN" --yes
