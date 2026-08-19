#!/bin/bash
# Shared functions for OpenTranscribe offline/Windows build scripts
# Sourced by: build-offline-package.sh, build-windows-installer.sh

#######################
# COLORS
#######################

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

#######################
# HELPER FUNCTIONS
#######################

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

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Calculate directory size
get_dir_size() {
    du -sh "$1" 2>/dev/null | cut -f1 || echo "0"
}

# Create checksums
create_checksums() {
    local dir=$1
    print_info "Creating checksums for verification..."

    (
        cd "$dir" || return 1
        find . -type f ! -name "checksums.sha256" -exec sha256sum {} \; > checksums.sha256
    )

    print_success "Checksums created"
}

#######################
# MODEL SELECTION
#######################

select_whisper_model_for_offline() {
    print_header "Whisper Model Selection"

    # Detect GPU if available for recommendation
    local RECOMMENDED_MODEL="large-v3-turbo"
    local RECOMMENDATION_REASON="6x faster than large-v3, excellent accuracy - recommended for offline deployments"
    local GPU_MEMORY=""

    if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
        GPU_MEMORY=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i "${GPU_DEVICE_ID:-0}" 2>/dev/null)
        if [ -n "$GPU_MEMORY" ]; then
            if [[ $GPU_MEMORY -gt 16000 ]]; then
                RECOMMENDED_MODEL="large-v3-turbo"
                RECOMMENDATION_REASON="High-end GPU detected (${GPU_MEMORY}MB VRAM) - 6x faster than large-v3, excellent accuracy"
            elif [[ $GPU_MEMORY -gt 8000 ]]; then
                RECOMMENDED_MODEL="large-v3-turbo"
                RECOMMENDATION_REASON="Mid-range GPU detected (${GPU_MEMORY}MB VRAM) - 6x faster than large-v3, excellent accuracy"
            elif [[ $GPU_MEMORY -gt 4000 ]]; then
                RECOMMENDED_MODEL="medium"
                RECOMMENDATION_REASON="Entry-level GPU detected (${GPU_MEMORY}MB VRAM)"
            else
                RECOMMENDED_MODEL="small"
                RECOMMENDATION_REASON="Low-memory GPU detected (${GPU_MEMORY}MB VRAM)"
            fi
        fi
    fi

    echo -e "${BLUE}Available Whisper Models:${NC}"
    echo ""
    echo "  Model            VRAM     English    Multilingual  Translate  Notes"
    echo "  ──────────────────────────────────────────────────────────────────────────────"
    echo "  tiny              ~1GB     Low        Low           Yes        Fastest, lowest accuracy"
    echo "  base              ~1GB     Fair       Fair          Yes        Very fast"
    echo "  small             ~2GB     Good       Good          Yes        Good balance for CPU"
    echo "  medium            ~5GB     Better     Better        Yes        Good balance for GPU"
    echo "  large-v2          ~10GB    Excellent  Good          Yes        Legacy, supports translation"
    echo "  large-v3          ~10GB    Excellent  Best          Yes        Best for non-English & translation"
    echo "  large-v3-turbo    ~6GB     Excellent  Good*         No         6x faster (recommended)"
    echo ""
    echo -e "${GREEN}Recommendation: ${RECOMMENDED_MODEL}${NC}"
    echo "  Reason: ${RECOMMENDATION_REASON}"
    echo ""
    echo -e "${YELLOW}IMPORTANT for Offline Deployments:${NC}"
    echo "  • Choose based on the GPU that will be used on the target system"
    echo "  • The selected model will be included in the offline package"
    echo "  • You cannot download a different model after deployment without internet"
    echo "  • Consider the target system's GPU memory, not your current build system"
    echo ""

    # Check if WHISPER_MODEL is already set in environment
    if [ -n "$WHISPER_MODEL" ]; then
        print_info "WHISPER_MODEL already set to: $WHISPER_MODEL"
        read -p "Use this model or select a different one? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            print_success "Using model: $WHISPER_MODEL"
            return 0
        fi
    fi

    # Prompt user for model selection
    while true; do
        read -r -p "Select model for offline package (tiny/base/small/medium/large-v2/large-v3/large-v3-turbo) [${RECOMMENDED_MODEL}]: " user_model

        # Use recommended if user just presses Enter
        if [ -z "$user_model" ]; then
            WHISPER_MODEL="$RECOMMENDED_MODEL"
            break
        fi

        # Validate input
        case "$user_model" in
            tiny|base|small|medium|large-v1|large-v2|large-v3|large-v3-turbo)
                WHISPER_MODEL="$user_model"
                break
                ;;
            *)
                print_error "Invalid model. Please choose: tiny, base, small, medium, large-v2, large-v3, or large-v3-turbo"
                ;;
        esac
    done

    echo ""
    print_success "Selected model for offline package: ${WHISPER_MODEL}"
    if [ "$WHISPER_MODEL" != "$RECOMMENDED_MODEL" ]; then
        print_warning "Note: You selected a different model than recommended"
    fi

    # Export for use in download_models
    export WHISPER_MODEL
    echo ""
}

select_opensearch_models_for_offline() {
    print_header "OpenSearch Neural Model Selection"

    echo -e "${BLUE}Available OpenSearch Neural Models for Semantic Search:${NC}"
    echo ""
    echo "  Model                                          Tier       Languages      Size"
    echo "  ─────────────────────────────────────────────────────────────────────────────"
    echo "  all-MiniLM-L6-v2 (default)                    Fast       English        80MB"
    echo "  paraphrase-multilingual-MiniLM-L12-v2         Fast       50+ languages  420MB"
    echo "  all-mpnet-base-v2                             Balanced   English        420MB"
    echo "  multi-qa-MiniLM-L6-cos-v1                     Fast       English        80MB"
    echo "  all-distilroberta-v1                          Best       English        290MB"
    echo "  distiluse-base-multilingual-cased-v1          Best       15 languages   480MB"
    echo ""
    echo -e "${GREEN}Recommendation: all-MiniLM-L6-v2${NC} (default)"
    echo "  Reason: Fast, lightweight, good for most use cases"
    echo ""
    echo -e "${YELLOW}IMPORTANT for Offline Deployments:${NC}"
    echo "  • You can select multiple models (comma-separated) to enable model switching"
    echo "  • Models cannot be downloaded after deployment without internet"
    echo "  • For multilingual content, add a multilingual model"
    echo ""

    # Check if OPENSEARCH_MODELS is already set
    if [ -n "$OPENSEARCH_MODELS" ]; then
        print_info "OPENSEARCH_MODELS already set to: $OPENSEARCH_MODELS"
        read -p "Use these models or select different ones? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            print_success "Using models: $OPENSEARCH_MODELS"
            return 0
        fi
    fi

    # Prompt user for model selection
    echo "Enter model names (comma-separated) or choose an option:"
    echo "  1. Default only (all-MiniLM-L6-v2) - 80MB"
    echo "  2. English suite (default + best quality) - 370MB"
    echo "  3. Multilingual suite (default + multilingual) - 500MB"
    echo "  4. Complete suite (all 6 models) - ~2.8GB"
    echo "  5. Custom selection (enter model names)"
    echo ""

    while true; do
        read -r -p "Select option [1-5] (default: 1): " user_choice

        # Use default if user just presses Enter
        if [ -z "$user_choice" ]; then
            OPENSEARCH_MODELS="all-MiniLM-L6-v2"
            break
        fi

        case "$user_choice" in
            1)
                OPENSEARCH_MODELS="all-MiniLM-L6-v2"
                break
                ;;
            2)
                OPENSEARCH_MODELS="all-MiniLM-L6-v2,all-distilroberta-v1"
                break
                ;;
            3)
                OPENSEARCH_MODELS="all-MiniLM-L6-v2,paraphrase-multilingual-MiniLM-L12-v2"
                break
                ;;
            4)
                export DOWNLOAD_ALL_OPENSEARCH_MODELS="true"
                OPENSEARCH_MODELS=""
                print_success "Downloading all 6 OpenSearch models"
                return 0
                ;;
            5)
                echo ""
                echo "Available model short names:"
                echo "  all-MiniLM-L6-v2, paraphrase-multilingual-MiniLM-L12-v2,"
                echo "  all-mpnet-base-v2, multi-qa-MiniLM-L6-cos-v1,"
                echo "  all-distilroberta-v1, distiluse-base-multilingual-cased-v1"
                echo ""
                read -r -p "Enter model names (comma-separated): " custom_models
                if [ -n "$custom_models" ]; then
                    OPENSEARCH_MODELS="$custom_models"
                    break
                else
                    print_error "No models specified. Please try again."
                fi
                ;;
            *)
                print_error "Invalid option. Please choose 1-5."
                ;;
        esac
    done

    echo ""
    print_success "Selected OpenSearch models: ${OPENSEARCH_MODELS}"
    export OPENSEARCH_MODELS
    echo ""
}

select_gpu_device_for_build() {
    print_header "GPU Selection for Model Downloads"

    # Check if nvidia-smi is available
    if ! command_exists nvidia-smi; then
        print_warning "nvidia-smi not found - GPU not available"
        print_info "Model downloads will use CPU mode (slower)"
        export GPU_DEVICE_ID=""
        return 0
    fi

    # Check if any GPUs are detected
    local gpu_count
    gpu_count=$(nvidia-smi --query-gpu=index --format=csv,noheader,nounits 2>/dev/null | wc -l)

    if [ "$gpu_count" -eq 0 ]; then
        print_warning "No NVIDIA GPUs detected"
        print_info "Model downloads will use CPU mode (slower)"
        export GPU_DEVICE_ID=""
        return 0
    fi

    # Single GPU - use it automatically
    if [ "$gpu_count" -eq 1 ]; then
        export GPU_DEVICE_ID="0"
        local gpu_name
        local gpu_memory
        gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader,nounits -i 0 2>/dev/null)
        gpu_memory=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i 0 2>/dev/null)
        print_success "Using GPU 0: ${gpu_name} (${gpu_memory}MB)"
        return 0
    fi

    # Multiple GPUs - prompt user to select
    echo
    print_info "Multiple GPUs Detected: $gpu_count GPUs available"
    echo
    print_info "Available GPUs:"
    nvidia-smi --query-gpu=index,name,memory.total,memory.free --format=csv,noheader | while IFS=, read -r idx name mem_total mem_free; do
        echo "  [${idx}] ${name} (${mem_total} total, ${mem_free} free)"
    done

    echo
    print_info "Select which GPU to use for model downloads"
    print_warning "Choose a GPU that is not heavily used by other tasks"
    echo

    local selected_gpu="0"

    # Prompt user for GPU selection
    while true; do
        read -r -p "Enter GPU index to use [0-$((gpu_count-1))] (default: 0): " user_input

        # Use default if empty
        if [ -z "$user_input" ]; then
            selected_gpu="0"
            break
        fi

        # Validate input is a number
        if ! [[ "$user_input" =~ ^[0-9]+$ ]]; then
            print_error "Invalid input. Please enter a number."
            continue
        fi

        # Validate GPU index is within range
        if [ "$user_input" -ge 0 ] && [ "$user_input" -lt "$gpu_count" ]; then
            selected_gpu="$user_input"
            break
        else
            print_error "Invalid GPU index. Please enter a number between 0 and $((gpu_count-1))."
        fi
    done

    # Export GPU_DEVICE_ID for use in download_models
    export GPU_DEVICE_ID="$selected_gpu"

    # Get selected GPU details
    local gpu_name
    local gpu_memory
    gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader,nounits -i "$GPU_DEVICE_ID" 2>/dev/null)
    gpu_memory=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i "$GPU_DEVICE_ID" 2>/dev/null)

    echo
    print_success "Selected GPU ${GPU_DEVICE_ID}: ${gpu_name} (${gpu_memory}MB)"
    echo
}

#######################
# IMAGE HANDLING
#######################

pull_and_save_images() {
    print_header "Pulling and Saving Docker Images"

    local total_images=${#IMAGES[@]}
    local current=0

    for image in "${IMAGES[@]}"; do
        current=$((current + 1))
        local image_file
        image_file=$(echo "$image" | tr '/:' '__')
        local output_path="${PACKAGE_DIR}/docker-images/${image_file}.tar"

        print_info "[$current/$total_images] Processing: $image"

        # Pull image from Docker Hub (unless --local flag is set)
        if [ "$USE_LOCAL_IMAGES" = true ]; then
            print_info "  Using local image (skipping pull)..."
            if ! docker image inspect "$image" &>/dev/null; then
                print_error "  Local image not found: $image"
                print_info "  Please build the image first with: docker compose build"
                exit 1
            fi
        else
            print_info "  Pulling image from Docker Hub..."
            if ! docker pull "$image"; then
                print_error "  Failed to pull $image"
                exit 1
            fi
        fi

        # Save image
        print_info "  Saving image to tar..."
        if ! docker save "$image" -o "$output_path"; then
            print_error "  Failed to save $image"
            exit 1
        fi

        local size
        size=$(get_dir_size "$output_path")
        print_success "  Saved ($size)"
    done

    # Create image metadata
    print_info "Creating image metadata..."
    cat > "${PACKAGE_DIR}/docker-images/metadata.json" <<EOF
{
  "created_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "version": "${VERSION}",
  "images": [
$(for img in "${IMAGES[@]}"; do
    echo "    \"$img\","
done | sed '$ s/,$//')
  ]
}
EOF

    print_success "All Docker images saved"
}
