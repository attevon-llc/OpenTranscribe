#!/bin/bash
# Scenario A — fresh-install via the one-liner setup script.
#
# Validates the documented one-liner end-to-end:
#   curl -fsSL .../setup-opentranscribe.sh | bash
#
# REQUIRES the live deployment to be stopped first (./opentr.sh stop) so the
# default container names and ports are available. The one-liner runs with
# its NORMAL defaults — same container names, same ports — so this is a
# faithful test of what a brand-new user would see.
#
# The only post-setup patching is to pin the locally-built :${LOCAL_IMAGE_TAG}
# image (because we haven't pushed 0.4.0 to Docker Hub yet) and force
# pull_policy: never. After Phase 5 of the release pipeline pushes the new
# tag to Docker Hub, this patching could be skipped entirely.
#
# Idempotent: phases are tracked under $TEST_ROOT/.phase/<phase>.done so
# re-running picks up where it left off. Pass --force to clear them.

set -euo pipefail

# ─── Locate library ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ─── Tunables (overridable via env) ─────────────────────────────────────────
TEST_SCENARIO="fresh-install"
TEST_PROJECT_NAME="${TEST_PROJECT_NAME:-ot-reltest-fresh}"
TEST_ROOT="${TEST_ROOT:-/mnt/nvm/opentranscribe-test-runs/${TEST_PROJECT_NAME}-$(date +%Y%m%d-%H%M%S)}"
TEST_LABEL="com.opentranscribe.release-test=${TEST_SCENARIO}"

TO_BRANCH="${TO_BRANCH:-master}"
# Version under test, derived from the VERSION file rather than hardcoded. The
# previous default (v0.4.0) went stale the moment v0.4.1 shipped, and the two
# scenarios disagreed on whether the tag carried a `v` — this one said "v0.4.0",
# the upgrade scenario said "0.4.0", and docker-build-push.sh always produces
# "vX.Y.Z", so the upgrade scenario's default never matched a real local build.
# lib/versions.sh owns that normalisation now. Sourced after guardrails.sh below.
LOCAL_IMAGE_TAG="${LOCAL_IMAGE_TAG:-}"

# Set USE_HUB_IMAGES=true to skip the local build phase and pull the published
# Docker Hub images instead. Phase 03 will set pull_policy: always and pin the
# image tag to :${LOCAL_IMAGE_TAG} from Hub (not local cache). Use this for
# the final post-push smoke test.
USE_HUB_IMAGES="${USE_HUB_IMAGES:-false}"

# GPU policy: pin to GPU 1 (RTX 3080 Ti, free) — leaves GPU 0 (A6000) and
# GPU 2 (A6000, busy with LLM) untouched. Override with TEST_USE_GPU=false.
TEST_USE_GPU="${TEST_USE_GPU:-true}"
TEST_GPU_DEVICE_ID="${TEST_GPU_DEVICE_ID:-1}"
export TEST_USE_GPU TEST_GPU_DEVICE_ID

# Use the one-liner's default ports (5173-5180) since the live deployment
# is stopped. Override only if you have other services squatting these.
TEST_FRONTEND_PORT="${FRONTEND_PORT:-5173}"
TEST_BACKEND_PORT="${BACKEND_PORT:-5174}"
TEST_FLOWER_PORT="${FLOWER_PORT:-5175}"
TEST_POSTGRES_PORT="${POSTGRES_PORT:-5176}"
TEST_REDIS_PORT="${REDIS_PORT:-5177}"
TEST_MINIO_PORT="${MINIO_PORT:-5178}"
TEST_MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-5179}"
TEST_OPENSEARCH_PORT="${OPENSEARCH_PORT:-5180}"
TEST_PORTS="$TEST_FRONTEND_PORT $TEST_BACKEND_PORT $TEST_FLOWER_PORT $TEST_POSTGRES_PORT $TEST_REDIS_PORT $TEST_MINIO_PORT $TEST_MINIO_CONSOLE_PORT $TEST_OPENSEARCH_PORT"

# Test admin user
# Default admin user is created by the backend on first start.
TEST_ADMIN_EMAIL="${TEST_ADMIN_EMAIL:-admin@example.com}"
TEST_ADMIN_PASSWORD="${TEST_ADMIN_PASSWORD:-password}"

# Test media: directory of small real media files (mp3/m4a/wav/mp4) to upload
# and transcribe. Files are copied from this dir into the test container via
# multipart upload — this exercises the same code path a real user uses when
# dragging a file into the UI. Files in this dir are NOT committed to git.
TEST_MEDIA_DIR="${TEST_MEDIA_DIR:-/mnt/nvm/opentranscribe-test-runs/test-media}"

# Cleanup mode
DO_CLEANUP=0
DO_FORCE=0

# ─── Argument parsing ───────────────────────────────────────────────────────
while (( $# > 0 )); do
    case "$1" in
        --cleanup)   DO_CLEANUP=1 ;;
        --force)     DO_FORCE=1 ;;
        --yes)       export OT_RELEASE_TEST_YES=1 ;;
        --help|-h)
            cat <<EOF
Usage: $0 [--cleanup] [--force] [--yes]

Prerequisite: stop the live deployment first with \`./opentr.sh stop\`.
After the test, restart it with \`./opentr.sh start dev\` (or whichever
mode you were using).

Env:
  TEST_PROJECT_NAME      default ot-reltest-fresh  (used as label namespace)
  TEST_ROOT              default /mnt/nvm/opentranscribe-test-runs/<name>-<ts>
  TO_BRANCH              default master  (branch the one-liner pulls files from)
  LOCAL_IMAGE_TAG        default 0.4.0   (locally built tag the test pins)
  USE_HUB_IMAGES         default false   (true = skip local build, pull from Docker Hub)
  TEST_USE_GPU           default true
  TEST_GPU_DEVICE_ID     default 1       (RTX 3080 Ti, leaves A6000 free)
EOF
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

export TEST_SCENARIO TEST_PROJECT_NAME TEST_ROOT TEST_LABEL
export TEST_FRONTEND_PORT TEST_BACKEND_PORT TEST_FLOWER_PORT TEST_POSTGRES_PORT \
       TEST_REDIS_PORT TEST_MINIO_PORT TEST_MINIO_CONSOLE_PORT TEST_OPENSEARCH_PORT \
       TEST_PORTS

# ─── Source library ─────────────────────────────────────────────────────────
# shellcheck source=lib/guardrails.sh
source "$LIB_DIR/guardrails.sh"
# shellcheck source=lib/compose-patch.sh
source "$LIB_DIR/compose-patch.sh"
# shellcheck source=lib/api-client.sh
source "$LIB_DIR/api-client.sh"
# shellcheck source=lib/assertions.sh
source "$LIB_DIR/assertions.sh"
# shellcheck source=lib/versions.sh
source "$LIB_DIR/versions.sh"

# Resolve the version under test now that versions.sh is available.
if [[ -z "$LOCAL_IMAGE_TAG" ]]; then
    LOCAL_IMAGE_TAG="$(ver_to_version)"
fi
LOCAL_IMAGE_TAG="$(ver_normalize "$LOCAL_IMAGE_TAG")"
gr_log "version under test: $LOCAL_IMAGE_TAG (from ${TO_VERSION:+TO_VERSION}${TO_VERSION:-VERSION file})"

# ─── Cleanup mode ───────────────────────────────────────────────────────────
if (( DO_CLEANUP == 1 )); then
    gr_log "cleanup requested"
    gr_cleanup
    exit 0
fi

# ─── Phase tracking ─────────────────────────────────────────────────────────
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

# ─── Helpers ────────────────────────────────────────────────────────────────
ensure_secrets_file() {
    local f="$SCRIPT_DIR/.env.test-secrets"
    if [[ ! -f "$f" ]]; then
        cp "$SCRIPT_DIR/.env.test-secrets.example" "$f"
        chmod 600 "$f"
        gr_die "created template at $f — fill in HUGGINGFACE_TOKEN and re-run"
    fi
    # shellcheck disable=SC1090
    source "$f"
    if [[ -z "${HUGGINGFACE_TOKEN:-}" ]]; then
        gr_die "HUGGINGFACE_TOKEN missing in $f — required for PyAnnote model download"
    fi
    export HUGGINGFACE_TOKEN
}

ensure_live_deployment_stopped() {
    # Refuse to run if any opentranscribe-* container is still up — would
    # collide with the one-liner's default container names.
    local running
    running=$(docker ps --format '{{.Names}}' --filter 'name=^opentranscribe-' || true)
    if [[ -n "$running" ]]; then
        gr_die "live deployment is still running:
$running

Stop it first with: ./opentr.sh stop
(this preserves all data — postgres bind mount, NAS minio, named volumes)"
    fi
    # Also refuse if there are stopped opentranscribe-* containers (would
    # collide on container_name during create).
    local stopped
    stopped=$(docker ps -a --format '{{.Names}}' --filter 'name=^opentranscribe-' || true)
    if [[ -n "$stopped" ]]; then
        gr_warn "removing stopped (already-down) live containers to free names: $stopped"
        docker rm $stopped >/dev/null
    fi
    gr_ok "no opentranscribe-* containers exist; safe to start the test stack"
}

# ─── Phase implementations ──────────────────────────────────────────────────

phase_00_preflight() {
    ensure_secrets_file
    gr_preflight
    ensure_live_deployment_stopped
}

phase_01_build_local_images() {
    if [[ "$USE_HUB_IMAGES" == "true" ]]; then
        gr_log "USE_HUB_IMAGES=true — skipping local build; images will be pulled from Docker Hub in phase 03"
        gr_ok "phase skipped (hub mode)"
        return 0
    fi
    # Intentionally tag ONLY :${LOCAL_IMAGE_TAG}, never :latest.
    if docker image inspect "davidamacey/opentranscribe-backend:${LOCAL_IMAGE_TAG}" >/dev/null 2>&1; then
        gr_ok "backend image davidamacey/opentranscribe-backend:${LOCAL_IMAGE_TAG} already present"
    else
        gr_log "building backend (this can take ~5-10 min)"
        docker build \
            -t "davidamacey/opentranscribe-backend:${LOCAL_IMAGE_TAG}" \
            -f "$REPO_ROOT/backend/Dockerfile.prod" \
            "$REPO_ROOT/backend"
    fi
    if docker image inspect "davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG}" >/dev/null 2>&1; then
        gr_ok "frontend image davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG} already present"
    else
        gr_log "building frontend"
        docker build \
            -t "davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG}" \
            -f "$REPO_ROOT/frontend/Dockerfile.prod" \
            "$REPO_ROOT/frontend"
    fi
    docker image inspect "davidamacey/opentranscribe-backend:${LOCAL_IMAGE_TAG}" \
        --format 'backend digest: {{.Id}}' | tee "$TEST_ROOT/image-digests.txt"
    docker image inspect "davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG}" \
        --format 'frontend digest: {{.Id}}' | tee -a "$TEST_ROOT/image-digests.txt"
}

phase_02_run_one_liner() {
    local install_dir="$TEST_ROOT/install"
    mkdir -p "$install_dir"
    pushd "$install_dir" >/dev/null

    gr_log "running setup-opentranscribe.sh from branch $TO_BRANCH in unattended mode"
    OPENTRANSCRIBE_BRANCH="$TO_BRANCH" \
    OPENTRANSCRIBE_UNATTENDED=1 \
    HUGGINGFACE_TOKEN="$HUGGINGFACE_TOKEN" \
    WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}" \
    OPENSEARCH_MODELS="${OPENSEARCH_MODELS:-all-MiniLM-L6-v2}" \
    GPU_DEVICE_ID="$TEST_GPU_DEVICE_ID" \
    bash "$REPO_ROOT/setup-opentranscribe.sh" || gr_die "one-liner failed"

    popd >/dev/null
}

phase_03_pin_local_image() {
    # Minimal post-setup patch: pin :${LOCAL_IMAGE_TAG}, force pull_policy: never,
    # inject the release-test label so cleanup can find managed resources.
    # No name/port/volume rewrites — the one-liner's defaults are used as-is
    # because the live deployment is stopped.
    local target="$TEST_ROOT/install/opentranscribe"
    [[ -d "$target" ]] || target="$TEST_ROOT/install"
    [[ -f "$target/docker-compose.prod.yml" ]] || gr_die "no docker-compose.prod.yml under $target"

    cp "$target/docker-compose.prod.yml" "$target/docker-compose.prod.yml.bak"

    if [[ "$USE_HUB_IMAGES" == "true" ]]; then
        # Hub mode: remove any cached local image first so Docker is forced to
        # actually pull from the registry. Pin to the explicit release tag so we
        # know exactly which Hub image ran. pull_policy: always ensures a fresh pull.
        gr_log "hub mode: removing cached local images to force Hub pull"
        docker image rm -f \
            "davidamacey/opentranscribe-backend:${LOCAL_IMAGE_TAG}" \
            "davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG}" \
            "davidamacey/opentranscribe-backend:latest" \
            "davidamacey/opentranscribe-frontend:latest" 2>/dev/null || true
        cp_force_pull_policy "$target/docker-compose.prod.yml" always
        cp_inject_labels "$target/docker-compose.prod.yml" "$TEST_LABEL"
        cp_pin_image_tag "$target/docker-compose.prod.yml" backend "$LOCAL_IMAGE_TAG"
        cp_pin_image_tag "$target/docker-compose.prod.yml" frontend "$LOCAL_IMAGE_TAG"
        for svc in celery-worker celery-cpu-worker celery-nlp-worker celery-embedding-worker celery-download-worker celery-redaction celery-cloud-asr-worker celery-beat flower; do
            cp_pin_image_tag "$target/docker-compose.prod.yml" "$svc" "$LOCAL_IMAGE_TAG" 2>/dev/null || true
        done
        gr_ok "pull_policy=always, image tag pinned to Hub :${LOCAL_IMAGE_TAG}"
    else
        cp_force_pull_policy "$target/docker-compose.prod.yml" never
        cp_inject_labels "$target/docker-compose.prod.yml" "$TEST_LABEL"
        cp_pin_image_tag "$target/docker-compose.prod.yml" backend "$LOCAL_IMAGE_TAG"
        cp_pin_image_tag "$target/docker-compose.prod.yml" frontend "$LOCAL_IMAGE_TAG"
        for svc in celery-worker celery-cpu-worker celery-nlp-worker celery-embedding-worker celery-download-worker celery-redaction celery-cloud-asr-worker celery-beat flower; do
            cp_pin_image_tag "$target/docker-compose.prod.yml" "$svc" "$LOCAL_IMAGE_TAG" 2>/dev/null || true
        done
        gr_ok "image tag pinned to :${LOCAL_IMAGE_TAG}, pull_policy=never, label injected"
    fi

    # Also label the base file's services for cleanup symmetry
    cp "$target/docker-compose.yml" "$target/docker-compose.yml.bak"
    cp_inject_labels "$target/docker-compose.yml" "$TEST_LABEL"

    # Pre-create the model cache directory with appuser (UID 1000) ownership.
    # The setup-opentranscribe.sh fix_model_cache_permissions step runs before
    # docker compose up, so when bind mounts auto-create subdirs, docker
    # creates them as root. We chown them now so the appuser inside the
    # backend/celery containers can write the model cache.
    local model_cache_dir
    model_cache_dir=$(awk -F= '/^MODEL_CACHE_DIR=/{print $2; exit}' "$target/.env")
    [[ -z "$model_cache_dir" || "$model_cache_dir" == "./models" ]] && model_cache_dir="$target/models"
    [[ "$model_cache_dir" != /* ]] && model_cache_dir="$target/${model_cache_dir#./}"
    mkdir -p "$model_cache_dir"/{huggingface,torch,nltk_data,sentence-transformers,opensearch-ml}

    # In hub mode (USE_HUB_IMAGES=true) we intentionally skip the shared cache
    # so models download from HuggingFace exactly as a fresh user would experience.
    # Set SEED_MODEL_CACHE=true to opt into the fast path even in hub mode.
    local shared_cache="/mnt/nvm/opentranscribe-test-runs/.shared-model-cache"
    if [[ "$USE_HUB_IMAGES" == "true" && "${SEED_MODEL_CACHE:-false}" != "true" ]]; then
        # Verify the model directory is truly empty so models download fresh.
        # TEST_ROOT is timestamped so this directory should not exist yet.
        if [[ -d "$model_cache_dir/huggingface/hub" ]]; then
            gr_die "model cache at $model_cache_dir/huggingface/hub already exists — " \
                   "this would skip model downloads. Delete it or use a fresh TEST_ROOT."
        fi
        gr_log "hub mode: model cache is empty — models will download from HuggingFace on first start (fresh-user path)"
    elif [[ -d "$shared_cache" && -f "$shared_cache/.seeded-from-live" ]]; then
        gr_log "seeding model cache from shared cache (rsync --link-dest) …"
        rsync -a --link-dest="$shared_cache/" "$shared_cache/" "$model_cache_dir/" 2>/dev/null || \
            gr_warn "rsync seed failed — models will download from HuggingFace on first start"
        gr_ok "model cache seeded from $shared_cache (hard-linked, no disk copy)"
    else
        gr_warn "shared model cache not found at $shared_cache — first start will download models"
    fi

    docker run --rm -v "$model_cache_dir:/m" busybox sh -c \
        "chown -R 1000:1000 /m && chmod -R u+w /m" >/dev/null 2>&1 || \
        gr_warn "could not chown $model_cache_dir to 1000:1000 (model downloads may fail)"
    gr_ok "model cache pre-created at $model_cache_dir with UID 1000 ownership"

    # Override GPU_DEVICE_ID in the .env if a non-default was requested
    if [[ "$TEST_GPU_DEVICE_ID" != "0" ]]; then
        if grep -q '^GPU_DEVICE_ID=' "$target/.env"; then
            sed -i.bak "s|^GPU_DEVICE_ID=.*|GPU_DEVICE_ID=$TEST_GPU_DEVICE_ID|" "$target/.env"
            rm -f "$target/.env.bak"
        else
            echo "GPU_DEVICE_ID=$TEST_GPU_DEVICE_ID" >> "$target/.env"
        fi
        gr_ok "pinned GPU_DEVICE_ID=$TEST_GPU_DEVICE_ID in .env"
    fi
}

phase_04_start_stack() {
    local target="$TEST_ROOT/install/opentranscribe"
    [[ -d "$target" ]] || target="$TEST_ROOT/install"
    pushd "$target" >/dev/null
    local compose_args=(-f docker-compose.yml -f docker-compose.prod.yml)
    if [[ "$TEST_USE_GPU" == "true" && -f docker-compose.gpu.yml ]]; then
        compose_args+=(-f docker-compose.gpu.yml)
        gr_log "docker compose up -d (base + prod + gpu, GPU_DEVICE_ID=$TEST_GPU_DEVICE_ID)"
    else
        gr_log "docker compose up -d (base + prod, CPU)"
    fi
    docker compose "${compose_args[@]}" up -d
    popd >/dev/null
}

phase_05_wait_for_health() {
    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    ac_wait_for_health 900
}

phase_06_api_smoke() {
    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    TEST_REPORT_FILE="$TEST_ROOT/REPORT.md"
    : > "$TEST_REPORT_FILE"
    {
        echo "# Release Test Report — $TEST_SCENARIO"
        echo ""
        echo "- Project: $TEST_PROJECT_NAME"
        echo "- Test root: $TEST_ROOT"
        echo "- Branch: $TO_BRANCH"
        echo "- Image tag: $LOCAL_IMAGE_TAG ($([ "$USE_HUB_IMAGES" == "true" ] && echo "Docker Hub" || echo "local build"))"
        echo "- Started: $(date -Iseconds)"
        echo ""
        echo "| Status | Assertion | Detail |"
        echo "|---|---|---|"
    } >> "$TEST_REPORT_FILE"
    export TEST_REPORT_FILE

    # The backend creates a default admin (admin@example.com / password) on first
    # start, so registration is not needed. Just log in.
    ac_login "$TEST_ADMIN_EMAIL" "$TEST_ADMIN_PASSWORD"

    local fe_code
    fe_code=$(curl -o /dev/null -s -w '%{http_code}' "http://localhost:${TEST_FRONTEND_PORT}/")
    as_assert_http "frontend GET /" 200 "$fe_code"

    local api_code
    api_code=$(curl -o /dev/null -s -w '%{http_code}' "http://localhost:${TEST_BACKEND_PORT}/api/docs")
    as_assert_http "backend GET /api/docs" 200 "$api_code"

    if [[ ! -d "$TEST_MEDIA_DIR" ]]; then
        as_record FAIL "TEST_MEDIA_DIR missing: $TEST_MEDIA_DIR"
    else
        # Pick up to 3 small real media files from the dir
        local media_files=()
        while IFS= read -r f; do
            media_files+=("$f")
        done < <(find "$TEST_MEDIA_DIR" -maxdepth 1 -type f \
                    \( -iname "*.mp3" -o -iname "*.m4a" -o -iname "*.mp4" \
                       -o -iname "*.wav" -o -iname "*.flac" -o -iname "*.ogg" \) \
                    -size -5M | head -2)
        if (( ${#media_files[@]} == 0 )); then
            as_record FAIL "no media files found in $TEST_MEDIA_DIR (need at least one .mp3/.m4a/.wav/.mp4 under 5 MB)"
        else
            local file_ids=()
            for path in "${media_files[@]}"; do
                local fid
                fid=$(ac_upload_file "$path") || { as_record FAIL "upload $(basename "$path")"; continue; }
                file_ids+=("$fid")
                as_record PASS "upload accepted: $(basename "$path") (uuid=$fid)"
            done

            for fid in "${file_ids[@]}"; do
                if ac_wait_for_file_status "$fid" 1800; then
                    as_record PASS "transcription completed for file $fid"
                    local seg_count
                    seg_count=$(ac_get_segments "$fid" | python3 -c '
import sys, json
d = json.load(sys.stdin)
if isinstance(d, list):
    print(len(d))
else:
    segs = d.get("segments") or d.get("transcript_segments") or d.get("results") or []
    print(len(segs))
' 2>/dev/null || echo 0)
                    as_assert_ge "segments[] non-empty for $fid" "$seg_count" 1
                else
                    as_record FAIL "transcription for file $fid"
                fi
            done

            # Hybrid search — query for a common English stop word that any
            # transcribed audiobook will contain.
            local hits
            hits=$(ac_search "the" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d.get("total_results") or len(d.get("results") or d.get("hits") or []))
' 2>/dev/null || echo 0)
            as_assert_ge "hybrid search returns hits" "$hits" 1

            # Stricter neural-search assertion: confirm that the OpenSearch
            # ML model is actually DEPLOYED (not just that hybrid search
            # silently fell back to BM25 keyword matching). Without this
            # check the heap-too-small bug from v0.3.x can ship undetected.
            local ml_deployed
            ml_deployed=$(docker exec opentranscribe-opensearch curl -s \
                'http://localhost:9200/_plugins/_ml/models/_search' \
                -H 'Content-Type: application/json' \
                -d '{"query":{"term":{"model_state":"DEPLOYED"}},"size":1}' \
                2>/dev/null \
                | python3 -c 'import sys,json; print(json.load(sys.stdin).get("hits",{}).get("total",{}).get("value",0))' \
                2>/dev/null || echo 0)
            as_assert_ge "OpenSearch ML model deployed (neural search active)" "$ml_deployed" 1
        fi
    fi

    # Alembic head check (one-liner uses the standard 'opentranscribe-postgres' name)
    local alembic_head expected_head
    alembic_head=$(docker exec opentranscribe-postgres \
        psql -U postgres -d opentranscribe -tAc \
        "SELECT version_num FROM alembic_version" 2>/dev/null || echo "")
    # Derived from the down_revision graph, not `grep | tail -1`. That old form
    # sorted by FILENAME and only worked by luck of 3-digit zero-padded ids; the
    # chain is already non-contiguous (v130->v071, v073->v140, two v270* files,
    # v375-v381 renumbered), and a 4-digit id or a second head would have made it
    # silently assert the wrong revision.
    expected_head=$(ver_alembic_head "$REPO_ROOT/backend")
    as_assert_eq "alembic head" "$expected_head" "$alembic_head"

    as_summary | tee -a "$TEST_REPORT_FILE"
    {
        echo ""
        echo "Finished: $(date -Iseconds)"
    } >> "$TEST_REPORT_FILE"
}

# ─── Driver ─────────────────────────────────────────────────────────────────
mkdir -p "$TEST_ROOT"
exec > >(tee -a "$TEST_ROOT/run.log") 2>&1

echo "OpenTranscribe Release Test — Scenario A (fresh install)"
echo "Started: $(date -Iseconds)"
echo "Repo:    $REPO_ROOT (commit $(git -C "$REPO_ROOT" rev-parse --short HEAD))"
echo

# Always source secrets before any phase runs (phases 02+ need
# HUGGINGFACE_TOKEN and friends; phase 00 may have been skipped on resume).
ensure_secrets_file

phase 00 phase_00_preflight
phase 01 phase_01_build_local_images
phase 02 phase_02_run_one_liner
phase 03 phase_03_pin_local_image
phase 04 phase_04_start_stack
phase 05 phase_05_wait_for_health
phase 06 phase_06_api_smoke

echo
echo "Done. Report: $TEST_ROOT/REPORT.md"
echo "Stack left running for inspection. Tear down with: $0 --cleanup"
echo "Then restart your live deployment with: ./opentr.sh start dev"
