#!/bin/bash

# Common functions for OpenTranscribe shell scripts
# These functions are used by opentr.sh to provide common functionality
#
# Usage: source ./scripts/common.sh
#
# NOTE: opentr.sh runs under `set -uo pipefail`, and this file is sourced INTO
# that shell — so every expansion here of a variable this file does not itself
# assign must carry a `:-` default, or the whole script aborts with "unbound
# variable". Relying on the caller's defaults block is not enough: it defaults
# the vars it happens to know about, and `GPU_DEVICE_ID` was missing from it for
# as long as the check below existed, which killed `./opentr.sh start dev` on
# every checkout with no .env (a fresh clone, and every git worktree — .env is
# gitignored, so it never comes along).

# Ownership the backend/worker images actually run as. `backend/Dockerfile.prod` pins the
# UID explicitly (`useradd -u 1000`) but creates the group with `groupadd -r appuser`, a
# system group with no GID pin — it lands at 999, not 1000 (verified live:
# `id appuser` -> uid=1000(appuser) gid=999(appuser)). Every chown of a path the containers
# own must use this, not a hardcoded 1000:1000, so a host directory or volume repaired by a
# script matches one created by the image itself (issue #580).
# NOTE: only the owner bits are load-bearing today — nothing here relies on group access —
# so a stale 1000:1000 was cosmetically wrong rather than broken. Keep it correct anyway:
# the moment a path needs group-write between two identities, the wrong GID becomes a bug.
CONTAINER_UID_GID="${CONTAINER_UID_GID:-1000:999}"

#######################
# UTILITY FUNCTIONS
#######################

# Check if Docker is running and exit if not
check_docker() {
  local docker_output
  docker_output=$(docker info 2>&1)
  if [ $? -eq 0 ]; then
    return 0
  fi

  if echo "$docker_output" | grep -qi "permission denied"; then
    echo ""
    echo "❌ Error: Permission denied accessing Docker."
    echo ""
    # `${USER:-$(id -un)}`: USER is not maintained by bash, so it is unset under `env -i`,
    # in a bare container and in some cron/systemd units — and this is the message whose
    # whole job is to EXPLAIN a permission failure, so under `set -u` the explanation was
    # itself the crash. `id -un` can fail too (a container with no passwd entry for the
    # uid), so it falls back again rather than substituting an empty parenthetical.
    echo "Your user (${USER:-$(id -un 2>/dev/null || echo "unknown")}) is not in the 'docker' group."
    echo "Run the following commands, then log out and back in:"
    echo ""
    echo "  sudo usermod -aG docker \$USER"
    echo "  newgrp docker"
    echo ""
    echo "Or re-run this script with sudo."
  elif echo "$docker_output" | grep -qi "cannot connect\|is the docker daemon running\|no such file"; then
    echo ""
    echo "❌ Error: Docker daemon is not running."
    echo ""
    echo "Start it with:"
    echo "  sudo systemctl start docker"
    echo ""
    echo "To start on boot:  sudo systemctl enable docker"
  else
    echo ""
    echo "❌ Error: Failed to connect to Docker."
    echo "Details: $docker_output"
  fi
  exit 1
}

# Create required directories
create_required_dirs() {
  # Check if the models directory exists and create it if needed
  if [ ! -d "./backend/models" ]; then
    echo "📁 Creating models directory..."
    mkdir -p ./backend/models
  fi

  # Check if the temp directory exists and create it if needed
  if [ ! -d "./backend/temp" ]; then
    echo "📁 Creating temp directory..."
    mkdir -p ./backend/temp
  fi
}

# Fix model cache permissions for non-root container user
fix_model_cache_permissions() {
  # Read MODEL_CACHE_DIR from .env if it exists
  local MODEL_CACHE_DIR=""
  if [ -f .env ]; then
    MODEL_CACHE_DIR=$(grep 'MODEL_CACHE_DIR' .env | grep -v '^#' | cut -d'#' -f1 | cut -d'=' -f2 | tr -d ' "' | head -1)
  fi

  # Use default if not set
  MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-./models}"

  # Check if model cache directory exists
  if [ ! -d "$MODEL_CACHE_DIR" ]; then
    echo "📁 Creating model cache directory: $MODEL_CACHE_DIR"
    mkdir -p "$MODEL_CACHE_DIR/huggingface" "$MODEL_CACHE_DIR/torch" "$MODEL_CACHE_DIR/nltk_data" "$MODEL_CACHE_DIR/sentence-transformers"
  fi

  # Ensure all required subdirectories exist
  mkdir -p "$MODEL_CACHE_DIR/huggingface" "$MODEL_CACHE_DIR/torch" "$MODEL_CACHE_DIR/nltk_data" "$MODEL_CACHE_DIR/sentence-transformers" "$MODEL_CACHE_DIR/opensearch-ml" 2>/dev/null

  # Check ownership of parent AND all subdirectories (subdirs may be root-owned
  # even if the parent is correctly owned by UID 1000)
  local needs_fix=false
  for dir in "$MODEL_CACHE_DIR" "$MODEL_CACHE_DIR"/*/; do
    [ -d "$dir" ] || continue
    local owner
    owner=$(stat -c '%u' "$dir" 2>/dev/null || stat -f '%u' "$dir" 2>/dev/null || echo "unknown")
    if [ "$owner" != "1000" ]; then
      needs_fix=true
      break
    fi
  done

  if [ "$needs_fix" = true ]; then
    echo "🔧 Fixing model cache permissions for non-root container (UID 1000)..."

    # Try using Docker to fix permissions (works without sudo)
    if command -v docker &> /dev/null; then
      if docker run --rm -v "$MODEL_CACHE_DIR:/models" busybox:latest sh -c "chown -R $CONTAINER_UID_GID /models && chmod -R 755 /models" > /dev/null 2>&1; then
        echo "✅ Model cache permissions fixed using Docker"
        return 0
      fi
    fi

    # Fallback: try direct chown if user has permissions
    if chown -R "$CONTAINER_UID_GID" "$MODEL_CACHE_DIR" > /dev/null 2>&1 && chmod -R 755 "$MODEL_CACHE_DIR" > /dev/null 2>&1; then
      echo "✅ Model cache permissions fixed"
      return 0
    fi

    # If both methods fail, show warning
    echo "⚠️  Warning: Could not automatically fix model cache permissions"
    echo "   If you encounter permission errors, run: ./scripts/fix-model-permissions.sh"
    return 1
  fi

  return 0
}

# Give every file in the NLTK cache its own inode (issue #491).
#
# NLTK >= 3.10 hardens file access with `nltk/pathsec.py`, which REFUSES to open any
# multiply-linked file:
#
#   PermissionError: Security Violation [pathsec.open]: refusing multiply-linked file
#   '.../nltk_data/tokenizers/punkt_tab/english/collocations.tab' (st_nlink=3);
#   a hardlink can point at an outside-root inode (CWE-59)
#
# That is a legitimate control (a hardlink in a mounted cache can alias an inode outside
# the data root), so the fix is to make the DATA comply — never to disable the check or
# pin back to an unhardened NLTK. A cache restored from a backup, or copied with
# `cp -al` / `rsync --link-dest`, arrives fully hardlinked; every punkt read then raises
# and `split_sentences_nltk` fails for every transcription on the box.
#
# ⚠️ Scoped to nltk_data ON PURPOSE. The huggingface/torch/sentence-transformers caches
# are also commonly hardlinked, but nothing reads them through pathsec, and those caches
# dedupe deliberately — breaking their links can double tens of GB on disk. nltk_data is
# ~45 MB, so rewriting it is free.
#
# Content is preserved exactly (`cp -p` then atomic `mv`); only the inode changes.
# Safe and idempotent: a cache with no multiply-linked files is a no-op.
ensure_nltk_data_unlinked() {
  local MODEL_CACHE_DIR=""
  if [ -f .env ]; then
    MODEL_CACHE_DIR=$(grep 'MODEL_CACHE_DIR' .env | grep -v '^#' | cut -d'#' -f1 | cut -d'=' -f2 | tr -d ' "' | head -1)
  fi
  MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-./models}"

  local nltk_dir="$MODEL_CACHE_DIR/nltk_data"
  [ -d "$nltk_dir" ] || return 0

  local linked
  linked=$(find "$nltk_dir" -type f -links +1 2>/dev/null | wc -l)
  [ "$linked" -gt 0 ] || return 0

  echo "🔗 De-hardlinking $linked NLTK data file(s) — NLTK >= 3.10 refuses multiply-linked files"

  local rewritten=0 failed=0 f
  while IFS= read -r f; do
    if cp -p "$f" "$f.dehl" 2>/dev/null && mv -f "$f.dehl" "$f" 2>/dev/null; then
      rewritten=$((rewritten + 1))
    else
      rm -f "$f.dehl" 2>/dev/null
      failed=$((failed + 1))
    fi
  done < <(find "$nltk_dir" -type f -links +1 2>/dev/null)

  if [ "$failed" -gt 0 ]; then
    echo "⚠️  Warning: could not de-hardlink $failed NLTK file(s); sentence splitting will fail"
    echo "   with 'Security Violation [pathsec.open]: refusing multiply-linked file'."
    echo "   Manual fix (content is preserved, only the inode changes):"
    echo "     find $nltk_dir -type f -links +1 \\"
    echo "       -exec sh -c 'cp -p \"\$1\" \"\$1.dehl\" && mv -f \"\$1.dehl\" \"\$1\"' _ {} \\;"
    return 1
  fi

  echo "✅ NLTK cache de-hardlinked ($rewritten file(s))"
  return 0
}

# Fix ownership of the shared pipeline_scratch Docker named volume.
#
# The volume is created root-owned by default, which blocks the non-root
# container user (UID 1000) from staging the preprocessed WAV into it —
# turning the Phase 2 shared-memory handoff into a silent fallback to
# MinIO. This helper resolves the namespaced volume (compose prefixes
# it with the project name) and chowns it via a throwaway busybox
# container — same pattern used for the model cache.
#
# Safe to run multiple times. No-op when the volume doesn't exist yet
# (first boot before compose up).
fix_pipeline_scratch_permissions() {
  if ! command -v docker &> /dev/null; then
    return 0
  fi

  # Resolve the actual volume name (docker-compose prefixes with project
  # name; matching on the suffix keeps this portable across checkouts).
  local vol
  vol=$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -E '_pipeline_scratch$' | head -1)

  if [ -z "$vol" ]; then
    # Volume not created yet — will be picked up on next invocation after
    # `docker compose up`. Silent return; not an error.
    return 0
  fi

  # Cheap probe: if the volume already mounts as UID 1000, skip the chown.
  local owner
  owner=$(docker run --rm -v "$vol:/scratch" busybox:latest stat -c '%u' /scratch 2>/dev/null || echo "unknown")
  if [ "$owner" = "1000" ]; then
    return 0
  fi

  echo "🔧 Fixing pipeline_scratch volume permissions for non-root container (UID 1000)..."
  if docker run --rm -v "$vol:/scratch" busybox:latest \
      sh -c "chown -R $CONTAINER_UID_GID /scratch && chmod 775 /scratch" > /dev/null 2>&1; then
    echo "✅ pipeline_scratch permissions fixed"
    return 0
  fi

  echo "⚠️  Warning: Could not fix pipeline_scratch permissions."
  echo "   Scratch-volume handoff will fall back to MinIO until this is resolved."
  echo "   Manual fix:"
  echo "     docker run --rm -v $vol:/scratch busybox chown -R $CONTAINER_UID_GID /scratch"
  return 1
}

# Ensure OpenSearch neural models are downloaded for offline capability
ensure_opensearch_models() {
  # Read MODEL_CACHE_DIR from .env if it exists
  local MODEL_CACHE_DIR=""
  if [ -f .env ]; then
    MODEL_CACHE_DIR=$(grep 'MODEL_CACHE_DIR' .env | grep -v '^#' | cut -d'#' -f1 | cut -d'=' -f2 | tr -d ' "' | head -1)
  fi

  # Use default if not set
  MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-./models}"

  # Check if OpenSearch neural models directory exists and has content
  local opensearch_models_dir="$MODEL_CACHE_DIR/opensearch-ml"

  # Check if default model exists (all-MiniLM-L6-v2)
  if [ -d "$opensearch_models_dir/all-MiniLM-L6-v2" ] && [ -n "$(ls -A "$opensearch_models_dir/all-MiniLM-L6-v2" 2>/dev/null)" ]; then
    echo "✅ OpenSearch neural models found"
    return 0
  fi

  # Models not found - try to download them
  echo "📥 OpenSearch neural models not found - attempting download..."
  echo "   (Default model: all-MiniLM-L6-v2, ~80MB)"

  # Check if download-models.py exists
  if [ ! -f "./scripts/download-models.py" ]; then
    echo "⚠️  Warning: download-models.py not found - models will download on first use"
    return 1
  fi

  # Check if Docker is available
  if ! command -v docker &> /dev/null; then
    echo "⚠️  Warning: Docker not found - models will download on first use"
    return 1
  fi

  # Check if backend Docker image exists (pull if not)
  if ! docker image inspect davidamacey/opentranscribe-backend:latest > /dev/null 2>&1; then
    echo "   Backend Docker image not found locally - pulling from Docker Hub..."
    if ! docker pull davidamacey/opentranscribe-backend:latest > /dev/null 2>&1; then
      echo "⚠️  Warning: Could not pull backend image - models will download on first use"
      return 1
    fi
  fi

  # Set environment to download only default OpenSearch model
  export OPENSEARCH_MODELS="all-MiniLM-L6-v2"

  # Run download script (only for OpenSearch models - others handled separately)
  echo "📥 Downloading OpenSearch neural model (all-MiniLM-L6-v2)..."

  # Get Hugging Face token from .env if available
  local HF_TOKEN=""
  if [ -f .env ]; then
    HF_TOKEN=$(grep '^HUGGINGFACE_TOKEN=' .env | grep -v '^#' | cut -d'=' -f2 | tr -d ' "' | head -1)
  fi

  # Detect GPU
  local use_gpu="false"
  local gpu_args=""
  if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
    use_gpu="true"
    if [ -n "${GPU_DEVICE_ID:-}" ]; then
      gpu_args="--gpus device=${GPU_DEVICE_ID}"
    else
      gpu_args="--gpus all"
    fi
  fi

  # Create opensearch-ml directory if needed
  mkdir -p "$opensearch_models_dir"

  # Download OpenSearch models using Docker (same approach as transcription models)
  # When using --gpus device=X, Docker isolates that GPU as device 0 in the container
  # Do NOT set CUDA_VISIBLE_DEVICES - PyTorch will automatically use the only available GPU
  # shellcheck disable=SC2086
  docker run --rm \
      $gpu_args \
      -e HUGGINGFACE_TOKEN="${HF_TOKEN}" \
      -e USE_GPU="${use_gpu}" \
      -e OPENSEARCH_MODELS="all-MiniLM-L6-v2" \
      -v "$(realpath "$opensearch_models_dir"):/home/appuser/.cache/opensearch-ml" \
      -v "./scripts/download-models.py:/app/download-models.py:ro" \
      davidamacey/opentranscribe-backend:latest \
      python /app/download-models.py 2>&1 | grep -E "(Downloading|Downloaded|ERROR|WARNING|Success)" || true

  # Check if model was actually downloaded
  if [ -d "$opensearch_models_dir/all-MiniLM-L6-v2" ] && [ -n "$(ls -A "$opensearch_models_dir/all-MiniLM-L6-v2" 2>/dev/null)" ]; then
    echo "✅ OpenSearch neural model downloaded and cached"
    return 0
  else
    echo ""
    echo "⚠️  OpenSearch model download was unsuccessful"
    echo "   Don't worry - models will auto-download during backend startup"
    echo "   This is normal and search will work correctly"
    echo ""
    return 1
  fi
}

# Ensure the NLTK corpora are on disk BEFORE anything needs them (issue #491).
#
# The sibling `ensure_opensearch_models` above mounts only `opensearch-ml`, so
# anything the downloader fetched outside that directory — including every NLTK
# corpus — was written into the one-shot container and discarded with it. Nothing
# else prefetched them either, so they were fetched at RUNTIME, on first use, from
# inside the transcription and topic pipelines. On an airgapped or firewalled
# deployment those calls do not fail fast: `nltk.download` swallows its own
# network errors, so the caller hangs on a socket timeout or quietly finds the
# corpus still missing one line later.
#
# Same shape as its sibling deliberately: probe, one-shot container, verify. It
# needs no GPU and no Hugging Face token (the corpora come from NLTK's own CDN),
# which is why the `--only nltk` selector exists on the downloader.
ensure_nltk_corpora() {
  # Read MODEL_CACHE_DIR from .env if it exists
  local MODEL_CACHE_DIR=""
  if [ -f .env ]; then
    MODEL_CACHE_DIR=$(grep 'MODEL_CACHE_DIR' .env | grep -v '^#' | cut -d'#' -f1 | cut -d'=' -f2 | tr -d ' "' | head -1)
  fi
  MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-./models}"

  local nltk_dir="$MODEL_CACHE_DIR/nltk_data"

  # `-d` is NOT the check: the directory is created unconditionally by the
  # downloader and bind-mounted by every compose file, so it exists on a
  # deployment that has never fetched a corpus. `tokenizers/` is what the app
  # actually loads.
  if [ -d "$nltk_dir/tokenizers" ] && [ -n "$(ls -A "$nltk_dir/tokenizers" 2>/dev/null)" ]; then
    return 0
  fi

  echo "📥 NLTK corpora not found - fetching (needed offline; ~50MB)..."

  if [ ! -f "./scripts/download-models.py" ]; then
    echo "⚠️  Warning: download-models.py not found - NLTK will be fetched at runtime"
    return 1
  fi

  if ! command -v docker &> /dev/null; then
    echo "⚠️  Warning: Docker not found - NLTK will be fetched at runtime"
    return 1
  fi

  if ! docker image inspect davidamacey/opentranscribe-backend:latest > /dev/null 2>&1; then
    echo "   Backend Docker image not found locally - pulling from Docker Hub..."
    if ! docker pull davidamacey/opentranscribe-backend:latest > /dev/null 2>&1; then
      echo "⚠️  Warning: Could not pull backend image - NLTK will be fetched at runtime"
      return 1
    fi
  fi

  mkdir -p "$nltk_dir"

  # NLTK_DATA is passed explicitly and the downloader honours it, so the corpora
  # land in the mounted directory whatever the image's $HOME happens to be —
  # Dockerfile.blackwell runs as `user`, not `appuser`.
  docker run --rm \
      -e NLTK_DATA=/nltk_data \
      -v "$(realpath "$nltk_dir"):/nltk_data" \
      -v "./scripts/download-models.py:/app/download-models.py:ro" \
      davidamacey/opentranscribe-backend:latest \
      python /app/download-models.py --only nltk 2>&1 | grep -E "(Downloading|Downloaded|ERROR|WARNING|Success)" || true

  if [ -d "$nltk_dir/tokenizers" ] && [ -n "$(ls -A "$nltk_dir/tokenizers" 2>/dev/null)" ]; then
    echo "✅ NLTK corpora cached"
    return 0
  fi

  echo "⚠️  NLTK corpora were not cached - they will be fetched at runtime instead."
  echo "   An airgapped deployment will degrade to the regex sentence splitter."
  return 1
}

#######################
# INFO FUNCTIONS
#######################

# Print access information for all services
# Detects NGINX configuration and shows appropriate URLs
print_access_info() {
  # Check if NGINX is configured (via NGINX_SERVER_NAME env var or .env file)
  # In dev mode, NGINX is never used (Vite serves frontend directly)
  local domain=""
  local protocol="https"
  local https_port="${NGINX_HTTPS_PORT:-443}"

  # Only show NGINX info in production mode
  if [ "${ENVIRONMENT:-dev}" != "dev" ]; then
    # Check environment variable first
    if [ -n "${NGINX_SERVER_NAME:-}" ]; then
      domain="$NGINX_SERVER_NAME"
    # Then check .env file
    elif [ -f .env ]; then
      domain=$(grep '^NGINX_SERVER_NAME=' .env | grep -v '^#' | cut -d'=' -f2 | tr -d ' "' | head -1)
      https_port=$(grep '^NGINX_HTTPS_PORT=' .env | grep -v '^#' | cut -d'=' -f2 | tr -d ' "' | head -1)
      https_port="${https_port:-443}"
    fi
  fi

  echo ""
  if [ -n "$domain" ]; then
    # NGINX reverse proxy mode - single entry point with HTTPS
    local port_suffix=""
    if [ "$https_port" != "443" ]; then
      port_suffix=":$https_port"
    fi

    echo "🔒 NGINX Reverse Proxy Mode (HTTPS)"
    echo "🌐 Access the application at:"
    echo "   - Frontend:          ${protocol}://${domain}${port_suffix}"
    echo "   - API:               ${protocol}://${domain}${port_suffix}/api"
    echo "   - API Documentation: ${protocol}://${domain}${port_suffix}/api/docs"
    echo "   - Flower Dashboard:  ${protocol}://${domain}${port_suffix}/flower/"
    echo "   - MinIO Console:     ${protocol}://${domain}${port_suffix}/minio/"
    echo "   - Documentation:     ${protocol}://${domain}${port_suffix}/docs/"
    echo ""
    echo "📝 Note: Browser microphone recording is now available via HTTPS!"
    echo "   If you see certificate warnings, trust the certificate on your device."
    echo "   See: docs/NGINX_SETUP.md for instructions"
  else
    # Direct container access mode (development default).
    # Read the same *_PORT variables compose interpolates into its `ports:`
    # entries, so a custom .env layout — or a fresh stack's --port-offset,
    # which exports them offset — prints the ports actually published.
    echo "🌐 Access the application at:"
    echo "   - Frontend:            http://localhost:${FRONTEND_PORT:-5173}"
    echo "   - API:                 http://localhost:${BACKEND_PORT:-5174}/api"
    echo "   - API Documentation:   http://localhost:${BACKEND_PORT:-5174}/docs"
    echo "   - MinIO Console:       http://localhost:${MINIO_CONSOLE_PORT:-5179}"
    echo "   - Flower Dashboard:    http://localhost:${FLOWER_PORT:-5175}/flower"
    echo "   - OpenSearch:          http://localhost:${OPENSEARCH_PORT:-5180}"
    echo "   - Documentation:       http://localhost:${DOCS_PORT:-5183}/docs/"
    echo ""
    echo "📝 Note: Microphone recording only works on localhost in this mode."
    echo "   For HTTPS access from other devices, set NGINX_SERVER_NAME in .env"
    echo "   See: docs/NGINX_SETUP.md for instructions"
  fi
  echo ""
}

#######################
# DOCKER FUNCTIONS
#######################

# Wait for backend to be healthy with timeout
# Uses $COMPOSE_CMD if set (for prod mode), otherwise uses 'docker compose' (for dev mode)
wait_for_backend_health() {
  TIMEOUT=60
  INTERVAL=2
  ELAPSED=0

  # Use COMPOSE_CMD if set (prod mode), otherwise default to 'docker compose' (dev mode)
  local CMD="${COMPOSE_CMD:-docker compose}"

  while [ $ELAPSED -lt $TIMEOUT ]; do
    if $CMD ps | grep backend | grep "(healthy)" > /dev/null; then
      echo "✅ Backend is healthy!"
      return 0
    fi
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
    echo "⏳ Waiting for backend... ($ELAPSED/$TIMEOUT seconds)"
  done

  echo "⚠️ Backend health check timed out, but continuing anyway..."
  $CMD logs backend --tail 20
  return 1
}

#######################
# DATABASE RESTORE HELPERS (issue #599, extended for #600)
#######################
#
# Pure helpers factored out of opentr.sh's restore_database so the integration tests
# (backend/tests/integration/test_opentr_restore_roundtrip.py,
# backend/tests/integration/test_scheduled_backup_restore_roundtrip.py) drive the exact code
# the CLI ships, not a re-implementation that could silently diverge from it.
#
# Each takes an *exec prefix* — a command that runs a psql/pg_dump/pg_restore client with
# stdin attached — so production passes "docker compose exec -T postgres" and a test passes
# "docker exec -i <throwaway-container>". Word-splitting the prefix is intentional,
# the same pattern $COMPOSE_FILES uses elsewhere in this repo.
#
# Two dump formats reach this file, with two independent helper families:
#
# - Plain-SQL (`./opentr.sh backup`, and the mandatory pre-restore safety dump) — replayed
#   with `psql`. Background: a plain `pg_dump` file has no DROP/--clean statements, so
#   replaying it into an already-populated database makes every statement fail — and without
#   `ON_ERROR_STOP`, `psql` exits 0 anyway. Worse, the dump's `alembic_version` INSERT does
#   NOT collide on primary key with a drifted row already there, so it succeeds while every
#   other statement fails — leaving TWO rows in `alembic_version`, which Alembic can no longer
#   migrate from. The fix is to guarantee an empty target: drop and recreate the database,
#   then replay inside a single transaction so any failure rolls back to nothing rather than a
#   hybrid schema.
# - Custom-format `-Fc` (the scheduled/S3 backup feature, `backup_service.run_pg_dump`) —
#   replayed with `pg_restore` (issue #600). Same guarantee, different tool: `pg_restore`
#   also needs a guaranteed-empty target and a single-transaction replay, but its own
#   quirks — `-j`/`--single-transaction` are mutually exclusive, and it can exit 1 while
#   having ALREADY committed partial damage — are documented beside `pg_replay_custom_dump`
#   below. Both formats share one comparison routine, `_pg_verify_against`, so "does the
#   restored database match the backup" cannot drift into two different definitions.

# Drop and recreate a database so a restore always lands in a guaranteed-empty
# target, regardless of schema drift between backup time and restore time.
# Must connect via `-d postgres` — you cannot drop the database you are connected
# to. `WITH (FORCE)` (PostgreSQL 13+) also terminates any connection holders
# (flower, the monitoring postgres-exporter, a stray psql session) that a
# stop-list of services could miss.
#
# $1 exec_prefix  e.g. "docker compose exec -T postgres" or "docker exec -i <container>"
# $2 user
# $3 db
pg_drop_and_recreate_database() {
  local exec_prefix="$1"
  local user="$2"
  local db="$3"
  # shellcheck disable=SC2086
  $exec_prefix psql -v ON_ERROR_STOP=1 -U "$user" -d postgres \
      -c "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE);" \
    && $exec_prefix psql -v ON_ERROR_STOP=1 -U "$user" -d postgres \
      -c "CREATE DATABASE \"$db\" OWNER \"$user\";"
}

# Replay a plain-SQL pg_dump file into what must already be an empty database.
# `--single-transaction` is what turns a mid-dump failure into a full rollback
# rather than a hybrid schema — measured: a failure partway through the dump
# leaves zero tables with this flag, versus however far the dump got without it.
#
# $1 exec_prefix
# $2 user
# $3 db
# $4 sql_file
pg_replay_dump() {
  local exec_prefix="$1"
  local user="$2"
  local db="$3"
  local sql_file="$4"
  # shellcheck disable=SC2086
  $exec_prefix psql -v ON_ERROR_STOP=1 --single-transaction -U "$user" "$db" < "$sql_file"
}

# Shared comparison routine behind both pg_verify_restore (plain-SQL) and
# pg_verify_custom_restore (-Fc). The two public functions differ ONLY in how they obtain
# expected_head/expected_tables from their respective archive format — everything here (the
# alembic-head match, the exactly-one-row check, the public BASE TABLE count comparison, the
# "no head found -> warn and skip, don't fail" allowance) is format-independent. Keeping this
# as one function is deliberate: two verifiers with independently drifting comparison logic
# is exactly the "two paths doing the same job" this repo's conventions forbid.
#
# $1 exec_prefix
# $2 user
# $3 db
# $4 expected_head    (may be empty — see above)
# $5 expected_tables
# $6 caller_name       (for error-message prefixes only, e.g. "pg_verify_restore")
# Returns 0 on pass, 1 on failure (mismatch detail printed to stdout).
_pg_verify_against() {
  local exec_prefix="$1"
  local user="$2"
  local db="$3"
  local expected_head="$4"
  local expected_tables="$5"
  local caller_name="$6"

  local ok=1

  if [ -z "$expected_head" ]; then
    echo "⚠️  $caller_name: no alembic_version head found in the backup — skipping head check (fine for a hand-trimmed dump)."
  else
    local actual_head
    actual_head="$($exec_prefix psql -tA -U "$user" "$db" -c "SELECT version_num FROM alembic_version;" 2>/dev/null | tr -d '[:space:]')"
    if [ "$actual_head" != "$expected_head" ]; then
      echo "❌ $caller_name: alembic_version mismatch — expected '$expected_head', got '$actual_head'"
      ok=0
    fi

    local head_count
    head_count="$($exec_prefix psql -tA -U "$user" "$db" -c "SELECT count(*) FROM alembic_version;" 2>/dev/null | tr -d '[:space:]')"
    if [ "$head_count" != "1" ]; then
      echo "❌ $caller_name: alembic_version has $head_count row(s), expected exactly 1"
      ok=0
    fi
  fi

  local actual_tables
  actual_tables="$($exec_prefix psql -tA -U "$user" "$db" -c \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE';" 2>/dev/null | tr -d '[:space:]')"
  if [ "$actual_tables" != "$expected_tables" ]; then
    echo "❌ $caller_name: public table count mismatch — expected $expected_tables, got $actual_tables"
    ok=0
  fi

  [ "$ok" -eq 1 ]
}

# Verify a restore actually reproduced the dump's content — the regression
# detector for issue #599's exact failure mode (a restore that reports success
# while leaving TWO conflicting `alembic_version` rows and every data table
# untouched).
#
# Checks, against the live database:
#   - exactly one `alembic_version` row, matching the dump's own head (if the
#     dump's head can't be determined — e.g. a hand-trimmed dump with no
#     alembic_version COPY block — this check is skipped with a warning, not
#     treated as failure; a hand-trimmed dump is a legitimate input)
#   - the public schema's BASE TABLE count matches the dump's `CREATE TABLE`
#     count (assumes no partitioned tables — OpenTranscribe has none today;
#     `CREATE TABLE ... PARTITION OF` would need a different count)
#
# $1 exec_prefix
# $2 user
# $3 db
# $4 sql_file
# Returns 0 on pass, 1 on failure (mismatch detail printed to stdout).
pg_verify_restore() {
  local exec_prefix="$1"
  local user="$2"
  local db="$3"
  local sql_file="$4"

  local expected_head
  expected_head="$(awk '/^COPY public\.alembic_version /{getline; print; exit}' "$sql_file")"

  local expected_tables
  expected_tables="$(grep -c '^CREATE TABLE ' "$sql_file")"

  _pg_verify_against "$exec_prefix" "$user" "$db" "$expected_head" "$expected_tables" "pg_verify_restore"
}

# Replay a custom-format (`pg_dump --format=custom`, i.e. `-Fc`) dump into what must
# already be an empty database — the `-Fc` sibling of pg_replay_dump, for the scheduled/S3
# backup feature's artifacts (issue #600).
#
# `--single-transaction` implies `--exit-on-error`, but both are passed explicitly so that
# dropping one later is a visible edit rather than a silent loss of atomicity.
# `--no-owner --no-privileges` is belt-and-braces given the dump was taken with `pg_dump
# --no-owner --no-acl` (backup_service.run_pg_dump) — and is what makes a dump taken
# *without* those flags still restorable here.
# `-j`/`--jobs` (parallel restore) is DELIBERATELY ABSENT: measured, it is mutually
# exclusive with `--single-transaction` (`pg_restore: error: cannot specify both
# --single-transaction and multiple jobs`). This is an accepted, documented cost of the
# safe path, not a bug — an operator who knowingly needs `-j` can run pg_restore by hand.
#
# ⚠️ pg_restore's exit code is NOT a safety property the way psql's would be here: a
# corrupted/truncated archive can exit 1 having already committed partial work within the
# transaction before failing (measured, issue #600). --single-transaction still guarantees
# the *whole* thing rolls back on failure — verify that with pg_verify_custom_restore /
# the public table count, never by trusting the exit code alone.
#
# $1 exec_prefix
# $2 user
# $3 db
# $4 dump_file
pg_replay_custom_dump() {
  local exec_prefix="$1"
  local user="$2"
  local db="$3"
  local dump_file="$4"
  # shellcheck disable=SC2086
  $exec_prefix pg_restore -U "$user" -d "$db" --exit-on-error --single-transaction --no-owner --no-privileges < "$dump_file"
}

# Echo a custom-format dump's own alembic head, the -Fc sibling of pg_verify_restore's
# `awk` extraction over a plain-SQL file. `pg_restore --data-only --table=alembic_version
# -f -` emits a plain-SQL fragment containing a literal `COPY public.alembic_version
# (version_num) FROM stdin;` block, so the SAME awk line reads it — one convention for
# "read the head out of a backup", two producers.
#
# $1 exec_prefix
# $2 user
# $3 dump_file
pg_custom_dump_expected_head() {
  local exec_prefix="$1"
  local user="$2"
  local dump_file="$3"
  # shellcheck disable=SC2086
  $exec_prefix pg_restore -U "$user" --data-only --table=alembic_version -f - < "$dump_file" \
    | awk '/^COPY public\.alembic_version /{getline; print; exit}'
}

# Verify a custom-format restore — the -Fc sibling of pg_verify_restore, sharing the same
# comparison logic via _pg_verify_against. The one real difference is HOW the expected facts
# are read out of the archive:
#   - expected_head: pg_custom_dump_expected_head (above).
#   - expected_tables: the archive's own TOC (`pg_restore --list`), filtered on the type
#     field. ⚠️ Measured gotcha: the naive `grep -c ' TABLE '` OVERCOUNTS, because `TABLE
#     DATA` entries also match ' TABLE ' (4 vs the correct 2, on a two-table archive) — a
#     verifier built on that filter fails on every correct restore. The type field is
#     column 4 of the TOC line and `TABLE DATA` sets column 5 to `DATA`, so `$4 == "TABLE"
#     && $5 != "DATA"` counts only the real CREATE TABLE entries.
#
# $1 exec_prefix
# $2 user
# $3 db
# $4 dump_file
# Returns 0 on pass, 1 on failure (mismatch detail printed to stdout).
pg_verify_custom_restore() {
  local exec_prefix="$1"
  local user="$2"
  local db="$3"
  local dump_file="$4"

  local expected_head
  expected_head="$(pg_custom_dump_expected_head "$exec_prefix" "$user" "$dump_file")"

  local expected_tables
  # shellcheck disable=SC2086
  expected_tables="$($exec_prefix pg_restore --list < "$dump_file" \
    | awk '$4 == "TABLE" && $5 != "DATA"' | wc -l | tr -d '[:space:]')"

  _pg_verify_against "$exec_prefix" "$user" "$db" "$expected_head" "$expected_tables" "pg_verify_custom_restore"
}

# Display quick reference commands
print_help_commands() {
  echo "⚡ Quick Commands Reference:"
  echo "   - Reset environment: ./opentr.sh reset [dev|prod]"
  echo "   - Stop all services: ./opentr.sh stop"
  echo "   - View logs: ./opentr.sh logs [service_name]"
  echo "   - Restart backend: ./opentr.sh restart-backend"
  echo "   - Rebuild after code changes: ./opentr.sh rebuild-backend or ./opentr.sh rebuild-frontend"
}
