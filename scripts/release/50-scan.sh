#!/bin/bash
# Scan the LOCALLY BUILT images. Never the registry.
#
# SCAN_SOURCE=local matters: run_parallel_scans' default path does `docker rmi`
# then `docker pull` of :latest, i.e. it scans the PREVIOUS release. That is
# correct only after a push and actively wrong as a pre-push gate.
#
# Exit: 0 clean · 1 blocking findings

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
OUT_DIR="${OT_SCAN_DIR:-./security-reports}"
BLUE='\033[0;34m'; NC='\033[0m'

echo -e "${BLUE}Scanning locally built ${VERSION} images${NC}" >&2

rc=0
SCAN_SOURCE=local \
FAIL_ON_CRITICAL="${FAIL_ON_CRITICAL:-true}" \
OUTPUT_DIR="$OUT_DIR" \
VERSION="$VERSION" \
    ./scripts/security-scan.sh all || rc=$?

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"scan","version":"%s","status":"%s","reports":"%s","next":%s}\n' \
        "$VERSION" "$([[ $rc -eq 0 ]] && echo pass || echo fail)" "$OUT_DIR" \
        "$([[ $rc -eq 0 ]] && echo '["rehearse"]' || echo '["review the findings, or --force-scan with a recorded reason"]')"
fi
exit $rc
