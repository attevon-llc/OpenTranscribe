#!/bin/bash

# OpenTranscribe Utility Script
# A comprehensive script for all OpenTranscribe operations
# Usage: ./opentr.sh [command] [options]

# Source common functions
# shellcheck source=scripts/common.sh
source ./scripts/common.sh

# Load environment variables from .env if present
if [ -f ".env" ]; then
  set -a
  # shellcheck source=.env
  source ./.env
  set +a
fi

# Export APP_VERSION so docker compose can pass it through to containers
# (used instead of ./VERSION file bind-mount to avoid OCI stub creation in dev mode)
export APP_VERSION
APP_VERSION=$(cat VERSION 2>/dev/null || echo "unknown")

#######################
# HELPER FUNCTIONS
#######################

# Display help menu
show_help() {
  echo "🚀 OpenTranscribe Utility Script"
  echo "-------------------------------"
  echo "Usage: ./opentr.sh [command] [options]"
  echo ""
  echo "Basic Commands:"
  echo "  start [dev|prod] [options]             - Start the application (dev mode by default)"
  echo "  stop                                   - Stop OpenTranscribe containers"
  echo "  status                                 - Show container status"
  echo "  logs [service]                         - View logs (all services by default)"
  echo ""
  echo "Start/Reset Options:"
  echo "  --build              - Build prod images locally (test before push)"
  echo "  --pull               - Force pull prod images from Docker Hub"
  echo "  --gpu-scale          - Enable multi-GPU worker scaling (multiple workers on one GPU)"
  echo "  --with-gpu-split     - Enable GPU split: separate gpu-transcribe / gpu-diarize workers"
  echo "  --nas                - Use custom storage paths (NAS for media, NVMe for DB/search)"
  echo "  --lite               - Cloud-only ASR mode (no GPU required)"
  echo "  --cpu                - CPU-only mode (local transcription, no GPU overlay)"
  echo "  --with-pki           - Enable PKI certificate authentication (PROD MODE ONLY - requires nginx)"
  echo "  --with-ldap-test     - Start LDAP test container (dev or prod)"
  echo "  --with-keycloak-test - Start Keycloak test container (dev or prod)"
  echo ""
  echo "Reset & Database Commands:"
  echo "  reset [dev|prod] [options]             - Reset and reinitialize (deletes all data!)"
  echo "                                           (Accepts same options as 'start' command)"
  echo "  backup              - Create a database backup"
  echo "  restore [file]      - Restore database from backup"
  echo ""
  echo "Development Commands:"
  echo "  restart-backend     - Restart backend, all celery workers, celery-beat & flower without database reset"
  echo "  restart-frontend    - Restart frontend without affecting backend services"
  echo "  restart-all         - Restart all services without resetting database"
  echo "  rebuild-backend [--nas]  - Rebuild backend services with code changes"
  echo "                             (pass --nas on NAS/NVMe deployments; auto-detected"
  echo "                             from MINIO_NAS_PATH/POSTGRES_DATA_PATH/OPENSEARCH_DATA_PATH"
  echo "                             env vars). --no-deps protects postgres/minio/opensearch."
  echo "  rebuild-frontend         - Rebuild frontend with code changes (--no-deps)"
  echo "  shell [service]     - Open a shell in a container"
  echo "  build               - Rebuild all containers without starting"
  echo ""
  echo "Cleanup Commands:"
  echo "  remove              - Stop containers and remove data volumes"
  echo "  purge               - Remove everything including images (most destructive)"
  echo ""
  echo "Advanced Commands:"
  echo "  health              - Check health status of all services"
  echo "  help                - Show this help menu"
  echo ""
  echo "Benchmark Commands (isolated from NAS data):"
  echo "  bench start [master|branch|current|<name>]- Wipe bench volumes, switch branch, start bench stack (default: current)"
  echo "  bench stop                               - Stop bench stack (keep volumes)"
  echo "  bench clean                              - Stop bench stack and wipe all bench volumes"
  echo "  bench run [output.csv] [fixtures_dir]    - Run upload-speed benchmark on current branch"
  echo "  bench engine                             - Run engine split-stage benchmarks (Phase 2 gate)"
  echo "  bench status                             - Show bench containers, GPU state, volumes"
  echo "  bench compare <master.csv> <branch.csv>  - Print side-by-side speedup table"
  echo ""
  echo "HTTPS/SSL Setup (for microphone recording from other devices):"
  echo "  1. Generate certificates: ./scripts/generate-ssl-cert.sh opentranscribe.local --auto-ip"
  echo "  2. Add to .env: NGINX_SERVER_NAME=opentranscribe.local"
  echo "  3. Start normally: ./opentr.sh start dev"
  echo "  See docs/NGINX_SETUP.md for full instructions"
  echo ""
  echo "Examples:"
  echo "  ./opentr.sh start                            # Start in development mode"
  echo "  ./opentr.sh start dev --gpu-scale            # Dev with multi-GPU scaling (parallel workers)"
  echo "  ./opentr.sh start dev --gpu-scale --nas      # Multi-GPU + NAS/NVMe storage"
  echo "  ./opentr.sh start dev --with-gpu-split       # Split transcribe/diarize across two GPUs"
  echo "  ./opentr.sh start dev --lite                 # Cloud-only ASR mode (no GPU)"
  echo "  ./opentr.sh start dev --cpu                  # Local CPU-only (skip GPU overlay)"
  echo "  ./opentr.sh start dev --with-ldap-test       # Dev with LDAP test container"
  echo "  ./opentr.sh start dev --with-keycloak-test   # Dev with Keycloak test container"
  echo "  ./opentr.sh start prod                       # Production (pulls from Docker Hub)"
  echo "  ./opentr.sh start prod --build               # Production with local build (test before push)"
  echo "  ./opentr.sh start prod --build --with-pki    # Production with PKI (requires nginx)"
  echo "  ./opentr.sh reset dev                        # Reset development environment"
  echo "  ./opentr.sh reset dev --lite                 # Reset in cloud-only ASR mode"
  echo "  ./opentr.sh logs backend                     # View backend logs"
  echo "  ./opentr.sh restart-backend                  # Restart backend services only"
  echo ""
}

# Build production images locally (backend + frontend)
build_prod_images() {
  echo "🥽 Building production Docker images locally..."

  echo "🧱 Building backend image (davidamacey/opentranscribe-backend:latest)..."
  docker build -t davidamacey/opentranscribe-backend:latest -f backend/Dockerfile.prod backend || {
    echo "❌ Backend image build failed"
    exit 1
  }

  echo "🧱 Building frontend image (davidamacey/opentranscribe-frontend:latest)..."
  docker build -t davidamacey/opentranscribe-frontend:latest -f frontend/Dockerfile.prod frontend || {
    echo "❌ Frontend image build failed"
    exit 1
  }

  echo "🧱 Building docs image (davidamacey/opentranscribe-docs:latest)..."
  docker build --build-arg DOCS_BASE_URL=/docs/ -t davidamacey/opentranscribe-docs:latest docs-site || {
    echo "❌ Docs image build failed"
    exit 1
  }

  echo "✅ Local production images built successfully"
}

# Function to detect and configure hardware
detect_and_configure_hardware() {
  echo "🔍 Detecting hardware configuration..."

  # Detect platform
  PLATFORM=$(uname -s | tr '[:upper:]' '[:lower:]')
  ARCH=$(uname -m)

  # Initialize default values
  export TORCH_DEVICE="auto"
  export COMPUTE_TYPE="auto"
  export USE_GPU="auto"
  export DOCKER_RUNTIME=""
  export BACKEND_DOCKERFILE="Dockerfile.prod"
  export BUILD_ENV="development"

  # Check for NVIDIA GPU
  if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
    echo "✅ NVIDIA GPU detected"
    export DOCKER_RUNTIME="nvidia"
    export TORCH_DEVICE="cuda"
    export COMPUTE_TYPE="float16"
    export USE_GPU="true"

    # Check for NVIDIA Container Toolkit (efficient method)
    if docker info 2>/dev/null | grep -q nvidia; then
      echo "✅ NVIDIA Container Toolkit available"

      # Detect Blackwell architecture (compute capability 12.x)
      local compute_cap
      compute_cap=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null \
        | head -1 | tr -d '[:space:]')
      if [[ "$compute_cap" == 12.* ]]; then
        export IS_BLACKWELL_GPU="true"
        echo "✅ Blackwell GPU detected (SM_${compute_cap//./_})"
      else
        export IS_BLACKWELL_GPU=""
      fi
    else
      echo "⚠️  NVIDIA GPU detected but Container Toolkit not available"
      echo "   Falling back to CPU mode"
      export DOCKER_RUNTIME=""
      export TORCH_DEVICE="cpu"
      export COMPUTE_TYPE="int8"
      export USE_GPU="false"
    fi
  elif [[ "$PLATFORM" == "darwin" && "$ARCH" == "arm64" ]]; then
    echo "✅ Apple Silicon detected"
    export TORCH_DEVICE="mps"
    export COMPUTE_TYPE="float32"
    export USE_GPU="false"
  else
    echo "ℹ️  Using CPU processing"
    export TORCH_DEVICE="cpu"
    export COMPUTE_TYPE="int8"
    export USE_GPU="false"
  fi

  # Set additional environment variables
  TARGETPLATFORM="linux/$([[ "$ARCH" == "arm64" ]] && echo "arm64" || echo "amd64")"
  export TARGETPLATFORM

  echo "📋 Hardware Configuration:"
  echo "  Platform: $PLATFORM"
  echo "  Architecture: $ARCH"
  echo "  Device: $TORCH_DEVICE"
  echo "  Compute Type: $COMPUTE_TYPE"
  echo "  Docker Runtime: ${DOCKER_RUNTIME:-default}"
}

# Add GPU compose overlay(s) to COMPOSE_FILES.
# Handles Blackwell detection — must be called after detect_environment().
# Usage: add_gpu_overlay
add_gpu_overlay() {
  if [ "$DOCKER_RUNTIME" != "nvidia" ]; then
    return
  fi

  if [ -n "$IS_BLACKWELL_GPU" ] && [ -f "docker-compose.blackwell.yml" ]; then
    # Blackwell overlay includes GPU resources AND image override — no need
    # for docker-compose.gpu.yml on top.
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.blackwell.yml"
    echo "🎯 Adding Blackwell GPU overlay (SM_12x detected)"
  elif [ -f "docker-compose.gpu.yml" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.gpu.yml"
    echo "🎯 Adding GPU overlay (docker-compose.gpu.yml) for NVIDIA acceleration"
  fi
}

# Append the NAS/NVMe storage overlay to $COMPOSE_FILES if requested explicitly
# via --nas OR auto-detected from custom storage path env vars. This mirrors
# the block inside start_app so rebuild-backend/rebuild-frontend can keep
# NAS-mounted deployments pointing at their real data paths.
add_nas_overlay() {
  # Auto-detect when storage path env vars are set (same rule as start).
  if [ -z "$NAS_FLAG" ] && { [ -n "$MINIO_NAS_PATH" ] || [ -n "$POSTGRES_DATA_PATH" ] || [ -n "$OPENSEARCH_DATA_PATH" ]; }; then
    NAS_FLAG="--nas"
    echo "ℹ️  Auto-detected custom storage paths in .env, enabling NAS overlay"
  fi
  if [ -z "$NAS_FLAG" ]; then
    return
  fi
  if [ ! -f "docker-compose.nas.yml" ]; then
    echo "⚠️  --nas specified but docker-compose.nas.yml not found"
    return
  fi
  NAS_PATH="${MINIO_NAS_PATH:-/mnt/nas/opentranscribe-minio}"
  PG_PATH="${POSTGRES_DATA_PATH:-/mnt/nvm/opentranscribe/pg}"
  OS_PATH="${OPENSEARCH_DATA_PATH:-/mnt/nvm/opentranscribe/os}"
  # Sanity-check the NAS mount is reachable. DB/OS paths are NVMe-local and
  # almost always present; NAS is the one that can silently unmount.
  if [ ! -d "$NAS_PATH" ]; then
    echo "❌ NAS path not accessible: $NAS_PATH"
    echo "   Ensure NAS is mounted and set MINIO_NAS_PATH in .env"
    exit 1
  fi
  COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.nas.yml"
  echo "💾 Adding custom storage overlay (docker-compose.nas.yml)"
  echo "   MinIO media:  $NAS_PATH"
  echo "   PostgreSQL:   $PG_PATH"
  echo "   OpenSearch:   $OS_PATH"
}

# Function to start the environment
start_app() {
  ENVIRONMENT=${1:-dev}
  shift || true  # Remove first argument

  # Parse optional flags
  BUILD_FLAG=""
  GPU_SCALE_FLAG=""
  GPU_SPLIT_FLAG=""
  NAS_FLAG=""
  PULL_FLAG=""
  WITH_PKI_FLAG=""
  WITH_LDAP_TEST_FLAG=""
  WITH_KEYCLOAK_TEST_FLAG=""
  LITE_FLAG=""
  CPU_FLAG=""

  while [ $# -gt 0 ]; do
    case "$1" in
      --build)
        BUILD_FLAG="--build"
        shift
        ;;
      --pull)
        PULL_FLAG="--pull"
        shift
        ;;
      --gpu-scale)
        GPU_SCALE_FLAG="--gpu-scale"
        shift
        ;;
      --with-gpu-split)
        GPU_SPLIT_FLAG="--with-gpu-split"
        shift
        ;;
      --nas)
        NAS_FLAG="--nas"
        shift
        ;;
      --lite)
        LITE_FLAG="--lite"
        shift
        ;;
      --cpu)
        CPU_FLAG="--cpu"
        shift
        ;;
      --with-pki)
        WITH_PKI_FLAG="--with-pki"
        shift
        ;;
      --with-ldap-test)
        WITH_LDAP_TEST_FLAG="--with-ldap-test"
        shift
        ;;
      --with-keycloak-test)
        WITH_KEYCLOAK_TEST_FLAG="--with-keycloak-test"
        shift
        ;;
      *)
        echo "⚠️  Unknown flag: $1"
        shift
        ;;
    esac
  done

  # PKI requires production mode (nginx with mTLS)
  if [ -n "$WITH_PKI_FLAG" ] && [ "$ENVIRONMENT" = "dev" ]; then
    echo "❌ Error: PKI authentication requires production mode (nginx with mTLS)"
    echo "   Use: ./opentr.sh start prod --build --with-pki"
    echo ""
    echo "   PKI cannot work in dev mode because:"
    echo "   - Dev mode uses Vite dev server (no nginx)"
    echo "   - PKI requires nginx to verify client certificates (mTLS)"
    echo "   - Certificate headers must be set by nginx, not the browser"
    exit 1
  fi

  if [ -n "$GPU_SCALE_FLAG" ] && [ -n "$GPU_SPLIT_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-scale,gpu-split"
  elif [ -n "$GPU_SCALE_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-scale"
  elif [ -n "$GPU_SPLIT_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-split"
  fi

  echo "🚀 Starting OpenTranscribe in ${ENVIRONMENT} mode..."

  if [ -n "$GPU_SCALE_FLAG" ]; then
    echo "🎯 Multi-GPU scaling enabled"
  fi

  if [ -n "$GPU_SPLIT_FLAG" ]; then
    echo "🔀 GPU split enabled (transcribe → GPU_TRANSCRIBE_DEVICE_ID=${GPU_TRANSCRIBE_DEVICE_ID:-0}, diarize → GPU_DIARIZE_DEVICE_ID=${GPU_DIARIZE_DEVICE_ID:-1})"
  fi

  if [ -n "$LITE_FLAG" ]; then
    echo "☁️  Lite mode enabled (cloud-only ASR, no GPU required)"
  fi

  if [ -n "$CPU_FLAG" ]; then
    echo "🧮 CPU-only mode enabled (local CPU transcription, no GPU overlay)"
  fi

  # Ensure Docker is running
  check_docker

  # Detect and configure hardware (skipped in lite/cpu modes — no GPU needed)
  if [ -n "$CPU_FLAG" ] || [ -n "$LITE_FLAG" ]; then
    if [ -n "$CPU_FLAG" ]; then
      echo "ℹ️  Skipping GPU detection (--cpu mode: local CPU transcription only)"
    else
      echo "ℹ️  Skipping GPU detection (lite mode uses cloud ASR providers)"
    fi
    export DOCKER_RUNTIME=""
    export TORCH_DEVICE="cpu"
    export COMPUTE_TYPE="int8"
    export USE_GPU="false"
  else
    detect_and_configure_hardware
  fi

  # Set build environment
  export BUILD_ENV="$ENVIRONMENT"

  # Create necessary directories
  create_required_dirs

  # Fix model cache permissions for non-root container
  fix_model_cache_permissions

  # Ensure OpenSearch neural models are downloaded for offline capability
  ensure_opensearch_models

  # Build compose file list based on environment and flags
  COMPOSE_FILES="-f docker-compose.yml"

  if [ "$ENVIRONMENT" = "prod" ]; then
    # Production: Use base + prod override files
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.prod.yml"

    # Note: Database schema is managed by Alembic migrations on backend startup

    if [ "$PULL_FLAG" = "--pull" ]; then
      echo "⬇️  Forcing pull of latest production images from Docker Hub..."
      # shellcheck disable=SC2086
      docker compose $COMPOSE_FILES pull || {
        echo "❌ Failed to pull production images"
        exit 1
      }
    fi

    if [ "$BUILD_FLAG" = "--build" ]; then
      echo "🔄 Starting services in PRODUCTION mode with LOCAL BUILD (testing before push)..."
      echo "⚠️  Building backend and frontend images locally instead of pulling from Docker Hub"
      build_prod_images
      # Add local override to prevent pulling from Docker Hub (overrides pull_policy: always)
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.local.yml"
      BUILD_CMD=""
    else
      echo "🔄 Starting services in PRODUCTION mode (pulling from Docker Hub)..."
      BUILD_CMD=""
    fi
  else
    # Development: Auto-loads docker-compose.override.yml (always builds)
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.override.yml"
    echo "🔄 Starting services in DEVELOPMENT mode (auto-loads docker-compose.override.yml)..."
    BUILD_CMD="--build"
  fi

  # Add GPU overlay if NVIDIA GPU is detected and Container Toolkit is available
  add_gpu_overlay

  # Add GPU scaling overlay if requested
  if [ -n "$GPU_SCALE_FLAG" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.gpu-scale.yml"
    echo "🎯 Adding GPU scaling overlay (docker-compose.gpu-scale.yml)"
  fi
  # gpu-split workers (celery-worker-gpu-transcribe / celery-worker-gpu-diarize) are defined
  # in docker-compose.yml with profiles: [gpu-split] and activated via COMPOSE_PROFILES above.
  # No extra overlay file is needed.

  # Add NAS/NVMe storage overlay if requested via --nas flag
  # or auto-detect when storage path env vars are set
  if [ -z "$NAS_FLAG" ] && { [ -n "$MINIO_NAS_PATH" ] || [ -n "$POSTGRES_DATA_PATH" ] || [ -n "$OPENSEARCH_DATA_PATH" ]; }; then
    NAS_FLAG="--nas"
    echo "ℹ️  Auto-detected custom storage paths in .env, enabling NAS overlay"
  fi
  if [ -n "$NAS_FLAG" ]; then
    if [ -f "docker-compose.nas.yml" ]; then
      # Validate required directories exist
      NAS_PATH="${MINIO_NAS_PATH:-/mnt/nas/opentranscribe-minio}"
      PG_PATH="${POSTGRES_DATA_PATH:-/mnt/nvm/opentranscribe/pg}"
      OS_PATH="${OPENSEARCH_DATA_PATH:-/mnt/nvm/opentranscribe/os}"

      # Create directories if they don't exist
      mkdir -p "$NAS_PATH" "$PG_PATH" "$OS_PATH" 2>/dev/null || true

      # Check mount points are accessible
      if [ ! -d "$NAS_PATH" ]; then
        echo "❌ NAS path not accessible: $NAS_PATH"
        echo "   Ensure NAS is mounted and set MINIO_NAS_PATH in .env"
        exit 1
      fi

      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.nas.yml"
      echo "💾 Adding custom storage overlay (docker-compose.nas.yml)"
      echo "   MinIO media:  $NAS_PATH"
      echo "   PostgreSQL:   $PG_PATH"
      echo "   OpenSearch:   $OS_PATH"
    else
      echo "⚠️  --nas specified but docker-compose.nas.yml not found"
    fi
  fi

  # Add lite overlay if requested (cloud-only ASR, no GPU)
  if [ -n "$LITE_FLAG" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.lite.yml"
    echo "☁️  Adding lite overlay (docker-compose.lite.yml)"
  fi

  # Add NGINX reverse proxy if NGINX_SERVER_NAME is set (production only)
  # Dev mode uses Vite dev server directly — nginx would be redundant
  if [ -n "$NGINX_SERVER_NAME" ] && [ "$ENVIRONMENT" = "prod" ]; then
    if [ -f "docker-compose.nginx.yml" ]; then
      # Check for SSL certificates
      CERT_FILE="${NGINX_CERT_FILE:-./nginx/ssl/server.crt}"
      KEY_FILE="${NGINX_CERT_KEY:-./nginx/ssl/server.key}"

      if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
        echo ""
        echo "⚠️  SSL certificates not found!"
        echo "   Expected: $CERT_FILE and $KEY_FILE"
        echo ""
        echo "   Generate certificates with:"
        echo "   ./scripts/generate-ssl-cert.sh $NGINX_SERVER_NAME --auto-ip"
        echo ""
        echo "   Or disable NGINX by commenting out NGINX_SERVER_NAME in .env"
        exit 1
      fi

      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.nginx.yml"
      echo "🔒 Adding NGINX reverse proxy (HTTPS enabled)"
      echo "   Server name: $NGINX_SERVER_NAME"
      echo "   Access URL: https://$NGINX_SERVER_NAME"
    else
      echo "⚠️  NGINX_SERVER_NAME is set but docker-compose.nginx.yml not found"
    fi
  elif [ -n "$NGINX_SERVER_NAME" ] && [ "$ENVIRONMENT" = "dev" ]; then
    echo "ℹ️  NGINX_SERVER_NAME is set but skipped in dev mode (Vite serves frontend directly)"
  fi

  # Add PKI overlay if requested
  if [ -n "$WITH_PKI_FLAG" ]; then
    if [ -f "docker-compose.pki.yml" ]; then
      # Check for PKI certificates
      if [ ! -f "scripts/pki/test-certs/ca/ca.crt" ]; then
        echo "⚠️  PKI certificates not found. Generating test certificates..."
        ./scripts/pki/setup-test-pki.sh || {
          echo "❌ Failed to generate PKI certificates"
          exit 1
        }
      fi

      # Check for server certificate
      if [ ! -f "scripts/pki/test-certs/nginx/server.crt" ] || [ ! -f "scripts/pki/test-certs/nginx/server.key" ]; then
        echo "⚠️  HTTPS server certificate not found. Generating self-signed certificate..."
        cd scripts/pki/test-certs/nginx || exit 1
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
          -keyout server.key -out server.crt \
          -subj "/CN=${PKI_SERVER_NAME:-localhost}" \
          -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" || {
          echo "❌ Failed to generate server certificate"
          exit 1
        }
        cd - > /dev/null || exit 1
      fi

      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.pki.yml"
      echo "🔐 Adding PKI authentication overlay (docker-compose.pki.yml)"
      echo "   Access URL: https://localhost:${PKI_HTTPS_PORT:-5182}"
      echo "   Import client certificate from: scripts/pki/test-certs/clients/"
    else
      echo "⚠️  --with-pki specified but docker-compose.pki.yml not found"
    fi
  fi

  # Add LDAP test container if requested
  if [ -n "$WITH_LDAP_TEST_FLAG" ]; then
    if [ -f "docker-compose.ldap-test.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.ldap-test.yml"
      echo "🔐 Adding LDAP test container (docker-compose.ldap-test.yml)"
      echo "   LDAP server: localhost:3890"
      echo "   Web UI: http://localhost:17170"
    else
      echo "⚠️  --with-ldap-test specified but docker-compose.ldap-test.yml not found"
    fi
  fi

  # Add Keycloak test container if requested
  if [ -n "$WITH_KEYCLOAK_TEST_FLAG" ]; then
    if [ -f "docker-compose.keycloak.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.keycloak.yml"
      echo "🔐 Adding Keycloak test container (docker-compose.keycloak.yml)"
      echo "   Keycloak URL: http://localhost:8180"
      echo "   Admin credentials: admin / admin"
    else
      echo "⚠️  --with-keycloak-test specified but docker-compose.keycloak.yml not found"
    fi
  fi

  # Start services with appropriate compose files
  # shellcheck disable=SC2086
  docker compose $COMPOSE_FILES up -d $BUILD_CMD

  # Fix pipeline_scratch volume permissions (created by compose above) —
  # the volume is root-owned by default, which breaks the shared-memory
  # handoff between CPU preprocess and GPU/embedding workers.
  fix_pipeline_scratch_permissions

  # Display container status
  echo "📊 Container status:"
  # shellcheck disable=SC2086
  docker compose $COMPOSE_FILES ps

  # Print access information
  echo "✅ Services are starting up."
  print_access_info

  # Display log commands
  echo "📋 To view logs, run:"
  echo "- All logs: docker compose logs -f"
  echo "- Backend logs: docker compose logs -f backend"
  echo "- Frontend logs: docker compose logs -f frontend"
  if [ -n "$GPU_SCALE_FLAG" ]; then
    echo "- GPU scaled workers: docker compose logs -f celery-worker-gpu-scaled"
  elif [ -n "$GPU_SPLIT_FLAG" ]; then
    echo "- GPU transcribe worker: docker compose logs -f celery-worker-gpu-transcribe"
    echo "- GPU diarize worker: docker compose logs -f celery-worker-gpu-diarize"
  elif [ -n "$LITE_FLAG" ]; then
    echo "- Cloud ASR worker logs: docker compose logs -f celery-cloud-worker"
  else
    echo "- Celery worker logs: docker compose logs -f celery-worker"
  fi
  echo "- Celery beat logs: docker compose logs -f celery-beat"

  # Print help information
  print_help_commands
}

# Function to reset and initialize the environment
reset_and_init() {
  ENVIRONMENT=${1:-dev}
  shift || true  # Remove first argument

  # Parse optional flags
  BUILD_FLAG=""
  GPU_SCALE_FLAG=""
  GPU_SPLIT_FLAG=""
  NAS_FLAG=""
  PULL_FLAG=""
  WITH_PKI_FLAG=""
  WITH_LDAP_TEST_FLAG=""
  WITH_KEYCLOAK_TEST_FLAG=""
  LITE_FLAG=""
  CPU_FLAG=""

  while [ $# -gt 0 ]; do
    case "$1" in
      --build)
        BUILD_FLAG="--build"
        shift
        ;;
      --pull)
        PULL_FLAG="--pull"
        shift
        ;;
      --gpu-scale)
        GPU_SCALE_FLAG="--gpu-scale"
        shift
        ;;
      --with-gpu-split)
        GPU_SPLIT_FLAG="--with-gpu-split"
        shift
        ;;
      --nas)
        NAS_FLAG="--nas"
        shift
        ;;
      --lite)
        LITE_FLAG="--lite"
        shift
        ;;
      --cpu)
        CPU_FLAG="--cpu"
        shift
        ;;
      --with-pki)
        WITH_PKI_FLAG="--with-pki"
        shift
        ;;
      --with-ldap-test)
        WITH_LDAP_TEST_FLAG="--with-ldap-test"
        shift
        ;;
      --with-keycloak-test)
        WITH_KEYCLOAK_TEST_FLAG="--with-keycloak-test"
        shift
        ;;
      *)
        echo "⚠️  Unknown flag: $1"
        shift
        ;;
    esac
  done

  # PKI requires production mode (nginx with mTLS)
  if [ -n "$WITH_PKI_FLAG" ] && [ "$ENVIRONMENT" = "dev" ]; then
    echo "❌ Error: PKI authentication requires production mode (nginx with mTLS)"
    echo "   Use: ./opentr.sh reset prod --build --with-pki"
    echo ""
    echo "   PKI cannot work in dev mode because:"
    echo "   - Dev mode uses Vite dev server (no nginx)"
    echo "   - PKI requires nginx to verify client certificates (mTLS)"
    echo "   - Certificate headers must be set by nginx, not the browser"
    exit 1
  fi

  if [ -n "$GPU_SCALE_FLAG" ] && [ -n "$GPU_SPLIT_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-scale,gpu-split"
  elif [ -n "$GPU_SCALE_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-scale"
  elif [ -n "$GPU_SPLIT_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-split"
  fi

  echo "🔄 Running reset and initialize for OpenTranscribe in ${ENVIRONMENT} mode..."

  if [ -n "$GPU_SCALE_FLAG" ]; then
    echo "🎯 Multi-GPU scaling enabled"
  fi

  if [ -n "$GPU_SPLIT_FLAG" ]; then
    echo "🔀 GPU split enabled (transcribe → GPU_TRANSCRIBE_DEVICE_ID=${GPU_TRANSCRIBE_DEVICE_ID:-0}, diarize → GPU_DIARIZE_DEVICE_ID=${GPU_DIARIZE_DEVICE_ID:-1})"
  fi

  if [ -n "$LITE_FLAG" ]; then
    echo "☁️  Lite mode enabled (cloud-only ASR, no GPU required)"
  fi

  if [ -n "$CPU_FLAG" ]; then
    echo "🧮 CPU-only mode enabled (local CPU transcription, no GPU overlay)"
  fi

  # Ensure Docker is running
  check_docker

  # Detect and configure hardware (skipped in lite/cpu modes — no GPU needed)
  if [ -n "$CPU_FLAG" ] || [ -n "$LITE_FLAG" ]; then
    if [ -n "$CPU_FLAG" ]; then
      echo "ℹ️  Skipping GPU detection (--cpu mode: local CPU transcription only)"
    else
      echo "ℹ️  Skipping GPU detection (lite mode uses cloud ASR providers)"
    fi
    export DOCKER_RUNTIME=""
    export TORCH_DEVICE="cpu"
    export COMPUTE_TYPE="int8"
    export USE_GPU="false"
  else
    detect_and_configure_hardware
  fi

  # Set build environment
  export BUILD_ENV="$ENVIRONMENT"

  # Build compose file list based on environment and flags
  COMPOSE_FILES="-f docker-compose.yml"

  if [ "$ENVIRONMENT" = "prod" ]; then
    # Production: Use base + prod override files
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.prod.yml"
    # Note: Database schema is managed by Alembic migrations on backend startup

    if [ "$PULL_FLAG" = "--pull" ]; then
      echo "⬇️  Forcing pull of latest production images from Docker Hub..."
      # shellcheck disable=SC2086
      docker compose $COMPOSE_FILES pull || {
        echo "❌ Failed to pull production images"
        exit 1
      }
    fi

    if [ "$BUILD_FLAG" = "--build" ]; then
      echo "🔄 Resetting in PRODUCTION mode with LOCAL BUILD (testing before push)..."
      echo "⚠️  Building backend and frontend images locally instead of pulling from Docker Hub"
      build_prod_images
      # Add local override to prevent pulling from Docker Hub (overrides pull_policy: always)
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.local.yml"
      BUILD_CMD=""
    else
      echo "🔄 Resetting in PRODUCTION mode (pulling from Docker Hub)..."
      BUILD_CMD=""
    fi
  else
    # Development: Auto-loads docker-compose.override.yml (always builds)
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.override.yml"
    echo "🔄 Resetting in DEVELOPMENT mode (auto-loads docker-compose.override.yml)..."
    BUILD_CMD="--build"
  fi

  # Add GPU overlay if NVIDIA GPU is detected and Container Toolkit is available
  add_gpu_overlay

  # Add GPU scaling overlay if requested
  if [ -n "$GPU_SCALE_FLAG" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.gpu-scale.yml"
    echo "🎯 Adding GPU scaling overlay (docker-compose.gpu-scale.yml)"
  fi
  # gpu-split workers (celery-worker-gpu-transcribe / celery-worker-gpu-diarize) are defined
  # in docker-compose.yml with profiles: [gpu-split] and activated via COMPOSE_PROFILES above.
  # No extra overlay file is needed.

  # Add NAS/NVMe storage overlay if requested via --nas flag
  # or auto-detect when storage path env vars are set
  if [ -z "$NAS_FLAG" ] && { [ -n "$MINIO_NAS_PATH" ] || [ -n "$POSTGRES_DATA_PATH" ] || [ -n "$OPENSEARCH_DATA_PATH" ]; }; then
    NAS_FLAG="--nas"
    echo "ℹ️  Auto-detected custom storage paths in .env, enabling NAS overlay"
  fi
  if [ -n "$NAS_FLAG" ]; then
    if [ -f "docker-compose.nas.yml" ]; then
      # Validate required directories exist
      NAS_PATH="${MINIO_NAS_PATH:-/mnt/nas/opentranscribe-minio}"
      PG_PATH="${POSTGRES_DATA_PATH:-/mnt/nvm/opentranscribe/pg}"
      OS_PATH="${OPENSEARCH_DATA_PATH:-/mnt/nvm/opentranscribe/os}"

      # Create directories if they don't exist
      mkdir -p "$NAS_PATH" "$PG_PATH" "$OS_PATH" 2>/dev/null || true

      # Check mount points are accessible
      if [ ! -d "$NAS_PATH" ]; then
        echo "❌ NAS path not accessible: $NAS_PATH"
        echo "   Ensure NAS is mounted and set MINIO_NAS_PATH in .env"
        exit 1
      fi

      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.nas.yml"
      echo "💾 Adding custom storage overlay (docker-compose.nas.yml)"
      echo "   MinIO media:  $NAS_PATH"
      echo "   PostgreSQL:   $PG_PATH"
      echo "   OpenSearch:   $OS_PATH"
    else
      echo "⚠️  --nas specified but docker-compose.nas.yml not found"
    fi
  fi

  # Add lite overlay if requested (cloud-only ASR, no GPU)
  if [ -n "$LITE_FLAG" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.lite.yml"
    echo "☁️  Adding lite overlay (docker-compose.lite.yml)"
  fi

  # Add NGINX reverse proxy if NGINX_SERVER_NAME is set (production only)
  # Dev mode uses Vite dev server directly — nginx would be redundant
  if [ -n "$NGINX_SERVER_NAME" ] && [ "$ENVIRONMENT" = "prod" ]; then
    if [ -f "docker-compose.nginx.yml" ]; then
      # Check for SSL certificates
      CERT_FILE="${NGINX_CERT_FILE:-./nginx/ssl/server.crt}"
      KEY_FILE="${NGINX_CERT_KEY:-./nginx/ssl/server.key}"

      if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
        echo ""
        echo "⚠️  SSL certificates not found!"
        echo "   Expected: $CERT_FILE and $KEY_FILE"
        echo ""
        echo "   Generate certificates with:"
        echo "   ./scripts/generate-ssl-cert.sh $NGINX_SERVER_NAME --auto-ip"
        echo ""
        echo "   Or disable NGINX by commenting out NGINX_SERVER_NAME in .env"
        exit 1
      fi

      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.nginx.yml"
      echo "🔒 Adding NGINX reverse proxy (HTTPS enabled)"
      echo "   Server name: $NGINX_SERVER_NAME"
      echo "   Access URL: https://$NGINX_SERVER_NAME"
    else
      echo "⚠️  NGINX_SERVER_NAME is set but docker-compose.nginx.yml not found"
    fi
  elif [ -n "$NGINX_SERVER_NAME" ] && [ "$ENVIRONMENT" = "dev" ]; then
    echo "ℹ️  NGINX_SERVER_NAME is set but skipped in dev mode (Vite serves frontend directly)"
  fi

  # Add PKI overlay if requested
  if [ -n "$WITH_PKI_FLAG" ]; then
    if [ -f "docker-compose.pki.yml" ]; then
      # Check for PKI certificates
      if [ ! -f "scripts/pki/test-certs/ca/ca.crt" ]; then
        echo "⚠️  PKI certificates not found. Generating test certificates..."
        ./scripts/pki/setup-test-pki.sh || {
          echo "❌ Failed to generate PKI certificates"
          exit 1
        }
      fi

      # Check for server certificate
      if [ ! -f "scripts/pki/test-certs/nginx/server.crt" ] || [ ! -f "scripts/pki/test-certs/nginx/server.key" ]; then
        echo "⚠️  HTTPS server certificate not found. Generating self-signed certificate..."
        cd scripts/pki/test-certs/nginx || exit 1
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
          -keyout server.key -out server.crt \
          -subj "/CN=${PKI_SERVER_NAME:-localhost}" \
          -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" || {
          echo "❌ Failed to generate server certificate"
          exit 1
        }
        cd - > /dev/null || exit 1
      fi

      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.pki.yml"
      echo "🔐 Adding PKI authentication overlay (docker-compose.pki.yml)"
      echo "   Access URL: https://localhost:${PKI_HTTPS_PORT:-5182}"
      echo "   Import client certificate from: scripts/pki/test-certs/clients/"
    else
      echo "⚠️  --with-pki specified but docker-compose.pki.yml not found"
    fi
  fi

  # Add LDAP test container if requested
  if [ -n "$WITH_LDAP_TEST_FLAG" ]; then
    if [ -f "docker-compose.ldap-test.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.ldap-test.yml"
      echo "🔐 Adding LDAP test container (docker-compose.ldap-test.yml)"
      echo "   LDAP server: localhost:3890"
      echo "   Web UI: http://localhost:17170"
    else
      echo "⚠️  --with-ldap-test specified but docker-compose.ldap-test.yml not found"
    fi
  fi

  # Add Keycloak test container if requested
  if [ -n "$WITH_KEYCLOAK_TEST_FLAG" ]; then
    if [ -f "docker-compose.keycloak.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.keycloak.yml"
      echo "🔐 Adding Keycloak test container (docker-compose.keycloak.yml)"
      echo "   Keycloak URL: http://localhost:8180"
      echo "   Admin credentials: admin / admin"
    else
      echo "⚠️  --with-keycloak-test specified but docker-compose.keycloak.yml not found"
    fi
  fi

  echo "🛑 Stopping all containers and removing volumes..."
  # shellcheck disable=SC2086
  docker compose $COMPOSE_FILES down -v

  # Create necessary directories
  create_required_dirs

  # Fix model cache permissions for non-root container
  fix_model_cache_permissions

  # Ensure OpenSearch neural models are downloaded for offline capability
  ensure_opensearch_models

  # Start all services - docker compose handles dependency ordering via depends_on
  echo "🚀 Starting all services..."
  # shellcheck disable=SC2086
  docker compose $COMPOSE_FILES up -d $BUILD_CMD

  # Fix pipeline_scratch volume permissions (recreated after reset).
  fix_pipeline_scratch_permissions

  # Wait for backend to be ready for database operations
  echo "⏳ Waiting for backend to be ready..."
  wait_for_backend_health

  # Note: Database tables, admin user, default tags, and system prompts are
  # automatically created by Alembic migrations and initial_data.py on backend startup
  # (runs on first container start when postgres_data volume is empty after 'down -v')

  echo "✅ Setup complete!"

  # Print access information
  print_access_info
}

# Function to backup the database
backup_database() {
  TIMESTAMP=$(date +%Y%m%d_%H%M%S)
  BACKUP_FILE="opentranscribe_backup_${TIMESTAMP}.sql"

  echo "📦 Creating database backup: ${BACKUP_FILE}..."
  mkdir -p ./backups

  if docker compose exec -T postgres pg_dump -U postgres opentranscribe > "./backups/${BACKUP_FILE}"; then
    echo "✅ Backup created successfully: ./backups/${BACKUP_FILE}"
  else
    echo "❌ Backup failed."
    exit 1
  fi
}

# Function to restore database from backup
restore_database() {
  BACKUP_FILE=$1

  if [ -z "$BACKUP_FILE" ]; then
    echo "❌ Error: Backup file not specified."
    echo "Usage: ./opentr.sh restore [backup_file]"
    exit 1
  fi

  if [ ! -f "$BACKUP_FILE" ]; then
    echo "❌ Error: Backup file not found: $BACKUP_FILE"
    exit 1
  fi

  echo "🔄 Restoring database from ${BACKUP_FILE}..."

  # Stop services that use the database
  docker compose stop backend celery-worker celery-download-worker celery-cpu-worker celery-nlp-worker celery-embedding-worker celery-beat

  # Restore the database
  if docker compose exec -T postgres psql -U postgres opentranscribe < "$BACKUP_FILE"; then
    echo "✅ Database restored successfully."
    echo "🔄 Restarting services..."
    docker compose start backend celery-worker celery-download-worker celery-cpu-worker celery-nlp-worker celery-embedding-worker celery-beat
  else
    echo "❌ Database restore failed."
    echo "🔄 Restarting services anyway..."
    docker compose start backend celery-worker celery-download-worker celery-cpu-worker celery-nlp-worker celery-embedding-worker celery-beat
    exit 1
  fi
}

# Function to restart backend services (backend, all celery workers, flower) without database reset
restart_backend() {
  echo "🔄 Restarting backend services (backend, all celery workers, celery-beat, flower)..."

  # Restart backend and all celery services in place
  # Note: celery-worker-gpu-scaled is optional (scale: 0 by default) so we ignore errors for it
  docker compose restart backend \
    celery-worker \
    celery-download-worker \
    celery-cpu-worker \
    celery-nlp-worker \
    celery-embedding-worker \
    celery-beat \
    flower 2>/dev/null

  # Try to restart gpu-scaled worker if it exists (optional service)
  docker compose restart celery-worker-gpu-scaled 2>/dev/null || true

  echo "✅ Backend services restarted successfully."

  # Display container status
  echo "📊 Container status:"
  docker compose ps
}

# Function to restart frontend only
restart_frontend() {
  echo "🔄 Restarting frontend service..."

  # Restart frontend in place
  docker compose restart frontend

  echo "✅ Frontend service restarted successfully."

  # Display container status
  echo "📊 Container status:"
  docker compose ps
}

# Function to restart all services without resetting the database
restart_all() {
  echo "🔄 Restarting all services without database reset..."

  # Restart all services in place - docker compose handles dependency ordering
  docker compose restart

  echo "✅ All services restarted successfully."

  # Display container status
  echo "📊 Container status:"
  docker compose ps
}

# Helper: stop all containers from both dev and prod compose chains, plus stragglers
stop_all_containers() {
  # Dev compose chain
  docker compose -f docker-compose.yml -f docker-compose.override.yml \
    -f docker-compose.gpu.yml -f docker-compose.blackwell.yml \
    -f docker-compose.gpu-scale.yml \
    -f docker-compose.nas.yml "$@" 2>/dev/null || true

  # Prod compose chain
  docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    -f docker-compose.local.yml -f docker-compose.gpu.yml \
    -f docker-compose.blackwell.yml -f docker-compose.gpu-scale.yml \
    -f docker-compose.nas.yml \
    -f docker-compose.nginx.yml -f docker-compose.pki.yml "$@" 2>/dev/null || true

  # Catch stragglers by container name pattern
  for container in $(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E 'opentranscribe-|transcribe-app-'); do
    docker stop "$container" 2>/dev/null && docker rm "$container" 2>/dev/null || true
  done
}

# Function to remove containers and data volumes (but preserve images)
remove_system() {
  echo "🗑️ Stopping containers and removing data volumes..."
  stop_all_containers down -v

  echo "✅ Containers and data volumes removed. Images preserved for faster rebuilds."
}

# Function to purge everything including images (most destructive)
purge_system() {
  echo "💥 Purging ALL OpenTranscribe resources including images..."
  echo "🗑️ Stopping and removing containers, volumes, and images..."
  stop_all_containers down -v --rmi all

  # Remove any remaining OpenTranscribe images
  echo "🗑️ Removing any remaining OpenTranscribe images..."
  docker images --filter "reference=transcribe-app*" -q | xargs -r docker rmi -f
  docker images --filter "reference=*opentranscribe*" -q | xargs -r docker rmi -f

  echo "✅ Complete purge finished. Everything removed."
}

# Function to check health of all services
check_health() {
  echo "🩺 Checking health of all services..."

  # Check if services are running
  docker compose ps

  # Check specific service health if available
  echo "📋 Backend health:"
  docker compose exec -T backend curl -s http://localhost:8080/health || echo "⚠️ Backend health check failed."

  echo "📋 Redis health:"
  docker compose exec -T redis redis-cli ping || echo "⚠️ Redis health check failed."

  echo "📋 Postgres health:"
  docker compose exec -T postgres pg_isready -U postgres || echo "⚠️ Postgres health check failed."

  echo "📋 OpenSearch health:"
  docker compose exec -T opensearch curl -s http://localhost:9200 > /dev/null && echo "OK" || echo "⚠️ OpenSearch health check failed."

  echo "📋 MinIO health:"
  docker compose exec -T minio curl -s http://localhost:9000/minio/health/live > /dev/null && echo "OK" || echo "⚠️ MinIO health check failed."

  echo "📋 Flower health:"
  if docker compose exec -T flower curl -s "http://localhost:5555/${FLOWER_URL_PREFIX:-flower}/healthcheck" > /dev/null 2>&1; then
    echo "OK (http://localhost:${FLOWER_PORT:-5175}/${FLOWER_URL_PREFIX:-flower}/)"
  else
    if docker compose ps flower 2>/dev/null | grep -q "Up"; then
      echo "⚠️ Flower container running but not responding"
    else
      echo "⚠️ Flower not running"
    fi
  fi

  # NGINX health (only if configured)
  if [ -n "$NGINX_SERVER_NAME" ]; then
    echo "📋 NGINX health:"
    if curl -s -k https://localhost:${NGINX_HTTPS_PORT:-443}/health > /dev/null 2>&1 || \
       curl -s http://localhost:${NGINX_HTTP_PORT:-80}/health > /dev/null 2>&1; then
      echo "OK (https://$NGINX_SERVER_NAME)"
    else
      # Check if container is running but not responding
      if docker compose ps nginx 2>/dev/null | grep -q "Up"; then
        echo "⚠️ NGINX running but not responding"
      else
        echo "⚠️ NGINX not running"
      fi
    fi
  fi

  echo "✅ Health check complete."
}

#######################
# MAIN SCRIPT
#######################

# Process commands
if [ $# -eq 0 ]; then
  show_help
  exit 0
fi

# Check Docker is available for all commands
check_docker

# Process the command
case "$1" in
  start)
    shift  # Remove 'start' command
    start_app "$@"  # Pass all remaining arguments
    ;;

  stop)
    echo "🛑 Stopping all containers..."
    # Stop containers from both dev and prod compose chains, plus any stragglers.
    # Using MAX_COMPOSE_FILES with conflicting overlays (prod + override) can fail
    # silently, so we run each chain separately.
    stop_all_containers down
    echo "✅ All containers stopped."
    ;;

  reset)
    shift  # Remove 'reset' command
    echo "⚠️ Warning: This will delete all data! Continue? (y/n)"
    read -r confirm
    if [[ $confirm =~ ^[Yy]$ ]]; then
      reset_and_init "$@"  # Pass all remaining arguments
    else
      echo "❌ Reset cancelled."
    fi
    ;;

  logs)
    SERVICE=${2:-}
    if [ -z "$SERVICE" ]; then
      echo "📋 Showing logs for all services... (press Ctrl+C to exit)"
      docker compose logs -f
    else
      echo "📋 Showing logs for $SERVICE... (press Ctrl+C to exit)"
      docker compose logs -f "$SERVICE"
    fi
    ;;

  status)
    echo "📊 Container status:"
    docker compose ps
    ;;

  shell)
    SERVICE=${2:-backend}
    echo "🔧 Opening shell in $SERVICE container..."
    docker compose exec "$SERVICE" /bin/bash || docker compose exec "$SERVICE" /bin/sh
    ;;

  backup)
    backup_database
    ;;

  restore)
    restore_database "$2"
    ;;

  restart-backend)
    restart_backend
    ;;

  restart-frontend)
    restart_frontend
    ;;

  restart-all)
    restart_all
    ;;

  rebuild-backend)
    echo "🔨 Rebuilding backend services..."
    detect_and_configure_hardware

    # Parse optional flags so NAS-configured deployments keep their mounts.
    NAS_FLAG=""
    shift || true  # drop "rebuild-backend"
    while [ $# -gt 0 ]; do
      case "$1" in
        --nas)
          NAS_FLAG="--nas"
          shift
          ;;
        *)
          echo "⚠️  rebuild-backend: ignoring unknown arg '$1'"
          shift
          ;;
      esac
    done

    # Build compose file list. --no-deps on the docker compose command below
    # prevents cascade recreation of postgres/minio/opensearch, but the
    # overlay order still has to be correct so the backend containers get
    # the right env (e.g. host URLs) under NAS mode.
    COMPOSE_FILES="-f docker-compose.yml -f docker-compose.override.yml"

    # Add GPU overlay if NVIDIA GPU is detected
    add_gpu_overlay

    # Add NAS/NVMe storage overlay (honors --nas or auto-detected env vars).
    # Without this, rebuilt backend containers would come up bound to default
    # docker volumes instead of the user's NAS/NVMe paths -- data would
    # appear missing even though it's still on disk.
    add_nas_overlay

    # --no-deps keeps postgres/minio/opensearch/redis containers exactly as
    # they were running. Only rebuild + recreate the services that actually
    # consume backend code.
    # shellcheck disable=SC2086
    docker compose $COMPOSE_FILES up -d --build --no-deps \
      backend celery-worker celery-download-worker celery-cpu-worker \
      celery-cloud-asr-worker celery-nlp-worker celery-embedding-worker \
      celery-beat flower

    # Fix pipeline_scratch volume permissions in case it was recreated.
    fix_pipeline_scratch_permissions

    echo "✅ Backend services rebuilt successfully."
    ;;

  rebuild-frontend)
    echo "🔨 Rebuilding frontend service..."
    # Frontend doesn't use NAS volumes, but keep --no-deps for symmetry
    # with rebuild-backend so data containers are untouched.
    docker compose up -d --build --no-deps frontend
    echo "✅ Frontend service rebuilt successfully."
    ;;

  remove)
    echo "⚠️ Warning: This will remove all data volumes! Continue? (y/n)"
    read -r confirm
    if [[ $confirm =~ ^[Yy]$ ]]; then
      remove_system
    else
      echo "❌ Remove cancelled."
    fi
    ;;

  purge)
    echo "⚠️ WARNING: This will remove EVERYTHING including images! Continue? (y/n)"
    read -r confirm
    if [[ $confirm =~ ^[Yy]$ ]]; then
      purge_system
    else
      echo "❌ Purge cancelled."
    fi
    ;;

  health)
    check_health
    ;;

  build)
    echo "🔨 Rebuilding containers..."
    detect_and_configure_hardware

    # Build compose file list
    COMPOSE_FILES="-f docker-compose.yml -f docker-compose.override.yml"

    # Add GPU overlay if NVIDIA GPU is detected
    add_gpu_overlay

    # shellcheck disable=SC2086
    docker compose $COMPOSE_FILES build
    echo "✅ Build complete. Use './opentr.sh start' to start the application."
    ;;

  bench)
    # Isolated upload-speed A/B benchmark — never touches NAS data.
    # Uses docker-compose.bench.yml which mounts fresh named volumes for
    # postgres, minio, and opensearch, completely separate from the NAS dataset.
    BENCH_SUBCOMMAND="${2:-help}"
    # NOTE: docker-compose.override.yml is intentionally excluded — it mounts ./backend:/app
    # which makes the running code track the current git branch instead of the built image.
    # docker-compose.bench.yml provides its own build/image definitions without source mounts.
    BENCH_COMPOSE="-f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.bench.yml"
    BENCH_VOLUME_PREFIX="transcribe-app"

    case "$BENCH_SUBCOMMAND" in
      start)
        BENCH_TARGET="${3:-current}"

        if [[ "$BENCH_TARGET" == "master" ]]; then
          TARGET_BRANCH="master"
        elif [[ "$BENCH_TARGET" == "branch" ]]; then
          # Legacy alias for the upload-speed benchmark — kept for back-compat.
          TARGET_BRANCH="feat/upload-speed-improvement"
        elif [[ "$BENCH_TARGET" == "current" || "$BENCH_TARGET" == "." ]]; then
          TARGET_BRANCH="$(git branch --show-current)"
          if [[ -z "$TARGET_BRANCH" ]]; then
            echo "❌ Could not determine current branch (detached HEAD?)"
            exit 1
          fi
        else
          # Treat as a literal branch name — verify it exists locally.
          if git show-ref --verify --quiet "refs/heads/$BENCH_TARGET"; then
            TARGET_BRANCH="$BENCH_TARGET"
          else
            echo "❌ Unknown target '$BENCH_TARGET'. Use 'master', 'branch', 'current', or a local branch name."
            exit 1
          fi
        fi

        echo "🧪 Preparing bench environment for: $BENCH_TARGET ($TARGET_BRANCH)"
        echo ""

        # GPU safety check before touching anything
        if ! nvidia-smi --query-gpu=index,name,memory.used,utilization.gpu \
            --format=csv,noheader 2>/dev/null; then
          echo "❌ nvidia-smi failed — check GPU state before benchmarking. Aborting."
          exit 1
        fi
        echo ""

        # Stop any running bench stack cleanly
        echo "🛑 Stopping any running bench stack..."
        # shellcheck disable=SC2086
        # Use 'down' (not 'stop') so containers are removed before volume rm.
        # docker volume rm fails silently when stopped containers still reference it.
        docker compose $BENCH_COMPOSE down --remove-orphans 2>/dev/null || true

        # Wipe bench volumes so each run starts with an empty DB/MinIO/OpenSearch/Redis
        echo "🗑  Wiping bench volumes for clean run..."
        docker volume rm \
          "${BENCH_VOLUME_PREFIX}_postgres_bench_data" \
          "${BENCH_VOLUME_PREFIX}_minio_bench_data" \
          "${BENCH_VOLUME_PREFIX}_redis_bench_data" \
          "${BENCH_VOLUME_PREFIX}_opensearch_bench_data" \
          "${BENCH_VOLUME_PREFIX}_flower_bench_data" 2>/dev/null || true

        # Switch git branch
        CURRENT_BRANCH="$(git branch --show-current)"
        if [[ "$CURRENT_BRANCH" != "$TARGET_BRANCH" ]]; then
          echo "🔀 Switching from $CURRENT_BRANCH → $TARGET_BRANCH..."
          git checkout "$TARGET_BRANCH" || { echo "❌ git checkout failed"; exit 1; }
        fi

        echo ""
        echo "🚀 Starting bench stack on $TARGET_BRANCH (no NAS, fresh volumes)..."
        # shellcheck disable=SC2086
        docker compose $BENCH_COMPOSE up -d --build

        echo ""
        echo "⏳ Waiting 20 s for healthchecks to settle..."
        sleep 20
        docker ps --format 'table {{.Names}}\t{{.Status}}' | grep opentranscribe
        echo ""
        echo "✅ Bench stack ready on $TARGET_BRANCH."
        if [[ "$TARGET_BRANCH" == "master" ]]; then
          echo "   Run:  ./opentr.sh bench run /tmp/master_full.csv"
        else
          echo "   Run:  ./opentr.sh bench run /tmp/branch_after.csv"
        fi
        ;;

      stop)
        echo "🛑 Stopping bench stack..."
        # shellcheck disable=SC2086
        docker compose $BENCH_COMPOSE stop
        echo "✅ Bench stack stopped. Volumes preserved. Use 'bench clean' to wipe."
        ;;

      clean)
        echo "🗑  Stopping bench stack and wiping all bench volumes..."
        # shellcheck disable=SC2086
        docker compose $BENCH_COMPOSE down --remove-orphans 2>/dev/null || true
        docker volume rm \
          "${BENCH_VOLUME_PREFIX}_postgres_bench_data" \
          "${BENCH_VOLUME_PREFIX}_minio_bench_data" \
          "${BENCH_VOLUME_PREFIX}_redis_bench_data" \
          "${BENCH_VOLUME_PREFIX}_opensearch_bench_data" \
          "${BENCH_VOLUME_PREFIX}_flower_bench_data" 2>/dev/null || true
        echo "✅ Bench volumes wiped."
        ;;

      run)
        OUTPUT_CSV="${3:-}"
        FIXTURES_DIR="${4:-benchmark/test_audio}"

        # Auto-detect which script to use based on current branch
        CURRENT_BRANCH="$(git branch --show-current)"
        if [[ "$CURRENT_BRANCH" == "master" ]]; then
          BENCH_SCRIPT="/tmp/benchmark_upload_baseline.py"
          DEFAULT_CSV="/tmp/master_full_$(date +%Y%m%d_%H%M%S).csv"
        else
          BENCH_SCRIPT="scripts/benchmark_upload_baseline.py"
          DEFAULT_CSV="/tmp/branch_after_$(date +%Y%m%d_%H%M%S).csv"
        fi
        [[ -z "$OUTPUT_CSV" ]] && OUTPUT_CSV="$DEFAULT_CSV"

        if [[ ! -f "$BENCH_SCRIPT" ]]; then
          echo "❌ Benchmark script not found: $BENCH_SCRIPT"
          if [[ "$CURRENT_BRANCH" == "master" ]]; then
            echo "   Copy it first: git show feat/upload-speed-improvement:scripts/benchmark_upload_baseline.py > /tmp/benchmark_upload_baseline.py"
          fi
          exit 1
        fi

        echo "🧪 Upload benchmark — branch: $CURRENT_BRANCH"
        echo "   Script:   $BENCH_SCRIPT"
        echo "   Fixtures: $FIXTURES_DIR"
        echo "   Output:   $OUTPUT_CSV"
        echo ""
        nvidia-smi --query-gpu=index,name,memory.used,utilization.gpu --format=csv,noheader
        echo ""

        # shellcheck disable=SC1091
        source backend/venv/bin/activate
        BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
          python3 "$BENCH_SCRIPT" "$FIXTURES_DIR" "$OUTPUT_CSV"
        ;;

      status)
        echo "=== Bench Containers ==="
        docker ps --format 'table {{.Names}}\t{{.Status}}' | grep opentranscribe || echo "(none running)"
        echo ""
        echo "=== GPU State ==="
        nvidia-smi --query-gpu=index,name,memory.used,utilization.gpu --format=csv,noheader
        echo ""
        echo "=== Bench Volumes ==="
        docker volume ls | grep bench || echo "(none)"
        echo ""
        echo "=== Current Branch ==="
        git branch --show-current
        ;;

      compare)
        MASTER_CSV="${3:-}"
        BRANCH_CSV="${4:-}"
        if [[ -z "$MASTER_CSV" || -z "$BRANCH_CSV" ]]; then
          echo "❌ Usage: ./opentr.sh bench compare <master.csv> <branch.csv>"
          exit 1
        fi
        if [[ ! -f "scripts/compare_baseline.py" ]]; then
          echo "❌ scripts/compare_baseline.py not found — switch to the branch first."
          exit 1
        fi
        # shellcheck disable=SC1091
        source backend/venv/bin/activate
        python3 scripts/compare_baseline.py "$MASTER_CSV" "$BRANCH_CSV"
        ;;

      engine)
        # Run engine split-stage benchmarks on the current branch.
        # Uses the bench stack (fresh named volumes — never touches NAS/prod data).
        # Audio: benchmark/test_audio/ WAV files (mounted read-only inside container).
        # Results: docs/engine-benchmark-results/*.csv
        TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
        AUDIO_DIR="/app/benchmark/test_audio"
        FAST_AUDIO="${AUDIO_DIR}/0.5h_1899s.wav"  # 30-minute file for single-stage timing
        SINGLE_CSV="engine_single_${TIMESTAMP}.csv"
        QUEUE_CSV="engine_queue_${TIMESTAMP}.csv"
        RESULTS_DIR="docs/engine-benchmark-results"
        WORKER="opentranscribe-celery-worker"

        echo "🔬 Engine benchmark — branch: $(git branch --show-current)"
        echo "   Using bench stack (fresh volumes, never touches NAS/prod data)"
        echo ""

        # GPU safety check
        if ! nvidia-smi --query-gpu=index,name,memory.used,utilization.gpu \
            --format=csv,noheader 2>/dev/null; then
          echo "❌ nvidia-smi failed — check GPU state before benchmarking. Aborting."
          exit 1
        fi
        echo ""

        # Wipe bench volumes for a clean slate
        echo "🗑  Wiping bench volumes for clean run..."
        # shellcheck disable=SC2086
        docker compose $BENCH_COMPOSE down --remove-orphans 2>/dev/null || true
        docker volume rm \
          "${BENCH_VOLUME_PREFIX}_postgres_bench_data" \
          "${BENCH_VOLUME_PREFIX}_minio_bench_data" \
          "${BENCH_VOLUME_PREFIX}_redis_bench_data" \
          "${BENCH_VOLUME_PREFIX}_opensearch_bench_data" \
          "${BENCH_VOLUME_PREFIX}_flower_bench_data" 2>/dev/null || true

        # Build and start bench stack on current branch.
        # Build only the services needed for engine benchmarks — skip docs/frontend
        # (they don't affect AI processing and docs has an MDX build step that can
        # fail on unrelated content changes).
        echo "🚀 Building bench stack from current branch (backend + infra only)..."
        # shellcheck disable=SC2086
        docker compose $BENCH_COMPOSE build \
          backend celery-worker celery-download-worker celery-cpu-worker \
          celery-cloud-asr-worker celery-nlp-worker celery-embedding-worker \
          celery-beat flower
        # shellcheck disable=SC2086
        docker compose $BENCH_COMPOSE up -d --no-build \
          postgres redis minio opensearch \
          backend celery-worker celery-download-worker celery-cpu-worker \
          celery-cloud-asr-worker celery-nlp-worker celery-embedding-worker \
          celery-beat flower

        echo ""
        echo "⏳ Waiting 45 s for stack to be ready (DB migrations, model pre-load)..."
        sleep 45
        docker ps --format 'table {{.Names}}\t{{.Status}}' | grep opentranscribe

        # Verify the worker is up
        if ! docker ps --format '{{.Names}}' | grep -q "^${WORKER}$"; then
          echo "❌ Worker container '${WORKER}' not running — check logs."
          # shellcheck disable=SC2086
          docker compose $BENCH_COMPOSE logs --tail=30 celery-worker
          exit 1
        fi

        echo ""
        echo "▶  [1/2] Per-stage latency (3 runs on 0.5h file)..."
        docker exec -e PYTHONPATH=/app "$WORKER" \
          python /app/scripts/benchmark_engine_single.py \
            --audio "$FAST_AUDIO" \
            --runs 3 \
            --output "/tmp/${SINGLE_CSV}"

        echo ""
        echo "▶  [2/2] Queue throughput (concurrency=3, max 5 files)..."
        docker exec -e PYTHONPATH=/app "$WORKER" \
          python /app/scripts/benchmark_engine_queue.py \
            --audio-dir "$AUDIO_DIR" \
            --max-files 5 \
            --concurrency 3 \
            --output "/tmp/${QUEUE_CSV}"

        # Copy results out
        mkdir -p "$RESULTS_DIR"
        docker cp "${WORKER}:/tmp/${SINGLE_CSV}" "${RESULTS_DIR}/${SINGLE_CSV}"
        docker cp "${WORKER}:/tmp/${QUEUE_CSV}"  "${RESULTS_DIR}/${QUEUE_CSV}"

        echo ""
        echo "✅ Results saved to:"
        echo "   ${RESULTS_DIR}/${SINGLE_CSV}"
        echo "   ${RESULTS_DIR}/${QUEUE_CSV}"
        echo ""
        echo "📊 Gate criteria:"
        echo "   Stage 1 (preprocess):            < 30 s per file"
        echo "   Stage 2 GPU (transcribe+diarize): ≥ 20× realtime"
        echo "   Stage 3 (finalize):              <  5 s per file"
        echo "   GPU idle between tasks (conc=3):  <  5 s"
        echo ""
        echo "🛑 Stopping bench stack (volumes kept for inspection)..."
        # shellcheck disable=SC2086
        docker compose $BENCH_COMPOSE stop
        ;;

      help|*)
        echo "🧪 Benchmark subcommands (isolated from NAS data):"
        echo "  bench start [master|branch|current|<name>]- Wipe bench volumes, switch branch, start bench stack (default: current)"
        echo "  bench stop                               - Stop bench stack (keep volumes)"
        echo "  bench clean                              - Stop bench stack and wipe all bench volumes"
        echo "  bench run [output.csv] [fixtures_dir]    - Run upload-speed benchmark on current branch"
        echo "  bench engine                             - Run engine split-stage benchmarks (Phase 2 gate)"
        echo "  bench status                             - Show bench containers, GPU state, volumes"
        echo "  bench compare <master.csv> <branch.csv>  - Print side-by-side speedup table"
        ;;
    esac
    ;;

  help|--help|-h)
    show_help
    ;;

  *)
    echo "❌ Unknown command: $1"
    show_help
    exit 1
    ;;
esac

exit 0
