#!/bin/bash
set -e

# OpenTranscribe Windows Installer Builder
# Creates a complete Windows installer package for offline deployment
# Usage: ./scripts/build-windows-installer.sh [version] [--local]
#
# Options:
#   version    Package version (default: git short hash)
#   --local    Use locally built Docker images instead of pulling from Docker Hub
#
# This script runs on Linux and prepares all files needed for Inno Setup on Windows

# Source shared functions (colors, print helpers, model selection, GPU selection)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/offline-common.sh
source "${SCRIPT_DIR}/offline-common.sh"

# Load .env file if it exists and HUGGINGFACE_TOKEN is not already set
if [ -z "$HUGGINGFACE_TOKEN" ] && [ -f .env ]; then
    HUGGINGFACE_TOKEN=$(grep "^HUGGINGFACE_TOKEN=" .env | cut -d'=' -f2)
    export HUGGINGFACE_TOKEN
fi

# Parse command line arguments
# shellcheck disable=SC2034 # used by sourced offline-common.sh
USE_LOCAL_IMAGES=false
VERSION=""

for arg in "$@"; do
    case $arg in
        --local)
            # shellcheck disable=SC2034
            USE_LOCAL_IMAGES=true
            shift
            ;;
        *)
            # Assume it's the version if not a flag
            if [ -z "$VERSION" ]; then
                VERSION="$arg"
            fi
            ;;
    esac
done

# Configuration
VERSION="${VERSION:-$(git rev-parse --short HEAD)}"
PACKAGE_NAME="opentranscribe-windows-v${VERSION}"
BUILD_DIR="./offline-package-build/windows"
PACKAGE_DIR="${BUILD_DIR}/${PACKAGE_NAME}"


#######################
# PRE-FLIGHT CHECKS
#######################

preflight_checks() {
    print_header "Pre-flight Checks"

    # Check for required commands
    local missing_commands=()

    for cmd in docker git tar; do
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
        print_error "Docker is not running"
        exit 1
    fi

    print_success "Docker is running"

    # Check for required source files
    local missing_files=()

    if [ ! -f ".env.example" ]; then
        missing_files+=(".env.example")
    fi

    if [ ! -f "windows-installer/README-WINDOWS.md" ]; then
        missing_files+=("windows-installer/README-WINDOWS.md")
    fi

    if [ ! -f "windows-installer/installer.iss" ]; then
        missing_files+=("windows-installer/installer.iss")
    fi

    if [ ${#missing_files[@]} -ne 0 ]; then
        print_error "Missing required files: ${missing_files[*]}"
        exit 1
    fi

    print_success "All required source files present"

    # Check disk space (need at least 80GB free)
    local available_space
    available_space=$(df -BG . | tail -1 | awk '{print $4}' | sed 's/G//')
    if [ "$available_space" -lt 80 ]; then
        print_warning "Less than 80GB free space available (${available_space}GB)"
        print_warning "Package building may fail if you run out of space"
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    else
        print_success "Sufficient disk space available (${available_space}GB)"
    fi

    print_success "Pre-flight checks passed"
}

#######################
# IMAGE EXTRACTION
#######################

extract_infrastructure_images() {
    local compose_file="docker-compose.yml"

    if [ ! -f "$compose_file" ]; then
        print_error "docker-compose.yml not found!"
        exit 1
    fi

    # Extract infrastructure service images (postgres, redis, minio, opensearch)
    grep -E "^\s*image:\s*" "$compose_file" | \
        sed -E 's/^\s*image:\s*//; s/\s*$//' | \
        grep -v "^#" | \
        sort -u
}

extract_docker_images() {
    print_header "Extracting Docker Images from Configuration"

    print_info "Source: docker-compose.yml (single source of truth)"

    # Get infrastructure images from main docker-compose.yml
    mapfile -t INFRASTRUCTURE_IMAGES < <(extract_infrastructure_images)

    # Add production application images
    APPLICATION_IMAGES=(
        "davidamacey/opentranscribe-backend:latest"
        "davidamacey/opentranscribe-frontend:latest"
        "davidamacey/opentranscribe-docs:latest"
    )

    # Combine all images
    IMAGES=("${APPLICATION_IMAGES[@]}" "${INFRASTRUCTURE_IMAGES[@]}")

    if [ ${#IMAGES[@]} -eq 0 ]; then
        print_error "No images found!"
        exit 1
    fi

    print_success "Found ${#IMAGES[@]} images to package:"
    print_info "Application images (pre-built for production):"
    for img in "${APPLICATION_IMAGES[@]}"; do
        print_info "  - $img"
    done
    print_info "Infrastructure images (from docker-compose.yml):"
    for img in "${INFRASTRUCTURE_IMAGES[@]}"; do
        print_info "  - $img"
    done
    echo
}

#######################
# SETUP
#######################

setup_directories() {
    print_header "Setting Up Build Environment"

    # Clean old build if exists
    if [ -d "$BUILD_DIR" ]; then
        print_warning "Removing existing build directory..."
        rm -rf "$BUILD_DIR"
    fi

    # Create directory structure
    print_info "Creating directory structure..."
    mkdir -p "${PACKAGE_DIR}/docker-images"
    mkdir -p "${PACKAGE_DIR}/models/huggingface"
    mkdir -p "${PACKAGE_DIR}/models/torch"
    mkdir -p "${PACKAGE_DIR}/models/nltk_data"
    mkdir -p "${PACKAGE_DIR}/models/sentence-transformers"
    mkdir -p "${PACKAGE_DIR}/models/opensearch-ml"
    mkdir -p "${PACKAGE_DIR}/config"
    mkdir -p "${PACKAGE_DIR}/database"
    mkdir -p "${PACKAGE_DIR}/scripts"

    print_success "Directory structure created"
}

# pull_and_save_images() is provided by offline-common.sh

# select_whisper_model_for_offline(), select_opensearch_models_for_offline(),
# select_gpu_device_for_build(), pull_and_save_images() are provided by offline-common.sh

download_models() {
    print_header "Downloading AI Models"

    # Check for HuggingFace token
    if [ -z "$HUGGINGFACE_TOKEN" ]; then
        print_error "HUGGINGFACE_TOKEN environment variable not set"
        print_info "Get your token at: https://huggingface.co/settings/tokens"
        print_info "Export it: export HUGGINGFACE_TOKEN=your_token_here"
        exit 1
    fi

    print_info "Starting model download container..."
    print_info "Downloading Whisper model: ${WHISPER_MODEL}"
    print_info "This will download approximately 5-40GB depending on model size"
    print_warning "This may take 10-60 minutes depending on your internet speed..."

    # Create temporary model cache directory
    local temp_model_cache="${BUILD_DIR}/temp_models"
    mkdir -p "${temp_model_cache}/huggingface"
    mkdir -p "${temp_model_cache}/torch"
    mkdir -p "${temp_model_cache}/nltk_data"
    mkdir -p "${temp_model_cache}/sentence-transformers"
    mkdir -p "${temp_model_cache}/opensearch-ml"

    # Run backend container with model download script
    print_info "Running model download in Docker container..."

    # Determine GPU arguments
    local gpu_args="--gpus all"
    if [ -n "$GPU_DEVICE_ID" ]; then
        gpu_args="--gpus device=${GPU_DEVICE_ID}"
        print_info "Using GPU ${GPU_DEVICE_ID} for model downloads"
    fi

    # Run as appuser (non-root) matching container security configuration
    # IMPORTANT: When using --gpus device=X, Docker isolates that GPU and it appears as device 0 in the container
    # Do NOT set CUDA_VISIBLE_DEVICES - PyTorch will automatically use the only available GPU
    # shellcheck disable=SC2086
    docker run --rm \
        $gpu_args \
        -e HUGGINGFACE_TOKEN="${HUGGINGFACE_TOKEN}" \
        -e WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}" \
        -e DIARIZATION_MODEL="${DIARIZATION_MODEL:-pyannote/speaker-diarization-3.1}" \
        -e USE_GPU="${USE_GPU:-true}" \
        -e COMPUTE_TYPE="${COMPUTE_TYPE:-float16}" \
        -e OPENSEARCH_MODELS="${OPENSEARCH_MODELS:-}" \
        -e DOWNLOAD_ALL_OPENSEARCH_MODELS="${DOWNLOAD_ALL_OPENSEARCH_MODELS:-false}" \
        -v "${temp_model_cache}/huggingface:/home/appuser/.cache/huggingface" \
        -v "${temp_model_cache}/torch:/home/appuser/.cache/torch" \
        -v "${temp_model_cache}/nltk_data:/home/appuser/.cache/nltk_data" \
        -v "${temp_model_cache}/sentence-transformers:/home/appuser/.cache/sentence-transformers" \
        -v "${temp_model_cache}/opensearch-ml:/home/appuser/.cache/opensearch-ml" \
        -v "$(pwd)/scripts/download-models.py:/app/download-models.py" \
        davidamacey/opentranscribe-backend:latest \
        python /app/download-models.py

    # Copy models to package
    print_info "Copying models to package..."
    if [ -d "${temp_model_cache}/huggingface" ] && [ "$(ls -A ${temp_model_cache}/huggingface 2>/dev/null)" ]; then
        cp -r "${temp_model_cache}/huggingface"/* "${PACKAGE_DIR}/models/huggingface/"
        print_info "  Copied HuggingFace models"
    else
        print_warning "No HuggingFace models found to copy"
    fi

    if [ -d "${temp_model_cache}/torch" ] && [ "$(ls -A ${temp_model_cache}/torch 2>/dev/null)" ]; then
        cp -r "${temp_model_cache}/torch"/* "${PACKAGE_DIR}/models/torch/"
        print_info "  Copied PyTorch/PyAnnote models"
    else
        print_warning "No PyTorch models found to copy"
    fi

    if [ -d "${temp_model_cache}/nltk_data" ] && [ "$(ls -A ${temp_model_cache}/nltk_data 2>/dev/null)" ]; then
        cp -r "${temp_model_cache}/nltk_data"/* "${PACKAGE_DIR}/models/nltk_data/"
        print_info "  Copied NLTK data files"
    else
        print_warning "No NLTK data found to copy"
    fi

    if [ -d "${temp_model_cache}/sentence-transformers" ] && [ "$(ls -A ${temp_model_cache}/sentence-transformers 2>/dev/null)" ]; then
        cp -r "${temp_model_cache}/sentence-transformers"/* "${PACKAGE_DIR}/models/sentence-transformers/"
        print_info "  Copied sentence-transformers models"
    else
        print_warning "No sentence-transformers models found to copy"
    fi

    if [ -d "${temp_model_cache}/opensearch-ml" ] && [ "$(ls -A ${temp_model_cache}/opensearch-ml 2>/dev/null)" ]; then
        cp -r "${temp_model_cache}/opensearch-ml"/* "${PACKAGE_DIR}/models/opensearch-ml/"
        print_info "  Copied OpenSearch neural models"
    else
        print_warning "No OpenSearch neural models found to copy"
    fi

    # Check if model manifest was created
    if [ -f "${temp_model_cache}/huggingface/model_manifest.json" ]; then
        cp "${temp_model_cache}/huggingface/model_manifest.json" "${PACKAGE_DIR}/models/"
    else
        print_warning "Model manifest not found"
    fi

    # Clean up temp directory
    print_info "Cleaning up temporary files..."
    rm -rf "${temp_model_cache}"

    local model_size
    model_size=$(get_dir_size "${PACKAGE_DIR}/models")
    print_success "Models downloaded and packaged ($model_size)"
}

#######################
# CONFIGURATION FILES
#######################

copy_configuration() {
    print_header "Copying Configuration Files"

    # Copy docker-compose for Windows (base + offline overlay + gpu-scale)
    print_info "Copying docker-compose configuration..."
    cp docker-compose.yml "${PACKAGE_DIR}/config/docker-compose.yml"
    cp docker-compose.offline.yml "${PACKAGE_DIR}/config/docker-compose.offline.yml"

    # Copy gpu-scale.yml for multi-GPU Windows systems
    print_info "Copying docker-compose.gpu-scale.yml (multi-GPU scaling)..."
    cp docker-compose.gpu-scale.yml "${PACKAGE_DIR}/config/docker-compose.gpu-scale.yml"

    # Ship .env with placeholders intact - secrets are generated ON THE USER'S
    # MACHINE at first run (generate-secrets.ps1, invoked by run_opentranscribe.bat).
    # Baking secrets in at build time would give every installation of this
    # package identical passwords and encryption keys.
    print_info "Creating .env (credentials generated at first run on the target machine)..."
    cp .env.example "${PACKAGE_DIR}/.env"

    # Configure for offline/air-gapped deployment
    print_info "Configuring offline mode settings..."

    # Enable offline mode for HuggingFace (uncomment the line)
    sed -i "s|^#.*HF_HUB_OFFLINE=.*|HF_HUB_OFFLINE=1|g" "${PACKAGE_DIR}/.env"

    # Note: Windows paths use forward slashes in Docker volume mounts even on Windows
    # Docker Desktop on Windows handles the path translation automatically
    # The installer will be at C:\Program Files\OpenTranscribe, but Docker sees it as /host_mnt/c/...
    # We keep relative paths which work correctly with Docker Desktop's automatic path mounting

    print_success "Offline mode configured for Windows installer"
    print_info "✓ Offline mode enabled (HF_HUB_OFFLINE=1) - AI models pre-installed"
    print_info "✓ HUGGINGFACE_TOKEN not required for offline operation"
    print_info "✓ Unique credentials will be generated per-install at first run"

    # Also copy .env.example as template for reference
    print_info "Copying .env.example as template..."
    cp .env.example "${PACKAGE_DIR}/config/.env.example"

    # Note: Database schema is managed by Alembic migrations on backend startup
    # No init_db.sql needed in the package

    # Copy nginx config if it exists
    if [ -f "frontend/nginx.conf" ]; then
        print_info "Copying nginx configuration..."
        cp frontend/nginx.conf "${PACKAGE_DIR}/config/"
    fi

    print_success "Configuration files copied"
}

#######################
# WINDOWS INSTALLER FILES
#######################

copy_windows_installer_files() {
    print_header "Copying Windows Installer Files"

    # Copy existing installer files from windows-installer directory
    print_info "Copying Inno Setup script..."
    cp windows-installer/installer.iss "${PACKAGE_DIR}/"

    print_info "Copying batch scripts..."
    cp windows-installer/run_opentranscribe.bat "${PACKAGE_DIR}/"
    cp windows-installer/uninstall_opentranscribe.bat "${PACKAGE_DIR}/"

    print_info "Copying installer assets..."
    cp windows-installer/ot-icon.ico "${PACKAGE_DIR}/"

    # Copy license file (create minimal one if empty)
    if [ -s windows-installer/license.txt ]; then
        cp windows-installer/license.txt "${PACKAGE_DIR}/"
    else
        print_warning "license.txt is empty, creating placeholder..."
        echo "OpenTranscribe - AI-Powered Transcription Application" > "${PACKAGE_DIR}/license.txt"
        echo "" >> "${PACKAGE_DIR}/license.txt"
        echo "Copyright (c) $(date +%Y)" >> "${PACKAGE_DIR}/license.txt"
        echo "" >> "${PACKAGE_DIR}/license.txt"
        echo "See LICENSE file in the main repository for full license terms." >> "${PACKAGE_DIR}/license.txt"
    fi

    cp windows-installer/preinstall.txt "${PACKAGE_DIR}/"
    cp windows-installer/after-install.txt "${PACKAGE_DIR}/"

    # Copy prerequisite checker script
    if [ -f "windows-installer/check-prerequisites.ps1" ]; then
        print_info "Copying prerequisite checker..."
        cp windows-installer/check-prerequisites.ps1 "${PACKAGE_DIR}/"
    fi

    # Copy first-run secret generator (required - secrets are not baked into the package)
    print_info "Copying first-run secret generator..."
    cp windows-installer/generate-secrets.ps1 "${PACKAGE_DIR}/"

    # Copy Windows-specific README
    print_info "Copying Windows README..."
    cp windows-installer/README-WINDOWS.md "${PACKAGE_DIR}/"

    print_success "Windows installer files copied"
}

#######################
# PACKAGE FINALIZATION
#######################

finalize_package() {
    print_header "Finalizing Package"

    # Create checksums for Docker images (important for integrity verification)
    print_info "Creating Docker image checksums..."
    cd "${PACKAGE_DIR}/docker-images"
    for tar_file in *.tar; do
        if [ -f "$tar_file" ]; then
            sha256sum "$tar_file" >> checksums.sha256
            print_info "  Checksum created for $tar_file"
        fi
    done
    cd - > /dev/null

    # Create package info
    print_info "Creating package metadata..."
    cat > "${PACKAGE_DIR}/package-info.json" <<EOF
{
  "name": "OpenTranscribe Windows Installer Package",
  "version": "${VERSION}",
  "created_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "created_by": "$(whoami)@$(hostname)",
  "git_commit": "$(git rev-parse HEAD)",
  "git_branch": "$(git rev-parse --abbrev-ref HEAD)",
  "docker_images": ${#IMAGES[@]},
  "whisper_model": "${WHISPER_MODEL}",
  "package_size_uncompressed": "$(get_dir_size "$PACKAGE_DIR")",
  "target_platform": "Windows 10/11 with Docker Desktop"
}
EOF

    # Create checksums for entire package
    create_checksums "$PACKAGE_DIR"

    print_success "Package finalized"
}

#######################
# MAIN
#######################

main() {
    print_header "OpenTranscribe Windows Installer Builder v${VERSION}"

    print_info "This script will create a Windows installer package"
    print_info "Package name: ${PACKAGE_NAME}"
    print_info "Estimated size: ~60GB uncompressed"
    print_warning "This process will take 1-2 hours and requires internet access"
    echo

    # Execute build steps
    preflight_checks
    extract_docker_images
    setup_directories
    select_whisper_model_for_offline
    select_opensearch_models_for_offline
    select_gpu_device_for_build
    pull_and_save_images
    download_models
    copy_configuration
    copy_windows_installer_files
    finalize_package

    # Final summary
    print_header "Build Complete!"

    echo -e "${GREEN}✅ Windows installer package created successfully!${NC}\n"

    echo -e "Package location:"
    echo -e "  ${CYAN}${PACKAGE_DIR}/${NC}\n"

    echo -e "Next steps:"
    echo -e "  1. Transfer the entire folder to a Windows machine"
    echo -e "  2. Install Inno Setup on Windows: ${YELLOW}https://jrsoftware.org/isdl.php${NC}"
    echo -e "  3. Open ${YELLOW}installer.iss${NC} in Inno Setup Compiler"
    echo -e "  4. Update the BuildDir path in installer.iss to match your Windows location"
    echo -e "  5. Click Build → Compile to create the installer executable"
    echo -e "  6. Run the generated .exe to install OpenTranscribe\n"

    echo -e "Package contents:"
    echo -e "  - Docker images: ${#IMAGES[@]} images"
    echo -e "  - AI models: ${WHISPER_MODEL} and supporting models"
    echo -e "  - Configuration files and database schema"
    echo -e "  - Inno Setup script and batch files"
    echo -e "  - Complete documentation\n"

    print_success "🎉 Build process complete!"
}

# Run main function
main
