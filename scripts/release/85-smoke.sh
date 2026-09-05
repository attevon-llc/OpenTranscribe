#!/bin/bash
# Install from Docker Hub and verify capability, post-publish.
#
# This is the first stage that tests what a user will actually pull, rather than
# what was built locally. issue #680's whole point is that "the manifest exists"
# and "reports a version string" are NOT the same claim as "has the capability its
# repository/tag promises" — the broken arm64 backend would have passed the old
# `$APP_VERSION` echo check. So this stage asserts CAPABILITY, per component:
#
#   lite  (CPU-only, opentranscribe-backend-lite): torch.version.cuda must be
#         EMPTY — a non-empty value here means the "lite" image actually shipped
#         CUDA wheels, which is the #680 failure mode in the other direction.
#   full  (CUDA, opentranscribe-backend):          torch.version.cuda must be
#         NON-EMPTY.
#   both: record onnxruntime's available execution providers (informational —
#         differs legitimately by arch/capability, not asserted equal), and run
#         `diar-server provision-models --mode cpu --json` with no token to
#         confirm the diar-native binary that was COPYed in is actually
#         runnable on this architecture (exit 5 TOKEN_DENIED expected — anything
#         else, especially "exec format error", is exactly what issue #680's
#         Dockerfile.lite/.prod bug would have produced on arm64 before the
#         per-TARGETARCH fix).
#
# `full` (opentranscribe-backend) ships amd64-only for v0.5.0 (see backend/
# Dockerfile.prod's diar-native-bin-arm64 stage) — its capability check always
# runs on amd64 here. `lite` ships both architectures, so its arm64 leg is
# checked over the remote builder's docker context when available.
#
# Exit: 0 verified · 1 failed · 3 live stack running

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
REMOTE_CTX="${REMOTE_ARM64_CONTEXT:-remote-arm64}"
HUB="${DOCKERHUB_USERNAME:-davidamacey}"
IMG_FULL="${HUB}/opentranscribe-backend:${VERSION}"
IMG_LITE="${HUB}/opentranscribe-backend-lite:${VERSION}"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
: "${VERSION:?85-smoke.sh needs a version}"

fail=0

# Run capability checks inside an image (optionally over a remote docker
# context) and assert torch.version.cuda's emptiness matches this component's
# declared capability.
assert_cuda_capability() {
    local label="$1" img="$2" ctx="$3" expect_nonempty="$4"

    local ctx_args=()
    [[ -n "$ctx" ]] && ctx_args=(--context "$ctx")

    # ⚠️ The exit code is load-bearing, and swallowing it was a fail-open: with
    # `2>/dev/null` and no rc check, an image that could not run AT ALL (wrong-arch
    # ELF, no python3, failed pull) yields an EMPTY string — which the lite branch
    # below reads as "torch.version.cuda is empty, CPU-only confirmed" and PASSES.
    # "could not check" must never look like "checked and correct" (issue #681's
    # rule, applied here). So: capture rc, keep stderr, and fail on a bad run.
    local run_out run_rc=0
    run_out=$(docker "${ctx_args[@]}" run --rm --pull always "$img" \
        python3 -c 'import torch; print(torch.version.cuda or "")' 2>&1) || run_rc=$?
    if [[ $run_rc -ne 0 ]]; then
        echo -e "${RED}FAIL  ${label}: could NOT run the capability probe (docker rc=${run_rc}) — this is 'not verified', not 'CPU-only'${NC}" >&2
        echo "      ${run_out}" >&2
        fail=1
        return 1
    fi
    local cuda_ver
    cuda_ver=$(printf '%s' "$run_out" | tr -d '\r' | tail -n 1)

    if [[ "$expect_nonempty" == "true" ]]; then
        if [[ -n "$cuda_ver" ]]; then
            echo -e "${GREEN}PASS  ${label}: torch.version.cuda='${cuda_ver}' (full/CUDA capability confirmed)${NC}" >&2
        else
            echo -e "${RED}FAIL  ${label}: torch.version.cuda is EMPTY — this is supposed to be the CUDA image${NC}" >&2
            fail=1
        fi
    else
        if [[ -z "$cuda_ver" ]]; then
            echo -e "${GREEN}PASS  ${label}: torch.version.cuda is empty (CPU-only capability confirmed)${NC}" >&2
        else
            echo -e "${RED}FAIL  ${label}: torch.version.cuda='${cuda_ver}' — this is supposed to be the lite/CPU image (issue #680)${NC}" >&2
            fail=1
        fi
    fi

    local providers
    providers=$(docker "${ctx_args[@]}" run --rm "$img" \
        python3 -c 'import onnxruntime as ort; print(",".join(ort.get_available_providers()))' 2>/dev/null | tr -d '\r')
    echo -e "${BLUE}      ${label}: onnxruntime providers = ${providers:-<none reported>}${NC}" >&2

    # diar-server must be the RIGHT-ARCH binary. exit 5 = TOKEN_DENIED (expected,
    # no token supplied) — anything else, especially a shell "exec format error"
    # (rc 126) or "cannot execute binary file" (rc 127), means the COPY --from
    # in this Dockerfile shipped a binary for the wrong architecture (#680).
    local diar_rc=0
    docker "${ctx_args[@]}" run --rm --entrypoint diar-server "$img" \
        provision-models --models-dir /tmp/diar-smoke --mode cpu --json >/dev/null 2>&1 || diar_rc=$?
    if [[ "$diar_rc" -eq 5 ]]; then
        echo -e "${GREEN}PASS  ${label}: diar-server runs on this architecture (exit 5 TOKEN_DENIED, as expected)${NC}" >&2
    else
        echo -e "${RED}FAIL  ${label}: diar-server exited ${diar_rc}, expected 5 (TOKEN_DENIED) — possible wrong-arch binary (#680)${NC}" >&2
        fail=1
    fi
}

echo -e "${BLUE}amd64: full/CUDA image capability${NC}" >&2
assert_cuda_capability "full-amd64" "$IMG_FULL" "" "true"

echo -e "${BLUE}amd64: lite/CPU image capability${NC}" >&2
assert_cuda_capability "lite-amd64" "$IMG_LITE" "" "false"

if docker context inspect "$REMOTE_CTX" >/dev/null 2>&1; then
    echo -e "${BLUE}arm64: lite/CPU image capability (over ${REMOTE_CTX})${NC}" >&2
    assert_cuda_capability "lite-arm64" "$IMG_LITE" "$REMOTE_CTX" "false"
else
    echo -e "${YELLOW}SKIP  no '${REMOTE_CTX}' docker context — lite arm64 unverified${NC}" >&2
fi
# full/CUDA has no arm64 leg for v0.5.0 (see backend/Dockerfile.prod) — nothing
# to check there yet; do NOT probe IMG_FULL over $REMOTE_CTX.

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
