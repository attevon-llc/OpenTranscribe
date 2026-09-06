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

# Severities from release-criteria.yaml; outcomes from here. Bidirectional — see
# criteria-lib.sh. Exported because the consumer lives across a file boundary.
export STAGE_ID=test
# shellcheck source=scripts/release/criteria-lib.sh
source "$SCRIPT_DIR/criteria-lib.sh"

if ! (exec 3<>/dev/tcp/127.0.0.1/5176) 2>/dev/null; then
    record dev-stack-up fail "nothing answering on 127.0.0.1:5176" "./opentr.sh start dev"
    echo -e "${RED}The dev stack must be running for the test stage.${NC}" >&2
    echo "  ./opentr.sh start dev" >&2
    if [[ "$JSON_OUT" == "true" ]]; then
        printf '{"stage":"test","version":"%s","status":"fail","criteria":[%s],"next":["./opentr.sh start dev"]}\n' \
            "$VERSION" "$(criteria_json)"
    fi
    # Exit 3 (precondition unmet) as before. Deliberately NOT via criteria_assert_all_checked:
    # the gate genuinely never ran, and reporting that as wiring drift (2) would both bury the
    # real reason and change this stage's exit contract.
    exit 3
fi
record dev-stack-up pass

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

if (( rc == 0 )); then
    record integration-gate pass
else
    record integration-gate fail "run-integration-tests.sh exited $rc" \
        "read the per-phase output above; re-run ./scripts/run-integration-tests.sh"
fi

# Both halves of the contract. Reachable on both outcomes — the gate above always records
# one — so this cannot convert a test failure (1) into a wiring-misuse exit (2).
criteria_assert_all_checked

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"test","version":"%s","status":"%s","criteria":[%s],"next":%s}\n' \
        "$VERSION" "$([[ $rc -eq 0 ]] && echo pass || echo fail)" "$(criteria_json)" \
        "$([[ $rc -eq 0 ]] && echo '["build"]' || echo '["fix the failing suites"]')"
fi
exit $rc
