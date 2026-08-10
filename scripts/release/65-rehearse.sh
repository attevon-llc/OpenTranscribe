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

echo -e "${BLUE}Scenario B — upgrade from the previous published release${NC}" >&2
REQUIRE_PREVIOUS=1 ./scripts/release-tests/test-upgrade.sh --yes || upgrade_rc=$?

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
