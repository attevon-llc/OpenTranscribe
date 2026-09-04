#!/bin/bash
# The existing pre-merge gates, run against the release candidate.
#
# Deliberately delegates rather than reimplementing: run-integration-tests.sh is
# the canonical gate and already knows about the RUN_*-gated suites and both FIPS
# modes. A second definition of "the tests" would drift from it.
#
# Requires the dev stack UP (it TCP-probes Postgres on 5176).
#
# Exit: 0 pass · 1 failures · 3 precondition unmet

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'

if ! (exec 3<>/dev/tcp/127.0.0.1/5176) 2>/dev/null; then
    echo -e "${RED}The dev stack must be running for the test stage.${NC}" >&2
    echo "  ./opentr.sh start dev" >&2
    exit 3
fi

echo -e "${BLUE}Running the canonical pre-merge gate${NC}" >&2
rc=0
# --export-capability drives a REAL diar-native model export (~150s, downloads the gated
# pyannote/speaker-diarization-community-1 weights) against the live backend container.
# It is opt-in in run-integration-tests.sh (too heavy for the everyday dev loop), but a
# release must never ship on the strength of a test that has never actually run — so the
# release gate always asks for it here. 10-preflight.sh already warns when no
# HUGGINGFACE_TOKEN is configured; without one this phase skips loudly (never silently,
# never counted as a pass) rather than failing the release outright.
./scripts/run-integration-tests.sh --coverage --e2e-smoke --export-capability || rc=$?

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"test","version":"%s","status":"%s","next":%s}\n' \
        "$VERSION" "$([[ $rc -eq 0 ]] && echo pass || echo fail)" \
        "$([[ $rc -eq 0 ]] && echo '["build"]' || echo '["fix the failing suites"]')"
fi
exit $rc
