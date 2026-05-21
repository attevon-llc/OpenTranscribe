#!/bin/bash
# OpenTranscribe Cross-Platform Setup Script
#
# Environment variables:
#   OPENTRANSCRIBE_BRANCH      Git branch to download files from (default: master)
#   OPENTRANSCRIBE_UNATTENDED  When non-empty, skip all interactive prompts and
#                              use safe defaults or pre-set environment variables
#                              (used by CI and release-test harnesses)
#   OPENTRANSCRIBE_FORCE_CPU   When non-empty, skip NVIDIA/GPU detection entirely
#                              and install in CPU-only mode. Equivalent to passing
#                              --cpu on the command line. Useful on hosts with the
#                              NVIDIA Container Toolkit installed but no working
#                              GPU passthrough (e.g. WSL2 without a supported
#                              Windows NVIDIA driver), where auto-detection would
#                              otherwise enable a GPU overlay that fails at
#                              container start with an nvidia-container-cli
#                              adapter error.
#
# Env vars honored in unattended mode (all optional; any can be pre-set):
#   PROJECT_DIR                Where to install (default: ./opentranscribe)
#   HUGGINGFACE_TOKEN          PyAnnote HuggingFace token
#   WHISPER_MODEL              Whisper model id (e.g. large-v3-turbo)
#   OPENSEARCH_MODELS          OpenSearch embedding model id
#   GPU_DEVICE_ID              CUDA device index to pin (default: 0)
#   LLM_PROVIDER               vllm|openai|ollama|anthropic|openrouter (default: vllm, no-op)
#   NGINX_SERVER_NAME          If set, enables HTTPS/NGINX setup; else skipped
#   VLLM_BASE_URL, VLLM_API_KEY, VLLM_MODEL_NAME
#   OPENAI_API_KEY, OPENAI_MODEL_NAME
#   OLLAMA_BASE_URL, OLLAMA_MODEL_NAME
#   ANTHROPIC_API_KEY, ANTHROPIC_MODEL_NAME
#   OPENROUTER_API_KEY, OPENROUTER_MODEL_NAME

set -e

YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ─── Unattended-mode helpers ─────────────────────────────────────────────────
# When OPENTRANSCRIBE_UNATTENDED is set, interactive prompts are skipped and
# pre-set environment variables (or safe defaults) are used instead.
is_unattended() {
    [[ -n "${OPENTRANSCRIBE_UNATTENDED:-}" ]]
}

# ot_log_unattended: emit a log line when a prompt is skipped in unattended mode
ot_log_unattended() {
    echo -e "${BLUE}[unattended]${NC} $*"
}

# Helper functions for colored output
print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

print_header() {
    echo ""
    echo -e "${BLUE}═══════════════════════════════════════════${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════${NC}"
    echo ""
}

echo -e "${YELLOW}OpenTranscribe Cross-Platform Setup Script${NC}"
echo "Automatic hardware detection and configuration for CUDA, MPS, and CPU"
echo ""

# Global variables
PROJECT_DIR="opentranscribe"
DETECTED_PLATFORM=""
DETECTED_DEVICE=""
COMPUTE_TYPE=""
BATCH_SIZE=""
DOCKER_RUNTIME=""
USE_GPU_RUNTIME="false"
# FORCE_CPU: when "true", skip NVIDIA/GPU detection entirely and install in
# CPU-only mode. Set via --cpu flag or OPENTRANSCRIBE_FORCE_CPU=1 env var.
# This is a supported manual opt-out for hosts where GPU auto-detection
# produces a false positive (e.g. WSL2 with toolkit installed but no working
# adapter passthrough), or where the user simply wants to exclude GPU
# workloads. Default (unset) preserves the original auto-detect behaviour.
FORCE_CPU="false"
if [[ -n "${OPENTRANSCRIBE_FORCE_CPU:-}" ]]; then
    FORCE_CPU="true"
fi

# CPU-friendly diarization default written into .env when
# DETECTED_DEVICE=cpu (either forced via --cpu or auto-detected on a host
# with no GPU). PyAnnote diarization requires CUDA — running it on CPU
# would either fail or be unusably slow — so we default it to off at
# install time. The Whisper model recommendation is handled separately by
# select_whisper_model() which already proposes "base" for CPU. Users can
# edit .env afterwards to override either choice.
ENABLE_DIARIZATION_CPU_DEFAULT="false"

#######################
# HARDWARE DETECTION
#######################

detect_platform() {
    echo -e "${BLUE}🔍 Detecting platform and hardware...${NC}"

    # Detect OS and Architecture
    DETECTED_PLATFORM=$(uname -s | tr '[:upper:]' '[:lower:]')
    ARCH=$(uname -m)

    case "$DETECTED_PLATFORM" in
        "linux")
            echo "✓ Detected: Linux ($ARCH)"
            ;;
        "darwin")
            DETECTED_PLATFORM="macos"
            echo "✓ Detected: macOS ($ARCH)"
            ;;
        "mingw"*|"msys"*|"cygwin"*)
            DETECTED_PLATFORM="windows"
            echo "✓ Detected: Windows ($ARCH)"
            ;;
        *)
            echo "⚠️  Unknown platform: $DETECTED_PLATFORM ($ARCH)"
            ;;
    esac

    # Detect hardware acceleration
    detect_hardware_acceleration
}

detect_hardware_acceleration() {
    DETECTED_DEVICE="cpu"  # Default fallback

    # CPU-only mode requested explicitly (--cpu flag or OPENTRANSCRIBE_FORCE_CPU):
    # skip all GPU probing so we don't touch nvidia-smi or docker's NVIDIA
    # runtime. This also prevents a later false-positive overlay being layered
    # in at runtime by opentranscribe.sh, because we write USE_GPU=false into
    # .env below.
    if [[ "$FORCE_CPU" == "true" ]]; then
        echo "ℹ️  CPU-only mode requested (--cpu / OPENTRANSCRIBE_FORCE_CPU set)"
        echo "    Skipping NVIDIA GPU detection."
        DETECTED_DEVICE="cpu"
        COMPUTE_TYPE="int8"
        BATCH_SIZE="4"
        USE_GPU_RUNTIME="false"

        if command -v nproc &> /dev/null; then
            CPU_CORES=$(nproc)
        elif command -v sysctl &> /dev/null; then
            CPU_CORES=$(sysctl -n hw.ncpu)
        else
            CPU_CORES=4
        fi
        echo "✓ Detected $CPU_CORES CPU cores"
        return
    fi

    # Check for NVIDIA GPU (CUDA)
    if command -v nvidia-smi &> /dev/null; then
        if nvidia-smi &> /dev/null; then
            echo "✓ NVIDIA GPU detected"
            nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader
            DETECTED_DEVICE="cuda"
            COMPUTE_TYPE="float16"
            BATCH_SIZE="16"
            USE_GPU_RUNTIME="true"

            # Get count of available GPUs
            GPU_COUNT=$(nvidia-smi --query-gpu=index --format=csv,noheader,nounits | wc -l)

            # Get default GPU (first available)
            DEFAULT_GPU=$(nvidia-smi --query-gpu=index --format=csv,noheader,nounits | head -n1)
            GPU_DEVICE_ID=${DEFAULT_GPU:-0}

            return
        fi
    fi

    # Check for Apple Silicon (MPS)
    if [[ "$DETECTED_PLATFORM" == "macos" ]]; then
        # Check for Apple Silicon
        if [[ $(uname -m) == "arm64" ]]; then
            echo "✓ Apple Silicon detected (M1/M2)"
            DETECTED_DEVICE="mps"
            COMPUTE_TYPE="float32"
            BATCH_SIZE="8"

            # Check macOS version for MPS support (requires macOS 12.3+)
            macos_version=$(sw_vers -productVersion)
            if [[ $(echo "$macos_version" | cut -d. -f1) -ge 12 ]] && [[ $(echo "$macos_version" | cut -d. -f2) -ge 3 ]]; then
                echo "✓ macOS $macos_version supports MPS acceleration"
            else
                echo "⚠️  macOS $macos_version detected, MPS requires 12.3+, falling back to CPU"
                DETECTED_DEVICE="cpu"
            fi

            return
        else
            echo "✓ Intel Mac detected"
        fi
    fi

    # CPU fallback
    echo "ℹ️  Using CPU processing (no GPU acceleration detected)"
    DETECTED_DEVICE="cpu"
    COMPUTE_TYPE="int8"
    BATCH_SIZE="4"

    # Detect CPU cores for optimization
    if command -v nproc &> /dev/null; then
        CPU_CORES=$(nproc)
    elif command -v sysctl &> /dev/null; then
        CPU_CORES=$(sysctl -n hw.ncpu)
    else
        CPU_CORES=4
    fi
    echo "✓ Detected $CPU_CORES CPU cores"
}

#######################
# DOCKER CONFIGURATION
#######################

check_gpu_support() {
    # Check for NVIDIA GPUs
    if command -v nvidia-smi &> /dev/null; then
        echo "✅ NVIDIA GPU detected:"
        nvidia-smi --query-gpu=name --format=csv,noheader
    else
        echo "❌ No NVIDIA GPU detected or nvidia-smi not found"
        return 1
    fi

    # Check for NVIDIA container runtime
    if docker info 2>/dev/null | grep -q "nvidia"; then
        echo "✅ NVIDIA Container Runtime is properly configured"
        return 0
    else
        echo "❌ NVIDIA Container Runtime is not properly configured"
        return 1
    fi
}

configure_docker_runtime() {
    echo -e "${BLUE}🐳 Configuring Docker runtime...${NC}"

    if [[ "$USE_GPU_RUNTIME" == "true" && "$DETECTED_DEVICE" == "cuda" ]]; then
        echo "🧪 Testing NVIDIA Container Toolkit..."

        if check_gpu_support; then
            echo -e "${GREEN}✅ NVIDIA Container Toolkit fully functional${NC}"
            DOCKER_RUNTIME="nvidia"
        else
            echo -e "${RED}❌ NVIDIA Container Toolkit tests failed${NC}"
            echo ""
            echo "Possible solutions:"
            echo "1. Install NVIDIA Container Toolkit:"
            echo "   https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
            echo "2. Restart Docker daemon after installation"
            echo "3. Check NVIDIA driver installation with: nvidia-smi"
            echo ""
            echo -e "${YELLOW}⚠️  Automatically falling back to CPU mode...${NC}"
            fallback_to_cpu
        fi
    else
        DOCKER_RUNTIME="default"
        echo "✓ Using default Docker runtime"
    fi
}

fallback_to_cpu() {
    DETECTED_DEVICE="cpu"
    COMPUTE_TYPE="int8"
    BATCH_SIZE="4"
    USE_GPU_RUNTIME="false"
    DOCKER_RUNTIME="default"
}

#######################
# NETWORK AND DEPENDENCY CHECKS
#######################

check_network_connectivity() {
    echo -e "${BLUE}🌐 Checking network connectivity...${NC}"

    # Test GitHub connectivity
    if ! curl -s --connect-timeout 5 --max-time 10 https://raw.githubusercontent.com > /dev/null 2>&1; then
        echo -e "${YELLOW}⚠️  GitHub may not be accessible for downloading files${NC}"
        echo "This could affect the setup process. Please check your internet connection."
        echo ""
        if is_unattended; then
            ot_log_unattended "Aborting on network check failure (unattended mode cannot continue without GitHub)."
            exit 1
        fi
        read -p "Do you want to continue anyway? (y/N) " -n 1 -r </dev/tty
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Setup cancelled by user."
            exit 1
        fi
    else
        echo "✓ Network connectivity verified"
    fi
}

validate_downloaded_files() {
    echo -e "${BLUE}🔍 Validating downloaded files...${NC}"

    # Note: Database schema is managed by Alembic migrations on backend startup.
    # No init_db.sql validation needed.

    # Validate docker-compose files exist
    if [ ! -f "docker-compose.yml" ]; then
        echo -e "${RED}❌ docker-compose.yml file not found${NC}"
        return 1
    fi

    if [ ! -f "docker-compose.prod.yml" ]; then
        echo -e "${RED}❌ docker-compose.prod.yml file not found${NC}"
        return 1
    fi

    # Check for essential services in base file
    if ! grep -q "services:" docker-compose.yml; then
        echo -e "${RED}❌ docker-compose.yml appears invalid (no 'services:' section)${NC}"
        return 1
    fi

    if ! grep -q "backend:" docker-compose.yml || ! grep -q "frontend:" docker-compose.yml; then
        echo -e "${RED}❌ docker-compose.yml missing essential services${NC}"
        return 1
    fi

    echo "✓ docker-compose.yml and docker-compose.prod.yml validated"
    echo "  (Full configuration validation will occur after .env file creation)"
    echo "✓ All downloaded files validated successfully"
    return 0
}

check_dependencies() {
    echo -e "${BLUE}📋 Checking dependencies...${NC}"

    # Check for curl
    if ! command -v curl &> /dev/null; then
        echo -e "${RED}❌ curl is not installed${NC}"
        echo "curl is required to download configuration files."
        echo "Please install curl and try again."
        exit 1
    else
        echo "✓ curl detected"
    fi

    # Check for Docker
    if ! command -v docker &> /dev/null; then
        echo -e "${RED}❌ Docker is not installed${NC}"
        echo "Visit https://docs.docker.com/get-docker/ for installation instructions."
        exit 1
    else
        docker_version=$(docker --version | cut -d' ' -f3 | cut -d',' -f1)
        echo "✓ Docker $docker_version detected"
    fi

    # Check for Docker Compose
    if ! docker compose version &> /dev/null; then
        echo -e "${RED}❌ Docker Compose is not installed or not in PATH${NC}"
        echo "Visit https://docs.docker.com/compose/install/ for installation instructions."
        exit 1
    else
        compose_version=$(docker compose version --short)
        echo "✓ Docker Compose $compose_version detected"
    fi

    # Check if Docker daemon is running
    local docker_check_output
    docker_check_output=$(docker info 2>&1)
    if [ $? -ne 0 ]; then
        if echo "$docker_check_output" | grep -qi "permission denied"; then
            echo -e "${RED}❌ Permission denied accessing Docker.${NC}"
            echo ""
            echo "Your user ($USER) is not in the 'docker' group."
            echo "Run:  sudo usermod -aG docker \$USER  then log out and back in."
            echo "Or re-run this script with sudo."
        elif echo "$docker_check_output" | grep -qi "cannot connect\|is the docker daemon running\|no such file"; then
            echo -e "${RED}❌ Docker daemon is not running.${NC}"
            echo "Run:  sudo systemctl start docker"
            echo "To start on boot:  sudo systemctl enable docker"
        else
            echo -e "${RED}❌ Failed to connect to Docker.${NC}"
            echo "$docker_check_output"
        fi
        exit 1
    else
        echo "✓ Docker daemon is running"
    fi

    # Check network connectivity
    check_network_connectivity
}

#######################
# CONFIGURATION SETUP
#######################

setup_project_directory() {
    echo -e "${BLUE}📁 Setting up project directory...${NC}"

    # Create and enter project directory
    mkdir -p "$PROJECT_DIR"
    cd "$PROJECT_DIR"
    # Resolve to absolute path so later cd calls work regardless of cwd changes
    PROJECT_DIR="$(pwd)"
    echo "✓ Created project directory: $PROJECT_DIR"
}

create_database_files() {
    echo "✓ Downloading database initialization files..."

    # Create database directory
    mkdir -p database

    # Note: Database schema is managed by Alembic migrations - no init_db.sql download needed
}

create_configuration_files() {
    echo -e "${BLUE}📄 Creating configuration files...${NC}"

    # Create database initialization files
    create_database_files

    # Create comprehensive docker-compose.yml directly
    create_production_compose

    # Validate all downloaded files
    if ! validate_downloaded_files; then
        echo -e "${RED}❌ File validation failed${NC}"
        exit 1
    fi

    # Download opentranscribe.sh management script
    download_management_script

    # Download NGINX/SSL configuration files
    download_nginx_files

    # Download model downloader scripts
    download_model_downloader_scripts

    # Create .env.example
    create_production_env_example
}

create_production_compose() {
    echo "✓ Downloading production docker-compose configuration..."

    local max_retries=3
    local branch="${OPENTRANSCRIBE_BRANCH:-master}"
    # URL-encode the branch name (replace / with %2F)
    local encoded_branch
    encoded_branch=$(echo "$branch" | sed 's|/|%2F|g')

    # Download base docker-compose.yml
    echo "  Downloading base docker-compose.yml..."
    local retry_count=0
    local base_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/docker-compose.yml"

    while [ $retry_count -lt $max_retries ]; do
        if curl -fsSL --connect-timeout 10 --max-time 30 "$base_url" -o docker-compose.yml; then
            if [ -s docker-compose.yml ] && grep -q "services:" docker-compose.yml; then
                echo "  ✓ Downloaded base docker-compose.yml"
                break
            else
                echo "  ⚠️  Downloaded base file appears invalid, retrying..."
                rm -f docker-compose.yml
            fi
        else
            echo "  ⚠️  Download attempt $((retry_count + 1)) failed"
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            sleep 2
        fi
    done

    if [ $retry_count -ge $max_retries ]; then
        echo -e "${RED}❌ Failed to download base docker-compose.yml${NC}"
        echo "Please check your internet connection and try again."
        echo "Alternative: You can manually download from: $base_url"
        exit 1
    fi

    # Download production overrides docker-compose.prod.yml
    echo "  Downloading production overrides docker-compose.prod.yml..."
    retry_count=0
    local prod_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/docker-compose.prod.yml"

    while [ $retry_count -lt $max_retries ]; do
        if curl -fsSL --connect-timeout 10 --max-time 30 "$prod_url" -o docker-compose.prod.yml; then
            if [ -s docker-compose.prod.yml ] && grep -q "services:" docker-compose.prod.yml; then
                echo "  ✓ Downloaded production docker-compose.prod.yml"

                # Download GPU overlay for NVIDIA acceleration (non-fatal)
                download_gpu_overlay

                # Download optional gpu-scale overlay (non-fatal)
                download_gpu_scale_overlay

                echo "✓ Production docker-compose configuration complete"
                return 0
            else
                echo "  ⚠️  Downloaded prod file appears invalid, retrying..."
                rm -f docker-compose.prod.yml
            fi
        else
            echo "  ⚠️  Download attempt $((retry_count + 1)) failed"
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            sleep 2
        fi
    done

    echo -e "${RED}❌ Failed to download docker-compose.prod.yml${NC}"
    echo "Please check your internet connection and try again."
    echo "Alternative: You can manually download from: $prod_url"
    exit 1
}

download_gpu_overlay() {
    # Download docker-compose.gpu.yml for NVIDIA GPU support
    # This enables GPU acceleration when NVIDIA Container Toolkit is detected
    echo "  Downloading docker-compose.gpu.yml (GPU acceleration support)..."

    local branch="${OPENTRANSCRIBE_BRANCH:-master}"
    local encoded_branch
    encoded_branch=$(echo "$branch" | sed 's|/|%2F|g')
    local gpu_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/docker-compose.gpu.yml"

    if curl -fsSL --connect-timeout 10 --max-time 30 "$gpu_url" -o docker-compose.gpu.yml 2>/dev/null; then
        if [ -s docker-compose.gpu.yml ] && grep -q "celery-worker:" docker-compose.gpu.yml; then
            echo "  ✓ Downloaded docker-compose.gpu.yml (GPU acceleration)"
        else
            echo "  ⚠️  Downloaded gpu file appears invalid, removing..."
            rm -f docker-compose.gpu.yml
        fi
    else
        echo "  ℹ️  docker-compose.gpu.yml not available (GPU support optional)"
    fi
}

download_gpu_scale_overlay() {
    # Optional: Download docker-compose.gpu-scale.yml for multi-GPU support
    # This is non-fatal - users can skip if they don't have multi-GPU setups
    echo "  Downloading optional docker-compose.gpu-scale.yml (multi-GPU support)..."

    local branch="${OPENTRANSCRIBE_BRANCH:-master}"
    local encoded_branch
    encoded_branch=$(echo "$branch" | sed 's|/|%2F|g')
    local gpu_scale_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/docker-compose.gpu-scale.yml"

    if curl -fsSL --connect-timeout 10 --max-time 30 "$gpu_scale_url" -o docker-compose.gpu-scale.yml 2>/dev/null; then
        if [ -s docker-compose.gpu-scale.yml ] && grep -q "celery-worker-gpu-scaled:" docker-compose.gpu-scale.yml; then
            echo "  ✓ Downloaded docker-compose.gpu-scale.yml (optional multi-GPU scaling)"
        else
            echo "  ⚠️  Downloaded gpu-scale file appears invalid, removing..."
            rm -f docker-compose.gpu-scale.yml
        fi
    else
        echo "  ℹ️  docker-compose.gpu-scale.yml not available (optional feature)"
    fi
}

download_management_script() {
    echo "✓ Downloading OpenTranscribe management script..."

    # Download the opentranscribe.sh script from the repository
    local max_retries=3
    local retry_count=0
    local branch="${OPENTRANSCRIBE_BRANCH:-master}"
    # URL-encode the branch name (replace / with %2F)
    local encoded_branch
    encoded_branch=$(echo "$branch" | sed 's|/|%2F|g')
    local download_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/opentranscribe.sh"

    while [ $retry_count -lt $max_retries ]; do
        if curl -fsSL --connect-timeout 10 --max-time 30 "$download_url" -o opentranscribe.sh; then
            # Validate downloaded file
            if [ -s opentranscribe.sh ] && grep -q "OpenTranscribe Management Script" opentranscribe.sh; then
                chmod +x opentranscribe.sh
                echo "✓ Downloaded and validated opentranscribe.sh"
                return 0
            else
                echo "⚠️  Downloaded opentranscribe.sh appears invalid, retrying..."
                rm -f opentranscribe.sh
            fi
        else
            echo "⚠️  Download attempt $((retry_count + 1)) failed"
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            echo "⏳ Retrying in 2 seconds..."
            sleep 2
        fi
    done

    echo -e "${YELLOW}⚠️  Failed to download opentranscribe.sh after $max_retries attempts${NC}"
    echo "You can manually download from: $download_url"
}

download_nginx_files() {
    echo "✓ Downloading NGINX/SSL configuration files..."

    local max_retries=3
    local branch="${OPENTRANSCRIBE_BRANCH:-master}"
    local encoded_branch
    encoded_branch=$(echo "$branch" | sed 's|/|%2F|g')

    # Create nginx and scripts directory structure
    mkdir -p nginx/ssl
    mkdir -p scripts
    touch nginx/ssl/.gitkeep

    # Download docker-compose.nginx.yml
    echo "  Downloading docker-compose.nginx.yml..."
    local retry_count=0
    local nginx_compose_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/docker-compose.nginx.yml"

    while [ $retry_count -lt $max_retries ]; do
        if curl -fsSL --connect-timeout 10 --max-time 30 "$nginx_compose_url" -o docker-compose.nginx.yml; then
            if [ -s docker-compose.nginx.yml ] && grep -q "nginx:" docker-compose.nginx.yml; then
                echo "  ✓ Downloaded docker-compose.nginx.yml"
                break
            else
                echo "  ⚠️  Downloaded nginx compose file appears invalid, retrying..."
                rm -f docker-compose.nginx.yml
            fi
        else
            echo "  ⚠️  Download attempt $((retry_count + 1)) failed"
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            sleep 2
        fi
    done

    if [ $retry_count -ge $max_retries ]; then
        echo "  ⚠️  Could not download docker-compose.nginx.yml (HTTPS support optional)"
    fi

    # Download nginx/site.conf.template
    echo "  Downloading nginx/site.conf.template..."
    retry_count=0
    local nginx_conf_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/nginx/site.conf.template"

    while [ $retry_count -lt $max_retries ]; do
        if curl -fsSL --connect-timeout 10 --max-time 30 "$nginx_conf_url" -o nginx/site.conf.template; then
            if [ -s nginx/site.conf.template ] && grep -q "server" nginx/site.conf.template; then
                echo "  ✓ Downloaded nginx/site.conf.template"
                break
            else
                echo "  ⚠️  Downloaded nginx config appears invalid, retrying..."
                rm -f nginx/site.conf.template
            fi
        else
            echo "  ⚠️  Download attempt $((retry_count + 1)) failed"
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            sleep 2
        fi
    done

    if [ $retry_count -ge $max_retries ]; then
        echo "  ⚠️  Could not download nginx/site.conf.template (HTTPS support optional)"
    fi

    # Download scripts/generate-ssl-cert.sh
    echo "  Downloading scripts/generate-ssl-cert.sh..."
    retry_count=0
    local ssl_script_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/scripts/generate-ssl-cert.sh"

    while [ $retry_count -lt $max_retries ]; do
        if curl -fsSL --connect-timeout 10 --max-time 30 "$ssl_script_url" -o scripts/generate-ssl-cert.sh; then
            if [ -s scripts/generate-ssl-cert.sh ] && grep -q "SSL Certificate" scripts/generate-ssl-cert.sh; then
                chmod +x scripts/generate-ssl-cert.sh
                echo "  ✓ Downloaded scripts/generate-ssl-cert.sh"
                break
            else
                echo "  ⚠️  Downloaded SSL script appears invalid, retrying..."
                rm -f scripts/generate-ssl-cert.sh
            fi
        else
            echo "  ⚠️  Download attempt $((retry_count + 1)) failed"
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            sleep 2
        fi
    done

    if [ $retry_count -ge $max_retries ]; then
        echo "  ⚠️  Could not download scripts/generate-ssl-cert.sh (HTTPS support optional)"
    fi

    # Download scripts/fix-model-permissions.sh
    echo "  Downloading scripts/fix-model-permissions.sh..."
    retry_count=0
    local fix_perms_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/scripts/fix-model-permissions.sh"

    while [ $retry_count -lt $max_retries ]; do
        if curl -fsSL --connect-timeout 10 --max-time 30 "$fix_perms_url" -o scripts/fix-model-permissions.sh; then
            if [ -s scripts/fix-model-permissions.sh ] && grep -q "Permission" scripts/fix-model-permissions.sh; then
                chmod +x scripts/fix-model-permissions.sh
                echo "  ✓ Downloaded scripts/fix-model-permissions.sh"
                break
            else
                echo "  ⚠️  Downloaded fix-permissions script appears invalid, retrying..."
                rm -f scripts/fix-model-permissions.sh
            fi
        else
            echo "  ⚠️  Download attempt $((retry_count + 1)) failed"
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            sleep 2
        fi
    done

    if [ $retry_count -ge $max_retries ]; then
        echo "  ⚠️  Could not download scripts/fix-model-permissions.sh"
    fi

    echo "✓ NGINX/SSL files download complete"
}

download_model_downloader_scripts() {
    echo "✓ Downloading model downloader scripts..."

    # Create scripts directory
    mkdir -p scripts

    # Download download-models.sh
    local max_retries=3
    local retry_count=0
    local branch="${OPENTRANSCRIBE_BRANCH:-master}"
    local encoded_branch
    encoded_branch=$(echo "$branch" | sed 's|/|%2F|g')
    local download_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/scripts/download-models.sh"

    while [ $retry_count -lt $max_retries ]; do
        if curl -fsSL --connect-timeout 10 --max-time 30 "$download_url" -o scripts/download-models.sh; then
            if [ -s scripts/download-models.sh ] && grep -q "OpenTranscribe Model Downloader" scripts/download-models.sh; then
                chmod +x scripts/download-models.sh
                echo "✓ Downloaded and validated download-models.sh"
                break
            else
                echo "⚠️  Downloaded download-models.sh appears invalid, retrying..."
                rm -f scripts/download-models.sh
            fi
        else
            echo "⚠️  Download attempt $((retry_count + 1)) failed"
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            echo "⏳ Retrying in 2 seconds..."
            sleep 2
        fi
    done

    # Download download-models.py
    retry_count=0
    download_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/scripts/download-models.py"

    while [ $retry_count -lt $max_retries ]; do
        if curl -fsSL --connect-timeout 10 --max-time 30 "$download_url" -o scripts/download-models.py; then
            if [ -s scripts/download-models.py ] && grep -q "Download all required AI models" scripts/download-models.py; then
                echo "✓ Downloaded and validated download-models.py"
                break
            else
                echo "⚠️  Downloaded download-models.py appears invalid, retrying..."
                rm -f scripts/download-models.py
            fi
        else
            echo "⚠️  Download attempt $((retry_count + 1)) failed"
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            echo "⏳ Retrying in 2 seconds..."
            sleep 2
        fi
    done

    # Download common.sh (utility functions used by opentr.sh)
    retry_count=0
    download_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/scripts/common.sh"

    while [ $retry_count -lt $max_retries ]; do
        if curl -fsSL --connect-timeout 10 --max-time 30 "$download_url" -o scripts/common.sh; then
            if [ -s scripts/common.sh ] && grep -q "check_docker" scripts/common.sh; then
                echo "✓ Downloaded and validated common.sh"
                return 0
            else
                echo "⚠️  Downloaded common.sh appears invalid, retrying..."
                rm -f scripts/common.sh
            fi
        else
            echo "⚠️  Download attempt $((retry_count + 1)) failed"
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            echo "⏳ Retrying in 2 seconds..."
            sleep 2
        fi
    done

    echo -e "${YELLOW}⚠️  Failed to download model downloader scripts${NC}"
    echo "Models will be downloaded on first application run instead."
}

create_production_env_example() {
    echo "✓ Downloading environment configuration template..."

    # Download the official .env.example from the repository
    local max_retries=3
    local retry_count=0
    local branch="${OPENTRANSCRIBE_BRANCH:-master}"
    # URL-encode the branch name (replace / with %2F)
    local encoded_branch
    encoded_branch=$(echo "$branch" | sed 's|/|%2F|g')
    local download_url="https://raw.githubusercontent.com/davidamacey/OpenTranscribe/${encoded_branch}/.env.example"

    while [ $retry_count -lt $max_retries ]; do
        if curl -fsSL --connect-timeout 10 --max-time 30 "$download_url" -o .env.example; then
            # Validate downloaded file
            if [ -s .env.example ] && grep -q "POSTGRES_HOST" .env.example && grep -q "HUGGINGFACE_TOKEN" .env.example; then
                echo "✓ Downloaded and validated .env.example"
                return 0
            else
                echo "⚠️  Downloaded env file appears invalid, retrying..."
                rm -f .env.example
            fi
        else
            echo "⚠️  Download attempt $((retry_count + 1)) failed"
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            echo "⏳ Retrying in 2 seconds..."
            sleep 2
        fi
    done

    echo -e "${RED}❌ Failed to download .env.example file after $max_retries attempts${NC}"
    echo "Please check your internet connection and try again."
    echo "Alternative: You can manually download from:"
    echo "$download_url"
    exit 1
}

prompt_huggingface_token() {
    # Unattended mode: use HUGGINGFACE_TOKEN from environment (or leave empty)
    if is_unattended; then
        if [[ -n "${HUGGINGFACE_TOKEN:-}" ]]; then
            ot_log_unattended "Using HUGGINGFACE_TOKEN from environment"
        else
            ot_log_unattended "HUGGINGFACE_TOKEN not set; continuing without it"
            HUGGINGFACE_TOKEN=""
        fi
        return 0
    fi

    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}🤗 HuggingFace Token Configuration${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "${RED}⚠️  IMPORTANT: Token + Model Agreements REQUIRED!${NC}"
    echo ""
    echo "Without this token:"
    echo "  • Transcription will work normally"
    echo "  • Speaker diarization (who said what) will NOT work"
    echo "  • Models cannot be pre-downloaded (will download on first use)"
    echo ""
    echo -e "${CYAN}Step 1: Get your FREE HuggingFace token${NC}"
    echo "  1. Visit: https://huggingface.co/settings/tokens"
    echo "  2. Click 'New token'"
    echo "  3. Give it a name (e.g., 'OpenTranscribe')"
    echo "  4. Select 'Read' permissions"
    echo "  5. Copy the token"
    echo ""
    echo -e "${CYAN}Step 2: Accept BOTH gated model agreements (CRITICAL!)${NC}"
    echo -e "  ${RED}You MUST accept BOTH models or downloads will fail!${NC}"
    echo ""
    echo "  1. Segmentation Model:"
    echo "     https://huggingface.co/pyannote/segmentation-3.0"
    echo -e "     ${GREEN}→ Click 'Agree and access repository'${NC}"
    echo ""
    echo "  2. Speaker Diarization Model:"
    echo "     https://huggingface.co/pyannote/speaker-diarization-3.1"
    echo -e "     ${GREEN}→ Click 'Agree and access repository'${NC}"
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    # Ask if they want to enter token now
    read -p "Do you have a HuggingFace token to enter now? (Y/n) " -n 1 -r </dev/tty
    echo
    echo

    if [[ $REPLY =~ ^[Nn]$ ]]; then
        print_warning "Skipping HuggingFace token - you can add it later"
        echo "To add later:"
        echo "  1. Edit: $PROJECT_DIR/.env"
        echo "  2. Set: HUGGINGFACE_TOKEN=your_token_here"
        echo "  3. Restart: cd $PROJECT_DIR && ./opentranscribe.sh restart"
        echo ""
        HUGGINGFACE_TOKEN=""
        return 0
    fi

    # Prompt for token
    echo "Please enter your HuggingFace token:"
    echo "(Token will be hidden for security)"
    read -s HUGGINGFACE_TOKEN </dev/tty
    echo

    # Strip any 'HUGGINGFACE_TOKEN=' prefix if user pasted it
    HUGGINGFACE_TOKEN="${HUGGINGFACE_TOKEN#HUGGINGFACE_TOKEN=}"

    # Validate token format (basic check - should start with hf_)
    if [[ -z "$HUGGINGFACE_TOKEN" ]]; then
        print_warning "No token entered - you can add it later in .env file"
        HUGGINGFACE_TOKEN=""
    elif [[ ! "$HUGGINGFACE_TOKEN" =~ ^hf_ ]]; then
        print_warning "Token doesn't start with 'hf_' - this may not be valid"
        echo "Using it anyway, but verify it's correct."
        echo ""
    else
        print_success "HuggingFace token configured!"
        echo ""
    fi
}

configure_environment() {
    echo -e "${BLUE}⚙️  Configuring environment...${NC}"

    if [ -f .env ]; then
        echo "ℹ️  Using existing .env file"
        return
    fi

    # Generate all secure secrets using openssl or python3 fallback
    echo "🔒 Generating secure credentials..."

    if command -v openssl &> /dev/null; then
        # Use openssl for cryptographically secure random generation
        POSTGRES_PASSWORD=$(openssl rand -hex 32)
        MINIO_ROOT_PASSWORD=$(openssl rand -hex 32)
        JWT_SECRET=$(openssl rand -hex 64)
        # ENCRYPTION_KEY: Add prefix to make it invalid base64, forcing backend exception handler path
        # This ensures backend uses the working derive-from-string logic
        ENCRYPTION_KEY="opentranscribe_$(openssl rand -base64 48)"
        REDIS_PASSWORD=$(openssl rand -hex 32)
        OPENSEARCH_PASSWORD=$(openssl rand -hex 32)
        FLOWER_PASSWORD=$(openssl rand -hex 16)
        # MinIO server-side encryption key (AES-256-GCM)
        MINIO_KMS_KEY="opentranscribe-key:$(openssl rand -base64 32)"
    elif command -v python3 &> /dev/null; then
        # Fallback to Python's secrets module
        POSTGRES_PASSWORD=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        MINIO_ROOT_PASSWORD=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(64))")
        # ENCRYPTION_KEY: Add prefix to force backend exception handler path
        ENCRYPTION_KEY=$(python3 -c "import secrets, base64; print('opentranscribe_' + base64.b64encode(secrets.token_bytes(48)).decode())")
        REDIS_PASSWORD=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        OPENSEARCH_PASSWORD=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        FLOWER_PASSWORD=$(python3 -c "import secrets; print(secrets.token_hex(16))")
        # MinIO server-side encryption key (AES-256-GCM)
        MINIO_KMS_KEY=$(python3 -c "import secrets, base64; print('opentranscribe-key:' + base64.b64encode(secrets.token_bytes(32)).decode())")
    else
        # Basic fallback (not recommended for production)
        POSTGRES_PASSWORD="postgres_$(date +%s)_$(shuf -i 10000-99999 -n 1 2>/dev/null || echo $RANDOM)"
        MINIO_ROOT_PASSWORD="minio_$(date +%s)_$(shuf -i 10000-99999 -n 1 2>/dev/null || echo $RANDOM)"
        JWT_SECRET="jwt_secret_$(date +%s)_$(shuf -i 10000-99999 -n 1 2>/dev/null || echo $RANDOM)"
        ENCRYPTION_KEY="encryption_key_$(date +%s)_$(shuf -i 10000-99999 -n 1 2>/dev/null || echo $RANDOM)"
        REDIS_PASSWORD="redis_$(date +%s)_$(shuf -i 10000-99999 -n 1 2>/dev/null || echo $RANDOM)"
        OPENSEARCH_PASSWORD="opensearch_$(date +%s)_$(shuf -i 10000-99999 -n 1 2>/dev/null || echo $RANDOM)"
        FLOWER_PASSWORD="flower_$(date +%s)_$(shuf -i 10000-99999 -n 1 2>/dev/null || echo $RANDOM)"
        # MinIO KMS key (basic fallback)
        MINIO_KMS_KEY="opentranscribe-key:$(date +%s | md5sum | head -c 32 | base64)"
        echo "⚠️  Using basic secrets - install openssl or python3 for cryptographically secure generation"
    fi

    print_success "Secure credentials generated (64-char JWT/encryption, 32-char passwords)"

    # Prompt for HuggingFace token
    prompt_huggingface_token

    # Model selection based on hardware
    select_whisper_model

    # OpenSearch neural search model selection
    select_opensearch_models

    # GPU selection for multi-GPU systems
    select_gpu_device

    # LLM configuration for AI features
    configure_llm_settings

    # Create .env file
    create_env_file
}

select_whisper_model() {
    echo -e "${YELLOW}🎤 Selecting Whisper Model based on hardware...${NC}"
    echo ""

    # Auto-select optimal model based on hardware with GPU memory detection
    local RECOMMENDED_MODEL=""
    local RECOMMENDATION_REASON=""

    case "$DETECTED_DEVICE" in
        "cuda")
            # Try to detect GPU memory for better model selection
            if command -v nvidia-smi &> /dev/null; then
                GPU_MEMORY=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i "${GPU_DEVICE_ID:-0}")
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
            else
                # Fallback if nvidia-smi fails
                RECOMMENDED_MODEL="large-v3-turbo"
                RECOMMENDATION_REASON="CUDA detected - fast default with excellent accuracy"
            fi
            ;;
        "mps")
            RECOMMENDED_MODEL="small"
            RECOMMENDATION_REASON="Apple Silicon detected - optimized for CPU/MPS processing"
            ;;
        "cpu")
            RECOMMENDED_MODEL="base"
            RECOMMENDATION_REASON="CPU processing - fastest option"
            ;;
    esac

    # Display recommendation
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
    echo "Note: Larger models provide better accuracy but require more memory"
    echo "      and processing time. You can change this later in the .env file."
    echo ""

    # Unattended: honor WHISPER_MODEL env var if set, else use recommended
    if is_unattended; then
        if [[ -n "${WHISPER_MODEL:-}" ]]; then
            ot_log_unattended "Using WHISPER_MODEL=${WHISPER_MODEL} from environment"
        else
            WHISPER_MODEL="$RECOMMENDED_MODEL"
            ot_log_unattended "WHISPER_MODEL defaulted to ${WHISPER_MODEL}"
        fi
    else
        # Prompt user for model selection
        while true; do
            read -p "Select model (tiny/base/small/medium/large-v2/large-v3/large-v3-turbo) [${RECOMMENDED_MODEL}]: " user_model </dev/tty

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
                    echo -e "${RED}Invalid model. Please choose: tiny, base, small, medium, large-v2, large-v3, or large-v3-turbo${NC}"
                    ;;
            esac
        done
    fi

    echo ""
    echo -e "${GREEN}✓ Selected model: ${WHISPER_MODEL}${NC}"
    if [ "$WHISPER_MODEL" != "$RECOMMENDED_MODEL" ]; then
        echo -e "${YELLOW}  Note: You selected a different model than recommended${NC}"
    fi
    echo ""
}

select_opensearch_models() {
    echo -e "${YELLOW}🔍 Selecting OpenSearch Neural Search Models...${NC}"
    echo ""

    # Auto-select optimal model based on use case
    local RECOMMENDED_MODEL="all-MiniLM-L6-v2"
    local RECOMMENDATION_REASON="Fast, lightweight, good for English keyword-heavy searches"

    # Display available models
    echo -e "${BLUE}Available Neural Search Models (for semantic/vector search):${NC}"
    echo ""
    echo "Quality Tier       Model                    Dims  Size   Languages    Use Case"
    echo "───────────────────────────────────────────────────────────────────────────────────────────────────"
    echo "FAST (Default)     all-MiniLM-L6-v2         384   80MB   English      Fast baseline, keyword-heavy"
    echo "FAST (Multi)       multilingual-MiniLM      384   420MB  50+ langs    Fast multilingual"
    echo "BALANCED           all-mpnet-base-v2        768   420MB  English      Better semantic understanding"
    echo "BALANCED (Multi)   multilingual-mpnet       768   1.1GB  50+ langs    Quality multilingual"
    echo "BEST               all-distilroberta-v1     768   290MB  English      Best retrieval quality"
    echo "BEST (Multi)       multilingual-cased       512   480MB  15 langs     Best for common languages"
    echo ""
    echo -e "${GREEN}Recommendation: ${RECOMMENDED_MODEL}${NC}"
    echo "  Reason: ${RECOMMENDATION_REASON}"
    echo ""
    echo "Note: Higher quality models provide better semantic search but are larger."
    echo "      You can change this later via Admin UI (Settings → Search → Model)."
    echo ""

    # Simplified options
    echo "Select model quality level:"
    echo "  1) fast         - all-MiniLM-L6-v2 (80MB, English only) [DEFAULT]"
    echo "  2) balanced     - all-mpnet-base-v2 (420MB, English, better quality)"
    echo "  3) best         - all-distilroberta-v1 (290MB, English, highest quality)"
    echo "  4) multilingual-fast      - paraphrase-multilingual-MiniLM-L12-v2 (420MB, 50+ langs)"
    echo "  5) multilingual-balanced  - paraphrase-multilingual-mpnet-base-v2 (1.1GB, 50+ langs)"
    echo "  6) multilingual-best      - distiluse-base-multilingual-cased-v1 (480MB, 15 langs)"
    echo "  7) all-models   - Download all 6 models (~2.6GB total, complete offline support)"
    echo "  8) skip         - Don't download now (download on first use)"
    echo ""

    # Unattended: honor OPENSEARCH_MODELS env var if set, else default to fast model
    if is_unattended; then
        if [[ -n "${OPENSEARCH_MODELS:-}" ]]; then
            OPENSEARCH_NEURAL_MODEL="${OPENSEARCH_NEURAL_MODEL:-huggingface/sentence-transformers/${OPENSEARCH_MODELS}}"
            ot_log_unattended "Using OPENSEARCH_MODELS=${OPENSEARCH_MODELS} from environment"
        else
            OPENSEARCH_MODELS="all-MiniLM-L6-v2"
            OPENSEARCH_NEURAL_MODEL="huggingface/sentence-transformers/all-MiniLM-L6-v2"
            ot_log_unattended "OPENSEARCH_MODELS defaulted to ${OPENSEARCH_MODELS}"
        fi
        echo ""
        return 0
    fi

    # Prompt user for model selection
    while true; do
        read -p "Select model quality [1-8, default=1]: " user_choice </dev/tty

        # Use default if user just presses Enter
        if [ -z "$user_choice" ] || [ "$user_choice" = "1" ]; then
            OPENSEARCH_MODELS="all-MiniLM-L6-v2"
            OPENSEARCH_NEURAL_MODEL="huggingface/sentence-transformers/all-MiniLM-L6-v2"
            break
        fi

        # Validate input
        case "$user_choice" in
            2)
                OPENSEARCH_MODELS="all-mpnet-base-v2"
                OPENSEARCH_NEURAL_MODEL="huggingface/sentence-transformers/all-mpnet-base-v2"
                break
                ;;
            3)
                OPENSEARCH_MODELS="all-distilroberta-v1"
                OPENSEARCH_NEURAL_MODEL="huggingface/sentence-transformers/all-distilroberta-v1"
                break
                ;;
            4)
                OPENSEARCH_MODELS="paraphrase-multilingual-MiniLM-L12-v2"
                OPENSEARCH_NEURAL_MODEL="huggingface/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
                break
                ;;
            5)
                OPENSEARCH_MODELS="paraphrase-multilingual-mpnet-base-v2"
                OPENSEARCH_NEURAL_MODEL="huggingface/sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
                break
                ;;
            6)
                OPENSEARCH_MODELS="distiluse-base-multilingual-cased-v1"
                OPENSEARCH_NEURAL_MODEL="huggingface/sentence-transformers/distiluse-base-multilingual-cased-v1"
                break
                ;;
            7)
                # Download all models for complete offline support
                export DOWNLOAD_ALL_OPENSEARCH_MODELS=true
                OPENSEARCH_MODELS=""  # Empty means all models will be downloaded
                OPENSEARCH_NEURAL_MODEL="huggingface/sentence-transformers/all-MiniLM-L6-v2"
                echo ""
                echo -e "${GREEN}✓ Will download all 6 models (~2.6GB)${NC}"
                echo "This provides complete offline support with all quality tiers"
                echo ""
                break
                ;;
            8)
                OPENSEARCH_MODELS=""
                OPENSEARCH_NEURAL_MODEL="huggingface/sentence-transformers/all-MiniLM-L6-v2"
                echo ""
                echo -e "${YELLOW}⚠️  Skipping OpenSearch model download${NC}"
                echo "Model will download automatically on first use (causes delay)"
                echo ""
                return 0
                ;;
            *)
                echo -e "${RED}Invalid choice. Please enter 1-8.${NC}"
                ;;
        esac
    done

    echo ""
    echo -e "${GREEN}✓ Selected model: ${OPENSEARCH_MODELS}${NC}"
    echo ""
}

select_gpu_device() {
    # Only prompt if CUDA is detected and multiple GPUs are available
    if [[ "$DETECTED_DEVICE" != "cuda" ]] || [[ ${GPU_COUNT:-0} -le 1 ]]; then
        return 0
    fi

    # Unattended: honor GPU_DEVICE_ID env var if set, else keep the auto-detected default
    if is_unattended; then
        ot_log_unattended "GPU_DEVICE_ID pinned to ${GPU_DEVICE_ID:-0}"
        return 0
    fi

    echo ""
    echo -e "${YELLOW}🎮 Multiple GPUs Detected${NC}"
    echo "================================================="
    echo "Your system has ${GPU_COUNT} NVIDIA GPUs available."
    echo ""
    echo "Available GPUs:"
    echo ""

    # Display GPU list with detailed information
    nvidia-smi --query-gpu=index,name,memory.total,memory.free --format=csv,noheader | while IFS=, read -r idx name mem_total mem_free; do
        echo "  [${idx}] ${name} (${mem_total} total, ${mem_free} free)"
    done

    echo ""
    echo "Select which GPU to use for AI model downloads and transcription processing."
    echo "Consider GPU availability, memory, and existing workloads when choosing."
    echo ""

    # Prompt user for GPU selection
    while true; do
        read -p "Enter GPU index to use [0-$((GPU_COUNT-1))] (default: ${GPU_DEVICE_ID}): " selected_gpu </dev/tty

        # Use default if empty
        if [[ -z "$selected_gpu" ]]; then
            selected_gpu=$GPU_DEVICE_ID
            break
        fi

        # Validate input is a number
        if ! [[ "$selected_gpu" =~ ^[0-9]+$ ]]; then
            echo -e "${RED}❌ Invalid input. Please enter a number.${NC}"
            continue
        fi

        # Validate GPU index is within range
        if [[ $selected_gpu -ge 0 && $selected_gpu -lt $GPU_COUNT ]]; then
            break
        else
            echo -e "${RED}❌ Invalid GPU index. Please enter a number between 0 and $((GPU_COUNT-1)).${NC}"
        fi
    done

    # Update GPU_DEVICE_ID with user selection
    GPU_DEVICE_ID=$selected_gpu

    # Get selected GPU details
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader,nounits -i "$GPU_DEVICE_ID")
    GPU_MEMORY=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i "$GPU_DEVICE_ID")

    echo ""
    echo -e "${GREEN}✓ Selected GPU ${GPU_DEVICE_ID}: ${GPU_NAME} (${GPU_MEMORY}MB)${NC}"
    echo ""
}

configure_llm_settings() {
    # Unattended: honor LLM_PROVIDER and provider-specific env vars if set, else skip
    if is_unattended; then
        LLM_PROVIDER="${LLM_PROVIDER:-vllm}"
        ot_log_unattended "LLM_PROVIDER=${LLM_PROVIDER} (interactive LLM wizard skipped)"
        return 0
    fi

    echo ""
    echo -e "${YELLOW}🤖 LLM Configuration for AI Features${NC}"
    echo "=================================================="
    echo "OpenTranscribe includes AI features that require an LLM (Large Language Model):"
    echo "  • AI-powered transcript summarization with BLUF format"
    echo "  • Speaker identification suggestions"
    echo ""
    echo -e "${BLUE}Supported LLM Providers:${NC}"
    echo "1. vLLM (Default) - Local server with OpenAI-compatible API"
    echo "2. OpenAI - Official OpenAI API (requires API key)"
    echo "3. Ollama - Local Ollama server"
    echo "4. Anthropic Claude - Claude API (requires API key)"
    echo "5. OpenRouter - Multi-provider API service"
    echo ""
    echo -e "${YELLOW}💡 Configuration Options:${NC}"
    echo "• Configure now (recommended for vLLM users)"
    echo "• Skip configuration (you can set up later in .env file)"
    echo ""

    read -p "Do you want to configure LLM settings now? (y/N) " -n 1 -r </dev/tty
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo ""
        echo "Select your LLM provider:"
        echo "1) vLLM (Local server - recommended)"
        echo "2) OpenAI"
        echo "3) Ollama"
        echo "4) Anthropic Claude"
        echo "5) OpenRouter"
        echo "6) Skip (configure manually later)"

        read -p "Enter your choice (1-6): " -n 1 -r llm_choice </dev/tty
        echo
        echo

        case $llm_choice in
            1)
                echo "✓ Configuring vLLM (Local server)"
                LLM_PROVIDER="vllm"
                read -p "Enter your vLLM server URL [http://localhost:8000/v1]: " vllm_url </dev/tty
                VLLM_BASE_URL=${vllm_url:-"http://localhost:8000/v1"}
                read -p "Enter your vLLM API key (optional): " vllm_key </dev/tty
                VLLM_API_KEY=${vllm_key:-""}
                read -p "Enter your model name [gpt-oss]: " vllm_model </dev/tty
                VLLM_MODEL_NAME=${vllm_model:-"gpt-oss"}
                echo "✓ vLLM configured: $VLLM_BASE_URL with model $VLLM_MODEL_NAME"
                ;;
            2)
                echo "✓ Configuring OpenAI"
                LLM_PROVIDER="openai"
                read -p "Enter your OpenAI API key: " openai_key </dev/tty
                OPENAI_API_KEY=$openai_key
                read -p "Enter OpenAI model [gpt-4o-mini]: " openai_model </dev/tty
                OPENAI_MODEL_NAME=${openai_model:-"gpt-4o-mini"}
                echo "✓ OpenAI configured with model $OPENAI_MODEL_NAME"
                ;;
            3)
                echo "✓ Configuring Ollama"
                LLM_PROVIDER="ollama"
                read -p "Enter your Ollama server URL [http://localhost:11434]: " ollama_url </dev/tty
                OLLAMA_BASE_URL=${ollama_url:-"http://localhost:11434"}
                read -p "Enter Ollama model [llama2:7b-chat]: " ollama_model </dev/tty
                OLLAMA_MODEL_NAME=${ollama_model:-"llama2:7b-chat"}
                echo "✓ Ollama configured: $OLLAMA_BASE_URL with model $OLLAMA_MODEL_NAME"
                ;;
            4)
                echo "✓ Configuring Anthropic Claude"
                LLM_PROVIDER="anthropic"
                read -p "Enter your Anthropic API key: " anthropic_key </dev/tty
                ANTHROPIC_API_KEY=$anthropic_key
                read -p "Enter Claude model [claude-3-haiku-20240307]: " anthropic_model </dev/tty
                ANTHROPIC_MODEL_NAME=${anthropic_model:-"claude-3-haiku-20240307"}
                echo "✓ Anthropic Claude configured with model $ANTHROPIC_MODEL_NAME"
                ;;
            5)
                echo "✓ Configuring OpenRouter"
                LLM_PROVIDER="openrouter"
                read -p "Enter your OpenRouter API key: " openrouter_key </dev/tty
                OPENROUTER_API_KEY=$openrouter_key
                read -p "Enter OpenRouter model [anthropic/claude-3-haiku]: " openrouter_model </dev/tty
                OPENROUTER_MODEL_NAME=${openrouter_model:-"anthropic/claude-3-haiku"}
                echo "✓ OpenRouter configured with model $OPENROUTER_MODEL_NAME"
                ;;
            6|*)
                echo "⏭️  Skipping LLM configuration - you can configure manually in .env file"
                LLM_PROVIDER="vllm"  # Default
                ;;
        esac
    else
        echo "⏭️  Skipping LLM configuration - you can configure manually in .env file"
        echo "💡 Edit the LLM_* variables in .env file to enable AI features"
        LLM_PROVIDER="vllm"  # Default
    fi

    echo ""
    echo -e "${YELLOW}💡 LLM Configuration Notes:${NC}"
    echo "• AI features require a working LLM endpoint"
    echo "• You can change providers anytime by editing .env file"
    echo "• See .env.example for all available configuration options"
}

create_env_file() {
    echo "✓ Creating .env file with optimized settings..."

    # Copy example and update values
    cp .env.example .env

    # Update security credentials (auto-generated)
    sed -i.bak "s|POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$POSTGRES_PASSWORD|g" .env
    sed -i.bak "s|MINIO_ROOT_PASSWORD=.*|MINIO_ROOT_PASSWORD=$MINIO_ROOT_PASSWORD|g" .env
    sed -i.bak "s|JWT_SECRET_KEY=.*|JWT_SECRET_KEY=$JWT_SECRET|g" .env
    sed -i.bak "s|ENCRYPTION_KEY=.*|ENCRYPTION_KEY=$ENCRYPTION_KEY|g" .env
    sed -i.bak "s|REDIS_PASSWORD=.*|REDIS_PASSWORD=$REDIS_PASSWORD|g" .env
    sed -i.bak "s|OPENSEARCH_PASSWORD=.*|OPENSEARCH_PASSWORD=$OPENSEARCH_PASSWORD|g" .env
    sed -i.bak "s|FLOWER_PASSWORD=.*|FLOWER_PASSWORD=$FLOWER_PASSWORD|g" .env

    # MinIO server-side encryption (data at rest)
    sed -i.bak "s|MINIO_KMS_SECRET_KEY=.*|MINIO_KMS_SECRET_KEY=$MINIO_KMS_KEY|g" .env

    # Update AI model configuration
    sed -i.bak "s|HUGGINGFACE_TOKEN=.*|HUGGINGFACE_TOKEN=$HUGGINGFACE_TOKEN|g" .env
    sed -i.bak "s|WHISPER_MODEL=.*|WHISPER_MODEL=$WHISPER_MODEL|g" .env
    sed -i.bak "s|BATCH_SIZE=.*|BATCH_SIZE=$BATCH_SIZE|g" .env
    sed -i.bak "s|COMPUTE_TYPE=.*|COMPUTE_TYPE=$COMPUTE_TYPE|g" .env

    # Update OpenSearch neural model configuration (if selected)
    if [[ -n "$OPENSEARCH_NEURAL_MODEL" ]]; then
        sed -i.bak "s|OPENSEARCH_NEURAL_MODEL=.*|OPENSEARCH_NEURAL_MODEL=$OPENSEARCH_NEURAL_MODEL|g" .env
    fi

    # Update LLM configuration
    if [[ -n "$LLM_PROVIDER" ]]; then
        sed -i.bak "s|LLM_PROVIDER=.*|LLM_PROVIDER=$LLM_PROVIDER|g" .env
    fi

    # Provider-specific configurations
    if [[ "$LLM_PROVIDER" == "vllm" && -n "$VLLM_BASE_URL" ]]; then
        sed -i.bak "s|VLLM_BASE_URL=.*|VLLM_BASE_URL=$VLLM_BASE_URL|g" .env
        if [[ -n "$VLLM_API_KEY" ]]; then
            sed -i.bak "s|VLLM_API_KEY=.*|VLLM_API_KEY=$VLLM_API_KEY|g" .env
        fi
        if [[ -n "$VLLM_MODEL_NAME" ]]; then
            sed -i.bak "s|VLLM_MODEL_NAME=.*|VLLM_MODEL_NAME=$VLLM_MODEL_NAME|g" .env
        fi
    elif [[ "$LLM_PROVIDER" == "openai" && -n "$OPENAI_API_KEY" ]]; then
        sed -i.bak "s|# OPENAI_API_KEY=.*|OPENAI_API_KEY=$OPENAI_API_KEY|g" .env
        if [[ -n "$OPENAI_MODEL_NAME" ]]; then
            sed -i.bak "s|# OPENAI_MODEL_NAME=.*|OPENAI_MODEL_NAME=$OPENAI_MODEL_NAME|g" .env
        fi
    elif [[ "$LLM_PROVIDER" == "ollama" && -n "$OLLAMA_BASE_URL" ]]; then
        sed -i.bak "s|# OLLAMA_BASE_URL=.*|OLLAMA_BASE_URL=$OLLAMA_BASE_URL|g" .env
        if [[ -n "$OLLAMA_MODEL_NAME" ]]; then
            sed -i.bak "s|# OLLAMA_MODEL_NAME=.*|OLLAMA_MODEL_NAME=$OLLAMA_MODEL_NAME|g" .env
        fi
    elif [[ "$LLM_PROVIDER" == "anthropic" && -n "$ANTHROPIC_API_KEY" ]]; then
        sed -i.bak "s|# ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY|g" .env
        if [[ -n "$ANTHROPIC_MODEL_NAME" ]]; then
            sed -i.bak "s|# ANTHROPIC_MODEL_NAME=.*|ANTHROPIC_MODEL_NAME=$ANTHROPIC_MODEL_NAME|g" .env
        fi
    elif [[ "$LLM_PROVIDER" == "openrouter" && -n "$OPENROUTER_API_KEY" ]]; then
        sed -i.bak "s|# OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=$OPENROUTER_API_KEY|g" .env
        if [[ -n "$OPENROUTER_MODEL_NAME" ]]; then
            sed -i.bak "s|# OPENROUTER_MODEL_NAME=.*|OPENROUTER_MODEL_NAME=$OPENROUTER_MODEL_NAME|g" .env
        fi
    fi

    # Hardware-specific configurations
    case "$DETECTED_DEVICE" in
        "cuda")
            sed -i.bak "s|USE_GPU=.*|USE_GPU=true|g" .env
            sed -i.bak "s|GPU_DEVICE_ID=.*|GPU_DEVICE_ID=${GPU_DEVICE_ID:-0}|g" .env
            sed -i.bak "s|TORCH_DEVICE=.*|TORCH_DEVICE=cuda|g" .env
            ;;
        "mps")
            sed -i.bak "s|USE_GPU=.*|USE_GPU=false|g" .env
            sed -i.bak "s|TORCH_DEVICE=.*|TORCH_DEVICE=mps|g" .env
            ;;
        "cpu")
            sed -i.bak "s|USE_GPU=.*|USE_GPU=false|g" .env
            sed -i.bak "s|TORCH_DEVICE=.*|TORCH_DEVICE=cpu|g" .env
            ;;
    esac

    # Set model cache directory
    MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-./models}"
    sed -i.bak "s|MODEL_CACHE_DIR=.*|MODEL_CACHE_DIR=$MODEL_CACHE_DIR|g" .env

    # Add Docker runtime configuration
    echo "" >> .env
    echo "# Hardware Configuration (Auto-detected)" >> .env
    echo "DETECTED_DEVICE=${DETECTED_DEVICE}" >> .env
    echo "USE_NVIDIA_RUNTIME=${USE_GPU_RUNTIME}" >> .env
    # FORCE_CPU_MODE: persisted opt-out signal read by opentranscribe.sh at
    # start/restart time. When "true", the management script skips the
    # docker-compose.gpu.yml / docker-compose.blackwell.yml overlays even if
    # Docker reports an nvidia runtime — ensuring the CPU-only choice made at
    # install time survives subsequent start/stop/restart cycles without
    # requiring the user to re-pass --cpu.
    echo "FORCE_CPU_MODE=${FORCE_CPU}" >> .env

    # CPU-friendly diarization default. When DETECTED_DEVICE=cpu (either
    # because FORCE_CPU was set or because no GPU was found on the host),
    # disable diarization in .env. PyAnnote requires CUDA — leaving it on
    # would crash workers or run unusably slow. WHISPER_MODEL is already
    # handled by select_whisper_model() (which recommends "base" for CPU
    # and is written to .env above via WHISPER_MODEL=$WHISPER_MODEL). Users
    # can flip ENABLE_DIARIZATION=true in .env if they have a reason.
    if [[ "$DETECTED_DEVICE" == "cpu" ]]; then
        if grep -q '^ENABLE_DIARIZATION=' .env; then
            sed -i.bak "s|^ENABLE_DIARIZATION=.*|ENABLE_DIARIZATION=${ENABLE_DIARIZATION_CPU_DEFAULT}|" .env
        else
            echo "ENABLE_DIARIZATION=${ENABLE_DIARIZATION_CPU_DEFAULT}" >> .env
        fi
    fi

    # Note: Database schema is managed by Alembic migrations on backend startup

    # Clean up backup file
    rm -f .env.bak

    echo "✓ Environment configured for $DETECTED_DEVICE with $COMPUTE_TYPE precision"
}

configure_https_settings() {
    # Unattended: only configure HTTPS if NGINX_SERVER_NAME is pre-set in the environment
    if is_unattended; then
        if [[ -z "${NGINX_SERVER_NAME:-}" ]]; then
            ot_log_unattended "NGINX_SERVER_NAME not set; skipping HTTPS/NGINX setup"
            NGINX_SERVER_NAME=""
            SSL_CONFIGURED=false
            return 0
        fi
        ot_log_unattended "NGINX_SERVER_NAME=${NGINX_SERVER_NAME}; certificates must be generated out of band"
        SSL_CONFIGURED=false
        return 0
    fi

    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}🔒 HTTPS/SSL Configuration${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "HTTPS is required for:"
    echo "  • Browser microphone recording from other devices"
    echo "  • Secure access from your local network"
    echo "  • Production/homelab deployments"
    echo ""
    echo "Without HTTPS:"
    echo "  • Microphone recording only works on localhost"
    echo "  • Other devices on your network can't use recording"
    echo ""

    read -p "Do you want to set up HTTPS with self-signed certificates? (y/N) " -n 1 -r </dev/tty
    echo
    echo

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Skipping HTTPS setup - you can configure later"
        echo "To enable HTTPS later:"
        echo "  1. Run: cd $PROJECT_DIR && ./opentranscribe.sh setup-ssl"
        echo "  2. Or follow: docs/NGINX_SETUP.md (if downloaded)"
        echo ""
        NGINX_SERVER_NAME=""
        return 0
    fi

    # Prompt for hostname
    echo -e "${CYAN}Enter a hostname for your OpenTranscribe installation:${NC}"
    echo "(e.g., opentranscribe.local, transcribe.home, your-hostname.lan)"
    echo ""
    read -p "Hostname [opentranscribe.local]: " user_hostname </dev/tty
    NGINX_SERVER_NAME="${user_hostname:-opentranscribe.local}"

    # Validate hostname (basic check)
    if [[ ! "$NGINX_SERVER_NAME" =~ ^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$ ]]; then
        print_warning "Hostname appears invalid, using default: opentranscribe.local"
        NGINX_SERVER_NAME="opentranscribe.local"
    fi

    echo ""
    print_success "Hostname set to: $NGINX_SERVER_NAME"

    # Update .env file with NGINX_SERVER_NAME
    if [ -f .env ]; then
        # Check if NGINX_SERVER_NAME already exists in .env
        if grep -q "^NGINX_SERVER_NAME=" .env || grep -q "^#.*NGINX_SERVER_NAME=" .env; then
            # Update existing entry (commented or not)
            sed -i.bak "s|^#*\s*NGINX_SERVER_NAME=.*|NGINX_SERVER_NAME=$NGINX_SERVER_NAME|g" .env
        else
            # Add new entry
            echo "" >> .env
            echo "# HTTPS/SSL Configuration" >> .env
            echo "NGINX_SERVER_NAME=$NGINX_SERVER_NAME" >> .env
        fi
        rm -f .env.bak
        echo "✓ Updated .env with NGINX_SERVER_NAME=$NGINX_SERVER_NAME"
    fi

    # Check if SSL certificate generation script exists
    if [ -f "scripts/generate-ssl-cert.sh" ]; then
        echo ""
        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${YELLOW}📜 SSL Certificate Generation${NC}"
        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""

        # Check for existing certificates
        if [ -f "nginx/ssl/server.crt" ] && [ -f "nginx/ssl/server.key" ]; then
            echo -e "${YELLOW}⚠️  Existing SSL certificates detected!${NC}"
            echo "   nginx/ssl/server.crt and nginx/ssl/server.key already exist."
            echo ""
            read -p "Overwrite existing certificates? (y/N) " -n 1 -r </dev/tty
            echo
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                print_info "Keeping existing certificates"
                SSL_CONFIGURED=true
                return 0
            fi
            echo ""
        fi

        read -p "Generate SSL certificates now? (Y/n) " -n 1 -r </dev/tty
        echo
        echo

        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            echo -e "${BLUE}Generating SSL certificates...${NC}"
            echo ""

            # Run the SSL certificate generation script with auto-IP detection
            if bash scripts/generate-ssl-cert.sh "$NGINX_SERVER_NAME" --auto-ip; then
                echo ""
                print_success "SSL certificates generated successfully!"
                SSL_CONFIGURED=true
            else
                print_warning "SSL certificate generation failed"
                echo "You can generate certificates later with:"
                echo "  cd $PROJECT_DIR && ./scripts/generate-ssl-cert.sh $NGINX_SERVER_NAME --auto-ip"
                SSL_CONFIGURED=false
            fi
        else
            print_info "Skipping certificate generation"
            echo "Generate certificates later with:"
            echo "  cd $PROJECT_DIR && ./scripts/generate-ssl-cert.sh $NGINX_SERVER_NAME --auto-ip"
            SSL_CONFIGURED=false
        fi
    else
        print_warning "SSL certificate generation script not found"
        echo "You may need to manually create certificates."
        SSL_CONFIGURED=false
    fi

    # Show next steps for DNS configuration
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}📋 HTTPS Setup Next Steps${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "To complete HTTPS setup:"
    echo ""
    echo "1. Configure DNS (choose one):"
    echo "   • Router DNS: Add $NGINX_SERVER_NAME → your server IP"
    echo "   • /etc/hosts: Add 'YOUR_SERVER_IP  $NGINX_SERVER_NAME'"
    echo ""
    echo "2. Trust the certificate on each device:"
    echo "   • Copy nginx/ssl/server.crt to client devices"
    echo "   • Import into browser/system trust store"
    echo ""
    echo "3. Access at: https://$NGINX_SERVER_NAME"
    echo ""
}

#######################
# MODEL DOWNLOADING
#######################

download_ai_models() {
    print_header "AI Model Pre-Download"

    # Unattended: skip all model downloads and let them fetch at first use
    if is_unattended; then
        ot_log_unattended "Skipping model pre-download (models will lazy-download on first use)"
        return 0
    fi

    echo "OpenTranscribe requires AI models (~2.9GB) for transcription, speaker diarization, and semantic search."
    echo ""
    echo "Configuration summary:"
    echo "  • Hardware: $DETECTED_DEVICE ($COMPUTE_TYPE precision)"
    echo "  • Whisper Model: $WHISPER_MODEL"
    echo "  • HuggingFace Token: $([[ -n "$HUGGINGFACE_TOKEN" ]] && echo "✓ Configured" || echo "✗ Not configured")"
    echo ""

    # If HuggingFace token not set, offer one more chance to enter it
    if [ -z "$HUGGINGFACE_TOKEN" ]; then
        print_warning "HuggingFace token not configured"
        echo ""
        echo "Without a token, speaker diarization will not work and models cannot be pre-downloaded."
        echo ""
        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${RED}REMINDER: You need BOTH steps completed:${NC}"
        echo "  1. HuggingFace token (Read permissions)"
        echo "  2. Accept BOTH gated model agreements:"
        echo "     • https://huggingface.co/pyannote/segmentation-3.0"
        echo "     • https://huggingface.co/pyannote/speaker-diarization-3.1"
        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        read -p "Would you like to enter your HuggingFace token now? (y/N) " -n 1 -r </dev/tty
        echo
        echo

        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo "Please enter your HuggingFace token:"
            echo "(Token will be hidden for security)"
            read -s HUGGINGFACE_TOKEN </dev/tty
            echo

            # Strip any 'HUGGINGFACE_TOKEN=' prefix if user pasted it
            HUGGINGFACE_TOKEN="${HUGGINGFACE_TOKEN#HUGGINGFACE_TOKEN=}"

            # Validate and update .env file
            if [[ -n "$HUGGINGFACE_TOKEN" ]]; then
                # Update the token in .env file
                sed -i.bak "s|HUGGINGFACE_TOKEN=.*|HUGGINGFACE_TOKEN=$HUGGINGFACE_TOKEN|g" .env
                rm -f .env.bak

                if [[ "$HUGGINGFACE_TOKEN" =~ ^hf_ ]]; then
                    print_success "HuggingFace token configured and saved to .env!"
                    echo ""
                    echo -e "${YELLOW}⚠️  FINAL REMINDER:${NC} Ensure you accepted BOTH model agreements:"
                    echo "   • pyannote/segmentation-3.0"
                    echo "   • pyannote/speaker-diarization-3.1"
                    echo ""
                else
                    print_warning "Token doesn't start with 'hf_' - this may not be valid"
                    echo "Continuing anyway..."
                    echo ""
                fi
            else
                print_warning "No token entered - skipping model pre-download"
                echo ""
                HUGGINGFACE_TOKEN=""
            fi
        fi
    fi

    # If still no token, skip download
    if [ -z "$HUGGINGFACE_TOKEN" ]; then
        print_info "Skipping model pre-download"
        echo ""
        echo "Models will be downloaded automatically when you first run the application."
        echo "This will cause a 10-30 minute delay on first use."
        echo ""
        echo "To pre-download models later:"
        echo "  1. Add your HuggingFace token to .env file"
        echo "  2. Run: cd $PROJECT_DIR && bash scripts/download-models.sh"
        echo ""
        read -p "Press Enter to continue setup..." -r </dev/tty
        echo
        return 0
    fi

    # Token is configured - proceed with download
    echo -e "${YELLOW}Ready to download AI models (~2.9GB)${NC}"
    echo "This will take 10-30 minutes depending on your internet speed."
    echo ""
    read -p "Start model download now? (Y/n) " -n 1 -r </dev/tty
    echo
    echo

    if [[ $REPLY =~ ^[Nn]$ ]]; then
        print_info "Skipping model pre-download"
        echo "Models will be downloaded automatically when you first run the application."
        echo ""
        echo "To download models later, run:"
        echo "  cd $PROJECT_DIR && bash scripts/download-models.sh"
        echo ""
        return 0
    fi

    # Check if scripts exist
    if [ ! -f "scripts/download-models.sh" ]; then
        print_warning "Model download script not found - skipping pre-download"
        echo "Models will be downloaded automatically when you first run the application."
        echo ""
        return 0
    fi

    print_info "Starting model download..."
    echo ""

    # Export necessary environment variables
    export HUGGINGFACE_TOKEN
    export WHISPER_MODEL
    export COMPUTE_TYPE
    export DETECTED_DEVICE
    export GPU_DEVICE_ID
    export OPENSEARCH_MODELS
    export OPENSEARCH_NEURAL_MODEL

    # Create models directory structure with proper permissions
    print_info "Creating model cache directories with proper permissions..."

    # Create main directory and subdirectories
    mkdir -p models/huggingface models/torch models/nltk_data models/sentence-transformers models/opensearch-ml

    # Set ownership to prevent permission issues in non-root containers
    # Container runs as UID 1000, so we need to ensure host directories are accessible
    if [ "$(id -u)" -eq 0 ]; then
        # Running as root - explicitly set ownership to UID 1000 for container compatibility
        echo "  Detected root user - setting ownership to UID 1000 for container compatibility"
        chown -R 1000:1000 models
    else
        # Running as regular user - ensure current user owns the directories
        current_uid=$(id -u)
        current_gid=$(id -g)
        echo "  Setting ownership to current user (UID:GID $current_uid:$current_gid)"
        chown -R "$current_uid:$current_gid" models 2>/dev/null || true
    fi

    # Set proper permissions (755 for directories)
    chmod -R 755 models

    # Verify directories are writable
    if [ -w models/huggingface ] && [ -w models/torch ] && [ -w models/nltk_data ] && [ -w models/sentence-transformers ] && [ -w models/opensearch-ml ]; then
        echo "✓ Model cache directories created with proper permissions"
    else
        print_warning "Model directories exist but may not be writable"
        echo "  If you encounter permission errors, run: ./scripts/fix-model-permissions.sh"
    fi

    # Run the download script and capture exit status
    # Important: Use || true to prevent script exit on download failure
    set +e  # Temporarily disable exit on error
    bash scripts/download-models.sh models
    local download_exit_code=$?
    set -e  # Re-enable exit on error

    echo ""

    # Check download result and provide clear feedback
    if [ $download_exit_code -eq 0 ]; then
        print_success "✨ Models downloaded and cached successfully!"
        print_info "Docker containers will start with models ready to use"
        echo ""
        return 0
    else
        print_error "⚠️  ⚠️  CRITICAL: Model download failed"
        echo ""
        echo -e "${RED}╔══════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${RED}║  WITHOUT PYANNOTE MODELS, TRANSCRIPTION WILL NOT WORK!           ║${NC}"
        echo -e "${RED}╚══════════════════════════════════════════════════════════════════╝${NC}"
        echo ""
        echo -e "${YELLOW}What this means:${NC}"
        echo -e "  ${RED}• The transcription pipeline REQUIRES speaker diarization models${NC}"
        echo -e "  ${RED}• Without PyAnnote models, ALL transcriptions will FAIL${NC}"
        echo "  • Models will auto-download on first use (10-30 minute delay)"
        echo "  • First transcription attempt may fail if models can't download"
        echo ""
        echo -e "${YELLOW}Most common cause: Missing gated model access${NC}"
        echo "  You likely have NOT accepted BOTH PyAnnote model agreements"
        echo ""
        echo -e "${CYAN}To fix before starting:${NC}"
        echo "  1. Accept both model agreements (URLs shown above)"
        echo "  2. Wait 1-2 minutes for permissions to propagate"
        echo "  3. Run: cd $PROJECT_DIR && bash scripts/download-models.sh models"
        echo ""
        echo -e "${YELLOW}Or continue and fix later:${NC}"
        echo "  • Models will download when you first transcribe a file"
        echo "  • Ensure you've accepted model agreements before first use"
        echo ""

        # Ask if user wants to continue or abort with default to NO
        read -p "Continue setup WITHOUT models? (y/N): " continue_choice </dev/tty
        if [[ ! $continue_choice =~ ^[Yy]$ ]]; then
            print_error "Setup aborted - please fix model access and run setup again"
            echo ""
            echo "Quick fix steps:"
            echo "  1. Accept: https://huggingface.co/pyannote/segmentation-3.0"
            echo "  2. Accept: https://huggingface.co/pyannote/speaker-diarization-3.1"
            echo "  3. Wait 1-2 minutes"
            echo "  4. Run setup again"
            exit 1
        fi

        print_warning "⚠️  Continuing WITHOUT models - transcription will not work until models download"
        echo ""
        return 0  # Return success to allow setup to continue
    fi
}



#######################
# FINAL VALIDATION
#######################

validate_setup() {
    echo -e "${BLUE}✅ Validating setup...${NC}"

    # Check required files
    local required_files=(".env" "docker-compose.yml" "docker-compose.prod.yml" "opentranscribe.sh")
    for file in "${required_files[@]}"; do
        if [ -f "$file" ]; then
            echo "✓ $file exists"
        else
            echo -e "${RED}❌ $file missing${NC}"
            echo "Required file not found: $file"
            exit 1
        fi
    done

    # Validate Docker Compose (use production overlay) - now that .env exists
    echo "Validating Docker Compose configuration with .env file..."

    # For one-line installation, build contexts (./backend, ./frontend) don't exist
    # since we're using pre-built images from Docker Hub. We'll check if the
    # configuration can be parsed and validated for image-only deployment.
    local compose_error
    compose_error=$(docker compose -f docker-compose.yml -f docker-compose.prod.yml config 2>&1)
    local exit_code=$?

    # Check for specific errors vs warnings
    if [ $exit_code -eq 0 ]; then
        echo "✓ Docker Compose configuration valid (with .env file)"
    elif echo "$compose_error" | grep -q "build context.*does not exist"; then
        # Build contexts don't exist - this is expected for one-line installation
        echo "✓ Docker Compose configuration valid (using pre-built images)"
        echo "  Note: Build contexts not present (expected for Docker Hub deployment)"
    else
        # Real configuration error
        echo -e "${RED}❌ Docker Compose configuration validation failed${NC}"
        echo -e "${YELLOW}Error details:${NC}"
        echo "$compose_error" | head -15
        echo ""
        echo "This usually means:"
        echo "  1. Missing or invalid environment variables in .env"
        echo "  2. Syntax errors in docker-compose files"
        echo "  3. Missing required files referenced in docker-compose"
        echo "  4. Backend failed to apply Alembic migrations"
        echo ""
        if is_unattended; then
            ot_log_unattended "Aborting on compose validation failure (unattended mode)"
            exit 1
        fi
        read -p "Continue setup anyway? (y/N) " -n 1 -r </dev/tty
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
        echo "⚠️  Continuing with potentially invalid configuration..."
    fi

    echo "✓ Setup validation complete"
}

pull_docker_images() {
    print_header "Pulling Latest Docker Images"

    print_info "Pulling latest OpenTranscribe container images..."
    print_info "This ensures you have the newest features and fixes"
    echo ""

    # Pull images explicitly to ensure latest versions (use production overlay)
    if docker compose -f docker-compose.yml -f docker-compose.prod.yml pull; then
        print_success "Docker images pulled successfully"
        return 0
    else
        print_warning "Failed to pull some images - will use cached versions"
        print_info "You can manually pull images later with: docker compose -f docker-compose.yml -f docker-compose.prod.yml pull"
        return 1
    fi
}

display_summary() {
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}🎉  OpenTranscribe Setup Complete!${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
    echo ""

    # Configuration Summary (show first, above the fold)
    echo -e "${BLUE}📋 Configuration Summary${NC}"
    echo "┌─ Hardware:"
    echo "│  • Platform: $DETECTED_PLATFORM ($ARCH)"
    if [[ "$FORCE_CPU" == "true" ]]; then
        echo "│  • Device: cpu ($COMPUTE_TYPE precision) — forced via --cpu / OPENTRANSCRIBE_FORCE_CPU"
    else
        echo "│  • Device: $DETECTED_DEVICE ($COMPUTE_TYPE precision)"
    fi
    if [[ "$DETECTED_DEVICE" == "cuda" ]]; then
        if command -v nvidia-smi &> /dev/null; then
            GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader,nounits -i "${GPU_DEVICE_ID:-0}" 2>/dev/null || echo "Unknown")
            GPU_MEMORY=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i "${GPU_DEVICE_ID:-0}" 2>/dev/null || echo "Unknown")
            echo "│  • GPU: $GPU_NAME (${GPU_MEMORY}MB) [Device ID: ${GPU_DEVICE_ID:-0}]"
        fi
    fi
    echo "│"
    echo "└─ Application:"
    echo "   • Whisper Model: $WHISPER_MODEL"
    echo "   • Speaker Diarization: $([[ -n "$HUGGINGFACE_TOKEN" ]] && echo "✅ Enabled" || echo "⚠️  Not configured")"
    echo "   • LLM Provider: ${LLM_PROVIDER:-vllm}"
    # Show HTTPS status with more detail based on SSL_CONFIGURED
    if [[ -n "$NGINX_SERVER_NAME" ]]; then
        if [[ "$SSL_CONFIGURED" == "true" ]]; then
            echo "   • HTTPS/SSL: ✅ Ready ($NGINX_SERVER_NAME)"
        else
            echo "   • HTTPS/SSL: ⚠️  Hostname set ($NGINX_SERVER_NAME) - certificates pending"
        fi
    else
        echo "   • HTTPS/SSL: ⚠️  Not configured"
    fi
    echo "   • Project Location: $PROJECT_DIR"
    echo ""

    # CPU-mode advisory: explain the trade-offs and how to undo this choice.
    # Emitted whenever the running stack will use CPU for transcription —
    # whether the user passed --cpu or no GPU was detected.
    if [[ "$DETECTED_DEVICE" == "cpu" ]]; then
        echo -e "${YELLOW}╔══════════════════════════════════════════════════╗${NC}"
        echo -e "${YELLOW}║  🧮  CPU-Only Mode — Performance Notes          ║${NC}"
        echo -e "${YELLOW}╚══════════════════════════════════════════════════╝${NC}"
        if [[ "$FORCE_CPU" == "true" ]]; then
            echo "   • Reason: forced via --cpu / OPENTRANSCRIBE_FORCE_CPU"
        else
            echo "   • Reason: no NVIDIA GPU detected on this host"
        fi
        echo "   • Whisper model: ${WHISPER_MODEL}"
        echo "       (CPU is too slow for large-v3-turbo — base/small recommended)"
        echo "   • Diarization (speaker labels): DISABLED"
        echo "       (PyAnnote requires CUDA — leaving it on would crash workers)"
        echo ""
        echo "   To re-enable diarization later (slow but functional on a"
        echo "   strong CPU): edit .env, set ENABLE_DIARIZATION=true, restart."
        echo "   To switch this install to GPU later: edit .env, set"
        echo "   FORCE_CPU_MODE=false, ensure nvidia-smi works, restart."
        echo ""
    fi

    # QUICK START section (show last so users see it without scrolling)
    echo ""
    echo -e "${YELLOW}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║  🚀  QUICK START - Get Up and Running Now!      ║${NC}"
    echo -e "${YELLOW}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${GREEN}1. Navigate to the project directory:${NC}"
    echo -e "   ${BLUE}cd $PROJECT_DIR${NC}"
    echo ""
    echo -e "${GREEN}2. Start OpenTranscribe:${NC}"
    echo -e "   ${BLUE}./opentranscribe.sh start${NC}"
    echo ""
    echo -e "${GREEN}3. Wait 30-60 seconds for services to initialize${NC}"
    echo ""
    echo -e "${GREEN}4. Open your browser and visit:${NC}"
    if [[ -n "$NGINX_SERVER_NAME" ]]; then
        echo -e "   ${BLUE}https://$NGINX_SERVER_NAME${NC}"
        echo ""
        echo -e "${YELLOW}   Note: Add '$NGINX_SERVER_NAME' to your DNS or /etc/hosts${NC}"
    else
        echo -e "   ${BLUE}http://localhost:${FRONTEND_PORT:-5173}${NC}"
    fi
    echo ""
    echo -e "${GREEN}5. Login with default credentials:${NC}"
    echo -e "   Email:    ${BLUE}admin@example.com${NC}"
    echo -e "   Password: ${BLUE}password${NC}"
    echo -e "   ${RED}⚠️  Change password after first login!${NC}"
    echo ""
    echo -e "${YELLOW}════════════════════════════════════════════════════${NC}"
    echo ""

    # Access URLs
    echo -e "${BLUE}🌐 Service URLs (after starting)${NC}"
    if [[ -n "$NGINX_SERVER_NAME" ]]; then
        echo "  🔒 HTTPS Mode (via NGINX reverse proxy)"
        echo "  • Web Interface:     https://$NGINX_SERVER_NAME"
        echo "  • Documentation:     https://$NGINX_SERVER_NAME/docs/"
        echo "  • API:               https://$NGINX_SERVER_NAME/api"
        echo "  • API Documentation: https://$NGINX_SERVER_NAME/api/docs"
        echo "  • Task Monitor:      https://$NGINX_SERVER_NAME/flower/"
        echo "  • MinIO Console:     https://$NGINX_SERVER_NAME/minio/"
    else
        echo "  • Web Interface:     http://localhost:${FRONTEND_PORT:-5173}"
        echo "  • Documentation:     http://localhost:${FRONTEND_PORT:-5173}/docs/"
        echo "  • API Documentation: http://localhost:${BACKEND_PORT:-5174}/docs"
        echo "  • Task Monitor:      http://localhost:${FLOWER_PORT:-5175}/flower"
        echo "  • MinIO Console:     http://localhost:${MINIO_CONSOLE_PORT:-5179}"
    fi
    echo ""

    # Management commands
    echo -e "${BLUE}📚 Useful Management Commands${NC}"
    echo -e "  ${BLUE}./opentranscribe.sh status${NC}  - Check service status"
    echo -e "  ${BLUE}./opentranscribe.sh logs${NC}    - View logs (Ctrl+C to exit)"
    echo -e "  ${BLUE}./opentranscribe.sh restart${NC} - Restart all services"
    echo -e "  ${BLUE}./opentranscribe.sh stop${NC}    - Stop all services"
    echo -e "  ${BLUE}./opentranscribe.sh help${NC}    - Show all commands"
    echo ""

    # Optional setup notices (collapsed, less prominent)
    if [[ -z "$HUGGINGFACE_TOKEN" ]]; then
        echo -e "${YELLOW}💡 Optional: Enable Speaker Diarization${NC}"
        echo "   To identify who said what in transcripts:"
        echo "   1. Get free token: https://huggingface.co/settings/tokens"
        echo "   2. Edit .env file: HUGGINGFACE_TOKEN=your_token_here"
        echo "   3. Run: ./opentranscribe.sh restart"
        echo ""
    fi

    if [[ -z "$VLLM_BASE_URL" && "$LLM_PROVIDER" == "vllm" ]]; then
        echo -e "${YELLOW}💡 Optional: Enable AI Summarization${NC}"
        echo "   To get AI-powered transcript summaries:"
        echo "   1. Set up LLM server (vLLM, Ollama, OpenAI, etc.)"
        echo "   2. Edit .env file and configure LLM_* variables"
        echo "   3. Run: ./opentranscribe.sh restart"
        echo ""
    fi

    if [[ "$DETECTED_DEVICE" == "cuda" && "$DOCKER_RUNTIME" != "nvidia" ]]; then
        echo -e "${YELLOW}⚠️  Note: NVIDIA GPU detected but runtime not fully configured${NC}"
        echo "   If you experience GPU issues:"
        echo "   https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
        echo ""
    fi

    # SSL certificate reminder — shown whenever HTTPS is configured so users
    # don't wonder why their browser shows a security warning on first visit.
    if [[ -n "$NGINX_SERVER_NAME" && "$SSL_CONFIGURED" == "true" ]]; then
        echo -e "${YELLOW}🔒 SSL Certificate — Browser Trust Required${NC}"
        echo "   Before your first visit, trust the self-signed certificate:"
        echo ""
        echo "   Chrome/Chromium:  chrome://settings/certificates → Authorities → Import"
        echo "   Firefox:          about:preferences#privacy → Certificates → View → Authorities → Import"
        echo ""
        echo "   Certificate file: $PROJECT_DIR/nginx/ssl/server.crt"
        echo ""
        echo "   Or add to /etc/hosts / router DNS:"
        echo "   $(hostname -I | awk '{print $1}')  $NGINX_SERVER_NAME"
        echo ""
    fi
}

#######################
# MAIN EXECUTION
#######################

prompt_start() {
    # Skip in unattended mode
    [[ -n "${OPENTRANSCRIBE_UNATTENDED:-}" ]] && return 0

    echo ""
    local answer
    read -r -p "Start OpenTranscribe now? (Y/n): " answer </dev/tty
    answer="${answer:-Y}"
    if [[ "$answer" =~ ^[Yy] ]]; then
        echo ""
        echo -e "${BLUE}Starting OpenTranscribe...${NC}"
        cd "$PROJECT_DIR" || return 1
        ./opentranscribe.sh start
        echo ""
        if [[ -n "$NGINX_SERVER_NAME" && "$SSL_CONFIGURED" == "true" ]]; then
            echo -e "${GREEN}✅ OpenTranscribe is starting up!${NC}"
            echo -e "${YELLOW}   (allow 30-60 seconds for all services to initialize)${NC}"
            echo ""
            echo -e "${YELLOW}🔒 Before opening your browser, complete these two steps:${NC}"
            echo ""
            echo -e "   ${BLUE}Step 1 — Trust the SSL certificate:${NC}"
            echo "   Chrome:  chrome://settings/certificates → Authorities → Import"
            echo "   Firefox: about:preferences#privacy → View Certificates → Authorities → Import"
            echo "   File:    $PROJECT_DIR/nginx/ssl/server.crt"
            echo ""
            echo -e "   ${BLUE}Step 2 — Add to /etc/hosts (or router DNS):${NC}"
            echo "   $(hostname -I | awk '{print $1}')  $NGINX_SERVER_NAME"
            echo "   Command: echo '$(hostname -I | awk '{print $1}')  $NGINX_SERVER_NAME' | sudo tee -a /etc/hosts"
            echo ""
            echo -e "   Then visit: ${BLUE}https://$NGINX_SERVER_NAME${NC}"
        else
            echo -e "${GREEN}✅ OpenTranscribe is starting up at http://localhost:${FRONTEND_PORT:-5173}${NC}"
            echo -e "${YELLOW}   (allow 30-60 seconds for all services to initialize)${NC}"
        fi
    else
        echo ""
        echo "When you're ready, start with:"
        echo -e "   ${BLUE}cd $PROJECT_DIR && ./opentranscribe.sh start${NC}"
    fi
}

main() {
    # Parse command-line arguments. Currently the only supported flag is
    # --cpu, which forces CPU-only mode and skips NVIDIA/GPU detection.
    # Equivalent to setting OPENTRANSCRIBE_FORCE_CPU=1 in the environment.
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --cpu)
                FORCE_CPU="true"
                shift
                ;;
            -h|--help)
                echo "OpenTranscribe installer"
                echo ""
                echo "Usage: setup-opentranscribe.sh [--cpu]"
                echo ""
                echo "Options:"
                echo "  --cpu       Install in CPU-only mode. Skips NVIDIA GPU"
                echo "              detection and configures the stack to run"
                echo "              without the GPU compose overlay. Use this on"
                echo "              hosts that have the NVIDIA Container Toolkit"
                echo "              installed but no working GPU passthrough"
                echo "              (e.g. WSL2 without a WSL-capable driver), or"
                echo "              on any machine where you simply do not want"
                echo "              GPU acceleration."
                echo "  -h, --help  Show this help message."
                echo ""
                echo "Environment variables (see script header for the full list):"
                echo "  OPENTRANSCRIBE_FORCE_CPU  Non-empty = same as --cpu"
                echo "  OPENTRANSCRIBE_UNATTENDED Non-empty = skip all prompts"
                echo "  OPENTRANSCRIBE_BRANCH     Git branch for downloaded files"
                exit 0
                ;;
            *)
                echo -e "${YELLOW}⚠️  Unknown argument: $1 (ignored)${NC}"
                shift
                ;;
        esac
    done

    if [[ "$FORCE_CPU" == "true" ]]; then
        echo -e "${BLUE}ℹ️  CPU-only install mode selected${NC}"
    fi

    # Auto-enable unattended mode when /dev/tty is unavailable (CI, Docker without -it, etc.)
    # The interactive read prompts all use </dev/tty; without it they would produce an
    # obscure error rather than a helpful message.
    if [[ ! -e /dev/tty ]] && ! is_unattended; then
        echo -e "${YELLOW}⚠️  No terminal device (/dev/tty) detected — enabling unattended mode.${NC}"
        echo "   Pre-set environment variables to customise defaults (see script header)."
        echo "   Example: OPENTRANSCRIBE_UNATTENDED=1 HUGGINGFACE_TOKEN=hf_xxx bash setup-opentranscribe.sh"
        echo ""
        export OPENTRANSCRIBE_UNATTENDED=1
    fi

    # Run setup steps
    detect_platform
    check_dependencies
    # Note: check_network_connectivity is already called inside check_dependencies()
    configure_docker_runtime
    setup_project_directory
    create_configuration_files
    configure_environment
    configure_https_settings
    download_ai_models
    validate_setup
    pull_docker_images
    display_summary
    prompt_start
}

# Execute main function
main "$@"
