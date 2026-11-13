#!/bin/bash
# Build the release candidate LOCALLY. Publishes nothing.
#
# The whole point of a separate build stage is that the artifact exists and can be
# scanned and rehearsed before anything reaches Docker Hub, and so that publish can then push
# those very images rather than rebuild them — :latest is what every
# existing user pulls, so it must not move before the scenarios pass.
#
# Records every leg's image ID in .release/<version>/legs.tsv: 80-publish.sh pushes exactly
# these images (no rebuild) and refuses if one no longer matches.
#
# Asserts the build-arg contract afterwards: an image reporting version "unknown"
# is a release-process failure, not a cosmetic one (issues #411).
#
# RELEASE_HOST_ARCH_ONLY=1 (`release.sh --host-arch-only`): build only the legs matching
# this host's architecture and never contact the remote builder. A recorded exception, not a
# pass: every-declared-leg-built is not-measured+waived (warn) and .release/<v>/host-arch-only
# is written beside legs.tsv so 80-publish.sh / 90-promote.sh refuse a partial release.
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
failed_leg=""

# One leg, one `docker-build-push.sh` call. Start/end lines carry a UTC timestamp and the
# duration, so the cost of each leg is readable from the log (RELEASE_SPEED_PLAN R9(2)).
build_leg() {
    local component="$1" platform="$2" use_remote leg_start rc=0
    use_remote="${USE_REMOTE_BUILDER:-false}"
    [[ "$platform" != "$HOST_PLATFORM" ]] && use_remote=true
    leg_start=$(date +%s)
    echo -e "${BLUE}  build leg ${component} ${platform} start $(date -u +%FT%TZ)${NC}" >&2
    # BUILD_CACHE_REGISTRY passed through (default remains OFF, set by
    # docker-build-push.sh's build_cache_args -- deliberately not defaulted to true
    # here). `build_cache_args` scopes this correctly for BUILD_MODE=local when opted
    # in -- read-only (`--cache-from`), never `--cache-to` (a registry cache WRITE,
    # which local mode's "publishes nothing" contract forbids) -- and stores it as a
    # `:buildcache-*` tag in the component's OWN repo, never touching `:vX.Y.Z`/`:latest`.
    # It stays an unmeasured, explicit opt-in (`BUILD_CACHE_REGISTRY=true
    # ./scripts/release.sh build ...`) -- see test_build_registry_cache_optin.py's
    # docstring for why that bar exists. 80-publish.sh no longer builds (issue #938's
    # "publish repeats this stage's work" is gone: it pushes these legs), so this is the
    # only build in a release.
    PLATFORMS="$platform" BUILD_MODE=local PUSH_LATEST=false \
        SKIP_SECURITY_SCAN=true VERSION="$VERSION" \
        USE_REMOTE_BUILDER="$use_remote" \
        BUILD_CACHE_REGISTRY="${BUILD_CACHE_REGISTRY:-false}" \
        ./scripts/docker-build-push.sh "$component" || rc=$?
    echo -e "${BLUE}  build leg ${component} ${platform} end $(date -u +%FT%TZ) rc=${rc} duration_s=$(( $(date +%s) - leg_start ))${NC}" >&2
    return "$rc"
}

host_legs=()
remote_legs=()
while IFS=$'\t' read -r component _capability platforms; do
    # blackwell is never part of a release build (built only on request, publishes no
    # versioned tag) -- same exclusion 50-scan.sh applies.
    [[ "$component" == "blackwell" ]] && continue
    IFS=',' read -r -a plats <<< "$platforms"
    for platform in "${plats[@]}"; do
        [[ -n "$platform" ]] || continue
        legs_declared=$((legs_declared + 1))
        if [[ "$platform" == "$HOST_PLATFORM" ]]; then
            host_legs+=("${component}|${platform}")
        else
            remote_legs+=("${component}|${platform}")
        fi
    done
done < <(./scripts/docker-build-push.sh list-platforms)

HOST_ONLY=false
[[ "${RELEASE_HOST_ARCH_ONLY:-}" == "1" || "${RELEASE_HOST_ARCH_ONLY:-}" == "true" ]] && HOST_ONLY=true
skipped_remote=0
if [[ "$HOST_ONLY" == "true" ]]; then
    skipped_remote=${#remote_legs[@]}
    remote_legs=()
    echo -e "${BLUE}host-arch-only: ${skipped_remote} non-host leg(s) will NOT be built; this release cannot be published${NC}" >&2
fi

# R4, OPT-IN and default OFF (measure the arm64 chain's length first; RELEASE_SPEED_PLAN R4):
# the non-host legs run on the remote builder as one background chain, serial among
# themselves, while host legs run here. Builder selection is per-process (BUILDX_BUILDER in
# docker-build-push.sh), so the two chains no longer race on a global `buildx use`. Every
# leg must still pass: each chain's exit status is reaped and either failure fails closed.
# Without the opt-in every leg runs serially (host legs first, then non-host), as before.
if [[ "${OT_BUILD_REMOTE_CONCURRENT:-false}" == "true" ]]; then
    ordered_host=("${host_legs[@]}")
    remote_log="$(mktemp -t ot-build-remote.XXXXXX)"
    (
        for leg in "${remote_legs[@]}"; do
            build_leg "${leg%%|*}" "${leg##*|}" || { echo "${leg}" > "${remote_log}.failed"; exit 1; }
        done
    ) >"$remote_log" 2>&1 &
    remote_pid=$!
    for leg in "${ordered_host[@]}"; do
        if build_leg "${leg%%|*}" "${leg##*|}"; then
            legs_built=$((legs_built + 1))
        else
            failed_leg="${leg%%|*} ${leg##*|}"
            break
        fi
    done
    if wait "$remote_pid"; then
        legs_built=$((legs_built + ${#remote_legs[@]}))
    elif [[ -z "$failed_leg" ]]; then
        failed_leg="$(tr '|' ' ' < "${remote_log}.failed" 2>/dev/null || echo 'a remote leg')"
    fi
    cat "$remote_log" >&2
    rm -f "$remote_log" "${remote_log}.failed"
else
    # B2, OPT-IN and default OFF (the docker root is HDD RAID6: measure before enabling): host-arch
    # legs run as background jobs, at most OT_BUILD_HOST_JOBS (default 2) at once, each with its own
    # mktemp log. Every pid is reaped with `wait <pid>`, logs replay in declared order, and the first
    # failed leg in declared order is named, so every-declared-leg-built stays fail-closed.
    # docker-build-push.sh was audited for shared mutable state between concurrent calls: its only
    # temp path is a unique mktemp, per-leg image tags are distinct, builder choice is per-process.
    if [[ "${OT_BUILD_HOST_CONCURRENT:-false}" == "true" && ${#host_legs[@]} -gt 0 ]]; then
        host_jobs="${OT_BUILD_HOST_JOBS:-2}"
        (( host_jobs >= 1 )) || host_jobs=1
        for ((i = 0; i < ${#host_legs[@]}; i += host_jobs)); do
            batch_pids=(); batch_logs=(); batch_legs=()
            for leg in "${host_legs[@]:i:host_jobs}"; do
                log="$(mktemp -t ot-build-host.XXXXXX)"
                build_leg "${leg%%|*}" "${leg##*|}" >"$log" 2>&1 &
                batch_pids+=($!); batch_logs+=("$log"); batch_legs+=("$leg")
            done
            for j in "${!batch_pids[@]}"; do
                if wait "${batch_pids[$j]}"; then
                    legs_built=$((legs_built + 1))
                elif [[ -z "$failed_leg" ]]; then
                    failed_leg="${batch_legs[$j]%%|*} ${batch_legs[$j]##*|}"
                fi
                cat "${batch_logs[$j]}" >&2
                rm -f "${batch_logs[$j]}"
            done
            [[ -z "$failed_leg" ]] || break
        done
        host_legs=()
    fi
    for leg in "${host_legs[@]}" "${remote_legs[@]}"; do
        [[ -n "$leg" ]] || continue
        [[ -z "$failed_leg" ]] || break
        if build_leg "${leg%%|*}" "${leg##*|}"; then
            legs_built=$((legs_built + 1))
        else
            failed_leg="${leg%%|*} ${leg##*|}"
            break
        fi
    done
fi

if [[ -n "$failed_leg" ]]; then
    echo -e "${RED}build failed: ${failed_leg}${NC}" >&2
    record platform-table-readable pass "$legs_declared leg(s) declared"
    record every-declared-leg-built fail \
        "${failed_leg} failed after ${legs_built} leg(s) built" \
        "read the build output above; USE_REMOTE_BUILDER=true is required for a non-host arch"
    build_fail_out 1
fi

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
if [[ "$HOST_ONLY" == "true" ]]; then
    record every-declared-leg-built not-measured \
        "host-arch-only: ${skipped_remote} non-host legs not built" \
        "run ./scripts/release.sh build ${VERSION} without --host-arch-only before publish" waived
else
    record every-declared-leg-built pass "$legs_built leg(s) built"
fi
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

# ── Record the image ID of every leg, so publish can push EXACTLY these bytes ────────
#
# 80-publish.sh no longer rebuilds: it pushes the leg tags this stage produced and assembles
# the indexes from the pushed digests. "Exactly these bytes" is only checkable against
# something written down NOW, so the local image ID (the config digest on a classic-store
# daemon) and architecture of each leg go to .release/<version>/legs.tsv. Publish refuses
# (exit 1, naming the leg) when a leg's ID no longer matches — a rebuild or re-tag between
# build and publish would otherwise ship bytes that scan and rehearse never saw.
#
# The architecture is read back too: an arm64 leg `--load`ed from the remote builder is only
# a name in the local daemon, and BuildKit does not reject a wrong-arch base (see
# docker-build-push.sh's build_platforms note). The file is written only if EVERY leg
# resolved, atomically, so a half-written file can never read as a complete one.
#
# --- BEGIN leg-image-ids ---
# (extracted verbatim and driven against a fake docker by
#  backend/tests/unit/test_publish_pushes_built_legs.py — keep the markers)
LEGS_FILE="${OT_LEGS_FILE:-.release/${VERSION}/legs.tsv}"
leg_rows=()
leg_problems=()
while IFS=$'\t' read -r component capability platforms; do
    [[ "$component" == "blackwell" ]] && continue
    leg_repo="${ALL_REPO_FOR_COMPONENT[${component}]:-}"
    if [[ -z "$leg_repo" ]]; then
        leg_problems+=("${component}: no repository in security-scan.sh list-repos")
        continue
    fi
    IFS=',' read -r -a leg_plats <<< "$platforms"
    for leg_platform in "${leg_plats[@]}"; do
        [[ -n "$leg_platform" ]] || continue
        [[ "${HOST_ONLY:-false}" == "true" && "$leg_platform" != "${HOST_PLATFORM:-}" ]] && continue
        leg_arch="${leg_platform#linux/}"
        leg_tag="${leg_repo}:${VERSION}-${capability}-${leg_arch}"
        leg_id="$(docker image inspect "$leg_tag" --format '{{.Id}}' 2>/dev/null)"
        leg_actual_arch="$(docker image inspect "$leg_tag" --format '{{.Architecture}}' 2>/dev/null)"
        if [[ -z "$leg_id" ]]; then
            leg_problems+=("${leg_tag}: no local image ID")
        elif [[ "$leg_actual_arch" != "$leg_arch" ]]; then
            leg_problems+=("${leg_tag}: reports architecture '${leg_actual_arch:-<unreadable>}', expected ${leg_arch}")
        else
            leg_rows+=("${component}"$'\t'"${leg_repo}"$'\t'"${leg_platform}"$'\t'"${leg_tag}"$'\t'"${leg_id}")
        fi
    done
done < <(./scripts/docker-build-push.sh list-platforms)

if (( ${#leg_problems[@]} )); then
    record leg-image-ids-recorded fail "${leg_problems[*]}" \
        "./scripts/release.sh build $VERSION   # a declared leg is missing or is the wrong architecture"
    echo -e "${RED}could not capture every leg's image ID: ${leg_problems[*]}${NC}" >&2
    build_fail_out 1
elif (( ${#leg_rows[@]} == 0 )); then
    record leg-image-ids-recorded not-measured "no legs resolved, so nothing was recorded" \
        "./scripts/docker-build-push.sh list-platforms"
    build_fail_out 1
else
    mkdir -p "$(dirname "$LEGS_FILE")"
    printf '%s\n' "${leg_rows[@]}" > "${LEGS_FILE}.tmp" && mv "${LEGS_FILE}.tmp" "$LEGS_FILE"
    record leg-image-ids-recorded pass "${#leg_rows[@]} leg image ID(s) -> ${LEGS_FILE}"
fi
# --- END leg-image-ids ---

# Every failure above exited, so legs.tsv was just written. The marker is the ledger evidence
# 80-publish.sh / 90-promote.sh read to refuse a partial release; a full build clears it.
HOST_ONLY_MARKER="$(dirname "$LEGS_FILE")/host-arch-only"
if [[ "$HOST_ONLY" == "true" ]]; then
    echo "host-arch-only: ${skipped_remote} non-host legs not built" > "$HOST_ONLY_MARKER"
else
    rm -f "$HOST_ONLY_MARKER"
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

    # Metadata read, not a container: `docker run` on the CUDA image attaches every GPU
    # (nvidia default runtime + NVIDIA_VISIBLE_DEVICES), including ones this project must not touch.
    baked=$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
        "${leg_tag}" 2>/dev/null | sed -n 's/^APP_VERSION=//p' | tail -n1 | tr -d '\r')
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
