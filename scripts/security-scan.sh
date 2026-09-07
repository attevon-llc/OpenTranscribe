#!/bin/bash
set -e

# Security Scanning Script for OpenTranscribe Docker Images
# Uses free, open-source tools to scan for vulnerabilities and security issues
# No Docker Hub/Scout subscription required

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
DOCKERHUB_USERNAME="${DOCKERHUB_USERNAME:-davidamacey}"
REPO_BACKEND="${DOCKERHUB_USERNAME}/opentranscribe-backend"
REPO_BACKEND_LITE="${DOCKERHUB_USERNAME}/opentranscribe-backend-lite"
REPO_FRONTEND="${DOCKERHUB_USERNAME}/opentranscribe-frontend"
REPO_DOCS="${DOCKERHUB_USERNAME}/opentranscribe-docs"
SCAN_TARGET="${1:-all}"
OUTPUT_DIR="${OUTPUT_DIR:-./security-reports}"
SEVERITY_THRESHOLD="${SEVERITY_THRESHOLD:-MEDIUM}"
FAIL_ON_CRITICAL="${FAIL_ON_CRITICAL:-true}"

# Exit codes. THREE outcomes, not two (issue #681).
#
#   0  scanned, nothing blocking
#   1  SCANNED, and the scan found something
#   2  COULD NOT SCAN — unknown component, image unobtainable, or a sub-scan
#      that died without recording a verdict
#
# 1 and 2 must never be collapsed into a single "non-zero" branch. Tolerating
# findings is a policy choice a caller is entitled to make
# (FAIL_ON_SECURITY_ISSUES); "we never looked" is not a choice anybody made, so
# a caller cannot be allowed to wave it through with the same flag. Collapsing
# them is exactly how an unscannable component produced
# "All security scans completed successfully!".
readonly EXIT_FINDINGS=1
readonly EXIT_COULD_NOT_SCAN=2

# Per-ARCHITECTURE scanning (issue #667).
#
# A multi-arch tag is not one artifact, it is an index over several, and a CVE report on one
# leg says nothing about the others: different base layers, different compiled dependencies,
# different vulnerable versions. `opentranscribe-backend-lite` publishes amd64 AND arm64, and
# on arm64 hosts it is the ONLY backend available (opentranscribe.sh defaults arm64 to
# DEPLOYMENT_MODE=lite), yet the registry-pull path hardcoded `--platform linux/amd64` — so
# the arm64 leg would have shipped having never been looked at, and the amd64 report would
# have sat beside it looking like coverage.
#
# SCAN_PLATFORM=linux/arm64 scans exactly one leg. Unset — the default — scans EVERY platform
# the component declares in docker-build-push.sh's COMPONENT_PLATFORMS table, which is the
# single home for that set. Never transcribe a platform list here.
SCAN_PLATFORM="${SCAN_PLATFORM:-}"

# Where the platform set comes from. Asking the other script keeps one home for the table;
# `list-platforms` needs no Docker, no login and no version, and exits before any of those
# checks, so this is a cheap pure query and not a circular build dependency.
PLATFORM_SOURCE="${PLATFORM_SOURCE:-./scripts/docker-build-push.sh}"

# Platforms a component declares, one per line. Emits NOTHING on any failure — callers must
# treat an empty result as COULD NOT SCAN, never as "no platforms to scan".
component_platforms() {
    local component="$1"
    "${PLATFORM_SOURCE}" list-platforms 2>/dev/null \
        | awk -F'\t' -v c="${component}" '$1 == c { print $3 }' \
        | tr ',' '\n' \
        | sed '/^$/d'
}

# The report-filename stem for one component+platform. Every artifact this script writes is
# keyed by it, so an amd64 report can neither overwrite nor be mistaken for an arm64 one.
#
# The arch is ALWAYS present, even for single-platform components: a scheme where the arch
# appears only sometimes is one where a missing leg looks like an ordinary report. That is the
# property the maintainer asked for — "impossible for a missing arch to look like a passing
# one" — and it only holds if the naming is unconditional.
scan_label() {
    local component="$1" platform="$2"
    printf '%s-%s' "${component}" "${platform#linux/}"
}

# The ONE place that says what this script can scan (issue #681).
#
# `docs` used to be built and published by docker-build-push.sh while that
# script's registry-pull dispatch handled only backend and frontend, because the
# component list existed in three places that were free to disagree. Everything
# now derives from this table: scan_component's lookup, the `all` target, the
# `list-components` command that docker-build-push.sh validates against, and the
# usage text. Adding a component here is the whole change; forgetting to add one
# is now a loud failure rather than a silent green scan.
declare -A SCAN_COMPONENT_DOCKERFILE=(
    [backend]="backend/Dockerfile.prod"
    [lite]="backend/Dockerfile.lite"
    [frontend]="frontend/Dockerfile.prod"
    [docs]="docs-site/Dockerfile"
    [blackwell]="backend/Dockerfile.blackwell"
)
declare -A SCAN_COMPONENT_REPO=(
    [backend]="${REPO_BACKEND}"
    [lite]="${REPO_BACKEND_LITE}"
    [frontend]="${REPO_FRONTEND}"
    [docs]="${REPO_DOCS}"
    [blackwell]="${REPO_BACKEND}"
)

# Sorted, one per line — the machine-readable contract other scripts consume.
scan_components() {
    printf '%s\n' "${!SCAN_COMPONENT_DOCKERFILE[@]}" | sort
}

# Create output directory
mkdir -p "${OUTPUT_DIR}"

# Function to print colored output
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_header() {
    echo -e "${CYAN}========================================${NC}"
    echo -e "${CYAN}$1${NC}"
    echo -e "${CYAN}========================================${NC}"
}

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to install Trivy
install_trivy() {
    if command_exists trivy; then
        print_info "Trivy already installed: $(trivy --version | head -1)"
        return 0
    fi

    print_warning "Trivy not found. Installing..."

    if [[ "$OSTYPE" == "darwin"* ]]; then
        if command_exists brew; then
            brew install trivy
        else
            print_error "Homebrew not found. Please install Trivy manually: https://aquasecurity.github.io/trivy/latest/getting-started/installation/"
            return 1
        fi
    else
        curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin
    fi

    print_success "Trivy installed successfully"
}

# Function to install Grype
install_grype() {
    if command_exists grype; then
        print_info "Grype already installed: $(grype version | head -1)"
        return 0
    fi

    print_warning "Grype not found. Installing..."
    curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh -s -- -b /usr/local/bin
    print_success "Grype installed successfully"
}

# Function to install Syft
install_syft() {
    if command_exists syft; then
        print_info "Syft already installed: $(syft version | head -1)"
        return 0
    fi

    print_warning "Syft not found. Installing..."
    curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin
    print_success "Syft installed successfully"
}

# Function to install Hadolint
install_hadolint() {
    if command_exists hadolint; then
        print_info "Hadolint already installed: $(hadolint --version)"
        return 0
    fi

    print_warning "Hadolint not found. Installing..."

    if [[ "$OSTYPE" == "darwin"* ]]; then
        if command_exists brew; then
            brew install hadolint
        else
            print_error "Homebrew not found. Please install Hadolint manually: https://github.com/hadolint/hadolint"
            return 1
        fi
    else
        HADOLINT_VERSION=$(curl -s https://api.github.com/repos/hadolint/hadolint/releases/latest | grep '"tag_name":' | sed -E 's/.*"v([^"]+)".*/\1/')
        curl -sL -o /usr/local/bin/hadolint "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT_VERSION}/hadolint-Linux-x86_64"
        chmod +x /usr/local/bin/hadolint
    fi

    print_success "Hadolint installed successfully"
}

# Function to check and install Dockle
check_dockle() {
    if ! command_exists docker; then
        print_warning "Docker not found. Dockle requires Docker to run."
        return 1
    fi

    print_info "Dockle will run via Docker image (no installation needed)"
    return 0
}

# Function to lint Dockerfile with Hadolint
lint_dockerfile() {
    local dockerfile=$1
    local component=$2

    print_header "Linting Dockerfile: ${dockerfile}"

    local output_file="${OUTPUT_DIR}/${component}-hadolint.txt"

    if hadolint "${dockerfile}" | tee "${output_file}"; then
        print_success "Dockerfile passed Hadolint checks"
        return 0
    else
        print_warning "Dockerfile has linting issues (see ${output_file})"
        return 1
    fi
}

# Function to run Dockle on image with all CIS Docker Benchmark checks (optimized - single run)
run_dockle() {
    local image=$1
    local component=$2

    print_header "Running Dockle (CIS Docker Benchmark) on ${image}"

    local output_file="${OUTPUT_DIR}/${component}-dockle.json"
    local abs_output_dir
    abs_output_dir=$(cd "${OUTPUT_DIR}" && pwd)

    # Run Dockle ONCE with JSON output only (faster)
    local dockle_args=(
        --timeout 300s
        --exit-code 1
        --exit-level WARN
        --accept-file settings.py
        --accept-key KEY_SHA512
        --ignore CIS-DI-0005
        --ignore DKL-DI-0006
        --format json
        --output "/output/${component}-dockle.json"
    )

    if docker run --rm \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v "${abs_output_dir}:/output" \
        goodwithtech/dockle:latest \
        "${dockle_args[@]}" \
        "${image}"; then
        print_success "Dockle scan completed (see ${output_file})"
        return 0
    else
        print_warning "Dockle found issues (see ${output_file})"
        return 1
    fi
}

# Function to generate SBOM with Syft
generate_sbom() {
    local image=$1
    local component=$2

    print_header "Generating SBOM for ${image}"

    local sbom_file="${OUTPUT_DIR}/${component}-sbom.json"

    syft "${image}" -o cyclonedx-json > "${sbom_file}"
    print_success "SBOM generated: ${sbom_file}"

    # Also generate human-readable table format
    syft "${image}" -o table > "${OUTPUT_DIR}/${component}-sbom.txt"
    print_info "Human-readable SBOM: ${OUTPUT_DIR}/${component}-sbom.txt"

    echo "${sbom_file}"
}

# Function to scan vulnerabilities with Trivy (optimized - single scan, multiple outputs)
scan_trivy() {
    local image=$1
    local component=$2

    print_header "Scanning ${image} with Trivy"

    local json_output="${OUTPUT_DIR}/${component}-trivy.json"
    local txt_output="${OUTPUT_DIR}/${component}-trivy.txt"

    # Run Trivy scan ONCE with JSON output, then convert to table.
    #
    # --timeout: Trivy's own default (5m) is comfortably enough for frontend/docs but not
    # for the backend image (~13.8 GB, a large torch/CUDA dependency tree) -- measured
    # "context deadline exceeded" at 5m22s against davidamacey/opentranscribe-backend:v0.5.0,
    # and a from-cold re-run still hadn't finished at 23+ minutes. That failure is SILENT to
    # this function: the JSON write fails, the `trivy convert` fallback below then does its
    # own fresh (and slower, uncached) table-only scan of the same image, which happens to
    # fit under ITS OWN 5m default often enough that the overall scan_trivy() call looks like
    # it succeeded -- while json_output silently keeps whatever it held from the last time
    # the JSON step actually finished (a stale prior release's report, in the case that
    # surfaced this). 45m is deliberately generous: it costs nothing when a scan finishes in
    # seconds (frontend/docs), and a failed release-gate re-run from an insufficient ceiling
    # costs far more wall-clock than a longer wait on the one image that needs it.
    # --skip-dirs: yt-dlp bundles ~10 site extractors (adultswim, aenetworks,
    # blackboardcollaborate, cloudflarestream, espn, go, nbc, shahid, tbs, vice) that embed
    # public, site-issued API credentials/JWTs/example URLs as class constants and `_TESTS`
    # fixtures -- required for the extractor to authenticate exactly as that site's own
    # official web/mobile client does, not a leaked secret of ours. Confirmed by inspecting
    # several: JWT/API-key constants used as documented client credentials, plus
    # `'only_matching': True` test-fixture URLs that are asserted against a regex, never
    # fetched. This does not affect vulnerability detection for the yt-dlp package itself,
    # which Trivy resolves from installed-package metadata, not by walking this directory.
    trivy image \
        --severity "${SEVERITY_THRESHOLD},HIGH,CRITICAL" \
        --format json \
        --output "${json_output}" \
        --timeout 45m \
        --skip-dirs '**/yt_dlp/extractor' \
        --quiet \
        "${image}"

    # Generate table format from JSON (faster than re-scanning)
    trivy convert --format table --output "${txt_output}" "${json_output}" 2>/dev/null || \
        trivy image --severity "${SEVERITY_THRESHOLD},HIGH,CRITICAL" --format table --timeout 45m \
            --skip-dirs '**/yt_dlp/extractor' --output "${txt_output}" "${image}"

    print_success "Trivy reports generated:"
    print_info "  - JSON: ${json_output}"
    print_info "  - Text: ${txt_output}"

    # Check for CRITICAL vulnerabilities
    local critical_count
    local high_count
    critical_count=$(jq '[.Results[]?.Vulnerabilities[]? | select(.Severity == "CRITICAL")] | length' "${json_output}")
    high_count=$(jq '[.Results[]?.Vulnerabilities[]? | select(.Severity == "HIGH")] | length' "${json_output}")

    print_info "Found ${critical_count} CRITICAL and ${high_count} HIGH severity vulnerabilities"

    if [ "${FAIL_ON_CRITICAL}" = "true" ] && [ "${critical_count}" -gt 0 ]; then
        print_error "CRITICAL vulnerabilities found - scan failed"
        return 1
    fi

    return 0
}

# Function to scan vulnerabilities with Grype
scan_grype() {
    local image=$1
    local component=$2
    local sbom_file=$3

    print_header "Scanning with Grype"

    local json_output="${OUTPUT_DIR}/${component}-grype.json"
    local txt_output="${OUTPUT_DIR}/${component}-grype.txt"

    # Scan from SBOM for speed
    if [ -n "${sbom_file}" ] && [ -f "${sbom_file}" ]; then
        print_info "Scanning from SBOM for faster results..."
        grype "sbom:${sbom_file}" \
            --output json \
            --file "${json_output}"

        grype "sbom:${sbom_file}" \
            --output table \
            | tee "${txt_output}"
    else
        # Scan image directly
        grype "${image}" \
            --output json \
            --file "${json_output}"

        grype "${image}" \
            --output table \
            | tee "${txt_output}"
    fi

    print_success "Grype reports generated:"
    print_info "  - JSON: ${json_output}"
    print_info "  - Text: ${txt_output}"

    # Check for CRITICAL vulnerabilities
    local critical_count
    local high_count
    critical_count=$(jq '[.matches[]? | select(.vulnerability.severity == "Critical")] | length' "${json_output}")
    high_count=$(jq '[.matches[]? | select(.vulnerability.severity == "High")] | length' "${json_output}")

    print_info "Found ${critical_count} Critical and ${high_count} High severity vulnerabilities"

    if [ "${FAIL_ON_CRITICAL}" = "true" ] && [ "${critical_count}" -gt 0 ]; then
        print_error "CRITICAL vulnerabilities found - scan failed"
        return 1
    fi

    return 0
}

# Obtain the image for ONE platform and prove it is that platform. Echoes a local image ref
# on success; returns non-zero (caller maps to COULD NOT SCAN) on any failure.
#
# ⚠️ THE VERIFICATION IS THE POINT, not defensive padding. `docker pull --platform linux/arm64`
# against a tag that has no arm64 leg does NOT reliably fail — depending on daemon version and
# whether the reference is an index or a single manifest, it can succeed and hand back the
# amd64 image with at most a warning. Scanning that and filing the result as `-arm64` is worse
# than not scanning at all: it manufactures evidence for a leg nobody examined. This is the
# same trap as BuildKit's InvalidBaseImagePlatform warning (see docker-build-push.sh's
# build_platforms) — the exit code does not tell you what you got, so read the artifact.
#
# A local image already carrying the tag is reused ONLY if its architecture matches; otherwise
# it is a leftover from another leg's pull and must not be scanned as this one.
resolve_platform_image() {
    local repo="$1" tag="$2" platform="$3"
    # Only the arch is needed as a separate token (for the re-tag below); the os/arch pair is
    # compared as a whole string against what the daemon reports.
    local want_arch="${platform#*/}"
    local ref="${repo}:${tag}"

    _actual() { docker image inspect "$1" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null; }

    # Look for an ALREADY-LOCAL image of this repo+version that is the right architecture,
    # across the bare tag AND any leg tag derived from it (repo:vX.Y.Z-<cap>-<arch>, what
    # BUILD_MODE=local writes per leg).
    #
    # Checking only the bare `repo:${tag}` would be ORDER-DEPENDENT: building several legs
    # locally leaves that tag pointing at whichever leg was built last, so the other leg's scan
    # would see a mismatch and try to pull a version that is not published yet. Which leg got
    # scanned locally and which got refused would then depend on table iteration order — a
    # coin-flip deciding what is covered is not coverage.
    local candidate actual=""
    for candidate in "${ref}" $(docker images --format '{{.Repository}}:{{.Tag}}' "${repo}" 2>/dev/null | grep -F "${repo}:${tag}-" || true); do
        if [ "$(_actual "${candidate}")" = "${platform}" ]; then
            ref="${candidate}"
            actual="${platform}"
            break
        fi
    done

    if [ -z "${actual}" ]; then
        ref="${repo}:${tag}"

        # Ask the REGISTRY what the tag publishes before downloading anything.
        #
        # Measured: `docker pull --platform linux/s390x davidamacey/opentranscribe-backend:latest`
        # against an index that publishes only amd64+arm64 does not fail fast — it proceeds to
        # download a multi-GB image of a DIFFERENT architecture, and the mismatch is only
        # detectable afterwards. The post-pull check below still catches it (it is the
        # authoritative one, and stays), but without this pre-flight the refusal costs a full
        # image download every time, which in a scan loop over legs is minutes to hours.
        #
        # `docker manifest inspect` failing is NOT treated as absence: a single-arch manifest,
        # a local-only image, or an unauthenticated registry all fail here, and any of those may
        # still be scannable. Only an index that positively lists its platforms and omits this
        # one is a refusal — otherwise fall through to pull-and-verify.
        # ⚠️ NO escaped double quotes inside this single-quoted Python. Bash does not process
        # backslash escapes inside '...', so `\"` reaches Python literally and the whole snippet
        # dies with SyntaxError — which `2>/dev/null || true` then swallows, leaving `declared`
        # empty and the pre-flight a silent no-op that always falls through to the pull. That is
        # exactly how the first version of this behaved: it looked correct and checked nothing.
        local declared
        declared="$(docker manifest inspect "${ref}" 2>/dev/null \
            | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for m in doc.get("manifests", []) or []:
    p = m.get("platform") or {}
    arch = p.get("architecture")
    if arch and arch != "unknown":
        print(str(p.get("os")) + "/" + str(arch))
' 2>/dev/null || true)"

        if [ -n "${declared}" ] && ! printf '%s\n' "${declared}" | grep -Fqx -- "${platform}"; then
            print_error "${ref} publishes no ${platform} leg — refusing to pull a different one" >&2
            print_error "  declared: $(printf '%s ' ${declared})" >&2
            return 1
        fi

        print_info "No local ${platform} image for ${repo}:${tag} — pulling..." >&2
        if ! docker pull --platform "${platform}" "${ref}" >&2; then
            print_error "Could not pull ${ref} for ${platform}" >&2
            return 1
        fi
        actual="$(_actual "${ref}")"
    fi

    if [ -z "${actual}" ]; then
        print_error "No local image for ${ref} after pull — cannot scan ${platform}" >&2
        return 1
    fi
    if [ "${actual}" != "${platform}" ]; then
        # The registry served a different architecture than the one requested. Refuse: the
        # alternative is a report labelled with an arch it does not describe.
        print_error "Requested ${platform} for ${ref} but got ${actual} — refusing to scan" >&2
        print_error "  The tag has no ${platform} leg, or the daemon silently substituted one." >&2
        return 1
    fi

    # Re-tag per architecture so two legs of the same version can coexist locally. Without
    # this the second pull overwrites the first and both scans examine the same image.
    local scan_ref="${repo}:${tag}-scanleg-${want_arch}"
    docker tag "${ref}" "${scan_ref}" >&2 || return 1
    printf '%s' "${scan_ref}"
}

# Scan every platform a component declares, aggregating with the 0 < 1 < 2 precedence.
#
# Fails CLOSED in both directions that matter: an empty/underivable platform list is COULD NOT
# SCAN (not "nothing to do"), and any single leg that could not be scanned makes the whole
# component COULD NOT SCAN regardless of how the other legs went. A component is only as
# scanned as its least-scanned leg.
scan_component_all_platforms() {
    local component="$1"

    local platforms=()
    mapfile -t platforms < <(component_platforms "${component}")
    if [ ${#platforms[@]} -eq 0 ]; then
        print_error "No platforms declared for '${component}' (via ${PLATFORM_SOURCE} list-platforms)"
        print_error "  This is COULD NOT SCAN, not 'no architectures to scan' — refusing to report a pass"
        return "${EXIT_COULD_NOT_SCAN}"
    fi

    print_info "${component}: scanning ${#platforms[@]} platform(s): ${platforms[*]}"

    local rc=0 platform leg_rc
    for platform in "${platforms[@]}"; do
        leg_rc=0
        scan_component "${component}" "${platform}" || leg_rc=$?
        if [ "${leg_rc}" -ge "${EXIT_COULD_NOT_SCAN}" ]; then
            rc="${EXIT_COULD_NOT_SCAN}"
        elif [ "${leg_rc}" -ne 0 ] && [ "${rc}" -eq 0 ]; then
            rc="${EXIT_FINDINGS}"
        fi
    done
    return "${rc}"
}

# The ONE entry point every dispatch arm uses, so `all` and a single-component run cannot
# differ in how much they actually scan. SCAN_PLATFORM narrows to one leg; unset means every
# declared leg, which is the default because "scan the component" has to mean "scan all of it".
scan_target_component() {
    local component="$1"
    if [ -n "${SCAN_PLATFORM}" ]; then
        scan_component "${component}" "${SCAN_PLATFORM}"
    else
        scan_component_all_platforms "${component}"
    fi
}

# Function to scan a component (backend or frontend) - PARALLEL EXECUTION
#
# $2 is the platform to scan. When given, the image is obtained and VERIFIED for that
# architecture and every report is named <component>-<arch>-<tool>.
scan_component() {
    local component=$1
    local platform="${2:-}"
    local dockerfile=""
    local image=""
    # Tag to scan. Defaults to :latest for dev/post-push workflows.
    # Set IMAGE_TAG=0.4.0 (or any other tag) to scan a release candidate
    # before pushing it — e.g. the Phase 1 pre-release security scan in
    # .claude/commands/release.md should pass IMAGE_TAG=X.Y.Z.
    local tag="${IMAGE_TAG:-latest}"

    dockerfile="${SCAN_COMPONENT_DOCKERFILE[${component}]:-}"
    local repo="${SCAN_COMPONENT_REPO[${component}]:-}"
    if [ -z "${dockerfile}" ] || [ -z "${repo}" ]; then
        print_error "Invalid component: ${component}"
        print_error "Known components: $(scan_components | tr '\n' ' ')"
        # NOT ${EXIT_FINDINGS}: nothing was examined, so there is no finding to
        # tolerate. See the exit-code block at the top of this file.
        return "${EXIT_COULD_NOT_SCAN}"
    fi
    # Report-filename stem. Arch-qualified whenever a platform was named, so the legs of a
    # multi-arch tag cannot overwrite one another or be confused for one another.
    local label="${component}"
    [ -n "${platform}" ] && label="$(scan_label "${component}" "${platform}")"

    if [ -n "${platform}" ]; then
        # Obtain-and-verify. Any failure here is COULD NOT SCAN: we have no image, or we have
        # one that is not the architecture we were asked about.
        if ! image="$(resolve_platform_image "${repo}" "${tag}" "${platform}")"; then
            print_error "Cannot scan ${component} on ${platform} — NOT SCANNED, not a pass"
            return "${EXIT_COULD_NOT_SCAN}"
        fi
    else
        image="${repo}:${tag}"
        # Check if image exists locally
        if ! docker image inspect "${image}" >/dev/null 2>&1; then
            print_warning "Image not found locally: ${image}"
            print_info "Attempting to pull from registry..."
            if ! docker pull "${image}"; then
                print_error "Failed to pull image. Please build it first."
                # There is no image, so there is nothing to have findings about.
                return "${EXIT_COULD_NOT_SCAN}"
            fi
        fi
    fi

    print_header "Security Scanning: ${label}"
    print_info "Image: ${image}"
    [ -n "${platform}" ] && print_info "Platform: ${platform} (verified)"
    print_info "Dockerfile: ${dockerfile}"
    print_info "Reports: ${OUTPUT_DIR}/${label}-*"
    print_info "Running tools in PARALLEL for speed..."
    echo ""

    # Create status directory for parallel job tracking
    local status_dir
    status_dir=$(mktemp -d)

    # === PARALLEL PHASE 1: Hadolint + Dockle + SBOM ===
    # These have no dependencies on each other

    # NOTE on `|| rc=$?` in every sub-scan below — this is load-bearing, not style.
    #
    # This script runs under `set -e` (line 2). Written as
    #
    #     ( scan_trivy ...; echo $? > .../trivy.status ) &
    #
    # a scanner that returns non-zero — i.e. FOUND SOMETHING — kills the
    # subshell on that very line, so the status file is never written. The
    # collector then saw no file and scored the tool as a pass. The result was a
    # gate that passed *because* the scan failed: 39 CRITICAL CVEs reported
    # "All security scans passed!". Keeping the call in a `||` list exempts it
    # from `set -e`, so the status is always recorded.

    # Hadolint (fast - Dockerfile only)
    if [ -f "${dockerfile}" ]; then
        (
            rc=0; lint_dockerfile "${dockerfile}" "${label}" || rc=$?
            echo "${rc}" > "${status_dir}/hadolint.status"
        ) &
    else
        echo "0" > "${status_dir}/hadolint.status"
    fi

    # Dockle (medium speed)
    (
        rc=0; { check_dockle && run_dockle "${image}" "${label}"; } || rc=$?
        echo "${rc}" > "${status_dir}/dockle.status"
    ) &

    # SBOM generation (needed for Grype, but can start now)
    (
        rc=0; generate_sbom "${image}" "${label}" > "${status_dir}/sbom_path.txt" || rc=$?
        echo "${rc}" > "${status_dir}/sbom.status"
    ) &

    # Wait for Phase 1 to complete
    wait
    print_info "Phase 1 complete (Hadolint, Dockle, SBOM)"

    # Get SBOM path for Grype
    local sbom_file=""
    if [ -f "${status_dir}/sbom_path.txt" ]; then
        sbom_file=$(cat "${status_dir}/sbom_path.txt")
    fi

    # === PARALLEL PHASE 2: Trivy + Grype ===
    # Both vulnerability scanners run in parallel

    print_info "Phase 2: Running Trivy and Grype in parallel..."

    # Trivy scan
    (
        rc=0; scan_trivy "${image}" "${label}" || rc=$?
        echo "${rc}" > "${status_dir}/trivy.status"
    ) &

    # Grype scan (uses SBOM for speed)
    (
        rc=0; scan_grype "${image}" "${label}" "${sbom_file}" || rc=$?
        echo "${rc}" > "${status_dir}/grype.status"
    ) &

    # Wait for Phase 2 to complete
    wait
    print_info "Phase 2 complete (Trivy, Grype)"

    # Collect results.
    #
    # A MISSING status file is a FAILURE, not a pass. It means the sub-scan died
    # before it could record anything — killed, crashed, or exited early — and
    # "we have no idea what that scanner found" must never read as "clean". This
    # previously fell through the `if [ -f ... ]` and scored as success, which is
    # what let a dead scanner silently pass the gate.
    #
    # It is also not the same thing as a finding, so it returns
    # EXIT_COULD_NOT_SCAN rather than EXIT_FINDINGS: a caller running with
    # FAIL_ON_SECURITY_ISSUES=false is saying "known CVEs are acceptable to me
    # today", not "a scanner that never ran is acceptable to me today".
    local exit_code=0
    for tool in hadolint dockle sbom trivy grype; do
        if [ -f "${status_dir}/${tool}.status" ]; then
            local status
            status=$(cat "${status_dir}/${tool}.status")
            if [ "$status" != "0" ]; then
                print_warning "${label}: ${tool} reported status ${status}"
                if [ "${exit_code}" -eq 0 ]; then
                    exit_code="${EXIT_FINDINGS}"
                fi
            fi
        else
            print_error "${label}: ${tool} produced no status — NOT SCANNED, not a pass"
            exit_code="${EXIT_COULD_NOT_SCAN}"
        fi
    done

    # Cleanup
    rm -rf "${status_dir}"

    echo ""
    if [ "${exit_code}" -eq 0 ]; then
        print_success "Security scan completed for ${label}"
    elif [ "${exit_code}" -ge "${EXIT_COULD_NOT_SCAN}" ]; then
        print_error "Security scan could NOT be completed for ${label}"
    else
        print_warning "Security scan completed with issues for ${label}"
    fi

    return "${exit_code}"
}

# Function to generate summary report
generate_summary() {
    print_header "Security Scan Summary"

    print_info "All reports saved to: ${OUTPUT_DIR}"
    echo ""

    print_info "Report files:"
    find "${OUTPUT_DIR}" -maxdepth 1 -type f -exec ls -lh {} \; | awk '{printf "  %-40s %8s\n", $9, $5}'
    echo ""

    # Reports are keyed <component>-<arch>-<tool>, so this cannot name a fixed file any more —
    # it used to look for `backend-trivy.json`, a filename nothing writes since #667.
    if compgen -G "${OUTPUT_DIR}/*-trivy.json" > /dev/null; then
        print_info "To view detailed reports:"
        for file in "${OUTPUT_DIR}"/*.json; do
            [ -f "$file" ] && print_info "  - $(basename "${file}")"
        done
    fi
}

# Function to show usage
show_usage() {
    cat << EOF
Usage: $0 [OPTION]

Security scanning for OpenTranscribe Docker images using free, open-source tools

Tools used:
  - Hadolint: Dockerfile linter
  - Dockle: Container image CIS best practices checker
  - Syft: SBOM (Software Bill of Materials) generator
  - Trivy: Comprehensive vulnerability scanner
  - Grype: Fast vulnerability scanner

Options:
    backend          Scan only backend image
    frontend         Scan only frontend image
    docs             Scan only docs image
    all              Scan all images (default)
    list-components  Print the scannable component names, one per line
    install          Install all required tools
    help             Show this help message

Exit codes:
    0   scanned; nothing blocking
    1   SCANNED, and the scan found something
    2   COULD NOT SCAN (unknown component, image unobtainable, or a sub-scan
        that died without recording a verdict)

    1 and 2 are deliberately distinct. A caller may choose to tolerate findings;
    no caller may tolerate never having looked. Do not collapse them.

Environment Variables:
    OUTPUT_DIR              Report output directory (default: ./security-reports)
    SEVERITY_THRESHOLD      Minimum severity to report (default: MEDIUM)
    FAIL_ON_CRITICAL        Fail if CRITICAL vulnerabilities found (default: true)
    DOCKERHUB_USERNAME      Docker Hub username (default: davidamacey)
    IMAGE_TAG               Image tag to scan (default: latest). Set to a
                            release-candidate tag like 0.4.0 to scan a
                            built-but-not-yet-pushed image.

Examples:
    $0                      # Scan both images (uses :latest)
    $0 backend              # Scan only backend
    $0 install              # Install all required tools

    # Customize scanning
    OUTPUT_DIR=./reports SEVERITY_THRESHOLD=HIGH $0 all

    # Scan a release-candidate tag before pushing (required by Phase 1
    # of the .claude/commands/release.md checklist):
    IMAGE_TAG=0.4.0 $0 all
    FAIL_ON_CRITICAL=false $0 backend

Reports:
    All reports are saved to \${OUTPUT_DIR}/ with multiple formats:
    - *-hadolint.txt: Dockerfile linting results
    - *-dockle.json: CIS best practices check
    - *-sbom.json: Software Bill of Materials (CycloneDX format)
    - *-trivy.json: Trivy vulnerability scan (JSON)
    - *-trivy.txt: Trivy vulnerability scan (human-readable)
    - *-grype.json: Grype vulnerability scan (JSON)
    - *-grype.txt: Grype vulnerability scan (human-readable)

EOF
}

# Function to install all tools
install_all_tools() {
    print_header "Installing Security Scanning Tools"

    install_trivy
    install_grype
    install_syft
    install_hadolint
    check_dockle

    print_success "All tools installed successfully!"
    echo ""
    print_info "Tool versions:"
    command_exists trivy && trivy --version | head -1
    command_exists grype && grype version | head -1
    command_exists syft && syft version | head -1
    command_exists hadolint && hadolint --version
    print_info "Dockle: runs via Docker image"
}

# Main function
main() {
    # Answer the machine-readable query BEFORE any banner output. This is a
    # contract docker-build-push.sh parses to derive its component list, and a
    # decorative header on stdout corrupts it — which is not a hypothetical: the
    # first version of this handled list-components as an ordinary case arm and
    # returned the banner plus the list.
    if [ "${SCAN_TARGET}" = "list-components" ]; then
        scan_components
        exit 0
    fi

    # component<TAB>repo, one per line — the machine-readable contract release
    # stages (50-scan.sh) derive their component->repo mapping from, so that
    # mapping has exactly one home instead of being re-hardcoded per caller.
    if [ "${SCAN_TARGET}" = "list-repos" ]; then
        local component
        for component in $(scan_components); do
            printf '%s\t%s\n' "${component}" "${SCAN_COMPONENT_REPO[${component}]}"
        done
        exit 0
    fi

    print_header "OpenTranscribe Security Scanner"
    print_info "Output directory: ${OUTPUT_DIR}"
    print_info "Severity threshold: ${SEVERITY_THRESHOLD}"
    print_info "Fail on critical: ${FAIL_ON_CRITICAL}"
    echo ""

    case "${SCAN_TARGET}" in
        install)
            install_all_tools
            exit 0
            ;;
        help|--help|-h)
            show_usage
            exit 0
            ;;
        all)
            # Check required tools
            install_trivy
            install_grype
            install_syft
            install_hadolint
            check_dockle

            # Run the component scans in parallel so total wall time is roughly
            # max(components) instead of the sum. Each sub-scan logs
            # independently to the same OUTPUT_DIR with unique per-component
            # filenames so there is no contention.
            local all_components=()
            mapfile -t all_components < <(scan_components)
            print_info "Running ${all_components[*]} scans in parallel..."

            local comp pids=() names=()
            for comp in "${all_components[@]}"; do
                ( scan_target_component "${comp}" > "${OUTPUT_DIR}/.scan-${comp}.log" 2>&1 ) &
                pids+=($!)
                names+=("${comp}")
            done

            # `wait` must be in a `||` list: this script runs under `set -e`, so
            # a bare `wait "$pid"` on a scan that found something aborts main on
            # that line — skipping the log replay and the summary below.
            local idx rc
            exit_code=0
            for idx in "${!pids[@]}"; do
                rc=0
                wait "${pids[$idx]}" || rc=$?
                if [ "${rc}" -ge "${EXIT_COULD_NOT_SCAN}" ]; then
                    exit_code="${EXIT_COULD_NOT_SCAN}"
                elif [ "${rc}" -ne 0 ] && [ "${exit_code}" -eq 0 ]; then
                    exit_code="${EXIT_FINDINGS}"
                fi
            done

            # Replay each sub-scan's log in order so user sees normal output.
            for idx in "${!names[@]}"; do
                print_info "=== ${names[$idx]} scan output ==="
                cat "${OUTPUT_DIR}/.scan-${names[$idx]}.log"
                rm -f "${OUTPUT_DIR}/.scan-${names[$idx]}.log"
            done
            ;;
        *)
            if ! scan_components | grep -Fqx -- "${SCAN_TARGET}"; then
                print_error "Invalid option: ${SCAN_TARGET}"
                show_usage
                # Exit 2, not 1: a caller must be able to tell "this component
                # was scanned and had findings" from "this component cannot be
                # scanned at all". See the exit-code block at the top.
                exit "${EXIT_COULD_NOT_SCAN}"
            fi

            # Check required tools
            install_trivy
            install_grype
            install_syft
            install_hadolint
            check_dockle

            exit_code=0
            scan_target_component "${SCAN_TARGET}" || exit_code=$?
            ;;
    esac

    echo ""
    generate_summary

    if [ "${exit_code}" -eq 0 ]; then
        print_success "All security scans passed!"
        exit 0
    elif [ "${exit_code}" -ge "${EXIT_COULD_NOT_SCAN}" ]; then
        print_error "Security scans could NOT RUN — nothing here establishes these images are clean"
        exit "${EXIT_COULD_NOT_SCAN}"
    else
        print_error "Security scans failed"
        exit "${EXIT_FINDINGS}"
    fi
}

# Run main function — but only when EXECUTED, not when sourced.
#
# Sourcing is how scripts/tests/test-scan-not-a-pass.sh exercises the `all`
# aggregation (the path the release `scan` stage runs) with stubbed scanners:
# doing it for real needs the three published multi-gigabyte images.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main
fi
