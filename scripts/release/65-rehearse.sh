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
# Exit: 0 both passed · 1 a scenario failed · 3 live stack still running

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

if docker ps --format '{{.Names}}' | grep -q '^opentranscribe-'; then
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
    docker ps -a --format '{{.Names}}' | grep -q '^opentranscribe-' || break
    sleep 2
done
if docker ps -a --format '{{.Names}}' | grep -q '^opentranscribe-'; then
    echo -e "${RED}Scenario A's containers did not go away; Scenario B cannot start${NC}" >&2
    docker ps -a --format '  {{.Names}}\t{{.Status}}' | grep '^  opentranscribe-' >&2
    upgrade_rc=3
fi

if [[ $upgrade_rc -eq 0 ]]; then
    echo -e "${BLUE}Scenario B — upgrade from the previous published release${NC}" >&2
    REQUIRE_PREVIOUS=1 ./scripts/release-tests/test-upgrade.sh --yes || upgrade_rc=$?
fi

rc=0
[[ $fresh_rc -eq 0 && $upgrade_rc -eq 0 ]] || rc=1
[[ $rc -eq 0 ]] && echo -e "${GREEN}both scenarios passed${NC}" >&2

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"rehearse","version":"%s","status":"%s","criteria":[{"id":"fresh-install","status":"%s"},{"id":"upgrade-from-previous","status":"%s"}],"next":%s}\n' \
        "$VERSION" "$([[ $rc -eq 0 ]] && echo pass || echo fail)" \
        "$([[ $fresh_rc -eq 0 ]] && echo pass || echo fail)" \
        "$([[ $upgrade_rc -eq 0 ]] && echo pass || echo fail)" \
        "$([[ $rc -eq 0 ]] && echo '["restart the dev stack, then tag"]' || echo '["read the REPORT.md under each TEST_ROOT"]')"
fi
exit $rc
