#!/bin/bash
set -e

# Docker Build and Push Script for OpenTranscribe
# Quick fix for pushing Docker images to Docker Hub locally

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
DOCKERHUB_USERNAME="${DOCKERHUB_USERNAME:-davidamacey}"
REPO_BACKEND="${DOCKERHUB_USERNAME}/opentranscribe-backend"
REPO_FRONTEND="${DOCKERHUB_USERNAME}/opentranscribe-frontend"
REPO_DOCS="${DOCKERHUB_USERNAME}/opentranscribe-docs"

# Get commit SHA for tagging
COMMIT_SHA=$(git rev-parse --short HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)

# Default to building both platforms
PLATFORMS="linux/amd64,linux/arm64"
BUILD_TARGET="${1:-all}"

# Remote builder configuration
# Set USE_REMOTE_BUILDER=true to use remote ARM64 builder (much faster!)
# Set REMOTE_BUILDER_NAME to override the builder name
USE_REMOTE_BUILDER="${USE_REMOTE_BUILDER:-false}"
REMOTE_BUILDER_NAME="${REMOTE_BUILDER_NAME:-opentranscribe-multiarch}"
DEFAULT_BUILDER_NAME="opentranscribe-builder"

# BUILD_MODE=local  — build for the host arch only, --load into the local daemon,
#                     push NOTHING. This is what the release flow uses to produce
#                     images it can scan and run the release scenarios against
#                     BEFORE anything reaches Docker Hub.
# BUILD_MODE=push   — the historical behaviour: multi-arch, --push (default, so
#                     existing callers are unaffected).
#
# Until this existed, EVERY path through this script ended in `buildx --push`:
# there was no way to build a release candidate without publishing it, which
# meant :latest — what every existing user pulls — moved before any release test
# had run against it.
#
# --load cannot export a multi-arch manifest, so local mode is single-arch by
# necessity; the other arch is validated after publish by the arm64 smoke step.
BUILD_MODE="${BUILD_MODE:-push}"

# Publish the moving :latest tag alongside :vX.Y.Z. The release flow sets this to
# false and moves :latest afterwards with `buildx imagetools create`, which copies
# the manifest by digest — so :latest and :vX.Y.Z are provably the same bytes
# rather than two independent builds that happen to share a source tree.
PUSH_LATEST="${PUSH_LATEST:-true}"

# Print the buildx invocations and exit without building.
DRY_RUN="${DRY_RUN:-false}"

# Build identity baked into the images. The backend build context is ./backend,
# so the repo-root VERSION file is NOT in the image — these args are the only
# source a prod container has for its own version. See backend/app/core/version.py.
GIT_SHA_FULL="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

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

# Function to check if Docker is running
check_docker() {
    if ! docker info > /dev/null 2>&1; then
        print_error "Docker is not running. Please start Docker and try again."
        exit 1
    fi
}

# Function to check if logged into Docker Hub
check_docker_login() {
    if ! docker info | grep -q "Username"; then
        print_warning "Not logged into Docker Hub. Attempting login..."
        docker login
    else
        print_success "Already logged into Docker Hub"
    fi
}

# Function to detect changes since last commit
detect_changes() {
    local component=$1

    if [ -z "$(git status --porcelain "${component}/")" ]; then
        print_info "No uncommitted changes in ${component}"
        # Check last commit
        if git diff --name-only HEAD~1 HEAD | grep -q "^${component}/"; then
            print_info "Changes detected in last commit for ${component}"
            return 0
        else
            print_warning "No recent changes in ${component}"
            return 1
        fi
    else
        print_info "Uncommitted changes detected in ${component}"
        return 0
    fi
}

# Function to update security scanning tool databases
update_security_tools() {
    print_info "Updating security scanning tool databases..."

    # Update Trivy vulnerability database
    if command -v trivy &> /dev/null; then
        print_info "Updating Trivy vulnerability database..."
        trivy image --download-db-only --quiet 2>/dev/null || true
        trivy image --download-java-db-only --quiet 2>/dev/null || true
        print_success "Trivy database updated"
    else
        print_warning "Trivy not found, skipping database update"
    fi

    # Update Grype vulnerability database
    if command -v grype &> /dev/null; then
        print_info "Updating Grype vulnerability database..."
        grype db update --quiet 2>/dev/null || true
        print_success "Grype database updated"
    else
        print_warning "Grype not found, skipping database update"
    fi

    # Update Syft (no database, but check for updates)
    if command -v syft &> /dev/null; then
        print_info "Syft is available (no database update needed)"
    else
        print_warning "Syft not found"
    fi

    # Hadolint and Dockle are rule-based, no database updates needed
    print_info "Hadolint and Dockle are rule-based (no database updates needed)"

    print_success "Security tool updates complete!"
}

# Function to run security scan if enabled
run_security_scan() {
    local component=$1

    if [ "${SKIP_SECURITY_SCAN}" = "true" ]; then
        print_warning "Security scanning skipped (SKIP_SECURITY_SCAN=true)"
        return 0
    fi

    if [ ! -f "./scripts/security-scan.sh" ]; then
        print_warning "Security scan script not found, skipping..."
        return 0
    fi

    print_info "Running security scan for ${component}..."
    if OUTPUT_DIR="./security-reports" FAIL_ON_CRITICAL="${FAIL_ON_CRITICAL:-false}" ./scripts/security-scan.sh "${component}"; then
        print_success "Security scan passed for ${component}"
        return 0
    else
        print_warning "Security scan found issues for ${component}"
        if [ "${FAIL_ON_SECURITY_ISSUES}" = "true" ]; then
            print_error "Failing build due to security issues (FAIL_ON_SECURITY_ISSUES=true)"
            return 1
        else
            print_warning "Continuing despite security issues (set FAIL_ON_SECURITY_ISSUES=true to fail)"
            return 0
        fi
    fi
}

# ---------------------------------------------------------------------------
# Build-mode helpers. One place decides platform / output / tag set, so the four
# build functions cannot drift apart on it.
# ---------------------------------------------------------------------------

# Platforms for this run. Local mode is host-arch only: `--load` cannot export a
# multi-arch manifest into the local image store.
build_platforms() {
    if [ "${BUILD_MODE}" = "local" ]; then
        docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null || echo "linux/amd64"
    else
        echo "${PLATFORMS}"
    fi
}

# --load (keep it here) vs --push (send it to Docker Hub).
build_output_flag() {
    if [ "${BUILD_MODE}" = "local" ]; then
        echo "--load"
    else
        echo "--push"
    fi
}

# Tag arguments for a repo: always :vX.Y.Z, plus :latest unless the caller is
# going to move :latest by digest afterwards.
build_tag_args() {
    local repo="$1"
    local args=("--tag" "${repo}:${VERSION_FULL}")
    if [ "${PUSH_LATEST}" = "true" ]; then
        args+=("--tag" "${repo}:latest")
    fi
    printf '%s\n' "${args[@]}"
}

# OCI provenance labels. Safe on every image — labels need no ARG declaration,
# unlike build args, which warn when the Dockerfile does not declare them.
# These are what makes `docker inspect` able to answer "what is this image?";
# nothing set them before, which is why opentranscribe.sh's label-based version
# fallback always came back empty.
build_identity_labels() {
    printf '%s\n' \
        "--label" "org.opencontainers.image.version=${VERSION_FULL}" \
        "--label" "org.opencontainers.image.revision=${GIT_SHA_FULL}" \
        "--label" "org.opencontainers.image.created=${BUILD_TIME}"
}

# Build args for the BACKEND only. backend/Dockerfile.prod declares all three;
# they are the sole source of build identity inside the image, because the build
# context is ./backend and the repo-root VERSION file is therefore absent.
#
# Deliberately NOT applied to the frontend: frontend/Dockerfile.prod declares no
# ARGs at all, so the --build-arg APP_VERSION this script used to pass it was a
# silent no-op. The frontend takes its version from frontend/package.json at
# build time via vite's __APP_VERSION__ define.
build_backend_identity_args() {
    printf '%s\n' \
        "--build-arg" "APP_VERSION=${VERSION_FULL}" \
        "--build-arg" "GIT_SHA=${COMMIT_SHA}" \
        "--build-arg" "BUILD_TIME=${BUILD_TIME}"
}

# Announce what a build is about to do, and short-circuit under DRY_RUN.
# Returns 1 when the caller should skip the actual build.
build_announce() {
    local what="$1"
    print_info "${what}: mode=${BUILD_MODE} platforms=$(build_platforms) version=${VERSION_FULL}"
    if [ "${BUILD_MODE}" = "local" ]; then
        print_info "  local mode — loading into the local daemon, pushing NOTHING"
    fi
    if [ "${DRY_RUN}" = "true" ]; then
        print_warning "  DRY_RUN=true — not building"
        return 1
    fi
    return 0
}

# Function to build and push Blackwell backend image (ARM64 only)
# Uses Dockerfile.blackwell with SM_121 compatibility patches for DGX Spark / GB10
build_backend_blackwell() {
    print_info "Building Blackwell backend image..."
    print_info "Platform: linux/arm64 (DGX Spark / Blackwell is ARM64-only)"
    print_info "Version: ${VERSION_FULL}"
    print_info "Tags: blackwell, blackwell-${VERSION_FULL}"

    cd backend

    docker buildx build \
        --platform "linux/arm64" \
        --file Dockerfile.blackwell \
        --build-arg APP_VERSION="${VERSION_FULL}" \
        --tag "${REPO_BACKEND}:blackwell" \
        --tag "${REPO_BACKEND}:blackwell-${VERSION_FULL}" \
        ${CACHE_FLAG} \
        --push \
        .

    cd ..

    print_success "Blackwell backend image built and pushed successfully"
    print_info "Tags pushed:"
    print_info "  - ${REPO_BACKEND}:blackwell"
    print_info "  - ${REPO_BACKEND}:blackwell-${VERSION_FULL}"
}

# Function to build and push backend (no scan - scan runs separately)
build_backend() {
    build_announce "Building backend image" || return 0

    local tag_args identity_args
    mapfile -t tag_args < <(build_tag_args "${REPO_BACKEND}")
    mapfile -t identity_args < <(build_backend_identity_args; build_identity_labels)

    cd backend

    docker buildx build \
        --platform "$(build_platforms)" \
        --file Dockerfile.prod \
        "${identity_args[@]}" \
        "${tag_args[@]}" \
        ${CACHE_FLAG} \
        "$(build_output_flag)" \
        .

    cd ..

    print_success "Backend image built (${BUILD_MODE} mode)"
    printf '%s\n' "${tag_args[@]}" | grep -v '^--tag$' | sed 's/^/  - /'
}

# Function to build and push frontend (no scan - scan runs separately)
build_frontend() {
    build_announce "Building frontend image" || return 0

    local tag_args identity_args
    mapfile -t tag_args < <(build_tag_args "${REPO_FRONTEND}")
    mapfile -t identity_args < <(build_identity_labels)

    cd frontend

    docker buildx build \
        --platform "$(build_platforms)" \
        --file Dockerfile.prod \
        "${identity_args[@]}" \
        "${tag_args[@]}" \
        ${CACHE_FLAG} \
        "$(build_output_flag)" \
        .

    cd ..

    print_success "Frontend image built (${BUILD_MODE} mode)"
    printf '%s\n' "${tag_args[@]}" | grep -v '^--tag$' | sed 's/^/  - /'
}

# Function to build and push docs (nginx:alpine + Docusaurus static build)
build_docs() {
    build_announce "Building docs image" || return 0

    local tag_args identity_args
    mapfile -t tag_args < <(build_tag_args "${REPO_DOCS}")
    mapfile -t identity_args < <(build_identity_labels)

    cd docs-site

    # OT_VERSION drives the homepage version badge. docs-site/Dockerfile has
    # declared this ARG all along and this script never passed it, so every
    # published opentranscribe-docs image rendered an empty badge — the badge only
    # ever worked for local `opentr.sh` builds, which do pass it.
    #
    # DOCS_BASE_URL keeps internal links correct when proxied at /docs/.
    docker buildx build \
        --platform "$(build_platforms)" \
        --file Dockerfile \
        --build-arg DOCS_BASE_URL=/docs/ \
        --build-arg OT_VERSION="${VERSION_FULL}" \
        "${identity_args[@]}" \
        "${tag_args[@]}" \
        ${CACHE_FLAG} \
        "$(build_output_flag)" \
        .

    cd ..

    print_success "Docs image built (${BUILD_MODE} mode)"
    printf '%s\n' "${tag_args[@]}" | grep -v '^--tag$' | sed 's/^/  - /'
}

# Function to run parallel security scans on both images
run_parallel_scans() {
    local components=("$@")

    if [ "${SKIP_SECURITY_SCAN}" = "true" ]; then
        print_warning "Security scanning skipped (SKIP_SECURITY_SCAN=true)"
        return 0
    fi

    print_info ""
    print_info "=========================================="
    print_info "Running security scans in parallel..."
    print_info "=========================================="

    # Update security tool databases first
    update_security_tools

    # SCAN_SOURCE=registry — discard the local image and pull :latest from Docker
    #   Hub, then scan that. The historical behaviour. Only meaningful AFTER a
    #   push, and only correct because the push happened first.
    # SCAN_SOURCE=local    — scan the image that was just built, without touching
    #   the registry. Required for a pre-push gate: registry mode would `docker
    #   rmi` the candidate and pull the PREVIOUS release in its place, so the
    #   scan would pass or fail on the wrong bytes entirely.
    local scan_source="${SCAN_SOURCE:-registry}"
    if [ "${BUILD_MODE}" = "local" ]; then
        scan_source="local"
    fi

    local pids=()

    if [ "${scan_source}" = "local" ]; then
        print_info "Scanning locally built images (no registry pull)"
        return_early_if_missing() {
            local image="$1"
            if ! docker image inspect "$image" >/dev/null 2>&1; then
                print_error "Image not present locally: $image"
                print_error "Build it first (BUILD_MODE=local) or use SCAN_SOURCE=registry"
                return 1
            fi
        }
        for component in "${components[@]}"; do
            case "$component" in
                backend)  return_early_if_missing "${REPO_BACKEND}:${VERSION_FULL}" || return 1 ;;
                frontend) return_early_if_missing "${REPO_FRONTEND}:${VERSION_FULL}" || return 1 ;;
            esac
        done
    else
        # Pull images in parallel
        print_info "Pulling images from the registry for scanning..."

        for component in "${components[@]}"; do
            if [ "$component" = "backend" ]; then
                (
                    docker rmi "${REPO_BACKEND}:latest" 2>/dev/null || true
                    docker pull --platform linux/amd64 "${REPO_BACKEND}:latest"
                ) &
                pids+=($!)
            elif [ "$component" = "frontend" ]; then
                (
                    docker rmi "${REPO_FRONTEND}:latest" 2>/dev/null || true
                    docker pull --platform linux/amd64 "${REPO_FRONTEND}:latest"
                ) &
                pids+=($!)
            fi
        done
    fi

    # Wait for all pulls (no-op in local mode — nothing was pulled)
    if [ ${#pids[@]} -gt 0 ]; then
        for pid in "${pids[@]}"; do
            wait "$pid"
        done
        print_success "Images pulled for scanning"
    fi

    # Run scans in parallel
    print_info "Starting parallel security scans..."

    # Create temp files for status tracking
    local status_dir
    status_dir=$(mktemp -d)

    for component in "${components[@]}"; do
        (
            print_info "[${component^}] Starting security scan..."
            if run_security_scan "$component"; then
                echo "0" > "${status_dir}/${component}.status"
                print_success "[${component^}] Security scan completed"
            else
                echo "1" > "${status_dir}/${component}.status"
                print_warning "[${component^}] Security scan had issues"
            fi
        ) 2>&1 | sed "s/^/[${component^}] /" &
    done

    # Wait for all scans to complete
    wait

    # Check results
    local all_passed=true
    for component in "${components[@]}"; do
        if [ -f "${status_dir}/${component}.status" ]; then
            status=$(cat "${status_dir}/${component}.status")
            if [ "$status" != "0" ]; then
                all_passed=false
            fi
        fi
    done

    # Cleanup
    rm -rf "${status_dir}"

    if [ "$all_passed" = true ]; then
        print_success "All security scans completed successfully!"
    else
        print_warning "Some security scans had issues (see above)"
    fi
}

# Function to scan only (no build, pull latest and scan)
scan_only() {
    print_info "Running security scan only (no build)..."
    # Reuse run_parallel_scans which handles DB updates, parallel pulls, and parallel scans
    run_parallel_scans "backend" "frontend"
}

# Function to delete old partial version tags from Docker Hub
cleanup_old_tags() {
    print_info "Cleaning up old partial version tags from Docker Hub..."
    print_info "This will delete vX and vX.X style tags (keeping latest and vX.Y.Z)"

    # Get Docker Hub token
    print_info "Authenticating with Docker Hub..."
    local token
    token=$(curl -s -X POST "https://hub.docker.com/v2/users/login/" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"${DOCKERHUB_USERNAME}\",\"password\":\"$(docker-credential-desktop get <<< 'https://index.docker.io/v1/' 2>/dev/null | jq -r .Secret 2>/dev/null || echo '')\"}" \
        2>/dev/null | jq -r .token 2>/dev/null)

    if [ -z "$token" ] || [ "$token" = "null" ]; then
        print_warning "Could not get Docker Hub token automatically."
        print_info "Please delete tags manually via Docker Hub web interface:"
        print_info "  Backend: https://hub.docker.com/r/${DOCKERHUB_USERNAME}/opentranscribe-backend/tags"
        print_info "  Frontend: https://hub.docker.com/r/${DOCKERHUB_USERNAME}/opentranscribe-frontend/tags"
        print_info ""
        print_info "Tags to delete (partial versions):"
        print_info "  - v0, v0.1, v0.2 (and similar)"
        print_info "  - Any commit SHA tags (e.g., 14accb6)"
        return 1
    fi

    # Function to delete a tag
    delete_tag() {
        local repo=$1
        local tag=$2
        print_info "Deleting ${repo}:${tag}..."
        local response
        response=$(curl -s -X DELETE \
            "https://hub.docker.com/v2/repositories/${DOCKERHUB_USERNAME}/${repo}/tags/${tag}/" \
            -H "Authorization: Bearer ${token}" \
            -w "%{http_code}")
        if [ "$response" = "204" ]; then
            print_success "Deleted ${repo}:${tag}"
        else
            print_warning "Failed to delete ${repo}:${tag} (may not exist)"
        fi
    }

    # List of partial version tags to delete
    local partial_tags=("v0" "v0.1" "v0.2")

    # Also find and delete commit SHA tags (7-8 character hex strings)
    print_info "Fetching existing tags..."
    for repo in "opentranscribe-backend" "opentranscribe-frontend"; do
        print_info "Processing ${repo}..."

        # Get all tags
        local tags_json
        tags_json=$(curl -s "https://hub.docker.com/v2/repositories/${DOCKERHUB_USERNAME}/${repo}/tags/?page_size=100" \
            -H "Authorization: Bearer ${token}" 2>/dev/null)

        # Delete partial version tags
        for tag in "${partial_tags[@]}"; do
            if echo "$tags_json" | jq -r '.results[].name' 2>/dev/null | grep -q "^${tag}$"; then
                delete_tag "$repo" "$tag"
            fi
        done

        # Delete commit SHA tags (7-8 hex characters)
        local sha_tags
        sha_tags=$(echo "$tags_json" | jq -r '.results[].name' 2>/dev/null | grep -E '^[a-f0-9]{7,8}$' || true)
        for tag in $sha_tags; do
            delete_tag "$repo" "$tag"
        done
    done

    print_success "Cleanup complete!"
    print_info "Remaining tags should be: latest, vX.Y.Z versions"
}

# Function to show usage
show_usage() {
    cat << EOF
Usage: $0 [OPTION]

Build and push Docker images to Docker Hub

Options:
    backend     Build and push only backend image
    frontend    Build and push only frontend image
    docs        Build and push only docs image (nginx:alpine + Docusaurus static)
    blackwell   Build and push Blackwell backend image (ARM64, :blackwell tag)
    all         Build and push all three images (default)
    auto        Auto-detect changes and build only changed components
    scan        Security scan only (pull latest images, scan, push reports)
    cleanup     Delete old partial version tags (vX, vX.X) from Docker Hub
    help        Show this help message

Environment Variables:
    VERSION                   Semantic version (e.g., v1.2.3) - overrides VERSION file
    DOCKERHUB_USERNAME        Docker Hub username (default: davidamacey)
    PLATFORMS                 Target platforms (default: linux/amd64,linux/arm64)
    USE_REMOTE_BUILDER        Use remote ARM64 builder for faster builds (default: false)
    REMOTE_BUILDER_NAME       Remote builder name (default: opentranscribe-multiarch)
    NO_CACHE                  Build without cache (default: false)
    SKIP_SECURITY_SCAN        Skip security scanning (default: false)
    FAIL_ON_SECURITY_ISSUES   Fail build if security issues found (default: false)
    FAIL_ON_CRITICAL          Fail scan if CRITICAL vulnerabilities found (default: false)

Examples:
    $0              # Build and push both images with security scanning
    $0 backend      # Build and push only backend
    $0 auto         # Auto-detect and build changed components
    $0 scan         # Security scan only (no build, pulls latest images)

    # Specify version (creates tags: latest, v1.2.3)
    VERSION=v1.2.3 $0 all

    # Version from VERSION file (recommended for releases)
    echo "v1.2.3" > VERSION
    $0 all

    # Use remote ARM64 builder for 10-20x faster builds
    USE_REMOTE_BUILDER=true $0 all

    # Build without cache (fresh build)
    NO_CACHE=true $0 frontend

    # Build only for current platform (faster)
    PLATFORMS=linux/amd64 $0 backend

    # Skip security scanning for faster builds
    SKIP_SECURITY_SCAN=true $0 all

    # Fail build if security issues found (recommended for CI)
    FAIL_ON_SECURITY_ISSUES=true FAIL_ON_CRITICAL=true $0 all

Versioning:
    The script supports semantic versioning via VERSION file or environment variable:
    - Creates tags: latest, vX.Y.Z (full version only)
    - Version can be specified as v1.2.3 or 1.2.3 (v prefix added automatically)
    - Environment variable VERSION overrides VERSION file
    - If neither exists, defaults to v0.0.0 with a warning
    - Use 'cleanup' command to remove old partial version tags (vX, vX.X)

Remote Builder Setup:
    For dramatically faster ARM64 builds, set up a remote builder:
    1. Run: ./scripts/setup-remote-builder.sh setup
    2. Follow the interactive prompts to configure your Mac Studio
    3. Use: USE_REMOTE_BUILDER=true $0

    This uses native ARM64 compilation instead of QEMU emulation (10-20x faster!)

Security Scanning:
    After building, images are automatically scanned with:
    - Hadolint: Dockerfile linting
    - Dockle: CIS best practices
    - Trivy: Vulnerability scanning
    - Grype: Additional vulnerability scanning
    - Syft: SBOM generation

    Reports are saved to ./security-reports/

EOF
}

# Main script
main() {
    # Version management - read from VERSION file or environment variable
    if [ -n "${VERSION}" ]; then
        # Use VERSION from environment variable
        SEMVER="${VERSION}"
    elif [ -f "VERSION" ]; then
        # Read VERSION from file
        SEMVER=$(cat VERSION | tr -d '[:space:]')
    else
        # Default version if neither exists
        SEMVER="v0.0.0"
    fi

    # Validate semantic version format (vX.Y.Z or X.Y.Z)
    if [[ ! "${SEMVER}" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        print_error "Invalid semantic version format: ${SEMVER}"
        print_error "Expected format: v1.2.3 or 1.2.3"
        exit 1
    fi

    # Ensure version starts with 'v'
    if [[ ! "${SEMVER}" =~ ^v ]]; then
        SEMVER="v${SEMVER}"
    fi

    # Parse version for tagging (only full version used now)
    VERSION_FULL="${SEMVER}"  # e.g., v1.2.3

    print_info "OpenTranscribe Docker Build & Push Script"
    print_info "=========================================="
    print_info "Version: ${VERSION_FULL}"
    print_info "Commit:  ${COMMIT_SHA}"
    print_info "Branch:  ${BRANCH}"
    print_info ""

    # Warn if using default version
    if [ "${SEMVER}" = "v0.0.0" ]; then
        print_warning "No VERSION file or VERSION env var found, using default: ${SEMVER}"
        print_warning "Create a VERSION file or set VERSION environment variable for production builds"
        print_info ""
    fi

    # Cache control - set NO_CACHE=true to force rebuild without cache
    CACHE_FLAG=""
    if [ "${NO_CACHE}" = "true" ]; then
        CACHE_FLAG="--no-cache"
        print_info "Building without cache (NO_CACHE=true)"
    fi

    # Check prerequisites
    check_docker
    check_docker_login

    # Select and configure builder based on USE_REMOTE_BUILDER setting
    if [ "${USE_REMOTE_BUILDER}" = "true" ]; then
        # Use remote multi-arch builder
        if ! docker buildx inspect "${REMOTE_BUILDER_NAME}" > /dev/null 2>&1; then
            print_error "Remote builder '${REMOTE_BUILDER_NAME}' not found!"
            print_info "Please run: ./scripts/setup-remote-builder.sh setup"
            print_info "Or set USE_REMOTE_BUILDER=false to use QEMU emulation"
            exit 1
        fi
        print_success "Using remote multi-arch builder: ${REMOTE_BUILDER_NAME}"
        print_info "This will use native ARM64 builds on your remote machine (much faster!)"
        docker buildx use "${REMOTE_BUILDER_NAME}"
        docker buildx inspect --bootstrap
    else
        # Use local builder with QEMU emulation (slower but works without setup)
        if ! docker buildx inspect "${DEFAULT_BUILDER_NAME}" > /dev/null 2>&1; then
            print_info "Creating local buildx builder (with QEMU emulation)..."
            docker buildx create --name "${DEFAULT_BUILDER_NAME}" --use
            docker buildx inspect --bootstrap
        else
            print_info "Using existing local buildx builder (with QEMU emulation)"
            docker buildx use "${DEFAULT_BUILDER_NAME}"
        fi
        if [[ "${PLATFORMS}" == *"arm64"* ]]; then
            print_warning "Building ARM64 with QEMU emulation (slow)"
            print_info "For faster builds, set up remote builder: ./scripts/setup-remote-builder.sh"
            print_info "Then use: USE_REMOTE_BUILDER=true $0"
        fi
    fi

    # Track which components were built for parallel scanning
    BUILT_COMPONENTS=()

    case "${BUILD_TARGET}" in
        backend)
            print_info "Building backend only..."
            build_backend
            BUILT_COMPONENTS+=("backend")
            ;;
        frontend)
            print_info "Building frontend only..."
            build_frontend
            BUILT_COMPONENTS+=("frontend")
            ;;
        docs)
            print_info "Building docs only..."
            build_docs
            BUILT_COMPONENTS+=("docs")
            ;;
        blackwell)
            print_info "Building Blackwell backend only (ARM64)..."
            build_backend_blackwell
            ;;
        all)
            print_info "Building backend, frontend, and docs..."
            build_backend
            build_frontend
            build_docs
            BUILT_COMPONENTS+=("backend" "frontend" "docs")
            ;;
        auto)
            print_info "Auto-detecting changes..."

            if detect_changes "backend"; then
                build_backend
                BUILT_COMPONENTS+=("backend")
            fi

            if detect_changes "frontend"; then
                build_frontend
                BUILT_COMPONENTS+=("frontend")
            fi

            if detect_changes "docs-site"; then
                build_docs
                BUILT_COMPONENTS+=("docs")
            fi

            if [ ${#BUILT_COMPONENTS[@]} -eq 0 ]; then
                print_warning "No changes detected in backend, frontend, or docs-site"
                print_info "Use '$0 all' to force build all images"
                exit 0
            fi
            ;;
        scan)
            print_info "Security scan only mode..."
            scan_only

            # Opt-in, same reasoning as the main path: this git-commits and
            # pushes security-reports/ onto the current branch.
            if [ "${PUSH_SECURITY_REPORTS:-false}" = "true" ]; then
                print_info ""
                print_info "📋 Pushing security reports..."
                SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
                if [ -f "${SCRIPT_DIR}/push-security-reports.sh" ]; then
                    VERSION="${VERSION_FULL}" "${SCRIPT_DIR}/push-security-reports.sh" || {
                        print_warning "⚠️  Security reports push had issues (see above for details)"
                    }
                fi
            else
                print_info ""
                print_info "📋 Security reports left uncommitted (PUSH_SECURITY_REPORTS=true to commit+push)"
            fi
            exit 0
            ;;
        cleanup)
            print_info "Cleanup mode - removing old partial version tags..."
            cleanup_old_tags
            exit 0
            ;;
        help|--help|-h)
            show_usage
            exit 0
            ;;
        *)
            print_error "Invalid option: ${BUILD_TARGET}"
            show_usage
            exit 1
            ;;
    esac

    # Run parallel security scans on all built components
    if [ ${#BUILT_COMPONENTS[@]} -gt 0 ]; then
        run_parallel_scans "${BUILT_COMPONENTS[@]}"
    fi

    print_success "All builds completed successfully!"
    print_info ""

    # Report what actually happened. This summary used to say "Images pushed to
    # Docker Hub" and list :latest unconditionally, which in local mode (or with
    # PUSH_LATEST=false) described a push that never occurred.
    local _dest _tags
    if [ "${BUILD_MODE}" = "local" ]; then
        _dest="loaded into the LOCAL Docker daemon (nothing pushed)"
    else
        _dest="pushed to Docker Hub"
    fi
    print_info "Images ${_dest} with version ${VERSION_FULL}:"

    _tags() {
        local repo="$1"
        print_info "  - ${repo}:${VERSION_FULL}"
        [ "${PUSH_LATEST}" = "true" ] && print_info "  - ${repo}:latest"
        return 0
    }
    if [ "${BUILD_TARGET}" = "backend" ] || [ "${BUILD_TARGET}" = "all" ] || [ "${BUILD_TARGET}" = "auto" ]; then
        print_info "Backend:"
        _tags "${REPO_BACKEND}"
    fi
    if [ "${BUILD_TARGET}" = "frontend" ] || [ "${BUILD_TARGET}" = "all" ] || [ "${BUILD_TARGET}" = "auto" ]; then
        print_info "Frontend:"
        _tags "${REPO_FRONTEND}"
    fi

    if [ "${BUILD_MODE}" != "local" ]; then
        print_info ""
        print_info "To pull:"
        print_info "  docker pull ${REPO_BACKEND}:${VERSION_FULL}   # Specific version"
        [ "${PUSH_LATEST}" = "true" ] && \
            print_info "  docker pull ${REPO_BACKEND}:latest      # Always latest"
    fi
    if [ "${PUSH_LATEST}" != "true" ] && [ "${BUILD_MODE}" != "local" ]; then
        print_info ""
        print_info "NOTE: :latest was NOT moved. Promote it by digest once validated:"
        print_info "  docker buildx imagetools create -t ${REPO_BACKEND}:latest ${REPO_BACKEND}:${VERSION_FULL}"
    fi

    # CRITICAL: Switch back to default builder to prevent interference with local dev builds
    print_info ""
    print_info "🔧 Switching back to default Docker builder..."
    docker buildx use default
    print_success "✅ Default builder restored. Local development builds will work normally."

    # Show build performance info if using emulation. Local mode builds host-arch
    # only, so QEMU is never involved there regardless of what PLATFORMS says.
    if [ "${BUILD_MODE}" != "local" ] && [ "${USE_REMOTE_BUILDER}" = "false" ] && [[ "${PLATFORMS}" == *"arm64"* ]]; then
        print_info ""
        print_info "⚡ Performance Tip:"
        print_info "You used QEMU emulation for ARM64 builds (10-20x slower than native)"
        print_info "To speed up future builds, set up a remote ARM64 builder:"
        print_info "  1. Run: ./scripts/setup-remote-builder.sh setup"
        print_info "  2. Then: USE_REMOTE_BUILDER=true $0"
    fi

    # Commit and push security reports — OPT-IN.
    #
    # push-security-reports.sh performs a `git commit` + `git push` of
    # security-reports/ onto WHATEVER BRANCH IS CHECKED OUT. Running that
    # automatically from a build is surprising in normal use and actively unsafe
    # during a release: it races the release commit, can land scan output on a
    # release tag's branch, and mutates the working tree in the middle of a
    # multi-stage pipeline.
    #
    # The release flow invokes it explicitly at the end, on master, after tagging.
    if [ "${PUSH_SECURITY_REPORTS:-false}" != "true" ]; then
        print_info ""
        print_info "📋 Security reports left uncommitted (PUSH_SECURITY_REPORTS=true to commit+push)"
        return 0
    fi

    print_info ""
    print_info "📋 Pushing security reports..."

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "${SCRIPT_DIR}/push-security-reports.sh" ]; then
        VERSION="${VERSION_FULL}" "${SCRIPT_DIR}/push-security-reports.sh" || {
            print_warning "⚠️  Security reports push had issues (see above for details)"
            print_info "You can manually run: ./scripts/push-security-reports.sh"
        }
    else
        print_warning "push-security-reports.sh not found, skipping auto-push"
    fi
}

# Run main function
main
