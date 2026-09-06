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

# EVERY declared architecture leg is built, not just the host's (issue #667).
#
# This used to be a single `docker-build-push.sh all`, which in BUILD_MODE=local builds the
# HOST architecture only — so the arm64 legs of lite/frontend/docs simply did not exist when
# the scan stage ran, and the scan could not have examined them even in principle. Since the
# scan stage now covers every leg and fails closed on any it cannot obtain, the build stage
# has to produce them, or every release would (correctly) stop at `scan`.
#
# One invocation per leg because `--load` cannot export a multi-arch manifest; the layer cache
# makes repeats of an already-built leg cheap. arm64 legs need USE_REMOTE_BUILDER=true or they
# run under QEMU (hours, not minutes) — it is passed through rather than defaulted here so an
# operator can still see and override it.
HOST_PLATFORM="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null || echo linux/amd64)"

legs_built=0
while IFS=$'\t' read -r component _capability platforms; do
    # blackwell is never part of a release build (built only on request, publishes no
    # versioned tag) — same exclusion 50-scan.sh applies.
    [[ "$component" == "blackwell" ]] && continue
    IFS=',' read -r -a plats <<< "$platforms"
    for platform in "${plats[@]}"; do
        [[ -n "$platform" ]] || continue
        echo -e "${BLUE}  building ${component} for ${platform}${NC}" >&2
        use_remote="${USE_REMOTE_BUILDER:-false}"
        [[ "$platform" != "$HOST_PLATFORM" ]] && use_remote=true
        if ! PLATFORMS="$platform" BUILD_MODE=local PUSH_LATEST=false \
             SKIP_SECURITY_SCAN=true VERSION="$VERSION" \
             USE_REMOTE_BUILDER="$use_remote" \
                ./scripts/docker-build-push.sh "$component"; then
            echo -e "${RED}build failed: ${component} ${platform}${NC}" >&2
            exit 1
        fi
        legs_built=$((legs_built + 1))
    done
done < <(./scripts/docker-build-push.sh list-platforms)

if (( legs_built == 0 )); then
    echo -e "${RED}no legs built — could not derive the platform table${NC}" >&2
    exit 1
fi
echo -e "${GREEN}built ${legs_built} architecture leg(s)${NC}" >&2

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
while IFS=$'\t' read -r component capability platforms; do
    repo="${REPO_FOR_COMPONENT[${component}]:-}"
    [ -n "$repo" ] || continue

    # Run the HOST-architecture leg by its own leg tag, not the bare :VERSION tag.
    #
    # Two reasons. `docker run` cannot execute a foreign-architecture image without QEMU, so a
    # non-host leg would fail here for a reason that says nothing about the build-arg contract.
    # And building several legs leaves the bare `repo:$VERSION` tag pointing at whichever leg
    # was built LAST — so checking it would silently test a different artifact depending on
    # table ordering. The leg tag is unambiguous.
    #
    # Non-host legs are therefore NOT run-checked here. They are not unchecked overall: the
    # scan stage examines every leg, and 80-publish.sh verifies each published leg's declared
    # platform and cross-arch size equivalence.
    if [[ ",${platforms}," != *",${HOST_PLATFORM},"* ]]; then
        echo -e "${BLUE}SKIP  ${component}: no ${HOST_PLATFORM} leg to run here (declares ${platforms})${NC}" >&2
        continue
    fi
    leg_tag="${repo}:${VERSION}-${capability}-${HOST_PLATFORM#linux/}"

    baked=$(docker run --rm --entrypoint sh \
        "${leg_tag}" \
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
