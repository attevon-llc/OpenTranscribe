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

# Generate a real MinIO KMS secret key on first run if .env.example's shipped
# placeholder was never replaced (issue #614).
#
# MinIO's KMS auto-encryption (MINIO_KMS_AUTO_ENCRYPTION=on, the .env.example
# default) requires MINIO_KMS_SECRET_KEY in the form <key-name>:<base64-encoded-
# 32-byte-key> -- see the minio service's own comment in docker-compose.yml.
# .env.example ships MINIO_KMS_SECRET_KEY=CHANGE_ME_auto_generated_on_install,
# which is not that format, so a genuinely fresh `cp .env.example .env` +
# `./opentr.sh start dev` refused to boot MinIO until an operator manually
# generated a real key. scripts/install-offline-package.sh and
# windows-installer/generate-secrets.ps1 already generate a value in this exact
# format for THEIR OWN first-run paths (opentranscribe-key:$(openssl rand
# -base64 32)); this is the same generation, for the one first-run path neither
# of those covers -- a plain `./opentr.sh start dev` / `start prod`.
#
# read_env_value KEY [ENV_FILE]
#   Reads one plain-config value out of a dotenv file, honouring the dotenv inline-comment
#   convention: a `#` PRECEDED BY WHITESPACE starts a comment, a bare `#` inside a value does
#   not. `ENVIRONMENT=production  # prod box` used to yield `production#prodbox`, silently
#   breaking every prod-hardening string comparison in opentranscribe.sh (issue #590).
#
#   NOT for secrets. REDIS_PASSWORD / JWT_SECRET_KEY / ENCRYPTION_KEY may legitimately
#   contain `#` (even ` #`), so those reads deliberately do NOT go through this and must
#   keep their raw `cut -d= -f2-` form.
#
#   `|| true` is load-bearing: callers run under `set -e`, and an ABSENT optional key is the
#   normal case (grep exits 1). Same reasoning as setup-opentranscribe.sh's `_env_val`.
#   `-f2-` not `-f2`: a value may legitimately contain `=`.
#
#   Also honours the two spellings `docker compose` itself accepts in a `.env` file but this
#   parser used to miss (verified with `docker compose config` against a real compose file):
#   leading whitespace before the key (`  KEY=value`) and an `export ` prefix (`export
#   KEY=value`). Missing either meant the CONTAINER got the value while this function read
#   back empty — e.g. a leading-space `ENGINE_DIARIZER_BACKEND=pyannote` ran pyannote in the
#   container while every gate here still saw "" and defaulted to native, starting/guarding
#   for an engine nothing was using. Stripped ONCE up front so `^${key}=` keeps anchoring on
#   the bare key, rather than growing a second regex per caller.
read_env_value() {
  local key="$1" env_file="${2:-.env}"
  [ -f "$env_file" ] || { echo ""; return 0; }
  sed -E 's/^[[:space:]]+//; s/^export[[:space:]]+//' "$env_file" 2>/dev/null \
    | grep -E "^${key}=" \
    | head -1 \
    | cut -d= -f2- \
    | sed -E 's/[[:space:]]+#.*$//' \
    | tr -d ' "' \
    || true
}

# Only touches the SHIPPED PLACEHOLDER. An empty or already-customized value is
# left alone -- either means an operator made a deliberate choice (e.g. leaving
# KMS auto-encryption off), and this must never clobber a real key.
ensure_minio_kms_secret() {
  local env_file="${1:-.env}"
  [ -f "$env_file" ] || return 0

  # Guard against ambiguous input BEFORE reading "the" current value: two matching
  # lines (a real key first, a leftover placeholder line second, or vice versa) means
  # `tail -1` and the sed replace-all below can silently pick or rewrite the wrong one
  # -- possibly clobbering a real, already-in-use key. Fail closed and name the fix
  # rather than guess.
  # `|| true` INSIDE the substitution, not after the assignment: `local match_count`
  # combined with `=$(...)` on one line reports the `local` builtin's own exit status
  # under `set -e`, but split as `local match_count; match_count=$(...)` the
  # assignment's status IS grep's -- and grep exits 1 on zero matches, which would
  # abort this whole function under opentranscribe.sh's `set -e`. Keeping the guard
  # inside the substitution keeps the count correct either way.
  local match_count
  match_count=$(grep -cE '^MINIO_KMS_SECRET_KEY=' "$env_file" || true)
  if [ "$match_count" -gt 1 ]; then
    echo "❌ Error: ${env_file} has ${match_count} lines starting with MINIO_KMS_SECRET_KEY= -- refusing to guess which one is real."
    echo "   Remove the duplicate line(s) by hand, then re-run."
    return 1
  fi

  local current
  current=$(grep -E '^MINIO_KMS_SECRET_KEY=' "$env_file" | tail -1 | cut -d'=' -f2- | tr -d ' "')

  case "$current" in
    *CHANGE_ME*)
      local generated
      generated="opentranscribe-key:$(openssl rand -base64 32)"
      # No confirmation prompt for this .env edit -- deliberate deviation from this
      # repo's usual ".env is never overwritten without confirmation" rule, because the
      # value being replaced is ONLY ever the shipped placeholder (never a real key --
      # see the case guard above and the "left alone" comment below), so there is
      # nothing of the operator's to lose. What IS consequential is the generated key
      # itself: MinIO decrypts every object it wrote under KMS auto-encryption with
      # whatever key was active at write time, so losing this value after real data
      # has been encrypted with it makes that data permanently unreadable. Warn loudly
      # rather than silently.
      sed -i "s|^MINIO_KMS_SECRET_KEY=.*|MINIO_KMS_SECRET_KEY=${generated}|" "$env_file"
      # A fresh install is `cp .env.example .env` → mode 0644, and GNU `sed -i` preserves
      # mode. The value just written is the one REAL secret in an otherwise placeholder-only
      # file, and losing it makes every KMS-encrypted object permanently unreadable — so it
      # must not stay world-readable. Matches scripts/install-offline-package.sh:541, which
      # this generation was modelled on and which already does exactly this.
      chmod 600 "$env_file" || echo "⚠️  Could not chmod 600 ${env_file} — it may be world-readable."
      echo "🔑 Generated MINIO_KMS_SECRET_KEY in ${env_file} (replaced the .env.example placeholder) so MinIO KMS auto-encryption can boot."
      echo "⚠️  BACK UP this value now: ${env_file}'s MINIO_KMS_SECRET_KEY. MinIO decrypts every"
      echo "   object written under KMS auto-encryption with this exact key -- if it is lost,"
      echo "   everything encrypted with it becomes permanently unreadable, with no recovery."
      # opentr.sh already `set -a; source ./.env`'d the placeholder into this
      # shell's environment before this runs, and docker compose's variable
      # interpolation prefers an inherited shell env var over re-reading .env
      # -- without this re-export, the freshly-patched file would be silently
      # ignored by the very `docker compose up` this is meant to unblock.
      export MINIO_KMS_SECRET_KEY="$generated"
      ;;
  esac
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
    MODEL_CACHE_DIR=$(read_env_value MODEL_CACHE_DIR .env)
  fi

  # Use default if not set
  MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-./models}"

  # Check if model cache directory exists
  if [ ! -d "$MODEL_CACHE_DIR" ]; then
    echo "📁 Creating model cache directory: $MODEL_CACHE_DIR"
    mkdir -p "$MODEL_CACHE_DIR/huggingface" "$MODEL_CACHE_DIR/torch" "$MODEL_CACHE_DIR/nltk_data" "$MODEL_CACHE_DIR/sentence-transformers"
  fi

  # Ensure all required subdirectories exist.
  #
  # ⚠️ diar-native MUST be created here, before `compose up`, even though nothing has
  # written to it yet. It is a bind-mount source: if it does not exist when the container
  # starts, dockerd creates it **root-owned**, and the backend — which runs as appuser and
  # is the process that exports the model set into it — then fails with
  # `provision-models` exit 7 (NOT_WRITABLE) on every fresh install. Reproduced live: a
  # fresh-install rehearsal left a root:root empty models/diar-native and the sidecar
  # silently served nothing. The ownership loop below only repairs directories that
  # exist, so creating it is what lets the repair reach it.
  mkdir -p "$MODEL_CACHE_DIR/huggingface" "$MODEL_CACHE_DIR/torch" "$MODEL_CACHE_DIR/nltk_data" "$MODEL_CACHE_DIR/sentence-transformers" "$MODEL_CACHE_DIR/opensearch-ml" "$MODEL_CACHE_DIR/diar-native" 2>/dev/null

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
    MODEL_CACHE_DIR=$(read_env_value MODEL_CACHE_DIR .env)
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
    MODEL_CACHE_DIR=$(read_env_value MODEL_CACHE_DIR .env)
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
    HF_TOKEN=$(read_env_value HUGGINGFACE_TOKEN .env)
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
    MODEL_CACHE_DIR=$(read_env_value MODEL_CACHE_DIR .env)
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
      domain=$(read_env_value NGINX_SERVER_NAME .env)
      https_port=$(read_env_value NGINX_HTTPS_PORT .env)
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

# Decide whether a completed restore should restart the app services `restore_database`
# stopped, or leave them stopped because the schema the app is about to see does not
# match what it expects (issue #610).
#
# The backend runs `alembic upgrade head` on every startup (`app/main.py` lifespan ->
# `app/db/migrations.py:run_migrations`). Restarting the CURRENTLY-RUNNING image over a
# database that was just restored to a DIFFERENT alembic head silently re-migrates the
# backup forward (or, for a newer dump onto an older image, crashes on an unknown
# revision) -- destroying the very state the restore recovered. Direction is
# deliberately not computed: either mismatch is unsafe to auto-restart, so one rule
# covers both.
#
# Pure: no docker, no psql, no globals -- every input is a parameter, which is what
# makes this directly testable (backend/tests/unit/test_restore_restart_decision.py)
# without spinning up Postgres.
#
# $1 dump_head        the restored backup's own alembic head (may be empty)
# $2 current_head      the live database's alembic head, read BEFORE the restore touched
#                       anything. Because the backend migrates on startup, this is a
#                       faithful proxy for "what head does the currently-running image
#                       expect" -- no container introspection needed. Empty or the
#                       literal string "unknown" (opentr.sh's placeholder when the read
#                       itself failed, or the #599 two-row corruption shape) both fail
#                       closed.
# $3 migrate_forward   "true" if the operator passed --migrate-forward (explicitly
#                       accepts the running image migrating this backup forward)
# $4 no_restart        "true" if the operator passed --no-restart (never restart,
#                       regardless of the head comparison)
#
# Echoes exactly one of: restart | hold:no-restart | hold:schema-mismatch
# Returns 1 (nothing echoed) if migrate_forward and no_restart are both "true" -- the
# caller's arg parser should already refuse that combination before this is ever
# called, but the function stays correct if called directly.
pg_restore_restart_decision() {
  local dump_head="${1:-}"
  local current_head="${2:-}"
  local migrate_forward="${3:-false}"
  local no_restart="${4:-false}"

  if [ "$migrate_forward" = true ] && [ "$no_restart" = true ]; then
    return 1
  fi

  if [ "$no_restart" = true ]; then
    echo "hold:no-restart"
    return 0
  fi

  local relationship="different"
  if [ -n "$dump_head" ] && [ -n "$current_head" ] && [ "$current_head" != "unknown" ] \
      && [ "$dump_head" = "$current_head" ]; then
    relationship="same"
  fi

  if [ "$relationship" = "same" ] || [ "$migrate_forward" = true ]; then
    echo "restart"
    return 0
  fi

  echo "hold:schema-mismatch"
}

#######################
# BACKUP / RESTORE (issue #613)
#######################
#
# Moved here from opentr.sh so both front ends — opentr.sh (a git checkout) and
# opentranscribe.sh (the shipped production entry point, release-manifest.txt:59) — share
# exactly one implementation of the DROP DATABASE restore path. scripts/common.sh already
# ships to every production install (release-manifest.txt:52, exec) and already holds the
# whole #599/#600/#610 restore-safety core these two functions call into.
#
# Every function below takes two new LEADING parameters, on top of the primitives' existing
# "first argument is an exec prefix" contract:
#
#   $1 compose_files  the `-f` chain to address the running stack, unquoted and
#                      word-split at every `docker compose $compose_files ...` call site
#                      (# shellcheck disable=SC2086, the same idiom opentranscribe.sh already
#                      uses at its own compose call sites). "" for opentr.sh — a repo clone
#                      auto-loads docker-compose.override.yml, which supplies image:/build:
#                      for every application service, so bare `docker compose` is
#                      byte-identical to what opentr.sh ran before this move.
#                      "$(get_compose_files)" for opentranscribe.sh. MEASURED (issue #613):
#                      the base compose file ALONE is not a valid compose project — it
#                      declares image: for only postgres/minio/redis/opensearch, and neither
#                      image: nor build: for backend, the nine celery-*, frontend, flower or
#                      docs — so `ps`, `exec` and `stop` (the three verbs restore needs) all
#                      fail before touching a container without a real chain.
#   $2 frontend_cmd   "./opentr.sh" | "./opentranscribe.sh" — used ONLY in operator-facing
#                      next-step messages, so a production install is never told to run a
#                      script it does not have.
#
# Copying this logic into opentranscribe.sh instead was rejected: two divergent
# implementations of a destructive DB path, and every existing static detector
# (test_opentr_restore_safety.py, test_backup_restore_format_contract.py) keys on
# extract_function(source, "restore_database") — a copy would be covered by none of them.

# Function to backup the database
# Usage: backup_database COMPOSE_FILES FRONTEND_CMD [--encrypt]
#   --encrypt: pipe pg_dump straight into gpg (AES-256, passphrase prompt) so the
#              plaintext dump never touches disk. Backups contain every user's
#              transcripts - encrypt anything that leaves this machine.
backup_database() {
  local compose_files="$1"; shift
  local frontend_cmd="$1"; shift

  ENCRYPT_BACKUP=false
  if [[ "$1" == "--encrypt" ]]; then
    ENCRYPT_BACKUP=true
    if ! command -v gpg &> /dev/null; then
      echo "❌ Error: gpg is required for encrypted backups (e.g. 'apt install gnupg')."
      exit 1
    fi
  elif [[ -n "$1" ]]; then
    echo "❌ Error: unknown backup option: $1"
    echo "Usage: $frontend_cmd backup [--encrypt]"
    exit 1
  fi

  TIMESTAMP=$(date +%Y%m%d_%H%M%S)
  BACKUP_FILE="opentranscribe_backup_${TIMESTAMP}.sql"
  mkdir -p ./backups

  # Respect a non-default DB name/user rather than hardcoding `postgres opentranscribe`
  # (issue #599 correction 7) — harmless for backup on its own, but restore's DROP
  # DATABASE must target the same names or it silently touches the wrong database.
  local db_user="${POSTGRES_USER:-postgres}"
  local db_name="${POSTGRES_DB:-opentranscribe}"

  if [[ "$ENCRYPT_BACKUP" == true ]]; then
    echo "📦 Creating encrypted database backup: ${BACKUP_FILE}.gpg..."
    # Subshell with pipefail so a pg_dump failure isn't masked by gpg succeeding
    # shellcheck disable=SC2086
    if (set -o pipefail; docker compose $compose_files exec -T postgres pg_dump -U "$db_user" "$db_name" \
        | gpg --symmetric --cipher-algo AES256 --output "./backups/${BACKUP_FILE}.gpg"); then
      echo "✅ Encrypted backup created successfully: ./backups/${BACKUP_FILE}.gpg"
      echo "   Restore with: $frontend_cmd restore ./backups/${BACKUP_FILE}.gpg"
    else
      rm -f "./backups/${BACKUP_FILE}.gpg"
      echo "❌ Backup failed."
      exit 1
    fi
  else
    echo "📦 Creating database backup: ${BACKUP_FILE}..."
    # shellcheck disable=SC2086
    if docker compose $compose_files exec -T postgres pg_dump -U "$db_user" "$db_name" > "./backups/${BACKUP_FILE}"; then
      echo "✅ Backup created successfully: ./backups/${BACKUP_FILE}"
      echo "ℹ️  Tip: backups contain all user transcripts in plaintext - use '$frontend_cmd backup --encrypt' for off-box storage."
    else
      echo "❌ Backup failed."
      exit 1
    fi
  fi
}

# Function to restore database from backup (issue #599)
#
# A plain `pg_dump` file carries no DROP/--clean statements, so replaying it into an
# already-populated database makes every statement fail — and without ON_ERROR_STOP,
# psql exits 0 anyway and prints "success" while nothing changed. Worse, the dump's
# alembic_version INSERT does not collide on primary key with a drifted row already
# present, so it succeeds while every data-table COPY fails — leaving TWO rows in
# alembic_version, which Alembic can no longer migrate from. This restores by
# guaranteeing an empty target (DROP DATABASE ... WITH (FORCE) + CREATE), replaying
# inside a single transaction (so a mid-dump failure rolls back to nothing rather
# than a hybrid schema), and verifying the result before ever reporting success.
#
# Usage: restore_database COMPOSE_FILES FRONTEND_CMD [--yes|-y] [--no-safety-dump] [--from-s3] \
#                          [--migrate-forward|--no-restart] <file-or-object-name>
restore_database() {
  local compose_files="$1"; shift
  local frontend_cmd="$1"; shift

  # Issue #613: backup_database always created ./backups; restore_database never did,
  # even though it writes there twice below (the GPG temp file, the safety dump). The
  # realistic DR scenario — copy a dump onto a fresh install, restore before ever
  # running a backup — used to fail closed here with a message blaming pg_dump instead
  # of a missing directory.
  mkdir -p ./backups

  local skip_confirm=false
  local skip_safety_dump=false
  local from_s3=false
  local migrate_forward=false
  local no_restart=false
  local backup_file=""

  while [ $# -gt 0 ]; do
    case "$1" in
      --yes|-y)
        skip_confirm=true
        shift
        ;;
      --no-safety-dump)
        skip_safety_dump=true
        shift
        ;;
      --from-s3)
        from_s3=true
        shift
        ;;
      --migrate-forward)
        # The operator explicitly accepts that the running (currently newer, in a
        # rollback scenario) image will migrate the restored backup forward. This is
        # the "recover data, stay on the current app version" intent (issue #610) —
        # NOT what a version rollback wants.
        migrate_forward=true
        shift
        ;;
      --no-restart)
        # Never restart the app services after this restore, regardless of whether the
        # backup's schema head matches the live one. Escape hatch for scripted/orchestrated
        # callers that manage the restart themselves (issue #610).
        no_restart=true
        shift
        ;;
      -*)
        echo "❌ Error: unknown restore option: $1"
        echo "Usage: $frontend_cmd restore [--yes] [--no-safety-dump] [--from-s3] [--migrate-forward|--no-restart] <backup_file>"
        exit 1
        ;;
      *)
        backup_file="$1"
        shift
        ;;
    esac
  done

  if [ "$migrate_forward" = true ] && [ "$no_restart" = true ]; then
    echo "❌ Error: --migrate-forward and --no-restart are mutually exclusive."
    exit 1
  fi

  if [ -z "$backup_file" ]; then
    echo "❌ Error: Backup file not specified."
    echo "Usage: $frontend_cmd restore [--yes] [--no-safety-dump] [--from-s3] [--migrate-forward|--no-restart] <backup_file>"
    exit 1
  fi

  # --from-s3: fetch the artifact out of the configured S3 destination FIRST — before
  # anything below touches the database. The S3 credentials are AES-256-GCM-encrypted in
  # SystemSettings, decryptable only inside the backend container (issue #600) — a host
  # shell script cannot reach them, and the database holding them is exactly what a
  # restore is about to drop. Fail closed, naming the manual two-step, if the backend
  # isn't up to do that decryption.
  if [ "$from_s3" = true ]; then
    # shellcheck disable=SC2086
    if ! docker compose $compose_files ps backend 2>/dev/null | grep -q "Up"; then
      echo "❌ Error: --from-s3 needs the backend container running (it holds the only"
      echo "   decryption key for the S3 credentials). Start it with '$frontend_cmd start dev'"
      echo "   (or 'start prod'), or fetch manually:"
      echo "     docker compose exec -T backend python -m app.scripts.fetch_backup $backup_file"
      echo "     $frontend_cmd restore --yes \"\${BACKUP_HOST_PATH:-./backups}/$backup_file\""
      exit 1
    fi
    local s3_name="$backup_file"
    echo "☁️  Fetching $s3_name from the configured S3 backup destination (before anything destructive)..."
    # shellcheck disable=SC2086
    if ! docker compose $compose_files exec -T backend python -m app.scripts.fetch_backup "$s3_name"; then
      echo "❌ Error: fetch of $s3_name failed — see the error above. Nothing was touched."
      exit 1
    fi
    # fetch_backup.py writes into cfg["destination"], which docker-compose.backup.yml
    # mounts at /backups <- BACKUP_HOST_PATH on the host — the same default the
    # --with-backup overlay documents. If backup.destination was reconfigured in the
    # admin UI to something other than the default /backups, this guess can miss; the
    # file-exists check right below reports that clearly rather than silently guessing wrong.
    backup_file="${BACKUP_HOST_PATH:-./backups}/$(basename "$s3_name")"
    echo "✅ Fetched to $backup_file"
  fi

  if [ ! -f "$backup_file" ]; then
    echo "❌ Error: Backup file not found: $backup_file"
    exit 1
  fi

  # Transparently decrypt GPG-encrypted backups (created with 'backup --encrypt')
  local restore_source="$backup_file"
  local temp_sql=""
  case "$backup_file" in
    *.gpg|*.asc)
      if ! command -v gpg &> /dev/null; then
        echo "❌ Error: gpg is required to restore encrypted backups (e.g. 'apt install gnupg')."
        exit 1
      fi
      echo "🔓 Decrypting backup..."
      temp_sql=$(mktemp ./backups/.restore_XXXXXX)
      if ! gpg --yes --output "$temp_sql" --decrypt "$backup_file"; then
        rm -f "$temp_sql"
        echo "❌ Decryption failed."
        exit 1
      fi
      restore_source="$temp_sql"
      ;;
  esac

  local db_user="${POSTGRES_USER:-postgres}"
  local db_name="${POSTGRES_DB:-opentranscribe}"
  # shellcheck disable=SC2086
  local exec_prefix="docker compose $compose_files exec -T postgres"

  # A custom-format (`pg_dump --format=custom`, i.e. `-Fc` — what the scheduled/S3 backup
  # feature produces, backup_service.run_pg_dump) or directory-format dump starts with the
  # "PGDMP" magic bytes. Dispatch the whole rest of this function on that: everything below
  # — confirmation, safety dump, drop/recreate, service stop/start — is shared; only the
  # expected-head read, the replay, and the verify step differ (issue #600).
  local dump_format="plain"
  if [ "$(head -c 5 "$restore_source" 2>/dev/null)" = "PGDMP" ]; then
    dump_format="custom"
  fi

  # Read the dump's own alembic head BEFORE anything destructive so it survives the
  # drop — the verify step needs the file, but the confirmation prompt below wants to
  # show it to the operator too.
  local expected_head
  if [ "$dump_format" = "custom" ]; then
    expected_head="$(pg_custom_dump_expected_head "$exec_prefix" "$db_user" "$restore_source")"
  else
    expected_head="$(awk '/^COPY public\.alembic_version /{getline; print; exit}' "$restore_source")"
  fi

  # Read the LIVE database's alembic head now — before anything is stopped or dropped,
  # and UNCONDITIONALLY (not just on the interactive confirm path below, which --yes
  # skips entirely — --yes is exactly the path issue #610 found unguarded). Because the
  # backend runs `alembic upgrade head` on every startup (app/main.py lifespan ->
  # app/db/migrations.py:run_migrations), this value is a faithful proxy for "what head
  # does the currently-running image expect".
  local current_head
  current_head="$($exec_prefix psql -tA -U "$db_user" "$db_name" -c "SELECT version_num FROM alembic_version;" 2>/dev/null | tr -d '[:space:]')"
  current_head="${current_head:-unknown}"

  # Which application image will serve this database after the restore, and does the
  # schema it is about to see match what that image expects? Both heads are already in
  # hand and both reads happen BEFORE anything is stopped or dropped (issue #610).
  #
  # Restarting the CURRENTLY-RUNNING image over a database restored to a DIFFERENT head
  # silently re-migrates the backup forward — destroying the very state the restore
  # recovered. "unknown" (either head unreadable, or the #599 two-row corruption shape,
  # which `tr -d '[:space:]'` concatenates into a string matching nothing) is treated as
  # not "same", i.e. fail closed. This is display-only; the actual restart decision
  # below is made by the shared, independently-tested pg_restore_restart_decision.
  local schema_relationship="unknown"
  if [ -n "$expected_head" ] && [ -n "$current_head" ] && [ "$current_head" != "unknown" ]; then
    if [ "$expected_head" = "$current_head" ]; then
      schema_relationship="same"
    else
      schema_relationship="different"
    fi
  fi

  echo "🔄 Restoring database from ${backup_file}..."

  if [ "$skip_confirm" != true ]; then
    if [ ! -t 0 ]; then
      echo "❌ Refusing to restore without confirmation on a non-interactive terminal."
      echo "   Re-run with --yes to confirm non-interactively: $frontend_cmd restore --yes $backup_file"
      [ -n "$temp_sql" ] && rm -f "$temp_sql"
      exit 4
    fi

    local current_media current_segments current_users backup_mtime
    current_media="$($exec_prefix psql -tA -U "$db_user" "$db_name" -c "SELECT count(*) FROM media_file;" 2>/dev/null | tr -d '[:space:]')"
    current_media="${current_media:-unknown}"
    current_segments="$($exec_prefix psql -tA -U "$db_user" "$db_name" -c "SELECT count(*) FROM transcript_segment;" 2>/dev/null | tr -d '[:space:]')"
    current_segments="${current_segments:-unknown}"
    current_users="$($exec_prefix psql -tA -U "$db_user" "$db_name" -c 'SELECT count(*) FROM "user";' 2>/dev/null | tr -d '[:space:]')"
    current_users="${current_users:-unknown}"
    backup_mtime="$(stat -c '%y' "$backup_file" 2>/dev/null || stat -f '%Sm' "$backup_file" 2>/dev/null || echo "unknown")"

    echo ""
    echo "⚠️  About to REPLACE database \"$db_name\" from backup file dated: $backup_mtime"
    echo ""
    echo "   Current database:"
    echo "     - alembic head:        $current_head"
    echo "     - media_file rows:      $current_media"
    echo "     - transcript_segment rows: $current_segments"
    echo "     - user rows:            $current_users"
    echo "   Backup file:"
    echo "     - alembic head:        ${expected_head:-unknown}"
    echo ""
    if [ "$schema_relationship" != "same" ]; then
      echo "   ⚠️  The backup's schema head differs from the running application's."
      echo "       Restoring it and restarting the CURRENT image would run every migration"
      echo "       between them, silently rolling this backup FORWARD (issue #610)."
      echo "       Application services will be left STOPPED so you can choose the image first."
      echo "       Pass --migrate-forward if you WANT the current version to migrate this backup."
      echo ""
    fi
    echo "   This DROPS the current database and recreates it from the backup."
    echo "   Everything written since that backup is destroyed."
    if [ "$skip_safety_dump" != true ]; then
      echo "   A safety dump of the CURRENT database will be written first, to ./backups/pre-restore_<timestamp>.sql"
    else
      echo "   ⚠️  --no-safety-dump: no safety dump will be taken. The current database is NOT recoverable after this."
    fi
    echo ""
    echo "   ⚠️  MinIO (media files) and OpenSearch (search indices) are NOT rolled back with the"
    echo "   database — restoring only PostgreSQL creates a time skew (media with no row, rows with"
    echo "   no media, stale search hits). Reindex from Admin → Search afterwards."
    echo ""
    printf '   Type the database name ("%s") to confirm, anything else cancels: ' "$db_name"
    local confirm_name
    read -r confirm_name
    if [ "$confirm_name" != "$db_name" ]; then
      echo "❌ Restore cancelled."
      [ -n "$temp_sql" ] && rm -f "$temp_sql"
      exit 4
    fi
  fi

  # Serialise the destructive portion of a restore. Two concurrent restore_database
  # calls would both have already read current_head and expected_head above, both take
  # their own safety dump, and the second one's DROP DATABASE ... WITH (FORCE) can kill
  # the first's replay mid-transaction -- the same class of hazard
  # run-mutation-tests.sh --verify's per-module flock exists for, and the same fix:
  # non-blocking acquire, then a trap that releases on INT/TERM/ERR/EXIT so every one of
  # this function's many `exit N` failure branches below still releases the lock (a
  # `return`-based cleanup would not fire for those -- they end the process, not just
  # this function). ./backups already exists (mkdir -p above), so the lock file lives
  # there rather than needing its own directory.
  # Save any trap a CALLER already had installed for these signals before this function
  # installs its own below -- the unconditional `trap - INT TERM ERR EXIT` on the success
  # path used to reset all four straight to the shell default, silently discarding
  # whatever the caller had, not just this function's own lock-release trap.
  local prev_exit_trap prev_int_trap prev_term_trap prev_err_trap
  prev_exit_trap="$(trap -p EXIT)"
  prev_int_trap="$(trap -p INT)"
  prev_term_trap="$(trap -p TERM)"
  prev_err_trap="$(trap -p ERR)"

  local restore_lock_file="./backups/.restore.lock"
  # macOS ships no util-linux `flock`. Without this check the `if ! flock -n …` below sees
  # exit 127 (command not found) and takes the "another restore is already in progress"
  # branch, so EVERY restore aborted with a message naming a cause that did not exist.
  # Fails closed, but confusingly — and on a platform where it fails ALWAYS.
  local restore_lock_fd=""
  if command -v flock >/dev/null 2>&1; then
    exec {restore_lock_fd}>"$restore_lock_file"
    if ! flock -n "$restore_lock_fd"; then
      echo "❌ Error: another restore is already in progress (lock: $restore_lock_file)."
      echo "   Wait for it to finish, then retry. The database was NOT touched."
      exec {restore_lock_fd}>&-
      [ -n "$temp_sql" ] && rm -f "$temp_sql"
      exit 3
    fi
    # shellcheck disable=SC2064  # expand $restore_lock_fd NOW (a literal fd number), not
    # when the trap fires -- matches run-mutation-tests.sh's verify_survivor trap.
    trap "flock -u $restore_lock_fd 2>/dev/null; exec $restore_lock_fd>&- 2>/dev/null" INT TERM ERR EXIT
  else
    echo "⚠️  flock is unavailable on this host — concurrent-restore serialisation is DISABLED."
    echo "   Do not run two restores at the same time; the second's DROP DATABASE can kill"
    echo "   the first's replay mid-transaction."
  fi

  # Stop services that use the database. Checked, like every other destructive step in
  # this function (the safety dump and the drop/recreate right below both are) -- an
  # unchecked stop here would let the DROP DATABASE that follows proceed while
  # backend/celery are still connected to the database being dropped, e.g. on a
  # version-skewed install whose compose file is missing one of these service names.
  # shellcheck disable=SC2086
  if ! docker compose $compose_files stop backend celery-worker celery-download-worker celery-cpu-worker celery-redaction celery-cloud-asr-worker celery-nlp-worker celery-embedding-worker celery-beat; then
    echo "❌ Could not stop application services — refusing to proceed with restore."
    echo "   The database was NOT touched."
    [ -n "$temp_sql" ] && rm -f "$temp_sql"
    exit 1
  fi

  # Unquoted on purpose: $compose_files is a pre-split "-f a.yml -f b.yml" chain, the same
  # word-splitting idiom every other compose call site in this file relies on.
  # shellcheck disable=SC2206
  local restart_services=(docker compose $compose_files start backend celery-worker celery-download-worker celery-cpu-worker celery-redaction celery-cloud-asr-worker celery-nlp-worker celery-embedding-worker celery-beat)

  # Pre-restore safety dump — fail CLOSED: if we can't dump the current database, we
  # refuse to drop it. This is what makes the destructive step below reversible.
  local safety_dump_file=""
  if [ "$skip_safety_dump" != true ]; then
    safety_dump_file="./backups/pre-restore_$(date +%Y%m%d_%H%M%S).sql"
    echo "🛡️  Taking a safety dump of the current database: $safety_dump_file"
    if ! $exec_prefix pg_dump -U "$db_user" "$db_name" > "$safety_dump_file"; then
      rm -f "$safety_dump_file"
      echo "❌ Could not dump the current database — refusing to drop it."
      "${restart_services[@]}"
      [ -n "$temp_sql" ] && rm -f "$temp_sql"
      exit 1
    fi
  fi

  echo "🗑️  Dropping and recreating database \"$db_name\"..."
  if ! pg_drop_and_recreate_database "$exec_prefix" "$db_user" "$db_name"; then
    echo "❌ Could not drop/recreate the database."
    [ -n "$safety_dump_file" ] && echo "   The current database's safety dump is at: $safety_dump_file"
    "${restart_services[@]}"
    [ -n "$temp_sql" ] && rm -f "$temp_sql"
    exit 1
  fi

  echo "📥 Replaying backup into \"$db_name\"..."
  local replay_ok=true
  if [ "$dump_format" = "custom" ]; then
    pg_replay_custom_dump "$exec_prefix" "$db_user" "$db_name" "$restore_source" || replay_ok=false
  else
    pg_replay_dump "$exec_prefix" "$db_user" "$db_name" "$restore_source" || replay_ok=false
  fi
  if [ "$replay_ok" != true ]; then
    echo "❌ Database restore failed — the database is now empty (the failed replay rolled back)."
    if [ -n "$safety_dump_file" ]; then
      echo "   Recover the previous state with:"
      echo "     $frontend_cmd restore --yes $safety_dump_file"
    else
      echo "   No safety dump was taken (--no-safety-dump) — the previous state is not recoverable here."
    fi
    # Deliberately NOT restarting (issue #610): the database is now EMPTY, so restarting
    # the newer backend would take run_migrations()'s "empty database" branch and seed a
    # fresh admin — turning a failed restore into a silently brand-new empty deployment.
    echo "⏸️  Services left stopped — recover with the command above before starting the app."
    [ -n "$temp_sql" ] && rm -f "$temp_sql"
    exit 1
  fi

  echo "🔍 Verifying restore..."
  local verify_ok=true
  if [ "$dump_format" = "custom" ]; then
    pg_verify_custom_restore "$exec_prefix" "$db_user" "$db_name" "$restore_source" || verify_ok=false
  else
    pg_verify_restore "$exec_prefix" "$db_user" "$db_name" "$restore_source" || verify_ok=false
  fi
  if [ "$verify_ok" != true ]; then
    echo "❌ Restore verification failed — the database does not match the backup. See the mismatch(es) above."
    [ -n "$safety_dump_file" ] && echo "   The current (unverified) database's pre-restore safety dump is at: $safety_dump_file"
    # Deliberately NOT restarting (issue #610) — same hazard family as the replay-failure
    # path above: the database is in an unverified state, not the one the running image
    # expects to see.
    echo "⏸️  Services left stopped — recover with the command above before starting the app."
    [ -n "$temp_sql" ] && rm -f "$temp_sql"
    exit 1
  fi

  [ -n "$temp_sql" ] && rm -f "$temp_sql"

  # Decide whether it is safe to bring the app back up automatically (issue #610). The
  # decision is made by the shared, independently-tested pg_restore_restart_decision
  # (scripts/common.sh) — never reimplemented here — so both front ends and their test
  # suites share exactly one definition of "safe to restart".
  local restart_decision
  restart_decision="$(pg_restore_restart_decision "$expected_head" "$current_head" "$migrate_forward" "$no_restart")"

  local hold_reason=""
  case "$restart_decision" in
    hold:no-restart)
      hold_reason="--no-restart"
      ;;
    hold:schema-mismatch)
      hold_reason="schema-head mismatch (backup=${expected_head:-unknown}, was-running=${current_head:-unknown})"
      ;;
  esac

  if [ -n "$hold_reason" ]; then
    echo "✅ Database restored successfully."
    echo ""
    echo "⏸️  Application services were left STOPPED on purpose: $hold_reason"
    echo "   Starting the previously-running image now would migrate this backup forward."
    echo ""
    echo "   Choose one:"
    echo "   • Roll the application back to the version that matches this backup:"
    echo "       ./opentranscribe.sh update --rollback      # or: update --version vX.Y.Z"
    echo "   • Keep the current version and let it migrate this backup forward (intended"
    echo "     for data recovery, NOT for a version rollback):"
    echo "       $frontend_cmd start dev                      # or: docker compose start backend …"
  else
    echo "🔄 Restarting services..."
    "${restart_services[@]}"
    echo "✅ Database restored successfully."
  fi
  echo "ℹ️  MinIO and OpenSearch were NOT rolled back — reindex from Admin → Search if the"
  echo "   restored database's file list differs from what MinIO/OpenSearch currently have."
  if [ -n "$safety_dump_file" ]; then
    echo "   Pre-restore safety dump of the database this replaced: $safety_dump_file"
  fi

  # Release the destructive-portion lock explicitly on this, the one path through the
  # function that returns instead of exiting (every failure branch above calls `exit N`,
  # which the INT/TERM/ERR/EXIT trap set above already covers). Guarded: restore_lock_fd
  # is empty when flock was unavailable (no lock was ever taken, nothing to release).
  # Also closes the fd here rather than leaving it open until the whole process exits --
  # the trap below only unregisters ITSELF, it does not run, so nothing else would.
  if [ -n "$restore_lock_fd" ]; then
    flock -u "$restore_lock_fd" 2>/dev/null
    exec {restore_lock_fd}>&- 2>/dev/null
  fi
  trap - INT TERM ERR EXIT
  # Restore whatever the caller had before this function ran, rather than leaving all
  # four signals with no trap at all.
  [ -n "$prev_exit_trap" ] && eval "$prev_exit_trap"
  [ -n "$prev_int_trap" ] && eval "$prev_int_trap"
  [ -n "$prev_term_trap" ] && eval "$prev_term_trap"
  [ -n "$prev_err_trap" ] && eval "$prev_err_trap"

  # Explicit, not left to the truthiness of the `echo` above: `[ -n "$safety_dump_file" ]`
  # is false (exit 1) whenever --no-safety-dump was used, and this was the LAST statement
  # in the function -- so its exit status silently became restore_database's own return
  # code, even on a completely successful restore. opentranscribe.sh's caller does
  # `rc=$?; exit $rc` right after this call, so `restore --yes --no-safety-dump <file>`
  # exited 1 after a clean restore, purely from that unrelated `[ -n ... ]` check.
  return 0
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
