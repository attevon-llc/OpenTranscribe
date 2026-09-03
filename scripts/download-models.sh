#!/bin/bash
# Exit on error is removed to allow graceful error handling
# set -e  # DO NOT use - we need to handle partial download failures

# OpenTranscribe Model Downloader
# Downloads all required AI models before application startup
#
# Usage: ./scripts/download-models.sh [model_cache_dir]
#
# Environment Variables:
#   WHISPER_MODEL              - Whisper model to download (default: large-v3-turbo)
#   OPENSEARCH_MODELS          - Comma-separated list of OpenSearch neural models
#                                Example: "all-MiniLM-L6-v2,all-mpnet-base-v2"
#   DOWNLOAD_ALL_OPENSEARCH_MODELS - Set to "true" to download all 6 neural models
#
# Available OpenSearch Neural Models (for semantic search):
#   Fast tier (384 dimensions):
#     - all-MiniLM-L6-v2                      (default, English, 80MB)
#     - paraphrase-multilingual-MiniLM-L12-v2 (Multilingual 50+, 420MB)
#   Balanced tier (768 dimensions):
#     - all-mpnet-base-v2                     (English, 420MB)
#     - multi-qa-MiniLM-L6-cos-v1 (Retrieval-tuned English, 80MB)
#   Best quality tier:
#     - all-distilroberta-v1                  (English, 768d, 290MB)
#     - distiluse-base-multilingual-cased-v1  (Multilingual 15, 512d, 480MB)
#
# Examples:
#   # Download default models only
#   ./scripts/download-models.sh
#
#   # Download specific OpenSearch models for multilingual support
#   OPENSEARCH_MODELS="all-MiniLM-L6-v2,paraphrase-multilingual-MiniLM-L12-v2" ./scripts/download-models.sh
#
#   # Download all models (for complete offline support)
#   DOWNLOAD_ALL_OPENSEARCH_MODELS=true ./scripts/download-models.sh

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
MODEL_CACHE_DIR="${1:-./models}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# scripts/lib/env_reader.py is a dev/CI-only helper: it lives in the repo checkout but is
# NOT in release-manifest.txt, so it never reaches a standalone `setup-opentranscribe.sh`
# install (issue #590/#581). Calling it there raised ModuleNotFoundError-adjacent failures
# silently swallowed by this script's lack of `set -e`, which degraded every .env read below
# to "" -- e.g. resolve_downloader_image() falling back to :latest, the exact regression its
# own comment says it exists to prevent. scripts/common.sh IS shipped
# (release-manifest.txt) and its read_env_value() is the grep/cut equivalent already used by
# opentranscribe.sh's shipped backup/restore arm, so use that here instead. Conditional
# source + fallback definition, identical pattern to opentranscribe.sh (~line 29): an
# install predating release-manifest.txt's common.sh entry still works, and common.sh's
# definition wins when present (bash keeps the last definition).
if [ -f "$SCRIPT_DIR/common.sh" ]; then
    # shellcheck source=scripts/common.sh
    . "$SCRIPT_DIR/common.sh"
fi
if ! declare -F read_env_value >/dev/null 2>&1; then
    read_env_value() {
        local key="$1" env_file="${2:-.env}"
        [ -f "$env_file" ] || { echo ""; return 0; }
        grep -E "^${key}=" "$env_file" 2>/dev/null \
            | head -1 \
            | cut -d= -f2- \
            | sed -E 's/[[:space:]]+#.*$//' \
            | tr -d ' "' \
            || true
    }
fi

# The image that does the downloading MUST be the version this deployment runs.
#
# This was hardcoded to `:latest`, which quietly defeats the point of a pinned
# install: a user on v0.5.0 would fetch models using whatever :latest happened to
# be that day. Model requirements change between releases (v0.5.0 adds the chat
# reranker and the redaction models), so the wrong image downloads the wrong set
# — and for an air-gapped install, "wrong set" means "missing at runtime with no
# network to recover".
#
# Resolution: explicit env > OT_IMAGE_TAG from .env (written by the installer and
# by `opentranscribe.sh update --version`) > latest.
resolve_downloader_image() {
    local tag="${OT_IMAGE_TAG:-}"
    if [ -z "$tag" ] && [ -f "$REPO_ROOT/.env" ]; then
        # read_env_value, not env_reader.py -- this script ships to end users and
        # env_reader.py does not (see the sourcing block above).
        tag=$(read_env_value OT_IMAGE_TAG "$REPO_ROOT/.env")
    fi
    # A deployment sitting in the install dir (not a git clone) keeps .env beside
    # the compose files rather than one level up.
    if [ -z "$tag" ] && [ -f "./.env" ]; then
        # read_env_value, not env_reader.py -- this script ships to end users and
        # env_reader.py does not (see the sourcing block above).
        tag=$(read_env_value OT_IMAGE_TAG ./.env)
    fi
    echo "${DOCKERHUB_USERNAME:-davidamacey}/opentranscribe-backend:${tag:-latest}"
}
DOWNLOADER_IMAGE="${DOWNLOADER_IMAGE:-$(resolve_downloader_image)}"

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

# Calculate directory size
get_dir_size() {
    du -sh "$1" 2>/dev/null | cut -f1 || echo "0"
}

# Whether the NLTK corpora are actually present, not merely that the directory is.
#
# `download-models.sh` creates `$MODEL_CACHE_DIR/nltk_data` unconditionally and the
# compose files bind-mount it, so `-d` is true on every deployment that has ever
# started — including one that has never fetched a corpus. The tokenizers
# directory is what the app actually loads (`tokenizers/punkt*`), so its emptiness
# is the honest signal.
nltk_data_present() {
    local nltk_dir="$MODEL_CACHE_DIR/nltk_data"
    [ -d "$nltk_dir/tokenizers" ] || return 1
    # `find -quit` stops at the first hit rather than walking the whole tree.
    [ -n "$(find "$nltk_dir/tokenizers" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]
}

check_models_exist() {
    print_info "Checking for existing models in $MODEL_CACHE_DIR..."

    # Check if models directory exists and has content
    if [ -d "$MODEL_CACHE_DIR/huggingface" ] && [ -d "$MODEL_CACHE_DIR/torch" ]; then
        local hf_size
        local torch_size
        hf_size=$(du -sb "$MODEL_CACHE_DIR/huggingface" 2>/dev/null | cut -f1)
        torch_size=$(du -sb "$MODEL_CACHE_DIR/torch" 2>/dev/null | cut -f1)

        # 1 GB, not the old 100 MB. The failure path below treats anything under
        # 10 GB as a PARTIAL download, so a 100 MB threshold meant a interrupted
        # download was confidently reported as "models exist" and skipped — the
        # user then hit missing weights at first transcription instead of here.
        # 1 GB is still conservative (WhisperX alone exceeds it) but no longer
        # contradicts the script's own definition of complete.
        if [ "$((hf_size + torch_size))" -gt 1000000000 ]; then
            # NLTK is checked SEPARATELY, and it has to be (issue #491). The size
            # gate above sums huggingface + torch only, so a cache holding tens of
            # gigabytes of model weights and an EMPTY nltk_data reported "models
            # exist" and skipped the download entirely — the corpora were then
            # fetched at runtime, from inside the transcription and topic
            # pipelines, which is exactly what an airgapped install cannot do.
            # nltk_data is ~50 MB against multi-GB weights, so it can never move
            # the combined threshold; only its own emptiness is observable.
            if ! nltk_data_present; then
                print_info "Model weights are present but nltk_data is empty — fetching corpora"
                return 1
            fi

            local total_size
            total_size=$(get_dir_size "$MODEL_CACHE_DIR")
            print_success "Found existing models ($total_size)"

            # Unattended runs must not block on stdin. The installer supports
            # OPENTRANSCRIBE_UNATTENDED and the release-test harness runs with no
            # TTY at all; `read` here would hang the install forever, with no
            # output explaining why. Reusing the cache is also the right default
            # for an unattended run.
            if [ -n "${OPENTRANSCRIBE_UNATTENDED:-}" ] || [ ! -t 0 ]; then
                print_info "Non-interactive — reusing the existing model cache"
                print_info "  (set FORCE_MODEL_REDOWNLOAD=1 to download again)"
                [ -n "${FORCE_MODEL_REDOWNLOAD:-}" ] || return 0
                print_info "FORCE_MODEL_REDOWNLOAD set — re-downloading"
            else
                echo -e "${YELLOW}Do you want to skip model download and use existing models? (Y/n)${NC}"
                read -n 1 -r
                echo
                if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                    print_info "Skipping model download - using existing models"
                    return 0
                else
                    print_info "Re-downloading models as requested"
                fi
            fi
        fi
    fi

    return 1
}

check_huggingface_token() {
    print_info "Checking for HuggingFace token..."

    # Check environment variable first
    if [ -n "$HUGGINGFACE_TOKEN" ]; then
        print_success "HuggingFace token found in environment"
        return 0
    fi

    # Check .env file
    if [ -f "$REPO_ROOT/.env" ]; then
        local token
        # read_env_value, not env_reader.py -- this script ships to end users and
        # env_reader.py does not (see the sourcing block above).
        token=$(read_env_value HUGGINGFACE_TOKEN "$REPO_ROOT/.env")
        if [ -n "$token" ]; then
            export HUGGINGFACE_TOKEN="$token"
            print_success "HuggingFace token loaded from .env file"
            return 0
        fi
    fi

    print_error "HUGGINGFACE_TOKEN not found!"
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  HUGGINGFACE TOKEN REQUIRED FOR SPEAKER DIARIZATION${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "${CYAN}To download PyAnnote speaker diarization models, you need:${NC}"
    echo ""
    echo "1. Create a FREE HuggingFace token:"
    echo "   • Visit: https://huggingface.co/settings/tokens"
    echo "   • Click 'New token'"
    echo "   • Give it a name (e.g., 'OpenTranscribe')"
    echo "   • Select 'Read' permissions"
    echo "   • Copy the token"
    echo ""
    echo -e "${RED}2. Accept BOTH gated model agreements (REQUIRED):${NC}"
    echo -e "   ${YELLOW}• Segmentation Model:${NC}"
    echo "     https://huggingface.co/pyannote/segmentation-3.0"
    echo -e "     ${GREEN}→ Click 'Agree and access repository'${NC}"
    echo ""
    echo -e "   ${YELLOW}• Speaker Diarization Model:${NC}"
    echo "     https://huggingface.co/pyannote/speaker-diarization-community-1"
    echo -e "     ${GREEN}→ Click 'Agree and access repository'${NC}"
    echo ""
    echo "3. Configure your token:"
    echo "   • Export it: export HUGGINGFACE_TOKEN=your_token_here"
    echo "   • Or add to .env file: HUGGINGFACE_TOKEN=your_token_here"
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    exit 1
}

download_models_docker() {
    print_header "Downloading AI Models"

    # Keep this list in step with the download_* functions in download-models.py.
    # It had drifted: two categories (the chat reranker and the redaction models)
    # were downloaded but never mentioned, and the "~2.9GB" total contradicted
    # this script's own failure path, which treats anything under 10GB as a
    # partial download.
    print_info "This will download the model set below (several GB; the exact"
    print_info "total depends on WHISPER_MODEL and DOWNLOAD_ALL_OPENSEARCH_MODELS):"
    print_info "  • WhisperX transcription models          (largest single item)"
    print_info "  • PyAnnote speaker diarization models    (gated — see below)"
    print_info "  • wav2vec2 gender classifier"
    print_info "  • NLTK tokenizers"
    print_info "  • Sentence-transformers embeddings"
    print_info "  • Chat reranker (cross-encoder, RAG chat)"
    print_info "  • OpenSearch neural search models"
    print_info "  • Content-redaction models (PII / toxicity)"
    print_info "  • Native diarizer (diar-server) ONNX/PLDA export"
    echo ""
    print_warning "This may take 10-30 minutes depending on your internet speed..."
    echo ""

    # Create model cache directories
    mkdir -p "$MODEL_CACHE_DIR/huggingface"
    mkdir -p "$MODEL_CACHE_DIR/torch"
    mkdir -p "$MODEL_CACHE_DIR/nltk_data"
    mkdir -p "$MODEL_CACHE_DIR/sentence-transformers"
    mkdir -p "$MODEL_CACHE_DIR/opensearch-ml"
    # diar-native's export lands at the top level, not under huggingface/torch like the
    # PyAnnote weights it is exported FROM — it is mounted at /models (DIAR_MODELS_DIR),
    # the same convention the backend and the diar-native sidecar both read.
    mkdir -p "$MODEL_CACHE_DIR/diar-native"

    print_info "Starting model download using Docker..."
    echo ""

    # Get Whisper model: caller's env var > .env file > default
    local whisper_model="${WHISPER_MODEL:-}"
    if [ -z "$whisper_model" ] && [ -f "$REPO_ROOT/.env" ]; then
        local env_model
        # read_env_value, not env_reader.py -- this script ships to end users and
        # env_reader.py does not (see the sourcing block above).
        env_model=$(read_env_value WHISPER_MODEL "$REPO_ROOT/.env")
        if [ -n "$env_model" ]; then
            whisper_model="$env_model"
        fi
    fi
    if [ -z "$whisper_model" ]; then
        whisper_model="large-v3-turbo"
    fi

    # CrisperWhisper: only the CTranslate2 build loads in faster-whisper. The
    # download container resolves WHISPER_MODEL through whisperx.load_model, which
    # triggers the same HF snapshot download as every other CT2 model, so the
    # weights are pre-fetched into the cache here (not just on first transcription).
    if [[ "$whisper_model" == "nyrahealth/faster_CrisperWhisper" || \
          "$whisper_model" == "crisperwhisper" ]]; then
        whisper_model="nyrahealth/faster_CrisperWhisper"
        print_info "CrisperWhisper selected (English-only, CTranslate2) — pre-downloading weights"
    elif [[ "$whisper_model" == "nyrahealth/CrisperWhisper" ]]; then
        # PyTorch checkpoint cannot be loaded by faster-whisper — remap to the CT2 build.
        whisper_model="nyrahealth/faster_CrisperWhisper"
        print_warning "nyrahealth/CrisperWhisper is a PyTorch checkpoint (not CTranslate2);"
        print_warning "using nyrahealth/faster_CrisperWhisper instead for faster-whisper compatibility"
    fi

    # Determine if GPU is available
    local use_gpu="false"
    local gpu_args=""
    if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
        use_gpu="true"
        # Use specific GPU if GPU_DEVICE_ID is set, otherwise use all GPUs.
        # Guarded with :- because nothing in this file assigns GPU_DEVICE_ID and
        # the script is run standalone (`bash scripts/download-models.sh models`).
        if [ -n "${GPU_DEVICE_ID:-}" ]; then
            gpu_args="--gpus device=${GPU_DEVICE_ID}"
            print_info "GPU detected - using GPU ${GPU_DEVICE_ID} for model initialization"
        else
            gpu_args="--gpus all"
            print_info "GPU detected - using GPU for faster model initialization"
        fi
    else
        print_info "No GPU detected - using CPU (this is fine, just slower)"
    fi

    # Run model download in Docker container with progress output
    print_info "Downloading models (progress shown below)..."
    echo ""

    # Run the download with real-time output
    # IMPORTANT: Backend runs as 'appuser' (UID 1000), so mount to /home/appuser/.cache
    # When using --gpus device=X, Docker isolates that GPU and it appears as device 0 in the container
    # Do NOT set CUDA_VISIBLE_DEVICES - PyTorch will automatically use the only available GPU
    # shellcheck disable=SC2086
    docker run --rm \
        $gpu_args \
        -e HUGGINGFACE_TOKEN="${HUGGINGFACE_TOKEN}" \
        -e WHISPER_MODEL="${whisper_model}" \
        -e USE_GPU="${use_gpu}" \
        -e COMPUTE_TYPE="${COMPUTE_TYPE:-float16}" \
        -e DIARIZATION_MODEL="${DIARIZATION_MODEL:-pyannote/speaker-diarization-community-1}" \
        -e DOWNLOAD_ALL_OPENSEARCH_MODELS="${DOWNLOAD_ALL_OPENSEARCH_MODELS:-false}" \
        -e OPENSEARCH_MODELS="${OPENSEARCH_MODELS:-}" \
        -e DOWNLOAD_REDACTION_MODELS="${DOWNLOAD_REDACTION_MODELS:-true}" \
        -v "$(realpath "$MODEL_CACHE_DIR/huggingface"):/home/appuser/.cache/huggingface" \
        -v "$(realpath "$MODEL_CACHE_DIR/torch"):/home/appuser/.cache/torch" \
        -v "$(realpath "$MODEL_CACHE_DIR/nltk_data"):/home/appuser/.cache/nltk_data" \
        -v "$(realpath "$MODEL_CACHE_DIR/sentence-transformers"):/home/appuser/.cache/sentence-transformers" \
        -v "$(realpath "$MODEL_CACHE_DIR/opensearch-ml"):/home/appuser/.cache/opensearch-ml" \
        -v "$(realpath "$MODEL_CACHE_DIR/diar-native"):/models" \
        -v "$SCRIPT_DIR/download-models.py:/app/download-models.py:ro" \
        "${DOWNLOADER_IMAGE}" \
        python /app/download-models.py

    local docker_exit_code=$?

    echo ""

    # Check if download succeeded
    if [ $docker_exit_code -eq 0 ]; then
        local total_size
        total_size=$(get_dir_size "$MODEL_CACHE_DIR")
        print_success "Models downloaded successfully ($total_size)"

        # Create marker file to indicate successful download
        date -u +"%Y-%m-%dT%H:%M:%SZ" > "$MODEL_CACHE_DIR/.download_complete"

        return 0
    else
        # Download failed - provide detailed error information
        print_error "Model download failed (exit code: $docker_exit_code)"
        echo ""

        # Check if any models were partially downloaded
        local hf_size
        local torch_size
        local partial_size
        hf_size=$(du -sb "$MODEL_CACHE_DIR/huggingface" 2>/dev/null | cut -f1 || echo "0")
        torch_size=$(du -sb "$MODEL_CACHE_DIR/torch" 2>/dev/null | cut -f1 || echo "0")
        partial_size=$(get_dir_size "$MODEL_CACHE_DIR")

        # Check if this is likely a gated model access issue
        # Expected size with all models is ~11GB, partial download ~5-6GB suggests PyAnnote models missing
        local expected_min_size=10000000000  # 10GB in bytes
        local has_pyannote_models=false

        if [ -d "$MODEL_CACHE_DIR/torch/pyannote" ]; then
            # Check if PyAnnote models exist
            if [ -d "$MODEL_CACHE_DIR/torch/pyannote/models--pyannote--segmentation-3.0" ] && \
               { [ -d "$MODEL_CACHE_DIR/torch/pyannote/models--pyannote--speaker-diarization-community-1" ] || \
                 [ -d "$MODEL_CACHE_DIR/torch/pyannote/models--pyannote--speaker-diarization-3.1" ]; }; then
                has_pyannote_models=true
            fi
        fi

        if [ "$((hf_size + torch_size))" -gt 1000000 ]; then
            print_warning "Partial download detected ($partial_size)"
            echo "Some models may have been downloaded successfully."

            if [ "$has_pyannote_models" = false ] && [ "$((hf_size + torch_size))" -lt "$expected_min_size" ]; then
                echo ""
                echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                echo -e "${RED}⚠️  MISSING PYANNOTE MODELS - LIKELY GATED ACCESS ISSUE!${NC}"
                echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                echo ""
                echo "PyAnnote speaker diarization models were NOT downloaded."
                echo "This usually means you haven't accepted the model agreements."
                echo ""
            fi

            echo "Remaining models will be downloaded on first application use."
        else
            print_error "No models were downloaded"
        fi

        echo ""
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${RED}CRITICAL: PyAnnote Models Required for Transcription Pipeline${NC}"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        echo -e "${YELLOW}⚠️  WITHOUT THESE MODELS, ALL TRANSCRIPTIONS WILL FAIL!${NC}"
        echo ""
        echo "The OpenTranscribe pipeline requires speaker diarization models."
        echo "Without them, the entire transcription process cannot complete."
        echo ""
        echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
        echo -e "${CYAN}REQUIRED ACTION: Accept BOTH Gated Model Agreements${NC}"
        echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
        echo ""
        echo "You MUST accept BOTH of these model agreements on HuggingFace:"
        echo ""
        echo "  1. Segmentation Model:"
        echo "     https://huggingface.co/pyannote/segmentation-3.0"
        echo -e "     ${GREEN}→ Click 'Agree and access repository'${NC}"
        echo ""
        echo "  2. Speaker Diarization Model:"
        echo "     https://huggingface.co/pyannote/speaker-diarization-community-1"
        echo -e "     ${GREEN}→ Click 'Agree and access repository'${NC}"
        echo ""
        echo -e "${CYAN}After accepting BOTH agreements:${NC}"
        echo "  • Wait 1-2 minutes for permissions to propagate"
        echo "  • Run this script again: bash scripts/download-models.sh models"
        echo ""
        echo -e "${YELLOW}Other possible causes (less common):${NC}"
        echo "  • Network connectivity issues"
        echo "  • Invalid HuggingFace token (verify 'Read' permissions)"
        echo "  • Docker image not available"
        echo ""
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""

        return 1
    fi
}

show_summary() {
    print_header "Model Download Summary"

    local total_size
    local hf_size
    local torch_size
    local nltk_size
    local st_size
    local opensearch_size
    local diar_native_size
    total_size=$(get_dir_size "$MODEL_CACHE_DIR")
    hf_size=$(get_dir_size "$MODEL_CACHE_DIR/huggingface")
    torch_size=$(get_dir_size "$MODEL_CACHE_DIR/torch")
    nltk_size=$(get_dir_size "$MODEL_CACHE_DIR/nltk_data")
    st_size=$(get_dir_size "$MODEL_CACHE_DIR/sentence-transformers")
    opensearch_size=$(get_dir_size "$MODEL_CACHE_DIR/opensearch-ml")
    diar_native_size=$(get_dir_size "$MODEL_CACHE_DIR/diar-native")

    echo -e "${GREEN}✅ Model cache ready!${NC}"
    echo ""
    echo "Cache location: $MODEL_CACHE_DIR"
    echo "Total size: $total_size"
    echo "  • HuggingFace models: $hf_size"
    echo "  • Torch models: $torch_size"
    echo "  • NLTK data: $nltk_size"
    echo "  • Sentence-transformers: $st_size"
    echo "  • OpenSearch neural models: $opensearch_size"
    echo "  • Native diarizer (diar-server) export: $diar_native_size"
    echo ""
    print_info "Models are cached and will be available immediately when Docker starts"
    echo ""
}

#######################
# MAIN
#######################

main() {
    print_header "OpenTranscribe Model Downloader"

    print_info "Model cache directory: $MODEL_CACHE_DIR"
    echo ""

    # Check if models already exist
    if check_models_exist; then
        show_summary
        exit 0
    fi

    # Check for HuggingFace token
    check_huggingface_token

    # Download models using Docker (recommended)
    if download_models_docker; then
        show_summary
        exit 0
    else
        print_error "Model download failed"
        echo ""
        print_info "Models will be downloaded automatically when you first run the application,"
        print_info "but this will cause a delay on first use."
        echo ""
        exit 1
    fi
}

# Run main function
main
