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
# image (because we haven't pushed it to Docker Hub yet) and force
# pull_policy: never. After Phase 5 of the release pipeline pushes the new
# tag to Docker Hub, this patching could be skipped entirely.
#
# The stack is then brought up with `./opentranscribe.sh start` — the SHIPPED command,
# the one setup-opentranscribe.sh's own prompt_start() runs. Not a hand-built
# `docker compose -f ...` list (that made the shipped overlay selector dead code at
# rehearsal time) and not `./opentr.sh` (dev-only, never present in a curl install).
# See scripts/release-tests/REHEARSAL_ALIGNMENT_PLAN.md.
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
# The docs service publishes ${DOCS_PORT:-5183}. It was missing from TEST_PORTS, so the
# preflight's "is the field clear?" check did not cover the one service this scenario
# never asserted on either (see phase 06's docs assertions).
TEST_DOCS_PORT="${DOCS_PORT:-5183}"
TEST_PORTS="$TEST_FRONTEND_PORT $TEST_BACKEND_PORT $TEST_FLOWER_PORT $TEST_POSTGRES_PORT $TEST_REDIS_PORT $TEST_MINIO_PORT $TEST_MINIO_CONSOLE_PORT $TEST_OPENSEARCH_PORT $TEST_DOCS_PORT"

# Test admin user
# Default admin user is created by the backend on first start.
TEST_ADMIN_EMAIL="${TEST_ADMIN_EMAIL:-admin@example.com}"
TEST_ADMIN_PASSWORD="${TEST_ADMIN_PASSWORD:-password}"

# Test media: directory of small real media files (mp3/m4a/wav/mp4) to upload
# and transcribe. Files are copied from this dir into the test container via
# multipart upload — this exercises the same code path a real user uses when
# dragging a file into the UI. Files in this dir are NOT committed to git.
TEST_MEDIA_DIR="${TEST_MEDIA_DIR:-/mnt/nvm/opentranscribe-test-runs/test-media}"
# See the matching note in test-upgrade.sh: the old hardcoded 5M excluded every
# realistic multi-speaker sample, so diarization was never exercised on
# production-like input. Bounds run time; not a smallness requirement.
TEST_MEDIA_MAX_SIZE="${TEST_MEDIA_MAX_SIZE:-100M}"

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
       TEST_DOCS_PORT TEST_PORTS

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
# shellcheck source=lib/compose-chain.sh
source "$LIB_DIR/compose-chain.sh"

# Resolve the version under test now that versions.sh is available.
if [[ -z "$LOCAL_IMAGE_TAG" ]]; then
    LOCAL_IMAGE_TAG="$(ver_to_version)"
fi
LOCAL_IMAGE_TAG="$(ver_normalize "$LOCAL_IMAGE_TAG")"
# Pins EVERY service via ${OT_IMAGE_TAG:-latest}, not just the ones named in
# cp_pin_image_tag's hand-maintained list (which misses docs and the GPU workers).
export OT_TEST_IMAGE_TAG="$LOCAL_IMAGE_TAG"
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
    # The docs image is part of the release (scripts/docker-build-push.sh's `all`
    # target builds backend, frontend AND docs), and docker-compose.prod.yml runs it as
    # a first-class service. It was missing here, which combined with the .env pin below
    # to make the docs container silently run whatever `davidamacey/opentranscribe-docs:latest`
    # happened to be on the host — four months stale on this one — or, on a host with no
    # such image, fall back to `build: context: ./docs-site`, a directory the installer
    # never downloads. Neither outcome was asserted on. See REHEARSAL_ALIGNMENT_PLAN.md
    # finding D.
    if docker image inspect "davidamacey/opentranscribe-docs:${LOCAL_IMAGE_TAG}" >/dev/null 2>&1; then
        gr_ok "docs image davidamacey/opentranscribe-docs:${LOCAL_IMAGE_TAG} already present"
    else
        gr_log "building docs"
        # OT_VERSION drives the homepage version badge; DOCS_BASE_URL keeps internal
        # links correct when nginx proxies the site at /docs/. Same build args
        # scripts/docker-build-push.sh:build_docs passes — omitting them produces an
        # image that renders an empty badge and broken links.
        docker build \
            -t "davidamacey/opentranscribe-docs:${LOCAL_IMAGE_TAG}" \
            --build-arg "OT_VERSION=${LOCAL_IMAGE_TAG}" \
            --build-arg DOCS_BASE_URL=/docs/ \
            "$REPO_ROOT/docs-site"
    fi
    docker image inspect "davidamacey/opentranscribe-backend:${LOCAL_IMAGE_TAG}" \
        --format 'backend digest: {{.Id}}' | tee "$TEST_ROOT/image-digests.txt"
    docker image inspect "davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG}" \
        --format 'frontend digest: {{.Id}}' | tee -a "$TEST_ROOT/image-digests.txt"
    docker image inspect "davidamacey/opentranscribe-docs:${LOCAL_IMAGE_TAG}" \
        --format 'docs digest: {{.Id}}' | tee -a "$TEST_ROOT/image-digests.txt"
}

phase_02_run_one_liner() {
    local install_dir="$TEST_ROOT/install"
    mkdir -p "$install_dir"
    pushd "$install_dir" >/dev/null

    # TEST_USE_GPU=false now goes through the installer's REAL opt-out flag rather than
    # being reinterpreted later by the harness. `--cpu` persists FORCE_CPU_MODE=true to
    # .env, which is the signal opentranscribe.sh's get_compose_files() reads to skip
    # the GPU overlay even where docker advertises an nvidia runtime. Before this, that
    # whole path had zero rehearsal coverage: the harness simply omitted
    # docker-compose.gpu.yml from a list it built itself, which exercises nothing.
    local installer_args=()
    if [[ "$TEST_USE_GPU" != "true" ]]; then
        installer_args+=(--cpu)
        gr_log "TEST_USE_GPU=false — installing with the documented --cpu opt-out"
    fi

    gr_log "running setup-opentranscribe.sh from branch $TO_BRANCH in unattended mode"
    OPENTRANSCRIBE_BRANCH="$TO_BRANCH" \
    OPENTRANSCRIBE_UNATTENDED=1 \
    HUGGINGFACE_TOKEN="$HUGGINGFACE_TOKEN" \
    WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}" \
    OPENSEARCH_MODELS="${OPENSEARCH_MODELS:-all-MiniLM-L6-v2}" \
    GPU_DEVICE_ID="$TEST_GPU_DEVICE_ID" \
    bash "$REPO_ROOT/setup-opentranscribe.sh" "${installer_args[@]+"${installer_args[@]}"}" \
        || gr_die "one-liner failed"

    popd >/dev/null
}

phase_03_pin_local_image() {
    # Minimal post-setup patch: pin the version under test via .env, force the right
    # pull_policy, inject the release-test label so cleanup can find managed resources.
    # No name/port/volume rewrites — the one-liner's defaults are used as-is because the
    # live deployment is stopped.
    local target="$TEST_ROOT/install/opentranscribe"
    [[ -d "$target" ]] || target="$TEST_ROOT/install"
    [[ -f "$target/docker-compose.prod.yml" ]] || gr_die "no docker-compose.prod.yml under $target"

    cp "$target/docker-compose.prod.yml" "$target/docker-compose.prod.yml.bak"

    # ONE line pins EVERY service.
    #
    # This replaces a hand-maintained per-service `cp_pin_image_tag` list
    # (backend + frontend + 9 celery services) that omitted `docs` and
    # `celery-worker-gpu-scaled`. Those two resolved `${OT_IMAGE_TAG:-latest}` against
    # the installer's own value — and this scenario installs with
    # OPENTRANSCRIBE_BRANCH=master, which makes resolve_install_ref() write
    # OT_IMAGE_TAG=latest — so the docs container ran whatever `:latest` happened to be
    # on the host (four months stale here), and nothing asserted on it.
    # REHEARSAL_ALIGNMENT_PLAN.md finding D.
    #
    # Every service in docker-compose.prod.yml resolves ${OT_IMAGE_TAG:-latest};
    # test_every_prod_service_image_is_tag_pinnable guards that statically, so this
    # single write is complete BY CONSTRUCTION where the list could only ever be
    # complete by vigilance.
    if grep -q '^OT_IMAGE_TAG=' "$target/.env"; then
        sed -i "s|^OT_IMAGE_TAG=.*|OT_IMAGE_TAG=${LOCAL_IMAGE_TAG}|" "$target/.env"
    else
        echo "OT_IMAGE_TAG=${LOCAL_IMAGE_TAG}" >> "$target/.env"
    fi
    # `./opentranscribe.sh version` falls back to this file when the stack is down.
    echo "${LOCAL_IMAGE_TAG}" > "$target/VERSION"

    if [[ "$USE_HUB_IMAGES" == "true" ]]; then
        # Hub mode: remove any cached local image first so Docker is forced to
        # actually pull from the registry. pull_policy: always ensures a fresh pull.
        gr_log "hub mode: removing cached local images to force Hub pull"
        docker image rm -f \
            "davidamacey/opentranscribe-backend:${LOCAL_IMAGE_TAG}" \
            "davidamacey/opentranscribe-frontend:${LOCAL_IMAGE_TAG}" \
            "davidamacey/opentranscribe-docs:${LOCAL_IMAGE_TAG}" \
            "davidamacey/opentranscribe-backend:latest" \
            "davidamacey/opentranscribe-frontend:latest" \
            "davidamacey/opentranscribe-docs:latest" 2>/dev/null || true
        cp_force_pull_policy "$target/docker-compose.prod.yml" always
        cp_inject_labels "$target/docker-compose.prod.yml" "$TEST_LABEL"
        gr_ok "pull_policy=always, OT_IMAGE_TAG pinned to Hub :${LOCAL_IMAGE_TAG}"
    else
        cp_force_pull_policy "$target/docker-compose.prod.yml" never
        cp_inject_labels "$target/docker-compose.prod.yml" "$TEST_LABEL"
        gr_ok "OT_IMAGE_TAG pinned to :${LOCAL_IMAGE_TAG}, pull_policy=never, label injected"
    fi

    # Also label the base file's services for cleanup symmetry
    cp "$target/docker-compose.yml" "$target/docker-compose.yml.bak"
    cp_inject_labels "$target/docker-compose.yml" "$TEST_LABEL"

    # Pre-create the model cache directory. Ownership is DELIBERATELY left alone here —
    # repairing it is `opentranscribe.sh start`'s job (fix_model_cache_permissions),
    # and phase 04 asserts the outcome. This scenario used to run its own
    # `docker run --rm busybox chown -R 1000:999`, which is a reimplementation of the
    # shipped fix: it made the shipped one a no-op (it early-returns when the directory
    # already belongs to UID 1000), so a regression in the code every real user depends
    # on was invisible. REHEARSAL_ALIGNMENT_PLAN.md finding B.
    local model_cache_dir
    model_cache_dir=$(awk -F= '/^MODEL_CACHE_DIR=/{print $2; exit}' "$target/.env")
    [[ -z "$model_cache_dir" || "$model_cache_dir" == "./models" ]] && model_cache_dir="$target/models"
    [[ "$model_cache_dir" != /* ]] && model_cache_dir="$target/${model_cache_dir#./}"
    mkdir -p "$model_cache_dir"/{huggingface,torch,nltk_data,sentence-transformers,opensearch-ml,diar-native}

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
        gr_log "seeding model cache from shared cache …"
        # Hardlinks for the big trees; a real copy for nltk_data (nltk >=3.10
        # pathsec refuses multiply-linked files) and for diar-native (its
        # provisioning step can rewrite the exported .onnx/.npy files in
        # place — issue #670). See lib/model-cache.sh for both stories.
        # diar-native is included here so this scenario proves the FAST path
        # (weights already present) rather than always paying for — and
        # depending on the reliability of — a live HuggingFace export at
        # backend startup; ac_diar_engine_verdict below is what actually
        # proves diarization worked, seeded or not.
        mc_seed_cache "$shared_cache" "$model_cache_dir" \
            huggingface torch nltk_data sentence-transformers pyannote opensearch-ml diar-native
        gr_ok "model cache seeded from $shared_cache"
    else
        gr_warn "shared model cache not found at $shared_cache — first start will download models"
    fi

    # Recorded so phase 04 can check what the SHIPPED permission fix did to it.
    echo "$model_cache_dir" > "$TEST_ROOT/model-cache-dir.txt"
    gr_ok "model cache pre-created at $model_cache_dir (ownership left to opentranscribe.sh start)"

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

    # Supply the bootstrap admin credential.
    #
    # A fresh install is HARDENED (ENVIRONMENT defaults to production), and
    # app/initial_data.py deliberately refuses to seed the well-known
    # admin@example.com/password there — it generates a random 192-bit password
    # instead and prints it once. This scenario previously assumed the dev
    # credential ("The backend creates a default admin ... so registration is not
    # needed"), which is true only in a relaxed environment, so phase 06 died at
    # `curl: (22) ... 401` on every run.
    #
    # INITIAL_ADMIN_PASSWORD is the documented way to bootstrap a hardened
    # deployment with a known credential, so setting it here exercises the real
    # supported path rather than scraping the generated password out of the logs.
    # It must be written BEFORE phase 04 starts the stack: the admin is seeded on
    # first backend boot.
    local env_file="$target/.env"
    for kv in "INITIAL_ADMIN_EMAIL=$TEST_ADMIN_EMAIL" "INITIAL_ADMIN_PASSWORD=$TEST_ADMIN_PASSWORD"; do
        local key="${kv%%=*}"
        if grep -q "^${key}=" "$env_file"; then
            sed -i "s|^${key}=.*|${kv}|" "$env_file"
        else
            echo "$kv" >> "$env_file"
        fi
    done
    gr_ok "bootstrap admin credential set for the hardened install ($TEST_ADMIN_EMAIL)"
}

# The report is opened by phase 04 (the first phase that asserts anything) and appended
# to by phase 06. Idempotent within a process so a `--force` re-run does not stack two
# headers on one file, and a resumed run that skips phase 04 still gets a header.
REPORT_INITIALISED=0
_init_report() {
    TEST_REPORT_FILE="$TEST_ROOT/REPORT.md"
    export TEST_REPORT_FILE
    (( REPORT_INITIALISED == 0 )) || return 0
    REPORT_INITIALISED=1
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
}

# _assert_model_cache_repaired MODEL_CACHE_DIR
#   The SHIPPED fix_model_cache_permissions (invoked by `opentranscribe.sh start`) is
#   what a real user relies on to make the bind-mounted cache writable by appuser
#   (UID 1000). Assert its OUTCOME, then repair anything it left behind so a product
#   defect here is reported rather than silently costing a 3-hour run at the first
#   model download.
_assert_model_cache_repaired() {
    local cache="$1"
    [[ -d "$cache" ]] || { as_record SKIP "model cache repaired by the shipped permission fix" "no cache dir at $cache"; return 0; }
    local stray
    stray="$(find "$cache" ! -uid 1000 -print -quit 2>/dev/null || true)"
    if [[ -z "$stray" ]]; then
        as_record PASS "model cache is owned by UID 1000 after './opentranscribe.sh start'"
        return 0
    fi
    as_record FAIL "model cache is owned by UID 1000 after './opentranscribe.sh start'" \
        "first offender: $stray — opentranscribe.sh's fix_model_cache_permissions left it"
    gr_warn "repairing the model cache so the rest of the run still measures something"
    docker run --rm -v "$cache:/m" busybox sh -c \
        "chown -R 1000:999 /m && chmod -R u+w /m" >/dev/null 2>&1 \
        || gr_warn "repair chown failed too — model downloads will probably fail"
}

phase_04_start_stack() {
    local target="$TEST_ROOT/install/opentranscribe"
    [[ -d "$target" ]] || target="$TEST_ROOT/install"
    _init_report

    # ── Start the stack the way a real self-hoster does. ───────────────────────
    #
    # `./opentranscribe.sh start` is THE shipped entry point: setup-opentranscribe.sh
    # places it next to the compose files and its own prompt_start() runs exactly this
    # command (docs-site/docs/getting-started/quick-start.md).
    #
    # It is deliberately NOT `./opentr.sh` — that is the DEVELOPMENT script, is absent
    # from release-manifest.txt on purpose, and no curl install has it. See
    # _stage_manager_at in test-upgrade.sh for the measured reason.
    #
    # It is also deliberately NOT a hand-built `docker compose -f ... up -d`. That is
    # what this used to be, and it made `get_compose_files()` — the shipped selector for
    # GPU vs Blackwell vs CPU-only, nginx and the scheduled-backup overlay — dead code
    # at rehearsal time, along with `fix_model_cache_permissions` and the first-run MinIO
    # KMS guard that `start` also runs. release-manifest.txt's header records what that
    # costs: a Blackwell fresh install silently ran the wrong image and no rehearsal
    # could have seen it. Full write-up: REHEARSAL_ALIGNMENT_PLAN.md finding A.
    #
    # Rehearsal-specific setup (image-tag pin, pull_policy, labels, seeded model cache)
    # stays in phase 03 where it belongs — none of it belongs in the shipped script.
    pushd "$target" >/dev/null
    gr_log "running './opentranscribe.sh start' (the shipped entry point)"
    ./opentranscribe.sh start || { popd >/dev/null; gr_die "'./opentranscribe.sh start' failed"; }
    popd >/dev/null

    # What did the shipped selector actually choose? Asserted here, before the 900s
    # health wait, so a wrong chain fails in seconds rather than at the end.
    cc_assert_chain "fresh install" "$target" "$REPO_ROOT"

    if [[ -f "$TEST_ROOT/model-cache-dir.txt" ]]; then
        _assert_model_cache_repaired "$(cat "$TEST_ROOT/model-cache-dir.txt")"
    fi
}

phase_05_wait_for_health() {
    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    ac_wait_for_health 900
}

phase_06_api_smoke() {
    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    _init_report

    # Log in as the bootstrap admin. The credential works because phase 03 wrote
    # INITIAL_ADMIN_EMAIL/INITIAL_ADMIN_PASSWORD into the install's .env — a
    # hardened deployment (which this is) refuses the well-known dev credential
    # and generates a random password instead. See phase_03.
    ac_login "$TEST_ADMIN_EMAIL" "$TEST_ADMIN_PASSWORD"

    local fe_code
    fe_code=$(curl -o /dev/null -s -w '%{http_code}' "http://localhost:${TEST_FRONTEND_PORT}/")
    as_assert_http "frontend GET /" 200 "$fe_code"

    # The docs service, which nothing in this scenario used to touch at all.
    #
    # It is a first-class service in docker-compose.prod.yml with its own published
    # port and healthcheck, it ships as a published image alongside backend/frontend,
    # and `show_access_info` tells every user to visit it. Three separate ways it could
    # be broken and go unnoticed until a user hit it: never built (phase 01 skipped it),
    # never pinned (the per-service pin list omitted it, so it ran a stale `:latest`),
    # and never asserted. REHEARSAL_ALIGNMENT_PLAN.md finding D.
    local docs_image docs_code
    docs_image="$(docker inspect opentranscribe-docs --format '{{.Config.Image}}' 2>/dev/null || echo "<not running>")"
    as_assert_eq "docs container runs the version under test" \
        "davidamacey/opentranscribe-docs:${LOCAL_IMAGE_TAG}" "$docs_image"
    if docs_code=$(curl -o /dev/null -s -w '%{http_code}' --max-time 15 \
                        "http://localhost:${TEST_DOCS_PORT}/docs/"); then
        as_assert_http "docs site serves /docs/" 200 "$docs_code"
    else
        as_record FAIL "docs site serves /docs/" "curl failed against http://localhost:${TEST_DOCS_PORT}/docs/"
    fi

    # API docs are a SECURITY surface, not a liveness check.
    #
    # main.py:_resolve_docs_urls publishes none of /api/docs, /api/redoc or
    # /api/openapi.json in a hardened deployment — Swagger is anonymously
    # reachable wherever it is mounted and enumerates the whole admin/auth attack
    # surface. A fresh install IS hardened, so 404 is the CORRECT answer and this
    # previously asserted 200, failing every run on intended behaviour.
    #
    # Assert whichever the deployment is configured for, so the check keeps
    # meaning either way: exposed when opted in, hidden by default.
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
                    -size "-$TEST_MEDIA_MAX_SIZE" | head -2)
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
                    seg_count=$(ac_segment_count "$fid")
                    as_assert_ge "segments[] non-empty for $fid" "$seg_count" 1
                else
                    as_record FAIL "transcription for file $fid"
                fi
            done

            # Which engine actually diarized these files? "completed" above proves
            # nothing — the fallback to in-process PyAnnote is SILENT BY DESIGN, so a
            # stack whose diarizer is dead can still pass every assertion above
            # (issue #670). Two independent, non-redundant signals — see
            # diar-native-smoke.sh's and ac_diar_engine_verdict's own headers for why
            # neither is sufficient alone.
            #
            # GPU residency is only meaningful when this run actually asked for a GPU
            # deployment: TEST_USE_GPU=false (--cpu) legitimately runs diar-native in
            # CPU mode, holding zero device memory, which diar-native-smoke.sh would
            # correctly but unhelpfully report as FAIL.
            if [[ "$TEST_USE_GPU" == "true" ]]; then
                # Wrapped in `if`, never called bare: diar-native-smoke.sh exits
                # non-zero on both FAIL (1) and NOT MEASURED (4), and this script runs
                # under `set -euo pipefail` — an unwrapped non-fatal-by-design helper
                # call here would silently truncate every phase after it (issues
                # #617/#618; see scripts/CLAUDE.md's "bare helper call" gotcha).
                local diar_smoke_rc=0 diar_smoke_out=""
                if diar_smoke_out=$("$REPO_ROOT/scripts/diar-native-smoke.sh" --json 2>&1); then
                    diar_smoke_rc=0
                else
                    diar_smoke_rc=$?
                fi
                case "$diar_smoke_rc" in
                    0) as_record PASS "diar-native sidecar GPU residency (diar-native-smoke.sh)" ;;
                    4) as_record SKIP "diar-native sidecar GPU residency" "NOT MEASURED: $diar_smoke_out" ;;
                    *) as_record FAIL "diar-native sidecar GPU residency" "$diar_smoke_out" ;;
                esac
            fi

            # Verdict is per-file (issue #706's diarization_provider column),
            # not a whole-worker-log grep, so the check is against the
            # specific file just proven "completed" — not whichever job
            # last happened to log a line in a 30-minute window. The last
            # uploaded file id stands in for "the file(s) that just
            # completed": a fresh install here uploads at most a couple of
            # small clips, so verifying one is representative of the batch.
            # This scenario always installs the CURRENT release, whose API always ships
            # #706's diarization_provider column (see ac_diar_engine_verdict's header) —
            # so a `:log` verdict here is never the legitimate old-stack case, only a
            # signal something is wrong. `native:log` therefore does NOT get an
            # unqualified PASS: an unscoped 30-minute log grep can report PASS from an
            # unrelated earlier job, which is exactly the false-pass hole #706 was
            # written to close (issue #707).
            local diar_verdict diar_verdict_source
            diar_verdict=$(ac_diar_engine_verdict "$fid" "opentranscribe-celery-worker" "30m")
            diar_verdict_source="${diar_verdict#*:}"
            case "$diar_verdict" in
                native:db)
                    as_record PASS "native diarizer served the completed file ($fid, source=$diar_verdict_source)"
                    ;;
                native:log)
                    as_record FAIL "native diarizer served the completed file ($fid)" \
                        "verdict=native:log — an unscoped legacy worker-log grep, not the per-file diarization_provider column. A fresh install of the current release should always carry that column; a :log result here means the DB-backed check was unavailable, not that diarization succeeded. Never trusted as a pass (issue #707)"
                    ;;
                pyannote:db)
                    as_record FAIL "native diarizer served the completed file ($fid)" \
                        "media_file.diarization_provider=pyannote — PyAnnote served this job (direct config or a silent native fallback)"
                    ;;
                fallback:log)
                    as_record FAIL "native diarizer served the completed file ($fid)" \
                        "worker log shows a 'falling back to PyAnnote' line — the sidecar degraded silently (no diarization_provider column on this API; legacy log-based check)"
                    ;;
                none:db)
                    as_record FAIL "native diarizer served the completed file ($fid)" \
                        "media_file.diarization_provider is NULL after completion — diarization never resolved a provider"
                    ;;
                error:request)
                    as_record FAIL "native diarizer served the completed file ($fid)" \
                        "ac_diar_engine_verdict's request to /api/files/$fid failed or returned unparseable JSON (verdict=error:request) — this is a request failure, not evidence the diarization_provider column is absent, and is never silently downgraded to the log fallback"
                    ;;
                absent:none)
                    as_record FAIL "native diarizer served the completed file ($fid)" \
                        "file record unreachable and opentranscribe-celery-worker is not running"
                    ;;
                unknown:db|unknown:log)
                    as_record FAIL "native diarizer served the completed file ($fid)" \
                        "could not determine which engine served this job (verdict=$diar_verdict)"
                    ;;
                *)
                    as_record FAIL "native diarizer served the completed file ($fid)" \
                        "unrecognized verdict from ac_diar_engine_verdict: $diar_verdict"
                    ;;
            esac

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
            #
            # Polled, not checked once: a fresh install is strictly colder than
            # an upgrade (no registered model in the OpenSearch volume, empty
            # /ml-models/ mount), and registering+deploying a ~92MB model can
            # take 30s+ on its own. A one-shot check here measured a real
            # v0.5.0 run failing at ~35s elapsed while the model was still
            # mid-registration -- hybrid search itself passed via BM25
            # fallback the whole time. Unlike test-upgrade.sh (180s poll,
            # warmer stack), a fresh install can never seed the shared
            # opensearch-ml cache -- mc_seed_cache's live-cache source
            # deliberately skips it as "container-specific" (see the
            # comment beside its call in test-upgrade.sh's own seeding),
            # so /ml-models/ is always empty and registration always goes
            # the cold remote-download route, whose duration depends on
            # network conditions rather than local disk. Measured: even
            # 300s (ml_model_service._REGISTRATION_MAX_WAIT) was not
            # always enough on this host under concurrent build/scan
            # load. 600s gives real headroom for that variance; costs
            # nothing on a healthy run -- exits on the first successful poll.
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

    # as_summary deliberately returns 1 when any assertion FAILed. This is the LAST
    # phase, and under set -euo pipefail a non-zero return from either stage of
    # `as_summary | tee -a ...` trips set -e right here — silently skipping the
    # "Finished:" line and this phase's own done-marker, and (worse) leaving the
    # driver's own exit code at whatever the last unrelated command happened to
    # return, which is not the assertion verdict at all (same class as #617/#618,
    # ported from test-upgrade.sh's phase_18_summary).
    RELEASE_TEST_EXIT_CODE=0
    as_summary | tee -a "$TEST_REPORT_FILE" || RELEASE_TEST_EXIT_CODE=$?
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

# Propagate phase 06's assertion verdict as the script's own exit code (see
# phase_06_api_smoke's comment). Without this, the capture above makes the script
# exit 0 even when an assertion FAILed -- worse than the truncation bug it fixes
# (silently green instead of noisily truncated). Defaults to 0 for a resumed run
# where phase 06 was already marked done and skipped.
exit "${RELEASE_TEST_EXIT_CODE:-0}"
