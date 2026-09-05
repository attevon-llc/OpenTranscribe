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
#   (c) for a component whose index declares >1 platform, those platforms are
#       equivalent to each other: same layer count, size ratio within the
#       component's purpose bound (1.25 for cpu/multiarch — lite, frontend,
#       docs; 2.00 for cuda) — NEVER compared across purposes/components.
#
# ⚠️ (c) MUST be fed per-platform MANIFESTS, not the index. An index carries no
# `layers` at all and its per-entry `size` is the ~2 KB manifest blob, so feeding
# it to check-ratio reports `sizes a=0 b=0 ratio=inf` and fails every time — even
# comparing an index against itself (measured against the real published
# opentranscribe-backend:v0.4.1 index). Hence resolve-digest + a second inspect.
#
# A checker exit of 3 means COULD NOT CHECK (malformed/absent/wrong-shape doc),
# never "checked and fine" — reported separately from a mismatch below, for the
# same reason security-scan.sh separates its exit 1 from its exit 2 (issue #681).
CHECKER="./scripts/lib/manifest_platform_check.py"
manifest_check_rc=0
MANIFEST_TMP="$(mktemp -d)"
trap 'rm -rf "$MANIFEST_TMP"' EXIT

# Fetch a ref's raw manifest JSON into $2. Returns 1 when nothing came back, so a
# registry/auth failure is never mistaken for an empty-but-valid document.
inspect_raw() {
    local ref="$1" out="$2"
    docker buildx imagetools inspect "$ref" --raw >"$out" 2>/dev/null
    [[ -s "$out" ]]
}

# Print a verdict, keeping "checked and WRONG" (rc 1) distinct from "could not
# check at all" (rc 3). Both fail the stage; only one is a claim about the image.
report_check() {
    local rc="$1" label="$2" msg="$3"
    case "$rc" in
        0) echo -e "${GREEN}${label}: ${msg}${NC}" >&2 ;;
        3) echo -e "${RED}${label}: NOT VERIFIED — ${msg}${NC}" >&2; manifest_check_rc=1 ;;
        *) echo -e "${RED}${label}: FAILED — ${msg}${NC}" >&2; manifest_check_rc=1 ;;
    esac
}

# Resolve $platform out of the index in $1 and write that platform's own manifest
# (the document that actually has `layers`) to $3. Returns non-zero on any failure.
platform_manifest() {
    local index_file="$1" platform="$2" out="$3" repo="$4"
    local digest
    digest=$(python3 "$CHECKER" resolve-digest "$index_file" "$platform") || return 1
    inspect_raw "${repo}@${digest}" "$out"
}

while IFS=$'\t' read -r component capability platforms; do
    [[ "$component" == "blackwell" ]] && continue  # never published by `all`
    repo="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-backend"
    [[ "$component" == "lite" ]] && repo="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-backend-lite"
    [[ "$component" == "frontend" ]] && repo="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-frontend"
    [[ "$component" == "docs" ]] && repo="${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-docs"

    IFS=',' read -r -a arch_list <<< "$platforms"

    # (a) Leg tags exist only for capability-bearing components (build_leg_tag).
    # frontend/docs publish ONE multi-platform build under repo:vX.Y.Z directly
    # (build_tag_args), so they have no legs to check — (b) and (c) still apply.
    if [[ "$capability" != "multiarch" ]]; then
        for arch in "${arch_list[@]}"; do
            leg_tag="${repo}:${VERSION}-${capability}-${arch#linux/}"
            leg_file="${MANIFEST_TMP}/${component}-leg-${arch#linux/}.json"
            if ! inspect_raw "$leg_tag" "$leg_file"; then
                report_check 3 "(a) ${leg_tag}" "could not inspect the leg tag at all"
                continue
            fi
            leg_rc=0
            out=$(python3 "$CHECKER" check-leg "$leg_file" "$arch") || leg_rc=$?
            # A leg tag pushed WITHOUT provenance attestations resolves to a bare
            # image manifest, which declares no platform at all (architecture lives
            # in the config blob that --raw does not fetch), so check-leg can only
            # answer CANNOT CHECK. MEASURED against the real published
            # davidamacey/diar-native:0.3.1-cpu, whose --raw is exactly that shape.
            # Falling back to the config blob is what keeps (a) a GATE rather than a
            # permanent "not verified" — attestations are a builder setting, and a
            # check that can never pass is not a check. `--format '{{json .Image}}'`
            # fetches the config blob without pulling the image.
            if [[ $leg_rc -eq 3 ]]; then
                cfg_file="${MANIFEST_TMP}/${component}-legcfg-${arch#linux/}.json"
                if docker buildx imagetools inspect "$leg_tag" \
                        --format '{{json .Image}}' >"$cfg_file" 2>/dev/null \
                   && [[ -s "$cfg_file" ]]; then
                    leg_rc=0
                    out=$(python3 "$CHECKER" check-image-config "$cfg_file" "$arch") || leg_rc=$?
                fi
            fi
            report_check "$leg_rc" "(a) ${leg_tag}" "$out"
        done
    fi

    # (b) The published index must declare EXACTLY the component's platform set.
    idx_file="${MANIFEST_TMP}/${component}-index.json"
    if ! inspect_raw "${repo}:${VERSION}" "$idx_file"; then
        report_check 3 "(b) ${repo}:${VERSION}" "could not inspect the index at all"
        continue
    fi
    idx_rc=0
    out=$(python3 "$CHECKER" check-index "$idx_file" "$platforms") || idx_rc=$?
    report_check "$idx_rc" "(b) ${repo}:${VERSION} vs declared (${platforms})" "$out"

    # (c) Cross-arch equivalence, within THIS component only. Applies to every
    # component whose index declares two platforms — lite AND frontend/docs, which
    # is what makes the 1.25 bound documented for them in releasing.md real rather
    # than aspirational.
    [[ ${#arch_list[@]} -eq 2 ]] || continue
    bound="1.25"
    [[ "$capability" == "cuda" ]] && bound="2.00"
    a_file="${MANIFEST_TMP}/${component}-plat-a.json"
    b_file="${MANIFEST_TMP}/${component}-plat-b.json"
    if ! platform_manifest "$idx_file" "${arch_list[0]}" "$a_file" "$repo" \
       || ! platform_manifest "$idx_file" "${arch_list[1]}" "$b_file" "$repo"; then
        report_check 3 "(c) ${component}" \
            "could not resolve both per-platform manifests (${arch_list[0]}, ${arch_list[1]}) from the index"
        continue
    fi
    ratio_rc=0
    out=$(python3 "$CHECKER" check-ratio "$a_file" "$b_file" "$bound") || ratio_rc=$?
    report_check "$ratio_rc" "(c) ${component} legs within bound ${bound}" "$out"
done < <(./scripts/docker-build-push.sh list-platforms)

if [[ $manifest_check_rc -ne 0 ]]; then
    echo -e "${RED}manifest structure checks FAILED — refusing to report a clean publish${NC}" >&2
    exit 1
fi

echo -e "${GREEN}published ${VERSION} — manifest structure verified for every declared leg/index${NC}" >&2
[[ "$JSON_OUT" == "true" ]] && printf '{"stage":"publish","version":"%s","status":"pass","next":["smoke"]}\n' "$VERSION"
exit 0
