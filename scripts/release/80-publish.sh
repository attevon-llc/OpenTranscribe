#!/bin/bash
# Multi-arch build and push of :vX.Y.Z ONLY. Never :latest.
#
# :latest moves later, by digest copy (90-promote.sh), so :latest and :vX.Y.Z are
# provably the same bytes rather than two builds that happen to share a tree.
#
# ARM64 goes to the remote builder: the backend image is ~13.8 GB and QEMU
# emulation turns 20 minutes into 2-3 hours.
#
# Exit: 0 published · 1 failed · 3 builder unreachable

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
BUILDER="${REMOTE_BUILDER_NAME:-opentranscribe-multiarch}"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
: "${VERSION:?80-publish.sh needs a version}"

if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    echo -e "${RED}buildx builder '$BUILDER' missing — cannot publish multi-arch${NC}" >&2
    echo "  ./scripts/setup-remote-builder.sh setup" >&2
    exit 3
fi
if docker buildx inspect "$BUILDER" 2>/dev/null | grep -qi 'error'; then
    echo -e "${RED}'$BUILDER' has a node in error (the remote host moved?)${NC}" >&2
    echo "  ./scripts/setup-remote-builder.sh --host user@<current-ip>" >&2
    exit 3
fi

echo -e "${YELLOW}PUBLISHING ${VERSION} to Docker Hub (:${VERSION} only, not :latest)${NC}" >&2
rc=0
USE_REMOTE_BUILDER=true BUILD_MODE=push PUSH_LATEST=false VERSION="$VERSION" \
    ./scripts/docker-build-push.sh all || rc=$?
[[ $rc -eq 0 ]] || { echo -e "${RED}publish failed${NC}" >&2; exit 1; }

# --- Manifest-structure checks (issue #680) ---------------------------------
#
# The pre-#680 version of this check only asked "does a manifest entry exist per
# arch" — grep for `"architecture": "$arch"` on the raw manifest text. That is
# exactly the check a broken-but-present arm64 leg still passes: #680's arm64
# backend manifest EXISTED, had 11 layers same as amd64, and was still 8.4x
# smaller. Existence is not equivalence. Three checks now, all driven off
# `docker-build-push.sh list-platforms` rather than a hardcoded `backend frontend`
# pair, via the shared parser in scripts/lib/manifest_platform_check.py:
#
#   (a) each -<cap>-<arch> leg tag declares EXACTLY the one platform it claims
#   (b) each vX.Y.Z index declares EXACTLY the component's declared platform set
#       (missing OR extra both fail)
#   (c) for a component with >1 platform, its legs are equivalent to each other:
#       same layer count, size ratio within the component's purpose bound
#       (1.25 for lite/frontend/docs, 2.00 for full/backend) — NEVER compared
#       across purposes/components.
CHECKER="./scripts/lib/manifest_platform_check.py"
manifest_check_rc=0

while IFS=$'\t' read -r component capability platforms; do
    [[ "$component" == "blackwell" ]] && continue  # never published by `all`
    repo="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-backend"
    [[ "$component" == "lite" ]] && repo="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-backend-lite"
    [[ "$component" == "frontend" ]] && repo="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-frontend"
    [[ "$component" == "docs" ]] && repo="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-docs"

    IFS=',' read -r -a arch_list <<< "$platforms"

    # frontend/docs carry no capability leg tags (build_tag_args, not build_leg_tag)
    # — they publish ONE multi-platform build under repo:vX.Y.Z directly, so only
    # check (b) applies to them.
    if [[ "$capability" == "multiarch" ]]; then
        idx_json=$(docker buildx imagetools inspect "${repo}:${VERSION}" --raw 2>/dev/null)
        if [[ -z "$idx_json" ]]; then
            echo -e "${RED}could not inspect ${repo}:${VERSION}${NC}" >&2
            manifest_check_rc=1
            continue
        fi
        expect_csv="$platforms"
        out=$(echo "$idx_json" | python3 "$CHECKER" check-index /dev/stdin "$expect_csv") || {
            echo -e "${RED}(b) index platform-set check FAILED for ${repo}:${VERSION}: ${out}${NC}" >&2
            manifest_check_rc=1
            continue
        }
        echo -e "${GREEN}(b) ${repo}:${VERSION} index matches declared platforms (${platforms}): ${out}${NC}" >&2
        continue
    fi

    # Capability-bearing component: check each leg (a), then the index (b), then
    # cross-arch equivalence within THIS component only (c).
    leg_files=()
    for arch in "${arch_list[@]}"; do
        arch_short="${arch#linux/}"
        leg_tag="${repo}:${VERSION}-${capability}-${arch_short}"
        leg_json=$(docker buildx imagetools inspect "$leg_tag" --raw 2>/dev/null)
        if [[ -z "$leg_json" ]]; then
            echo -e "${RED}could not inspect leg ${leg_tag}${NC}" >&2
            manifest_check_rc=1
            continue
        fi
        leg_rc=0
        out=$(echo "$leg_json" | python3 "$CHECKER" check-leg /dev/stdin "$arch") || leg_rc=$?
        if [[ $leg_rc -eq 0 ]]; then
            echo -e "${GREEN}(a) ${leg_tag}: ${out}${NC}" >&2
        else
            echo -e "${RED}(a) leg check FAILED for ${leg_tag}: ${out}${NC}" >&2
            manifest_check_rc=1
        fi
        leg_file="$(mktemp)"
        echo "$leg_json" > "$leg_file"
        leg_files+=("$leg_file")
    done

    idx_json=$(docker buildx imagetools inspect "${repo}:${VERSION}" --raw 2>/dev/null)
    if [[ -n "$idx_json" ]]; then
        idx_rc=0
        out=$(echo "$idx_json" | python3 "$CHECKER" check-index /dev/stdin "$platforms") || idx_rc=$?
        if [[ $idx_rc -eq 0 ]]; then
            echo -e "${GREEN}(b) ${repo}:${VERSION} index matches declared platforms (${platforms}): ${out}${NC}" >&2
        else
            echo -e "${RED}(b) index platform-set check FAILED for ${repo}:${VERSION}: ${out}${NC}" >&2
            manifest_check_rc=1
        fi
    else
        echo -e "${RED}could not inspect index ${repo}:${VERSION}${NC}" >&2
        manifest_check_rc=1
    fi

    if [[ ${#leg_files[@]} -eq 2 ]]; then
        bound="2.00"
        [[ "$capability" == "cpu" ]] && bound="1.25"
        ratio_rc=0
        out=$(python3 "$CHECKER" check-ratio "${leg_files[0]}" "${leg_files[1]}" "$bound") || ratio_rc=$?
        if [[ $ratio_rc -eq 0 ]]; then
            echo -e "${GREEN}(c) ${component}: legs equivalent within bound ${bound}: ${out}${NC}" >&2
        else
            echo -e "${RED}(c) equivalence check FAILED for ${component} (bound ${bound}): ${out}${NC}" >&2
            manifest_check_rc=1
        fi
    fi
    for f in "${leg_files[@]}"; do rm -f "$f"; done
done < <(./scripts/docker-build-push.sh list-platforms)

if [[ $manifest_check_rc -ne 0 ]]; then
    echo -e "${RED}manifest structure checks FAILED — refusing to report a clean publish${NC}" >&2
    exit 1
fi

echo -e "${GREEN}published ${VERSION} — manifest structure verified for every declared leg/index${NC}" >&2
[[ "$JSON_OUT" == "true" ]] && printf '{"stage":"publish","version":"%s","status":"pass","next":["smoke"]}\n' "$VERSION"
exit 0
