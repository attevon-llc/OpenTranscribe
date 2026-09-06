#!/bin/bash
# Scenario C — lite-mode full pipeline rehearsal (mocked cloud ASR + mocked LLM).
#
# Where test-fresh-install.sh proves the one-liner install path and
# scripts/lite-smoke.sh proves lite/cpu-only TOPOLOGY (no GPU worker, no GPU
# memory resident), NEITHER exercises the lite deployment's actual pipeline:
# upload -> cloud ASR -> segments/speakers -> search -> chat. This scenario
# does, using the mocked Gladia stand-in (scripts/mock-asr-server.py) and the
# mocked LLM (scripts/mock-llm-server.py) so it needs no GPU, no vendor API
# key, and no network egress — see backend/tests/integration/
# test_lite_mode_mocked_providers.py (Phase 3 of this same effort), whose
# proven-correct request/response shapes this script reuses via
# lib/api-client.sh's ac_create_asr_config / ac_create_llm_config /
# ac_chat_completion helpers.
#
# REQUIRES the live deployment to be stopped first (./opentr.sh stop) — same
# constraint as test-fresh-install.sh, for the same reason (stock container
# names/ports via the one-liner's defaults).
#
# TEST_USE_GPU is hard-coded false: this scenario's entire point is the
# no-GPU lite deployment shape.
#
# ⚠️ SCOPE (REWRITTEN BY ISSUE #680 — the previous version of this block is now FALSE).
#
# This block used to say `--lite` was not a user-reachable deployment shape, resting on
# three facts. All three were changed by #680, in one place, and are now asserted in the
# positive by `test_lite_mode_is_reachable_by_a_shipped_deployment`
# (backend/tests/unit/test_compose_file_selection.py):
#
#   * `docker-compose.lite.yml` IS in release-manifest.txt, so a real
#     `curl … setup-opentranscribe.sh | bash` install downloads it.
#   * `opentranscribe.sh:get_compose_files()` HAS a lite branch keyed on
#     DEPLOYMENT_MODE, so a production install can select it.
#   * `scripts/docker-build-push.sh all` builds the lite image, so a release publishes
#     `davidamacey/opentranscribe-backend-lite`.
#
# That change was not cosmetic: the full/CUDA image publishes an **amd64-only** manifest
# (no aarch64 CUDA torch wheel, no aarch64 onnxruntime-gpu wheel, no CUDA arm64
# diar-native sidecar), so `arm64_deployment_preflight()` defaults an arm64 host to
# DEPLOYMENT_MODE=lite. Lite is the ONLY published deployment for that architecture.
#
# This scenario nonetheless still hand-builds its `docker compose -f ...` chain, which
# is now justified differently rather than not at all: it must pin ONE fixed chain
# regardless of the host, and a machine with a GPU would otherwise have the shipped
# selector add the GPU overlay — defeating the no-GPU shape this scenario exists to
# exercise. It is also the live positive case for
# `test_the_hand_built_bringup_detector_actually_fires`. Driving it through
# `DEPLOYMENT_MODE=lite FORCE_CPU_MODE=true ./opentranscribe.sh start` is now POSSIBLE
# and is the right follow-up — see the exemption entry in that same test module.
#
# Since issue #660, this chain also hand-builds -f docker-compose.diar-native.yml: lite's
# speaker embeddings come from the diar-native CPU-EP sidecar's /embed_window, not an
# in-process PyAnnote model. Its weights come from DIAR_NATIVE_MODELS_DIR, pre-seeded
# here from the shared model cache — since issue #654, `requirements-lite.txt` DOES ship
# the export toolchain (pyannote.audio, onnx, onnxscript, onnxslim, onnxconverter-common),
# so a lite deployment can provision its own weights on first boot exactly like the full
# image; this rehearsal pre-seeds them anyway so the run is deterministic rather than
# depending on network access to HuggingFace mid-rehearsal. The sidecar is pinned to the
# SAME locally-built lite image as the other lite workers (DIAR_NATIVE_IMAGE in phase_03).
# This does NOT make lite shippable; it is still reachable only from here and from
# opentr.sh.
#
# Idempotent: phases are tracked under $TEST_ROOT/.phase/<phase>.done so
# re-running picks up where it left off. Pass --force to clear them.
#
# Exit codes — the contract scripts/release.sh and scripts/test-matrix.sh share:
#   0 every assertion PASSed · 1 an assertion FAILed or a guardrail refused ·
#   2 misuse (unknown argument) · 4 operator abort (declined the I UNDERSTAND prompt)
# Preconditions that a real operator can clear (live containers up, ports bound, disk
# space) currently exit 1 rather than the contract's 3 — see gr_die in lib/guardrails.sh.

set -euo pipefail

# ─── Locate library ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ─── Tunables (overridable via env) ─────────────────────────────────────────
TEST_SCENARIO="lite-mode"
TEST_PROJECT_NAME="${TEST_PROJECT_NAME:-ot-reltest-lite}"
# Also the compose project (env-template.sh writes it as COMPOSE_PROJECT_NAME into the
# generated .env) — export it here too so compose-project.sh's compose_project_name()
# scopes every `docker ps` lookup below to THIS rehearsal's stack, never whatever
# unrelated stack (e.g. the live dev one) happens to be running on the same host.
export COMPOSE_PROJECT_NAME="$TEST_PROJECT_NAME"
TEST_ROOT="${TEST_ROOT:-/mnt/nvm/opentranscribe-test-runs/${TEST_PROJECT_NAME}-$(date +%Y%m%d-%H%M%S)}"
TEST_LABEL="com.opentranscribe.release-test=${TEST_SCENARIO}"

TO_BRANCH="${TO_BRANCH:-master}"
LOCAL_IMAGE_TAG="${LOCAL_IMAGE_TAG:-}"

# Always CPU-only — this scenario's whole point is the no-GPU lite topology.
TEST_USE_GPU=false
TEST_GPU_DEVICE_ID="${TEST_GPU_DEVICE_ID:-1}"
export TEST_USE_GPU TEST_GPU_DEVICE_ID

TEST_FRONTEND_PORT="${FRONTEND_PORT:-5173}"
TEST_BACKEND_PORT="${BACKEND_PORT:-5174}"
TEST_FLOWER_PORT="${FLOWER_PORT:-5175}"
TEST_POSTGRES_PORT="${POSTGRES_PORT:-5176}"
TEST_REDIS_PORT="${REDIS_PORT:-5177}"
TEST_MINIO_PORT="${MINIO_PORT:-5178}"
TEST_MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-5179}"
TEST_OPENSEARCH_PORT="${OPENSEARCH_PORT:-5180}"
TEST_MOCK_ASR_PORT="${MOCK_ASR_PORT:-5198}"
TEST_MOCK_LLM_PORT="${MOCK_LLM_PORT:-5199}"
TEST_PORTS="$TEST_FRONTEND_PORT $TEST_BACKEND_PORT $TEST_FLOWER_PORT $TEST_POSTGRES_PORT $TEST_REDIS_PORT $TEST_MINIO_PORT $TEST_MINIO_CONSOLE_PORT $TEST_OPENSEARCH_PORT $TEST_MOCK_ASR_PORT $TEST_MOCK_LLM_PORT"

TEST_ADMIN_EMAIL="${TEST_ADMIN_EMAIL:-admin@example.com}"
TEST_ADMIN_PASSWORD="${TEST_ADMIN_PASSWORD:-password}"

# Small real WAV used by test_lite_mode_mocked_providers.py — 7 canned
# segments / 2 canned speakers, already proven against the mock.
TEST_SAMPLE_WAV="$REPO_ROOT/backend/tests/fixtures/media/sample_short.wav"

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
  TEST_PROJECT_NAME      default ot-reltest-lite  (used as label namespace)
  TEST_ROOT              default /mnt/nvm/opentranscribe-test-runs/<name>-<ts>
  TO_BRANCH               default master  (branch the one-liner pulls files from)
  LOCAL_IMAGE_TAG         default from VERSION file
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
       TEST_MOCK_ASR_PORT TEST_MOCK_LLM_PORT TEST_PORTS

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
# shellcheck source=lib/model-cache.sh
source "$LIB_DIR/model-cache.sh"
# shellcheck source=../lib/compose-project.sh
source "$REPO_ROOT/scripts/lib/compose-project.sh"

if [[ -z "$LOCAL_IMAGE_TAG" ]]; then
    LOCAL_IMAGE_TAG="$(ver_to_version)"
fi
LOCAL_IMAGE_TAG="$(ver_normalize "$LOCAL_IMAGE_TAG")"
export OT_TEST_IMAGE_TAG="$LOCAL_IMAGE_TAG"
gr_log "version under test: $LOCAL_IMAGE_TAG"

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
    # Lite mode never runs PyAnnote locally, but the one-liner install path
    # still asks for the token up front — reuse the same secrets file rather
    # than inventing a second bootstrap path.
    export HUGGINGFACE_TOKEN="${HUGGINGFACE_TOKEN:-unused-lite-mode}"
}

ensure_live_deployment_stopped() {
    local running
    running=$(docker ps --format '{{.Names}}' --filter 'name=^opentranscribe-' || true)
    if [[ -n "$running" ]]; then
        gr_die "live deployment is still running:
$running

Stop it first with: ./opentr.sh stop
(this preserves all data — postgres bind mount, NAS minio, named volumes)"
    fi
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
    [[ -f "$TEST_SAMPLE_WAV" ]] || gr_die "test fixture missing: $TEST_SAMPLE_WAV"
}

phase_01_build_lite_images() {
    [[ -f "$REPO_ROOT/backend/Dockerfile.lite" ]] || gr_die "backend/Dockerfile.lite not found"

    if docker image inspect "davidamacey/opentranscribe-backend-lite:${LOCAL_IMAGE_TAG}" >/dev/null 2>&1; then
        gr_ok "backend-lite image already present"
    else
        gr_log "building lite backend image (no CUDA/faster-whisper; carries the ONNX export toolchain)"
        docker build \
            -t "davidamacey/opentranscribe-backend-lite:${LOCAL_IMAGE_TAG}" \
            -f "$REPO_ROOT/backend/Dockerfile.lite" \
            "$REPO_ROOT/backend"
    fi
    if docker image inspect "davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG}" >/dev/null 2>&1; then
        gr_ok "frontend image already present"
    else
        gr_log "building prod frontend image"
        docker build \
            -t "davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG}" \
            -f "$REPO_ROOT/frontend/Dockerfile.prod" \
            "$REPO_ROOT/frontend"
    fi
    docker image inspect "davidamacey/opentranscribe-backend-lite:${LOCAL_IMAGE_TAG}" \
        --format 'backend-lite digest: {{.Id}}' | tee "$TEST_ROOT/image-digests.txt"
    docker image inspect "davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG}" \
        --format 'frontend digest: {{.Id}}' | tee -a "$TEST_ROOT/image-digests.txt"
}

phase_02_run_one_liner() {
    # setup-opentranscribe.sh has no lite-specific mode — it installs the
    # standard prod layout. Phase 03 layers the lite + mock overlays onto
    # that install directory afterward, the same way fresh-install's phase 03
    # patches in the locally-built image tag.
    local install_dir="$TEST_ROOT/install"
    mkdir -p "$install_dir"
    pushd "$install_dir" >/dev/null

    gr_log "running setup-opentranscribe.sh from branch $TO_BRANCH in unattended mode"
    OPENTRANSCRIBE_BRANCH="$TO_BRANCH" \
    OPENTRANSCRIBE_UNATTENDED=1 \
    HUGGINGFACE_TOKEN="$HUGGINGFACE_TOKEN" \
    OPENSEARCH_MODELS="${OPENSEARCH_MODELS:-all-MiniLM-L6-v2}" \
    bash "$REPO_ROOT/setup-opentranscribe.sh" || gr_die "one-liner failed"

    popd >/dev/null
}

phase_03_pin_and_layer_overlays() {
    local target="$TEST_ROOT/install/opentranscribe"
    [[ -d "$target" ]] || target="$TEST_ROOT/install"
    [[ -f "$target/docker-compose.prod.yml" ]] || gr_die "no docker-compose.prod.yml under $target"

    cp "$target/docker-compose.prod.yml" "$target/docker-compose.prod.yml.bak"
    cp "$target/docker-compose.yml" "$target/docker-compose.yml.bak"

    cp_force_pull_policy "$target/docker-compose.prod.yml" never
    cp_inject_labels "$target/docker-compose.prod.yml" "$TEST_LABEL"
    cp_inject_labels "$target/docker-compose.yml" "$TEST_LABEL"
    # Frontend and the workers not overridden by docker-compose.lite.yml still
    # come from docker-compose.prod.yml, so pin those too.
    cp_pin_image_tag "$target/docker-compose.prod.yml" frontend "$LOCAL_IMAGE_TAG"
    for svc in celery-worker celery-beat flower; do
        cp_pin_image_tag "$target/docker-compose.prod.yml" "$svc" "$LOCAL_IMAGE_TAG" 2>/dev/null || true
    done

    # Copy the lite + mock overlays (and the scripts/fixtures they bind-mount)
    # into the install dir so the compose chain below can reference them with
    # the same relative paths used everywhere else in this repo.
    cp "$REPO_ROOT/docker-compose.lite.yml" "$target/docker-compose.lite.yml"
    cp "$REPO_ROOT/docker-compose.mock-asr.yml" "$target/docker-compose.mock-asr.yml"
    cp "$REPO_ROOT/docker-compose.mock-llm.yml" "$target/docker-compose.mock-llm.yml"
    # Native diarization sidecar (issue #660): lite's speaker embeddings come from
    # its CPU-EP /embed_window, not an in-process PyAnnote model (which the lite
    # image no longer ships as of the requirements-lite.txt shrink). Copied in the
    # same way as the lite/mock overlays above — this is one of the two places
    # (here and opentr.sh) allowed to reference docker-compose.diar-native.yml
    # under lite; see the header comment and the exemption in
    # test_compose_file_selection.py / test_compose_bringup_delegation.py.
    cp "$REPO_ROOT/docker-compose.diar-native.yml" "$target/docker-compose.diar-native.yml"
    mkdir -p "$target/scripts" "$target/backend/tests/fixtures/media"
    cp "$REPO_ROOT/scripts/mock-asr-server.py" "$target/scripts/mock-asr-server.py"
    cp "$REPO_ROOT/scripts/mock-llm-server.py" "$target/scripts/mock-llm-server.py"
    cp "$REPO_ROOT/backend/tests/fixtures/media/sample_transcript.json" \
        "$target/backend/tests/fixtures/media/sample_transcript.json"

    # Pin the locally-built lite image explicitly (docker-compose.lite.yml
    # defaults to :latest via BACKEND_LITE_IMAGE, which is never what a
    # release rehearsal should trust).
    # Pin the sidecar to the SAME locally-built lite image (issue #660 B4) — the
    # overlay's own default is the FULL backend image, and resolving
    # docker-compose.yml + prod + lite + diar-native without this override
    # produces the mismatched pair B4 describes (lite workers on one image, the
    # sidecar on another).
    {
        echo ""
        echo "BACKEND_LITE_IMAGE=davidamacey/opentranscribe-backend-lite:${LOCAL_IMAGE_TAG}"
        echo "DIAR_NATIVE_IMAGE=davidamacey/opentranscribe-backend-lite:${LOCAL_IMAGE_TAG}"
        echo "PULL_POLICY=never"
    } >> "$target/.env"

    # docker-compose.mock-asr.yml (as of Phase 3) already sets
    # GLADIA_API_BASE_URL: http://mock-asr:5198 directly on the backend and
    # celery-cloud-asr-worker services in the compose file itself — the app
    # never reads a .env value of the same name because
    # GladiaProvider._base resolves it at construction time regardless of
    # source, so a duplicate .env entry would be redundant, not additive.
    # Verified by reading docker-compose.mock-asr.yml rather than assumed.
    if ! grep -q 'GLADIA_API_BASE_URL:' "$target/docker-compose.mock-asr.yml"; then
        gr_die "docker-compose.mock-asr.yml no longer sets GLADIA_API_BASE_URL directly — .env now needs the override"
    fi
    gr_ok "GLADIA_API_BASE_URL is set by docker-compose.mock-asr.yml; no .env write needed"

    # Model cache: lite mode still runs the embedding worker + OpenSearch ML
    # model locally (search/chat retrieval), so seed those trees only —
    # never nltk_data via a raw hardlink (see mc_seed_cache/mc_assert_no_hardlinks
    # in lib/model-cache.sh; hardlinked nltk corpus files fail nltk>=3.10's
    # pathsec check and silently break every downstream job that touches it).
    # diar-native (issue #660): the CPU-EP sidecar's own export, ~484 MB of
    # ONNX/PLDA weights. Since issue #654, `requirements-lite.txt` ships the export
    # toolchain (pyannote.audio, onnx, onnxscript, onnxslim, onnxconverter-common), so a
    # lite deployment CAN provision its own weights on first boot -- this rehearsal
    # pre-seeds them anyway via DIAR_NATIVE_MODELS_DIR (exactly as documented in
    # .env.example) so the run is deterministic rather than depending on network access
    # to HuggingFace mid-rehearsal. Seeded from the shared cache below if present;
    # otherwise the sidecar will exit 8 against an empty --models-dir and phase_06b will
    # report it, not silently pass.
    local model_cache_dir
    model_cache_dir=$(awk -F= '/^MODEL_CACHE_DIR=/{print $2; exit}' "$target/.env")
    [[ -z "$model_cache_dir" || "$model_cache_dir" == "./models" ]] && model_cache_dir="$target/models"
    [[ "$model_cache_dir" != /* ]] && model_cache_dir="$target/${model_cache_dir#./}"
    mkdir -p "$model_cache_dir"/{sentence-transformers,opensearch-ml,diar-native}

    local shared_cache="/mnt/nvm/opentranscribe-test-runs/.shared-model-cache"
    if [[ -d "$shared_cache" && -f "$shared_cache/.seeded-from-live" ]]; then
        gr_log "seeding embedding/opensearch-ml/diar-native model cache from shared cache …"
        mc_seed_cache "$shared_cache" "$model_cache_dir" sentence-transformers opensearch-ml diar-native
        gr_ok "model cache seeded from $shared_cache"
    else
        gr_warn "shared model cache not found at $shared_cache — first start will download the embedding model"
    fi

    docker run --rm -v "$model_cache_dir:/m" busybox sh -c \
        "chown -R 1000:999 /m && chmod -R u+w /m" >/dev/null 2>&1 || \
        gr_warn "could not chown $model_cache_dir to 1000:999 (model downloads may fail)"

    local env_file="$target/.env"
    for kv in "INITIAL_ADMIN_EMAIL=$TEST_ADMIN_EMAIL" "INITIAL_ADMIN_PASSWORD=$TEST_ADMIN_PASSWORD" \
              "MOCK_ASR_PORT=$TEST_MOCK_ASR_PORT" "MOCK_LLM_PORT=$TEST_MOCK_LLM_PORT"; do
        local key="${kv%%=*}"
        if grep -q "^${key}=" "$env_file"; then
            sed -i "s|^${key}=.*|${kv}|" "$env_file"
        else
            echo "$kv" >> "$env_file"
        fi
    done
    gr_ok "bootstrap admin credential + mock ports set in .env"
}

phase_04_start_stack() {
    local target="$TEST_ROOT/install/opentranscribe"
    [[ -d "$target" ]] || target="$TEST_ROOT/install"
    pushd "$target" >/dev/null
    # The one bring-up in this directory that does NOT go through
    # `./opentranscribe.sh start`, and the header explains why: no shipped command can
    # select docker-compose.lite.yml (absent from release-manifest.txt, no branch in
    # get_compose_files()). Scenarios A and B were converted; this one cannot be until
    # lite becomes a real shipped mode. Do not "fix" it by copying their pattern — you
    # would be asking opentranscribe.sh for a chain it has no way to produce.
    gr_log "docker compose up -d (base + prod + lite + mock-asr + mock-llm, CPU-only)"
    docker compose \
        -f docker-compose.yml -f docker-compose.prod.yml \
        -f docker-compose.lite.yml \
        -f docker-compose.diar-native.yml \
        -f docker-compose.mock-asr.yml -f docker-compose.mock-llm.yml \
        up -d
    popd >/dev/null
}

phase_05_wait_for_health() {
    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    ac_wait_for_health 600
}

phase_06_topology_check() {
    local report="$TEST_ROOT/topology.txt"
    : > "$report"

    local verdict_json
    if verdict_json=$("$REPO_ROOT/scripts/lite-smoke.sh" --json 2>&1); then
        echo "lite-smoke.sh: PASS" | tee -a "$report"
        echo "$verdict_json" >> "$report"
    else
        local exit_code=$?
        if [[ $exit_code -eq 4 ]]; then
            echo "lite-smoke.sh: NOT MEASURED (exit 4) — treated as a failed precondition, never a pass" | tee -a "$report"
            gr_die "lite-smoke.sh reported NOT MEASURED — cannot certify lite topology: $verdict_json"
        else
            echo "lite-smoke.sh: FAIL (exit $exit_code)" | tee -a "$report"
            gr_die "lite-smoke.sh topology check failed: $verdict_json"
        fi
    fi

    local backend_image
    backend_image=$(docker inspect opentranscribe-backend --format '{{.Config.Image}}' 2>/dev/null || echo "")
    [[ "$backend_image" == *-lite* ]] || gr_die "backend image '$backend_image' does not contain '-lite' — lite overlay did not take effect"
    echo "backend image: $backend_image" | tee -a "$report"

    local gpu_worker
    gpu_worker=$(docker ps --format '{{.Names}}' \
        --filter "label=com.docker.compose.project=$(compose_project_name)" \
        --filter 'name=celery-worker-gpu' || true)
    [[ -z "$gpu_worker" ]] || gr_die "GPU worker container present ($gpu_worker) in a lite-mode rehearsal"
    echo "no GPU worker container: confirmed" | tee -a "$report"
}

phase_06b_sidecar_check() {
    # Issue #660: prove lite's speaker embeddings actually come from the CPU-EP
    # sidecar running the LITE image, not a full-image sidecar or an in-process
    # PyAnnote model that Step 7's shrink was supposed to remove.
    local report="$TEST_ROOT/topology.txt"

    local sidecar_name
    sidecar_name="$(overlay_container_name diar-native)"
    [[ -n "$sidecar_name" ]] || gr_die "no diar-native sidecar container found in project $(compose_project_name) — --with-diar-native overlay did not start it"

    local sidecar_image
    sidecar_image=$(docker inspect "$sidecar_name" --format '{{.Config.Image}}' 2>/dev/null || echo "")
    [[ "$sidecar_image" == *-lite* ]] || gr_die "diar-native sidecar image '$sidecar_image' does not contain '-lite' — B4's mismatched image pair (issue #660)"
    echo "diar-native sidecar image: $sidecar_image" | tee -a "$report"

    local healthz
    healthz=$(docker exec "$sidecar_name" curl -fsS http://localhost:8701/healthz 2>/dev/null || echo "")
    [[ -n "$healthz" ]] || gr_die "diar-native /healthz did not respond"
    # Assert the LOADED `devices`, never `supported_devices` — the latter is a build-time
    # capability list a CUDA-only sidecar also reports, which cannot distinguish a lite
    # CPU sidecar from a full CUDA one (the exact failure this check exists to catch;
    # see native_embedding_client.py's devices-vs-supported_devices note).
    [ "$(echo "$healthz" | grep -c '"devices":\["cpu"\]')" -gt 0 ] \
        || gr_die "diar-native /healthz does not report cpu as a LOADED device: $healthz"
    echo "diar-native /healthz reports cpu loaded: confirmed" | tee -a "$report"

    local cpu_worker_name
    cpu_worker_name=$(overlay_container_name celery-cpu-worker)
    if [[ -n "$cpu_worker_name" ]]; then
        # `grep -c ... -gt 0`, never `| grep -q`. This script runs under `set -o pipefail`, and
        # `grep -q` exits at its FIRST match — so `docker logs` is killed by SIGPIPE mid-stream
        # and the pipeline reports failure precisely when the message WAS found. Measured on
        # this host: `docker logs opentranscribe-celery-cpu-worker` is 3.9 MB, 60x the 64 KB
        # pipe buffer, so the producer is always still writing when grep leaves. Written the
        # old way this check could never once report success — it warned "has not yet logged
        # the sidecar-served message" whether or not the sidecar was serving embeddings, which
        # is the failure direction that hides a real regression behind familiar noise.
        # `grep -c` consumes the whole stream, so there is no early exit to race.
        if [ "$(docker logs "$cpu_worker_name" 2>&1 \
                  | grep -c "Speaker embeddings served by the diar-native sidecar")" -gt 0 ]; then
            echo "celery-cpu-worker logged the sidecar-served message: confirmed" | tee -a "$report"
        else
            gr_warn "celery-cpu-worker has not yet logged the sidecar-served message — expected once a speaker-embedding extraction has run (phase_07 triggers one)"
        fi
    else
        gr_warn "celery-cpu-worker container not found by name filter — cannot check its log"
    fi

    # The measured fact that makes the shrink real, not merely asserted: pyannote.audio
    # must be ABSENT from the lite image's site-packages post-Step-7.
    if docker exec "$cpu_worker_name" python3 -c 'import pyannote.audio' >/dev/null 2>&1; then
        gr_die "pyannote.audio is still importable inside celery-cpu-worker — the lite image was not rebuilt after requirements-lite.txt's Step 7 shrink, or the shrink regressed"
    fi
    echo "pyannote.audio is NOT importable in celery-cpu-worker: confirmed (issue #660 Step 7)" | tee -a "$report"
}

phase_07_pipeline_assertions() {
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
        echo "- Image tag: $LOCAL_IMAGE_TAG (local build)"
        echo "- Started: $(date -Iseconds)"
        echo ""
        echo "| Status | Assertion | Detail |"
        echo "|---|---|---|"
    } >> "$TEST_REPORT_FILE"
    export TEST_REPORT_FILE

    ac_login "$TEST_ADMIN_EMAIL" "$TEST_ADMIN_PASSWORD"

    local fe_code
    fe_code=$(curl -o /dev/null -s -w '%{http_code}' "http://localhost:${TEST_FRONTEND_PORT}/")
    as_assert_http "frontend GET /" 200 "$fe_code"

    local api_code docs_enabled
    # python-dotenv, not grep/cut (issue #590).
    docs_enabled=$(python3 "$SCRIPT_DIR/../lib/env_reader.py" \
        "$TEST_ROOT/install/opentranscribe/.env" ENABLE_API_DOCS \
        | tr '[:upper:]' '[:lower:]' || true)
    api_code=$(curl -o /dev/null -s -w '%{http_code}' "http://localhost:${TEST_BACKEND_PORT}/api/docs")
    if [[ "$docs_enabled" == "true" || "$docs_enabled" == "1" || "$docs_enabled" == "yes" ]]; then
        as_assert_http "backend GET /api/docs (ENABLE_API_DOCS opted in)" 200 "$api_code"
    else
        as_assert_http "backend /api/docs NOT exposed by default (hardened)" 404 "$api_code"
    fi

    # ASR config: register the mock Gladia stand-in as an active provider.
    local asr_config_uuid=""
    if asr_config_uuid=$(ac_create_asr_config gladia "http://mock-asr:5198" "mock-key-not-secret" "lite-rehearsal-asr"); then
        as_record PASS "ASR config created (provider=gladia, mocked base_url)"
    else
        as_record FAIL "ASR config creation"
    fi

    local file_uuid=""
    if [[ -n "$asr_config_uuid" ]]; then
        if file_uuid=$(ac_upload_file "$TEST_SAMPLE_WAV"); then
            as_record PASS "upload accepted: $(basename "$TEST_SAMPLE_WAV") (uuid=$file_uuid)"
        else
            as_record FAIL "upload $(basename "$TEST_SAMPLE_WAV")"
        fi
    fi

    if [[ -n "$file_uuid" ]]; then
        if ac_wait_for_file_status "$file_uuid" 600; then
            as_record PASS "transcription completed for file $file_uuid"

            # sample_short.wav through the mocked Gladia server always produces
            # the canned transcript proven in test_lite_mode_mocked_providers.py:
            # 7 segments, 2 speakers, "Zylofenix" present verbatim.
            local segments_json seg_count speaker_labels transcript_text
            segments_json=$(ac_get_segments "$file_uuid")
            seg_count=$(echo "$segments_json" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d.get("transcript_segments") or d.get("segments") or []))' 2>/dev/null || echo 0)
            as_assert_eq "segment count matches the canned mock transcript" "7" "$seg_count"

            transcript_text=$(echo "$segments_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
segs = d.get("transcript_segments") or d.get("segments") or []
print("Zylofenix" in " ".join(s.get("text", "") for s in segs))
' 2>/dev/null || echo "False")
            as_assert_eq "transcript text matches canned transcript (distinctive token present)" "True" "$transcript_text"

            speaker_labels=$(echo "$segments_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
segs = d.get("transcript_segments") or d.get("segments") or []
labels = {s.get("speaker_label") for s in segs if s.get("speaker_label")}
print(len(labels))
' 2>/dev/null || echo 0)
            as_assert_eq "exactly 2 speakers" "2" "$speaker_labels"

            # Issue #660: the speaker rows this lite pipeline just produced must carry
            # 256-dim (v4) vectors — proof the CPU-EP sidecar, not an absent in-process
            # PyAnnote model, actually served the embedding.
            local embedding_dim
            embedding_dim=$(docker exec opentranscribe-opensearch curl -s \
                'http://localhost:9200/speakers/_search' \
                -H 'Content-Type: application/json' \
                -d '{"size":1,"sort":[{"created_at":{"order":"desc"}}],"_source":["embedding"]}' \
                2>/dev/null \
                | python3 -c 'import sys,json; hits=json.load(sys.stdin).get("hits",{}).get("hits",[]); print(len((hits[0]["_source"].get("embedding") or [])) if hits else 0)' \
                2>/dev/null || echo 0)
            as_assert_eq "speaker embedding is 256-dim (v4, via the diar-native sidecar)" "256" "$embedding_dim"
        else
            as_record FAIL "transcription for file $file_uuid"
        fi

        # Search for the distinctive token — indexing runs as a follow-on task,
        # so poll briefly rather than asserting immediately after "completed".
        local found=0
        for _attempt in $(seq 1 20); do
            local hit_uuids
            hit_uuids=$(ac_search "Zylofenix" 2>/dev/null | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(",".join(r.get("file_uuid", "") for r in d.get("results") or []))
' 2>/dev/null || echo "")
            if [[ ",$hit_uuids," == *",$file_uuid,"* ]]; then
                found=1
                break
            fi
            sleep 3
        done
        as_assert_eq "search for 'Zylofenix' returns this file" "1" "$found"

        # Polled, not checked once: registering+deploying the model can take
        # 30s+ on a cold volume, and a one-shot check here measured a real
        # v0.5.0 run failing while the model was still mid-registration. See
        # test-fresh-install.sh's identical fix for the full measurement and
        # why 600s (not 300s): the shared opensearch-ml cache is never
        # seeded, so this always cold-downloads from the network.
        local ml_deployed=0 ml_wait=0
        while [ "$ml_wait" -lt 600 ]; do
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
        as_assert_ge "OpenSearch ML model deployed (neural search active)" "$ml_deployed" 1

        # Chat via the mocked LLM, grounded in the mocked-ASR transcript.
        local llm_config_uuid=""
        if llm_config_uuid=$(ac_create_llm_config custom mock-gpt "http://mock-llm:5199/v1" "mock-key-not-secret" "lite-rehearsal-llm"); then
            as_record PASS "LLM config created (mocked, provider=custom)"
            # ac_chat_completion needs cookie-session CSRF, not the bearer
            # token ac_login sets — fetch one via /api/auth/token the same
            # way test_lite_mode_mocked_providers.py's api_session fixture
            # does, and export it for the helper.
            local cookie_jar csrf_token
            cookie_jar=$(mktemp)
            curl -sS -c "$cookie_jar" -X POST "$API_BASE/auth/token" \
                -d "username=${TEST_ADMIN_EMAIL}&password=${TEST_ADMIN_PASSWORD}" >/dev/null
            csrf_token=$(grep -oP 'csrf_token\s+\K\S+' "$cookie_jar" || true)
            rm -f "$cookie_jar"
            if [[ -n "$csrf_token" ]]; then
                export API_CSRF_TOKEN="$csrf_token"
                local chat_out answer citation_count chat_error
                chat_out=$(ac_chat_completion "$llm_config_uuid" "$file_uuid" "What was discussed?" || true)
                answer=$(ac_json_field "$chat_out" answer)
                citation_count=$(ac_json_field "$chat_out" citations)
                chat_error=$(ac_json_field "$chat_out" error)
                if [[ -n "$chat_error" ]]; then
                    # GH #595: an `event: error` frame means the turn ended for a
                    # REASON, not that the model produced nothing — most commonly
                    # the SSRF guard correctly refusing mock-llm's private-network
                    # address (LLM_ALLOW_PRIVATE_ENDPOINTS=false is the default, and
                    # correct, posture). Report the real cause instead of letting
                    # the empty-answer assertion below stand in for it.
                    as_record FAIL "chat completion" "LLM call ended in an error frame: $chat_error"
                else
                    as_assert "chat summary non-empty" "[[ -n \"$answer\" ]]"
                    as_assert_ge "chat turn has at least one citation" "${citation_count:-0}" 1
                fi
            else
                as_record FAIL "chat completion" "could not obtain csrf_token cookie for chat session"
            fi
        else
            as_record FAIL "LLM config creation"
        fi
    fi

    local alembic_head expected_head
    alembic_head=$(docker exec opentranscribe-postgres \
        psql -U postgres -d opentranscribe -tAc \
        "SELECT version_num FROM alembic_version" 2>/dev/null || echo "")
    expected_head=$(ver_alembic_head "$REPO_ROOT/backend")
    as_assert_eq "alembic head" "$expected_head" "$alembic_head"

    # Negative path: mock-asr's scenario is selected at server-start via
    # MOCK_ASR_SCENARIO (see the "known deviation" note in the plan this
    # script was written from — the mock has no per-request scenario hook
    # reachable from GladiaProvider itself). Restart the mock-asr container
    # with the error scenario for this one check, then restart it back to
    # "ok" afterward so nothing else in this phase is affected.
    local target="$TEST_ROOT/install/opentranscribe"
    [[ -d "$target" ]] || target="$TEST_ROOT/install"
    pushd "$target" >/dev/null
    MOCK_ASR_SCENARIO=error docker compose \
        -f docker-compose.yml -f docker-compose.prod.yml \
        -f docker-compose.lite.yml \
        -f docker-compose.diar-native.yml \
        -f docker-compose.mock-asr.yml -f docker-compose.mock-llm.yml \
        up -d --force-recreate --no-deps mock-asr
    popd >/dev/null
    sleep 3

    local error_file_uuid=""
    if error_file_uuid=$(ac_upload_file "$TEST_SAMPLE_WAV"); then
        local error_status
        error_status=$(
            local deadline=$(( $(date +%s) + 120 ))
            local s="unknown"
            while (( $(date +%s) < deadline )); do
                s=$(ac_curl "$API_BASE/files/$error_file_uuid" 2>/dev/null \
                    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || echo "")
                [[ "$s" == "error" || "$s" == "failed" ]] && break
                sleep 5
            done
            echo "$s"
        )
        if [[ "$error_status" == "error" || "$error_status" == "failed" ]]; then
            as_record PASS "negative-path upload reaches error status ($error_status)"
            local error_msg
            error_msg=$(ac_curl "$API_BASE/files/$error_file_uuid" 2>/dev/null \
                | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("last_error_message") or d.get("error_message") or "")' 2>/dev/null || echo "")
            # Key-free: must not leak the mock's api key placeholder verbatim.
            if [[ "$error_msg" != *"mock-key-not-secret"* ]]; then
                as_record PASS "error message contains no leaked API key"
            else
                as_record FAIL "error message leaked the API key" "$error_msg"
            fi
        else
            as_record FAIL "negative-path upload did not reach error status (got '$error_status')"
        fi
        ac_curl -X DELETE "$API_BASE/files/$error_file_uuid" >/dev/null 2>&1 || true
    else
        as_record FAIL "negative-path upload"
    fi

    pushd "$target" >/dev/null
    MOCK_ASR_SCENARIO=ok docker compose \
        -f docker-compose.yml -f docker-compose.prod.yml \
        -f docker-compose.lite.yml \
        -f docker-compose.diar-native.yml \
        -f docker-compose.mock-asr.yml -f docker-compose.mock-llm.yml \
        up -d --force-recreate --no-deps mock-asr
    popd >/dev/null

    # as_summary deliberately returns 1 when any assertion FAILed. Under
    # set -euo pipefail, a non-zero return from either stage of
    # `as_summary | tee -a ...` trips set -e right here -- phase_08_finish (the
    # informational "stack left running" banner) still follows this phase, so
    # unlike test-upgrade.sh's last-phase case this would abort the WHOLE
    # remaining run, not just skip a "Finished:" line. Same class as #617/#618,
    # ported from test-upgrade.sh's phase_18_summary.
    RELEASE_TEST_EXIT_CODE=0
    as_summary | tee -a "$TEST_REPORT_FILE" || RELEASE_TEST_EXIT_CODE=$?
    {
        echo ""
        echo "Finished: $(date -Iseconds)"
    } >> "$TEST_REPORT_FILE"
}

phase_08_finish() {
    echo
    echo "Stack left running for inspection at: $TEST_ROOT/install/opentranscribe"
    echo "Tear down with: $0 --cleanup"
    echo "Then restart your live deployment with: ./opentr.sh start dev"
}

# ─── Driver ─────────────────────────────────────────────────────────────────
mkdir -p "$TEST_ROOT"
exec > >(tee -a "$TEST_ROOT/run.log") 2>&1

echo "OpenTranscribe Release Test — Scenario C (lite-mode full rehearsal, mocked cloud ASR + LLM)"
echo "Started: $(date -Iseconds)"
echo "Repo:    $REPO_ROOT (commit $(git -C "$REPO_ROOT" rev-parse --short HEAD))"
echo

ensure_secrets_file

phase 00 phase_00_preflight
phase 01 phase_01_build_lite_images
phase 02 phase_02_run_one_liner
phase 03 phase_03_pin_and_layer_overlays
phase 04 phase_04_start_stack
phase 05 phase_05_wait_for_health
phase 06 phase_06_topology_check
phase 06b phase_06b_sidecar_check
phase 07 phase_07_pipeline_assertions
phase 08 phase_08_finish

echo
echo "Done. Report: $TEST_ROOT/REPORT.md"
echo "Stack left running for inspection. Tear down with: $0 --cleanup"
echo "Then restart your live deployment with: ./opentr.sh start dev"

# Propagate phase 07's assertion verdict as the script's own exit code (see
# phase_07_pipeline_assertions's comment). Without this, the capture above makes
# the script exit 0 even when an assertion FAILed -- worse than the truncation bug
# it fixes (silently green instead of noisily truncated). Defaults to 0 for a
# resumed run where phase 07 was already marked done and skipped.
exit "${RELEASE_TEST_EXIT_CODE:-0}"
