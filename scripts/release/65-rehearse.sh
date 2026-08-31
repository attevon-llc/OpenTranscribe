#!/bin/bash
# The two release scenarios: fresh install, and upgrade from the previous release.
#
# This is the stage that proves the release, and the only one that needs the live
# stack STOPPED — the scenarios run under the installer's stock container names
# and ports 5173-5180 by design, so they exercise what a real user gets.
#
# It never stops the stack for you. Taking someone's deployment down is their
# call, so this refuses and prints the command.
#
# Exit: 0 both passed · 1 a scenario failed · 3 live stack still running ·
#       4 operator declined a scenario's confirmation prompt (nothing ran —
#       scripts/release.sh records this as ledger status=aborted, never
#       =failed, and --force-rehearse does not apply to it: re-run and answer
#       the prompt, or pass --yes)

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

# Filter by compose project label, not a name prefix -- see 10-preflight.sh's
# live-stack check for why a naive prefix match false-positives on unrelated
# containers (e.g. "opentranscribe-homepage").
if docker ps --filter 'label=com.docker.compose.project=opentranscribe' --format '{{.Names}}' | grep -q .; then
    echo -e "${RED}The live stack is running; the scenarios cannot run beside it.${NC}" >&2
    echo -e "${YELLOW}  ./opentr.sh stop     # preserves all data${NC}" >&2
    echo -e "${YELLOW}  (restart afterwards with ./opentr.sh start dev)${NC}" >&2
    exit 3
fi

fresh_rc=0; upgrade_rc=0

echo -e "${BLUE}Scenario A — fresh install${NC}" >&2
./scripts/release-tests/test-fresh-install.sh --yes || fresh_rc=$?

# Scenario A ends with its stack UP, deliberately: run standalone, you want to
# poke at the thing you just installed. But both scenarios bind the same stock
# container names and ports 5173-5180, so Scenario B's guardrails refuse to
# start while A's stack exists — meaning this stage could never get past A.
#
# So the ORCHESTRATOR tears A down before B. That is different from stopping
# the operator's live deployment, which this stage still refuses to do: these
# are containers this stage created moments ago.
echo -e "${BLUE}Tearing down Scenario A's stack so Scenario B can bind its ports${NC}" >&2
./scripts/release-tests/test-fresh-install.sh --cleanup --yes || {
    echo -e "${YELLOW}Scenario A cleanup reported a problem; continuing to B${NC}" >&2
}

# Cleanup is asynchronous at the docker level — a container in 'Removing' still
# holds its port bindings, and B's preflight would see a stale name. Wait for
# the names to actually disappear rather than racing them.
for _ in $(seq 1 30); do
    docker ps -a --filter 'label=com.docker.compose.project=opentranscribe' --format '{{.Names}}' | grep -q . || break
    sleep 2
done
if docker ps -a --filter 'label=com.docker.compose.project=opentranscribe' --format '{{.Names}}' | grep -q .; then
    echo -e "${RED}Scenario A's containers did not go away; Scenario B cannot start${NC}" >&2
    docker ps -a --filter 'label=com.docker.compose.project=opentranscribe' --format '  {{.Names}}\t{{.Status}}' >&2
    upgrade_rc=3
fi

if [[ $upgrade_rc -eq 0 ]]; then
    echo -e "${BLUE}Scenario B — upgrade from the previous published release${NC}" >&2
    REQUIRE_PREVIOUS=1 ./scripts/release-tests/test-upgrade.sh --yes || upgrade_rc=$?
fi

# Preserve the shared exit-code contract rather than flattening everything to 1.
# An operator who declines a scenario's `I UNDERSTAND` prompt has ABORTED (4); the
# stale-container check above sets 3 for an unmet PRECONDITION. Collapsing both into
# "gate failed" told every caller — release.sh's ledger and scripts/test-matrix.sh's
# leg 3 alike — that a rehearsal had run and failed, when none had run at all.
rc=0
if [[ $fresh_rc -eq 4 || $upgrade_rc -eq 4 ]]; then
    rc=4
elif [[ $fresh_rc -eq 3 || $upgrade_rc -eq 3 ]]; then
    rc=3
elif [[ $fresh_rc -ne 0 || $upgrade_rc -ne 0 ]]; then
    rc=1
fi
[[ $rc -eq 0 ]] && echo -e "${GREEN}both scenarios passed${NC}" >&2

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"rehearse","version":"%s","status":"%s","criteria":[{"id":"fresh-install","status":"%s"},{"id":"upgrade-from-previous","status":"%s"}],"next":%s}\n' \
        "$VERSION" "$([[ $rc -eq 0 ]] && echo pass || echo fail)" \
        "$([[ $fresh_rc -eq 0 ]] && echo pass || echo fail)" \
        "$([[ $upgrade_rc -eq 0 ]] && echo pass || echo fail)" \
        "$([[ $rc -eq 0 ]] && echo '["restart the dev stack, then tag"]' || echo '["read the REPORT.md under each TEST_ROOT"]')"
fi
exit $rc
