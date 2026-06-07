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
  echo "  stop [--fresh [name]]                  - Stop OpenTranscribe containers (or a fresh deployment)"
  echo "  status [--fresh [name]]                - Show container status (or a fresh deployment)"
  echo "  logs [service]                         - View logs (all services by default)"
  echo "  data-paths                             - Print resolved live data locations (check before deleting!)"
  echo ""
  echo "Fresh Deployment Commands (isolated, guard-railed — never touch real data):"
  echo "  start dev --fresh [name] [--port-offset N] [--seed-benchmark]"
  echo "                                         - Start an isolated dev stack (own project + volumes)"
  echo "  stop --fresh [name]                    - Stop a fresh deployment (volumes kept)"
  echo "  status --fresh [name]                  - Status of a fresh deployment"
  echo "  fresh-list                             - List all fresh deployments + their volumes"
  echo "  fresh-destroy <name>                   - Remove a fresh deployment (containers+volumes, confirmed)"
  echo ""
  echo "Start/Reset Options:"
  echo "  --build              - Build prod images locally (test before push)"
  echo "  --pull               - Force pull prod images from Docker Hub"
  echo "  --gpu-scale          - Enable multi-GPU worker scaling (multiple workers on one GPU)"
  echo "  --with-gpu-split     - Enable GPU split: separate gpu-transcribe / gpu-diarize workers"
  echo "  --nas                - Use custom storage paths (NAS for media, NVMe for DB/search)"
  echo "  --no-nas             - Suppress the auto-loaded NAS overlay (use Docker named volumes)"
  echo "  --fresh [name]       - Isolated dev deployment: own project + named volumes, NAS"
  echo "                         overlay NEVER loaded, real data untouched (dev mode only)"
  echo "  --port-offset N      - With --fresh: offset every published port by N (run side-by-side)"
  echo "  --seed-benchmark     - With --fresh: upload small benchmark media once healthy"
  echo "  --dry-run            - Print the compose files + command that WOULD run; start nothing"
  echo "  --lite               - Cloud-only ASR mode (no GPU required)"
  echo "  --cpu                - CPU-only mode (local transcription, no GPU overlay)"
  echo "  --with-pki           - Enable PKI certificate authentication (PROD MODE ONLY - requires nginx)"
  echo "  --with-ldap-test     - Start LDAP test container (dev or prod)"
  echo "  --with-keycloak-test - Start Keycloak test container (dev or prod)"
  echo "  --with-watch         - Mount the host watch folder (WATCH_HOST_PATH, default ./watch) for auto-import"
  echo "  --with-smb-test      - Start a Samba test share for watch-source testing (localhost:4450)"
  echo "  --with-monitoring    - Start Prometheus (:5186) + Grafana (:5185) observability stack"
  echo ""
  echo "Reset & Database Commands:"
  echo "  reset [dev|prod] [options]             - Reset and reinitialize (deletes all data!)"
  echo "                                           (Accepts same options as 'start' command)"
  echo "  backup [--encrypt]  - Create a database backup (--encrypt: GPG AES-256, no plaintext on disk)"
  echo "  restore [file]      - Restore database from backup (.sql or .gpg)"
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

#######################
# FRESH DEPLOYMENT HELPERS
#######################
# A "fresh" deployment is a fully isolated stack: its own compose project name
# (otfresh-<name>) so containers AND named volumes never collide with the real
# dev/prod deployment, the NAS overlay is NEVER loaded (so the live bind-mounted
# data at MINIO_NAS_PATH/POSTGRES_DATA_PATH/OPENSEARCH_DATA_PATH is untouched),
# and container_name collisions with the hard-coded opentranscribe-* names are
# resolved by a generated overlay (.fresh/<name>.yml) that re-pins every
# container_name to otfresh-<name>-*.

# Directory holding generated per-deployment overlays (gitignored).
FRESH_OVERLAY_DIR=".fresh"

# Services in docker-compose.yml that hard-code a container_name. These must be
# re-pinned in fresh mode so `docker exec opentranscribe-postgres` can't hit the
# wrong container and teardown stays deterministic. The gpu-split/gpu-scale
# workers already use ${COMPOSE_PROJECT_NAME}-* in the base file, so the project
# name namespaces those automatically — they're intentionally omitted here.
FRESH_NAMED_SERVICES=(
  postgres minio redis opensearch backend
  celery-worker celery-download-worker celery-cpu-worker celery-redaction
  celery-cloud-asr-worker celery-nlp-worker celery-embedding-worker celery-beat
  frontend flower docs
)

# Sanitize a fresh deployment name to a safe slug (lowercase alnum + dashes).
fresh_sanitize_name() {
  local raw="${1:-default}"
  local slug
  slug="$(echo "$raw" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-//;s/-$//')"
  echo "${slug:-default}"
}

# Project name for a fresh deployment.
fresh_project_name() {
  echo "otfresh-$(fresh_sanitize_name "$1")"
}

# Generate (idempotently) the container_name override overlay for a fresh
# deployment and echo its path. Re-pins every hard-coded container_name to
# otfresh-<name>-* so there is zero collision with the real opentranscribe-*
# containers. Compose cannot UNSET container_name via an overlay, so we set an
# explicit per-service value instead.
fresh_generate_overlay() {
  local name="$1"
  local proj
  proj="$(fresh_project_name "$name")"
  mkdir -p "$FRESH_OVERLAY_DIR"
  local file="${FRESH_OVERLAY_DIR}/${name}.yml"
  {
    echo "# AUTO-GENERATED by opentr.sh for fresh deployment '${name}'."
    echo "# Re-pins every hard-coded container_name to ${proj}-* so this"
    echo "# isolated stack never collides with the real opentranscribe-* stack."
    echo "# Safe to delete; regenerated on demand. Do NOT edit by hand."
    echo "services:"
    local svc
    for svc in "${FRESH_NAMED_SERVICES[@]}"; do
      echo "  ${svc}:"
      echo "    container_name: ${proj}-${svc}"
    done
  } > "$file"
  echo "$file"
}

# Apply a published-port offset to COMPOSE_FILES via a generated overlay so a
# fresh stack can run side-by-side with the main stack. Echoes the overlay path.
# offset is added to every default host port; container ports are unchanged.
fresh_generate_port_overlay() {
  local name="$1"
  local offset="$2"
  mkdir -p "$FRESH_OVERLAY_DIR"
  local file="${FRESH_OVERLAY_DIR}/${name}-ports.yml"
  {
    echo "# AUTO-GENERATED by opentr.sh — port offset +${offset} for fresh '${name}'."
    echo "services:"
    echo "  postgres:"
    echo "    ports: [\"$((5176 + offset)):5432\"]"
    echo "  redis:"
    echo "    ports: [\"$((5177 + offset)):6379\"]"
    echo "  minio:"
    echo "    ports: [\"$((5178 + offset)):9000\", \"$((5179 + offset)):9001\"]"
    echo "  opensearch:"
    echo "    ports: [\"$((5180 + offset)):9200\", \"$((5181 + offset)):9600\"]"
    echo "  backend:"
    echo "    ports: [\"$((5174 + offset)):8080\"]"
    echo "  flower:"
    echo "    ports: [\"$((5175 + offset)):5555\"]"
    echo "  frontend:"
    echo "    ports: [\"$((5173 + offset)):5173\"]"
    echo "  docs:"
    echo "    ports: [\"$((5183 + offset)):8080\"]"
  } > "$file"
  echo "$file"
}

# The default published host ports the dev stack binds. Used to refuse a fresh
# start when the main stack is already up (zero offset).
FRESH_DEFAULT_PORTS=(5173 5174 5175 5176 5177 5178 5179 5180 5181)

# Return 0 if a TCP port is already bound on localhost, 1 otherwise.
fresh_port_in_use() {
  local port="$1"
  # bash /dev/tcp probe — no netstat/ss dependency.
  (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
  return 1
}

# Compute the resolved live data paths (NAS overlay active or not) and print
# them. Shared by the `data-paths` subcommand and the guardrail marker writer.
# Sets globals: DP_NAS_ACTIVE, DP_NAS_PATH, DP_PG_PATH, DP_OS_PATH.
resolve_data_paths() {
  DP_NAS_PATH="${MINIO_NAS_PATH:-}"
  DP_PG_PATH="${POSTGRES_DATA_PATH:-}"
  DP_OS_PATH="${OPENSEARCH_DATA_PATH:-}"
  if [ -n "$DP_NAS_PATH" ] || [ -n "$DP_PG_PATH" ] || [ -n "$DP_OS_PATH" ]; then
    DP_NAS_ACTIVE="true"
  else
    DP_NAS_ACTIVE="false"
  fi
}

# Best-effort write of a live-data marker into each bind data dir so humans AND
# cleanup agents can see the directory is in use before deleting it. Never fails
# startup — a read-only or missing dir is silently skipped.
write_live_data_markers() {
  local marker=".opentranscribe-live-data"
  local content="LIVE DATA — bind-mounted into the OpenTranscribe stack. DO NOT delete or 'clean up'. Managed by opentr.sh. See ./opentr.sh data-paths."
  local dir
  # Also mark the PARENTS of the pg/os dirs: the 2026-06 data-loss incident was
  # an `rm -rf` of the parent (/mnt/nvm/opentranscribe), not the leaf dirs.
  for dir in "$MINIO_NAS_PATH" "$POSTGRES_DATA_PATH" "$OPENSEARCH_DATA_PATH" \
             "${POSTGRES_DATA_PATH:+$(dirname "$POSTGRES_DATA_PATH")}" \
             "${OPENSEARCH_DATA_PATH:+$(dirname "$OPENSEARCH_DATA_PATH")}"; do
    [ -z "$dir" ] && continue
    [ "$dir" = "/" ] && continue
    [ -d "$dir" ] || continue
    printf '%s\n' "$content" > "${dir}/${marker}" 2>/dev/null || true
  done
}

# `data-paths` subcommand — print resolved live data locations so nothing gets
# deleted by accident. Read-only; never starts or stops anything.
print_data_paths() {
  resolve_data_paths
  echo "📍 OpenTranscribe data locations"
  echo "--------------------------------"
  if [ "$DP_NAS_ACTIVE" = "true" ]; then
    echo "NAS/bind overlay: ACTIVE (docker-compose.nas.yml auto-loads from .env)"
    echo ""
    echo "⚠️  LIVE BIND-MOUNTED DATA — DO NOT delete or 'clean up' these paths:"
    echo "   MinIO media:  ${MINIO_NAS_PATH:-<unset> (default /mnt/nas/opentranscribe-minio)}"
    echo "   PostgreSQL:   ${POSTGRES_DATA_PATH:-<unset> (default /mnt/nvm/opentranscribe/pg)}"
    echo "   OpenSearch:   ${OPENSEARCH_DATA_PATH:-<unset> (default /mnt/nvm/opentranscribe/os)}"
    echo ""
    echo "   These dirs carry a '.opentranscribe-live-data' marker after a non-fresh start."
  else
    echo "NAS/bind overlay: INACTIVE — data lives in Docker NAMED VOLUMES (project 'opentranscribe'):"
    echo "   opentranscribe_postgres_data, opentranscribe_minio_data,"
    echo "   opentranscribe_opensearch_data, opentranscribe_redis_data"
    echo "   (inspect with: docker volume ls | grep opentranscribe)"
  fi
  echo ""
  echo "Fresh deployments (isolated, never touch the above):"
  if [ -d "$FRESH_OVERLAY_DIR" ] && ls "${FRESH_OVERLAY_DIR}"/*.yml >/dev/null 2>&1; then
    local f n
    for f in "${FRESH_OVERLAY_DIR}"/*.yml; do
      case "$f" in *-ports.yml) continue;; esac
      n="$(basename "$f" .yml)"
      echo "   otfresh-${n} → named volumes otfresh-${n}_{postgres,minio,opensearch,redis}_data"
    done
  else
    echo "   (none — create with ./opentr.sh start dev --fresh <name>)"
  fi
}

# Build the compose-file chain used to address a fresh deployment for
# stop/status/destroy. Mirrors the dev chain (base + override + gpu) plus the
# generated container_name overlay; NAS is never included. Echoes the chain.
fresh_compose_chain() {
  local name="$1"
  local chain="-f docker-compose.yml -f docker-compose.override.yml"
  [ -f "docker-compose.gpu.yml" ] && chain="$chain -f docker-compose.gpu.yml"
  [ -f "${FRESH_OVERLAY_DIR}/${name}.yml" ] && chain="$chain -f ${FRESH_OVERLAY_DIR}/${name}.yml"
  [ -f "${FRESH_OVERLAY_DIR}/${name}-ports.yml" ] && chain="$chain -f ${FRESH_OVERLAY_DIR}/${name}-ports.yml"
  echo "$chain"
}

# `stop --fresh [name]` — stop a fresh deployment's containers (volumes kept).
fresh_stop() {
  local name
  name="$(fresh_sanitize_name "$1")"
  local proj
  proj="$(fresh_project_name "$name")"
  local chain
  chain="$(fresh_compose_chain "$name")"
  echo "🛑 Stopping fresh deployment '${name}' (project ${proj})..."
  # shellcheck disable=SC2086
  COMPOSE_PROJECT_NAME="$proj" docker compose $chain down 2>/dev/null || true
  echo "✅ Stopped. Volumes preserved — use 'fresh-destroy ${name}' to remove them."
}

# `status --fresh [name]` — show a fresh deployment's containers.
fresh_status() {
  local name
  name="$(fresh_sanitize_name "$1")"
  local proj
  proj="$(fresh_project_name "$name")"
  local chain
  chain="$(fresh_compose_chain "$name")"
  echo "📊 Fresh deployment '${name}' (project ${proj}):"
  # shellcheck disable=SC2086
  COMPOSE_PROJECT_NAME="$proj" docker compose $chain ps 2>/dev/null || true
}

# `fresh-list` — list all known fresh deployments (by generated overlay) plus
# their running containers and volumes.
fresh_list() {
  echo "🧪 Fresh deployments:"
  if [ -d "$FRESH_OVERLAY_DIR" ] && ls "${FRESH_OVERLAY_DIR}"/*.yml >/dev/null 2>&1; then
    local f n proj running
    for f in "${FRESH_OVERLAY_DIR}"/*.yml; do
      case "$f" in *-ports.yml) continue;; esac
      n="$(basename "$f" .yml)"
      proj="$(fresh_project_name "$n")"
      running="$(docker ps --filter "name=^${proj}-" --format '{{.Names}}' 2>/dev/null | wc -l | tr -d ' ')"
      echo "  • ${n}  (project ${proj}, ${running} container(s) running)"
    done
  else
    echo "  (none — create with ./opentr.sh start dev --fresh <name>)"
  fi
  echo ""
  echo "Fresh named volumes:"
  docker volume ls --format '{{.Name}}' 2>/dev/null | grep '^otfresh-' || echo "  (none)"
}

# `fresh-destroy <name>` — the ONLY destructive fresh op. Removes containers AND
# named volumes for one fresh deployment after explicit y/N confirmation. Never
# touches any bind path.
fresh_destroy() {
  local raw="$1"
  if [ -z "$raw" ]; then
    echo "❌ Usage: ./opentr.sh fresh-destroy <name>"
    exit 1
  fi
  local name
  name="$(fresh_sanitize_name "$raw")"
  local proj
  proj="$(fresh_project_name "$name")"
  local chain
  chain="$(fresh_compose_chain "$name")"

  local vols
  vols="$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep "^${proj}_" || true)"
  local containers
  containers="$(docker ps -a --filter "name=^${proj}-" --format '{{.Names}}' 2>/dev/null || true)"

  echo "⚠️  About to DESTROY fresh deployment '${name}' (project ${proj})."
  echo ""
  echo "   Containers to remove:"
  if [ -n "$containers" ]; then echo "$containers" | sed 's/^/     - /'; else echo "     (none)"; fi
  echo "   Named volumes to remove:"
  if [ -n "$vols" ]; then echo "$vols" | sed 's/^/     - /'; else echo "     (none)"; fi
  echo "   Generated overlays to remove:"
  ls "${FRESH_OVERLAY_DIR}/${name}.yml" "${FRESH_OVERLAY_DIR}/${name}-ports.yml" 2>/dev/null | sed 's/^/     - /' || true
  echo ""
  echo "   This touches ONLY this isolated project — no bind paths, no other stack."
  printf "   Proceed? (y/N) "
  read -r confirm
  if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "❌ Destroy cancelled."
    return 0
  fi

  # shellcheck disable=SC2086
  COMPOSE_PROJECT_NAME="$proj" docker compose $chain down -v 2>/dev/null || true
  # Catch any stragglers the compose chain didn't own.
  if [ -n "$vols" ]; then
    echo "$vols" | xargs -r docker volume rm 2>/dev/null || true
  fi
  rm -f "${FRESH_OVERLAY_DIR}/${name}.yml" "${FRESH_OVERLAY_DIR}/${name}-ports.yml" 2>/dev/null || true
  echo "✅ Fresh deployment '${name}' destroyed (containers + volumes + overlays)."
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
  WITH_WATCH_FLAG=""
  WITH_SMB_TEST_FLAG=""
  WITH_MONITORING_FLAG=""
  LITE_FLAG=""
  CPU_FLAG=""
  NO_NAS_FLAG=""
  FRESH_FLAG=""
  FRESH_NAME=""
  PORT_OFFSET=""
  DRY_RUN_FLAG=""
  SEED_BENCHMARK_FLAG=""

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
      --no-nas)
        NO_NAS_FLAG="--no-nas"
        shift
        ;;
      --fresh)
        FRESH_FLAG="--fresh"
        shift
        # Optional name argument (anything not starting with '-').
        if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
          FRESH_NAME="$1"
          shift
        fi
        ;;
      --port-offset)
        shift
        PORT_OFFSET="$1"
        shift
        ;;
      --dry-run)
        DRY_RUN_FLAG="--dry-run"
        shift
        ;;
      --seed-benchmark)
        SEED_BENCHMARK_FLAG="--seed-benchmark"
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
      --with-watch)
        WITH_WATCH_FLAG="--with-watch"
        shift
        ;;
      --with-smb-test)
        WITH_SMB_TEST_FLAG="--with-smb-test"
        shift
        ;;
      --with-monitoring)
        WITH_MONITORING_FLAG="--with-monitoring"
        shift
        ;;
      *)
        echo "⚠️  Unknown flag: $1"
        shift
        ;;
    esac
  done

  # ── Fresh deployment mode ───────────────────────────────────────────────
  # Isolated project + named volumes; NAS overlay forced OFF; container_name
  # collisions resolved via a generated overlay. Real data is never touched.
  FRESH_PROJECT=""
  FRESH_OVERLAY=""
  FRESH_PORT_OVERLAY=""
  if [ -n "$FRESH_FLAG" ]; then
    if [ "$ENVIRONMENT" != "dev" ]; then
      echo "❌ --fresh is only supported in dev mode (./opentr.sh start dev --fresh [name])"
      exit 1
    fi
    FRESH_NAME="$(fresh_sanitize_name "$FRESH_NAME")"
    FRESH_PROJECT="$(fresh_project_name "$FRESH_NAME")"

    # Resolve port offset (default 0 → standard dev ports so conftest works).
    local _offset="${PORT_OFFSET:-0}"
    if ! [[ "$_offset" =~ ^[0-9]+$ ]]; then
      echo "❌ --port-offset must be a non-negative integer (got '$_offset')"
      exit 1
    fi

    # Refuse to start on standard ports if ANOTHER stack already holds them.
    # Exception: when THIS SAME fresh project is what's bound to them, proceed —
    # `compose up -d` just recreates changed services (e.g. after a .env edit).
    if [ "$_offset" -eq 0 ]; then
      local _busy=""
      local _p
      for _p in "${FRESH_DEFAULT_PORTS[@]}"; do
        if fresh_port_in_use "$_p"; then _busy="$_busy $_p"; fi
      done
      if [ -n "$_busy" ]; then
        local _holder
        _holder="$(docker ps --filter "label=com.docker.compose.project=${FRESH_PROJECT}" --format '{{.Names}}' 2>/dev/null | head -1)"
        if [ -n "$_holder" ]; then
          echo "ℹ️  Standard ports are held by this same fresh deployment (${FRESH_PROJECT}) — re-upping in place."
        else
          echo "❌ Cannot start fresh deployment on the standard dev ports — already bound:${_busy}"
          echo "   The main stack appears to be running. Either stop it, or run side-by-side:"
          echo "   ./opentr.sh start dev --fresh ${FRESH_NAME} --port-offset 100"
          exit 1
        fi
      fi
    fi

    FRESH_OVERLAY="$(fresh_generate_overlay "$FRESH_NAME")"
    if [ "$_offset" -ne 0 ]; then
      FRESH_PORT_OVERLAY="$(fresh_generate_port_overlay "$FRESH_NAME" "$_offset")"
    fi
    export COMPOSE_PROJECT_NAME="$FRESH_PROJECT"

    echo ""
    echo "🧪 FRESH DEPLOYMENT '${FRESH_NAME}': isolated project + volumes; NAS overlay IGNORED; real data untouched."
    echo "   Project: ${FRESH_PROJECT}  (containers: ${FRESH_PROJECT}-*)"
    if [ "$_offset" -ne 0 ]; then
      echo "   Port offset: +${_offset} (e.g. backend on $((5174 + _offset)), frontend on $((5173 + _offset)))"
    else
      echo "   Ports: standard dev ports (5173-5181)"
    fi
    echo ""

    # Force NAS off in fresh mode regardless of .env.
    NO_NAS_FLAG="--no-nas"
    NAS_FLAG=""
  fi

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
      # Force no-pull at `up` time. pull_policy:never in the override is NOT reliably honored
      # when a service also defines a build: context — `docker compose up` still pulls the
      # referenced image: tag and clobbers the locally-built one. `--pull never` is explicit.
      BUILD_CMD="--pull never"
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
  # or auto-detect when storage path env vars are set.
  #
  # --no-nas (and fresh mode, which forces it) suppresses both the explicit and
  # the auto-detect path so the live bind-mounted data is never attached.
  if [ -n "$NO_NAS_FLAG" ]; then
    if [ -n "$FRESH_FLAG" ]; then
      :  # banner already printed by fresh mode
    elif [ -n "$MINIO_NAS_PATH" ] || [ -n "$POSTGRES_DATA_PATH" ] || [ -n "$OPENSEARCH_DATA_PATH" ]; then
      echo "🚫 --no-nas: NAS overlay suppressed (using Docker named volumes; live bind data untouched)"
    fi
    NAS_FLAG=""
  elif [ -z "$NAS_FLAG" ] && { [ -n "$MINIO_NAS_PATH" ] || [ -n "$POSTGRES_DATA_PATH" ] || [ -n "$OPENSEARCH_DATA_PATH" ]; }; then
    NAS_FLAG="--nas"
    echo "💾 NAS overlay AUTO-LOADED from .env (storage at MinIO=${MINIO_NAS_PATH:-default}, PG=${POSTGRES_DATA_PATH:-default}, OS=${OPENSEARCH_DATA_PATH:-default}). Use --no-nas to skip."
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

  # Add Watch Sources overlay if requested (mounts the host watch folder)
  if [ -n "$WITH_WATCH_FLAG" ]; then
    if [ -f "docker-compose.watch.yml" ]; then
      WATCH_HOST_PATH="${WATCH_HOST_PATH:-./watch}"
      mkdir -p "$WATCH_HOST_PATH"
      # Match the non-root container user (UID/GID 1000) so imports can read/write.
      chown -R 1000:1000 "$WATCH_HOST_PATH" 2>/dev/null || true
      export WATCH_HOST_PATH
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.watch.yml"
      echo "👁️  Adding Watch Sources overlay (docker-compose.watch.yml)"
      echo "   Host watch folder: $WATCH_HOST_PATH → /watch (in containers)"
    else
      echo "⚠️  --with-watch specified but docker-compose.watch.yml not found"
    fi
  fi

  # Add SMB test container if requested (Samba share for watch-source testing)
  if [ -n "$WITH_SMB_TEST_FLAG" ]; then
    if [ -f "docker-compose.smb-test.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.smb-test.yml"
      echo "🗄️  Adding SMB test container (docker-compose.smb-test.yml)"
      echo "   SMB share: smb://localhost:4450/media  (testuser / testpass)"
    else
      echo "⚠️  --with-smb-test specified but docker-compose.smb-test.yml not found"
    fi
  fi

  # Add Monitoring overlay if requested (Prometheus + Grafana for the /metrics endpoint)
  if [ -n "$WITH_MONITORING_FLAG" ]; then
    if [ -f "docker-compose.monitoring.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.monitoring.yml"
      echo "📈 Adding Monitoring overlay (docker-compose.monitoring.yml)"
      echo "   Prometheus: http://localhost:${PROMETHEUS_PORT:-5186}"
      echo "   Grafana:    http://localhost:${GRAFANA_PORT:-5185}  (admin / \$GRAFANA_PASSWORD, default admin)"
    else
      echo "⚠️  --with-monitoring specified but docker-compose.monitoring.yml not found"
    fi
  fi

  # Fresh-mode overlays go LAST so their container_name + port re-pinning wins.
  if [ -n "$FRESH_FLAG" ]; then
    [ -n "$FRESH_OVERLAY" ] && COMPOSE_FILES="$COMPOSE_FILES -f $FRESH_OVERLAY"
    [ -n "$FRESH_PORT_OVERLAY" ] && COMPOSE_FILES="$COMPOSE_FILES -f $FRESH_PORT_OVERLAY"
  fi

  # Dry-run: print exactly what WOULD run and exit without touching Docker.
  if [ -n "$DRY_RUN_FLAG" ]; then
    echo ""
    echo "🔎 DRY RUN — no containers started."
    echo "   COMPOSE_PROJECT_NAME: ${COMPOSE_PROJECT_NAME:-opentranscribe (default)}"
    echo "   Compose files:"
    # shellcheck disable=SC2086
    for _f in $COMPOSE_FILES; do
      [ "$_f" = "-f" ] && continue
      echo "     - $_f"
    done
    echo "   Command that WOULD run:"
    echo "     docker compose $COMPOSE_FILES up -d $BUILD_CMD"
    [ -n "$FRESH_FLAG" ] && echo "   (fresh mode: NAS overlay omitted by design; real data untouched)"
    [ -n "$SEED_BENCHMARK_FLAG" ] && echo "   (would seed benchmark media via scripts/seed-fresh-deployment.sh after healthy)"
    return 0
  fi

  # Best-effort live-data guardrail markers (only when the NAS overlay is active
  # and we are NOT in fresh mode — fresh uses named volumes, no bind dirs).
  if [ -n "$NAS_FLAG" ] && [ -z "$FRESH_FLAG" ]; then
    write_live_data_markers
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

  # Seed benchmark media into a fresh deployment once it is healthy.
  if [ -n "$SEED_BENCHMARK_FLAG" ]; then
    if [ -z "$FRESH_FLAG" ]; then
      echo "⚠️  --seed-benchmark is only honored in fresh mode (--fresh); skipping."
    elif [ -f "scripts/seed-fresh-deployment.sh" ]; then
      local _seed_offset="${PORT_OFFSET:-0}"
      local _seed_backend_port=$((5174 + _seed_offset))
      echo ""
      echo "🌱 Seeding benchmark media into fresh deployment '${FRESH_NAME}' (backend :${_seed_backend_port})..."
      BACKEND_URL="http://localhost:${_seed_backend_port}" \
        bash scripts/seed-fresh-deployment.sh || echo "⚠️  Seeding did not complete (non-fatal)."
    else
      echo "⚠️  --seed-benchmark requested but scripts/seed-fresh-deployment.sh not found."
    fi
  fi
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
  WITH_WATCH_FLAG=""
  WITH_SMB_TEST_FLAG=""
  WITH_MONITORING_FLAG=""
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
      --with-watch)
        WITH_WATCH_FLAG="--with-watch"
        shift
        ;;
      --with-smb-test)
        WITH_SMB_TEST_FLAG="--with-smb-test"
        shift
        ;;
      --with-monitoring)
        WITH_MONITORING_FLAG="--with-monitoring"
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

  # Add Watch Sources overlay if requested (mounts the host watch folder)
  if [ -n "$WITH_WATCH_FLAG" ]; then
    if [ -f "docker-compose.watch.yml" ]; then
      WATCH_HOST_PATH="${WATCH_HOST_PATH:-./watch}"
      mkdir -p "$WATCH_HOST_PATH"
      # Match the non-root container user (UID/GID 1000) so imports can read/write.
      chown -R 1000:1000 "$WATCH_HOST_PATH" 2>/dev/null || true
      export WATCH_HOST_PATH
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.watch.yml"
      echo "👁️  Adding Watch Sources overlay (docker-compose.watch.yml)"
      echo "   Host watch folder: $WATCH_HOST_PATH → /watch (in containers)"
    else
      echo "⚠️  --with-watch specified but docker-compose.watch.yml not found"
    fi
  fi

  # Add SMB test container if requested (Samba share for watch-source testing)
  if [ -n "$WITH_SMB_TEST_FLAG" ]; then
    if [ -f "docker-compose.smb-test.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.smb-test.yml"
      echo "🗄️  Adding SMB test container (docker-compose.smb-test.yml)"
      echo "   SMB share: smb://localhost:4450/media  (testuser / testpass)"
    else
      echo "⚠️  --with-smb-test specified but docker-compose.smb-test.yml not found"
    fi
  fi

  # Add Monitoring overlay if requested (Prometheus + Grafana for the /metrics endpoint)
  if [ -n "$WITH_MONITORING_FLAG" ]; then
    if [ -f "docker-compose.monitoring.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.monitoring.yml"
      echo "📈 Adding Monitoring overlay (docker-compose.monitoring.yml)"
      echo "   Prometheus: http://localhost:${PROMETHEUS_PORT:-5186}"
      echo "   Grafana:    http://localhost:${GRAFANA_PORT:-5185}  (admin / \$GRAFANA_PASSWORD, default admin)"
    else
      echo "⚠️  --with-monitoring specified but docker-compose.monitoring.yml not found"
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
# Usage: backup_database [--encrypt]
#   --encrypt: pipe pg_dump straight into gpg (AES-256, passphrase prompt) so the
#              plaintext dump never touches disk. Backups contain every user's
#              transcripts - encrypt anything that leaves this machine.
backup_database() {
  ENCRYPT_BACKUP=false
  if [[ "$1" == "--encrypt" ]]; then
    ENCRYPT_BACKUP=true
    if ! command -v gpg &> /dev/null; then
      echo "❌ Error: gpg is required for encrypted backups (e.g. 'apt install gnupg')."
      exit 1
    fi
  elif [[ -n "$1" ]]; then
    echo "❌ Error: unknown backup option: $1"
    echo "Usage: ./opentr.sh backup [--encrypt]"
    exit 1
  fi

  TIMESTAMP=$(date +%Y%m%d_%H%M%S)
  BACKUP_FILE="opentranscribe_backup_${TIMESTAMP}.sql"
  mkdir -p ./backups

  if [[ "$ENCRYPT_BACKUP" == true ]]; then
    echo "📦 Creating encrypted database backup: ${BACKUP_FILE}.gpg..."
    # Subshell with pipefail so a pg_dump failure isn't masked by gpg succeeding
    if (set -o pipefail; docker compose exec -T postgres pg_dump -U postgres opentranscribe \
        | gpg --symmetric --cipher-algo AES256 --output "./backups/${BACKUP_FILE}.gpg"); then
      echo "✅ Encrypted backup created successfully: ./backups/${BACKUP_FILE}.gpg"
      echo "   Restore with: ./opentr.sh restore ./backups/${BACKUP_FILE}.gpg"
    else
      rm -f "./backups/${BACKUP_FILE}.gpg"
      echo "❌ Backup failed."
      exit 1
    fi
  else
    echo "📦 Creating database backup: ${BACKUP_FILE}..."
    if docker compose exec -T postgres pg_dump -U postgres opentranscribe > "./backups/${BACKUP_FILE}"; then
      echo "✅ Backup created successfully: ./backups/${BACKUP_FILE}"
      echo "ℹ️  Tip: backups contain all user transcripts in plaintext - use './opentr.sh backup --encrypt' for off-box storage."
    else
      echo "❌ Backup failed."
      exit 1
    fi
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

  # Transparently decrypt GPG-encrypted backups (created with './opentr.sh backup --encrypt')
  RESTORE_SOURCE="$BACKUP_FILE"
  TEMP_SQL=""
  case "$BACKUP_FILE" in
    *.gpg|*.asc)
      if ! command -v gpg &> /dev/null; then
        echo "❌ Error: gpg is required to restore encrypted backups (e.g. 'apt install gnupg')."
        exit 1
      fi
      echo "🔓 Decrypting backup..."
      TEMP_SQL=$(mktemp ./backups/.restore_XXXXXX)
      if ! gpg --yes --output "$TEMP_SQL" --decrypt "$BACKUP_FILE"; then
        rm -f "$TEMP_SQL"
        echo "❌ Decryption failed."
        exit 1
      fi
      RESTORE_SOURCE="$TEMP_SQL"
      ;;
  esac

  echo "🔄 Restoring database from ${BACKUP_FILE}..."

  # Stop services that use the database
  docker compose stop backend celery-worker celery-download-worker celery-cpu-worker celery-nlp-worker celery-embedding-worker celery-beat

  # Restore the database
  if docker compose exec -T postgres psql -U postgres opentranscribe < "$RESTORE_SOURCE"; then
    [ -n "$TEMP_SQL" ] && rm -f "$TEMP_SQL"
    echo "✅ Database restored successfully."
    echo "🔄 Restarting services..."
    docker compose start backend celery-worker celery-download-worker celery-cpu-worker celery-nlp-worker celery-embedding-worker celery-beat
  else
    [ -n "$TEMP_SQL" ] && rm -f "$TEMP_SQL"
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

# Check Docker is available for all commands EXCEPT purely read-only ones that
# must work even when the daemon is down/recovering (data-paths, help). A
# start --dry-run also skips it — it never talks to Docker.
case "$1" in
  data-paths|help|--help|-h) ;;
  start)
    case " $* " in
      *" --dry-run "*) ;;
      *) check_docker ;;
    esac
    ;;
  *) check_docker ;;
esac

# Process the command
case "$1" in
  start)
    shift  # Remove 'start' command
    start_app "$@"  # Pass all remaining arguments
    ;;

  stop)
    if [ "${2:-}" = "--fresh" ]; then
      fresh_stop "${3:-default}"
    else
      echo "🛑 Stopping all containers..."
      # Stop containers from both dev and prod compose chains, plus any stragglers.
      # Using MAX_COMPOSE_FILES with conflicting overlays (prod + override) can fail
      # silently, so we run each chain separately.
      stop_all_containers down
      echo "✅ All containers stopped."
    fi
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
    if [ "${2:-}" = "--fresh" ]; then
      fresh_status "${3:-default}"
    else
      echo "📊 Container status:"
      docker compose ps
    fi
    ;;

  data-paths)
    print_data_paths
    ;;

  fresh-list)
    fresh_list
    ;;

  fresh-destroy)
    fresh_destroy "${2:-}"
    ;;

  shell)
    SERVICE=${2:-backend}
    echo "🔧 Opening shell in $SERVICE container..."
    docker compose exec "$SERVICE" /bin/bash || docker compose exec "$SERVICE" /bin/sh
    ;;

  backup)
    backup_database "$2"
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

      all|phase)
        # End-to-end engine benchmark orchestrator (scripts/run_benchmark.py).
        # Fully self-contained: it stands up the isolated otbench stack, uploads
        # the corpus like a user, collects metrics, and tears down per phase.
        # Never touches dev/NAS data (project otbench, *_bench_data volumes only).
        if ! nvidia-smi >/dev/null 2>&1; then
          echo "❌ nvidia-smi failed — check GPU state before benchmarking. Aborting."
          exit 1
        fi
        # Pass remaining args straight through (e.g. --smoke / --quick / --full / --phases / --conc).
        ORCH_ARGS=("${@:3}")
        if [[ "$BENCH_SUBCOMMAND" == "phase" ]]; then
          PHASE_NAME="${3:-}"
          if [[ -z "$PHASE_NAME" || "$PHASE_NAME" == --* ]]; then
            echo "❌ Usage: ./opentr.sh bench phase <name> [--smoke|--quick|--full] [--conc N]"
            exit 1
          fi
          ORCH_ARGS=(--phases "$PHASE_NAME" "${@:4}")
        fi
        echo "🧪 End-to-end engine benchmark — branch: $(git branch --show-current)"
        backend/venv/bin/python scripts/run_benchmark.py "${ORCH_ARGS[@]}"
        ;;

      collate)
        # Aggregate all per-level metrics.json into master + whitepaper tables.
        backend/venv/bin/python scripts/collate_benchmark.py "${@:3}"
        ;;

      help|*)
        echo "🧪 Benchmark subcommands (isolated from NAS data):"
        echo "  bench all [--smoke|--quick|--full] [--phases a,b]  - Full end-to-end run (all phases, all metrics)"
        echo "  bench phase <name> [--smoke|--quick|--full]        - Run a single phase end-to-end"
        echo "  bench collate                            - Aggregate metrics into master + whitepaper tables"
        echo "  bench start [master|branch|current|<name>]- Wipe bench volumes, switch branch, start bench stack (default: current)"
        echo "  bench stop                               - Stop bench stack (keep volumes)"
        echo "  bench clean                              - Stop bench stack and wipe all bench volumes"
        echo "  bench run [output.csv] [fixtures_dir]    - Run upload-speed benchmark on current branch"
        echo "  bench engine                             - Run engine split-stage benchmarks (Phase 2 gate)"
        echo "  bench status                             - Show bench containers, GPU state, volumes"
        echo "  bench compare <master.csv> <branch.csv>  - Print side-by-side speedup table"
        echo ""
        echo "  Phases: a6000_solo, ti_solo, dual_gpu_scale, gpu_split, duration_curve"
        echo "  Tiers:  --smoke (validate, minutes) -> --quick (~10-15h) -> --full (~58h, paper)"
        echo "  Add --fresh to wipe prior results for a clean run; resume is automatic otherwise."
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
