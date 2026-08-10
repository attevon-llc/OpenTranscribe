#!/bin/bash
# Preflight — every check that can run before anything is built.
#
# Exists so a release fails in seconds rather than 45 minutes into a 13.8 GB
# build. Each check here has, at some point, been the thing that went wrong:
# a stale remote-builder IP, a dirty worktree, a missing HUGGINGFACE_TOKEN, a
# scanner that was never installed.
#
# Read-only. Starts nothing, builds nothing, writes nothing outside .release/.
#
# Exit: 0 pass · 1 a blocking check failed · 3 precondition unmet

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

PASS=0; FAIL=0; WARN=0
RESULTS=()

record() {
    local id="$1" status="$2" detail="${3:-}" fix="${4:-}"
    case "$status" in
        pass) PASS=$((PASS+1)); echo -e "  ${GREEN}PASS${NC}  $id" >&2 ;;
        warn) WARN=$((WARN+1)); echo -e "  ${YELLOW}WARN${NC}  $id  $detail" >&2 ;;
        fail) FAIL=$((FAIL+1)); echo -e "  ${RED}FAIL${NC}  $id  $detail" >&2
              [[ -n "$fix" ]] && echo -e "        fix: $fix" >&2 ;;
    esac
    RESULTS+=("$(printf '{"id":"%s","status":"%s","detail":"%s","fix":"%s"}' \
        "$id" "$status" "${detail//\"/\'}" "${fix//\"/\'}")")
}

echo "Preflight for ${VERSION:-<version from VERSION file>}" >&2

# ── Version consistency ────────────────────────────────────────────────────
if python3 scripts/release/check-version-consistency.py --mode ci >/dev/null 2>&1; then
    record version-consistency pass
else
    record version-consistency fail \
        "version sources disagree" \
        "python3 scripts/release/check-version-consistency.py --mode ci"
fi

# ── Clean worktree ─────────────────────────────────────────────────────────
# A release must be reproducible from its tag. Uncommitted changes mean the
# images would contain code that the tag does not.
if [[ -z "$(git status --porcelain)" ]]; then
    record clean-worktree pass
else
    record clean-worktree fail \
        "$(git status --porcelain | wc -l) uncommitted change(s)" \
        "commit or stash before releasing"
fi

# ── Remote ARM64 builder ───────────────────────────────────────────────────
# Checked HERE rather than at publish time, which is where it used to be
# discovered — after the amd64 build had already run. The builder's docker
# context silently went stale when the Mac Studio's DHCP lease changed.
BUILDER="${REMOTE_BUILDER_NAME:-opentranscribe-multiarch}"
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    record remote-builder warn \
        "buildx builder '$BUILDER' not found (single-arch builds only)" \
        "./scripts/setup-remote-builder.sh setup"
elif docker buildx inspect "$BUILDER" 2>/dev/null | grep -qi 'error'; then
    endpoint="$(docker context inspect remote-arm64 --format '{{.Endpoints.docker.Host}}' 2>/dev/null || echo '?')"
    record remote-builder fail \
        "builder '$BUILDER' has a node in error (endpoint: $endpoint)" \
        "./scripts/setup-remote-builder.sh --host user@<current-ip>"
else
    record remote-builder pass
fi

# ── Scanners ───────────────────────────────────────────────────────────────
missing_tools=()
for tool in trivy grype syft; do
    command -v "$tool" >/dev/null 2>&1 || missing_tools+=("$tool")
done
if [[ ${#missing_tools[@]} -eq 0 ]]; then
    record security-tooling pass
else
    record security-tooling warn \
        "not installed: ${missing_tools[*]} (scan stage will skip those)" \
        "see scripts/security-scan.sh — it can self-install"
fi

# ── Release-test secrets ───────────────────────────────────────────────────
# Without a HuggingFace token the PyAnnote model cannot download and BOTH
# scenarios fail at their first transcription — hours in.
SECRETS="scripts/release-tests/.env.test-secrets"
if [[ -f "$SECRETS" ]] && grep -qE '^HUGGINGFACE_TOKEN=hf_' "$SECRETS"; then
    record release-test-secrets pass
else
    record release-test-secrets warn \
        "no HUGGINGFACE_TOKEN in $SECRETS (rehearse stage will fail at transcription)" \
        "cp $SECRETS.example $SECRETS && edit"
fi

# ── Disk ───────────────────────────────────────────────────────────────────
avail_gb=$(df -BG --output=avail /var/lib/docker 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)
if [[ "${avail_gb:-0}" -ge 60 ]]; then
    record disk-space pass
else
    record disk-space warn "only ${avail_gb}GB free on the docker root (60GB+ recommended)"
fi

# ── Deployment matrix ──────────────────────────────────────────────────────
if ./scripts/validate-deployments.sh >/dev/null 2>&1; then
    record deployment-matrix pass
else
    record deployment-matrix fail \
        "a deployment permutation produces an invalid compose config" \
        "./scripts/validate-deployments.sh"
fi

# ── Live stack (informational here, blocking at `rehearse`) ────────────────
if docker ps --format '{{.Names}}' | grep -q '^opentranscribe-'; then
    record live-stack warn \
        "the live stack is running — the rehearse stage requires it stopped" \
        "./opentr.sh stop  (preserves all data)"
else
    record live-stack pass
fi

# ── Report ─────────────────────────────────────────────────────────────────
if [[ "$JSON_OUT" == "true" ]]; then
    joined=$(IFS=,; echo "${RESULTS[*]}")
    next='["proceed to bump/verify"]'
    [[ $FAIL -gt 0 ]] && next='["fix the failing criteria above, then re-run preflight"]'
    printf '{"stage":"preflight","version":"%s","status":"%s","criteria":[%s],"next":%s}\n' \
        "${VERSION:-unknown}" "$([[ $FAIL -eq 0 ]] && echo pass || echo fail)" "$joined" "$next"
fi

echo >&2
echo "—— $PASS passed, $FAIL failed, $WARN warnings" >&2
[[ $FAIL -eq 0 ]] || exit 1
exit 0
