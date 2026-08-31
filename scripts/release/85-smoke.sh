#!/bin/bash
# Install from Docker Hub and verify BOTH architectures, post-publish.
#
# This is the first stage that tests what a user will actually pull, rather than
# what was built locally. The arm64 half runs the published image over the remote
# builder's docker context and asserts /api/version — which proves the build-arg
# contract survived on the architecture nobody builds locally.
#
# Exit: 0 verified · 1 failed · 3 live stack running

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
REMOTE_CTX="${REMOTE_ARM64_CONTEXT:-remote-arm64}"
IMG="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-backend:${VERSION}"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
: "${VERSION:?85-smoke.sh needs a version}"

fail=0

# ── arm64: run the PUBLISHED image natively and ask it what it is ───────────
if docker context inspect "$REMOTE_CTX" >/dev/null 2>&1; then
    echo -e "${BLUE}arm64: checking the published image reports its version${NC}" >&2
    got=$(docker --context "$REMOTE_CTX" run --rm --pull always --entrypoint sh "$IMG" \
            -c 'echo "$APP_VERSION"' 2>/dev/null | tr -d '\r')
    if [[ "$got" == "$VERSION" ]]; then
        echo -e "${GREEN}PASS  arm64 image reports ${got}${NC}" >&2
    else
        echo -e "${RED}FAIL  arm64 image reports '${got:-<empty>}', expected ${VERSION}${NC}" >&2
        fail=1
    fi
else
    echo -e "${YELLOW}SKIP  no '${REMOTE_CTX}' docker context — arm64 unverified${NC}" >&2
fi

# ── amd64: the documented one-liner, against Docker Hub ────────────────────
# Filter by compose project label, not a name prefix -- see 10-preflight.sh's
# live-stack check for why a naive prefix match false-positives on unrelated
# containers (e.g. "opentranscribe-homepage").
if docker ps --filter 'label=com.docker.compose.project=opentranscribe' --format '{{.Names}}' | grep -q .; then
    echo -e "${RED}live stack running — the Hub install smoke needs it stopped${NC}" >&2
    exit 3
fi
echo -e "${BLUE}amd64: fresh install pulling :${VERSION} from Docker Hub${NC}" >&2
USE_HUB_IMAGES=true LOCAL_IMAGE_TAG="$VERSION" \
    ./scripts/release-tests/test-fresh-install.sh --yes --force || fail=1

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"smoke","version":"%s","status":"%s","next":%s}\n' \
        "$VERSION" "$([[ $fail -eq 0 ]] && echo pass || echo fail)" \
        "$([[ $fail -eq 0 ]] && echo '["promote"]' || echo '["do NOT promote :latest"]')"
fi
exit $fail
