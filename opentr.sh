#!/bin/bash

# OpenTranscribe Utility Script
# A comprehensive script for all OpenTranscribe operations
# Usage: ./opentr.sh [command] [options]

# Fail on unset variables and on pipeline errors. NOTE: deliberately NO `set -e`
# — the script has many `|| true` / best-effort paths that rely on continuing
# after a non-zero exit.
set -uo pipefail

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

# Default the optional .env-sourced variables this script reads — directly or
# through scripts/common.sh — so `set -u` doesn't abort when they're absent from
# .env. These are all genuinely optional (storage paths, nginx server name, GPU
# device id, ports).
#
# Keep this list in sync with what the two files actually expand:
# backend/tests/unit/test_shell_env_var_guards.py fails the build on any
# unguarded expansion that is missing here. GPU_DEVICE_ID was the one that got
# away — common.sh tested it bare, so `./opentr.sh start dev` died with
# "GPU_DEVICE_ID: unbound variable" on any checkout without a .env.
: "${NGINX_SERVER_NAME:=}"
: "${MINIO_NAS_PATH:=}"
: "${POSTGRES_DATA_PATH:=}"
: "${OPENSEARCH_DATA_PATH:=}"
: "${COMPOSE_PROFILES:=}"
# GPU_DEVICE_ID is read by common.sh's benchmark helper via `[ -n "$GPU_DEVICE_ID" ]`,
# which aborts under `set -u` in any checkout whose .env omits it — i.e. every fresh
# worktree. Empty means "no specific device", which that helper already handles.
: "${GPU_DEVICE_ID:=}"
# --with-pki host ports (docker-compose.pki.yml / docker-compose.pki-dev.yml).
# Both are already guarded with `:-` at every read site, but listed here too
# per the contract this block documents.
: "${PKI_HTTPS_PORT:=}"
: "${PKI_HTTP_PORT:=}"
# Grace period (seconds) for CUDA-holding services on stop/down/restart (issue #782).
# scripts/common.sh (sourced above) already assigns this with `${OT_STOP_GRACE_GPU:-60}`,
# which satisfies `set -u` on its own — this entry exists for the drift-guard contract
# test_stop_grace_period_wiring.py enforces (opentr.sh's defaults block, common.sh's
# assignment, and the compose `${OT_STOP_GRACE_GPU:-Ns}` default must all agree on 60).
: "${OT_STOP_GRACE_GPU:=60}"
# Snapshot of the .env value, taken before any `--gpu-device` override replaces
# it. The containers read GPU_DEVICE_ID from `env_file: .env` (not from this
# shell), so the override has to be able to say which value they will still see.
GPU_DEVICE_ID_FROM_ENV="${GPU_DEVICE_ID}"
# ENVIRONMENT is assigned only inside opentr.sh's own subcommand functions
# (`ENVIRONMENT=${1:-dev}`), so any path reaching a common.sh helper without going
# through start/reset first aborted before it could print anything. `dev` matches the
# default those functions use, so defaulting here cannot change a real invocation.
: "${ENVIRONMENT:=dev}"

# Export APP_VERSION so docker compose can pass it through to containers
# (used instead of ./VERSION file bind-mount to avoid OCI stub creation in dev mode)
export APP_VERSION
APP_VERSION=$(cat VERSION 2>/dev/null || echo "unknown")

# Export GIT_SHA so docker compose can pass it through to containers, same as
# APP_VERSION above. This is measurement attribution (#55): once a stack is built
# from baked-in source rather than a bind mount, "which commit is this?" can only
# be answered from inside the image, so the build has to be told. A dirty working
# tree is not attributable to a commit either, so it gets a "-dirty" suffix rather
# than silently reporting the last commit as if it were what's actually running.
export GIT_SHA
if git rev-parse --short HEAD >/dev/null 2>&1; then
  GIT_SHA=$(git rev-parse --short HEAD)
  if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    GIT_SHA="${GIT_SHA}-dirty"
  fi
else
  GIT_SHA="unknown"
fi

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
  echo "  start dev --fresh [name] [--port-offset N] [--seed-benchmark] [--no-bindmount]"
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
  echo "  --gpu-device N       - Run this stack's AI work on host GPU N, overriding .env AFTER it"
  echo "                         is sourced (a pre-exported GPU_DEVICE_ID cannot win — .env clobbers"
  echo "                         it — and editing .env moves the LIVE stack too)."
  echo "                         Moves ALL SIX device ids together, because a flag that"
  echo "                         repoints one worker and leaves five behind just makes two stacks"
  echo "                         fight over one card: GPU_DEVICE_ID, REDACTION_GPU_DEVICE_ID,"
  echo "                         GPU_SCALE_DEVICE_ID, GPU_TRANSCRIBE_DEVICE_ID, GPU_DIARIZE_DEVICE_ID,"
  echo "                         DIAR_NATIVE_GPU (the sidecar — omitted from this list until #719)."
  echo "                         Does NOT move LLM_TEST_GPU_DEVICE_ID (--with-llm-test keeps its own"
  echo "                         card on purpose — co-locating a multi-GB LLM with transcription is"
  echo "                         what that separation prevents); use LLM_TEST_GPU_DEVICE_ID=N ./opentr.sh"
  echo "                         Does NOT move the in-container copy of GPU_DEVICE_ID: it comes from"
  echo "                         'env_file: .env', which no shell export can reach. It only labels the"
  echo "                         admin GPU-stats panel; placement is the reservation this flag sets."
  echo "  --diar-native-gpu N  - Move ONLY the diar-native sidecar to host GPU N, applied AFTER"
  echo "                         --gpu-device so the two can differ (e.g. --gpu-device 2"
  echo "                         --diar-native-gpu 1 puts gpu-scale workers on GPU 2 and the"
  echo "                         sidecar on GPU 1) — needed to test the cross-card arrangement the"
  echo "                         shipped defaults already describe (issue #711)."
  echo "  --nas                - Use custom storage paths (NAS for media, NVMe for DB/search)"
  echo "  --no-nas             - Suppress the auto-loaded NAS overlay (use Docker named volumes)"
  echo "  --no-diar-native     - Suppress the auto-loaded native diarization sidecar"
  echo "  --fresh [name]       - Isolated dev deployment: own project + named volumes, NAS"
  echo "                         overlay NEVER loaded, real data untouched (dev mode only)"
  echo "  --port-offset N      - With --fresh: offset every published port by N (run side-by-side;"
  echo "                         remembered per deployment — pass --port-offset 0 to reset)."
  echo "                         Covers the --with-ldap-test/--with-smb-test/--with-monitoring/"
  echo "                         --with-keycloak-test/--with-authentik-test overlays too."
  echo "  --seed-benchmark     - With --fresh: upload small benchmark media once healthy"
  echo "  --no-bindmount       - With --fresh: don't bind-mount ./backend into backend/worker"
  echo "                         containers, and drop uvicorn --reload. The stack then runs the"
  echo "                         IMAGE built at 'up --build' time, not live edits on disk — for"
  echo "                         measurement runs where every number must attribute to a commit,"
  echo "                         not whatever was on disk when a container last reloaded (#55)."
  echo "  --dry-run            - Print the compose files + command that WOULD run; start nothing"
  echo "  --lite               - Cloud-only ASR mode (no GPU required)"
  echo "  --cpu                - CPU-only mode (local transcription, no GPU overlay)"
  echo "  --with-pki           - Enable PKI certificate authentication (mTLS). Works in dev (via"
  echo "                         docker-compose.pki-dev.yml, a built nginx in front of the"
  echo "                         bind-mounted dev backend) and prod. Generates its own test-env"
  echo "                         fragment (scripts/pki/generate-test-env.sh) — never touches .env."
  echo "  --with-ldap-test     - Start LDAP test container (dev or prod; localhost:3890, UI :17170)"
  echo "  --with-mock-llm      - Start mock LLM provider (localhost:5199) so chat/AI features"
  echo "                         work without a GPU or API key. Models: mock-gpt, mock-echo,"
  echo "                         mock-empty, mock-error, mock-slow"
  echo "  --with-mock-asr      - Start mock cloud ASR provider (Gladia stand-in, localhost:5198)"
  echo "                         so cloud-ASR features work without a vendor account."
  echo "                         Scenarios: ok, error, malformed, upload-reject"
  echo "  --with-llm-test      - Start a real GPU-backed LLM (vLLM, localhost:5195) for chat"
  echo "                         testing against actual model output, not canned tokens."
  echo "                         Default model: Gemma 4 E4B (AWQ), GPU 2. See"
  echo "                         docker-compose.llm-test.yml for the Ollama alternative."
  echo "  --with-diar-native   - Start the native diarization sidecar (diar-server), the"
  echo "                         PRIMARY engine when engine.diarizer_backend=native."
  echo "                         GPU: DIAR_NATIVE_GPU, else GPU_DEVICE_ID; the sidecar"
  echo "                         holds ~2.2 GB of warm ORT arena on that card while up."
  echo "                         Without this flag a native-configured stack silently"
  echo "                         falls back to the in-process PyAnnote fork per file."
  echo "  --with-keycloak-test - Start Keycloak test container (dev or prod; localhost:8180)"
  echo "  --with-authentik-test - Start Authentik test container (dev or prod; localhost:9022)"
  echo "  --with-watch         - Mount the host watch folder (WATCH_HOST_PATH, default ./watch) for auto-import"
  echo "  --with-smb-test      - Start a Samba test share for watch-source testing (localhost:4450)"
  echo "  --with-monitoring    - Start Prometheus (:5186) + Grafana (:5185) observability stack"
  echo "                         (all four --with-* test overlays are isolated + port-offset by --fresh)"
  echo "  --with-backup        - Mount BACKUP_HOST_PATH (default ./backups) for in-app scheduled backups"
  echo "  --with-scratch-tmpfs - Put the pipeline_scratch WAV handoff volume on RAM-backed tmpfs"
  echo "                         (default 2g, override SCRATCH_TMPFS_SIZE). Sized off"
  echo "                         DIAR_NATIVE_MAX_INFLIGHT x largest in-flight file."
  echo ""
  echo "Reset & Database Commands:"
  echo "  reset [dev|prod] [options]             - Reset and reinitialize (deletes all data!)"
  echo "                                           (Accepts same options as 'start' command)"
  echo "  backup [--encrypt]  - Create a database backup (--encrypt: GPG AES-256, no plaintext on disk)"
  echo "  restore [--yes] [--no-safety-dump] [--from-s3] [--migrate-forward|--no-restart] <file>  - REPLACE the database from a backup"
  echo "                  (.sql, .dump, .sql.gpg, or .dump.gpg; --from-s3 fetches by name first) — destructive"
  echo ""
  echo "Development Commands:"
  echo "  restart-backend [--fresh <name>]"
  echo "                      - Restart backend, all celery workers, celery-beat & flower without database reset"
  echo "  restart-frontend [--fresh <name>]"
  echo "                      - Restart frontend without affecting backend services"
  echo "  restart-all [--fresh <name>]"
  echo "                      - Restart all services without resetting database"
  echo "                        (--fresh targets an isolated deployment; without it, the default stack)"
  echo "  rebuild-backend [--nas] [--with-diar-native|--no-diar-native]"
  echo "                           - Rebuild backend services with code changes. The"
  echo "                             diar-native overlay is kept automatically when this"
  echo "                             deployment already has the sidecar."
  echo "                             (pass --nas on NAS/NVMe deployments; auto-detected"
  echo "                             from MINIO_NAS_PATH/POSTGRES_DATA_PATH/OPENSEARCH_DATA_PATH"
  echo "                             env vars). --no-deps protects postgres/minio/opensearch."
  echo "  rebuild-frontend         - Rebuild frontend + docs with code changes (--no-deps)"
  echo "  shell [service]     - Open a shell in a container"
  echo "  build               - Rebuild all containers without starting"
  echo ""
  echo "Cleanup Commands:"
  echo "  remove              - Stop containers and remove data volumes"
  echo "  purge               - Remove everything including images (most destructive)"
  echo ""
  echo "Advanced Commands:"
  echo "  health              - Check health status of all services (human report, always exits 0)"
  echo "  healthcheck-all     - Same probes, but EXITS NON-ZERO on failure (for scripts/CI); --json supported"
  echo "  help                - Show this help menu"
  echo ""
  echo "Benchmark Commands (isolated from NAS data):"
  echo "  bench all [--smoke|--quick|--full] [--phases a,b]"
  echo "                                           - Full end-to-end run (all phases, all metrics)"
  echo "  bench phase <name> [--smoke|--quick|--full]"
  echo "                                           - Run a single phase end-to-end"
  echo "  bench collate                            - Aggregate metrics into master + whitepaper tables"
  echo "  bench start [master|branch|current|<name>]- Wipe bench volumes, switch branch, start bench stack (default: current)"
  echo "  bench stop                               - Stop bench stack (keep volumes)"
  echo "  bench clean                              - Stop bench stack and wipe all bench volumes"
  echo "  bench run [output.csv] [fixtures_dir]    - Run upload-speed benchmark on current branch"
  echo "  bench engine                             - Run engine split-stage benchmarks (Phase 2 gate)"
  echo "  bench rag --fresh <name> [args]          - Retrieval quality (nDCG/recall/MRR) over an injected eval corpus"
  echo "  bench status                             - Show bench containers, GPU state, volumes"
  echo "  bench compare <master.csv> <branch.csv>  - Print side-by-side speedup table"
  # `bench all|phase|collate` existed for a while without appearing here, so the only way to
  # find them was to read the case block (issue #399). `bench help` already listed them,
  # which is exactly how a top-level help goes stale unnoticed.
  echo "  bench all [--smoke|--quick|--full]       - Full end-to-end run (stands up otbench, all phases)"
  echo "  bench phase <name> [--smoke|--quick]     - Run a single phase end-to-end"
  echo "  bench collate                            - Aggregate per-level metrics into the master tables"
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
  echo "  ./opentr.sh start dev --fresh t1 --gpu-device 2   # Isolated stack on GPU 2, .env untouched"
  echo "  ./opentr.sh start dev --lite                 # Cloud-only ASR mode (no GPU)"
  echo "  ./opentr.sh start dev --cpu                  # Local CPU-only (skip GPU overlay)"
  echo "  ./opentr.sh start dev --with-ldap-test       # Dev with LDAP test container"
  echo "  ./opentr.sh start dev --with-mock-llm        # Dev with a fake LLM for chat/AI testing"
  echo "  ./opentr.sh start dev --with-mock-asr        # Dev with a fake cloud ASR provider for testing"
  echo "  ./opentr.sh start dev --with-llm-test        # Dev with a real GPU-backed LLM (vLLM) for chat testing"
  echo "  ./opentr.sh start dev --with-diar-native     # Dev with the native diarization sidecar"
  echo "  ./opentr.sh start dev --with-keycloak-test   # Dev with Keycloak test container"
  echo "  ./opentr.sh start dev --with-authentik-test  # Dev with Authentik test container"
  echo "  ./opentr.sh start prod                       # Production (pulls from Docker Hub)"
  echo "  ./opentr.sh start prod --build               # Production with local build (test before push)"
  echo "  ./opentr.sh start prod --build --with-pki    # Production with PKI (requires nginx)"
  echo "  ./opentr.sh start dev --with-pki             # Dev with PKI (docker-compose.pki-dev.yml)"
  echo "  ./opentr.sh reset dev                        # Reset development environment"
  echo "  ./opentr.sh reset dev --lite                 # Reset in cloud-only ASR mode"
  echo "  ./opentr.sh logs backend                     # View backend logs"
  echo "  ./opentr.sh restart-backend                  # Restart backend services only"
  echo "  ./opentr.sh restart-backend --fresh test1    # ...on the isolated 'test1' deployment"
  echo ""
}

# Resolve where the diar-native ONNX/PLDA export lives, and EXPORT it so the
# auto-load check below and docker-compose.diar-native.yml's volume mount can never
# disagree about the answer.
#
# Order: an explicit DIAR_NATIVE_MODELS_DIR wins; then the standard cache location
# (${MODEL_CACHE_DIR}/diar-native, a sibling of huggingface/ and torch/, and where a
# self-hosted export lands); then the pre-convention sibling-repo export, which is
# where this workstation's models still are and which has no entry in .env. Dropping
# that last probe silently moved existing dev checkouts onto the PyAnnote fallback.
#
# This probe is dev-only on purpose: opentr.sh is deliberately not shipped
# (test_opentr_sh_is_not_shipped_and_the_shipped_script_covers_it), and
# opentranscribe.sh resolves the standard location only.
resolve_diar_native_models_dir() {
  if [ -n "${DIAR_NATIVE_MODELS_DIR:-}" ]; then
    export DIAR_NATIVE_MODELS_DIR
    return 0
  fi

  local standard="${MODEL_CACHE_DIR:-./models}/diar-native"
  local legacy="/mnt/nvm/repos/diar-native/models_folded"

  # `-d` alone is not enough: Docker auto-creates an empty directory at a bind-mount
  # source that doesn't exist yet, so a standard path that was never populated (or was
  # only just created by a prior container start) would otherwise be silently preferred
  # over a legacy path that genuinely has the export -- reproduced live: diar-native
  # restart-looped on "File at /models/segmentation-3.0.onnx does not exist" the moment
  # an empty ./models/diar-native existed. Matches opentranscribe.sh's own
  # `[ -d ... ] && [ -n "$(ls -A ...)" ]` non-emptiness check.
  if [ -d "$standard" ] && [ -n "$(ls -A "$standard" 2>/dev/null)" ]; then
    export DIAR_NATIVE_MODELS_DIR="$standard"
  elif [ -d "$legacy" ]; then
    export DIAR_NATIVE_MODELS_DIR="$legacy"
    # Announced, not silent: this path is a property of one machine, not of the repo,
    # so it must never become invisible drift that only that machine benefits from.
    echo "ℹ️  diar-native models found at the legacy path $legacy."
    echo "   Set DIAR_NATIVE_MODELS_DIR in .env to pin it, or move the export to $standard."
  else
    export DIAR_NATIVE_MODELS_DIR="$standard"
  fi
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
  # The docs build context is docs-site/, so pass the repo-root VERSION in explicitly —
  # docusaurus.config.ts cannot read ../VERSION from inside the container.
  docker build --build-arg DOCS_BASE_URL=/docs/ \
    --build-arg "OT_VERSION=${APP_VERSION}" \
    -t davidamacey/opentranscribe-docs:latest docs-site || {
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

  # FORCE_CPU_MODE=true in .env is an explicit opt-out of GPU usage even on a GPU host —
  # opentranscribe.sh has honoured this since it was added; opentr.sh silently ignored it,
  # so `./opentr.sh start dev` loaded the nvidia GPU reservation (and the diar-native GPU
  # overlay, gated on the same $DOCKER_RUNTIME below) anyway. `--cpu`/`--lite` already clear
  # DOCKER_RUNTIME themselves; this only covers the .env-only opt-out those flags don't set.
  if [ "${FORCE_CPU_MODE:-}" = "true" ]; then
    echo "🧮 CPU-only mode (FORCE_CPU_MODE=true in .env) — skipping GPU detection"
    export TORCH_DEVICE="cpu"
    export COMPUTE_TYPE="int8"
    export USE_GPU="false"
    export DOCKER_RUNTIME=""
    export BACKEND_DOCKERFILE="Dockerfile.prod"
    export BUILD_ENV="development"
    TARGETPLATFORM="linux/$([[ "$ARCH" == "arm64" ]] && echo "arm64" || echo "amd64")"
    export TARGETPLATFORM
    echo "📋 Hardware Configuration:"
    echo "  Platform: $PLATFORM"
    echo "  Architecture: $ARCH"
    echo "  Device: $TORCH_DEVICE"
    echo "  Compute Type: $COMPUTE_TYPE"
    echo "  Docker Runtime: default"
    return
  fi

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

    # Pin the diar-native sidecar to the SAME Blackwell tag as celery-worker above.
    # docker-compose.blackwell.yml deliberately carries no `diar-native` entry of its own
    # (see its comment): this overlay is always appended BEFORE docker-compose.diar-native.yml
    # (add_diar_native_overlay runs after add_gpu_overlay at every call site), and compose's
    # last-file-wins merge means diar-native.yml's `image:` key always overrides whatever a
    # compose-side retag here would set — verified: resolving the full chain gave
    # celery-worker -> :blackwell but diar-native -> :latest. DIAR_NATIVE_IMAGE is the
    # variable docker-compose.diar-native.yml itself interpolates, so setting it here, in the
    # shell, before compose ever reads either file, wins regardless of `-f` order. `:-`
    # respects an operator's own override (e.g. a hand-set custom export location); safe to
    # call unconditionally since add_diar_native_overlay's own dev-mode default a few lines
    # later uses the same `:-` form and therefore never clobbers this.
    export DIAR_NATIVE_IMAGE="${DIAR_NATIVE_IMAGE:-davidamacey/opentranscribe-backend:${OT_BLACKWELL_IMAGE_TAG:-blackwell}}"
  elif [ -f "docker-compose.gpu.yml" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.gpu.yml"
    echo "🎯 Adding GPU overlay (docker-compose.gpu.yml) for NVIDIA acceleration"
  fi
}

# GPU device-id variables that docker compose INTERPOLATES to pick the physical
# card for one of OpenTranscribe's own AI workers. One list, so `--gpu-device`
# and the test that guards it cannot disagree about the membership.
GPU_DEVICE_VARS=(
  GPU_DEVICE_ID             # default GPU worker          (docker-compose.gpu.yml / .blackwell.yml)
  REDACTION_GPU_DEVICE_ID   # redaction worker            (docker-compose.gpu.yml)
  GPU_SCALE_DEVICE_ID       # --gpu-scale workers         (docker-compose.gpu-scale.yml)
  GPU_TRANSCRIBE_DEVICE_ID  # --with-gpu-split transcribe (docker-compose.gpu-split.yml)
  GPU_DIARIZE_DEVICE_ID     # --with-gpu-split diarize    (docker-compose.gpu-split.yml)
  DIAR_NATIVE_GPU           # diar-native sidecar         (docker-compose.diar-native.yml)
)

# `--gpu-device N` — retarget every GPU this stack's workers reserve, applied
# AFTER .env has been sourced.
#
# WHY IT EXISTS: opentr.sh does `set -a; source ./.env` near the top, so
# `GPU_DEVICE_ID=2 ./opentr.sh start dev` is silently overwritten by whatever
# .env says — a pre-export cannot win. The only remaining way to move a worker
# onto another card was to EDIT .env, which is shared with the live stack (and in
# a git worktree is often a copy of, or a symlink to, the same file). That has
# already happened here: a worktree .env edit moved the LIVE stack's GPU.
#
# WHY IT MOVES ALL FIVE: a flag that repoints one worker and leaves four behind
# is worse than no flag — it looks like it worked, and then two stacks fight over
# one card. `--gpu-device N` therefore means "this whole stack runs its AI work on
# host GPU N", and sets every variable in GPU_DEVICE_VARS.
#
# WHAT IT DELIBERATELY DOES NOT MOVE:
#   * LLM_TEST_GPU_DEVICE_ID (--with-llm-test) hosts a multi-GB LLM and is pinned
#     to a DIFFERENT card on purpose so it never contends with transcription.
#     Folding it in would co-locate them — the exact OOM that separation avoids.
#     Move it explicitly with `LLM_TEST_GPU_DEVICE_ID=N ./opentr.sh ...` (it is
#     absent from .env.example, so a pre-export survives unless your .env sets it).
#   * The container-side copy of GPU_DEVICE_ID is read INSIDE the container from
#     `env_file: .env` rather than interpolated by compose, so no shell export can
#     reach it. Warned about below.
apply_gpu_device_override() {
  local requested="$1"
  local var

  if ! [[ "$requested" =~ ^[0-9]+$ ]]; then
    echo "❌ --gpu-device must be a non-negative integer GPU index (got '$requested')"
    exit 1
  fi

  # Catch the typo now rather than as an opaque "could not select device driver"
  # from the daemon several minutes into a build. Skipped when nvidia-smi is
  # absent (macOS, CPU-only host, CI) — the flag is a no-op there anyway.
  if command -v nvidia-smi &> /dev/null; then
    local gpu_count
    gpu_count=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ' || true)
    if [ -n "$gpu_count" ] && [ "$gpu_count" -gt 0 ] && [ "$requested" -ge "$gpu_count" ]; then
      echo "❌ --gpu-device $requested: this host has $gpu_count GPU(s) (valid indices 0-$((gpu_count - 1)))"
      nvidia-smi -L 2>/dev/null | sed 's/^/   /'
      exit 1
    fi
  fi

  for var in "${GPU_DEVICE_VARS[@]}"; do
    export "$var=$requested"
  done

  echo "🎯 --gpu-device $requested: pinning this stack's AI workers to host GPU $requested (overrides .env)"
  echo "   Set: ${GPU_DEVICE_VARS[*]} = $requested"
  echo "   NOT set: LLM_TEST_GPU_DEVICE_ID (--with-llm-test keeps its own card on purpose)"

  # The container-side copy of GPU_DEVICE_ID comes from `env_file: .env`, which no
  # shell export can override. It is only read for host GPU-stats display
  # (nvidia-smi -i), never for placement — placement is the reservation above —
  # but the admin GPU panel will keep naming the .env card until .env is edited.
  if [ -n "${GPU_DEVICE_ID_FROM_ENV:-}" ] && [ "${GPU_DEVICE_ID_FROM_ENV}" != "$requested" ]; then
    echo "   ℹ️  In-container GPU_DEVICE_ID stays ${GPU_DEVICE_ID_FROM_ENV} (from env_file: .env) — that value"
    echo "      only labels the admin GPU-stats panel; the reserved card is $requested."
  fi

}

# Flag combinations that make --gpu-device mean less than it looks like it means.
# Separate from apply_gpu_device_override so the override itself stays a pure
# "set these vars" step that the other flags' parse order cannot affect.
warn_gpu_device_override_conflicts() {
  local requested="$1"

  if [ -n "${CPU_FLAG:-}" ] || [ -n "${LITE_FLAG:-}" ]; then
    echo "   ⚠️  --cpu/--lite loads no GPU overlay, so --gpu-device $requested reserves nothing."
  fi

  if [ -n "${GPU_SPLIT_FLAG:-}" ]; then
    echo "   ⚠️  --with-gpu-split exists to put transcribe and diarize on DIFFERENT cards;"
    echo "      --gpu-device $requested collapses both onto GPU $requested. Drop one of the two flags,"
    echo "      or set GPU_TRANSCRIBE_DEVICE_ID / GPU_DIARIZE_DEVICE_ID in .env instead."
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

# True when this compose project already has a diar-native container, in ANY state.
#
# Label-scoped, never name-prefix-matched: this host runs unrelated compose stacks
# whose container names share the `opentranscribe` prefix, and a naive
# `docker ps | grep '^opentranscribe-'` in `opentr.sh stop` once destroyed one of
# them. The diar-native service also declares no `container_name`, so its container
# is `<project>-diar-native-1` and a `name=^<project>-diar-native$` filter — the
# shape the gpu-scale/gpu-split loop uses for services that DO pin a name — would
# match nothing here.
#
# `-a`, i.e. any state, not `status=running`, on purpose: the sidecar is
# `restart: unless-stopped`, so a crash-looping instance reports `restarting`, and a
# stack whose sidecar is merely down between rebuilds is still a stack that HAS one.
# Being over-inclusive costs one named-volume mount and two env vars on
# celery-worker; being under-inclusive costs the silent PyAnnote fallback that
# add_diar_native_overlay exists to prevent.
#
# ⚠️ THE PROJECT NAME IS DERIVED FROM THE DIRECTORY, NOT DEFAULTED TO "opentranscribe".
# The first version of this probe used `${COMPOSE_PROJECT_NAME:-opentranscribe}` and was
# a NO-OP on the machine that has the bug. COMPOSE_PROJECT_NAME is never exported
# globally by this script — only locally inside the fresh-deployment helpers — so
# compose falls back to ITS default, the directory basename. Measured against the live
# daemon: the sidecar's `com.docker.compose.project` label is `transcribe-app` (the
# checkout is /mnt/nvm/repos/transcribe-app), and a probe filtering on `opentranscribe`
# matched 0 containers, dropped the overlay, and reproduced the 422 exactly.
# `basename "$(pwd)"` is the same resolution preflight_ports_or_die already uses; a
# checkout in a differently-named directory must keep working, so this is derived and
# never hardcoded. (test_the_probe_resolves_the_project_from_the_checkout_directory)
diar_native_container_present() {
  local project="${COMPOSE_PROJECT_NAME:-$(basename "$(pwd)")}"
  docker ps -a --format '{{.ID}}' \
    --filter "label=com.docker.compose.project=${project}" \
    --filter "label=com.docker.compose.service=diar-native" 2>/dev/null | grep -q .
}

# Append the native diarization sidecar overlay to $COMPOSE_FILES. Mirrors
# add_nas_overlay: ONE place decides, so start/reset/rebuild can never disagree
# about whether celery-worker gets the diar-native handoff volume.
#
# ⚠️ WHY THIS IS SHARED RATHER THAN INLINED. docker-compose.diar-native.yml is the
# only file that sets DIAR_NATIVE_URL on celery-worker and shares the pipeline_scratch
# handoff volume with the sidecar (issue #661 E2: one volume, engine/ and diar/
# namespaces — this used to be a dedicated `diar-native-tmp` volume at
# /tmp/diar-native, which is the shape the reproduction below happened in; the
# DEFECT this overlay prevents is unchanged, only the volume name moved).
# `rebuild-backend` used to omit the overlay, so it recreated celery-worker with the
# sidecar unreachable at all; the worker wrote the WAV to its own filesystem, the
# sidecar could not see it, and /diarize answered
#     HTTP 422  opening /tmp/diar-native/<job>.wav: No such file or directory
# which the worker classified as "sidecar failed mid-job" and answered by falling
# back to the in-process PyAnnote fork — slower, no speaker gender, and NOTHING
# surfaced to the user beyond one log line. Same bug class as the NAS overlay note
# above: containers that look correct, bound to the wrong storage.
#
# $1 picks the auto-detect predicate, because "should this deployment have the
# sidecar?" and "does this deployment have the sidecar?" are different questions:
#
#   start   - CONFIGURATION. engine.diarizer_backend resolves to native AND (the model
#             export already exists OR a HUGGINGFACE_TOKEN is configured to produce it on
#             this startup). The right question for a stack that does not exist yet.
#
#   rebuild - OBSERVATION. A diar-native container already exists in this compose
#             project. rebuild-backend recreates services in place, so the only
#             correct answer is the one the running deployment already made; the
#             config predicate disagrees with it in BOTH directions (a stack started
#             with --no-diar-native would gain the overlay; a stack running the
#             sidecar under ENGINE_DIARIZER_BACKEND=pyannote would lose it, which is
#             the original bug). Precedent: the gpu-scale/gpu-split loop in
#             rebuild-backend likewise rebuilds those workers only when they are up.
#             It can never START a sidecar nobody asked for — with no container there
#             is no overlay, and rebuild-backend passes an explicit service list with
#             --no-deps, so `diar-native` is never brought up either way.
#
# Either predicate is overridden by an explicit --with-diar-native / --no-diar-native.
add_diar_native_overlay() {
  local mode="${1:-start}"

  # Runs unconditionally (even under --no-diar-native): it EXPORTS the path
  # docker-compose.diar-native.yml interpolates for the read-only /models bind, and
  # its legacy-path banner is part of every start's output today.
  resolve_diar_native_models_dir

  if [ -z "${WITH_DIAR_NATIVE_FLAG:-}" ] && [ -z "${NO_DIAR_NATIVE_FLAG:-}" ]; then
    if [ "$mode" = "rebuild" ]; then
      if diar_native_container_present; then
        WITH_DIAR_NATIVE_FLAG="auto"
        echo "🎙️  diar-native sidecar is part of this deployment — keeping its overlay so celery-worker keeps DIAR_NATIVE_URL and the shared handoff namespace."
      fi
    else
      # Native diarization sidecar auto-load: `native` is the coded default engine, so a
      # stack without the sidecar silently serves every file from the in-process PyAnnote
      # fallback. Mirrors the NAS auto-detect: announced, and --no-diar-native suppresses.
      #
      # Gate is "models present OR a token is configured", not "models present" alone.
      # backend/app/transcription/native_provision.py now runs `diar-server
      # provision-models` from the FastAPI lifespan on backend startup, so gating on the
      # export already existing meant a fresh checkout needed TWO `start`s to converge —
      # one to provision, a second to notice the result. A configured HUGGINGFACE_TOKEN is
      # what lets that provisioning step succeed, so it stands in for the export until the
      # export exists. With neither, nothing can ever produce the weights and loading the
      # overlay would just crash-loop the sidecar (see resolve_diar_native_models_dir's
      # comment for the reproduction).
      #
      # `--fresh` is no longer excluded here. It used to be, back when a fresh stack had no
      # path to its own export; now the same lifespan provisioning runs there too, so a
      # `--fresh` stack with a token configured is precisely the fresh-install rehearsal
      # this auto-load exists to cover, not a case to skip.
      #
      # Lite is NO LONGER excluded. It used to be, on the premise that the lite image had
      # no Python exporter toolchain, so /models could never fill itself in and loading the
      # overlay would crash-loop diar-server against an empty --models-dir under
      # `restart: unless-stopped` (`diar-server serve` with an empty DIAR_MODELS_DIR exits
      # 8 — that part is still true and still what the gate below protects against).
      #
      # The premise was wrong in a way that removed the feature it was protecting. The
      # ONNX/PLDA graphs are non-redistributable derivatives of gated weights, so a
      # deployment that cannot export cannot obtain them AT ALL — excluding lite did not
      # avoid a broken sidecar, it guaranteed lite had no local voiceprint path whatsoever
      # (SpeakerEmbeddingService refuses when the sidecar is unusable, and lite runs cloud
      # ASR, so speaker embeddings are the ONE local model job it still has).
      #
      # `diar-server` carries the export itself — its five Python scripts are compiled into
      # the binary, which Dockerfile.lite copies in and which runs there. Only the packages
      # those scripts import were missing; requirements-lite.txt now installs the four the
      # binary's own preflight names (pyannote.audio, onnxscript, onnxslim,
      # onnxconverter-common). So lite provisions itself on first boot exactly as the full
      # image does, and falls through to the same gate as every other deployment: models
      # present, or a token configured to produce them.
      if [ "${ENGINE_DIARIZER_BACKEND:-native}" = "native" ]; then
        if [ -d "$DIAR_NATIVE_MODELS_DIR" ] && [ -n "$(ls -A "$DIAR_NATIVE_MODELS_DIR" 2>/dev/null)" ]; then
          WITH_DIAR_NATIVE_FLAG="auto"
          echo "🎙️  diar-native sidecar AUTO-LOADED (engine.diarizer_backend defaults to native; models present). Use --no-diar-native to skip."
        elif [ -n "${HUGGINGFACE_TOKEN:-}" ]; then
          WITH_DIAR_NATIVE_FLAG="auto"
          echo "🎙️  diar-native sidecar AUTO-LOADED (engine.diarizer_backend defaults to native; no export yet, but HUGGINGFACE_TOKEN is set — backend will provision it on startup). Use --no-diar-native to skip."
        fi
      fi
    fi
  fi

  # `predict` exists only for the --fresh aux-recording step (see the --fresh block in
  # start_app), which has to know whether this decision will come out "load the sidecar"
  # BEFORE $COMPOSE_FILES exists — fresh_write_aux/fresh_generate_overlay run long before
  # the real caller reaches this same function later in start_app. It stops here on
  # purpose: appending to $COMPOSE_FILES now would land the overlay before
  # docker-compose.yml itself (COMPOSE_FILES is not yet initialized at that point), and
  # that append gets clobbered anyway the moment start_app does
  # `COMPOSE_FILES="-f docker-compose.yml"`. The decision made above (WITH_DIAR_NATIVE_FLAG
  # plus the banner) is the only thing the caller needs, and it persists in the shell
  # variable for the real call to pick up unchanged.
  if [ "$mode" = "predict" ]; then
    return 0
  fi

  # Add the native diarization sidecar if requested
  if [ -n "${WITH_DIAR_NATIVE_FLAG:-}" ]; then
    if [ -f "docker-compose.diar-native.yml" ]; then
      # Lite pairing (issue #660): under --lite the workers that use this sidecar
      # (backend, celery-cpu-worker — the latter serves extract_speaker_embeddings,
      # NOT celery-embedding-worker, which is search indexing) run
      # opentranscribe-backend-lite. The overlay's own default is the FULL backend
      # image, so resolving docker-compose.yml + lite + diar-native without this
      # produces the exact mismatched image pair described in B4: lite workers on
      # one image, the sidecar on another. This must be exported before the
      # dev-mode default below, which only fires when this is unset. Model source
      # for lite used to be a separate decision on the premise that the lite image had no
      # Python exporter toolchain — since #654 restored it to requirements-lite.txt, that
      # premise is dead: lite provisions its own ONNX/PLDA export on first boot exactly
      # like a full install (see resolve_diar_native_models_dir and DIAR_NATIVE_MODELS_DIR
      # in .env.example). Lite's speaker embeddings stay PyAnnote-free-at-runtime either way.
      [ -n "${LITE_FLAG:-}" ] && export DIAR_NATIVE_IMAGE="${DIAR_NATIVE_IMAGE:-${BACKEND_LITE_IMAGE:-davidamacey/opentranscribe-backend-lite:latest}}"

      # The overlay defaults to the PUBLISHED backend image, which is correct for a
      # self-hosted deployment but wrong in this checkout — dev builds the image
      # locally as opentranscribe-backend:${OT_DEV_IMAGE_TAG:-latest} and never pushes it.
      # Point the sidecar at the local build so it matches the workers it serves — reading
      # OT_DEV_IMAGE_TAG (unset -> "latest" outside --fresh) keeps this in lockstep with the
      # tag docker-compose.override.yml's other 13 services resolve to, so a --fresh stack's
      # sidecar never ends up paired against the MAIN stack's :latest image.
      if [ "$ENVIRONMENT" = "dev" ]; then
        export DIAR_NATIVE_IMAGE="${DIAR_NATIVE_IMAGE:-opentranscribe-backend:${OT_DEV_IMAGE_TAG:-latest}}"
      fi
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.diar-native.yml"
      echo "🎙️  Adding native diarization sidecar (docker-compose.diar-native.yml)"

      # The base overlay above is deliberately CPU-safe: it declares no device
      # reservation and DIAR_MODE defaults to `cpu`, so it can load on a --lite or
      # GPU-less host without `up` failing on "could not select device driver" (#660).
      # The nvidia reservation and the `cuda` override live in this second file, gated on
      # the same runtime probe add_gpu_overlay uses — without it a GPU host would silently
      # run the sidecar on CPU, which is slower; embeddings stay EQUIVALENT for speaker
      # matching (measured 2026-09-04: cosine 0.999999816 CPU-vs-CUDA — not bit-identical,
      # and CUDA is not even bit-identical with itself at 2.86e-04 run to run) while
      # diarization segment boundaries may differ by up to one segmentation frame
      # (0.016875 s), so nothing would ever surface the mistake.
      if [ "$DOCKER_RUNTIME" = "nvidia" ] && [ -f "docker-compose.diar-native-gpu.yml" ]; then
        COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.diar-native-gpu.yml"
        echo "   diar-server on GPU ${DIAR_NATIVE_GPU:-${GPU_DEVICE_ID:-0}} — ~2.2 GB warm ORT arena while up."
      else
        echo "   diar-server on CPU (no nvidia runtime detected) — slower; embeddings identical," \
             "diarization boundaries may differ by up to 0.016875s (#679)."
      fi
      echo "   Used when engine.diarizer_backend=native (DB) / ENGINE_DIARIZER_BACKEND=native (env);"
      echo "   without the sidecar that config falls back to the in-process PyAnnote fork."
    else
      echo "⚠️  --with-diar-native specified but docker-compose.diar-native.yml not found"
    fi
  fi
}

# Append the PKI/mTLS overlay to $COMPOSE_FILES if --with-pki was passed.
# Replaces two ~25-line blocks that used to be duplicated verbatim in
# start_app() and reset() (issue: PKI test-env injection design doc).
#
# Dev vs prod: docker-compose.pki-dev.yml swaps in a Dockerfile.prod nginx in
# front of the bind-mounted dev backend (real mTLS, no image rebuild needed for
# a backend fix); docker-compose.pki.yml is the prod/nginx overlay. Either way
# docker-compose.override.yml / docker-compose.prod.yml must already be in
# $COMPOSE_FILES — start_app()/reset() add PKI after that base chain.
#
# Certificate generation and the test-env fragment are delegated to
# scripts/pki/generate-test-env.sh, which is the ONLY thing that decides
# whether certs need (re)issuing — see that script's header. This function
# never opens .env; PKI_HTTPS_PORT/PKI_HTTP_PORT are read from whatever the
# shell already has (a fresh deployment will have offset them beforehand).
add_pki_overlay() {
  if [ -z "$WITH_PKI_FLAG" ]; then
    return
  fi

  local pki_compose_file
  if [ "$ENVIRONMENT" = "dev" ]; then
    pki_compose_file="docker-compose.pki-dev.yml"
  else
    pki_compose_file="docker-compose.pki.yml"
  fi

  if [ ! -f "$pki_compose_file" ]; then
    echo "⚠️  --with-pki specified but $pki_compose_file not found"
    return
  fi

  if ! ./scripts/pki/generate-test-env.sh --quiet \
    --https-port "${PKI_HTTPS_PORT:-5182}" --http-port "${PKI_HTTP_PORT:-5187}"; then
    echo "❌ Failed to generate PKI test env (scripts/pki/generate-test-env.sh)"
    exit 1
  fi

  # Source the fragment AFTER .env (sourced at the top of this script), same
  # ordering rule apply_gpu_device_override() follows for GPU_DEVICE_ID:
  # PKI_HTTP_PORT is a live line in .env.example, so a pre-export would
  # otherwise lose to it. .env itself is never opened by this function or by
  # generate-test-env.sh.
  set -a
  # shellcheck source=scripts/pki/test-certs/pki-test.env
  source scripts/pki/test-certs/pki-test.env
  set +a

  COMPOSE_FILES="$COMPOSE_FILES -f $pki_compose_file -f scripts/pki/test-certs/pki-test.compose.yml"
  echo "🔐 Adding PKI authentication overlay ($pki_compose_file + generated test-env fragment)"
  echo "   Access URL: ${PKI_E2E_URL:-https://localhost:${PKI_HTTPS_PORT:-5182}}"
  echo "   Import client certificate from: scripts/pki/test-certs/clients/"
  if [ "$ENVIRONMENT" = "dev" ]; then
    echo "   ℹ️  Dev PKI frontend is a BUILT image — a *frontend* change still needs"
    echo "      ./opentr.sh rebuild-frontend. Only the backend hot-reloads."
  fi
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

# Aux test overlays that hard-code a container_name. Their services are added to
# the generated overlay ONLY when the matching --with-* flag is passed — an
# overlay entry for a service no compose file defines makes `up` fail (issue
# #347). docker-compose.keycloak.yml is absent on purpose: it declares no
# container_name, so the project name already namespaces it.
FRESH_LDAP_SERVICES=(lldap)
# Mock LLM provider (--with-mock-llm). Isolated like every other aux overlay so
# a fresh stack cannot collide with the main one on port 5199.
FRESH_MOCK_LLM_SERVICES=(mock-llm)
# Mock cloud ASR provider (--with-mock-asr). Isolated like every other aux
# overlay so a fresh stack cannot collide with the main one on port 5198.
FRESH_MOCK_ASR_SERVICES=(mock-asr)
# Native diarization sidecar (--with-diar-native). No published host port, but the
# service still needs re-pinning into the fresh project so two stacks never share one.
FRESH_DIAR_NATIVE_SERVICES=(diar-native)

# Adversarial-audit finding: container names, ports and named volumes are all
# namespaced by COMPOSE_PROJECT_NAME automatically, but a HOST BIND MOUNT is
# not — it is a literal path on disk. docker-compose.yml mounts
# DIAR_NATIVE_MODELS_DIR into `backend` READ-WRITE (the backend is what EXPORTS
# the model set, native_provision.py), so a --fresh stack that inherited the
# live value could re-export over, or corrupt, the 462MB export the main stack
# is serving from. This is exactly the "own copy" resolution the --fresh
# exclusion in add_diar_native_overlay was already removed to enable — a fresh
# stack with a HUGGINGFACE_TOKEN provisions its OWN export here instead of
# touching the live one, which is a real fresh-install rehearsal rather than a
# read-only peek at somebody else's model set.
#
# Directory, not a bare path builder: kept as a function (not another
# FRESH_*_PATH constant) because it has to derive from the already-sanitized
# deployment name, same as fresh_project_name.
fresh_diar_native_models_dir() {
  echo "${FRESH_OVERLAY_DIR}/$(fresh_sanitize_name "$1")/diar-native-models"
}

# Create (idempotently) and correctly own a fresh deployment's isolated
# diar-native export directory BEFORE `compose up` ever runs. Must happen
# host-side, first: an absent bind-mount source is auto-created by dockerd as
# root-owned the instant the container starts, and the backend (appuser, uid
# 1000) then fails `provision-models` with exit 7 NOT_WRITABLE — the exact
# ownership hazard scripts/common.sh's fix_model_cache_permissions was just
# fixed to avoid for the main $MODEL_CACHE_DIR path. That fix does not cover
# this directory: it is scoped to $MODEL_CACHE_DIR, and this one is
# deliberately OUTSIDE it (see fresh_diar_native_models_dir above), so it
# needs the same two-tier ownership fix (Docker helper container, then a
# direct chown fallback) duplicated here rather than shared.
fresh_prepare_diar_native_models_dir() {
  local dir="$1"
  mkdir -p "$dir"
  local owner
  owner=$(stat -c '%u' "$dir" 2>/dev/null || stat -f '%u' "$dir" 2>/dev/null || echo "unknown")
  [ "$owner" = "1000" ] && return 0
  if command -v docker &>/dev/null && \
     docker run --rm -v "$dir:/models" busybox:latest \
       sh -c "chown -R ${CONTAINER_UID_GID:-1000:999} /models && chmod -R 755 /models" \
       >/dev/null 2>&1; then
    return 0
  fi
  if chown -R "${CONTAINER_UID_GID:-1000:999}" "$dir" 2>/dev/null && chmod -R 755 "$dir" 2>/dev/null; then
    return 0
  fi
  echo "⚠️  Warning: could not fix ownership of fresh diar-native models dir: $dir"
  echo "   If provisioning fails with NOT_WRITABLE, chown it to ${CONTAINER_UID_GID:-1000:999} manually."
  return 1
}
FRESH_SMB_SERVICES=(smb-test)
FRESH_MONITORING_SERVICES=(prometheus grafana)
# Real GPU-backed LLM (--with-llm-test). This was the ONE aux overlay #347 never
# covered, and it is the worst one to leave out: both services hard-code a
# container_name AND publish a loopback port, so a fresh stack silently collided
# with the main one on `opentranscribe-llm-test-vllm` / 5195 — and because the
# services were never recorded in the deployment's `.aux` file, `fresh-destroy`
# walked straight past them and left a multi-GB vLLM holding a GPU.
#
# `llm-test-ollama` sits behind a compose profile and is not started by the flag
# alone, but it is listed anyway: the overlay re-pins names for services that
# exist in the compose files, and omitting it would leave the profile route
# un-isolated for anyone who does opt in.
#
# ⚠️ LLM_TEST_GPU_DEVICE_ID is deliberately NOT offset — see the port-offset
# banner. A fresh stack gets its own container and port, but the operator still
# chooses the card.
FRESH_LLM_TEST_SERVICES=(llm-test-vllm llm-test-ollama)

# Generate (idempotently) the container_name override overlay for a fresh
# deployment and echo its path. Re-pins every hard-coded container_name to
# otfresh-<name>-* so there is zero collision with the real opentranscribe-*
# containers. Compose cannot UNSET container_name via an overlay, so we set an
# explicit per-service value instead.
#
# $1 = deployment name; remaining args = extra aux-overlay service names to
# re-pin (see FRESH_*_SERVICES above).
fresh_generate_overlay() {
  local name="$1"
  shift
  local aux_services=("$@")
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
    for svc in "${FRESH_NAMED_SERVICES[@]}" ${aux_services[@]+"${aux_services[@]}"}; do
      echo "  ${svc}:"
      echo "    container_name: ${proj}-${svc}"
    done
  } > "$file"
  # Pre-#343 deployments also generated a <name>-ports.yml overlay that added a
  # SECOND `ports:` list; compose appends those, so the base port stayed bound.
  # Ports are env-var driven now — drop the stale file so nothing picks it up.
  rm -f "${FRESH_OVERLAY_DIR}/${name}-ports.yml"
  echo "$file"
}

# Services docker-compose.override.yml bind-mounts ./backend:/app into for
# hot-reload (issue #55). --no-bindmount replaces each one's volumes so the
# fresh stack runs the built image instead of whatever is on disk.
# celery-worker-gpu-* are profile-gated (gpu-scale/gpu-split) and simply won't
# appear in `docker compose config` when their flag wasn't passed — no special
# casing needed here for that.
FRESH_BAKED_SERVICES=(
  backend
  celery-worker celery-download-worker celery-cpu-worker celery-redaction
  celery-cloud-asr-worker celery-nlp-worker celery-embedding-worker celery-beat
  celery-worker-gpu-scaled celery-worker-gpu-transcribe celery-worker-gpu-diarize
)

# Generate (idempotently) the "baked image" overlay for `--no-bindmount` and
# echo its path. For every service in FRESH_BAKED_SERVICES, replaces its
# `volumes:` (via the `!override` YAML tag, Compose >=2.24) with the list
# `docker compose config` already resolved for THIS deployment, minus the
# ./backend:/app bind and its /app/venv exclusion — and drops `--reload` from
# any command that has it (only `backend`'s does today).
#
# `!override` is required, not a nicety: compose MERGES a plain `volumes:`
# key across files rather than replacing it, so an overlay listing fewer
# volumes would only ADD to the bind, never remove it (the same trap as the
# ports overlay, issue #343). And the replacement list is machine-generated,
# never hand-transcribed, because each service also carries model-cache /
# pipeline_scratch / transcription-temp mounts a hand-written list would
# silently drop, forcing a ~2.5GB model re-download.
#
# $1 = deployment name. $2 = the compose "-f a.yml -f b.yml ..." chain already
# assembled for this deployment (GPU/aux overlays and the fresh container_name
# overlay all included) — the baked overlay must reflect exactly what would
# otherwise be `up`'d, so it has to be generated from that same chain rather
# than a hand-picked subset of files.
fresh_generate_baked_overlay() {
  local name="$1"
  local files="$2"
  if ! command -v jq >/dev/null 2>&1; then
    echo "❌ --no-bindmount needs 'jq' to read the resolved compose config (not found on PATH)." >&2
    exit 1
  fi
  mkdir -p "$FRESH_OVERLAY_DIR"
  local file="${FRESH_OVERLAY_DIR}/${name}-baked.yml"
  local resolved
  # shellcheck disable=SC2086
  if ! resolved="$(docker compose $files config --format json 2>&1)"; then
    echo "❌ --no-bindmount: 'docker compose config' failed while generating the baked overlay:" >&2
    echo "$resolved" >&2
    exit 1
  fi
  {
    echo "# AUTO-GENERATED by opentr.sh (--no-bindmount) for fresh deployment '${name}'."
    echo "# Replaces each service's volumes: (via !override — a plain key MERGES"
    echo "# instead of replacing, issue #343's trap again) with the list already"
    echo "# resolved by 'docker compose config' minus the ./backend:/app hot-reload"
    echo "# bind and its /app/venv exclusion, and drops --reload from any command"
    echo "# that has it. Every other mount (model caches, pipeline_scratch,"
    echo "# transcription-temp) is carried over unchanged because it came FROM the"
    echo "# resolved config, not a hand-written list. Safe to delete; regenerated"
    echo "# on demand. Do NOT edit by hand."
    echo "services:"
    local svc vols cmd has_reload
    for svc in "${FRESH_BAKED_SERVICES[@]}"; do
      if ! jq -e --arg svc "$svc" '.services[$svc] != null' >/dev/null 2>&1 <<<"$resolved"; then
        continue # not part of this deployment (e.g. a gpu-scale/gpu-split worker that's off)
      fi
      echo "  ${svc}:"
      vols="$(jq -r --arg svc "$svc" '
        .services[$svc].volumes[]?
        | select(.target != "/app" and .target != "/app/venv")
        | select(.source != null)
        | "\(.source):\(.target)"
      ' <<<"$resolved")"
      if [ -n "$vols" ]; then
        echo "    volumes: !override"
        while IFS= read -r v; do
          echo "      - ${v}"
        done <<<"$vols"
      else
        # A bare "volumes: !override" with nothing under it parses as YAML
        # null, and compose rejects that with "must be a list" — celery-beat
        # hits this: its only mount today IS ./backend:/app, so filtering it
        # out empties the list. Explicit empty flow sequence keeps it a list.
        echo "    volumes: !override []"
      fi
      has_reload="$(jq -r --arg svc "$svc" '(.services[$svc].command // []) | any(. == "--reload")' <<<"$resolved")"
      if [ "$has_reload" = "true" ]; then
        cmd="$(jq -c --arg svc "$svc" '.services[$svc].command | map(select(. != "--reload"))' <<<"$resolved")"
        echo "    command: ${cmd}"
      fi
    done
  } >"$file"
  echo "$file"
}

# Verify a baked (--no-bindmount) stack is actually RUNNING the code it claims to
# (issue #528). Compose has been observed accepting `--force-recreate` on the wire
# and still leaving old containers up, so after `up` we read GIT_SHA back from
# every baked service and compare it to the SHA this run exported. A mismatch is
# self-healed once with the surgical per-service recreate (measured to work where
# the blanket flag did not), then re-checked; if it STILL disagrees we exit 1 —
# a baked stack on stale code silently invalidates every measurement taken
# against it, which is worse than a failed start.
#
# $1 = the compose "-f ..." chain for this deployment. Uses $GIT_SHA (exported
# at script top) as the expected value. Containers reporting nothing or
# "unknown" count as stale: an unstamped container cannot attribute a
# measurement either.
fresh_verify_baked_git_sha() {
  local files="$1"
  local expected="$GIT_SHA"
  local attempt svc cid got stale
  for attempt in 1 2; do
    stale=()
    for svc in "${FRESH_BAKED_SERVICES[@]}"; do
      # shellcheck disable=SC2086
      cid="$(docker compose $files ps -q "$svc" 2>/dev/null)"
      [ -z "$cid" ] && continue # not part of this deployment (profile off)
      got="$(docker exec "$cid" printenv GIT_SHA 2>/dev/null || echo '<unset>')"
      if [ "$got" != "$expected" ]; then
        stale+=("$svc")
        echo "   ✗ ${svc}: GIT_SHA=${got} (expected ${expected})"
      fi
    done
    if [ "${#stale[@]}" -eq 0 ]; then
      echo "🔏 Baked-stack code verified: GIT_SHA=${expected} on every baked service."
      return 0
    fi
    if [ "$attempt" -eq 1 ]; then
      echo "⚠️  ${#stale[@]} baked service(s) are running STALE code — recreating surgically (issue #528)..."
      # shellcheck disable=SC2086
      docker compose $files up -d --no-deps --force-recreate "${stale[@]}"
    fi
  done
  echo ""
  echo "❌ Baked stack is STILL running stale code after a surgical recreate (issue #528)."
  echo "   Any measurement against this stack would describe code nobody is running."
  echo "   Inspect: docker compose ${files} ps   and compare printenv GIT_SHA per container."
  exit 1
}

# Every host port a fresh dev stack publishes, as "VAR=default" pairs. VAR is the
# variable the base compose files already interpolate into their single `ports:`
# entry; default is the value baked into that entry.
#
# --port-offset works by EXPORTING these variables (value + offset) so compose
# substitutes the moved port into the existing mapping. It must never emit a
# second `ports:` list in an overlay: compose APPENDS port lists when merging
# files, so an overlay-based offset republished the offset port *in addition to*
# the base one and collided with the main stack anyway (issue #343). The env-var
# route also preserves the `127.0.0.1:` loopback binding the base file puts on
# every infra port — an overlay list replaced that with a 0.0.0.0 bind.
#
# Source of truth: `ports:` entries in docker-compose.yml + docker-compose.override.yml.
FRESH_PORT_VARS=(
  "FRONTEND_PORT=5173"          # frontend (dev override) → :5173
  "BACKEND_PORT=5174"           # backend                 → :8080
  "FLOWER_PORT=5175"            # flower                  → :5555
  "POSTGRES_PORT=5176"          # postgres                → :5432
  "REDIS_PORT=5177"             # redis                   → :6379
  "MINIO_PORT=5178"             # minio API               → :9000
  "MINIO_CONSOLE_PORT=5179"     # minio console           → :9001
  "OPENSEARCH_PORT=5180"        # opensearch API          → :9200
  "OPENSEARCH_ADMIN_PORT=5181"  # opensearch admin        → :9600
  "DOCS_PORT=5183"              # docs (dev override)     → :8080
)

# Aux-overlay host ports, appended to the list above only when the matching
# --with-* flag is passed. Same contract: the overlay interpolates the variable
# into its single `ports:` entry, so exporting it moves the port (issue #347).
#
# LDAP_TEST_PORT / LDAP_TEST_UI_PORT are deliberately NOT named LDAP_PORT:
# LDAP_PORT is the backend's LDAP *client* port (`.env` ships LDAP_PORT=636), and
# offsetting that would silently repoint the app's LDAP config.
FRESH_KEYCLOAK_PORT_VARS=(
  "KEYCLOAK_PORT=8180"          # keycloak → :8080
  "STEP_CA_PORT=9000"           # step-ca  → :9000
)
FRESH_AUTHENTIK_PORT_VARS=(
  "AUTHENTIK_PORT=9022"         # authentik-server → :9000
)
FRESH_LDAP_PORT_VARS=(
  "LDAP_TEST_PORT=3890"         # lldap LDAP   → :3890
  "LDAP_TEST_UI_PORT=17170"     # lldap web UI → :17170
)
FRESH_MOCK_LLM_PORT_VARS=(
  "MOCK_LLM_PORT=5199"          # mock LLM provider → :5199
)
FRESH_MOCK_ASR_PORT_VARS=(
  "MOCK_ASR_PORT=5198"          # mock cloud ASR provider → :5198
)
FRESH_SMB_PORT_VARS=(
  "SMB_TEST_PORT=4450"          # samba → :445
)
FRESH_MONITORING_PORT_VARS=(
  "GRAFANA_PORT=5185"           # grafana    → :3000
  "PROMETHEUS_PORT=5186"        # prometheus → :9090
)
FRESH_LLM_TEST_PORT_VARS=(
  "LLM_TEST_PORT=5195"          # vLLM   → :8000
  "LLM_TEST_OLLAMA_PORT=5196"   # ollama → :11434
)
# docker-compose.pki-dev.yml publishes BOTH — declares no container_name (backend
# and frontend are already re-pinned by FRESH_NAMED_SERVICES), but DOES publish
# ports, so the --with-pki exemption in
# backend/tests/unit/test_opentr_fresh_aux_isolation.py was wrong (issue: PKI
# test-env injection design doc, finding #5) and is isolated here instead.
FRESH_PKI_PORT_VARS=(
  "PKI_HTTPS_PORT=5182"         # PKI nginx mTLS listener  → :8443
  "PKI_HTTP_PORT=5187"          # PKI nginx plain listener → :8080
)

# Resolve and export the host ports a fresh stack publishes, offset by $1.
# Remaining args are "VAR=default" pairs. A value already in the environment
# (i.e. set in .env) is the base the offset applies to, so an operator's custom
# port layout is preserved. Sets FRESH_RESOLVED_PORTS ("VAR:port" entries) for
# the pre-flight bind check and the startup banner.
FRESH_RESOLVED_PORTS=()
fresh_apply_port_offset() {
  local offset="$1"
  shift
  FRESH_RESOLVED_PORTS=()
  local entry var base current
  for entry in "$@"; do
    var="${entry%%=*}"
    base="${entry#*=}"
    current="${!var:-}"
    [[ "$current" =~ ^[0-9]+$ ]] || current="$base"
    export "${var}=$((current + offset))"
    FRESH_RESOLVED_PORTS+=("${var}:$((current + offset))")
  done
}

# Human-readable one-liner of the resolved ports, e.g. "frontend=5273 backend=5274".
fresh_port_summary() {
  local entry out=""
  for entry in "${FRESH_RESOLVED_PORTS[@]}"; do
    out="${out} $(echo "${entry%%:*}" | sed 's/_PORT$//' | tr '[:upper:]' '[:lower:]')=${entry#*:}"
  done
  echo "$out"
}

# File recording a deployment's port offset so `stop`, `status`, `fresh-list` and
# a later re-up all address the same ports without repeating --port-offset.
fresh_offset_file() {
  echo "${FRESH_OVERLAY_DIR}/${1}.offset"
}

# Echo a deployment's recorded port offset (0 when none recorded).
fresh_read_offset() {
  local file value=""
  file="$(fresh_offset_file "$1")"
  [ -f "$file" ] && value="$(tr -dc '0-9' < "$file")"
  echo "${value:-0}"
}

# Record (or clear, when 0) a deployment's port offset.
fresh_write_offset() {
  local name="$1"
  local offset="$2"
  mkdir -p "$FRESH_OVERLAY_DIR"
  if [ "$offset" -eq 0 ]; then
    rm -f "$(fresh_offset_file "$name")"
  else
    echo "$offset" > "$(fresh_offset_file "$name")"
  fi
}

# File recording which aux overlays (--with-ldap-test etc.) a deployment was
# started with, one compose filename per line. Without it, `stop`, `status` and
# `fresh-destroy` would address a chain that lacks those files while the
# generated overlay still re-pins their container_name — compose would reject
# the "service with no image" and the aux containers would survive teardown.
fresh_aux_file() {
  echo "${FRESH_OVERLAY_DIR}/${1}.aux"
}

# Echo a deployment's recorded aux overlay files (nothing when none recorded).
fresh_read_aux() {
  local file
  file="$(fresh_aux_file "$1")"
  [ -f "$file" ] && cat "$file"
  return 0
}

# Record (or clear, when none given) a deployment's aux overlay files.
fresh_write_aux() {
  local name="$1"
  shift
  mkdir -p "$FRESH_OVERLAY_DIR"
  if [ $# -eq 0 ]; then
    rm -f "$(fresh_aux_file "$name")"
  else
    printf '%s\n' "$@" > "$(fresh_aux_file "$name")"
  fi
}

# Return 0 if a TCP port is already bound on localhost, 1 otherwise.
fresh_port_in_use() {
  local port="$1"
  # bash /dev/tcp probe — no netstat/ss dependency.
  (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
  return 1
}

# Resolve the host ports a NON-fresh stack will publish and refuse to start when
# one is already taken by something that is not us.
#
# `--fresh` has had this check since #347 (see the block in the fresh branch
# below); the ordinary `start` path never had one, and the failure mode is bad
# out of proportion to the cause: `docker compose up` aborts PART WAY THROUGH on
# the bind error, leaving every service it had not reached yet in `Created` --
# frontend, all eight celery workers, flower, docs -- while postgres/redis/minio/
# backend are up, so the stack looks half-alive rather than failed. E2E then
# errors at fixture setup and every celery-backed test fails, which reads as
# application breakage. Observed with an unrelated project holding 5183
# (DOCS_PORT); it cost a full debugging cycle on a branch that was fine.
#
# `--wait` does not cover this, despite the comment at the `up` call: the bind
# fails before any health check runs.
#
# Re-upping the SAME project is not a conflict -- `compose up -d` recreating
# changed services is the normal way to apply a .env edit -- so a port held by
# our own compose project is allowed through, matching the fresh path's rule.
preflight_ports_or_die() {
  local entry var base port busy="" holder
  local project="${COMPOSE_PROJECT_NAME:-$(basename "$(pwd)")}"
  local ours
  ours="$(docker ps --filter "label=com.docker.compose.project=${project}" --format '{{.Names}}' 2>/dev/null | head -1)"
  for entry in "$@"; do
    var="${entry%%=*}"
    base="${entry#*=}"
    port="${!var:-$base}"
    [[ "$port" =~ ^[0-9]+$ ]] || port="$base"
    if fresh_port_in_use "$port"; then
      busy="$busy $port"
    fi
  done
  [ -z "$busy" ] && return 0
  if [ -n "$ours" ]; then
    # Our own stack already holds them -- this is a re-up in place.
    return 0
  fi
  echo ""
  echo "❌ Cannot start: these host ports are already bound:${busy}"
  for port in $busy; do
    holder="$(docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null | grep -E "[:.]${port}->" | cut -f1 | head -1)"
    if [ -n "$holder" ]; then
      echo "   ${port}  held by container '${holder}'"
    else
      echo "   ${port}  held by a non-Docker process on the host"
      echo "        find it with:  lsof -iTCP:${port} -sTCP:LISTEN -P -n"
    fi
  done
  echo ""
  echo "   Free the port, or run an isolated stack beside it:"
  echo "     ./opentr.sh start dev --fresh <name> --port-offset 100"
  echo ""
  echo "   Refusing rather than letting 'compose up' abort part way through and"
  echo "   leave half the services in 'Created' (issue #553)."
  exit 1
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
#
# Note: the PostgreSQL data dir legitimately ends up WITHOUT a marker — postgres
# requires an empty dir at initdb and then owns it 0700 as uid 70, so the host
# user can't drop a file in it. That's fine: the data-loss incident this guards
# against was an `rm -rf` of the PARENT (/mnt/nvm/opentranscribe), and the parent
# (plus the os + minio dirs) IS marked below.
write_live_data_markers() {
  local marker=".opentranscribe-live-data"
  local content="LIVE DATA — bind-mounted into the OpenTranscribe stack. DO NOT delete or 'clean up'. Managed by opentr.sh. See ./opentr.sh data-paths."
  local dir
  # Mark the leaf bind dirs AND the PARENTS of the pg/os dirs: the 2026-06
  # data-loss incident was an `rm -rf` of the parent (/mnt/nvm/opentranscribe),
  # not the leaf dirs. (The postgres leaf dir silently fails — see note above.)
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
      # *-ports.yml is a stale pre-#343 artifact, not a deployment (see #343).
      # *-baked.yml is the --no-bindmount volumes overlay (#55), also not its own deployment.
      case "$f" in *-ports.yml|*-baked.yml) continue;; esac
      n="$(basename "$f" .yml)"
      echo "   otfresh-${n} → named volumes otfresh-${n}_{postgres,minio,opensearch,redis}_data"
    done
  else
    echo "   (none — create with ./opentr.sh start dev --fresh <name>)"
  fi
}

# Build the compose-file chain used to address a fresh deployment for
# stop/status/destroy. Mirrors the dev chain (base + override + gpu), plus any
# aux overlays the deployment was started with, plus the generated
# container_name overlay LAST so its re-pinning wins. NAS is never included.
fresh_compose_chain() {
  local name="$1"
  local chain="-f docker-compose.yml -f docker-compose.override.yml"
  [ -f "docker-compose.gpu.yml" ] && chain="$chain -f docker-compose.gpu.yml"
  local aux
  while IFS= read -r aux; do
    [ -n "$aux" ] && [ -f "$aux" ] && chain="$chain -f $aux"
  done < <(fresh_read_aux "$name")
  [ -f "${FRESH_OVERLAY_DIR}/${name}.yml" ] && chain="$chain -f ${FRESH_OVERLAY_DIR}/${name}.yml"
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
  # Drain CUDA-holding workers before `down` reaches them (issue #782) -- same reasoning
  # as the main dev stack's stop path, scoped to this isolated project only.
  COMPOSE_PROJECT_NAME="$proj" ot_drain_gpu_workers "$chain"
  # --remove-orphans so an aux container from an earlier start with a --with-*
  # flag that is no longer recorded still comes down. Safe: the project is
  # isolated by construction, so nothing outside this deployment can be hit.
  # shellcheck disable=SC2086
  COMPOSE_PROJECT_NAME="$proj" docker compose $chain down --remove-orphans 2>/dev/null || true
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
  local offset
  offset="$(fresh_read_offset "$name")"
  if [ "$offset" -ne 0 ]; then
    echo "📊 Fresh deployment '${name}' (project ${proj}, port offset +${offset}):"
  else
    echo "📊 Fresh deployment '${name}' (project ${proj}, standard dev ports):"
  fi
  # shellcheck disable=SC2086
  COMPOSE_PROJECT_NAME="$proj" docker compose $chain ps 2>/dev/null || true
}

# `fresh-list` — list all known fresh deployments (by generated overlay) plus
# their running containers and volumes.
fresh_list() {
  echo "🧪 Fresh deployments:"
  if [ -d "$FRESH_OVERLAY_DIR" ] && ls "${FRESH_OVERLAY_DIR}"/*.yml >/dev/null 2>&1; then
    local f n proj running offset ports
    for f in "${FRESH_OVERLAY_DIR}"/*.yml; do
      # *-ports.yml is a stale pre-#343 artifact, not a deployment (see #343).
      # *-baked.yml is the --no-bindmount volumes overlay (#55), also not its own deployment —
      # skip both or fresh_project_name mangles the suffix into a bogus second entry.
      case "$f" in *-ports.yml|*-baked.yml) continue;; esac
      n="$(basename "$f" .yml)"
      proj="$(fresh_project_name "$n")"
      running="$(docker ps --filter "name=^${proj}-" --format '{{.Names}}' 2>/dev/null | wc -l | tr -d ' ')"
      offset="$(fresh_read_offset "$n")"
      if [ "$offset" -ne 0 ]; then
        ports="port offset +${offset} → frontend $((${FRONTEND_PORT:-5173} + offset)), backend $((${BACKEND_PORT:-5174} + offset))"
      else
        ports="standard dev ports"
      fi
      echo "  • ${n}  (project ${proj}, ${running} container(s) running, ${ports})"
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
  echo "   Generated files to remove:"
  # <name>.yml = container_name overlay, <name>.offset = recorded --port-offset,
  # <name>.aux = recorded aux overlays (#347),
  # <name>-ports.yml = stale pre-#343 port overlay (see #343),
  # <name>-baked.yml = --no-bindmount volumes overlay (#55).
  ls "${FRESH_OVERLAY_DIR}/${name}.yml" "${FRESH_OVERLAY_DIR}/${name}.offset" \
     "${FRESH_OVERLAY_DIR}/${name}.aux" \
     "${FRESH_OVERLAY_DIR}/${name}-ports.yml" \
     "${FRESH_OVERLAY_DIR}/${name}-baked.yml" 2>/dev/null | sed 's/^/     - /' || true
  # The one host directory this deployment owns outright (fresh_diar_native_models_dir):
  # its own diar-native export, not the live one. Listed separately from the
  # generated-files glob above because it is a directory tree (up to ~462MB), not a
  # small generated file, and deleting it is what stops a --fresh stack from leaking
  # that export on every destroy the way llm-test's container used to leak a GPU (#347).
  local diar_dir
  diar_dir="$(fresh_diar_native_models_dir "$name")"
  if [ -d "$diar_dir" ]; then
    echo "   Isolated diar-native models directory to remove:"
    echo "     - $diar_dir"
  fi
  # Project-scoped IMAGE TAGS (#759). A --fresh build writes
  # `opentranscribe-{backend,frontend,docs}:otfresh-<name>` because start_app() exports
  # OT_DEV_IMAGE_TAG="$FRESH_PROJECT"; without reclaiming them here, every fresh
  # deployment that ever built leaves multi-GB images behind forever. Matched on the
  # TAG being exactly this project's name, so `:latest` and every other deployment's
  # tag are structurally unreachable from here.
  local imgs
  imgs="$(docker images --filter "reference=*:${proj}" --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | sort -u || true)"
  if [ -n "$imgs" ]; then
    echo "   Project-scoped image tags to remove:"
    echo "$imgs" | sed 's/^/     - /'
  fi
  echo ""
  echo "   This touches ONLY this isolated project and its own directories — no LIVE"
  echo "   bind paths, no other stack."
  printf "   Proceed? (y/N) "
  read -r confirm
  if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "❌ Destroy cancelled."
    return 0
  fi

  # COMPOSE_PROFILES="*" (supported since Compose v2.24) is load-bearing here, not
  # cosmetic: `docker compose down` only tears down services whose profile is ACTIVE
  # for THIS invocation, not services that merely exist in the project. A --fresh
  # deployment started with --gpu-scale (or --with-gpu-split / any other
  # `profiles:`-gated service, e.g. celery-worker-gpu-scaled) leaves that container
  # AND its named volumes running after a `fresh-destroy` that printed success —
  # reproduced live 2026-09-05: `otfresh-xcard711-celery-worker-gpu-scaled` stayed
  # "Up" and `otfresh-xcard711_pipeline_scratch` stayed mounted after this function
  # reported "destroyed", because the down call carried no COMPOSE_PROFILES at all.
  # Exactly the class of leak #347 closed for `--with-llm-test`'s vLLM container --
  # profile-gated services need the same treatment here, generically, rather than
  # enumerating every profile this repo happens to define today.
  # Drain CUDA-holding workers before the destructive `down -v` below (issue #782).
  COMPOSE_PROJECT_NAME="$proj" ot_drain_gpu_workers "$chain"

  # shellcheck disable=SC2086
  local _down_rc=0
  COMPOSE_PROJECT_NAME="$proj" COMPOSE_PROFILES="*" docker compose $chain down -v --remove-orphans 2>/dev/null || _down_rc=$?

  # The NETWORK is the third leak in this function (issue #772), and it was the one
  # resource with no explicit reclaim step: volumes and image tags each get one below,
  # the network got only whatever `down` managed. `down`'s failure was discarded by
  # `2>/dev/null || true`, so a network it could not remove was indistinguishable from
  # one it did — and unlike a leaked container, nothing later makes the leak visible.
  #
  # It is not hypothetical and it is not cheap: ELEVEN accumulated on this host, each
  # holding a /16 out of Docker's default pool (172.17-172.31), until
  # `docker network create` began failing HOST-WIDE with "all predefined address pools
  # have been fully subnetted" — blocking unrelated projects and a release task, with
  # nothing pointing back here.
  #
  # ⚠️ Removal genuinely can fail for a reason we cannot fix: Docker reports
  # `has active endpoints` while the network shows `containers=0` and no container,
  # running or stopped, is attached. That is a stale endpoint record and it survives
  # teardown; only a daemon restart clears it. So the goal here is NOT to guarantee
  # removal — it is to make the failure LOUD and name the cost, while the operator can
  # still act, instead of discovering it weeks later as an unrelated host-wide error.
  local _net="${proj}_default"
  if docker network inspect "$_net" >/dev/null 2>&1; then
    if docker network rm "$_net" >/dev/null 2>&1; then
      echo "  removed leftover network ${_net}"
    else
      echo "⚠️  network ${_net} could NOT be removed and is now leaked." >&2
      echo "    It still holds a subnet from Docker's default pool. Enough of these and" >&2
      echo "    \`docker network create\` fails host-wide for every project on this machine." >&2
      echo "    Check: docker network inspect ${_net} --format '{{len .Containers}}'" >&2
      echo "    If that prints 0, the endpoints are stale and only a Docker daemon restart" >&2
      echo "    clears them — \`docker network prune\` uses the same path and will also fail." >&2
    fi
  fi

  # A non-zero `down` is worth saying out loud even when the network came away cleanly:
  # it means something in the teardown did not do what it said, and every check below
  # this point is then reporting on a partial teardown.
  if [ "$_down_rc" -ne 0 ]; then
    echo "⚠️  \`docker compose down\` exited ${_down_rc} for ${proj} — teardown may be incomplete." >&2
    echo "    Re-run: ./opentr.sh fresh-destroy ${name}   (or inspect: docker ps -a --filter label=com.docker.compose.project=${proj})" >&2
  fi

  # Catch any stragglers the compose chain didn't own.
  if [ -n "$vols" ]; then
    echo "$vols" | xargs -r docker volume rm 2>/dev/null || true
  fi
  # Reclaim the project-scoped image tags (#759). `docker compose down --rmi local` is NOT
  # a substitute: it removes images the compose chain can still see, and a fresh deployment
  # whose overlay has already been deleted, or which was built and then stopped, leaves tags
  # the chain no longer resolves. Re-resolved here rather than reusing the `$imgs` captured
  # before the confirmation prompt, so a build that finished in between is still caught.
  # `docker rmi` on a tag UNTAGS when other tags share the image id, so this cannot delete
  # an image `:latest` still points at.
  local imgs_now
  imgs_now="$(docker images --filter "reference=*:${proj}" --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | sort -u || true)"
  if [ -n "$imgs_now" ]; then
    echo "$imgs_now" | xargs -r docker rmi 2>/dev/null || true
  fi
  rm -f "${FRESH_OVERLAY_DIR}/${name}.yml" "${FRESH_OVERLAY_DIR}/${name}.offset" \
        "${FRESH_OVERLAY_DIR}/${name}.aux" \
        "${FRESH_OVERLAY_DIR}/${name}-ports.yml" \
        "${FRESH_OVERLAY_DIR}/${name}-baked.yml" 2>/dev/null || true

  # Reclaim the isolated diar-native export directory (see the listing comment above for
  # why this is handled separately from the generated-files glob). A bare `rm -rf ... ||
  # true` cannot remove a tree that `fresh_prepare_diar_native_models_dir` chowned to
  # CONTAINER_UID_GID (root, on a host with no subuid mapping for that gid) — it fails
  # AND leaves every byte in place, but the `|| true` swallowed that as if there had been
  # nothing to do, so the ✅ line below printed success while up to 462MB stayed on disk.
  # Adversarial-audit finding: reproduced with `BEFORE: 5.1M ... AFTER: 5.1M`, i.e. the
  # very #347 leak this directory's isolation exists to close. Retry once with the same
  # docker-busybox chown fix_model_cache_permissions uses for the identical ownership
  # hazard on the main $MODEL_CACHE_DIR path, then report what is ACTUALLY true on disk —
  # never a fixed success line regardless of outcome.
  local diar_dir_left=""
  if [ -d "$diar_dir" ]; then
    rm -rf "$diar_dir" 2>/dev/null || true
    if [ -d "$diar_dir" ] && command -v docker &>/dev/null; then
      local diar_dir_abs
      diar_dir_abs="$(cd "$diar_dir" && pwd)"
      docker run --rm -v "${diar_dir_abs}:/reclaim" busybox:latest \
        chown -R "$(id -u):$(id -g)" /reclaim >/dev/null 2>&1 || true
      rm -rf "$diar_dir" 2>/dev/null || true
    fi
    if [ -d "$diar_dir" ]; then
      diar_dir_left="$diar_dir"
    else
      # Drop the now-empty per-deployment parent (.fresh/<name>/) too. `rmdir` refuses a
      # non-empty directory, so this is a silent no-op if anything else still lives there.
      rmdir "$(dirname "$diar_dir")" 2>/dev/null || true
    fi
  fi

  if [ -n "$diar_dir_left" ]; then
    echo "⚠️  Fresh deployment '${name}' destroy INCOMPLETE: containers, volumes and generated"
    echo "   files were removed, but the isolated diar-native models directory could not be"
    echo "   reclaimed — even after a docker-based chown attempt (likely root-owned from a"
    echo "   container that provisioned it, with no docker daemon access to fix it here):"
    echo "     - $diar_dir_left"
    echo "   Remove it by hand (e.g. 'sudo rm -rf ${diar_dir_left}') to reclaim the space."
  else
    echo "✅ Fresh deployment '${name}' destroyed (containers + volumes + generated files)."
  fi
}

# Function to start the environment
start_app() {
  ENVIRONMENT=${1:-dev}
  shift || true  # Remove first argument

  # Parse optional flags
  BUILD_FLAG=""
  GPU_SCALE_FLAG=""
  GPU_SPLIT_FLAG=""
  GPU_DEVICE_OVERRIDE=""
  DIAR_NATIVE_GPU_OVERRIDE=""
  NAS_FLAG=""
  PULL_FLAG=""
  WITH_PKI_FLAG=""
  WITH_LDAP_TEST_FLAG=""
  WITH_MOCK_LLM_FLAG=""
  WITH_MOCK_ASR_FLAG=""
  WITH_SCRATCH_TMPFS_FLAG=""
  WITH_DIAR_NATIVE_FLAG=""
  NO_DIAR_NATIVE_FLAG=""
  WITH_LLM_TEST_FLAG=""
  WITH_KEYCLOAK_TEST_FLAG=""
  WITH_AUTHENTIK_TEST_FLAG=""
  WITH_WATCH_FLAG=""
  WITH_SMB_TEST_FLAG=""
  WITH_MONITORING_FLAG=""
  WITH_BACKUP_FLAG=""
  LITE_FLAG=""
  CPU_FLAG=""
  NO_NAS_FLAG=""
  FRESH_FLAG=""
  FRESH_NAME=""
  PORT_OFFSET=""
  DRY_RUN_FLAG=""
  SEED_BENCHMARK_FLAG=""
  NO_BINDMOUNT_FLAG=""

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
      --gpu-device)
        shift
        # A missing value would abort on `set -u`; fail with something readable.
        if [ $# -eq 0 ] || [ "${1#-}" != "$1" ]; then
          echo "❌ --gpu-device requires a GPU index (e.g. --gpu-device 1)"
          exit 1
        fi
        GPU_DEVICE_OVERRIDE="$1"
        shift
        ;;
      --diar-native-gpu)
        shift
        # Deliberately NARROWER than --gpu-device: it moves only DIAR_NATIVE_GPU,
        # leaving GPU_SCALE_DEVICE_ID / GPU_DEVICE_ID / etc. wherever .env or
        # --gpu-device already put them. Exists to test the cross-card arrangement
        # the shipped defaults actually describe (issue #711 criterion 5:
        # GPU_SCALE_DEVICE_ID defaults to 2, DIAR_NATIVE_GPU defaults to
        # GPU_DEVICE_ID, i.e. 0) -- --gpu-device alone cannot express two different
        # cards because it pins every var in GPU_DEVICE_VARS to the same value.
        if [ $# -eq 0 ] || [ "${1#-}" != "$1" ]; then
          echo "❌ --diar-native-gpu requires a GPU index (e.g. --diar-native-gpu 1)"
          exit 1
        fi
        DIAR_NATIVE_GPU_OVERRIDE="$1"
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
        # A missing value would abort on `set -u`; fail with something readable.
        if [ $# -eq 0 ] || [ "${1#-}" != "$1" ]; then
          echo "❌ --port-offset requires a value (e.g. --port-offset 100)"
          exit 1
        fi
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
      --no-bindmount)
        NO_BINDMOUNT_FLAG="--no-bindmount"
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
      --with-mock-llm)
        WITH_MOCK_LLM_FLAG="--with-mock-llm"
        shift
        ;;
      --with-mock-asr)
        WITH_MOCK_ASR_FLAG="--with-mock-asr"
        shift
        ;;
      --with-scratch-tmpfs)
        WITH_SCRATCH_TMPFS_FLAG="--with-scratch-tmpfs"
        shift
        ;;
      --with-diar-native)
        WITH_DIAR_NATIVE_FLAG="--with-diar-native"
        shift
        ;;
      --no-diar-native)
        NO_DIAR_NATIVE_FLAG="--no-diar-native"
        shift
        ;;
      --with-llm-test)
        WITH_LLM_TEST_FLAG="--with-llm-test"
        shift
        ;;
      --with-keycloak-test)
        WITH_KEYCLOAK_TEST_FLAG="--with-keycloak-test"
        shift
        ;;
      --with-authentik-test)
        WITH_AUTHENTIK_TEST_FLAG="--with-authentik-test"
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
      --with-backup)
        WITH_BACKUP_FLAG="--with-backup"
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
  if [ -n "$FRESH_FLAG" ]; then
    if [ "$ENVIRONMENT" != "dev" ]; then
      echo "❌ --fresh is only supported in dev mode (./opentr.sh start dev --fresh [name])"
      exit 1
    fi
    FRESH_NAME="$(fresh_sanitize_name "$FRESH_NAME")"
    FRESH_PROJECT="$(fresh_project_name "$FRESH_NAME")"

    # Resolve port offset. Explicit --port-offset wins and is recorded; otherwise
    # reuse this deployment's recorded offset so a re-up lands on the same ports.
    # Default 0 → standard dev ports so conftest/e2e work unchanged.
    local _offset
    if [ -n "$PORT_OFFSET" ]; then
      _offset="$PORT_OFFSET"
      if ! [[ "$_offset" =~ ^[0-9]+$ ]]; then
        echo "❌ --port-offset must be a non-negative integer (got '$_offset')"
        exit 1
      fi
    else
      _offset="$(fresh_read_offset "$FRESH_NAME")"
      [ "$_offset" -ne 0 ] && echo "ℹ️  Reusing recorded port offset +${_offset} for '${FRESH_NAME}' (pass --port-offset 0 to reset)."
    fi

    # Export the *_PORT variables the base compose files interpolate, offset by
    # $_offset. This moves the SINGLE existing `ports:` entry — see FRESH_PORT_VARS
    # for why an overlay must not be used (issue #343).
    #
    # Aux test overlays join the same treatment when their flag is passed: their
    # ports are offset here, their hard-coded container_names are re-pinned by
    # the generated overlay, and the files are recorded so stop/status/destroy
    # address the whole deployment (issue #347).
    local _port_vars=("${FRESH_PORT_VARS[@]}")
    local _aux_services=()
    local _aux_files=()
    if [ -n "$WITH_KEYCLOAK_TEST_FLAG" ]; then
      _port_vars+=("${FRESH_KEYCLOAK_PORT_VARS[@]}")
      _aux_files+=("docker-compose.keycloak.yml")
    fi
    if [ -n "$WITH_AUTHENTIK_TEST_FLAG" ]; then
      _port_vars+=("${FRESH_AUTHENTIK_PORT_VARS[@]}")
      _aux_files+=("docker-compose.authentik.yml")
    fi
    if [ -n "$WITH_LDAP_TEST_FLAG" ]; then
      _port_vars+=("${FRESH_LDAP_PORT_VARS[@]}")
      _aux_services+=("${FRESH_LDAP_SERVICES[@]}")
      _aux_files+=("docker-compose.ldap-test.yml")
    fi
    if [ -n "$WITH_MOCK_LLM_FLAG" ]; then
      _port_vars+=("${FRESH_MOCK_LLM_PORT_VARS[@]}")
      _aux_services+=("${FRESH_MOCK_LLM_SERVICES[@]}")
      _aux_files+=("docker-compose.mock-llm.yml")
    fi
    if [ -n "$WITH_MOCK_ASR_FLAG" ]; then
      _port_vars+=("${FRESH_MOCK_ASR_PORT_VARS[@]}")
      _aux_services+=("${FRESH_MOCK_ASR_SERVICES[@]}")
      _aux_files+=("docker-compose.mock-asr.yml")
    fi
    # diar-native is handled separately, below, AFTER DIAR_NATIVE_MODELS_DIR is forced to
    # this deployment's isolated export path — see that comment for why (issue: the
    # aux-recording-vs-auto-load ordering finding on feat/diar-native-e2e).
    if [ -n "$WITH_SMB_TEST_FLAG" ]; then
      _port_vars+=("${FRESH_SMB_PORT_VARS[@]}")
      _aux_services+=("${FRESH_SMB_SERVICES[@]}")
      _aux_files+=("docker-compose.smb-test.yml")
    fi
    if [ -n "$WITH_MONITORING_FLAG" ]; then
      _port_vars+=("${FRESH_MONITORING_PORT_VARS[@]}")
      _aux_services+=("${FRESH_MONITORING_SERVICES[@]}")
      _aux_files+=("docker-compose.monitoring.yml")
    fi
    if [ -n "$WITH_LLM_TEST_FLAG" ]; then
      _port_vars+=("${FRESH_LLM_TEST_PORT_VARS[@]}")
      _aux_services+=("${FRESH_LLM_TEST_SERVICES[@]}")
      _aux_files+=("docker-compose.llm-test.yml")
    fi
    if [ -n "$WITH_PKI_FLAG" ]; then
      _port_vars+=("${FRESH_PKI_PORT_VARS[@]}")
      _aux_files+=("docker-compose.pki-dev.yml" "scripts/pki/test-certs/pki-test.compose.yml")
    fi
    fresh_apply_port_offset "$_offset" "${_port_vars[@]}"

    # Refuse to start when a port this stack needs is already bound. At offset 0
    # that is normally the main stack; at a non-zero offset it is a poorly chosen
    # offset. Exception: when THIS SAME fresh project holds them, proceed —
    # `compose up -d` just recreates changed services (e.g. after a .env edit).
    local _busy=""
    local _entry
    for _entry in "${FRESH_RESOLVED_PORTS[@]}"; do
      if fresh_port_in_use "${_entry#*:}"; then _busy="$_busy ${_entry#*:}"; fi
    done
    if [ -n "$_busy" ]; then
      local _holder
      _holder="$(docker ps --filter "label=com.docker.compose.project=${FRESH_PROJECT}" --format '{{.Names}}' 2>/dev/null | head -1)"
      if [ -n "$_holder" ]; then
        echo "ℹ️  Those ports are held by this same fresh deployment (${FRESH_PROJECT}) — re-upping in place."
      elif [ "$_offset" -eq 0 ]; then
        echo "❌ Cannot start fresh deployment on the standard dev ports — already bound:${_busy}"
        echo "   The main stack appears to be running. Either stop it, or run side-by-side:"
        echo "   ./opentr.sh start dev --fresh ${FRESH_NAME} --port-offset 100"
        exit 1
      else
        echo "❌ Cannot start fresh deployment '${FRESH_NAME}' with --port-offset ${_offset} — already bound:${_busy}"
        echo "   Something else holds those ports. Pick a different --port-offset."
        exit 1
      fi
    fi

    # The host-bind overlays are the one thing --fresh still cannot isolate:
    # they mount LIVE host directories, which a fresh stack would then share
    # with (and write into) alongside the main stack. Ports and container names
    # are not the problem here — the shared directory is.
    if [ -n "$WITH_WATCH_FLAG" ] || [ -n "$WITH_BACKUP_FLAG" ]; then
      echo "⚠️  --with-watch / --with-backup bind LIVE host directories (WATCH_HOST_PATH,"
      echo "   BACKUP_HOST_PATH, BACKUP_MIRROR_HOST_PATH) into the stack. Those paths are NOT"
      echo "   isolated by --fresh — this deployment will read and write the same host folders"
      echo "   as the main stack. Point them at a scratch dir before continuing if that matters."
    fi

    # Redirect the native diarizer's model export to a directory owned by THIS
    # fresh deployment, unconditionally — even over an explicit .env
    # DIAR_NATIVE_MODELS_DIR, the same way NAS is forced off a few lines above
    # regardless of .env. An explicit value in .env is set for the MAIN stack;
    # left alone here it would apply just as ambiently to every fresh stack,
    # which is precisely the live-data hazard this exists to close (see
    # fresh_diar_native_models_dir's comment for the full finding).
    # resolve_diar_native_models_dir (called from add_diar_native_overlay,
    # later in this function) treats an already-exported DIAR_NATIVE_MODELS_DIR
    # as an explicit override and returns it unexamined, so setting it here is
    # sufficient — no change to that function is needed, and its own pinned
    # resolution tests (which never set FRESH_FLAG) are unaffected.
    export DIAR_NATIVE_MODELS_DIR
    DIAR_NATIVE_MODELS_DIR="$(fresh_diar_native_models_dir "$FRESH_NAME")"

    # Decide, right now, whether the native diarization sidecar will load for THIS
    # deployment — using add_diar_native_overlay's own predicate (engine.diarizer_backend
    # defaults to native AND the isolated DIAR_NATIVE_MODELS_DIR just above already holds
    # an export OR a HUGGINGFACE_TOKEN is configured to produce one on this startup) —
    # and record it in the aux set below if so.
    #
    # This MUST happen here, not left to start_app's real call (mode "start") to the same
    # function later on: that call runs after $COMPOSE_FILES is built, which is after
    # fresh_write_aux/fresh_generate_overlay a few lines down have already run. An
    # AUTO-LOADED sidecar decided after the .aux file is written is recorded nowhere, so
    # stop/status/fresh-destroy address a different compose chain than the one actually
    # brought up — exactly the #347 shape .aux exists to prevent. `predict` mode makes the
    # identical decision (setting WITH_DIAR_NATIVE_FLAG and printing the same banner)
    # without touching $COMPOSE_FILES, which does not exist yet at this point in
    # start_app; the real call later sees WITH_DIAR_NATIVE_FLAG already set and just adds
    # the overlay, so the banner does not print twice.
    #
    # An explicit --with-diar-native (WITH_DIAR_NATIVE_FLAG already non-empty from CLI
    # parsing above) is unaffected — add_diar_native_overlay's own auto-detect is guarded
    # on the flag being unset, so `predict` is a no-op for that case and this block still
    # records it correctly.
    add_diar_native_overlay predict
    if [ -n "$WITH_DIAR_NATIVE_FLAG" ]; then
      _aux_services+=("${FRESH_DIAR_NATIVE_SERVICES[@]}")
      _aux_files+=("docker-compose.diar-native.yml")
    fi

    fresh_write_offset "$FRESH_NAME" "$_offset"
    fresh_write_aux "$FRESH_NAME" ${_aux_files[@]+"${_aux_files[@]}"}
    FRESH_OVERLAY="$(fresh_generate_overlay "$FRESH_NAME" ${_aux_services[@]+"${_aux_services[@]}"})"
    export COMPOSE_PROJECT_NAME="$FRESH_PROJECT"

    # --fresh isolates the compose PROJECT, named volumes, ports and container_names —
    # it does NOT touch the docker IMAGE TAG a build writes to, and docker-compose.override.yml
    # hard-codes bare `opentranscribe-backend:latest` (and frontend/docs) for every dev service.
    # A build can happen here with NO --build flag: an unbuilt --with-* overlay (e.g.
    # --with-diar-native) triggers one implicitly. Left unset, that build re-tags the SAME
    # `:latest` the MAIN dev stack's already-running containers resolved to — invisible until
    # its next restart-backend silently picks up this fresh deployment's code. Exporting
    # OT_DEV_IMAGE_TAG here (docker-compose.override.yml interpolates
    # `${OT_DEV_IMAGE_TAG:-latest}`) makes a fresh build write `opentranscribe-backend:otfresh-<name>`
    # instead — same pattern as DIAR_NATIVE_MODELS_DIR a few lines above: force it unconditionally,
    # not merely `:-`, because an ambient OT_DEV_IMAGE_TAG left over from a previous fresh shell
    # session must not leak into THIS one.
    export OT_DEV_IMAGE_TAG="$FRESH_PROJECT"

    echo ""
    echo "🧪 FRESH DEPLOYMENT '${FRESH_NAME}': isolated project + volumes; NAS overlay IGNORED; real data untouched."
    echo "   Project: ${FRESH_PROJECT}  (containers: ${FRESH_PROJECT}-*)"
    if [ "$_offset" -ne 0 ]; then
      echo "   Port offset: +${_offset} (recorded in $(fresh_offset_file "$FRESH_NAME"))"
    else
      echo "   Port offset: none — standard dev ports"
    fi
    echo "   Published ports:$(fresh_port_summary)"
    echo "   diar-native models: ${DIAR_NATIVE_MODELS_DIR} (isolated copy, not the live export)"
    echo ""

    # Force NAS off in fresh mode regardless of .env.
    NO_NAS_FLAG="--no-nas"
    NAS_FLAG=""

    if [ -n "$NO_BINDMOUNT_FLAG" ]; then
      echo "🖼️  --no-bindmount: this deployment will run the BUILT IMAGE, not live"
      echo "   ./backend edits — a code change needs '--build' + a fresh 'up', not a save."
      echo ""
    fi
  else
    if [ -n "$PORT_OFFSET" ]; then
      echo "⚠️  --port-offset is only honored in fresh mode (--fresh); ignoring."
    fi
    if [ -n "$NO_BINDMOUNT_FLAG" ]; then
      echo "⚠️  --no-bindmount is only honored in fresh mode (--fresh); ignoring."
    fi

    # Mirror image: the --fresh branch above unconditionally pins OT_DEV_IMAGE_TAG to
    # this deployment's own project. This non-fresh branch must be equally
    # unconditional in the OTHER direction — reset it to "latest" rather than leaving
    # it unset, or an OT_DEV_IMAGE_TAG left exported in the invoking shell (a prior
    # `--fresh` session, a CI job, a wrapper script) would silently carry over and
    # make a plain `start dev` build/run the SHARED opentranscribe-backend against a
    # stale fresh tag instead of :latest — the same cross-contamination this file
    # exists to prevent, just mirrored.
    export OT_DEV_IMAGE_TAG="latest"
  fi

  if [ -n "$GPU_SCALE_FLAG" ] && [ -n "$GPU_SPLIT_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-scale,gpu-split"
  elif [ -n "$GPU_SCALE_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-scale"
  elif [ -n "$GPU_SPLIT_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-split"
  fi

  echo "🚀 Starting OpenTranscribe in ${ENVIRONMENT} mode..."

  if [ -n "$GPU_DEVICE_OVERRIDE" ]; then
    apply_gpu_device_override "$GPU_DEVICE_OVERRIDE"
    warn_gpu_device_override_conflicts "$GPU_DEVICE_OVERRIDE"
  fi

  # Applied AFTER --gpu-device so a combined
  # `--gpu-device 2 --diar-native-gpu 1` (or the reverse) can express two
  # different cards; --gpu-device alone would have just pinned DIAR_NATIVE_GPU
  # back to the same value as everything else.
  if [ -n "$DIAR_NATIVE_GPU_OVERRIDE" ]; then
    if ! [[ "$DIAR_NATIVE_GPU_OVERRIDE" =~ ^[0-9]+$ ]]; then
      echo "❌ --diar-native-gpu must be a non-negative integer GPU index (got '$DIAR_NATIVE_GPU_OVERRIDE')"
      exit 1
    fi
    export DIAR_NATIVE_GPU="$DIAR_NATIVE_GPU_OVERRIDE"
    echo "🎯 --diar-native-gpu $DIAR_NATIVE_GPU_OVERRIDE: pinning the diar-native sidecar to host GPU $DIAR_NATIVE_GPU_OVERRIDE (overrides .env)"
  fi

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

  # Side-effecting host preparation. Skipped under --dry-run so the compose chain
  # can be resolved and validated cheaply (scripts/validate-deployments.sh runs this
  # ~20 times). ensure_opensearch_models in particular will `docker pull` a multi-GB
  # backend image when the model cache is cold, which a validation loop must never do.
  # detect_and_configure_hardware above is deliberately NOT skipped: it is read-only
  # and exports DOCKER_RUNTIME/COMPUTE_TYPE/TORCH_DEVICE, which the compose files
  # interpolate — skipping it would validate a different config than we run.
  if [ -z "$DRY_RUN_FLAG" ]; then
    # Create necessary directories
    create_required_dirs

    # Fix model cache permissions for non-root container
    fix_model_cache_permissions

    # fix_model_cache_permissions only reaches $MODEL_CACHE_DIR. A --fresh
    # deployment's diar-native export lives OUTSIDE it by design (see
    # fresh_diar_native_models_dir), so it needs this same ownership fix
    # applied to its own directory, BEFORE `compose up` — never after, per
    # fresh_prepare_diar_native_models_dir's own comment on the NOT_WRITABLE
    # hazard of letting dockerd create the bind-mount source itself.
    if [ -n "$FRESH_FLAG" ]; then
      fresh_prepare_diar_native_models_dir "$DIAR_NATIVE_MODELS_DIR"
    fi

    # Generate a real MinIO KMS secret key if .env still has .env.example's
    # shipped placeholder, so a genuinely fresh `cp .env.example .env` boots
    # MinIO's KMS auto-encryption without manual intervention (issue #614).
    ensure_minio_kms_secret ".env"

    # Fetch the NLTK corpora BEFORE de-hardlinking them: nothing else prefetches
    # them, so they were fetched at runtime from inside the transcription and
    # topic pipelines, which an airgapped deployment cannot do (issue #491).
    ensure_nltk_corpora

    # NLTK >= 3.10 refuses multiply-linked files (issue #491)
    ensure_nltk_data_unlinked

    # Ensure OpenSearch neural models are downloaded for offline capability
    ensure_opensearch_models
  fi

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
  # The gpu-split overlay grants each worker its dedicated GPU reservation.
  if [ -n "$GPU_SPLIT_FLAG" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.gpu-split.yml"
    echo "🔀 Adding GPU split overlay (docker-compose.gpu-split.yml)"
  fi

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

  # Add PKI overlay if requested (dev routes to docker-compose.pki-dev.yml,
  # prod to docker-compose.pki.yml; cert generation + the test-env fragment
  # are handled inside add_pki_overlay -> scripts/pki/generate-test-env.sh)
  add_pki_overlay

  # Add mock LLM provider if requested
  if [ -n "$WITH_MOCK_LLM_FLAG" ]; then
    if [ -f "docker-compose.mock-llm.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.mock-llm.yml"
      echo "🤖 Adding mock LLM provider (docker-compose.mock-llm.yml)"
      echo "   From containers: http://mock-llm:5199/v1   From host: http://localhost:${MOCK_LLM_PORT:-5199}/v1"
      echo "   Models: mock-gpt (normal) mock-echo mock-empty mock-error mock-slow"
    else
      echo "⚠️  --with-mock-llm specified but docker-compose.mock-llm.yml not found"
    fi
  fi

  # Add mock cloud ASR provider if requested
  if [ -n "$WITH_MOCK_ASR_FLAG" ]; then
    if [ -f "docker-compose.mock-asr.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.mock-asr.yml"
      echo "🤖 Adding mock cloud ASR provider (docker-compose.mock-asr.yml)"
      echo "   From containers: http://mock-asr:5198   From host: http://localhost:${MOCK_ASR_PORT:-5198}"
      echo "   Scenarios: ok (default) error malformed upload-reject"
    else
      echo "⚠️  --with-mock-asr specified but docker-compose.mock-asr.yml not found"
    fi
  fi

  # Add the opt-in tmpfs override for the pipeline_scratch handoff volume if requested
  # (issue #661 E5). No isolation dispatch needed: the overlay declares neither `ports:`
  # nor `container_name:`, only a driver override for the already project-namespaced
  # `pipeline_scratch` volume — see the --fresh aux-isolation exemption comment in
  # backend/tests/unit/test_opentr_fresh_aux_isolation.py.
  if [ -n "$WITH_SCRATCH_TMPFS_FLAG" ]; then
    if [ -f "docker-compose.scratch-tmpfs.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.scratch-tmpfs.yml"
      echo "🧠 Adding RAM-backed scratch volume override (docker-compose.scratch-tmpfs.yml)"
      echo "   pipeline_scratch is now tmpfs, size=${SCRATCH_TMPFS_SIZE:-2g}"
    else
      echo "⚠️  --with-scratch-tmpfs specified but docker-compose.scratch-tmpfs.yml not found"
    fi
  fi

  # Native diarization sidecar. Shared with rebuild-backend so a rebuild can never
  # drop celery-worker's DIAR_NATIVE_URL / shared pipeline_scratch diar/ handoff namespace — see add_diar_native_overlay.
  add_diar_native_overlay start

  # Add real GPU-backed LLM test provider if requested
  if [ -n "$WITH_LLM_TEST_FLAG" ]; then
    if [ -f "docker-compose.llm-test.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.llm-test.yml"
      echo "🧠 Adding real LLM test provider (docker-compose.llm-test.yml)"
      echo "   vLLM — from containers: http://llm-test-vllm:8000/v1   from host: http://localhost:${LLM_TEST_PORT:-5195}/v1"
      echo "   Model: ${LLM_TEST_SERVED_NAME:-gemma-4-e4b}   GPU: ${LLM_TEST_GPU_DEVICE_ID:-2}"
      echo "   Ollama alternative (not auto-started): docker compose ... --profile ollama up -d llm-test-ollama"
    else
      echo "⚠️  --with-llm-test specified but docker-compose.llm-test.yml not found"
    fi
  fi

  # Add LDAP test container if requested
  if [ -n "$WITH_LDAP_TEST_FLAG" ]; then
    if [ -f "docker-compose.ldap-test.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.ldap-test.yml"
      echo "🔐 Adding LDAP test container (docker-compose.ldap-test.yml)"
      echo "   LDAP server: localhost:${LDAP_TEST_PORT:-3890}  (from containers: ldap://lldap-test:3890)"
      echo "   Web UI: http://localhost:${LDAP_TEST_UI_PORT:-17170}"
    else
      echo "⚠️  --with-ldap-test specified but docker-compose.ldap-test.yml not found"
    fi
  fi

  # Add Keycloak test container if requested
  if [ -n "$WITH_KEYCLOAK_TEST_FLAG" ]; then
    if [ -f "docker-compose.keycloak.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.keycloak.yml"
      echo "🔐 Adding Keycloak test container (docker-compose.keycloak.yml)"
      echo "   Keycloak URL: http://localhost:${KEYCLOAK_PORT:-8180}"
      echo "   Admin credentials: admin / admin"
    else
      echo "⚠️  --with-keycloak-test specified but docker-compose.keycloak.yml not found"
    fi
  fi

  # Add Authentik test container if requested
  if [ -n "$WITH_AUTHENTIK_TEST_FLAG" ]; then
    if [ -f "docker-compose.authentik.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.authentik.yml"
      echo "🔐 Adding Authentik test container (docker-compose.authentik.yml)"
      echo "   Authentik URL: http://localhost:${AUTHENTIK_PORT:-9022}"
      echo "   Bootstrap credentials: ${AUTHENTIK_BOOTSTRAP_EMAIL:-admin@example.com} / ${AUTHENTIK_BOOTSTRAP_PASSWORD:-admin_password}"
    else
      echo "⚠️  --with-authentik-test specified but docker-compose.authentik.yml not found"
    fi
  fi

  # Add Watch Sources overlay if requested (mounts the host watch folder)
  if [ -n "$WITH_WATCH_FLAG" ]; then
    if [ -f "docker-compose.watch.yml" ]; then
      WATCH_HOST_PATH="${WATCH_HOST_PATH:-./watch}"
      mkdir -p "$WATCH_HOST_PATH"
      # Match the non-root container user so imports can read/write. appuser is
      # uid 1000 / gid 999 (see CONTAINER_UID_GID in scripts/common.sh) — the owner
      # bit is what the import path needs, but keep the GID honest.
      chown -R "${CONTAINER_UID_GID:-1000:999}" "$WATCH_HOST_PATH" 2>/dev/null || true
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
      echo "   SMB share: smb://localhost:${SMB_TEST_PORT:-4450}/media  (testuser / testpass)"
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

  # Add Backup overlay if requested (mounts BACKUP_HOST_PATH for scheduled backups
  # and BACKUP_MIRROR_HOST_PATH for the incremental media mirror, issue #242)
  if [ -n "$WITH_BACKUP_FLAG" ]; then
    if [ -f "docker-compose.backup.yml" ]; then
      BACKUP_HOST_PATH="${BACKUP_HOST_PATH:-./backups}"
      BACKUP_MIRROR_HOST_PATH="${BACKUP_MIRROR_HOST_PATH:-./media-mirror}"
      mkdir -p "$BACKUP_HOST_PATH" 2>/dev/null || true
      mkdir -p "$BACKUP_MIRROR_HOST_PATH" 2>/dev/null || true
      export BACKUP_HOST_PATH BACKUP_MIRROR_HOST_PATH
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.backup.yml"
      echo "💾 Adding Backup overlay (docker-compose.backup.yml)"
      echo "   Backup destination: $BACKUP_HOST_PATH → /backups (configure in admin UI → Backups)"
      echo "   Media mirror:       $BACKUP_MIRROR_HOST_PATH → /media-mirror (admin UI → Backups → Media Mirror)"
    else
      echo "⚠️  --with-backup specified but docker-compose.backup.yml not found"
    fi
  fi

  # Fresh-mode overlay goes LAST so its container_name re-pinning wins. Ports are
  # NOT overlaid — they come from the exported *_PORT vars (see FRESH_PORT_VARS).
  if [ -n "$FRESH_FLAG" ] && [ -n "$FRESH_OVERLAY" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f $FRESH_OVERLAY"
  fi

  # --no-bindmount: replace the ./backend:/app hot-reload bind (and drop
  # --reload) on every service it's mounted into, so the fresh stack runs the
  # built image instead of whatever is on disk (issue #55). Generated from
  # `docker compose config` over the EXACT chain assembled above (GPU + aux
  # overlays + the fresh container_name overlay all included), then appended
  # last so its !override sees — and replaces — the fully-merged volume list.
  if [ -n "$FRESH_FLAG" ] && [ -n "$NO_BINDMOUNT_FLAG" ]; then
    FRESH_BAKED_OVERLAY="$(fresh_generate_baked_overlay "$FRESH_NAME" "$COMPOSE_FILES")"
    COMPOSE_FILES="$COMPOSE_FILES -f $FRESH_BAKED_OVERLAY"
  fi

  # --no-bindmount means "the IMAGE is the code", so it MUST force a recreate.
  #
  # Compose keys recreation on the SERVICE CONFIG, and `opentranscribe-backend:latest`
  # is the same *name* even when the tag points at a different image — so `up -d --build`
  # rebuilt the image and left the old containers running. Measured: the measurement stack
  # served a two-hour-old image (866eace4) while the tag had moved to 9bfae667, and
  # `printenv GIT_SHA` inside the container disagreed with the SHA baked into that image.
  # "Rebuild + redeploy" deployed nothing, silently, and every measurement taken against
  # it described code nobody was running.
  #
  # Fatal for a BAKED stack specifically: with the bind mount gone there is no other route
  # for new code to reach the container. Bind-mounted dev is unaffected (the source is
  # live), which is why this is scoped to the flag instead of applied to every start.
  #
  # Computed BEFORE the dry-run block on purpose — a preview that omits a flag the real
  # run passes is the same class of defect as the bug above.
  #
  # ⚠️ MEASURED 2026-08-20: THIS IS NOT SUFFICIENT ON ITS OWN. `bash -x` confirms the flag
  # reaches the wire — `up -d --wait --wait-timeout 700 --build --force-recreate` — and
  # compose still reported 0 "Recreated" lines and left containers at their previous
  # creation timestamp. So `--force-recreate` is necessary but not honoured here, and the
  # cause is in compose rather than in this script (issue #75, still open).
  #
  # Until that is understood, VERIFY after any rebuild that is going to be measured:
  #     docker exec <proj>-backend printenv GIT_SHA
  #     docker run --rm --entrypoint printenv opentranscribe-backend:latest GIT_SHA
  # and if they disagree, recreate surgically (this DOES work):
  #     COMPOSE_PROJECT_NAME=<proj> <the *_PORT vars for the offset> \
  #     docker compose <the same -f chain> up -d --no-deps --force-recreate \
  #       backend celery-worker celery-embedding-worker
  RECREATE_CMD=""
  if [[ -n "$NO_BINDMOUNT_FLAG" ]]; then
    RECREATE_CMD="--force-recreate"
  fi

  # Dry-run: print exactly what WOULD run and exit without touching Docker.
  if [ -n "$DRY_RUN_FLAG" ]; then
    echo ""
    echo "🔎 DRY RUN — no containers started."
    echo "   COMPOSE_PROJECT_NAME: ${COMPOSE_PROJECT_NAME:-opentranscribe (default)}"
    echo "   OT_DEV_IMAGE_TAG: ${OT_DEV_IMAGE_TAG:-latest} (fresh and non-fresh both set this explicitly)"
    echo "   Compose files:"
    # shellcheck disable=SC2086
    for _f in $COMPOSE_FILES; do
      [ "$_f" = "-f" ] && continue
      echo "     - $_f"
    done
    # The values compose will interpolate into `device_ids:`. Printed always (not
    # only under --gpu-device) so a dry run answers "which card does this stack
    # actually take?" without reading .env and five overlay files.
    echo "   GPU device reservations (as compose will interpolate them):"
    # Iterates GPU_DEVICE_VARS so a device var added to the list (and to compose)
    # shows up here without a second edit; the guard test parses this block.
    _resv="  "
    for _var in "${GPU_DEVICE_VARS[@]}"; do
      _resv="${_resv} ${_var}=${!_var:-${GPU_DEVICE_ID:-0}}"
    done
    echo "   ${_resv} LLM_TEST_GPU_DEVICE_ID=${LLM_TEST_GPU_DEVICE_ID:-2}"
    echo "   Command that WOULD run:"
    echo "     docker compose $COMPOSE_FILES up -d $BUILD_CMD $RECREATE_CMD"
    [ -n "$FRESH_FLAG" ] && echo "   (fresh mode: NAS overlay omitted by design; real data untouched)"
    [ -n "$SEED_BENCHMARK_FLAG" ] && echo "   (would seed benchmark media via scripts/seed-fresh-deployment.sh after healthy)"
    return 0
  fi

  # Best-effort live-data guardrail markers (only when the NAS overlay is active
  # and we are NOT in fresh mode — fresh uses named volumes, no bind dirs).
  if [ -n "$NAS_FLAG" ] && [ -z "$FRESH_FLAG" ]; then
    write_live_data_markers
  fi

  # Refuse to start when a host port we are about to publish is already taken by
  # something that is not us (issue #553). The fresh path has had this since #347;
  # without it here, a collision makes `compose up` abort PART WAY THROUGH and
  # strand every service it had not reached in `Created`. Skipped in fresh mode,
  # which ran its own offset-aware check earlier.
  if [ -z "$FRESH_FLAG" ]; then
    _pf_ports=("${FRESH_PORT_VARS[@]}")
    [ -n "$WITH_LDAP_TEST_FLAG" ] && _pf_ports+=("${FRESH_LDAP_PORT_VARS[@]}")
    [ -n "$WITH_MOCK_LLM_FLAG" ] && _pf_ports+=("${FRESH_MOCK_LLM_PORT_VARS[@]}")
    [ -n "$WITH_MOCK_ASR_FLAG" ] && _pf_ports+=("${FRESH_MOCK_ASR_PORT_VARS[@]}")
    [ -n "$WITH_SMB_TEST_FLAG" ] && _pf_ports+=("${FRESH_SMB_PORT_VARS[@]}")
    [ -n "$WITH_MONITORING_FLAG" ] && _pf_ports+=("${FRESH_MONITORING_PORT_VARS[@]}")
    [ -n "$WITH_LLM_TEST_FLAG" ] && _pf_ports+=("${FRESH_LLM_TEST_PORT_VARS[@]}")
    # Keycloak (issue #630): this list previously covered every aux test overlay except
    # keycloak-test/authentik-test, so a bound 8180 failed deep inside `compose up --wait`
    # instead of failing fast here. Only Keycloak is added — Authentik is out of scope
    # (scripts/run-dev-tests.sh's overlay table deliberately excludes it; see that file).
    [ -n "$WITH_KEYCLOAK_TEST_FLAG" ] && _pf_ports+=("${FRESH_KEYCLOAK_PORT_VARS[@]}")
    preflight_ports_or_die "${_pf_ports[@]}"
  fi

  # Start services with appropriate compose files.
  # --wait blocks until every service is healthy (or the timeout elapses) so a
  # "created-but-never-started" container surfaces as a non-zero exit instead of
  # a silent failure. NOTE --wait does NOT cover a port-bind failure: that aborts
  # before any health check runs, which is what preflight_ports_or_die above is
  # for. --wait-timeout 700 covers the backend's 600s start_period.
  # shellcheck disable=SC2086
  if ! docker compose $COMPOSE_FILES up -d --wait --wait-timeout 700 $BUILD_CMD $RECREATE_CMD; then
    echo ""
    echo "❌ Startup failed — one or more services did not become healthy."
    echo "📊 Service status:"
    # shellcheck disable=SC2086
    docker compose $COMPOSE_FILES ps
    echo ""
    echo "📋 Recent logs:"
    # shellcheck disable=SC2086
    docker compose $COMPOSE_FILES logs --tail=50
    exit 1
  fi

  # Fix pipeline_scratch volume permissions (created by compose above) —
  # the volume is root-owned by default, which breaks the shared-memory
  # handoff between CPU preprocess and GPU/embedding workers.
  fix_pipeline_scratch_permissions

  # Baked stack: prove the containers run the code this invocation built
  # (issue #528) — compose has served a stale image here despite --force-recreate.
  if [ -n "$NO_BINDMOUNT_FLAG" ] && [ -n "$FRESH_FLAG" ]; then
    fresh_verify_baked_git_sha "$COMPOSE_FILES"
  fi

  # Display container status
  echo "📊 Container status:"
  # shellcheck disable=SC2086
  docker compose $COMPOSE_FILES ps

  # Print access information
  echo "✅ Services are up and healthy."
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
    echo "- Cloud ASR worker logs: docker compose logs -f celery-cloud-asr-worker"
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
      # BACKEND_PORT was exported with the offset already applied.
      local _seed_backend_port="${BACKEND_PORT:-5174}"
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
  GPU_DEVICE_OVERRIDE=""
  DIAR_NATIVE_GPU_OVERRIDE=""
  NAS_FLAG=""
  PULL_FLAG=""
  WITH_PKI_FLAG=""
  WITH_LDAP_TEST_FLAG=""
  WITH_MOCK_LLM_FLAG=""
  WITH_MOCK_ASR_FLAG=""
  WITH_SCRATCH_TMPFS_FLAG=""
  WITH_DIAR_NATIVE_FLAG=""
  NO_DIAR_NATIVE_FLAG=""
  WITH_LLM_TEST_FLAG=""
  WITH_KEYCLOAK_TEST_FLAG=""
  WITH_AUTHENTIK_TEST_FLAG=""
  WITH_WATCH_FLAG=""
  WITH_SMB_TEST_FLAG=""
  WITH_MONITORING_FLAG=""
  WITH_BACKUP_FLAG=""
  LITE_FLAG=""
  CPU_FLAG=""
  NO_NAS_FLAG=""
  FRESH_FLAG=""
  DRY_RUN_FLAG=""
  NO_BINDMOUNT_FLAG=""

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
      --gpu-device)
        shift
        # A missing value would abort on `set -u`; fail with something readable.
        if [ $# -eq 0 ] || [ "${1#-}" != "$1" ]; then
          echo "❌ --gpu-device requires a GPU index (e.g. --gpu-device 1)"
          exit 1
        fi
        GPU_DEVICE_OVERRIDE="$1"
        shift
        ;;
      --diar-native-gpu)
        shift
        if [ $# -eq 0 ] || [ "${1#-}" != "$1" ]; then
          echo "❌ --diar-native-gpu requires a GPU index (e.g. --diar-native-gpu 1)"
          exit 1
        fi
        DIAR_NATIVE_GPU_OVERRIDE="$1"
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
      --fresh|--port-offset|--seed-benchmark)
        # reset deletes data (down -v). A fresh deployment is meant to be ISOLATED,
        # so 'reset --fresh' would be a footgun: it would reset the REAL stack, not
        # an isolated one. Refuse it and point at the correct workflow.
        echo "❌ 'reset' does not support $1."
        echo "   To recreate an isolated stack from scratch:"
        echo "     ./opentr.sh fresh-destroy <name>      # remove its containers + volumes"
        echo "     ./opentr.sh start dev --fresh <name>  # start it clean"
        exit 1
        ;;
      --lite)
        LITE_FLAG="--lite"
        shift
        ;;
      --cpu)
        CPU_FLAG="--cpu"
        shift
        ;;
      --dry-run)
        DRY_RUN_FLAG="--dry-run"
        shift
        ;;
      --no-bindmount)
        # Only meaningful under --fresh, which reset refuses outright (see the
        # --fresh|--port-offset|--seed-benchmark branch above) -- parsed so it is
        # a recognized no-op rather than an "Unknown flag" warning, matching how
        # start_app() itself already ignores it outside fresh mode.
        NO_BINDMOUNT_FLAG="--no-bindmount"
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
      --with-mock-llm)
        WITH_MOCK_LLM_FLAG="--with-mock-llm"
        shift
        ;;
      --with-mock-asr)
        WITH_MOCK_ASR_FLAG="--with-mock-asr"
        shift
        ;;
      --with-scratch-tmpfs)
        WITH_SCRATCH_TMPFS_FLAG="--with-scratch-tmpfs"
        shift
        ;;
      --with-diar-native)
        WITH_DIAR_NATIVE_FLAG="--with-diar-native"
        shift
        ;;
      --no-diar-native)
        NO_DIAR_NATIVE_FLAG="--no-diar-native"
        shift
        ;;
      --with-llm-test)
        WITH_LLM_TEST_FLAG="--with-llm-test"
        shift
        ;;
      --with-keycloak-test)
        WITH_KEYCLOAK_TEST_FLAG="--with-keycloak-test"
        shift
        ;;
      --with-authentik-test)
        WITH_AUTHENTIK_TEST_FLAG="--with-authentik-test"
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
      --with-backup)
        WITH_BACKUP_FLAG="--with-backup"
        shift
        ;;
      *)
        echo "⚠️  Unknown flag: $1"
        shift
        ;;
    esac
  done


  if [ -n "$GPU_SCALE_FLAG" ] && [ -n "$GPU_SPLIT_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-scale,gpu-split"
  elif [ -n "$GPU_SCALE_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-scale"
  elif [ -n "$GPU_SPLIT_FLAG" ]; then
    export COMPOSE_PROFILES="gpu-split"
  fi

  echo "🔄 Running reset and initialize for OpenTranscribe in ${ENVIRONMENT} mode..."

  if [ -n "$GPU_DEVICE_OVERRIDE" ]; then
    apply_gpu_device_override "$GPU_DEVICE_OVERRIDE"
    warn_gpu_device_override_conflicts "$GPU_DEVICE_OVERRIDE"
  fi

  # See start_app()'s identical block for why this must come after --gpu-device.
  if [ -n "$DIAR_NATIVE_GPU_OVERRIDE" ]; then
    if ! [[ "$DIAR_NATIVE_GPU_OVERRIDE" =~ ^[0-9]+$ ]]; then
      echo "❌ --diar-native-gpu must be a non-negative integer GPU index (got '$DIAR_NATIVE_GPU_OVERRIDE')"
      exit 1
    fi
    export DIAR_NATIVE_GPU="$DIAR_NATIVE_GPU_OVERRIDE"
    echo "🎯 --diar-native-gpu $DIAR_NATIVE_GPU_OVERRIDE: pinning the diar-native sidecar to host GPU $DIAR_NATIVE_GPU_OVERRIDE (overrides .env)"
  fi

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
      # Force no-pull at `up` time so the locally-built image isn't clobbered by a
      # Hub pull (pull_policy:never is not reliably honored when a build: context
      # is also defined). Mirrors start prod --build.
      BUILD_CMD="--pull never"
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
  # The gpu-split overlay grants each worker its dedicated GPU reservation.
  if [ -n "$GPU_SPLIT_FLAG" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.gpu-split.yml"
    echo "🔀 Adding GPU split overlay (docker-compose.gpu-split.yml)"
  fi

  # Add NAS/NVMe storage overlay if requested via --nas flag
  # or auto-detect when storage path env vars are set.
  # --no-nas suppresses both the explicit and the auto-detect path so the live
  # bind-mounted data is never attached (and never wiped by the reset's down -v).
  if [ -n "$NO_NAS_FLAG" ]; then
    if [ -n "$MINIO_NAS_PATH" ] || [ -n "$POSTGRES_DATA_PATH" ] || [ -n "$OPENSEARCH_DATA_PATH" ]; then
      echo "🚫 --no-nas: NAS overlay suppressed (using Docker named volumes; live bind data untouched)"
    fi
    NAS_FLAG=""
  elif [ -z "$NAS_FLAG" ] && { [ -n "$MINIO_NAS_PATH" ] || [ -n "$POSTGRES_DATA_PATH" ] || [ -n "$OPENSEARCH_DATA_PATH" ]; }; then
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

  # Add PKI overlay if requested (dev routes to docker-compose.pki-dev.yml,
  # prod to docker-compose.pki.yml; cert generation + the test-env fragment
  # are handled inside add_pki_overlay -> scripts/pki/generate-test-env.sh)
  add_pki_overlay

  # Add mock LLM provider if requested
  if [ -n "$WITH_MOCK_LLM_FLAG" ]; then
    if [ -f "docker-compose.mock-llm.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.mock-llm.yml"
      echo "🤖 Adding mock LLM provider (docker-compose.mock-llm.yml)"
      echo "   From containers: http://mock-llm:5199/v1   From host: http://localhost:${MOCK_LLM_PORT:-5199}/v1"
      echo "   Models: mock-gpt (normal) mock-echo mock-empty mock-error mock-slow"
    else
      echo "⚠️  --with-mock-llm specified but docker-compose.mock-llm.yml not found"
    fi
  fi

  # Add mock cloud ASR provider if requested
  if [ -n "$WITH_MOCK_ASR_FLAG" ]; then
    if [ -f "docker-compose.mock-asr.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.mock-asr.yml"
      echo "🤖 Adding mock cloud ASR provider (docker-compose.mock-asr.yml)"
      echo "   From containers: http://mock-asr:5198   From host: http://localhost:${MOCK_ASR_PORT:-5198}"
      echo "   Scenarios: ok (default) error malformed upload-reject"
    else
      echo "⚠️  --with-mock-asr specified but docker-compose.mock-asr.yml not found"
    fi
  fi

  # Add the opt-in tmpfs override for the pipeline_scratch handoff volume if requested
  # (issue #661 E5). No isolation dispatch needed: the overlay declares neither `ports:`
  # nor `container_name:`, only a driver override for the already project-namespaced
  # `pipeline_scratch` volume — see the --fresh aux-isolation exemption comment in
  # backend/tests/unit/test_opentr_fresh_aux_isolation.py.
  if [ -n "$WITH_SCRATCH_TMPFS_FLAG" ]; then
    if [ -f "docker-compose.scratch-tmpfs.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.scratch-tmpfs.yml"
      echo "🧠 Adding RAM-backed scratch volume override (docker-compose.scratch-tmpfs.yml)"
      echo "   pipeline_scratch is now tmpfs, size=${SCRATCH_TMPFS_SIZE:-2g}"
    else
      echo "⚠️  --with-scratch-tmpfs specified but docker-compose.scratch-tmpfs.yml not found"
    fi
  fi

  # Native diarization sidecar. Shared with rebuild-backend so a rebuild can never
  # drop celery-worker's DIAR_NATIVE_URL / shared pipeline_scratch diar/ handoff namespace — see add_diar_native_overlay.
  add_diar_native_overlay start

  # Add real GPU-backed LLM test provider if requested
  if [ -n "$WITH_LLM_TEST_FLAG" ]; then
    if [ -f "docker-compose.llm-test.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.llm-test.yml"
      echo "🧠 Adding real LLM test provider (docker-compose.llm-test.yml)"
      echo "   vLLM — from containers: http://llm-test-vllm:8000/v1   from host: http://localhost:${LLM_TEST_PORT:-5195}/v1"
      echo "   Model: ${LLM_TEST_SERVED_NAME:-gemma-4-e4b}   GPU: ${LLM_TEST_GPU_DEVICE_ID:-2}"
      echo "   Ollama alternative (not auto-started): docker compose ... --profile ollama up -d llm-test-ollama"
    else
      echo "⚠️  --with-llm-test specified but docker-compose.llm-test.yml not found"
    fi
  fi

  # Add LDAP test container if requested
  if [ -n "$WITH_LDAP_TEST_FLAG" ]; then
    if [ -f "docker-compose.ldap-test.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.ldap-test.yml"
      echo "🔐 Adding LDAP test container (docker-compose.ldap-test.yml)"
      echo "   LDAP server: localhost:${LDAP_TEST_PORT:-3890}  (from containers: ldap://lldap-test:3890)"
      echo "   Web UI: http://localhost:${LDAP_TEST_UI_PORT:-17170}"
    else
      echo "⚠️  --with-ldap-test specified but docker-compose.ldap-test.yml not found"
    fi
  fi

  # Add Keycloak test container if requested
  if [ -n "$WITH_KEYCLOAK_TEST_FLAG" ]; then
    if [ -f "docker-compose.keycloak.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.keycloak.yml"
      echo "🔐 Adding Keycloak test container (docker-compose.keycloak.yml)"
      echo "   Keycloak URL: http://localhost:${KEYCLOAK_PORT:-8180}"
      echo "   Admin credentials: admin / admin"
    else
      echo "⚠️  --with-keycloak-test specified but docker-compose.keycloak.yml not found"
    fi
  fi

  # Add Authentik test container if requested
  if [ -n "$WITH_AUTHENTIK_TEST_FLAG" ]; then
    if [ -f "docker-compose.authentik.yml" ]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.authentik.yml"
      echo "🔐 Adding Authentik test container (docker-compose.authentik.yml)"
      echo "   Authentik URL: http://localhost:${AUTHENTIK_PORT:-9022}"
      echo "   Bootstrap credentials: ${AUTHENTIK_BOOTSTRAP_EMAIL:-admin@example.com} / ${AUTHENTIK_BOOTSTRAP_PASSWORD:-admin_password}"
    else
      echo "⚠️  --with-authentik-test specified but docker-compose.authentik.yml not found"
    fi
  fi

  # Add Watch Sources overlay if requested (mounts the host watch folder)
  if [ -n "$WITH_WATCH_FLAG" ]; then
    if [ -f "docker-compose.watch.yml" ]; then
      WATCH_HOST_PATH="${WATCH_HOST_PATH:-./watch}"
      mkdir -p "$WATCH_HOST_PATH"
      # Match the non-root container user so imports can read/write. appuser is
      # uid 1000 / gid 999 (see CONTAINER_UID_GID in scripts/common.sh) — the owner
      # bit is what the import path needs, but keep the GID honest.
      chown -R "${CONTAINER_UID_GID:-1000:999}" "$WATCH_HOST_PATH" 2>/dev/null || true
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
      echo "   SMB share: smb://localhost:${SMB_TEST_PORT:-4450}/media  (testuser / testpass)"
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

  # Add Backup overlay if requested (mounts BACKUP_HOST_PATH for scheduled backups
  # and BACKUP_MIRROR_HOST_PATH for the incremental media mirror, issue #242)
  if [ -n "$WITH_BACKUP_FLAG" ]; then
    if [ -f "docker-compose.backup.yml" ]; then
      BACKUP_HOST_PATH="${BACKUP_HOST_PATH:-./backups}"
      BACKUP_MIRROR_HOST_PATH="${BACKUP_MIRROR_HOST_PATH:-./media-mirror}"
      mkdir -p "$BACKUP_HOST_PATH" 2>/dev/null || true
      mkdir -p "$BACKUP_MIRROR_HOST_PATH" 2>/dev/null || true
      export BACKUP_HOST_PATH BACKUP_MIRROR_HOST_PATH
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.backup.yml"
      echo "💾 Adding Backup overlay (docker-compose.backup.yml)"
      echo "   Backup destination: $BACKUP_HOST_PATH → /backups (configure in admin UI → Backups)"
      echo "   Media mirror:       $BACKUP_MIRROR_HOST_PATH → /media-mirror (admin UI → Backups → Media Mirror)"
    else
      echo "⚠️  --with-backup specified but docker-compose.backup.yml not found"
    fi
  fi

  # Dry-run: print exactly what WOULD run and exit before touching Docker — most
  # important here of anywhere in this script, since the next step is `down -v`,
  # which destroys the current stack's data. A --dry-run that silently proceeded
  # to a real reset would be the opposite of what it promises.
  if [ -n "$DRY_RUN_FLAG" ]; then
    echo ""
    echo "🔎 DRY RUN — no containers stopped, no volumes removed."
    echo "   COMPOSE_PROJECT_NAME: ${COMPOSE_PROJECT_NAME:-opentranscribe (default)}"
    echo "   Compose files:"
    # shellcheck disable=SC2086
    for _f in $COMPOSE_FILES; do
      [ "$_f" = "-f" ] && continue
      echo "     - $_f"
    done
    echo "   Would run: docker compose \$COMPOSE_FILES down -v"
    echo "   ...then rebuild and start all services (--build)."
    return 0
  fi

  echo "🛑 Stopping all containers and removing volumes..."
  # Drain CUDA-holding workers before the destructive `down -v` below (issue #782).
  ot_drain_gpu_workers "$COMPOSE_FILES"
  # shellcheck disable=SC2086
  docker compose $COMPOSE_FILES down -v

  # Create necessary directories
  create_required_dirs

  # Fix model cache permissions for non-root container
  fix_model_cache_permissions

  # Generate a real MinIO KMS secret key if .env still has .env.example's
  # shipped placeholder (issue #614).
  ensure_minio_kms_secret ".env"

  # Fetch the NLTK corpora BEFORE de-hardlinking them (issue #491).
  ensure_nltk_corpora

  # NLTK >= 3.10 refuses multiply-linked files (issue #491)
  ensure_nltk_data_unlinked

  # Ensure OpenSearch neural models are downloaded for offline capability
  ensure_opensearch_models

  # Start all services - docker compose handles dependency ordering via depends_on.
  # --wait blocks until healthy (or timeout) so a failed startup is a non-zero
  # exit, not a silent "created but not running". --wait-timeout 700 covers the
  # backend's 600s start_period.
  echo "🚀 Starting all services..."
  # shellcheck disable=SC2086
  if ! docker compose $COMPOSE_FILES up -d --wait --wait-timeout 700 $BUILD_CMD; then
    echo ""
    echo "❌ Startup failed — one or more services did not become healthy."
    echo "📊 Service status:"
    # shellcheck disable=SC2086
    docker compose $COMPOSE_FILES ps
    echo ""
    echo "📋 Recent logs:"
    # shellcheck disable=SC2086
    docker compose $COMPOSE_FILES logs --tail=50
    exit 1
  fi

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

# backup_database / restore_database moved to scripts/common.sh (issue #613),
# parameterized by a leading compose-files chain and front-end name so opentr.sh and
# opentranscribe.sh share exactly one implementation of the DROP DATABASE restore path.
# common.sh is sourced at the top of this file, so both functions are already in scope.

# Function to restart backend services (backend, all celery workers, flower) without database reset
# Resolve which deployment the restart-* commands act on.
#
# The restart-* dispatch arms used to take NO arguments and call bare
# `docker compose restart`, which resolves the DEFAULT compose project. So
# `restart-backend --fresh <name>` did not merely ignore `--fresh` — it silently
# restarted the main dev stack instead, printed "restarted successfully", and then
# printed the default project's (often empty) container table. Two stacks, one of
# them live, and no message distinguishing them.
#
# Sets RESTART_CHAIN / RESTART_PROJECT / RESTART_LABEL for the caller. Same
# COMPOSE_PROJECT_NAME + fresh_compose_chain idiom fresh_stop/fresh_status use.
restart_resolve_target() {
  RESTART_CHAIN=""
  RESTART_PROJECT=""
  RESTART_LABEL="the default deployment"
  while [ $# -gt 0 ]; do
    case "$1" in
      --fresh)
        shift
        if [ $# -eq 0 ] || [ -z "${1:-}" ]; then
          echo "❌ --fresh needs a deployment name (see: ./opentr.sh fresh-list)" >&2
          return 2
        fi
        local _name
        _name="$(fresh_sanitize_name "$1")"
        if [ ! -f "${FRESH_OVERLAY_DIR}/${_name}.yml" ]; then
          # Refuse rather than fall through to the default project — falling
          # through is what restarted the live stack on a typo'd name.
          echo "❌ No fresh deployment '${_name}' (no ${FRESH_OVERLAY_DIR}/${_name}.yml)." >&2
          echo "   Known deployments: ./opentr.sh fresh-list" >&2
          return 2
        fi
        RESTART_PROJECT="$(fresh_project_name "$_name")"
        RESTART_CHAIN="$(fresh_compose_chain "$_name")"
        RESTART_LABEL="fresh deployment '${_name}' (project ${RESTART_PROJECT})"
        shift
        ;;
      *)
        echo "❌ Unknown option for restart: $1" >&2
        return 2
        ;;
    esac
  done
  return 0
}

# Run `docker compose` against the resolved target. Stderr is NOT discarded and the
# exit status IS returned: the old code sent both to /dev/null, so "✅ restarted
# successfully" printed whether or not anything had been restarted.
restart_compose() {
  if [ -n "$RESTART_PROJECT" ]; then
    # shellcheck disable=SC2086
    COMPOSE_PROJECT_NAME="$RESTART_PROJECT" docker compose $RESTART_CHAIN "$@"
  else
    # No --fresh: bare `docker compose`, which in a repo clone auto-loads
    # docker-compose.override.yml. Byte-identical to the previous behaviour, so
    # the default path is unchanged by this fix.
    docker compose "$@"
  fi
}

restart_backend() {
  restart_resolve_target "$@" || return $?
  echo "🔄 Restarting backend services on ${RESTART_LABEL} (backend, all celery workers, celery-beat, flower)..."

  # -t "$OT_STOP_GRACE_GPU" (issue #782): compose v2.29.7's `restart` passes only
  # options.Timeout, so a container created before docker-compose.yml carried
  # stop_grace_period (StopTimeout still null) needs it spelled out here too.
  local rc=0
  restart_compose restart -t "$OT_STOP_GRACE_GPU" backend \
    celery-worker \
    celery-download-worker \
    celery-cpu-worker \
    celery-redaction \
    celery-cloud-asr-worker \
    celery-nlp-worker \
    celery-embedding-worker \
    celery-beat \
    flower || rc=$?

  # celery-worker-gpu-scaled is optional (scale: 0 unless --gpu-scale), so its
  # absence is genuinely not an error — unlike everything above.
  restart_compose restart -t "$OT_STOP_GRACE_GPU" celery-worker-gpu-scaled 2>/dev/null || true

  if [ "$rc" -ne 0 ]; then
    echo "❌ Backend restart FAILED on ${RESTART_LABEL} (docker compose exit ${rc})." >&2
    echo "   Nothing above was necessarily restarted — do not read this as a success." >&2
    return "$rc"
  fi
  echo "✅ Backend services restarted successfully on ${RESTART_LABEL}."

  # Display container status
  echo "📊 Container status:"
  restart_compose ps
}

# Function to restart frontend only
restart_frontend() {
  restart_resolve_target "$@" || return $?
  echo "🔄 Restarting frontend service on ${RESTART_LABEL}..."

  # No -t/drain here (issue #782): the frontend holds no CUDA context, so the default
  # grace period is correct as-is. See backend/tests/unit/test_teardown_call_sites_drain.py's
  # allowlist entry for this exact line.
  local rc=0
  restart_compose restart frontend || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "❌ Frontend restart FAILED on ${RESTART_LABEL} (docker compose exit ${rc})." >&2
    return "$rc"
  fi
  echo "✅ Frontend service restarted successfully on ${RESTART_LABEL}."

  # Display container status
  echo "📊 Container status:"
  restart_compose ps
}

# Function to restart all services without resetting the database
restart_all() {
  restart_resolve_target "$@" || return $?
  echo "🔄 Restarting all services on ${RESTART_LABEL} without database reset..."

  # Restart all services in place - docker compose handles dependency ordering.
  # -t "$OT_STOP_GRACE_GPU" (issue #782): see restart_backend()'s comment -- `restart`
  # passes only options.Timeout, so pre-recreate containers need it explicit here too.
  local rc=0
  restart_compose restart -t "$OT_STOP_GRACE_GPU" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "❌ Restart FAILED on ${RESTART_LABEL} (docker compose exit ${rc})." >&2
    return "$rc"
  fi
  echo "✅ All services restarted successfully on ${RESTART_LABEL}."

  # Display container status
  echo "📊 Container status:"
  restart_compose ps
}

# Helper: stop all containers from both dev and prod compose chains, plus stragglers
# Drain (b): container-driven counterpart to ot_drain_gpu_workers() (scripts/common.sh),
# for stop_all_containers() specifically -- it runs BOTH a dev and a prod compose chain
# below, so no single chain is guaranteed to be the one that actually started a given
# service (e.g. after an overlay changed between the run that created a container and
# this one). Reads the SAME overridable project labels the straggler loop in
# stop_all_containers() uses, for the identical reason (issue #693): a bare name-prefix
# match previously stopped-and-removed an unrelated container ("opentranscribe-homepage",
# a dashboard app from a different compose project sharing the name prefix by
# coincidence) on this host. Matches by NAME SUBSTRING, not prefix, so it also reaches
# gpu-scale's celery-worker-gpu-scaled, gpu-split's celery-worker-gpu-{transcribe,diarize},
# a --fresh project's otfresh-<name>-celery-worker, and diar-native's default
# project-prefixed name (e.g. transcribe-app-diar-native-1) -- none of which carry the
# bare "opentranscribe-celery-worker" name a literal match would assume. Backgrounded +
# `wait` so N workers drain in PARALLEL, not N times OT_STOP_GRACE_GPU serially.
#
# ⚠️ A LABEL-based selector (e.g. com.opentranscribe.gpu=true) is tempting and WRONG:
# labels are also baked at container CREATE time, so it would miss exactly the
# pre-upgrade containers this helper exists to reach.
ot_drain_gpu_workers_by_container() {
  local pids=()
  local gpu_container
  for gpu_container in $( { docker ps --filter "label=com.docker.compose.project=${OPENTR_STOP_PROJECT_LABEL:-opentranscribe}" --format '{{.Names}}' 2>/dev/null
                            docker ps --filter "label=com.docker.compose.project=${OPENTR_STOP_PROJECT_LABEL_ALT:-transcribe-app}" --format '{{.Names}}' 2>/dev/null
                          } | sort -u ); do
    case "$gpu_container" in
      *celery-worker*|*celery-cpu-worker*|*celery-redaction*|*diar-native*)
        docker stop -t "$OT_STOP_GRACE_GPU" "$gpu_container" 2>/dev/null &
        pids+=("$!")
        ;;
    esac
  done
  local pid
  for pid in "${pids[@]:-}"; do
    [ -n "$pid" ] && wait "$pid" 2>/dev/null
  done
}

stop_all_containers() {
  # Drain CUDA-holding workers before EITHER `down`/stop chain below reaches them
  # (issue #782) -- see ot_drain_gpu_workers_by_container()'s docstring for why this is
  # container-driven rather than chain-driven here specifically.
  ot_drain_gpu_workers_by_container

  # Dev compose chain. -f docker-compose.gpu-split.yml -f docker-compose.diar-native.yml
  # and COMPOSE_PROFILES="*" close issue #782's N5 finding: without them, this chain never
  # named the services gpu-split/diar-native define, so celery-worker-gpu-{transcribe,
  # diarize}, -gpu-scaled and diar-native were reachable only by the straggler loop below
  # (same fix shape fresh_destroy() already uses for the identical class of profile leak).
  # shellcheck disable=SC2086
  COMPOSE_PROFILES="*" docker compose -f docker-compose.yml -f docker-compose.override.yml \
    -f docker-compose.gpu.yml -f docker-compose.blackwell.yml \
    -f docker-compose.gpu-scale.yml -f docker-compose.gpu-split.yml \
    -f docker-compose.diar-native.yml \
    -f docker-compose.nas.yml "$@" 2>/dev/null || true

  # Prod compose chain. Same N5 fix as the dev chain above.
  # shellcheck disable=SC2086
  COMPOSE_PROFILES="*" docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    -f docker-compose.local.yml -f docker-compose.gpu.yml \
    -f docker-compose.blackwell.yml -f docker-compose.gpu-scale.yml \
    -f docker-compose.gpu-split.yml -f docker-compose.diar-native.yml \
    -f docker-compose.nas.yml \
    -f docker-compose.nginx.yml -f docker-compose.pki.yml "$@" 2>/dev/null || true

  # Catch stragglers: containers left over from THIS project's compose chains
  # that a `docker compose down` above didn't reach (e.g. an overlay that
  # changed between runs). Filtered by compose PROJECT label, not a bare name
  # prefix -- a prefix match previously stopped-and-removed an unrelated
  # container on this host ("opentranscribe-homepage", a dashboard app from a
  # different compose project sharing the name prefix by coincidence). Two
  # project names are legitimate here: "opentranscribe" (every service with an
  # explicit container_name) and "transcribe-app" (docker-compose.diar-native.yml's
  # diar-native service has no explicit container_name, so compose falls back
  # to the checkout directory's basename as the project name).
  #
  # The two project names are parameterised ONLY so the test that drives this
  # real loop body (backend/tests/unit/test_opentr_stop_container_scoping.py)
  # can point it at a throwaway namespace instead of the live stack -- running
  # the loop unmodified against a developer's daemon destroyed 16 running
  # containers (issue #693). Nothing in this script, and nothing shipped, ever
  # sets them: unset, both expand to the literals they replaced, so `opentr.sh
  # stop` behaves exactly as before.
  for container in $( { docker ps -a --filter "label=com.docker.compose.project=${OPENTR_STOP_PROJECT_LABEL:-opentranscribe}" --format '{{.Names}}' 2>/dev/null
                        docker ps -a --filter "label=com.docker.compose.project=${OPENTR_STOP_PROJECT_LABEL_ALT:-transcribe-app}" --format '{{.Names}}' 2>/dev/null
                      } | sort -u ); do
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

# Container-name prefix for the bench stack. docker-compose.bench.yml overrides
# container_name to otbench-* on EVERY service precisely so a bench stack can
# coexist with the dev stack, so any `docker ps`/`docker inspect`/`docker exec`
# in the bench flow MUST address otbench-*, never opentranscribe-* (issue #399).
# Matching on the dev names made the engine gate validate the one stack the
# benchmark must not touch: it aborted when only the bench stack was up, and
# passed when the dev stack was up even if the bench worker was missing.
BENCH_CONTAINER_PREFIX="otbench"

# Poll the bench backend container until it reports healthy (deterministic
# replacement for blind `sleep`s in the bench flow).
# Usage: wait_for_bench_backend_health [timeout_seconds]
wait_for_bench_backend_health() {
  local timeout="${1:-180}"
  local interval=3
  local elapsed=0
  local status
  local container="${BENCH_CONTAINER_PREFIX}-backend"
  while [ "$elapsed" -lt "$timeout" ]; do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo "")"
    if [ "$status" = "healthy" ]; then
      echo "✅ Bench backend is healthy! (${elapsed}s)"
      return 0
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
    echo "⏳ Waiting for bench backend... (${elapsed}/${timeout}s, status: ${status:-starting})"
  done
  echo "⚠️ Bench backend health check timed out after ${timeout}s, continuing anyway..."
  docker logs --tail 20 "$container" 2>/dev/null || true
  return 1
}

# Function to check health of all services
# Machine-checkable health gate.
#
# check_health() below is a human status dump: every probe ends in
# `|| echo "⚠️ ..."`, so it always exits 0 and can never gate anything. That is
# the right behaviour for someone eyeballing a stack, and the wrong behaviour for
# a release step or a CI job, which needs a non-zero exit.
#
# Rather than change check_health's semantics (and break anyone parsing it), this
# re-probes the same services and accumulates failures.
#
# Usage: ./opentr.sh healthcheck-all [--json]
healthcheck_all() {
  local as_json=false
  [ "${1:-}" = "--json" ] && as_json=true

  local failures=() results=()

  _probe() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
      results+=("{\"service\":\"$name\",\"status\":\"ok\"}")
      $as_json || echo "  ✅ $name"
    else
      failures+=("$name")
      results+=("{\"service\":\"$name\",\"status\":\"fail\"}")
      $as_json || echo "  ❌ $name"
    fi
  }

  $as_json || echo "🩺 Health gate:"

  _probe backend    docker compose exec -T backend curl -sf http://localhost:8080/health
  _probe redis      docker compose exec -T redis redis-cli ping
  _probe postgres   docker compose exec -T postgres pg_isready -U postgres
  _probe opensearch docker compose exec -T opensearch curl -sf http://localhost:9200
  _probe minio      docker compose exec -T minio curl -sf http://localhost:9000/minio/health/live

  # Readiness carries the schema check, which is what makes this useful after an
  # upgrade: a backend can answer /health while sitting on a stale schema.
  _probe backend-ready docker compose exec -T backend curl -sf http://localhost:8080/health/ready

  if $as_json; then
    local joined
    joined=$(IFS=,; echo "${results[*]}")
    printf '{"stage":"healthcheck-all","status":"%s","checks":[%s]}\n' \
      "$([ ${#failures[@]} -eq 0 ] && echo pass || echo fail)" "$joined"
  fi

  if [ ${#failures[@]} -eq 0 ]; then
    $as_json || echo "✅ All services healthy."
    return 0
  fi
  $as_json || echo "❌ Unhealthy: ${failures[*]}"
  return 1
}

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
    # "" = bare `docker compose`, which in a repo clone auto-loads
    # docker-compose.override.yml — byte-identical behaviour to before the move (issue #613).
    backup_database "" "./opentr.sh" "${2:-}"
    ;;

  restore)
    shift  # Remove 'restore' command
    restore_database "" "./opentr.sh" "$@"  # Pass all remaining arguments (flags + file)
    ;;

  restart-backend)
    shift
    restart_backend "$@"
    ;;

  restart-frontend)
    shift
    restart_frontend "$@"
    ;;

  restart-all)
    shift
    restart_all "$@"
    ;;

  rebuild-backend)
    echo "🔨 Rebuilding backend services..."
    detect_and_configure_hardware

    # Parse optional flags so NAS-configured deployments keep their mounts, and so
    # the diar-native auto-detect below can be overridden either way.
    NAS_FLAG=""
    WITH_DIAR_NATIVE_FLAG=""
    NO_DIAR_NATIVE_FLAG=""
    shift || true  # drop "rebuild-backend"
    while [ $# -gt 0 ]; do
      case "$1" in
        --nas)
          NAS_FLAG="--nas"
          shift
          ;;
        --with-diar-native)
          WITH_DIAR_NATIVE_FLAG="--with-diar-native"
          shift
          ;;
        --no-diar-native)
          NO_DIAR_NATIVE_FLAG="--no-diar-native"
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

    # Keep the native diarization sidecar's handoff wiring on celery-worker.
    # Without this, a rebuilt worker has no DIAR_NATIVE_URL and loses the shared
    # pipeline_scratch/diar/ handoff namespace (issue #661 E2 -- was a dedicated
    # /tmp/diar-native mount before the consolidation), the sidecar cannot see the WAV
    # it is handed, /diarize answers 422, and diarization silently degrades to the
    # in-process PyAnnote fork -- same "correct-looking container, wrong storage"
    # failure as the NAS note above, but with no user-visible symptom at all. `rebuild`
    # mode keys off the sidecar container this deployment already has, so it never
    # starts one nobody asked for; see add_diar_native_overlay for why that predicate
    # and not the config one.
    add_diar_native_overlay rebuild

    # --no-deps keeps postgres/minio/opensearch/redis containers exactly as
    # they were running. Only rebuild + recreate the services that actually
    # consume backend code -- every service in docker-compose.override.yml
    # built from `image: opentranscribe-backend:latest`. celery-redaction was
    # missing from this list for a while: it shares the image but has its own
    # entry, so it silently kept running stale code after every rebuild until
    # something recreated it another way.
    #
    # `diar-native` is deliberately NOT in this list even when its overlay is
    # loaded: it runs a Rust binary out of the same image, so a Python-side change
    # cannot affect it, and recreating it costs a fresh ~2.2 GB ORT warm-up on the
    # GPU. Restart it explicitly when the image's diar-server binary itself changed.
    # shellcheck disable=SC2086
    docker compose $COMPOSE_FILES up -d --build --no-deps \
      backend celery-worker celery-download-worker celery-cpu-worker \
      celery-redaction celery-cloud-asr-worker celery-nlp-worker \
      celery-embedding-worker celery-beat flower

    # celery-worker-gpu-scaled (profile gpu-scale) and celery-worker-gpu-transcribe/
    # -diarize (profile gpu-split) share the same image too, but are scale:0 /
    # profile-gated -- only rebuild them if they're actually running, so a plain
    # rebuild-backend never starts a GPU worker profile nobody activated.
    for gpu_entry in "celery-worker-gpu-scaled:gpu-scale" "celery-worker-gpu-transcribe:gpu-split" "celery-worker-gpu-diarize:gpu-split"; do
      gpu_service="${gpu_entry%%:*}"
      gpu_profile="${gpu_entry##*:}"
      container="${COMPOSE_PROJECT_NAME:-opentranscribe}-${gpu_service}"
      if docker ps --filter "name=^${container}$" --filter "status=running" -q | grep -q .; then
        echo "🎯 ${gpu_service} is active — rebuilding it too"
        # shellcheck disable=SC2086
        COMPOSE_PROFILES="$gpu_profile" docker compose $COMPOSE_FILES up -d --build --no-deps "$gpu_service"
      fi
    done

    # Fix pipeline_scratch volume permissions in case it was recreated.
    fix_pipeline_scratch_permissions

    echo "✅ Backend services rebuilt successfully."
    ;;

  rebuild-frontend)
    echo "🔨 Rebuilding frontend + docs services..."
    # Frontend/docs don't use NAS volumes, but keep --no-deps for symmetry
    # with rebuild-backend so data containers are untouched. docs rides along
    # so the two never drift apart the way they did before this was added —
    # a rebuilt frontend with a stale docs image looks fine until someone
    # reads a stale changelog/auth-setup page for a feature that already shipped.
    docker compose up -d --build --no-deps frontend docs
    echo "✅ Frontend + docs services rebuilt successfully."
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

  healthcheck-all)
    # Same probes as `health`, but exits non-zero on any failure so scripts and
    # CI can gate on it. `health` stays a human-readable status dump.
    healthcheck_all "${2:-}"
    ;;

  build)
    echo "🔨 Rebuilding containers..."
    detect_and_configure_hardware

    # Build compose file list
    COMPOSE_FILES="-f docker-compose.yml -f docker-compose.override.yml"

    # Add GPU overlay if NVIDIA GPU is detected
    add_gpu_overlay

    # shellcheck disable=SC2086
    if ! docker compose $COMPOSE_FILES build; then
      # A build failure used to print "✅ Build complete" and exit 0, because the
      # success line was unconditional. The next `start` then served the PREVIOUS
      # image, so a change simply did not appear — the same class of silent-stale
      # deployment as issue #75, and equally hard to attribute to the build.
      echo "❌ Build FAILED. The previous images are unchanged; do not start and"
      echo "   assume your changes are deployed. Scroll up for the build error."
      exit 1
    fi
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
        # Drain otbench-celery-worker before `down` reaches it (issue #782) -- a real GPU
        # worker, not a mock.
        ot_drain_gpu_workers "$BENCH_COMPOSE"
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
        echo "⏳ Waiting for bench backend to become healthy..."
        wait_for_bench_backend_health
        docker ps --format 'table {{.Names}}\t{{.Status}}' | grep "$BENCH_CONTAINER_PREFIX"
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
        ot_drain_gpu_workers "$BENCH_COMPOSE"
        # shellcheck disable=SC2086
        docker compose $BENCH_COMPOSE stop
        echo "✅ Bench stack stopped. Volumes preserved. Use 'bench clean' to wipe."
        ;;

      clean)
        echo "🗑  Stopping bench stack and wiping all bench volumes..."
        ot_drain_gpu_workers "$BENCH_COMPOSE"
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
        docker ps --format 'table {{.Names}}\t{{.Status}}' | grep "$BENCH_CONTAINER_PREFIX" || echo "(none running)"
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
        WORKER="${BENCH_CONTAINER_PREFIX}-celery-worker"

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
        ot_drain_gpu_workers "$BENCH_COMPOSE"
        # shellcheck disable=SC2086
        docker compose $BENCH_COMPOSE down --remove-orphans 2>/dev/null || true
        docker volume rm \
          "${BENCH_VOLUME_PREFIX}_postgres_bench_data" \
          "${BENCH_VOLUME_PREFIX}_minio_bench_data" \
          "${BENCH_VOLUME_PREFIX}_redis_bench_data" \
          "${BENCH_VOLUME_PREFIX}_opensearch_bench_data" \
          "${BENCH_VOLUME_PREFIX}_flower_bench_data" 2>/dev/null || true

        # Build and start bench stack on current branch.
        # Build the full backend service set (every worker sharing the backend
        # image, so the bench stack never silently drifts from what
        # rebuild-backend rebuilds) plus infra — skip docs/frontend (they don't
        # affect AI processing and docs has an MDX build step that can fail on
        # unrelated content changes).
        echo "🚀 Building bench stack from current branch (backend + infra only)..."
        # shellcheck disable=SC2086
        docker compose $BENCH_COMPOSE build \
          backend celery-worker celery-download-worker celery-cpu-worker \
          celery-redaction celery-cloud-asr-worker celery-nlp-worker celery-embedding-worker \
          celery-beat flower
        # shellcheck disable=SC2086
        docker compose $BENCH_COMPOSE up -d --no-build \
          postgres redis minio opensearch \
          backend celery-worker celery-download-worker celery-cpu-worker \
          celery-redaction celery-cloud-asr-worker celery-nlp-worker celery-embedding-worker \
          celery-beat flower

        echo ""
        echo "⏳ Waiting for bench stack to be ready (DB migrations, model pre-load)..."
        wait_for_bench_backend_health 240
        docker ps --format 'table {{.Names}}\t{{.Status}}' | grep "$BENCH_CONTAINER_PREFIX"

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
        ot_drain_gpu_workers "$BENCH_COMPOSE"
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

      rag)
        # Retrieval-quality benchmark (#403 Stage 1) — a PEER of the GPU arms
        # above, not a mode of them. It measures nDCG/recall/MRR over an
        # already-injected eval corpus and needs no GPU, no ASR and no LLM.
        #
        # It runs against a --fresh deployment rather than the otbench stack,
        # because the corpus is injected there by scripts/inject-eval-corpus.sh
        # and the measurement must be reproducible against a NAMED, isolated
        # dataset. Same lesson as #399: address the target deployment's own
        # container names and published ports, never the dev stack's — a bench
        # arm that validates the wrong stack reports the wrong number.
        RAG_FRESH_NAME=""
        RAG_PORT_OFFSET=""
        RAG_ARGS=()
        shift 2  # drop "bench rag"
        while [[ $# -gt 0 ]]; do
          case "$1" in
            --fresh)       RAG_FRESH_NAME="$2"; shift 2 ;;
            --port-offset) RAG_PORT_OFFSET="$2"; shift 2 ;;
            *)             RAG_ARGS+=("$1"); shift ;;
          esac
        done

        if [[ -n "$RAG_FRESH_NAME" && -z "$RAG_PORT_OFFSET" ]]; then
          RAG_OFFSET_FILE=".fresh/${RAG_FRESH_NAME}.offset"
          if [[ -f "$RAG_OFFSET_FILE" ]]; then
            RAG_PORT_OFFSET="$(tr -d '[:space:]' < "$RAG_OFFSET_FILE")"
          else
            echo "❌ No offset recorded for fresh deployment '${RAG_FRESH_NAME}' (${RAG_OFFSET_FILE})."
            echo "   Pass --port-offset N, or check './opentr.sh fresh-list'."
            exit 1
          fi
        fi
        RAG_PORT_OFFSET="${RAG_PORT_OFFSET:-0}"
        if ! [[ "$RAG_PORT_OFFSET" =~ ^[0-9]+$ ]]; then
          echo "❌ --port-offset must be a non-negative integer, got '${RAG_PORT_OFFSET}'."
          exit 1
        fi

        # Verify the deployment we are about to measure is actually up, by ITS
        # container names (otfresh-<name>-*), before exporting anything.
        if [[ -n "$RAG_FRESH_NAME" ]]; then
          RAG_OS_CONTAINER="otfresh-${RAG_FRESH_NAME}-opensearch"
          if ! docker ps --format '{{.Names}}' | grep -q "^${RAG_OS_CONTAINER}$"; then
            echo "❌ '${RAG_OS_CONTAINER}' is not running — the corpus is indexed there."
            echo "   Start it:  ./opentr.sh start dev --fresh ${RAG_FRESH_NAME} --port-offset ${RAG_PORT_OFFSET}"
            exit 1
          fi
        fi

        # OT_EVAL_PYTHON exists because a git worktree does not carry the venv:
        # it lives in the main checkout, and the harness runs on the host.
        RAG_PYTHON="${OT_EVAL_PYTHON:-backend/venv/bin/python}"
        if [[ ! -x "$RAG_PYTHON" ]]; then
          echo "❌ No usable interpreter at '${RAG_PYTHON}'."
          echo "   Create backend/venv (see 'Backend / venv' in CLAUDE.md), or set"
          echo "   OT_EVAL_PYTHON=/path/to/venv/bin/python (needed in a worktree)."
          exit 1
        fi
        if ! "$RAG_PYTHON" -c "import pytrec_eval" 2>/dev/null; then
          echo "❌ The metric engine is missing. It is an EVAL-ONLY dependency, kept out of"
          echo "   requirements.txt and the published images for licence reasons:"
          echo "   ${RAG_PYTHON} -m pip install -r backend/requirements-eval.txt"
          exit 1
        fi

        # Assigned, not defaulted: opentr.sh has already loaded .env, whose hosts
        # are docker-network names (`postgres`, `opensearch`). The harness runs on
        # the HOST, against published ports, so those names do not resolve.
        RAG_HOST="${OT_EVAL_HOST:-localhost}"
        export POSTGRES_HOST="$RAG_HOST"
        export OPENSEARCH_HOST="$RAG_HOST"
        export MINIO_HOST="$RAG_HOST"
        export REDIS_HOST="$RAG_HOST"
        export POSTGRES_PORT=$((5176 + RAG_PORT_OFFSET))
        export REDIS_PORT=$((5177 + RAG_PORT_OFFSET))
        export MINIO_PORT=$((5178 + RAG_PORT_OFFSET))
        export OPENSEARCH_PORT=$((5180 + RAG_PORT_OFFSET))

        echo "🔎 RAG retrieval benchmark (#403 Stage 1)"
        [[ -n "$RAG_FRESH_NAME" ]] && echo "   Deployment: otfresh-${RAG_FRESH_NAME} (offset +${RAG_PORT_OFFSET})"
        echo "   OpenSearch: ${OPENSEARCH_HOST}:${OPENSEARCH_PORT}   Postgres: ${POSTGRES_HOST}:${POSTGRES_PORT}"
        echo "   No GPU, no ASR, no LLM — retrieval only (D6)."
        echo ""
        # Exit with the harness's own status. opentr.sh ends in `exit 0`, so a
        # bench arm that just runs a command reports success however it failed —
        # and this one is meant to be usable as a gate.
        "$RAG_PYTHON" scripts/benchmark_rag.py "${RAG_ARGS[@]}"
        exit $?
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
        echo "  bench rag --fresh <name> [args]          - Retrieval quality (nDCG/recall/MRR) over an injected eval corpus"
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
