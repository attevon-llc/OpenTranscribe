#!/bin/bash
set -euo pipefail

# OpenTranscribe Complete Build Pipeline
# Builds Docker images, runs security scans, and creates offline package
#
# Usage:
#   ./scripts/build-all.sh [OPTIONS] [version]
#
# Options:
#   -i, --interactive    Interactive mode with prompts and confirmation
#   -h, --help          Show this help message
#
# Examples:
#   ./scripts/build-all.sh                           # Run with auto-detected version
#   ./scripts/build-all.sh v2.1.0                    # Run with specific version
#   ./scripts/build-all.sh --interactive             # Interactive mode with prompts
#   ./scripts/build-all.sh --interactive v2.1.0      # Interactive mode with version
#
# Background execution:
#   nohup ./scripts/build-all.sh > build-$(date +%Y%m%d-%H%M%S).log 2>&1 &

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

# Parse command line arguments
INTERACTIVE_MODE=false
VERSION=""

while [[ $# -gt 0 ]]; do
    case $1 in
        -i|--interactive)
            INTERACTIVE_MODE=true
            shift
            ;;
        -h|--help)
            sed -n '3,18p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        -*)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
        *)
            VERSION="$1"
            shift
            ;;
    esac
done

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_FILE="${PROJECT_ROOT}/build-all-$(date +%Y%m%d-%H%M%S).log"
START_TIME=$(date +%s)

# Build configuration
export DOCKERHUB_USERNAME="${DOCKERHUB_USERNAME:-davidamacey}"
export FAIL_ON_SECURITY_ISSUES="${FAIL_ON_SECURITY_ISSUES:-false}"
export FAIL_ON_CRITICAL="${FAIL_ON_CRITICAL:-false}"
export SKIP_SECURITY_SCAN="${SKIP_SECURITY_SCAN:-false}"
export NO_CACHE="${NO_CACHE:-true}"  # Default to clean builds (like CI/CD)

#######################
# HELPER FUNCTIONS
#######################

print_banner() {
    echo -e "\n${MAGENTA}╔══════════════════════════════════════════════════════════════╗${NC}"
    printf "${MAGENTA}║${NC} %-60s ${MAGENTA}║${NC}\n" "$1"
    echo -e "${MAGENTA}╚══════════════════════════════════════════════════════════════╝${NC}\n"
}

print_header() {
    echo -e "\n${CYAN}================================================================${NC}"
    echo -e "${CYAN}  $1${NC}"
    echo -e "${CYAN}================================================================${NC}\n"
}

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

# Calculate elapsed time
get_elapsed_time() {
    local end_time
    end_time=$(date +%s)
    local elapsed=$((end_time - START_TIME))
    local hours=$((elapsed / 3600))
    local minutes=$(((elapsed % 3600) / 60))
    local seconds=$((elapsed % 60))
    printf "%02d:%02d:%02d" $hours $minutes $seconds
}

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

#######################
# PRE-FLIGHT CHECKS
#######################

preflight_checks() {
    print_header "Pre-flight Checks"

    # Check required commands
    local missing_commands=()
    for cmd in docker git tar xz jq; do
        if ! command_exists "$cmd"; then
            missing_commands+=("$cmd")
        fi
    done

    if [ ${#missing_commands[@]} -ne 0 ]; then
        print_error "Missing required commands: ${missing_commands[*]}"
        exit 1
    fi
    print_success "All required commands available"

    # Check Docker is running
    if ! docker info > /dev/null 2>&1; then
        print_error "Docker is not running. Please start Docker and try again."
        exit 1
    fi
    print_success "Docker is running"

    # Check Docker Hub login
    if ! docker info | grep -q "Username"; then
        print_error "Not logged into Docker Hub. Please run: docker login"
        exit 1
    fi
    print_success "Logged into Docker Hub as: $(docker info | grep Username | awk '{print $2}')"

    # Check for HuggingFace token (needed for offline package)
    if [ -z "${HUGGINGFACE_TOKEN:-}" ]; then
        # Try to load from .env
        if [ -f "${PROJECT_ROOT}/.env" ]; then
            # python-dotenv, not grep/cut/tr (issue #590) -- the old chain silently
            # corrupted a token line carrying a trailing `  # comment`.
            HUGGINGFACE_TOKEN=$(python3 "${SCRIPT_DIR}/lib/env_reader.py" "${PROJECT_ROOT}/.env" HUGGINGFACE_TOKEN)
            export HUGGINGFACE_TOKEN
        fi

        if [ -z "${HUGGINGFACE_TOKEN:-}" ]; then
            print_error "HUGGINGFACE_TOKEN not set and not found in .env file"
            print_info "Get your token at: https://huggingface.co/settings/tokens"
            print_info "Set it: export HUGGINGFACE_TOKEN=your_token_here"
            exit 1
        fi
    fi
    print_success "HuggingFace token configured"

    # Check disk space (need at least 100GB free for the full build)
    local available_space
    available_space=$(df -BG "$PROJECT_ROOT" | tail -1 | awk '{print $4}' | sed 's/G//')
    if [ "$available_space" -lt 100 ]; then
        print_warning "Less than 100GB free space available (${available_space}GB)"
        print_warning "Build may fail if you run out of space"
    else
        print_success "Sufficient disk space available (${available_space}GB)"
    fi

    # Check if we're in the project root
    if [ ! -f "${PROJECT_ROOT}/docker-compose.yml" ]; then
        print_error "docker-compose.yml not found. Are you in the project root?"
        exit 1
    fi
    print_success "Running from project root: ${PROJECT_ROOT}"

    print_success "All pre-flight checks passed"
}

#######################
# BUILD PHASE 1: Docker Images & Security Scans
#######################

build_docker_images() {
    print_banner "PHASE 1: Building Docker Images & Security Scanning"

    local phase_start
    phase_start=$(date +%s)

    print_info "Starting Docker image build and push..."
    print_info "Version tag: ${VERSION}"
    print_info "Build mode: $([ "$NO_CACHE" = "true" ] && echo "CLEAN BUILD (no cache)" || echo "Cached build")"
    print_info "Security scanning: $([ "$SKIP_SECURITY_SCAN" = "true" ] && echo "DISABLED" || echo "ENABLED")"
    echo ""

    cd "$PROJECT_ROOT"

    # Run docker build and push script
    if [ "$SKIP_SECURITY_SCAN" = "true" ]; then
        print_warning "Security scanning is DISABLED (SKIP_SECURITY_SCAN=true)"
    fi

    if ! "${SCRIPT_DIR}/docker-build-push.sh" all; then
        print_error "Docker build and push failed"
        exit 1
    fi

    local phase_end
    phase_end=$(date +%s)
    local phase_elapsed=$((phase_end - phase_start))
    local phase_minutes=$((phase_elapsed / 60))
    local phase_seconds=$((phase_elapsed % 60))

    print_success "Phase 1 completed in ${phase_minutes}m ${phase_seconds}s"

    # Display security report summary if scans were run
    if [ "$SKIP_SECURITY_SCAN" != "true" ] && [ -d "${PROJECT_ROOT}/security-reports" ]; then
        echo ""
        print_info "Security Reports Generated:"
        find "${PROJECT_ROOT}/security-reports" -maxdepth 1 -type f -exec ls -lh {} \; | awk '{printf "  %-40s %8s\n", $9, $5}'
        echo ""
    fi
}

#######################
# BUILD PHASE 2: Offline Package
#######################

build_offline_package() {
    print_banner "PHASE 2: Building Offline Deployment Package"

    local phase_start
    phase_start=$(date +%s)

    print_info "Starting offline package build..."
    print_info "Package version: ${VERSION}"
    print_warning "This phase will take 1.5-2 hours (pulling images + downloading models)"
    echo ""

    cd "$PROJECT_ROOT"

    # Export HUGGINGFACE_TOKEN for the script
    export HUGGINGFACE_TOKEN

    # Run offline package builder
    if ! "${SCRIPT_DIR}/build-offline-package.sh" "$VERSION"; then
        print_error "Offline package build failed"
        exit 1
    fi

    local phase_end
    phase_end=$(date +%s)
    local phase_elapsed=$((phase_end - phase_start))
    local phase_hours=$((phase_elapsed / 3600))
    local phase_minutes=$(((phase_elapsed % 3600) / 60))
    local phase_seconds=$((phase_elapsed % 60))

    print_success "Phase 2 completed in ${phase_hours}h ${phase_minutes}m ${phase_seconds}s"

    # Display package info
    if [ -d "${PROJECT_ROOT}/offline-package-build" ]; then
        echo ""
        print_info "Offline Package Created:"
        find "${PROJECT_ROOT}/offline-package-build" -maxdepth 1 -name "*.tar.xz*" -type f -exec ls -lh {} \; 2>/dev/null | awk '{printf "  %-60s %10s\n", $9, $5}' || print_warning "Package files not found"
        echo ""
    fi
}

#######################
# FINAL SUMMARY
#######################

generate_final_summary() {
    print_banner "BUILD PIPELINE COMPLETE"

    local end_time
    end_time=$(date +%s)
    local total_elapsed=$((end_time - START_TIME))
    local total_hours=$((total_elapsed / 3600))
    local total_minutes=$(((total_elapsed % 3600) / 60))
    local total_seconds=$((total_elapsed % 60))

    echo -e "${GREEN}✅ All build tasks completed successfully!${NC}\n"

    print_info "Build Summary"
    echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}"
    printf "  %-30s %s\n" "Version:" "${VERSION}"
    printf "  %-30s %s\n" "Total Duration:" "${total_hours}h ${total_minutes}m ${total_seconds}s"
    printf "  %-30s %s\n" "Completed At:" "$(date '+%Y-%m-%d %H:%M:%S')"
    echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}\n"

    print_info "Deliverables Created"
    echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}"

    # Docker images
    echo -e "\n  ${YELLOW}Docker Images (pushed to Docker Hub):${NC}"
    echo "    - ${DOCKERHUB_USERNAME}/opentranscribe-backend:latest"
    echo "    - ${DOCKERHUB_USERNAME}/opentranscribe-backend:${VERSION}"
    echo "    - ${DOCKERHUB_USERNAME}/opentranscribe-frontend:latest"
    echo "    - ${DOCKERHUB_USERNAME}/opentranscribe-frontend:${VERSION}"

    # Security reports
    if [ "$SKIP_SECURITY_SCAN" != "true" ] && [ -d "${PROJECT_ROOT}/security-reports" ]; then
        echo -e "\n  ${YELLOW}Security Reports:${NC}"
        echo "    📁 ${PROJECT_ROOT}/security-reports/"
        local report_count
        report_count=$(find "${PROJECT_ROOT}/security-reports" -maxdepth 1 -type f 2>/dev/null | wc -l)
        echo "    📊 ${report_count} report files generated"
    fi

    # Offline package
    if [ -d "${PROJECT_ROOT}/offline-package-build" ]; then
        echo -e "\n  ${YELLOW}Offline Package:${NC}"
        local package_file
        package_file=$(find "${PROJECT_ROOT}/offline-package-build" -maxdepth 1 -name "*.tar.xz" -type f 2>/dev/null | head -1)
        if [ -n "$package_file" ]; then
            local package_size
            package_size=$(du -sh "$package_file" 2>/dev/null | cut -f1)
            echo "    📦 $(basename "$package_file")"
            echo "    💾 Size: ${package_size}"
            echo "    📍 ${package_file}"
            echo "    🔐 Checksum: ${package_file}.sha256"
        fi
    fi

    echo -e "\n${CYAN}────────────────────────────────────────────────────────────${NC}\n"

    print_info "Next Steps"
    echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}"
    echo ""
    echo "  1. Verify offline package checksum:"
    echo "     ${YELLOW}cd offline-package-build && sha256sum -c *.tar.xz.sha256${NC}"
    echo ""
    echo "  2. Review security reports (if enabled):"
    echo "     ${YELLOW}ls -lh security-reports/${NC}"
    echo ""
    echo "  3. Test Docker images:"
    echo "     ${YELLOW}docker pull ${DOCKERHUB_USERNAME}/opentranscribe-backend:${VERSION}${NC}"
    echo "     ${YELLOW}docker pull ${DOCKERHUB_USERNAME}/opentranscribe-frontend:${VERSION}${NC}"
    echo ""
    echo "  4. Transfer offline package to target systems"
    echo ""
    echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}\n"

    print_success "🎉 Build pipeline finished successfully!"
    print_info "Log file: ${LOG_FILE}"
}

#######################
# ERROR HANDLER
#######################

error_handler() {
    local exit_code=$?
    local line_number=$1

    echo ""
    print_error "Build pipeline failed at line ${line_number} with exit code ${exit_code}"
    print_error "Elapsed time: $(get_elapsed_time)"
    print_info "Check the log file for details: ${LOG_FILE}"
    exit $exit_code
}

trap 'error_handler ${LINENO}' ERR

#######################
# INTERACTIVE MODE
#######################

interactive_setup() {
    print_banner "OpenTranscribe Complete Build Pipeline"

    print_info "Interactive Setup Mode"
    echo ""

    # Step 1: Check Docker login
    echo -e "${BLUE}[1/4] Checking Docker login...${NC}"
    if ! docker info | grep -q "Username"; then
        echo -e "${RED}❌ Not logged into Docker Hub${NC}"
        echo "Please run: docker login"
        exit 1
    fi
    echo -e "${GREEN}✅ Docker logged in as: $(docker info | grep Username | awk '{print $2}')${NC}"
    echo ""

    # Step 2: Check HuggingFace token
    echo -e "${BLUE}[2/4] Checking HuggingFace token...${NC}"
    if [ -z "${HUGGINGFACE_TOKEN:-}" ]; then
        if [ -f "${PROJECT_ROOT}/.env" ]; then
            # python-dotenv, not grep/cut/tr (issue #590) -- the old chain silently
            # corrupted a token line carrying a trailing `  # comment`.
            HUGGINGFACE_TOKEN=$(python3 "${SCRIPT_DIR}/lib/env_reader.py" "${PROJECT_ROOT}/.env" HUGGINGFACE_TOKEN)
            export HUGGINGFACE_TOKEN
        fi
    fi

    if [ -z "${HUGGINGFACE_TOKEN:-}" ]; then
        echo -e "${RED}❌ HUGGINGFACE_TOKEN not set${NC}"
        echo "Get your token at: https://huggingface.co/settings/tokens"
        echo "Then run: export HUGGINGFACE_TOKEN=hf_your_token_here"
        exit 1
    fi
    echo -e "${GREEN}✅ HuggingFace token configured${NC}"
    echo ""

    # Step 3: Check disk space
    echo -e "${BLUE}[3/4] Checking disk space...${NC}"
    local available_space
    available_space=$(df -BG "$PROJECT_ROOT" | tail -1 | awk '{print $4}' | sed 's/G//')
    if [ "$available_space" -lt 100 ]; then
        echo -e "${YELLOW}⚠️  Warning: Less than 100GB available (${available_space}GB)${NC}"
        echo "Build may fail if you run out of space."
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    else
        echo -e "${GREEN}✅ Sufficient disk space: ${available_space}GB${NC}"
    fi
    echo ""

    # Step 4: Version configuration
    echo -e "${BLUE}[4/4] Version configuration...${NC}"
    local git_commit
    git_commit=$(git rev-parse --short HEAD)
    echo "Current git commit: ${git_commit}"

    if [ -z "$VERSION" ]; then
        read -r -p "Enter version tag (or press Enter to use git commit SHA): " VERSION
        if [ -z "$VERSION" ]; then
            VERSION="$git_commit"
        fi
    fi
    echo -e "${GREEN}✅ Version: ${VERSION}${NC}"
    echo ""

    # Display configuration summary
    echo "=================================================="
    echo "Build Configuration"
    echo "=================================================="
    echo "Version:               $VERSION"
    echo "Docker Hub User:       $(docker info | grep Username | awk '{print $2}')"
    echo "HuggingFace Token:     Set ✅"
    echo "Disk Space:            ${available_space}GB"
    echo "Estimated Time:        2-3 hours"
    echo ""
    echo "This will create:"
    echo "  - Docker images (pushed to Docker Hub)"
    echo "  - Security reports (./security-reports/)"
    echo "  - Offline package (./offline-package-build/)"
    echo ""
    echo "=================================================="
    echo ""

    # Ask for confirmation
    read -p "Start build pipeline? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Build cancelled."
        exit 0
    fi

    echo ""
    echo -e "${GREEN}🚀 Starting build pipeline...${NC}"
    echo ""
}

#######################
# MAIN EXECUTION
#######################

main() {
    # Set default version if not provided
    if [ -z "$VERSION" ]; then
        VERSION=$(git rev-parse --short HEAD)
    fi

    # Run interactive setup if requested
    if [ "$INTERACTIVE_MODE" = true ]; then
        interactive_setup
    fi

    print_banner "OpenTranscribe Complete Build Pipeline v${VERSION}"

    print_info "Build Configuration"
    echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}"
    printf "  %-30s %s\n" "Version:" "${VERSION}"
    printf "  %-30s %s\n" "Docker Hub User:" "${DOCKERHUB_USERNAME}"
    printf "  %-30s %s\n" "Build Mode:" "$([ "$NO_CACHE" = "true" ] && echo "Clean (no cache)" || echo "Cached")"
    printf "  %-30s %s\n" "Security Scanning:" "$([ "$SKIP_SECURITY_SCAN" = "true" ] && echo "DISABLED" || echo "ENABLED")"
    printf "  %-30s %s\n" "Fail on Critical:" "${FAIL_ON_CRITICAL}"
    printf "  %-30s %s\n" "Project Root:" "${PROJECT_ROOT}"
    printf "  %-30s %s\n" "Started At:" "$(date '+%Y-%m-%d %H:%M:%S')"
    echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}\n"

    print_warning "Estimated total time: 2-3 hours"
    print_info "Phase 1 (Docker build + security): ~30-45 minutes"
    print_info "Phase 2 (Offline package): ~1.5-2 hours"
    echo ""

    # Execute build pipeline
    preflight_checks
    build_docker_images
    build_offline_package
    generate_final_summary
}

# Run main function
main
