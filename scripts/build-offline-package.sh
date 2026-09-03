#!/bin/bash
set -e

# OpenTranscribe Offline Package Builder
# Creates a complete offline installation package for air-gapped deployments
# Usage: ./scripts/build-offline-package.sh [version] [--local]
#
# Options:
#   version    Package version (default: git short hash)
#   --local    Use locally built Docker images instead of pulling from Docker Hub

# Source shared functions (colors, print helpers, model selection, GPU selection)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/offline-common.sh
source "${SCRIPT_DIR}/offline-common.sh"

# Load .env file if it exists and HUGGINGFACE_TOKEN is not already set
if [ -z "$HUGGINGFACE_TOKEN" ] && [ -f .env ]; then
    # python-dotenv, not grep/cut (issue #590) -- see gpu-scale-smoke.sh's read_env
    # for the inline-comment corruption class this replaces.
    HUGGINGFACE_TOKEN=$(python3 "${SCRIPT_DIR}/lib/env_reader.py" .env HUGGINGFACE_TOKEN)
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
PACKAGE_NAME="opentranscribe-offline-v${VERSION}"
BUILD_DIR="./offline-package-build/linux"
PACKAGE_DIR="${BUILD_DIR}/${PACKAGE_NAME}"

#######################
# PRE-FLIGHT CHECKS
#######################

preflight_checks() {
    print_header "Pre-flight Checks"

    # Check for required commands
    local missing_commands=()

    for cmd in docker git tar xz; do
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

    # Check disk space (need at least 80GB free for build)
    # Note: Windows check-prerequisites.ps1 requires 100GB for the installed system (includes runtime overhead)
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
    # These are the same in both dev and production
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

    # Add production application images (these use 'build:' in dev, pre-built images in prod)
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
    # Native diarizer (diar-server) ONNX/PLDA export. An air-gapped install cannot
    # provision this itself (no network to HuggingFace), so the tarball must carry it —
    # same reasoning as every other model group here, just mounted at /models rather
    # than under a ~/.cache/* subdirectory.
    mkdir -p "${PACKAGE_DIR}/models/diar-native"
    mkdir -p "${PACKAGE_DIR}/config"
    mkdir -p "${PACKAGE_DIR}/database"
    mkdir -p "${PACKAGE_DIR}/scripts"

    print_success "Directory structure created"
}

# pull_and_save_images() is provided by offline-common.sh

#######################
# AI MODELS
#######################

# select_whisper_model_for_offline(), select_opensearch_models_for_offline(),
# select_gpu_device_for_build() are provided by offline-common.sh

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
    if [ -n "$OPENSEARCH_MODELS" ]; then
        print_info "Downloading OpenSearch models: ${OPENSEARCH_MODELS}"
    elif [ "$DOWNLOAD_ALL_OPENSEARCH_MODELS" = "true" ]; then
        print_info "Downloading OpenSearch models: ALL (6 models)"
    else
        print_info "Downloading OpenSearch models: default (all-MiniLM-L6-v2)"
    fi
    print_info "This will download approximately 5-40GB depending on model selection"
    print_warning "This may take 10-60 minutes depending on your internet speed..."

    # Create temporary model cache directory
    local temp_model_cache="${BUILD_DIR}/temp_models"
    mkdir -p "${temp_model_cache}/huggingface"
    mkdir -p "${temp_model_cache}/torch"
    mkdir -p "${temp_model_cache}/nltk_data"
    mkdir -p "${temp_model_cache}/sentence-transformers"
    mkdir -p "${temp_model_cache}/opensearch-ml"
    mkdir -p "${temp_model_cache}/diar-native"

    # Run backend container with model download script
    print_info "Running model download in Docker container..."

    # Determine GPU arguments
    local gpu_args="--gpus all"
    if [ -n "$GPU_DEVICE_ID" ]; then
        gpu_args="--gpus device=${GPU_DEVICE_ID}"
        print_info "Using GPU ${GPU_DEVICE_ID} for model downloads"
    fi

    # Run as appuser (non-root) matching container security configuration
    # IMPORTANT: When using --gpus device=X, Docker isolates that GPU and it appears as the only GPU in the container
    # Do NOT set CUDA_VISIBLE_DEVICES as it conflicts with Docker's GPU isolation and causes CUDA errors
    # PyTorch will automatically use the only available GPU
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
        -v "${temp_model_cache}/diar-native:/models" \
        -v "$(pwd)/scripts/download-models.py:/app/download-models.py" \
        davidamacey/opentranscribe-backend:latest \
        python /app/download-models.py

    # Copy models to package (files created as appuser UID 1000)
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
        print_info "  Copied OpenSearch neural search models"
    else
        print_warning "No OpenSearch neural models found to copy"
    fi

    if [ -d "${temp_model_cache}/diar-native" ] && [ "$(ls -A ${temp_model_cache}/diar-native 2>/dev/null)" ]; then
        cp -r "${temp_model_cache}/diar-native"/* "${PACKAGE_DIR}/models/diar-native/"
        print_info "  Copied native diarizer (diar-server) export"
    else
        print_warning "No native diarizer export found to copy — DIAR_NATIVE_MODEL_SET may be"
        print_warning "unset, HUGGINGFACE_TOKEN may lack access, or this backend image predates it"
    fi

    # Check if model manifest was created (it's inside the huggingface cache dir)
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

    # Copy base docker-compose.yml
    print_info "Copying base docker-compose.yml..."
    cp docker-compose.yml "${PACKAGE_DIR}/config/docker-compose.yml"

    # Copy docker-compose.gpu-scale.yml for multi-GPU support
    print_info "Copying docker-compose.gpu-scale.yml (multi-GPU scaling)..."
    cp docker-compose.gpu-scale.yml "${PACKAGE_DIR}/config/docker-compose.gpu-scale.yml"

    # Copy docker-compose.diar-native.yml so an air-gapped install can run the native
    # diarization sidecar entirely from what shipped in the package -- it has no network
    # to fetch this overlay later. opentr-offline.sh loads it only once weights are
    # present under models/diar-native/ (populated above by download_models()).
    print_info "Copying docker-compose.diar-native.yml (native diarization sidecar)..."
    cp docker-compose.diar-native.yml "${PACKAGE_DIR}/config/docker-compose.diar-native.yml"
    # The GPU half is a separate file so the base overlay stays loadable on a CPU-only
    # host (#660). Both must ship: with only the first, an air-gapped GPU install gets a
    # CPU-bound sidecar that produces identical output, so the mistake never surfaces.
    if [ -f docker-compose.diar-native-gpu.yml ]; then
        print_info "Copying docker-compose.diar-native-gpu.yml (sidecar GPU reservation)..."
        cp docker-compose.diar-native-gpu.yml "${PACKAGE_DIR}/config/docker-compose.diar-native-gpu.yml"
    fi

    # Sync infrastructure image versions from docker-compose.yml to docker-compose.offline.yml
    print_info "Syncing infrastructure image versions to docker-compose.offline.yml..."

    # Create temporary copy of offline compose
    local temp_compose="${PACKAGE_DIR}/config/docker-compose.offline.yml"
    cp docker-compose.offline.yml "$temp_compose"

    # Extract and sync each infrastructure image version
    for img in "${INFRASTRUCTURE_IMAGES[@]}"; do
        # Get service name and image (e.g., postgres:17.5-alpine -> postgres and full image)
        local service_name
        service_name=$(echo "$img" | cut -d: -f1 | cut -d/ -f1)

        # Update the image line in offline compose file for this service
        # Find the service block and update its image line
        sed -i "s|image: ${service_name}[:/][^ ]*|image: ${img}|g" "$temp_compose"
    done

    print_success "Infrastructure images synced (base + offline override pattern)"

    # Copy .env.example file (required by installation script)
    print_info "Copying .env.example..."
    cp .env.example "${PACKAGE_DIR}/.env.example"

    # Note: Database schema is managed by Alembic migrations on backend startup
    # No init_db.sql needed in the package

    # Copy nginx config
    print_info "Copying nginx configuration..."
    cp frontend/nginx.conf "${PACKAGE_DIR}/config/"

    # Copy common scripts
    print_info "Copying utility scripts..."
    cp scripts/common.sh "${PACKAGE_DIR}/scripts/"

    print_success "Configuration files copied"
}

#######################
# INSTALLATION SCRIPTS
#######################

copy_installation_scripts() {
    print_header "Copying Installation Scripts"

    # Copy installation script
    print_info "Copying install.sh..."
    cp scripts/install-offline-package.sh "${PACKAGE_DIR}/install.sh"
    chmod +x "${PACKAGE_DIR}/install.sh"

    # Copy uninstall script
    print_info "Copying uninstall.sh..."
    cp scripts/uninstall-offline-package.sh "${PACKAGE_DIR}/uninstall.sh"
    chmod +x "${PACKAGE_DIR}/uninstall.sh"

    # Copy management wrapper
    print_info "Copying opentr-offline.sh..."
    cp scripts/opentr-offline.sh "${PACKAGE_DIR}/opentr-offline.sh"
    chmod +x "${PACKAGE_DIR}/opentr-offline.sh"

    print_success "Installation scripts copied"
}

#######################
# DOCUMENTATION
#######################

copy_documentation() {
    print_header "Copying Documentation"

    print_info "Copying README-OFFLINE.md..."
    cp README-OFFLINE.md "${PACKAGE_DIR}/"

    print_success "Documentation copied"
}

#######################
# PACKAGE FINALIZATION
#######################

finalize_package() {
    print_header "Finalizing Package"

    # Create checksums
    create_checksums "$PACKAGE_DIR"

    # Create package info
    print_info "Creating package metadata..."
    cat > "${PACKAGE_DIR}/package-info.json" <<EOF
{
  "name": "OpenTranscribe Offline Package",
  "version": "${VERSION}",
  "created_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "created_by": "$(whoami)@$(hostname)",
  "git_commit": "$(git rev-parse HEAD)",
  "git_branch": "$(git rev-parse --abbrev-ref HEAD)",
  "docker_images": ${#IMAGES[@]},
  "package_size_uncompressed": "$(get_dir_size "$PACKAGE_DIR")"
}
EOF

    print_success "Package finalized"
}

#######################
# COMPRESSION
#######################

prompt_compression() {
    print_header "Package Compression"

    local package_size
    package_size=$(get_dir_size "$PACKAGE_DIR")

    print_info "Package is ready to compress"
    print_info "Uncompressed package size: $package_size"
    print_info "Estimated compressed size: 15-20GB"
    echo
    print_warning "Compression will take 30-60 minutes using all CPU threads"
    print_info "You can skip compression if:"
    print_info "  - Testing the package locally"
    print_info "  - Using fast local network transfer"
    print_info "  - Planning to compress manually later"
    echo

    # Prompt user for compression
    while true; do
        read -p "Do you want to compress the package now? (y/n): " -r response
        case $response in
            [Yy]* )
                return 0  # Compress
                ;;
            [Nn]* )
                return 1  # Skip compression
                ;;
            * )
                echo "Please answer y (yes) or n (no)."
                ;;
        esac
    done
}

compress_package() {
    print_header "Compressing Package"

    local output_file="${BUILD_DIR}/${PACKAGE_NAME}.tar.xz"

    print_info "Compressing with multi-threaded xz..."
    print_info "This may take 30-60 minutes..."
    print_warning "Using all available CPU threads for maximum speed"

    cd "$BUILD_DIR"

    # Use xz with all threads (-T0) for maximum speed
    # -9 for maximum compression
    # -v for verbose output
    # --keep to keep the source directory
    tar -cf - "$PACKAGE_NAME" | xz -9 -T0 -v -c > "${PACKAGE_NAME}.tar.xz"

    cd - > /dev/null

    local compressed_size
    local uncompressed_size
    compressed_size=$(get_dir_size "$output_file")
    uncompressed_size=$(get_dir_size "$PACKAGE_DIR")

    print_success "Package compressed"
    print_info "Uncompressed size: $uncompressed_size"
    print_info "Compressed size: $compressed_size"
    print_info "Location: $output_file"

    # Create checksum for the compressed package
    print_info "Creating package checksum..."
    cd "$BUILD_DIR"
    sha256sum "${PACKAGE_NAME}.tar.xz" > "${PACKAGE_NAME}.tar.xz.sha256"
    cd - > /dev/null

    print_success "Package checksum created"
}

create_uncompressed_checksum() {
    print_header "Creating Package Checksum"

    print_info "Creating checksum for uncompressed package..."
    cd "$BUILD_DIR"

    # Create a checksum file listing all files in the package
    find "$PACKAGE_NAME" -type f -exec sha256sum {} \; > "${PACKAGE_NAME}.sha256"

    cd - > /dev/null

    print_success "Checksum file created: ${BUILD_DIR}/${PACKAGE_NAME}.sha256"
}

#######################
# MAIN
#######################

main() {
    print_header "OpenTranscribe Offline Package Builder v${VERSION}"

    print_info "This script will create a complete offline installation package"
    print_info "Package name: ${PACKAGE_NAME}"
    print_info "Estimated size: 15-20GB compressed, ~60GB uncompressed"
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
    copy_installation_scripts
    copy_documentation
    finalize_package

    # Prompt for compression
    if prompt_compression; then
        # User chose to compress
        compress_package
        PACKAGE_COMPRESSED=true
    else
        # User chose to skip compression
        print_info "Skipping compression..."
        create_uncompressed_checksum
        PACKAGE_COMPRESSED=false
    fi

    # Final summary
    print_header "Build Complete!"

    echo -e "${GREEN}✅ Offline package created successfully!${NC}\n"

    echo -e "Build output structure:"
    echo -e "  ${CYAN}./offline-package-build/${NC}"
    echo -e "  ├── ${CYAN}linux/${NC}     (Linux offline package)"
    echo -e "  │   └── ${YELLOW}opentranscribe-offline-v${VERSION}/${NC}"
    echo -e "  └── ${CYAN}windows/${NC}   (Windows installer, if built)\n"

    if [ "$PACKAGE_COMPRESSED" = true ]; then
        # Compressed package summary
        echo -e "Package location:"
        echo -e "  ${CYAN}${BUILD_DIR}/${PACKAGE_NAME}.tar.xz${NC}\n"
        echo -e "Package checksum:"
        echo -e "  ${CYAN}${BUILD_DIR}/${PACKAGE_NAME}.tar.xz.sha256${NC}\n"
        echo -e "Next steps:"
        echo -e "  1. Verify: ${YELLOW}cd ${BUILD_DIR} && sha256sum -c ${PACKAGE_NAME}.tar.xz.sha256${NC}"
        echo -e "  2. Transfer package to target system"
        echo -e "  3. Extract: ${YELLOW}tar -xf ${PACKAGE_NAME}.tar.xz${NC}"
        echo -e "  4. Install: ${YELLOW}cd ${PACKAGE_NAME} && sudo ./install.sh${NC}\n"
    else
        # Uncompressed package summary
        echo -e "Package location (uncompressed):"
        echo -e "  ${CYAN}${PACKAGE_DIR}/${NC}\n"
        echo -e "Package checksum:"
        echo -e "  ${CYAN}${BUILD_DIR}/${PACKAGE_NAME}.sha256${NC}\n"
        echo -e "Next steps:"
        echo -e "  ${YELLOW}Option 1: Transfer uncompressed${NC}"
        echo -e "    1. Copy entire directory to target system"
        echo -e "    2. Install: ${YELLOW}cd ${PACKAGE_NAME} && sudo ./install.sh${NC}\n"
        echo -e "  ${YELLOW}Option 2: Compress manually later${NC}"
        echo -e "    1. ${YELLOW}cd ${BUILD_DIR}${NC}"
        echo -e "    2. Compress: ${YELLOW}tar -cf - ${PACKAGE_NAME} | xz -9 -T0 > ${PACKAGE_NAME}.tar.xz${NC}"
        echo -e "    3. Checksum: ${YELLOW}sha256sum ${PACKAGE_NAME}.tar.xz > ${PACKAGE_NAME}.tar.xz.sha256${NC}"
        echo -e "    4. Transfer and extract as usual\n"
    fi

    print_success "🎉 Build process complete!"
}

# Run main function
main
