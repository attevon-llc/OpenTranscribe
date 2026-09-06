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

# Severities from release-criteria.yaml; outcomes from here. Bidirectional — see
# criteria-lib.sh. Exported because the consumer lives across a file boundary.
export STAGE_ID=smoke
# shellcheck source=scripts/release/criteria-lib.sh
source "$SCRIPT_DIR/criteria-lib.sh"

fail=0

# Per-criterion tallies. Three separate properties are asserted per image and they are NOT
# interchangeable — a probe that could not RUN, a probe that ran and disagreed with the
# declared capability, and a wrong-architecture diar-server binary are three different
# findings. Collapsing them into `fail` is what would let criteria[] say only "smoke failed".
probe_ran=0;    probe_unrunnable=()
cap_checked=0;  cap_mismatch=()
diar_checked=0; diar_wrong=()

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
        probe_unrunnable+=("$label")
        fail=1
        return 1
    fi
    probe_ran=$((probe_ran + 1))
    local cuda_ver
    cuda_ver=$(printf '%s' "$run_out" | tr -d '\r' | tail -n 1)

    cap_checked=$((cap_checked + 1))
    if [[ "$expect_nonempty" == "true" ]]; then
        if [[ -n "$cuda_ver" ]]; then
            echo -e "${GREEN}PASS  ${label}: torch.version.cuda='${cuda_ver}' (full/CUDA capability confirmed)${NC}" >&2
        else
            echo -e "${RED}FAIL  ${label}: torch.version.cuda is EMPTY — this is supposed to be the CUDA image${NC}" >&2
            cap_mismatch+=("${label}: CUDA image reports no CUDA")
            fail=1
        fi
    else
        if [[ -z "$cuda_ver" ]]; then
            echo -e "${GREEN}PASS  ${label}: torch.version.cuda is empty (CPU-only capability confirmed)${NC}" >&2
        else
            echo -e "${RED}FAIL  ${label}: torch.version.cuda='${cuda_ver}' — this is supposed to be the lite/CPU image (issue #680)${NC}" >&2
            cap_mismatch+=("${label}: lite image reports CUDA ${cuda_ver}")
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
    diar_checked=$((diar_checked + 1))
    if [[ "$diar_rc" -eq 5 ]]; then
        echo -e "${GREEN}PASS  ${label}: diar-server runs on this architecture (exit 5 TOKEN_DENIED, as expected)${NC}" >&2
    else
        echo -e "${RED}FAIL  ${label}: diar-server exited ${diar_rc}, expected 5 (TOKEN_DENIED) — possible wrong-arch binary (#680)${NC}" >&2
        diar_wrong+=("${label}: rc=${diar_rc}")
        fail=1
    fi
}

echo -e "${BLUE}amd64: full/CUDA image capability${NC}" >&2
assert_cuda_capability "full-amd64" "$IMG_FULL" "" "true"

echo -e "${BLUE}amd64: lite/CPU image capability${NC}" >&2
assert_cuda_capability "lite-amd64" "$IMG_LITE" "" "false"

if docker context inspect "$REMOTE_CTX" >/dev/null 2>&1; then
    echo -e "${BLUE}arm64: lite/CPU image capability (over ${REMOTE_CTX})${NC}" >&2
    arm_rc=0
    assert_cuda_capability "lite-arm64" "$IMG_LITE" "$REMOTE_CTX" "false" || arm_rc=$?
    if (( arm_rc == 0 )); then
        record lite-arm64-checked pass "probed over ${REMOTE_CTX}"
    else
        record lite-arm64-checked fail "the arm64 lite probe could not run over ${REMOTE_CTX}"
    fi
else
    # `warn` severity in release-criteria.yaml, matching the SKIP this has always printed.
    # Substantively this is the pipeline's weakest link — arm64 lite is the ONLY backend an
    # arm64 host can install, since opentranscribe.sh defaults arm64 to DEPLOYMENT_MODE=lite —
    # so an operator without the context ships it unexercised. Recording it as not-measured is
    # what makes that visible in criteria[] instead of only in a SKIP line.
    record lite-arm64-checked not-measured "no '${REMOTE_CTX}' docker context" \
        "docker context create ${REMOTE_CTX} --docker host=ssh://user@<arm64-host>"
    echo -e "${YELLOW}SKIP  no '${REMOTE_CTX}' docker context — lite arm64 unverified${NC}" >&2
fi
# full/CUDA has no arm64 leg for v0.5.0 (see backend/Dockerfile.prod) — nothing
# to check there yet; do NOT probe IMG_FULL over $REMOTE_CTX.

# ── amd64: the documented one-liner, against Docker Hub ────────────────────
# Filter by compose project label, not a name prefix -- see 10-preflight.sh's
# live-stack check for why a naive prefix match false-positives on unrelated
# containers (e.g. "opentranscribe-homepage").
#
# Captured rather than piped into `grep -q`: under `set -o pipefail` grep -q closes the pipe
# on its first match and `docker ps` can die with SIGPIPE, so the pipeline reports failure and
# a stack that IS up reads as an all-clear — the guard inverts exactly when it matters. Same
# fix as 65-rehearse.sh's live-stack check.
if [[ -n "$(docker ps --filter 'label=com.docker.compose.project=opentranscribe' --format '{{.Names}}')" ]]; then
    record live-stack-stopped fail "the live stack is running" "./opentr.sh stop"
    echo -e "${RED}live stack running — the Hub install smoke needs it stopped${NC}" >&2
    if [[ "$JSON_OUT" == "true" ]]; then
        printf '{"stage":"smoke","version":"%s","status":"fail","criteria":[%s],"next":["./opentr.sh stop"]}\n' \
            "$VERSION" "$(criteria_json)"
    fi
    # Exit 3 (precondition unmet) as before, and deliberately NOT through
    # criteria_assert_all_checked: the install smoke never ran, so its criteria are genuinely
    # unchecked, and the library's exit 2 would bury the real reason behind a wiring error.
    exit 3
fi
record live-stack-stopped pass

echo -e "${BLUE}amd64: fresh install pulling :${VERSION} from Docker Hub${NC}" >&2
install_rc=0
USE_HUB_IMAGES=true LOCAL_IMAGE_TAG="$VERSION" \
    ./scripts/release-tests/test-fresh-install.sh --yes --force || install_rc=$?
if (( install_rc == 0 )); then
    record hub-fresh-install pass
else
    record hub-fresh-install fail "test-fresh-install.sh exited $install_rc against :$VERSION" \
        "read the REPORT.md under its TEST_ROOT"
    fail=1
fi

# The three per-image properties, each its own criterion. `capability-probe-ran` is separate
# from `declared-capability-matches` on purpose: an image that cannot execute at all prints an
# empty string too, so folding them together turns "could not check" into "CPU-only
# confirmed" — the #680 failure mode with a green tick, in the stage whose job is to catch it.
if (( ${#probe_unrunnable[@]} )); then
    record capability-probe-ran fail "probe could not run for: ${probe_unrunnable[*]}" \
        "check the pull succeeded and the image is the right architecture"
elif (( probe_ran == 0 )); then
    record capability-probe-ran not-measured "no image was probed at all"
else
    record capability-probe-ran pass "$probe_ran image(s) probed"
fi

if (( ${#cap_mismatch[@]} )); then
    record declared-capability-matches fail "${cap_mismatch[*]}"
elif (( cap_checked == 0 )); then
    record declared-capability-matches not-measured "no probe produced a value to compare"
else
    record declared-capability-matches pass "$cap_checked image(s) match their declared capability"
fi

if (( ${#diar_wrong[@]} )); then
    record diar-server-arch-correct fail "${diar_wrong[*]}" \
        "expected exit 5 (TOKEN_DENIED); 126/127 means a wrong-architecture binary (#680)"
elif (( diar_checked == 0 )); then
    record diar-server-arch-correct not-measured "diar-server was never executed"
else
    record diar-server-arch-correct pass "$diar_checked image(s) ran diar-server"
fi

# Both halves of the contract. Reachable on both outcomes.
criteria_assert_all_checked

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"smoke","version":"%s","status":"%s","criteria":[%s],"next":%s}\n' \
        "$VERSION" "$([[ $fail -eq 0 ]] && echo pass || echo fail)" "$(criteria_json)" \
        "$([[ $fail -eq 0 ]] && echo '["promote"]' || echo '["do NOT promote :latest"]')"
fi
exit $fail
