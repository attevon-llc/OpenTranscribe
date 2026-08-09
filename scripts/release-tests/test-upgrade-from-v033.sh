#!/bin/bash
# Scenario B — v0.3.3 → 0.4.0 (or current head) in-place upgrade.
#
# What this proves:
#   A real user with real data on the previous release can run the documented
#   upgrade path (`./opentranscribe.sh update` ≈ `compose down/pull/up`) and
#   find their MinIO objects, transcripts, speakers, and search indices intact
#   after the migration chain runs.
#
# Phases:
#   00 preflight + secrets gate
#   01 build local 0.4.0 images (skipped if already present from Scenario A)
#   02 verify Docker Hub has :v0.3.3 tags (FROM_VERSION)
#   03 create v0.3.3 worktree, copy compose into TEST_ROOT, patch
#   04 generate isolated .env, start the v0.3.3 stack, wait for health
#   05 register user, upload media via URL, wait for completion
#   06 snapshot pre-upgrade state (postgres SELECTs, MinIO ETags, transcripts)
#   07 down v0.3.3, swap compose to current head, re-patch, point to local images
#   08 up the upgraded stack, wait for migrations + health
#   09 snapshot post-upgrade state
#   10 diff snapshots, run feature liveness checks, write REPORT.md
#
# Future releases: set FROM_VERSION=v0.4.0 etc. — every other piece is
# parameterised. Append a row to expected-schemas.tsv when adding a release.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ─── Tunables ───────────────────────────────────────────────────────────────
TEST_SCENARIO="upgrade"
# Label namespace (used for cleanup). The actual COMPOSE_PROJECT_NAME used by
# docker compose stays at its default ('opentranscribe') so this scenario
# exercises stock container, network, and volume names — same as a real user.
TEST_PROJECT_NAME="${TEST_PROJECT_NAME:-ot-reltest-upgrade}"
TEST_ROOT="${TEST_ROOT:-/mnt/nvm/opentranscribe-test-runs/${TEST_PROJECT_NAME}-$(date +%Y%m%d-%H%M%S)}"
TEST_LABEL="com.opentranscribe.release-test=${TEST_SCENARIO}"

FROM_VERSION="${FROM_VERSION:-v0.3.3}"
LOCAL_IMAGE_TAG="${LOCAL_IMAGE_TAG:-0.4.0}"

# GPU policy: default to GPU 1 (RTX 3080 Ti, free on this host).
TEST_USE_GPU="${TEST_USE_GPU:-true}"
TEST_GPU_DEVICE_ID="${TEST_GPU_DEVICE_ID:-1}"
export TEST_USE_GPU TEST_GPU_DEVICE_ID

# Use the one-liner's default ports (5173-5180) since the live deployment is
# stopped and Scenario A's containers will be torn down before this scenario
# starts. The compose project name 'opentranscribe' (the one-liner default)
# isolates this scenario's named volumes from any other run.
TEST_FRONTEND_PORT="${FRONTEND_PORT:-5173}"
TEST_BACKEND_PORT="${BACKEND_PORT:-5174}"
TEST_FLOWER_PORT="${FLOWER_PORT:-5175}"
TEST_POSTGRES_PORT="${POSTGRES_PORT:-5176}"
TEST_REDIS_PORT="${REDIS_PORT:-5177}"
TEST_MINIO_PORT="${MINIO_PORT:-5178}"
TEST_MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-5179}"
TEST_OPENSEARCH_PORT="${OPENSEARCH_PORT:-5180}"
TEST_PORTS="$TEST_FRONTEND_PORT $TEST_BACKEND_PORT $TEST_FLOWER_PORT $TEST_POSTGRES_PORT $TEST_REDIS_PORT $TEST_MINIO_PORT $TEST_MINIO_CONSOLE_PORT $TEST_OPENSEARCH_PORT"

# Default admin user created by backend on first start (override if changed)
TEST_ADMIN_EMAIL="${TEST_ADMIN_EMAIL:-admin@example.com}"
TEST_ADMIN_PASSWORD="${TEST_ADMIN_PASSWORD:-password}"

# Test media: directory of small real media files (mp3/m4a/wav/mp4) to upload.
TEST_MEDIA_DIR="${TEST_MEDIA_DIR:-/mnt/nvm/opentranscribe-test-runs/test-media}"

DO_CLEANUP=0
DO_FORCE=0
while (( $# > 0 )); do
    case "$1" in
        --cleanup) DO_CLEANUP=1 ;;
        --force)   DO_FORCE=1 ;;
        --yes)     export OT_RELEASE_TEST_YES=1 ;;
        --help|-h)
            cat <<EOF
Usage: $0 [--cleanup] [--force] [--yes]

Prerequisite: stop the live deployment first with \`./opentr.sh stop\`.
This scenario runs under the one-liner's stock container names and ports so it
exercises what a real user gets; it cannot run alongside the live stack.
After the test, restart it with \`./opentr.sh start dev\` (or whichever
mode you were using).

Env:
  TEST_PROJECT_NAME      default ot-reltest-upgrade  (used as label namespace)
  TEST_ROOT              default /mnt/nvm/opentranscribe-test-runs/<name>-<ts>
  FROM_VERSION           default v0.3.3   (Docker Hub tag for the "before" stack)
  LOCAL_IMAGE_TAG        default 0.4.0    (locally built tag for the "after" stack)
  FRONTEND_PORT..        default 5173-5180 (one-liner defaults; see README)
EOF
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

export TEST_SCENARIO TEST_PROJECT_NAME TEST_ROOT TEST_LABEL
export TEST_FRONTEND_PORT TEST_BACKEND_PORT TEST_FLOWER_PORT TEST_POSTGRES_PORT \
       TEST_REDIS_PORT TEST_MINIO_PORT TEST_MINIO_CONSOLE_PORT TEST_OPENSEARCH_PORT \
       TEST_PORTS

# shellcheck source=lib/guardrails.sh
source "$LIB_DIR/guardrails.sh"
# shellcheck source=lib/compose-patch.sh
source "$LIB_DIR/compose-patch.sh"
# shellcheck source=lib/env-template.sh
source "$LIB_DIR/env-template.sh"
# shellcheck source=lib/api-client.sh
source "$LIB_DIR/api-client.sh"
# shellcheck source=lib/assertions.sh
source "$LIB_DIR/assertions.sh"

if (( DO_CLEANUP == 1 )); then
    gr_log "cleanup requested"
    gr_cleanup
    exit 0
fi

PHASE_DIR="$TEST_ROOT/.phase"
phase_done()  { mkdir -p "$PHASE_DIR"; touch "$PHASE_DIR/$1.done"; }
phase_check() { [[ -f "$PHASE_DIR/$1.done" && $DO_FORCE -eq 0 ]]; }
phase()       { local n="$1"; shift
                if phase_check "$n"; then
                    echo -e "\033[0;33m[skip]\033[0m phase $n already complete"
                    return
                fi
                echo -e "\n\033[1;34m═══ phase $n ═══\033[0m"
                "$@"
                phase_done "$n"
              }

ensure_secrets_file() {
    local f="$SCRIPT_DIR/.env.test-secrets"
    if [[ ! -f "$f" ]]; then
        gr_die "missing $f — run test-fresh-install.sh first to bootstrap, or copy from .env.test-secrets.example"
    fi
    # shellcheck disable=SC1090
    source "$f"
    [[ -n "${HUGGINGFACE_TOKEN:-}" ]] || gr_die "HUGGINGFACE_TOKEN missing in $f"
    export HUGGINGFACE_TOKEN
}

# ─── Phase implementations ──────────────────────────────────────────────────

ensure_clean_test_state() {
    # Refuse if any live opentranscribe-* container is currently running.
    local running
    running=$(docker ps --format '{{.Names}}' --filter 'name=^opentranscribe-' || true)
    if [[ -n "$running" ]]; then
        gr_die "live opentranscribe-* containers are running:
$running

Stop them with: ./opentr.sh stop  (preserves all data)"
    fi
    # Remove stopped opentranscribe-* containers (would collide on container_name)
    local stopped
    stopped=$(docker ps -a --format '{{.Names}}' --filter 'name=^opentranscribe-' || true)
    if [[ -n "$stopped" ]]; then
        gr_log "removing stopped opentranscribe-* containers from previous runs"
        docker rm $stopped >/dev/null 2>&1 || true
    fi
    # Remove any leftover opentranscribe_* named volumes from previous test
    # runs. (Production volumes are namespaced under transcribe-app_* and are
    # never touched.)
    local stale_vols
    stale_vols=$(docker volume ls --format '{{.Name}}' | grep "^opentranscribe_" || true)
    if [[ -n "$stale_vols" ]]; then
        gr_log "removing stale opentranscribe_* volumes from previous runs:"
        echo "$stale_vols" | sed 's/^/  /' >&2
        for vol in $stale_vols; do
            docker volume rm "$vol" >/dev/null 2>&1 \
                || gr_warn "could not remove volume $vol (may be in use)"
        done
    fi
    gr_ok "test state clean — no live containers, no stale volumes"
}

phase_00_preflight() {
    ensure_secrets_file
    gr_preflight
    ensure_clean_test_state
    # If a previous crashed run left the default bridge network in a stale
    # state, clear it now so we don't discover it mid-phase-07 when the only
    # available workaround would be a daemon restart (which real users
    # cannot perform). See _clean_stale_opentranscribe_network below.
    _clean_stale_opentranscribe_network
}

phase_01_build_local_images() {
    # Intentionally tag ONLY :${LOCAL_IMAGE_TAG}, never :latest — retagging
    # :latest locally would affect the live production deployment on this host
    # if its containers ever restart.
    if docker image inspect "davidamacey/opentranscribe-backend:${LOCAL_IMAGE_TAG}" >/dev/null 2>&1 \
       && docker image inspect "davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG}" >/dev/null 2>&1; then
        gr_ok "local ${LOCAL_IMAGE_TAG} images already built"
        return
    fi
    gr_log "building local ${LOCAL_IMAGE_TAG} images"
    docker build -t "davidamacey/opentranscribe-backend:${LOCAL_IMAGE_TAG}" \
        -f "$REPO_ROOT/backend/Dockerfile.prod" "$REPO_ROOT/backend"
    docker build -t "davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG}" \
        -f "$REPO_ROOT/frontend/Dockerfile.prod" "$REPO_ROOT/frontend"
}

phase_02_verify_from_version() {
    gr_log "verifying davidamacey/opentranscribe-*:${FROM_VERSION} exists on Docker Hub"
    if ! docker manifest inspect "davidamacey/opentranscribe-backend:${FROM_VERSION}" >/dev/null 2>&1; then
        gr_die "Docker Hub does not have davidamacey/opentranscribe-backend:${FROM_VERSION}; cannot run upgrade test from a non-existent release"
    fi
    if ! docker manifest inspect "davidamacey/opentranscribe-frontend:${FROM_VERSION}" >/dev/null 2>&1; then
        gr_die "Docker Hub does not have davidamacey/opentranscribe-frontend:${FROM_VERSION}"
    fi
    gr_ok "${FROM_VERSION} images present on Docker Hub"
}

phase_03_prepare_v033_compose() {
    local worktree="$TEST_ROOT/worktree-${FROM_VERSION}"
    if [[ ! -d "$worktree" ]]; then
        gr_log "creating git worktree at $worktree"
        git -C "$REPO_ROOT" worktree add --detach "$worktree" "$FROM_VERSION"
    fi
    local stage="$TEST_ROOT/before"
    mkdir -p "$stage"

    cp "$worktree/docker-compose.yml" "$stage/docker-compose.yml"
    [[ -f "$worktree/docker-compose.prod.yml" ]] || gr_die "v$FROM_VERSION worktree missing docker-compose.prod.yml"
    cp "$worktree/docker-compose.prod.yml" "$stage/docker-compose.prod.yml"

    # v0.3.3 mounts ./database/init_db.sql into postgres for first-boot
    # bootstrapping. (Newer releases use Alembic exclusively, but v0.3.3 still
    # needs this file.) Copy the entire database/ directory from the worktree.
    if [[ -d "$worktree/database" ]]; then
        rm -rf "$stage/database"
        cp -r "$worktree/database" "$stage/database"
        gr_ok "copied database/ from v0.3.3 worktree"
    fi

    # Inject the release-test label so cleanup can find managed resources.
    # No container/volume rename — we use the stock 'opentranscribe-*' names
    # that the live deployment also uses. The live deployment is stopped
    # before tests run, so there is no collision.
    cp_inject_labels "$stage/docker-compose.yml" "$TEST_LABEL"

    # Prod file: pin image tag to FROM_VERSION + pull always (exercises the
    # real Docker Hub pull path) + label injection.
    cp_inject_labels "$stage/docker-compose.prod.yml" "$TEST_LABEL"
    cp_force_pull_policy "$stage/docker-compose.prod.yml" always
    cp_pin_image_tag "$stage/docker-compose.prod.yml" backend "$FROM_VERSION"
    cp_pin_image_tag "$stage/docker-compose.prod.yml" frontend "$FROM_VERSION"
    for svc in celery-worker celery-cpu-worker celery-nlp-worker celery-embedding-worker celery-download-worker celery-redaction celery-cloud-asr-worker celery-beat flower; do
        cp_pin_image_tag "$stage/docker-compose.prod.yml" "$svc" "$FROM_VERSION" 2>/dev/null || true
    done

    # GPU overlay (use the v0.3.3 worktree's copy if present, else current head's)
    if [[ "$TEST_USE_GPU" == "true" ]]; then
        local src_gpu="$worktree/docker-compose.gpu.yml"
        [[ -f "$src_gpu" ]] || src_gpu="$REPO_ROOT/docker-compose.gpu.yml"
        cp "$src_gpu" "$stage/docker-compose.gpu.yml"
        gr_ok "GPU overlay copied from $(basename "$(dirname "$src_gpu")")"
    fi

    # Model cache strategy: use a PERSISTENT shared cache across test runs so
    # we don't re-download ~5GB of PyAnnote/WhisperX/sentence-transformers
    # models every time. HuggingFace 503s and rate limits have repeatedly
    # flaked tests; a persistent cache eliminates that entire failure surface.
    #
    # The shared cache lives outside any test-root so it survives --cleanup
    # tear-downs. If a pre-warmed live cache exists at the production path,
    # we rsync it in on first use (read-only source, no writes to live path).
    local shared_cache="/mnt/nvm/opentranscribe-test-runs/.shared-model-cache"
    local model_cache="$shared_cache"
    mkdir -p "$model_cache/huggingface" "$model_cache/torch" \
             "$model_cache/nltk_data" "$model_cache/sentence-transformers" \
             "$model_cache/opensearch-ml" "$model_cache/pyannote"

    # One-time seed from live production cache if we haven't already. Check
    # for the sentinel file ".seeded-from-live" to avoid re-copying on every
    # run. Uses rsync --link-dest for hardlinks so the copy is cheap (no
    # actual data duplication) if source and dest are on the same filesystem.
    if [[ ! -f "$model_cache/.seeded-from-live" ]]; then
        local live_cache="/mnt/nvm/repos/transcribe-app/models"
        if [[ -d "$live_cache/huggingface" ]]; then
            gr_log "seeding shared model cache from live cache (one-time)"
            # Copy only the subdirs that HF/PyAnnote/Whisper actually need.
            # Skip opensearch-ml (container-specific) and onnx (0.4.0-only).
            for sub in huggingface torch nltk_data sentence-transformers pyannote; do
                if [[ -d "$live_cache/$sub" ]]; then
                    rsync -a --link-dest="$live_cache/$sub/" \
                        "$live_cache/$sub/" "$model_cache/$sub/" 2>/dev/null || \
                        cp -rL "$live_cache/$sub/." "$model_cache/$sub/" 2>/dev/null || true
                fi
            done
            touch "$model_cache/.seeded-from-live"
            gr_ok "shared model cache seeded from live cache"
        else
            gr_warn "no live model cache to seed from; models will download from HF on first boot"
            touch "$model_cache/.seeded-from-live"  # mark as attempted
        fi
    else
        gr_ok "reusing persistent shared model cache at $shared_cache"
    fi

    docker run --rm -v "$model_cache:/models" busybox:latest \
        sh -c "chown -R 1000:1000 /models && chmod -R 755 /models" >/dev/null 2>&1 \
        || gr_warn "could not chown model cache (may need sudo)"

    # Generate a .env for the v0.3.3 stack with isolated credentials.
    cat > "$stage/.env" <<EOF
# Auto-generated by test-upgrade-from-v033.sh phase 3
COMPOSE_PROJECT_NAME=opentranscribe
# Pin model cache to an absolute path so the chown above takes effect
# (default is ./models relative to the compose file location).
MODEL_CACHE_DIR=$model_cache
FRONTEND_PORT=$TEST_FRONTEND_PORT
BACKEND_PORT=$TEST_BACKEND_PORT
FLOWER_PORT=$TEST_FLOWER_PORT
POSTGRES_PORT=$TEST_POSTGRES_PORT
REDIS_PORT=$TEST_REDIS_PORT
MINIO_PORT=$TEST_MINIO_PORT
MINIO_CONSOLE_PORT=$TEST_MINIO_CONSOLE_PORT
OPENSEARCH_PORT=$TEST_OPENSEARCH_PORT
POSTGRES_USER=postgres
POSTGRES_PASSWORD=$(openssl rand -hex 16)
POSTGRES_DB=opentranscribe
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=$(openssl rand -hex 16)
MINIO_BUCKET=opentranscribe
JWT_SECRET_KEY=$(openssl rand -hex 32)
ENCRYPTION_KEY=upgrade-test-$(openssl rand -hex 16)
HUGGINGFACE_TOKEN=${HUGGINGFACE_TOKEN:-}
WHISPER_MODEL=large-v3-turbo
MODEL_CACHE_DIR=$model_cache
GPU_DEVICE_ID=$TEST_GPU_DEVICE_ID
USE_GPU=true
COMPUTE_TYPE=float16
BATCH_SIZE=16
LLM_PROVIDER=
EOF
    chmod 600 "$stage/.env"
    gr_ok "v0.3.3 compose staged at $stage"
}

phase_04_start_v033() {
    local stage="$TEST_ROOT/before"
    pushd "$stage" >/dev/null
    local compose_args=(-f docker-compose.yml -f docker-compose.prod.yml)
    if [[ "$TEST_USE_GPU" == "true" && -f docker-compose.gpu.yml ]]; then
        compose_args+=(-f docker-compose.gpu.yml)
    fi
    gr_log "compose pull (Docker Hub: ${FROM_VERSION})"
    docker compose "${compose_args[@]}" pull
    gr_log "compose up -d"
    docker compose "${compose_args[@]}" up -d
    popd >/dev/null

    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    ac_wait_for_health 900
}

phase_05_seed_data() {
    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    # The backend creates a default admin (admin@example.com / password) on first
    # start, so registration is not needed.
    ac_login "$TEST_ADMIN_EMAIL" "$TEST_ADMIN_PASSWORD"

    [[ -d "$TEST_MEDIA_DIR" ]] || gr_die "TEST_MEDIA_DIR missing: $TEST_MEDIA_DIR"
    local media_files=()
    while IFS= read -r f; do
        media_files+=("$f")
    done < <(find "$TEST_MEDIA_DIR" -maxdepth 1 -type f \
                \( -iname "*.mp3" -o -iname "*.m4a" -o -iname "*.mp4" \
                   -o -iname "*.wav" -o -iname "*.flac" -o -iname "*.ogg" \) \
                -size -5M | head -2)
    (( ${#media_files[@]} > 0 )) || gr_die "no media files in $TEST_MEDIA_DIR (need 1-2 small audio/video files)"

    local file_ids=()
    for path in "${media_files[@]}"; do
        local fid
        fid=$(ac_upload_file "$path")
        gr_log "queued upload: $(basename "$path") -> uuid=$fid"
        file_ids+=("$fid")
    done

    for fid in "${file_ids[@]}"; do
        ac_wait_for_file_status "$fid" 1800
    done
    printf '%s\n' "${file_ids[@]}" > "$TEST_ROOT/seeded-file-ids.txt"
    gr_ok "seeded $(wc -l < "$TEST_ROOT/seeded-file-ids.txt") files"
}

snapshot_state() {
    local label="$1"   # "before" or "after"
    local out="$TEST_ROOT/snapshots/$label"
    mkdir -p "$out"

    # Ensure API_BASE is set even when resuming from a later phase that
    # didn't run phase_04/phase_08 (which would otherwise export it).
    API_BASE="${API_BASE:-http://localhost:${TEST_BACKEND_PORT}/api}"
    export API_BASE

    gr_log "snapshotting state to $out"

    # Postgres deterministic queries (the one-liner uses the stock
    # 'opentranscribe-postgres' container name and 'postgres' superuser).
    # All queries are tolerant of missing tables — v0.3.3 may not have
    # alembic_version (it bootstraps via init_db.sql) or some 0.4.0 tables.
    local pg="opentranscribe-postgres"
    docker exec "$pg" psql -U postgres -d opentranscribe -tAc \
        "SELECT version_num FROM alembic_version" > "$out/alembic_head.txt" 2>/dev/null \
        || echo "(alembic_version table absent — pre-Alembic schema)" > "$out/alembic_head.txt"
    docker exec "$pg" psql -U postgres -d opentranscribe -tAc \
        "SELECT id, filename, status FROM media_file ORDER BY id" > "$out/media_files.txt" 2>/dev/null \
        || echo "(media_file query failed)" > "$out/media_files.txt"
    # transcript_segment.file_id was renamed to media_file_id at some point;
    # try the new name first, fall back to the old name for v0.3.3.
    docker exec "$pg" psql -U postgres -d opentranscribe -tAc \
        "SELECT media_file_id, COUNT(*) FROM transcript_segment GROUP BY media_file_id ORDER BY media_file_id" > "$out/segment_counts.txt" 2>/dev/null \
        || docker exec "$pg" psql -U postgres -d opentranscribe -tAc \
            "SELECT file_id, COUNT(*) FROM transcript_segment GROUP BY file_id ORDER BY file_id" > "$out/segment_counts.txt" 2>/dev/null \
        || echo "(transcript_segment query failed)" > "$out/segment_counts.txt"
    docker exec "$pg" psql -U postgres -d opentranscribe -tAc \
        "SELECT id, name FROM speaker ORDER BY id" > "$out/speakers.txt" 2>/dev/null \
        || echo "(speaker table query failed — schema may differ)" > "$out/speakers.txt"

    # MinIO ETag list (proves no object body mutation)
    local minio="opentranscribe-minio"
    docker exec "$minio" sh -c '
        mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1 || true
        mc ls --recursive --json local/opentranscribe 2>/dev/null
    ' > "$out/minio_etags.json" 2>/dev/null || echo "[]" > "$out/minio_etags.json"

    # Transcript dump per file (for prefix comparison)
    if [[ -f "$TEST_ROOT/seeded-file-ids.txt" ]]; then
        while IFS= read -r fid; do
            ac_get_transcript "$fid" > "$out/transcript-$fid.json" 2>/dev/null || true
        done < "$TEST_ROOT/seeded-file-ids.txt"
    fi

    # File-level API listing
    ac_list_files > "$out/files.json" 2>/dev/null || true
}

phase_06_snapshot_pre() {
    snapshot_state before
}

phase_07_swap_to_new() {
    local stage_before="$TEST_ROOT/before"
    local stage_after="$TEST_ROOT/after"

    # IMPORTANT: keep the SAME named volumes so the upgrade is in-place against
    # the data the v0.3.3 stack populated. We do this by reusing the same
    # COMPOSE_PROJECT_NAME (default 'opentranscribe') across both stages.
    mkdir -p "$stage_after"
    cp "$REPO_ROOT/docker-compose.yml" "$stage_after/docker-compose.yml"
    [[ -f "$REPO_ROOT/docker-compose.prod.yml" ]] || gr_die "current head missing docker-compose.prod.yml"
    cp "$REPO_ROOT/docker-compose.prod.yml" "$stage_after/docker-compose.prod.yml"

    # 0.4.0 no longer needs the database/init_db.sql bind mount, but copy it
    # anyway in case the compose file still references it (harmless if unused).
    if [[ -d "$REPO_ROOT/database" ]]; then
        rm -rf "$stage_after/database"
        cp -r "$REPO_ROOT/database" "$stage_after/database"
    fi

    cp_inject_labels "$stage_after/docker-compose.yml" "$TEST_LABEL"
    cp_inject_labels "$stage_after/docker-compose.prod.yml" "$TEST_LABEL"
    cp_force_pull_policy "$stage_after/docker-compose.prod.yml" never
    cp_pin_image_tag "$stage_after/docker-compose.prod.yml" backend "$LOCAL_IMAGE_TAG"
    cp_pin_image_tag "$stage_after/docker-compose.prod.yml" frontend "$LOCAL_IMAGE_TAG"
    for svc in celery-worker celery-cpu-worker celery-nlp-worker celery-embedding-worker celery-download-worker celery-redaction celery-cloud-asr-worker celery-beat flower; do
        cp_pin_image_tag "$stage_after/docker-compose.prod.yml" "$svc" "$LOCAL_IMAGE_TAG" 2>/dev/null || true
    done

    if [[ "$TEST_USE_GPU" == "true" && -f "$REPO_ROOT/docker-compose.gpu.yml" ]]; then
        cp "$REPO_ROOT/docker-compose.gpu.yml" "$stage_after/docker-compose.gpu.yml"
    fi

    # Stage the actual user-facing upgrade script so phase 08 can invoke
    # './opentranscribe.sh update' — exercising the real code path users run
    # when upgrading in place, not a hand-rolled compose sequence.
    cp "$REPO_ROOT/opentranscribe.sh" "$stage_after/opentranscribe.sh"
    chmod +x "$stage_after/opentranscribe.sh"

    # Reuse the SAME .env so credentials and ports are preserved across the
    # upgrade (mirrors what a real user sees on disk).
    cp "$stage_before/.env" "$stage_after/.env"
}

# Defensive cleanup for the stale-network-endpoint daemon bug. If a previous
# unclean shutdown (backend SIGKILL'd by a too-short healthcheck, crashed
# docker engine, etc.) left the 'opentranscribe_default' bridge in a state
# where the endpoint DB disagrees with the container list, 'compose down'
# fails with "has active endpoints" and a daemon restart becomes the only
# escape. Real users cannot restart dockerd, so we must prevent the bug from
# reaching phase 08 at all — detect empty-but-stuck networks up front and
# clear them before invoking the upgrade command.
_clean_stale_opentranscribe_network() {
    local net=opentranscribe_default
    docker network inspect "$net" >/dev/null 2>&1 || return 0
    local attached
    attached=$(docker network inspect "$net" --format '{{len .Containers}}' 2>/dev/null || echo 0)
    if [[ "$attached" != "0" ]]; then
        return 0  # network is in use — not stale
    fi
    gr_log "removing stale empty '$net' network before upgrade"
    if docker network rm "$net" >/dev/null 2>&1; then
        gr_ok "stale network cleared"
        return 0
    fi
    # "has active endpoints" on an empty network means the daemon's endpoint
    # DB is out of sync. Try to force-disconnect any phantom endpoints via
    # the raw API and retry. This is the non-destructive workaround that
    # avoids a 'systemctl restart docker'.
    gr_warn "network rm refused; attempting endpoint force-disconnect"
    local phantom_ids
    phantom_ids=$(docker network inspect "$net" --format '{{range $k,$v := .Containers}}{{$k}} {{end}}' 2>/dev/null)
    for cid in $phantom_ids; do
        docker network disconnect -f "$net" "$cid" >/dev/null 2>&1 || true
    done
    docker network rm "$net" >/dev/null 2>&1 || \
        gr_warn "could not remove stale network — upgrade may fail; run 'docker network prune'"
}

phase_08_start_new() {
    local stage_after="$TEST_ROOT/after"

    # Clear any stale daemon network state BEFORE invoking 'update' so that
    # the user-facing upgrade command runs against a clean host — same as a
    # real user's environment would be.
    _clean_stale_opentranscribe_network

    # Invoke the actual './opentranscribe.sh update' command. This is what
    # real users run to upgrade in place. It does 'compose down && compose
    # pull && compose up -d' under the hood, but going through the script
    # means we validate the code path users actually exercise — not a
    # hand-rolled sequence that could silently drift from the real behavior.
    pushd "$stage_after" >/dev/null
    gr_log "running './opentranscribe.sh update' (real user upgrade path)"
    ./opentranscribe.sh update || gr_die "opentranscribe.sh update failed"
    popd >/dev/null

    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    # Migrations may take several minutes on a populated DB — the healthcheck
    # start_period in docker-compose.yml is 600s and we mirror that budget.
    ac_wait_for_health 900

    # Tail backend logs for "Alembic upgrade complete" or similar marker
    docker logs opentranscribe-backend 2>&1 | grep -iE 'alembic|migration' | tail -20 \
        > "$TEST_ROOT/migration-log.txt" || true
}

phase_09_snapshot_post() {
    snapshot_state after
}

phase_10_assert_and_report() {
    TEST_REPORT_FILE="$TEST_ROOT/REPORT.md"
    : > "$TEST_REPORT_FILE"
    {
        echo "# Release Test Report — Scenario B (upgrade $FROM_VERSION → $LOCAL_IMAGE_TAG)"
        echo ""
        echo "- Project:    $TEST_PROJECT_NAME"
        echo "- Test root:  $TEST_ROOT"
        echo "- From:       $FROM_VERSION (Docker Hub)"
        echo "- To:         $LOCAL_IMAGE_TAG (local build)"
        echo "- Started:    $(date -Iseconds)"
        echo ""
        echo "## Migration log excerpt"
        echo '```'
        cat "$TEST_ROOT/migration-log.txt" 2>/dev/null || echo "(none captured)"
        echo '```'
        echo ""
        echo "## Assertions"
        echo ""
        echo "| Status | Assertion | Detail |"
        echo "|---|---|---|"
    } >> "$TEST_REPORT_FILE"
    export TEST_REPORT_FILE

    # ─── Snapshot diffs ─────────────────────────────────────────────────
    local pre="$TEST_ROOT/snapshots/before"
    local post="$TEST_ROOT/snapshots/after"

    # Compare media_file rows case-insensitively because v0.3.3 stores
    # filestatus as a native PG enum (uppercase: COMPLETED) and v0.4.0 stores
    # it as VARCHAR (lowercase: completed) after the v073 enum→varchar
    # migration. The IDs and filenames must match exactly; only the case of
    # the status string changes — that's the migration doing its job.
    if diff -q <(tr 'A-Z' 'a-z' < "$pre/media_files.txt") <(tr 'A-Z' 'a-z' < "$post/media_files.txt") >/dev/null 2>&1; then
        as_record PASS "media_file rows preserved (case-insensitive)"
    else
        as_record FAIL "media_file rows preserved (case-insensitive)" \
            "$(diff -u "$pre/media_files.txt" "$post/media_files.txt" | head -10 | tr '\n' ' ')"
    fi
    as_assert_diff_files "transcript_segment counts preserved" "$pre/segment_counts.txt" "$post/segment_counts.txt"
    as_assert_diff_files "MinIO ETag list unchanged" "$pre/minio_etags.json" "$post/minio_etags.json"

    # Alembic head should advance
    local pre_head post_head expected_head
    pre_head=$(<"$pre/alembic_head.txt")
    post_head=$(<"$post/alembic_head.txt")
    expected_head=$(grep -hE "^revision[[:space:]]*=" "$REPO_ROOT/backend/alembic/versions/"*.py \
        | tail -1 | awk -F'"' '{print $2}')
    as_assert_ne "alembic head advanced" "$pre_head" "$post_head"
    as_assert_eq "alembic head matches current head" "$expected_head" "$post_head"

    # Transcript prefix check (per file)
    if [[ -f "$TEST_ROOT/seeded-file-ids.txt" ]]; then
        while IFS= read -r fid; do
            python3 - "$pre/transcript-$fid.json" "$post/transcript-$fid.json" "$fid" \
                "$TEST_REPORT_FILE" <<'PY' || true
import json, sys
pre, post, fid, report = sys.argv[1:5]
def segs(p):
    try:
        d = json.load(open(p))
    except Exception:
        return None
    return d.get("segments") or d.get("transcript_segments") or []
pre_segs = segs(pre)
post_segs = segs(post)
ok = pre_segs is not None and post_segs is not None and len(post_segs) >= len(pre_segs)
if ok:
    for i, s in enumerate(pre_segs):
        ps = post_segs[i]
        if s.get("text") != ps.get("text") or abs((s.get("start") or 0) - (ps.get("start") or 0)) > 0.01:
            ok = False
            break
status = "PASS" if ok else "FAIL"
detail = "" if ok else f"pre={len(pre_segs or [])} post={len(post_segs or [])}"
print(f"{status:5}  transcript prefix preserved for file {fid}  {detail}")
with open(report, "a") as f:
    f.write(f"| {status} | transcript prefix preserved for file {fid} | {detail} |\n")
PY
        done < "$TEST_ROOT/seeded-file-ids.txt"
    fi

    # ─── New-feature liveness checks ────────────────────────────────────
    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    ac_login "$TEST_ADMIN_EMAIL" "$TEST_ADMIN_PASSWORD" || true
    local code

    code=$(curl -o /dev/null -s -w '%{http_code}' "http://localhost:${TEST_BACKEND_PORT}/api/docs")
    as_assert_http "API docs reachable post-upgrade" 200 "$code"

    code=$(curl -o /dev/null -s -w '%{http_code}' "http://localhost:${TEST_FRONTEND_PORT}/")
    as_assert_http "frontend reachable post-upgrade" 200 "$code"

    # MFA endpoint exists in 0.4.0 but did not in v0.3.3
    code=$(curl -o /dev/null -s -w '%{http_code}' \
        -H "Authorization: Bearer ${API_TOKEN:-}" \
        "$API_BASE/auth/mfa/status" || echo 000)
    as_assert "MFA endpoint present (was 404 in $FROM_VERSION)" '[[ "$code" != "404" && "$code" != "000" ]]'

    # Neural search / OpenSearch ML model check. This is the same strict
    # assertion Scenario A uses — it confirms the ML model is actually
    # DEPLOYED post-upgrade, not that hybrid search silently fell back to
    # BM25. The v0.3.x heap-too-small regression we fixed must not be able
    # to ship undetected via the upgrade path.
    #
    # Neural search registration + deployment runs as an ASYNC background
    # task after backend startup, so we poll for up to 3 minutes rather than
    # checking once immediately. This matches realistic user expectations:
    # "backend is up, wait a moment, then neural search is live".
    local ml_deployed=0
    local ml_wait=0
    while [ "$ml_wait" -lt 180 ]; do
        ml_deployed=$(docker exec opentranscribe-opensearch curl -s \
            'http://localhost:9200/_plugins/_ml/models/_search' \
            -H 'Content-Type: application/json' \
            -d '{"query":{"term":{"model_state":"DEPLOYED"}},"size":1}' \
            2>/dev/null \
            | python3 -c 'import sys,json; print(json.load(sys.stdin).get("hits",{}).get("total",{}).get("value",0))' \
            2>/dev/null || echo 0)
        [ "$ml_deployed" -ge 1 ] && break
        sleep 10
        ml_wait=$((ml_wait + 10))
    done
    as_assert_ge "OpenSearch ML model deployed post-upgrade (neural search active)" "$ml_deployed" 1

    # Hybrid search smoke — confirm the seeded transcript is still queryable
    # via the semantic path after the migration + reindex. After a v0.3.x →
    # 0.4.x upgrade, the existing transcripts need to be re-indexed with
    # neural embeddings (background task that runs after the ML model
    # deploys). This can take several minutes for the embedding task to pick
    # up pre-existing segments and compute vectors for them — poll up to 10
    # minutes.
    local hits=0
    local hit_wait=0
    while [ "$hit_wait" -lt 600 ]; do
        hits=$(ac_search "the" 2>/dev/null | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d.get("total_results") or len(d.get("results") or d.get("hits") or []))
' 2>/dev/null || echo 0)
        [ "$hits" -ge 1 ] && break
        sleep 10
        hit_wait=$((hit_wait + 10))
    done
    as_assert_ge "hybrid search returns hits post-upgrade" "$hits" 1

    as_summary | tee -a "$TEST_REPORT_FILE"
    {
        echo ""
        echo "Finished: $(date -Iseconds)"
    } >> "$TEST_REPORT_FILE"
}

# ─── Driver ─────────────────────────────────────────────────────────────────
mkdir -p "$TEST_ROOT"
exec > >(tee -a "$TEST_ROOT/run.log") 2>&1

echo "OpenTranscribe Release Test — Scenario B (upgrade ${FROM_VERSION} → ${LOCAL_IMAGE_TAG})"
echo "Started: $(date -Iseconds)"
echo "Repo:    $REPO_ROOT (commit $(git -C "$REPO_ROOT" rev-parse --short HEAD))"
echo

phase 00 phase_00_preflight
phase 01 phase_01_build_local_images
phase 02 phase_02_verify_from_version
phase 03 phase_03_prepare_v033_compose
phase 04 phase_04_start_v033
phase 05 phase_05_seed_data
phase 06 phase_06_snapshot_pre
phase 07 phase_07_swap_to_new
phase 08 phase_08_start_new
phase 09 phase_09_snapshot_post
phase 10 phase_10_assert_and_report

echo
echo "Done. Report: $TEST_ROOT/REPORT.md"
echo "Stack left running for inspection. Tear down with: $0 --cleanup"
