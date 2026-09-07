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

# Severities from release-criteria.yaml; outcomes from here. Bidirectional — see
# criteria-lib.sh. Exported because the consumer lives across a file boundary.
export STAGE_ID=build
# shellcheck source=scripts/release/criteria-lib.sh
source "$SCRIPT_DIR/criteria-lib.sh"

# Emits the criteria recorded SO FAR and exits the ORIGINAL code. Not
# criteria_assert_all_checked: on an early exit the later criteria genuinely were not
# checked, and the library exits 2 for that, which would turn this stage's gate failure (1)
# into a pipeline-misuse code.
build_fail_out() {
    local rc="$1"
    if [[ "$JSON_OUT" == "true" ]]; then
        printf '{"stage":"build","version":"%s","status":"fail","criteria":[%s],"next":["fix the build, then re-run: ./scripts/release.sh build %s"]}\n' \
            "$VERSION" "$(criteria_json)" "$VERSION"
    fi
    exit "$rc"
}

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
legs_declared=0
while IFS=$'\t' read -r component _capability platforms; do
    # blackwell is never part of a release build (built only on request, publishes no
    # versioned tag) — same exclusion 50-scan.sh applies.
    [[ "$component" == "blackwell" ]] && continue
    IFS=',' read -r -a plats <<< "$platforms"
    for platform in "${plats[@]}"; do
        [[ -n "$platform" ]] || continue
        legs_declared=$((legs_declared + 1))
        echo -e "${BLUE}  building ${component} for ${platform}${NC}" >&2
        use_remote="${USE_REMOTE_BUILDER:-false}"
        [[ "$platform" != "$HOST_PLATFORM" ]] && use_remote=true
        if ! PLATFORMS="$platform" BUILD_MODE=local PUSH_LATEST=false \
             SKIP_SECURITY_SCAN=true VERSION="$VERSION" \
             USE_REMOTE_BUILDER="$use_remote" \
                ./scripts/docker-build-push.sh "$component"; then
            echo -e "${RED}build failed: ${component} ${platform}${NC}" >&2
            record platform-table-readable pass "$legs_declared leg(s) declared"
            record every-declared-leg-built fail \
                "${component} ${platform} failed after ${legs_built} leg(s) built" \
                "read the build output above; USE_REMOTE_BUILDER=true is required for a non-host arch"
            build_fail_out 1
        fi
        legs_built=$((legs_built + 1))
    done
done < <(./scripts/docker-build-push.sh list-platforms)

# Zero declared legs is COULD NOT CHECK, never "nothing to build" — the same rule the scan
# stage applies to an empty platform list. A silently empty table would otherwise let this
# stage pass having built nothing, and `scan` would then find nothing to scan.
if (( legs_declared == 0 )); then
    record platform-table-readable not-measured \
        "docker-build-push.sh list-platforms yielded no legs" \
        "./scripts/docker-build-push.sh list-platforms"
    record every-declared-leg-built not-measured "no legs were declared"
    echo -e "${RED}no legs built — could not derive the platform table${NC}" >&2
    build_fail_out 1
fi
record platform-table-readable pass "$legs_declared leg(s) declared"
record every-declared-leg-built pass "$legs_built leg(s) built"
echo -e "${GREEN}built ${legs_built} architecture leg(s)${NC}" >&2

# ── The bare repo:vX.Y.Z tag must name the HOST leg ──────────────────────────────────
#
# `--load` cannot export a multi-arch manifest, so the loop above ran ONE build per declared
# platform and every one of them also wrote the bare `repo:$VERSION` tag. Docker tags are not
# additive: the LAST writer wins. The platform table lists amd64 before arm64, so on this amd64
# host `opentranscribe-frontend:$VERSION`, `opentranscribe-docs:$VERSION` and
# `opentranscribe-backend-lite:$VERSION` all ended up pointing at the ARM64 leg.
#
# This is a CORRECTNESS bug, not a cosmetic one, and it is invisible downstream:
#   * `test-fresh-install.sh:237-263` and `test-lite-mode.sh:243-252` reuse `repo:$VERSION`
#     when it already exists locally (no architecture check) — so the rehearsal that is
#     supposed to prove this release installs would run a foreign-arch image under QEMU, or
#     die with `exec format error`, for a reason that says nothing about the release.
#   * `security-scan.sh`'s local resolution DOES search leg tags, but frontend/docs had none
#     to find in local mode (fixed alongside this, in docker-build-push.sh's build_tag_args),
#     so it fell through to a Hub pull of a tag that is not published yet → COULD NOT SCAN.
#
# The loop's own baked-version check below already knew this ("checking [the bare tag] would
# silently test a different artifact depending on table ordering") and worked around it by
# using the leg tag. That was the right call for that one check and left every OTHER consumer
# holding the ambiguous tag. Re-pointing it here fixes it once, for all of them.
#
# Deliberately a re-TAG, not a rebuild: the host leg was just built and the leg tag is
# unambiguous, so this is a pointer move with no chance of producing different bytes.
#
# --- BEGIN bare-tag-host-arch ---
# (extracted verbatim and driven against a fake docker by
#  backend/tests/unit/test_build_bare_tag_host_arch.py — keep the markers)
declare -A ALL_REPO_FOR_COMPONENT=()
while IFS=$'\t' read -r component repo; do
    [[ -n "$component" && -n "$repo" ]] || continue
    [[ "$component" == "blackwell" ]] && continue   # publishes :blackwell, never :vX.Y.Z
    ALL_REPO_FOR_COMPONENT["$component"]="$repo"
done < <(./scripts/security-scan.sh list-repos)

host_arch="${HOST_PLATFORM#linux/}"
bare_retagged=0
bare_wrong=()
bare_no_host_leg=()
while IFS=$'\t' read -r component capability platforms; do
    [[ "$component" == "blackwell" ]] && continue
    repo="${ALL_REPO_FOR_COMPONENT[${component}]:-}"
    [[ -n "$repo" ]] || continue
    if [[ ",${platforms}," != *",${HOST_PLATFORM},"* ]]; then
        bare_no_host_leg+=("${component}")
        continue
    fi
    leg_tag="${repo}:${VERSION}-${capability}-${host_arch}"
    if ! docker tag "$leg_tag" "${repo}:${VERSION}" 2>/dev/null; then
        bare_wrong+=("${component}: no ${leg_tag} to re-tag from")
        continue
    fi
    # Read the artefact back, never the command's exit status: `docker tag` succeeding says
    # only that a name was written. Same discipline as security-scan.sh's post-pull check.
    actual="$(docker image inspect "${repo}:${VERSION}" --format '{{.Architecture}}' 2>/dev/null)"
    if [[ "$actual" != "$host_arch" ]]; then
        bare_wrong+=("${repo}:${VERSION} reports '${actual:-<unreadable>}', expected ${host_arch}")
        continue
    fi
    echo -e "${GREEN}PASS  ${repo}:${VERSION} -> ${host_arch} leg${NC}" >&2
    bare_retagged=$((bare_retagged + 1))
done < <(./scripts/docker-build-push.sh list-platforms)

if (( ${#bare_wrong[@]} )); then
    record bare-tag-is-host-arch fail "${bare_wrong[*]}" \
        "./scripts/release.sh build $VERSION   # the host leg did not build, or its leg tag is missing"
    echo -e "${RED}the bare :$VERSION tag does not name the ${host_arch} leg: ${bare_wrong[*]}${NC}" >&2
    build_fail_out 1
elif (( bare_retagged == 0 )); then
    # Zero checked is COULD NOT CHECK, never "nothing to do" — the same empty-set rule the
    # legs_declared branch above applies. A host with no matching leg for ANY component would
    # otherwise pass this silently while every rehearsal ran a foreign image.
    record bare-tag-is-host-arch not-measured \
        "no component declares a ${HOST_PLATFORM} leg (${bare_no_host_leg[*]:-none listed})" \
        "build on a host matching one of the declared platforms"
else
    record bare-tag-is-host-arch pass \
        "$bare_retagged bare tag(s) point at the ${host_arch} leg"
fi
# --- END bare-tag-host-arch ---

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
baked_checked=0
baked_wrong=()
no_host_leg=()
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
        no_host_leg+=("${component} (declares ${platforms})")
        continue
    fi
    leg_tag="${repo}:${VERSION}-${capability}-${HOST_PLATFORM#linux/}"

    baked=$(docker run --rm --entrypoint sh \
        "${leg_tag}" \
        -c 'echo "$APP_VERSION"' 2>/dev/null | tr -d '\r')
    baked_by_component["$component"]="$baked"

    baked_checked=$((baked_checked + 1))
    if [[ "$baked" != "$VERSION" ]]; then
        echo -e "${RED}FAIL  ${component} image reports '${baked:-<empty>}', expected ${VERSION}${NC}" >&2
        echo "      the --build-arg APP_VERSION contract is broken for ${component}" >&2
        baked_wrong+=("${component} reports '${baked:-<empty>}'")
        status=fail
    else
        echo -e "${GREEN}PASS  ${component} image reports ${baked}${NC}" >&2
    fi
done < <(./scripts/docker-build-push.sh list-platforms)

if (( ${#baked_wrong[@]} )); then
    record baked-version-host-leg fail "${baked_wrong[*]}" \
        "rebuild with --build-arg APP_VERSION=$VERSION — see the Dockerfile's ARG block"
elif (( baked_checked == 0 )); then
    # No backend-derived component had a host-arch leg to run. That is COULD NOT CHECK for
    # the baked-version contract, not a pass: the whole point of this assertion is that an
    # image reporting "unknown" must never ship (issue #411).
    record baked-version-host-leg not-measured \
        "no backend-derived component declares a ${HOST_PLATFORM} leg" \
        "build on a host matching one of the declared platforms, or check it after publish"
    status=fail
else
    record baked-version-host-leg pass "$baked_checked image(s) report $VERSION"
fi

# warn severity, matching today's behaviour: a component with no host-arch leg prints SKIP and
# the stage passes, because `docker run` cannot execute a foreign architecture. Recording it
# makes the gap visible in criteria[] rather than only in a SKIP line nobody greps.
if (( ${#no_host_leg[@]} )); then
    record host-arch-leg-present not-measured \
        "not run-checked on this host: ${no_host_leg[*]}" \
        "80-publish.sh verifies those legs' platform and size equivalence after publish"
else
    record host-arch-leg-present pass
fi

# Both halves of the contract. Reachable on every path that gets here.
criteria_assert_all_checked

if [[ "$JSON_OUT" == "true" ]]; then
    artifacts="{"
    first=true
    for component in "${!baked_by_component[@]}"; do
        [[ "$first" == true ]] || artifacts+=","
        first=false
        artifacts+="\"${component}_baked_version\":\"${baked_by_component[$component]}\""
    done
    artifacts+="}"
    printf '{"stage":"build","version":"%s","status":"%s","artifacts":%s,"criteria":[%s],"next":%s}\n' \
        "$VERSION" "$status" "$artifacts" "$(criteria_json)" \
        "$([[ "$status" == pass ]] && echo '["scan"]' || echo '["fix the build-arg contract and rebuild"]')"
fi

[[ "$status" == pass ]] || exit 1
exit 0
