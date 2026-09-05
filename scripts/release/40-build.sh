#!/bin/bash
# Build the release candidate LOCALLY. Publishes nothing.
#
# The whole point of a separate build stage is that the artifact exists and can be
# scanned and rehearsed before anything reaches Docker Hub — :latest is what every
# existing user pulls, so it must not move before the scenarios pass.
#
# Asserts the build-arg contract afterwards: an image reporting version "unknown"
# is a release-process failure, not a cosmetic one (issues #411).
#
# Exit: 0 built · 1 build or verification failed

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'

: "${VERSION:?40-build.sh needs a version}"

echo -e "${BLUE}Building ${VERSION} locally (nothing will be pushed)${NC}" >&2

if ! BUILD_MODE=local PUSH_LATEST=false SKIP_SECURITY_SCAN=true VERSION="$VERSION" \
        ./scripts/docker-build-push.sh all; then
    echo -e "${RED}build failed${NC}" >&2
    exit 1
fi

# The image must be able to state what it is. This is the check that would have
# caught the build-arg omission in the documented `docker build` commands.
#
# Every backend-derived component from `docker-build-push.sh list-platforms`
# is checked, not just `backend` (issue #680) — `all` also builds `lite` now,
# and frontend/docs declare no APP_VERSION build-arg contract at all (frontend
# takes its version from package.json, docs bakes OT_VERSION separately), so
# only the two Dockerfile.prod/Dockerfile.lite-based backend images have this
# baked-version contract to verify.
DOCKERHUB_USERNAME="${DOCKERHUB_USERNAME:-davidamacey}"
declare -A REPO_FOR_COMPONENT=(
    [backend]="${DOCKERHUB_USERNAME}/opentranscribe-backend"
    [lite]="${DOCKERHUB_USERNAME}/opentranscribe-backend-lite"
)

status=pass
declare -A baked_by_component=()
while IFS=$'\t' read -r component _capability _platforms; do
    repo="${REPO_FOR_COMPONENT[${component}]:-}"
    [ -n "$repo" ] || continue

    baked=$(docker run --rm --entrypoint sh \
        "${repo}:${VERSION}" \
        -c 'echo "$APP_VERSION"' 2>/dev/null | tr -d '\r')
    baked_by_component["$component"]="$baked"

    if [[ "$baked" != "$VERSION" ]]; then
        echo -e "${RED}FAIL  ${component} image reports '${baked:-<empty>}', expected ${VERSION}${NC}" >&2
        echo "      the --build-arg APP_VERSION contract is broken for ${component}" >&2
        status=fail
    else
        echo -e "${GREEN}PASS  ${component} image reports ${baked}${NC}" >&2
    fi
done < <(./scripts/docker-build-push.sh list-platforms)

if [[ "$JSON_OUT" == "true" ]]; then
    artifacts="{"
    first=true
    for component in "${!baked_by_component[@]}"; do
        [[ "$first" == true ]] || artifacts+=","
        first=false
        artifacts+="\"${component}_baked_version\":\"${baked_by_component[$component]}\""
    done
    artifacts+="}"
    printf '{"stage":"build","version":"%s","status":"%s","artifacts":%s,"next":%s}\n' \
        "$VERSION" "$status" "$artifacts" \
        "$([[ "$status" == pass ]] && echo '["scan"]' || echo '["fix the build-arg contract and rebuild"]')"
fi

[[ "$status" == pass ]] || exit 1
exit 0
