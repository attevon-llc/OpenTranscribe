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
REPO_FRONTEND="${DOCKERHUB_USERNAME}/opentranscribe-frontend"
REPO_DOCS="${DOCKERHUB_USERNAME}/opentranscribe-docs"
SCAN_TARGET="${1:-all}"
OUTPUT_DIR="${OUTPUT_DIR:-./security-reports}"
SEVERITY_THRESHOLD="${SEVERITY_THRESHOLD:-MEDIUM}"
FAIL_ON_CRITICAL="${FAIL_ON_CRITICAL:-true}"

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

# Function to scan a component (backend or frontend) - PARALLEL EXECUTION
scan_component() {
    local component=$1
    local dockerfile=""
    local image=""
    # Tag to scan. Defaults to :latest for dev/post-push workflows.
    # Set IMAGE_TAG=0.4.0 (or any other tag) to scan a release candidate
    # before pushing it — e.g. the Phase 1 pre-release security scan in
    # .claude/commands/release.md should pass IMAGE_TAG=X.Y.Z.
    local tag="${IMAGE_TAG:-latest}"

    case "${component}" in
        backend)
            dockerfile="backend/Dockerfile.prod"
            image="${REPO_BACKEND}:${tag}"
            ;;
        frontend)
            dockerfile="frontend/Dockerfile.prod"
            image="${REPO_FRONTEND}:${tag}"
            ;;
        docs)
            dockerfile="docs-site/Dockerfile"
            image="${REPO_DOCS}:${tag}"
            ;;
        *)
            print_error "Invalid component: ${component}"
            return 1
            ;;
    esac

    print_header "Security Scanning: ${component}"
    print_info "Image: ${image}"
    print_info "Dockerfile: ${dockerfile}"
    print_info "Running tools in PARALLEL for speed..."
    echo ""

    # Check if image exists locally
    if ! docker image inspect "${image}" >/dev/null 2>&1; then
        print_warning "Image not found locally: ${image}"
        print_info "Attempting to pull from registry..."
        if ! docker pull "${image}"; then
            print_error "Failed to pull image. Please build it first."
            return 1
        fi
    fi

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
            rc=0; lint_dockerfile "${dockerfile}" "${component}" || rc=$?
            echo "${rc}" > "${status_dir}/hadolint.status"
        ) &
    else
        echo "0" > "${status_dir}/hadolint.status"
    fi

    # Dockle (medium speed)
    (
        rc=0; { check_dockle && run_dockle "${image}" "${component}"; } || rc=$?
        echo "${rc}" > "${status_dir}/dockle.status"
    ) &

    # SBOM generation (needed for Grype, but can start now)
    (
        rc=0; generate_sbom "${image}" "${component}" > "${status_dir}/sbom_path.txt" || rc=$?
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
        rc=0; scan_trivy "${image}" "${component}" || rc=$?
        echo "${rc}" > "${status_dir}/trivy.status"
    ) &

    # Grype scan (uses SBOM for speed)
    (
        rc=0; scan_grype "${image}" "${component}" "${sbom_file}" || rc=$?
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
    local exit_code=0
    for tool in hadolint dockle sbom trivy grype; do
        if [ -f "${status_dir}/${tool}.status" ]; then
            local status
            status=$(cat "${status_dir}/${tool}.status")
            if [ "$status" != "0" ]; then
                print_warning "${component}: ${tool} reported status ${status}"
                exit_code=1
            fi
        else
            print_error "${component}: ${tool} produced no status — treating as FAILED"
            exit_code=1
        fi
    done

    # Cleanup
    rm -rf "${status_dir}"

    echo ""
    if [ ${exit_code} -eq 0 ]; then
        print_success "Security scan completed for ${component}"
    else
        print_warning "Security scan completed with issues for ${component}"
    fi

    return ${exit_code}
}

# Function to generate summary report
generate_summary() {
    print_header "Security Scan Summary"

    print_info "All reports saved to: ${OUTPUT_DIR}"
    echo ""

    print_info "Report files:"
    find "${OUTPUT_DIR}" -maxdepth 1 -type f -exec ls -lh {} \; | awk '{printf "  %-40s %8s\n", $9, $5}'
    echo ""

    # Generate HTML summary if reports exist
    if [ -f "${OUTPUT_DIR}/backend-trivy.json" ] || [ -f "${OUTPUT_DIR}/frontend-trivy.json" ]; then
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
    backend     Scan only backend image
    frontend    Scan only frontend image
    docs        Scan only docs image
    all         Scan all images (default)
    install     Install all required tools
    help        Show this help message

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
        backend|frontend|docs)
            # Check required tools
            install_trivy
            install_grype
            install_syft
            install_hadolint
            check_dockle

            scan_component "${SCAN_TARGET}"
            exit_code=$?
            ;;
        all)
            # Check required tools
            install_trivy
            install_grype
            install_syft
            install_hadolint
            check_dockle

            # Run the three component scans in parallel so total wall time
            # is roughly max(backend, frontend, docs) instead of the sum.
            # Each sub-scan logs independently to the same OUTPUT_DIR with
            # unique per-component filenames so there is no contention.
            print_info "Running backend, frontend, and docs scans in parallel..."
            local backend_log="${OUTPUT_DIR}/.scan-backend.log"
            local frontend_log="${OUTPUT_DIR}/.scan-frontend.log"
            local docs_log="${OUTPUT_DIR}/.scan-docs.log"

            ( scan_component "backend" > "${backend_log}" 2>&1 ) &
            local backend_pid=$!
            ( scan_component "frontend" > "${frontend_log}" 2>&1 ) &
            local frontend_pid=$!
            ( scan_component "docs" > "${docs_log}" 2>&1 ) &
            local docs_pid=$!

            wait "${backend_pid}"
            backend_exit=$?
            wait "${frontend_pid}"
            frontend_exit=$?
            wait "${docs_pid}"
            docs_exit=$?

            # Replay each sub-scan's log in order so user sees normal output
            print_info "=== backend scan output ==="
            cat "${backend_log}"
            print_info "=== frontend scan output ==="
            cat "${frontend_log}"
            print_info "=== docs scan output ==="
            cat "${docs_log}"
            rm -f "${backend_log}" "${frontend_log}" "${docs_log}"

            exit_code=$((backend_exit + frontend_exit + docs_exit))
            ;;
        *)
            print_error "Invalid option: ${SCAN_TARGET}"
            show_usage
            exit 1
            ;;
    esac

    echo ""
    generate_summary

    if [ ${exit_code} -eq 0 ]; then
        print_success "All security scans passed!"
        exit 0
    else
        print_error "Security scans failed"
        exit 1
    fi
}

# Run main function
main
