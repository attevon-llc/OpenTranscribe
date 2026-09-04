#!/bin/bash
# Scenario B — in-place upgrade from the previous published release to this one.
#
# What this proves:
#   A real user with real data on the previous release can run the documented
#   upgrade path (`./opentranscribe.sh update` ≈ `compose down/pull/up`) and
#   find their MinIO objects, transcripts, speakers, and search indices intact
#   after the migration chain runs — AND, if the upgrade goes wrong, that the
#   documented recovery path (`opentranscribe.sh backup`/`restore`,
#   `opentranscribe.sh update --rollback`) actually works (issue #598, and issue
#   #613 — backup/restore moved from opentr.sh, which a real production install
#   never has, into opentranscribe.sh, the script it actually ships with).
#
# Phases:
#   00  preflight + secrets gate
#   01  build local $TO_VERSION images (skipped if already present from Scenario A)
#   02  verify Docker Hub has :$FROM_VERSION tags
#   03  create $FROM_VERSION worktree, copy compose into TEST_ROOT, patch
#   04  generate isolated .env, start the $FROM_VERSION stack, wait for health
#   05  register user, upload media via URL, wait for completion
#   06  snapshot pre-upgrade state (postgres SELECTs, MinIO ETags, transcripts)
#   06b take the pre-upgrade backup — the rollback oracle (shipped pg_dump
#       recipe + opentranscribe.sh backup, cross-checked; scratch-restore
#       verified; issue #613 — this is the SHIPPED command now, not opentr.sh)
#   07  down $FROM_VERSION, swap compose to current head, re-patch, point to local images
#   08  './opentranscribe.sh update --version $TO_VERSION' — the real upgrade
#       path, which also records the rollback bookkeeping phase 12 checks
#   09  snapshot post-upgrade state
#   10  diff snapshots, run feature liveness checks, write REPORT.md
#   11  new upload + transcription on the upgraded stack (does it still work?)
#   12  assert the rollback precondition (# OT_PREVIOUS_IMAGE_TAG) was recorded
#   13  stage the rollback tree (unpinned images); verify the TO-side backup too
#   14  damage the database through the real API (delete a file, rename a speaker)
#   15  restore the phase-06b backup; assert EXACT pre-backup state (content
#       digests, not row counts) — R-1..R-2, R-6..R-13, gated by ROLLBACK_REHEARSAL.
#       Issue #610: restore now leaves the app STOPPED on a schema-head mismatch
#       (R-13 asserts this directly), so R-3/R-4/R-5 — which need a running
#       application to serve the restored data — moved to phase 16.
#   16  './opentranscribe.sh update --rollback'; assert the FROM image serves
#       the restored FROM database through its real API — B-1..B-8, plus
#       R-3/R-4/R-5 (moved from phase 15, issue #610)
#   17  roll forward again — the documented recovery loop, end to end — F-1..F-5
#   18  summary
#
# ROLLBACK_REHEARSAL=0 / --no-rollback skips 13-17 (12 and 18 still run).
# ROLLBACK_INJECT_FAULT=truncate|no-damage|stale-oracle deliberately breaks
# one input of the tail so its own failure detection is exercised for real —
# see selftest-rollback-fault-injection.sh for the fast (~1 min), no-live-stack
# version of that same proof.
#
# Future releases need NO edits: FROM and TO are discovered (see the Tunables
# block). FROM_VERSION / TO_VERSION override; FROM_VERSIONS (plural) runs the
# scenario once per source, for multi-hop / oldest-supported coverage.
#
# Exit codes — the contract scripts/release.sh and scripts/test-matrix.sh share:
#   0 every assertion PASSed · 1 an assertion FAILed or a guardrail refused ·
#   2 misuse (unknown argument, --only-rollback without a completed TEST_ROOT) ·
#   4 operator abort (declined the I UNDERSTAND prompt)
# Preconditions that a real operator can clear (live containers up, ports bound, disk
# space) currently exit 1 rather than the contract's 3 — see gr_die in lib/guardrails.sh.

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

# FROM/TO are DISCOVERED, not hardcoded.
#
# The old defaults (v0.3.3 -> 0.4.0) were correct for exactly one release and
# then quietly tested the wrong thing. GitLab deleted their equivalent CI job for
# precisely this reason: it read the previous version from a file that went stale
# and silently validated an upgrade nobody was performing.
#
# TO   = the VERSION file (the release being cut; its tag does not exist yet and
#        its images are not on Hub, so nothing else can name it).
# FROM = the newest git tag below TO that ALSO has published Docker Hub images.
#        A tag with no images is not something a user could be running, so it is
#        not a valid upgrade source.
#
# Both remain overridable. FROM_VERSIONS (plural, space-separated) runs the whole
# scenario once per source, which is how the oldest-supported hop keeps being
# exercised after auto-detection moves FROM forward.
FROM_VERSION="${FROM_VERSION:-}"
FROM_VERSIONS="${FROM_VERSIONS:-}"
LOCAL_IMAGE_TAG="${LOCAL_IMAGE_TAG:-}"
# Set REQUIRE_PREVIOUS=1 to turn "no published previous release" into a failure
# rather than a skip. The release gate sets it; a first-ever release does not.
REQUIRE_PREVIOUS="${REQUIRE_PREVIOUS:-0}"

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
# Upper bound on a single test media file.
#
# This was a hardcoded 5M, which quietly excluded every realistic sample: real
# multi-speaker material is tens of megabytes, so the harness was only ever
# transcribing short single-speaker clips and never exercised diarization on
# anything resembling production input. The cap exists to bound run time, not
# to keep files small for their own sake — so it is a knob, and a generous one.
TEST_MEDIA_MAX_SIZE="${TEST_MEDIA_MAX_SIZE:-100M}"

DO_CLEANUP=0
DO_FORCE=0
ONLY_ROLLBACK=0
# ROLLBACK_REHEARSAL gates phases 13-17 (issue #598): the backup/restore +
# `update --rollback` tail. Defaults ON — it is part of what a release
# rehearsal is now expected to prove — with an explicit opt-out for anyone who
# only wants the forward-upgrade proof phases 00-11 already gave.
ROLLBACK_REHEARSAL="${ROLLBACK_REHEARSAL:-1}"
# ROLLBACK_INJECT_FAULT deliberately breaks one step of the rollback tail so the
# tail's own failure detection is exercised for real rather than assumed — a
# leg that silently asserts nothing looks exactly like a leg that passes.
# Modes: truncate | no-damage | stale-oracle (see phases 14/15). Unset = off.
ROLLBACK_INJECT_FAULT="${ROLLBACK_INJECT_FAULT:-}"
while (( $# > 0 )); do
    case "$1" in
        --cleanup) DO_CLEANUP=1 ;;
        --force)   DO_FORCE=1 ;;
        --yes)     export OT_RELEASE_TEST_YES=1 ;;
        --no-rollback)   ROLLBACK_REHEARSAL=0 ;;
        --only-rollback) ONLY_ROLLBACK=1 ;;
        --help|-h)
            cat <<EOF
Usage: $0 [--cleanup] [--force] [--yes] [--no-rollback|--only-rollback]

Prerequisite: stop the live deployment first with \`./opentr.sh stop\`.
This scenario runs under the one-liner's stock container names and ports so it
exercises what a real user gets; it cannot run alongside the live stack.
After the test, restart it with \`./opentr.sh start dev\` (or whichever
mode you were using).

Env:
  TEST_PROJECT_NAME      default ot-reltest-upgrade  (used as label namespace)
  TEST_ROOT              default /mnt/nvm/opentranscribe-test-runs/<name>-<ts>
  FROM_VERSION           auto: newest git tag below TO that has Docker Hub images
  FROM_VERSIONS          space-separated list; runs the scenario once per source
  TO_VERSION             auto: the VERSION file
  LOCAL_IMAGE_TAG        alias for TO_VERSION (locally built tag for the "after" stack)
  REQUIRE_PREVIOUS       1 = fail instead of skip when no previous release exists
  FRONTEND_PORT..        default 5173-5180 (one-liner defaults; see README)
  ROLLBACK_REHEARSAL     0 = skip phases 13-17 (the backup/restore + rollback tail)
  ROLLBACK_INJECT_FAULT  truncate|no-damage|stale-oracle — self-check, breaks the
                         tail on purpose so its failure detection is exercised
  --no-rollback          same as ROLLBACK_REHEARSAL=0
  --only-rollback        resume at phase 12, reusing an already-completed
                         TEST_ROOT's phase-00..11 markers (run the full
                         scenario first; this does not fabricate that state)
EOF
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

if (( ONLY_ROLLBACK == 1 )); then
    ROLLBACK_REHEARSAL=1
    if [[ ! -f "$TEST_ROOT/.phase/11.done" ]]; then
        echo "--only-rollback requires a TEST_ROOT that already completed phase 11." >&2
        echo "Run the full scenario first (or point TEST_ROOT at one that did):" >&2
        echo "  TEST_ROOT=$TEST_ROOT $0" >&2
        exit 2
    fi
fi
export ROLLBACK_REHEARSAL ROLLBACK_INJECT_FAULT

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
# shellcheck source=lib/versions.sh
source "$LIB_DIR/versions.sh"
# shellcheck source=lib/model-cache.sh
source "$LIB_DIR/model-cache.sh"
# shellcheck source=lib/db-snapshot.sh
source "$LIB_DIR/db-snapshot.sh"
# shellcheck source=lib/compose-chain.sh
source "$LIB_DIR/compose-chain.sh"

if (( DO_CLEANUP == 1 )); then
    gr_log "cleanup requested"
    gr_cleanup
    exit 0
fi

# ─── Resolve FROM / TO ──────────────────────────────────────────────────────
#
# Done here, after the libs are sourced and before any phase runs, so a bad
# assumption fails in seconds rather than 40 minutes into the scenario.

if [[ -z "$LOCAL_IMAGE_TAG" ]]; then
    LOCAL_IMAGE_TAG="$(ver_to_version)"
fi
LOCAL_IMAGE_TAG="$(ver_normalize "$LOCAL_IMAGE_TAG")"
TO_VERSION="$LOCAL_IMAGE_TAG"

if [[ -z "$FROM_VERSION" ]]; then
    if FROM_VERSION="$(TO_VERSION="$TO_VERSION" ver_previous_version)"; then
        ver_warn_if_unreleased "$FROM_VERSION"
    else
        if [[ "$REQUIRE_PREVIOUS" == "1" ]]; then
            gr_die "no published previous release found below $TO_VERSION, and REQUIRE_PREVIOUS=1"
        fi
        gr_warn "no published previous release below $TO_VERSION — nothing to upgrade FROM"
        gr_warn "this is expected for a first release; set REQUIRE_PREVIOUS=1 to make it fatal"
        mkdir -p "$TEST_ROOT"
        {
            echo "# Upgrade scenario — SKIPPED"
            echo
            echo "| Status | Assertion | Detail |"
            echo "|---|---|---|"
            echo "| SKIP | upgrade path | no published release below $TO_VERSION |"
        } > "$TEST_ROOT/REPORT.md"
        exit 0
    fi
fi
FROM_VERSION="$(ver_normalize "$FROM_VERSION")"

if ! ver_lt "$FROM_VERSION" "$TO_VERSION"; then
    gr_die "FROM ($FROM_VERSION) must be strictly older than TO ($TO_VERSION) — the migration chain is one-way"
fi

# The head the FROM release shipped with, derived from ITS OWN migration chain in
# phase 03's worktree, then compared against what the running FROM stack reports.
# That measured-vs-derived pair replaced expected-schemas.tsv, a hand-maintained
# table that nothing read and that never got its v0.4.1 row.
gr_ok "upgrade path: $FROM_VERSION  ->  $TO_VERSION"

# The generated .env pins every service through ${OT_IMAGE_TAG:-latest}. It is
# re-written for each side of the upgrade: the FROM stack must run FROM_VERSION
# throughout, the upgraded stack TO_VERSION throughout. Without this the services
# outside cp_pin_image_tag's hardcoded list (docs, the three GPU workers) would
# resolve to :latest on BOTH sides and the upgrade would not actually be tested.
export OT_TEST_IMAGE_TAG="$FROM_VERSION"

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
    #
    # `diar-native` is the only project service with no `container_name`, so compose gives
    # it its DEFAULT name (`<project>-diar-native-<n>`) instead — which matches this
    # `^opentranscribe-` regex only when the compose project is actually named
    # `opentranscribe` (true for this rehearsal, which pins COMPOSE_PROJECT_NAME=opentranscribe
    # below, but not for a real install whose directory is named something else). Also catch
    # it by compose project label so this stays correct if that pin ever changes.
    local running running_diar
    running=$(docker ps --format '{{.Names}}' --filter 'name=^opentranscribe-' || true)
    running_diar=$(docker ps --format '{{.Names}}' \
        --filter 'label=com.docker.compose.project=opentranscribe' \
        --filter 'label=com.docker.compose.service=diar-native' || true)
    if [[ -n "$running" || -n "$running_diar" ]]; then
        gr_die "live opentranscribe-* containers are running:
$running
$running_diar

Stop them with: ./opentr.sh stop  (preserves all data)"
    fi
    # Remove stopped opentranscribe-* containers (would collide on container_name)
    local stopped stopped_diar
    stopped=$(docker ps -a --format '{{.Names}}' --filter 'name=^opentranscribe-' || true)
    stopped_diar=$(docker ps -a --format '{{.Names}}' \
        --filter 'label=com.docker.compose.project=opentranscribe' \
        --filter 'label=com.docker.compose.service=diar-native' || true)
    stopped="$(printf '%s\n%s' "$stopped" "$stopped_diar" | sed '/^$/d' | sort -u)"
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

phase_01b_build_docs_image() {
    # docs is a released image like any other (scripts/docker-build-push.sh's `all`
    # target builds backend, frontend AND docs), so the upgrade should move it the same
    # way — by tag. Without this, the after-stack has no :$LOCAL_IMAGE_TAG docs image
    # and falls back to docker-compose.prod.yml's `build: context: ./docs-site`, which
    # rehearses a Docusaurus build rather than the image upgrade a real user gets.
    if docker image inspect "davidamacey/opentranscribe-docs:${LOCAL_IMAGE_TAG}" >/dev/null 2>&1; then
        gr_ok "docs image davidamacey/opentranscribe-docs:${LOCAL_IMAGE_TAG} already built"
        return
    fi
    gr_log "building local docs image ${LOCAL_IMAGE_TAG}"
    # Same build args scripts/docker-build-push.sh:build_docs passes — omitting OT_VERSION
    # renders an empty version badge, omitting DOCS_BASE_URL breaks links behind nginx.
    docker build -t "davidamacey/opentranscribe-docs:${LOCAL_IMAGE_TAG}" \
        --build-arg "OT_VERSION=${LOCAL_IMAGE_TAG}" \
        --build-arg DOCS_BASE_URL=/docs/ \
        "$REPO_ROOT/docs-site"
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
    [[ -f "$worktree/docker-compose.prod.yml" ]] || gr_die "$FROM_VERSION worktree missing docker-compose.prod.yml"
    cp "$worktree/docker-compose.prod.yml" "$stage/docker-compose.prod.yml"

    # Some older releases mount ./database/init_db.sql into postgres for
    # first-boot bootstrapping (v0.3.3 did; newer releases use Alembic
    # exclusively). FEATURE-DETECTED rather than version-gated, so this needs no
    # edit as FROM moves forward and still works if an old FROM is pinned.
    if [[ -d "$worktree/database" ]]; then
        rm -rf "$stage/database"
        cp -r "$worktree/database" "$stage/database"
        gr_ok "copied database/ bootstrap from the $FROM_VERSION worktree"
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

    # GPU overlay (use the FROM worktree's copy if present, else current head's)
    if [[ "$TEST_USE_GPU" == "true" ]]; then
        local src_gpu="$worktree/docker-compose.gpu.yml"
        [[ -f "$src_gpu" ]] || src_gpu="$REPO_ROOT/docker-compose.gpu.yml"
        cp "$src_gpu" "$stage/docker-compose.gpu.yml"
        gr_ok "GPU overlay copied from $(basename "$(dirname "$src_gpu")")"
    fi

    # Stage the FROM release's OWN management script, plus whatever else that release
    # shipped alongside it, so phase 04 can start this stack the way a real user on
    # $FROM_VERSION starts theirs.
    #
    # It must be FROM's copy, not HEAD's. A deployment keeps running the script it was
    # installed with until the operator runs `update-full`, which is the one command
    # that re-downloads it (docs-site/docs/operations/upgrading.md). Using HEAD's here
    # meant the FROM stack was driven by a script that release never shipped — so a
    # regression in FROM's own `start`/`get_compose_files` was invisible, and so was any
    # incompatibility between an older script and a newer compose file.
    # REHEARSAL_ALIGNMENT_PLAN.md finding C.
    #
    # scripts/common.sh is FEATURE-DETECTED, not version-gated: FROM releases before
    # issue #613 have no such file and their opentranscribe.sh does not source it.
    [[ -f "$worktree/opentranscribe.sh" ]] \
        || gr_die "$FROM_VERSION worktree has no opentranscribe.sh — that release had no shipped management script, so this scenario cannot represent a real user of it"
    cp "$worktree/opentranscribe.sh" "$stage/opentranscribe.sh"
    chmod +x "$stage/opentranscribe.sh"
    if [[ -f "$worktree/scripts/common.sh" ]]; then
        mkdir -p "$stage/scripts"
        cp "$worktree/scripts/common.sh" "$stage/scripts/common.sh"
        chmod +x "$stage/scripts/common.sh"
    fi
    gr_ok "staged $FROM_VERSION's own opentranscribe.sh at $stage"

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
             "$model_cache/opensearch-ml" "$model_cache/pyannote" \
             "$model_cache/diar-native"

    local live_cache="/mnt/nvm/repos/transcribe-app/models"

    # One-time seed from live production cache if we haven't already. Check
    # for the sentinel file ".seeded-from-live" to avoid re-copying on every
    # run. mc_seed_cache hardlinks the big trees (cheap, no data duplication)
    # but makes a REAL copy of nltk_data — nltk >=3.10 pathsec refuses
    # multiply-linked files, and a hardlinked nltk_data fails every
    # transcription in the run — and of diar-native, whose export can be
    # rewritten in place by an older FROM release's provisioning step, which
    # would corrupt this host's LIVE diar-native weights through the
    # hardlink (issue #670; see lib/model-cache.sh's MC_NO_HARDLINK_SUBDIRS
    # header for the full reasoning).
    if [[ ! -f "$model_cache/.seeded-from-live" ]]; then
        if [[ -d "$live_cache/huggingface" ]]; then
            gr_log "seeding shared model cache from live cache (one-time)"
            # diar-native is included so this scenario proves the FAST path
            # (weights already present, no live HuggingFace export at
            # backend startup) rather than depending on that export
            # succeeding every run. ac_diar_engine_verdict in phase 11 is
            # what actually proves diarization worked, seeded or not.
            # Skip opensearch-ml (container-specific) and onnx (newer releases only).
            mc_seed_cache "$live_cache" "$model_cache" \
                huggingface torch nltk_data sentence-transformers pyannote diar-native
            touch "$model_cache/.seeded-from-live"
            gr_ok "shared model cache seeded from live cache"
        else
            gr_warn "no live model cache to seed from; models will download from HF on first boot"
            touch "$model_cache/.seeded-from-live"  # mark as attempted
        fi
    else
        gr_ok "reusing persistent shared model cache at $shared_cache"
        # A cache seeded by an OLDER revision of this script may still be
        # hardlinked. The sentinel means "seeded", not "seeded correctly", so
        # re-assert the invariant on every reuse rather than trusting it.
        mc_break_hardlinks "$model_cache/nltk_data"

        # diar-native did not exist as a seeded subdir before issue #670's fix,
        # so a shared cache whose sentinel predates this change never gets it
        # from the branch above (the sentinel means "seeded", not "seeded
        # completely" either). Top it up incrementally rather than requiring
        # an operator to blow away the whole multi-GB cache to pick up one
        # new subdir.
        if [[ -z "$(ls -A "$model_cache/diar-native" 2>/dev/null)" ]]; then
            if [[ -d "$live_cache/diar-native" ]] && [[ -n "$(ls -A "$live_cache/diar-native" 2>/dev/null)" ]]; then
                gr_log "shared cache predates diar-native seeding — topping it up from the live cache"
                mc_seed_cache "$live_cache" "$model_cache" diar-native
                gr_ok "diar-native seeded into the existing shared model cache"
            else
                gr_warn "no diar-native export in the live cache either — the backend will export its own on startup"
            fi
        fi
    fi

    # Gate: whichever branch ran above, the pathsec invariant must hold before
    # a single container starts, so a regression fails here rather than as an
    # opaque transcription error ten minutes later.
    mc_assert_no_hardlinks "$model_cache/nltk_data" "shared model cache"

    docker run --rm -v "$model_cache:/models" busybox:latest \
        sh -c "chown -R 1000:999 /models && chmod -R 755 /models" >/dev/null 2>&1 \
        || gr_warn "could not chown model cache (may need sudo)"

    # Generate a .env for the FROM stack with isolated credentials.
    cat > "$stage/.env" <<EOF
# Auto-generated by test-upgrade.sh phase 3
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
# The persisted CPU-only opt-out setup-opentranscribe.sh --cpu writes, and the signal
# the TO release's get_compose_files() reads to skip the GPU overlay. The FROM release
# may predate it and simply not find a GPU overlay staged (phase 03 only stages one when
# TEST_USE_GPU=true); the TO side honours this, so both halves of the upgrade agree on
# the topology instead of the harness deciding by withholding a file.
FORCE_CPU_MODE=$([[ "$TEST_USE_GPU" == "true" ]] && echo false || echo true)
COMPUTE_TYPE=float16
BATCH_SIZE=16
LLM_PROVIDER=
# Required by _validate_production_secrets on BOTH sides of the upgrade. Omitting
# it made the v0.5.0 backend refuse to start after the upgrade while v0.4.1 booted
# fine from the same file -- v0.4.1's gate was fail-OPEN (ENVIRONMENT defaulted to
# "development"), v0.5.0's is fail-CLOSED. That divergence is a real breaking
# change for users and is tracked in #410; this line is about the harness
# representing a correctly-configured deployment, not about hiding it.
REDIS_PASSWORD=$(openssl rand -hex 16)
# Pins every service image. This scenario writes its own .env rather than using
# lib/env-template.sh, so it needs its own copy of this line; phase 07 rewrites it
# to the target version when the stack is swapped.
OT_IMAGE_TAG=${OT_TEST_IMAGE_TAG:-latest}
EOF
    chmod 600 "$stage/.env"
    gr_ok "$FROM_VERSION compose staged at $stage"
}

phase_04_start_from_stack() {
    local stage="$TEST_ROOT/before"

    # ── Start the FROM stack with the FROM release's OWN shipped script. ───────
    #
    # `./opentranscribe.sh start` is what a real user on $FROM_VERSION runs; phase 03
    # staged that release's own copy of it beside its own compose files. Two things
    # this replaces, both of which were second implementations of shipped logic:
    #
    #   * a hand-built `-f docker-compose.yml -f docker-compose.prod.yml [-f
    #     docker-compose.gpu.yml]` chain keyed on the harness's own TEST_USE_GPU, which
    #     left FROM's get_compose_files() — GPU vs Blackwell vs CPU-only, nginx — never
    #     executed by a release gate; and
    #   * an explicit `compose pull`. Phase 03 sets pull_policy: always on the FROM prod
    #     overlay, so `up` performs the real Docker Hub pull on its own. That is also
    #     what a real user's `start` does.
    #
    # `./opentr.sh` is deliberately not used anywhere in this scenario: it is the
    # DEVELOPMENT script, absent from release-manifest.txt on purpose, and no curl
    # install has it (see _stage_manager_at's comment for the measured reason).
    # Full write-up: REHEARSAL_ALIGNMENT_PLAN.md finding A/C.
    pushd "$stage" >/dev/null
    gr_log "running '${FROM_VERSION}'s own ./opentranscribe.sh start (real user path, pulls from Docker Hub)"
    ./opentranscribe.sh start || { popd >/dev/null; gr_die "'${FROM_VERSION} opentranscribe.sh start' failed"; }
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
    # SORTED, then take the first two. `find` alone returns directory order,
    # which is arbitrary — so without the sort, WHICH files get seeded varies
    # between runs on the same machine. Phase 11 then cannot reliably reserve
    # an unseeded file, and the "new data post-upgrade" assertion would
    # silently degrade to re-uploading something already present.
    done < <(find "$TEST_MEDIA_DIR" -maxdepth 1 -type f \
                \( -iname "*.mp3" -o -iname "*.m4a" -o -iname "*.mp4" \
                   -o -iname "*.wav" -o -iname "*.flac" -o -iname "*.ogg" \) \
                 -size "-$TEST_MEDIA_MAX_SIZE" | sort | head -2)
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
    # Recorded so phase 11 can pick a file that was NOT seeded here. If it
    # re-uploaded a seeded file, a pass could come from pre-upgrade state
    # rather than from the upgraded stack doing new work.
    printf '%s\n' "${media_files[@]##*/}" > "$TEST_ROOT/seeded-media-names.txt"
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

    # API surface: the sorted "METHOD /path" set from the running stack's
    # OpenAPI document. Diffed in phase 10 to catch a route that disappeared
    # across the upgrade — a break for every existing client, and something no
    # data-level assertion can see.
    #
    # Tolerant of absence: a hardened deployment serves no openapi.json
    # (ENABLE_API_DOCS=false), and an old FROM may not expose it at this path.
    # Phase 10 records SKIP rather than failing when either side is empty.
    curl -fsS --max-time 15 "http://localhost:${TEST_BACKEND_PORT}/api/openapi.json" 2>/dev/null \
        | python3 -c '
import json, sys
try:
    spec = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for path, methods in sorted(spec.get("paths", {}).items()):
    for method in sorted(methods):
        if method.lower() in {"get", "post", "put", "patch", "delete"}:
            print(f"{method.upper()} {path}")
' > "$out/routes.txt" 2>/dev/null || : > "$out/routes.txt"
    gr_log "  captured $(wc -l < "$out/routes.txt") API routes"

    # Runtime build identity, so the report says what was actually running on
    # each side rather than what we believe we deployed.
    curl -fsS --max-time 10 "http://localhost:${TEST_BACKEND_PORT}/api/version" \
        > "$out/version.json" 2>/dev/null \
        || echo '{"version":"unavailable"}' > "$out/version.json"

    # Postgres deterministic queries (the one-liner uses the stock
    # 'opentranscribe-postgres' container name and 'postgres' superuser).
    # All queries are tolerant of missing tables: an old FROM may predate
    # alembic_version (bootstrapped via init_db.sql) or lack newer tables.
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

# _capture_minio_etags OUT_PATH
#   Same MinIO ETag capture snapshot_state uses, factored out so the rollback
#   tail (phase 15, R-11) can call it around a DB-only restore without
#   re-running the rest of snapshot_state.
_capture_minio_etags() {
    local out="$1"
    local minio="opentranscribe-minio"
    docker exec "$minio" sh -c '
        mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1 || true
        mc ls --recursive --json local/opentranscribe 2>/dev/null
    ' > "$out" 2>/dev/null || echo "[]" > "$out"
}

# _stage_manager_at SRC_STAGE_DIR
#   (Re)stages a copy of opentranscribe.sh + scripts/common.sh under TEST_ROOT, paired
#   with BOTH compose files + .env from SRC_STAGE_DIR (e.g. $TEST_ROOT/before or
#   $TEST_ROOT/after) so `docker compose exec ...` inside the staged opentranscribe.sh
#   resolves the SAME running project/containers this scenario already has up.
#   Echoes the staged directory.
#
#   Renamed from _stage_opentr_at (issue #613). opentr.sh is deliberately NOT on
#   release-manifest.txt — its backup/restore path uses bare `docker compose` with no
#   -f chain, which only works in a repo clone (docker-compose.override.yml auto-loads
#   there and supplies image:/build: for every application service). A curl install has
#   no override file, so the base compose project alone is INVALID (measured: `docker
#   compose -f docker-compose.yml exec -T postgres echo hi` fails with "service ... has
#   neither an image nor a build context specified"). This function used to stage ONLY
#   docker-compose.yml for exactly that reason — which meant phase 06b was rehearsing a
#   broken command shape and never noticed, because SRC_STAGE_DIR's postgres container
#   was already running from an EARLIER, correctly-chained `docker compose up` (phase
#   04/08's own compose_args includes docker-compose.prod.yml), so `exec` still found
#   it despite the invalid project. The fix stages opentranscribe.sh (which resolves its
#   own compose chain via get_compose_files(), including docker-compose.prod.yml) and
#   both compose files, so this rehearses the command a real user actually has.
#
#   SECOND PARAMETER (optional): the directory to take opentranscribe.sh + common.sh
#   FROM, defaulting to $REPO_ROOT (i.e. HEAD, the TO release). Callers pass the FROM
#   worktree when the command being rehearsed is one that release actually shipped —
#   see _manager_source_for below, which decides that by FEATURE-DETECTING the arm
#   rather than by comparing versions.
_stage_manager_at() {
    local src_stage="$1"
    local script_src="${2:-$REPO_ROOT}"
    local dst="$TEST_ROOT/manager-stage"
    mkdir -p "$dst/scripts" "$dst/backups"
    cp "$script_src/opentranscribe.sh" "$dst/opentranscribe.sh"
    chmod +x "$dst/opentranscribe.sh"
    cp "$script_src/scripts/common.sh" "$dst/scripts/common.sh"
    cp "$src_stage/docker-compose.yml" "$dst/docker-compose.yml"
    [[ -f "$src_stage/docker-compose.prod.yml" ]] \
        || gr_die "$src_stage missing docker-compose.prod.yml — opentranscribe.sh's " \
                  "get_compose_files() needs it, and a base-only compose project is " \
                  "invalid (issue #613)"
    cp "$src_stage/docker-compose.prod.yml" "$dst/docker-compose.prod.yml"
    cp "$src_stage/.env" "$dst/.env"
    echo "$dst"
}

# _manager_source_for ARM
#   Echo the directory whose opentranscribe.sh should drive a given command, preferring
#   the FROM release's own copy and falling back to HEAD's when that release did not
#   ship the command at all.
#
#   FEATURE-DETECTED, never version-gated. Measured against the real FROM release
#   (v0.4.1): its opentranscribe.sh has no `backup|restore` arm — issue #613 moved
#   backup/restore into this script for v0.5.0 — and no `--version` handling in
#   `update`. So a v0.4.1 user genuinely CANNOT run `./opentranscribe.sh backup`, which
#   is exactly why upgrading.md also gives the raw `docker compose exec ... pg_dump`
#   recipe and why phase 06b takes BOTH artifacts. Asking the file whether it dispatches
#   the arm means this needs no edit as FROM moves forward: from v0.5.0 onward the
#   answer flips to "yes" on its own and the rehearsal becomes strictly more faithful.
#
#   Also requires scripts/common.sh, since _stage_manager_at copies it unconditionally
#   and the backup/restore implementation lives there.
_manager_source_for() {
    local arm="$1"
    local from_dir="$TEST_ROOT/worktree-${FROM_VERSION}"
    if [[ -f "$from_dir/opentranscribe.sh" && -f "$from_dir/scripts/common.sh" ]] \
       && grep -qE "^[[:space:]]*[a-z|-]*\b${arm}\b[a-z|-]*\)" "$from_dir/opentranscribe.sh"; then
        echo "$from_dir"
    else
        echo "$REPO_ROOT"
    fi
}

# ─── Phase 06b: the pre-upgrade backup — the rollback oracle (issue #598) ───
#
# Runs while the FROM stack is up and seeded, exactly where upgrading.md's
# step 1 tells a real user to take a backup before upgrading. Two artifacts,
# because two different audiences run two different procedures (#598 §2.2,
# amended by #613): an installed deployment copy-pastes the raw pg_dump recipe
# upgrading.md gives it as a fallback, and can ALSO use `./opentranscribe.sh
# backup` — the shipped wrapper around that same recipe, and (since #613) the
# only backup command a real production install actually has.
phase_06b_pre_upgrade_backup() {
    local pg="opentranscribe-postgres"
    local db_user="postgres"
    local db_name="opentranscribe"
    local backups_dir="$TEST_ROOT/backups"
    mkdir -p "$backups_dir"

    # issue #617: phase_05_seed_data's wait loop only waits for status=completed —
    # that flip is what FIRES postprocess.py's _dispatch_speaker_attributes, not
    # a signal that it has finished. Both backup artifacts below and the
    # dbs_fingerprint oracle at the end of this phase read the `speaker` table,
    # so wait for that fire-and-forget detection to settle on every seeded file
    # BEFORE taking any of them — otherwise the "before" snapshot can land
    # mid-write and produce a false digest mismatch in phase 15/17.
    if [[ -s "$TEST_ROOT/seeded-file-ids.txt" ]]; then
        local seeded_uuids=()
        while IFS= read -r f; do
            seeded_uuids+=("$f")
        done < "$TEST_ROOT/seeded-file-ids.txt"
        dbs_wait_for_speaker_attributes "$pg" "$db_user" "$db_name" 300 "${seeded_uuids[@]}" \
            || gr_warn "proceeding with the pre-upgrade backup even though speaker attribute detection had not settled — a digest mismatch below is now recorded, not fatal (Fix 1, issue #617)"
    else
        gr_warn "no $TEST_ROOT/seeded-file-ids.txt — skipping the speaker-attribute settle wait"
    fi

    # issue #619: the SAME class of race as the speaker-table one above, for two more
    # tables. media_file has several async post-completion writers of its own (not
    # individually tracked, unlike the speaker predicates above), and this is also the
    # window app/main.py's one-time embedding-normalization startup task (fires ~60s
    # after backend startup, touching system_settings) can land in — straddling THIS
    # phase's two backup snapshots below produced the intermittent "opentranscribe.sh
    # backup produces the same content as the shipped pg_dump recipe" failure. Waiting
    # for both tables' content digests to stop moving before taking either dump shrinks
    # the residual race window to "changed in between the wait finishing and the first
    # dump starting" rather than eliminating it outright — same best-effort posture as
    # the speaker-attribute wait above.
    dbs_wait_for_media_file_settled "$pg" "$db_user" "$db_name" 120 \
        || gr_warn "proceeding with the pre-upgrade backup even though media_file had not settled — a digest mismatch in phase 17's F-4 is now a known possibility, not fatal (issue #619)"
    dbs_wait_for_system_settings_settled "$pg" "$db_user" "$db_name" 90 \
        || gr_warn "proceeding with the pre-upgrade backup even though system_settings had not settled — the shipped-vs-wrapper backup content-diff assertion below may still race issue #619's window"

    # Guard the repo's OWN ./backups/ and .env for the rest of this run — both
    # opentranscribe.sh commands below and every later rollback-tail phase are staged
    # under TEST_ROOT, but the guard needs to be armed before the first one
    # runs, not after.
    gr_fingerprint_repo_backups

    # Artifact 1 — the SHIPPED procedure (upgrading.md step 1): a plain
    # `docker compose exec -T postgres pg_dump`, run from the staged before/
    # directory so compose resolves THIS run's project.
    local shipped_dump="$backups_dir/pre-upgrade-${FROM_VERSION}.sql"
    if ! dbs_dump "$pg" "$db_user" "$db_name" "$shipped_dump"; then
        gr_die "shipped pg_dump procedure failed — cannot take the rollback oracle backup"
    fi
    gr_ok "shipped-procedure backup: $shipped_dump"

    # Artifact 2 — ./opentranscribe.sh backup, exercised through a copy staged under
    # TEST_ROOT (never the repo checkout — see _stage_manager_at's doc comment).
    #
    # WHICH copy of the script is feature-detected, not assumed. A real user standing
    # here is on $FROM_VERSION and has FROM's script; if that release shipped a `backup`
    # arm this rehearses their actual command. If it did not — v0.4.1 did not, issue
    # #613 added it for v0.5.0 — then no such user could run this at all, artifact 1
    # above IS their documented procedure, and this artifact falls back to the TO
    # script so the wrapper-vs-recipe equivalence below still gets checked.
    local manager_src manager_stage
    manager_src="$(_manager_source_for backup)"
    if [[ "$manager_src" == "$REPO_ROOT" && -d "$TEST_ROOT/worktree-${FROM_VERSION}" ]]; then
        as_record SKIP "'./opentranscribe.sh backup' as a ${FROM_VERSION} user would run it" \
            "${FROM_VERSION}'s opentranscribe.sh has no backup arm, so no user of that release could run this command; upgrading.md's raw pg_dump recipe (artifact 1 above) is their documented path. Falling back to the ${TO_VERSION} script for the equivalence check below."
    fi
    manager_stage="$(_stage_manager_at "$TEST_ROOT/before" "$manager_src")"
    gr_assert_not_repo_cwd "$manager_stage"
    pushd "$manager_stage" >/dev/null
    ./opentranscribe.sh backup || { popd >/dev/null; gr_die "'./opentranscribe.sh backup' failed"; }
    popd >/dev/null
    local manager_dump
    manager_dump="$(ls -t "$manager_stage/backups"/opentranscribe_backup_*.sql 2>/dev/null | head -1)"
    [[ -n "$manager_dump" && -s "$manager_dump" ]] \
        || gr_die "'./opentranscribe.sh backup' produced no dump file"
    gr_ok "opentranscribe.sh-wrapper backup: $manager_dump"

    # The wrapper claims to be a wrapper — prove it. Diff modulo pg_dump's own
    # "-- Dumped at" / "-- Dumped by" timestamp comment lines, the only lines
    # that legitimately differ between two dumps taken seconds apart.
    if diff -q \
        <(grep -vE '^-- Dumped (at|by)' "$shipped_dump") \
        <(grep -vE '^-- Dumped (at|by)' "$manager_dump") >/dev/null 2>&1; then
        as_record PASS "opentranscribe.sh backup produces the same content as the shipped pg_dump recipe"
    else
        as_record FAIL "opentranscribe.sh backup produces the same content as the shipped pg_dump recipe" \
            "$(diff -u "$shipped_dump" "$manager_dump" | head -10 | tr '\n' ' ')"
    fi

    # The cheapest place to learn a backup does not restore: replay it into a
    # scratch database in the SAME container, never the database under test.
    local scratch_rows
    if scratch_rows=$(dbs_verify_dump_restores "$pg" "$db_user" "$shipped_dump" opentranscribe_verify_pre); then
        as_assert_ge "pre-upgrade backup restores cleanly into a scratch database" "${scratch_rows:-0}" 1
    else
        as_record FAIL "pre-upgrade backup restores cleanly into a scratch database" "replay failed — see log"
    fi
    dbs_scratch_drop "$pg" "$db_user" opentranscribe_verify_pre

    # The digest-level oracle phase 15's restore is measured against, and the
    # table-list oracle phase 15's R-6 (no post-FROM-migration table survives)
    # is derived from.
    dbs_fingerprint "$pg" "$db_user" "$db_name" "$TEST_ROOT/snapshots/before/db-fingerprint"
    dbs_table_list "$pg" "$db_user" "$db_name" > "$TEST_ROOT/snapshots/before/tables.txt"
}

# _replay_release_manifest SRC_TREE DST_DIR
#   Copy every artifact release-manifest.txt lists from SRC_TREE into DST_DIR — the
#   local-filesystem equivalent of what `./opentranscribe.sh update-full` downloads.
#
#   This replaces a hand-written `cp` list (base compose, prod compose, gpu overlay,
#   opentranscribe.sh). That list was a FOURTH place the set of deployment artifacts was
#   maintained, alongside the installer, update-full, and the release-validate workflow —
#   the exact duplication release-manifest.txt was created to end, and its header records
#   the two production bugs the earlier duplicates caused. Concretely, the old list left
#   the after-stack with no blackwell overlay, no nginx overlay, no backup overlay and
#   none of scripts/, so `get_compose_files()` could not have selected them and
#   `fix_model_cache_permissions`' remedy hint pointed at a file that was not there.
#
#   `optional` entries are skipped when absent (a release may not carry them);
#   `preserve` entries are skipped outright, exactly as update-full skips them.
_replay_release_manifest() {
    local src="$1" dst="$2"
    local manifest="$src/release-manifest.txt"
    [[ -f "$manifest" ]] || gr_die "no release-manifest.txt in $src — cannot stage a deployment the way update-full would"

    local line path flags copied=0 skipped=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in ''|'#'*) continue ;; esac
        path="$(printf '%s' "$line" | cut -f1 | tr -d '[:space:]')"
        flags="$(printf '%s' "$line" | cut -s -f2)"
        [[ -n "$path" ]] || continue
        case ",$flags," in *,preserve,*) continue ;; esac

        if [[ ! -f "$src/$path" ]]; then
            case ",$flags," in
                *,optional,*) skipped=$((skipped + 1)); continue ;;
                *) gr_die "release-manifest.txt lists $path but $src does not have it" ;;
            esac
        fi
        mkdir -p "$dst/$(dirname "$path")"
        cp "$src/$path" "$dst/$path"
        case ",$flags," in *,exec,*) chmod +x "$dst/$path" ;; esac
        copied=$((copied + 1))
    done < "$manifest"
    gr_ok "staged $copied manifest artifacts into $dst ($skipped optional entries absent)"
}

phase_07_swap_to_new() {
    local stage_before="$TEST_ROOT/before"
    local stage_after="$TEST_ROOT/after"

    # IMPORTANT: keep the SAME named volumes so the upgrade is in-place against
    # the data the FROM stack populated. We do this by reusing the same
    # COMPOSE_PROJECT_NAME (default 'opentranscribe') across both stages.
    mkdir -p "$stage_after"

    # Stage the after-tree the way `./opentranscribe.sh update-full` builds one: from
    # release-manifest.txt, not from a list written here. This is also the step that
    # justifies phase 08 running the TO script — update-full is what puts it on a real
    # user's disk. See _replay_release_manifest above and REHEARSAL_ALIGNMENT_PLAN.md
    # finding C for why phase 08 cannot instead run FROM's script.
    _replay_release_manifest "$REPO_ROOT" "$stage_after"
    [[ -f "$stage_after/docker-compose.prod.yml" ]] || gr_die "current head missing docker-compose.prod.yml"

    # 0.4.0 no longer needs the database/init_db.sql bind mount, but copy it
    # anyway in case the compose file still references it (harmless if unused).
    if [[ -d "$REPO_ROOT/database" ]]; then
        rm -rf "$stage_after/database"
        cp -r "$REPO_ROOT/database" "$stage_after/database"
    fi

    # docker-compose.prod.yml declares `build: context: ./docs-site` for the
    # docs service even though it normally just pulls a published image. With
    # pull_policy forced to 'never' below, an absent local docs image falls
    # back to that build context — a real user always has docs-site/ in their
    # checkout, but this staged "after" tree didn't, so the docs container's
    # own upgrade was silently skipped ("unable to prepare context") instead
    # of actually being exercised.
    if [[ -d "$REPO_ROOT/docs-site" ]]; then
        rm -rf "$stage_after/docs-site"
        cp -r "$REPO_ROOT/docs-site" "$stage_after/docs-site"
    fi

    cp_inject_labels "$stage_after/docker-compose.yml" "$TEST_LABEL"
    cp_inject_labels "$stage_after/docker-compose.prod.yml" "$TEST_LABEL"
    cp_force_pull_policy "$stage_after/docker-compose.prod.yml" never

    # NO per-service cp_pin_image_tag here, deliberately.
    #
    # Those pins wrote `:$LOCAL_IMAGE_TAG` literally into backend/frontend/9 celery
    # services, which meant phase 08's `update --version` was not what actually moved
    # them — the harness had already done it. The upgrade this scenario exists to prove
    # was therefore only ever measured on the four services OUTSIDE that hardcoded list.
    # Every service in docker-compose.prod.yml resolves ${OT_IMAGE_TAG:-latest}
    # (test_every_prod_service_image_is_tag_pinnable guards that statically), so leaving
    # them alone makes the .env rewrite performed by the real command the SOLE mechanism
    # — and phase 10/12's version and tag assertions the thing that catches it if it did
    # not happen. Phase 13 already re-stages without these pins for the same reason.

    # Note: docker-compose.gpu.yml is now staged by _replay_release_manifest above,
    # unconditionally — that is what a real deployment has on disk. Whether it is
    # SELECTED is get_compose_files()' decision, driven by FORCE_CPU_MODE in the .env
    # phase 03 generated (see its TEST_USE_GPU block). The harness no longer decides by
    # withholding the file.

    # Reuse the SAME .env so credentials and ports are preserved across the
    # upgrade (mirrors what a real user sees on disk).
    cp "$stage_before/.env" "$stage_after/.env"

    # OT_IMAGE_TAG is deliberately left at FROM_VERSION here — every service image
    # resolves ${OT_IMAGE_TAG:-latest}, so nothing upgrades until it moves. That move
    # used to happen here via a hand-rolled `sed`, which is also what `opentranscribe.sh
    # update --version` does to a real user's .env — except the sed never recorded
    # `# OT_PREVIOUS_IMAGE_TAG`, so a `--rollback` invoked at the end of this scenario
    # had no target and exited 1 (issue #598 §2.4). Phase 08 runs the real
    # `update --version` command instead, which performs the same rewrite AND writes the
    # rollback bookkeeping phase 12 checks and phase 16 depends on.
    gr_ok "after-stack .env left at OT_IMAGE_TAG=${FROM_VERSION} — phase 08's real 'update --version' does the rewrite"
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

# The report is opened by phase 08 (the first phase with assertions of its own) and
# appended to by phase 10 and the rollback tail. Idempotent within a process so two
# callers cannot stack two headers on one file, and so a resumed run that skips phase 08
# still gets a header from phase 10.
REPORT_INITIALISED=0
_init_report() {
    TEST_REPORT_FILE="$TEST_ROOT/REPORT.md"
    export TEST_REPORT_FILE
    (( REPORT_INITIALISED == 0 )) || return 0
    REPORT_INITIALISED=1
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
}

phase_08_start_new() {
    local stage_after="$TEST_ROOT/after"

    # Clear any stale daemon network state BEFORE invoking 'update' so that
    # the user-facing upgrade command runs against a clean host — same as a
    # real user's environment would be.
    _clean_stale_opentranscribe_network

    # Invoke the actual './opentranscribe.sh update --version' command. This
    # is what real users run to upgrade to a specific release. It does
    # 'compose down && compose pull && compose up -d' under the hood (plus the
    # .env rewrite and rollback bookkeeping --version adds), but going through
    # the script means we validate the code path users actually exercise —
    # not a hand-rolled sequence that could silently drift from the real
    # behavior. `--version` (over a bare `update`) is what actually moves
    # OT_IMAGE_TAG to LOCAL_IMAGE_TAG now that phase 07 no longer seds it, and
    # it is also the only path that records `# OT_PREVIOUS_IMAGE_TAG`, which
    # phase 12 asserts and phase 16's `--rollback` depends on (issue #598).
    #
    # WHY THE **TO** SCRIPT DRIVES THIS, when phase 04 deliberately used FROM's.
    # Measured against the real FROM release (v0.4.1): its `update` has no `--version`
    # at all — it can only pull whatever `:latest` currently is, and the version under
    # test is unreleased and not on Docker Hub, so FROM's script CANNOT reach it. There
    # is no "run the upgrade from the old script" variant to test here. What puts the TO
    # script on a real user's disk is `update-full`, which re-downloads every artifact in
    # release-manifest.txt — which is precisely what phase 07 now replays.
    # REHEARSAL_ALIGNMENT_PLAN.md finding C has the full table.
    #
    # `./opentr.sh` is again deliberately absent: dev-only, not in release-manifest.txt,
    # and no curl install has it (see _stage_manager_at).
    pushd "$stage_after" >/dev/null
    gr_log "running './opentranscribe.sh update --version ${LOCAL_IMAGE_TAG}' (real user upgrade path)"
    ./opentranscribe.sh update --version "$LOCAL_IMAGE_TAG" \
        || gr_die "opentranscribe.sh update --version failed"
    popd >/dev/null

    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    # Migrations may take several minutes on a populated DB — the healthcheck
    # start_period in docker-compose.yml is 600s and we mirror that budget.
    ac_wait_for_health 900

    # Tail backend logs for "Alembic upgrade complete" or similar marker
    docker logs opentranscribe-backend 2>&1 | grep -iE 'alembic|migration' | tail -20 \
        > "$TEST_ROOT/migration-log.txt" || true

    # The report is opened HERE now, not in phase 10, because this phase has assertions
    # of its own and as_record would otherwise write rows into a file phase 10 then
    # truncates. _init_report is idempotent per process, so phase 10 still gets its
    # header on a resumed run where this phase was skipped. The migration-log excerpt it
    # embeds is captured just above, which is why the call sits here and not earlier.
    _init_report

    # What did the TO release's selector choose for the upgraded stack? Asserted so that
    # "the upgrade exited 0" is no longer the whole verdict — a `get_compose_files()`
    # regression that dropped the GPU overlay, or picked Blackwell on non-Blackwell
    # hardware, would otherwise show up only as unexplained slowness weeks later.
    cc_assert_chain "after upgrade to ${LOCAL_IMAGE_TAG}" "$stage_after" "$REPO_ROOT"

    # Honest scoping, recorded rather than left implicit: the one documented upgrade
    # command this scenario does NOT execute.
    as_record SKIP "'${FROM_VERSION} ./opentranscribe.sh update-full' (self-refreshing cross-release upgrade)" \
        "update-full fetches every release-manifest.txt artifact from raw.githubusercontent.com/<branch>; the release under test is the local HEAD, which is not fetchable at that URL. Phase 07 replays the manifest from disk to reproduce its RESULT, but the download half is unexercised. Closing this needs a URL-base seam in the shipped script or a local HTTP mirror — a product decision, see REHEARSAL_ALIGNMENT_PLAN.md finding C."
}

phase_09_snapshot_post() {
    snapshot_state after
}

phase_10_assert_and_report() {
    _init_report

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
    # Derived from the down_revision graph, not `grep | tail -1`. That old form
    # sorted by FILENAME and only worked by luck of 3-digit zero-padded ids; the
    # chain is already non-contiguous (v130->v071, v073->v140, two v270* files,
    # v375-v381 renumbered), and a 4-digit id or a second head would have made it
    # silently assert the wrong revision.
    expected_head=$(ver_alembic_head "$REPO_ROOT/backend")
    as_assert_ne "alembic head advanced" "$pre_head" "$post_head"
    as_assert_eq "alembic head matches current head" "$expected_head" "$post_head"

    # The FROM release's head, MEASURED off the running stack vs DERIVED from
    # that release's own migration chain in the phase-03 worktree.
    #
    # This pair is what replaced expected-schemas.tsv. That file claimed to be
    # "the single source of truth for what head release X shipped with", was
    # hand-maintained, was read by no script, and never got its v0.4.1 row. Both
    # sides here are computed, so there is nothing to forget to update — and it
    # is a strictly stronger claim: the TSV only ever recorded what someone typed.
    local from_worktree="$TEST_ROOT/worktree-${FROM_VERSION}"
    if [[ -d "$from_worktree/backend/alembic/versions" ]]; then
        local derived_from_head
        if derived_from_head=$(ver_alembic_head "$from_worktree/backend" 2>/dev/null); then
            if [[ "$pre_head" == *"absent"* ]]; then
                # Pre-Alembic releases (v0.3.3 bootstrapped via init_db.sql) have
                # no alembic_version row to measure; the derivation still applies.
                as_record SKIP "$FROM_VERSION shipped head (measured vs derived)" \
                    "pre-Alembic schema: $derived_from_head derived, nothing recorded in the DB"
            else
                as_assert_eq "$FROM_VERSION shipped head (measured == derived)" \
                    "$derived_from_head" "$pre_head"
            fi
        else
            as_record SKIP "$FROM_VERSION shipped head" "chain in the worktree is not single-headed"
        fi
    else
        as_record SKIP "$FROM_VERSION shipped head" "worktree not present (resumed run?)"
    fi

    # Transcript prefix check (per file)
    if [[ -f "$TEST_ROOT/seeded-file-ids.txt" ]]; then
        while IFS= read -r fid; do
            local detail
            detail=""
            if detail="$(python3 - "$pre/transcript-$fid.json" "$post/transcript-$fid.json" "$fid" <<'PY'
import json, sys
pre, post, fid = sys.argv[1:4]
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
print(f"pre={len(pre_segs or [])} post={len(post_segs or [])}")
sys.exit(0 if ok else 1)
PY
)"; then
                as_record PASS "transcript prefix preserved for file $fid"
            else
                as_record FAIL "transcript prefix preserved for file $fid" "$detail"
            fi
        done < "$TEST_ROOT/seeded-file-ids.txt"
    fi

    # ─── New-feature liveness checks ────────────────────────────────────
    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    ac_login "$TEST_ADMIN_EMAIL" "$TEST_ADMIN_PASSWORD" || true
    local code

    # Docs are a security surface, not a liveness check: a hardened deployment
    # publishes none of /api/docs, /api/redoc, /api/openapi.json. 404 is the
    # CORRECT answer for a real install, so assert whichever the deployment is
    # configured for. Same correction as the fresh-install scenario.
    local docs_enabled
    # python-dotenv, not grep/cut (issue #590).
    docs_enabled=$(python3 "$SCRIPT_DIR/../lib/env_reader.py" \
        "$TEST_ROOT/after/.env" ENABLE_API_DOCS \
        | tr '[:upper:]' '[:lower:]' || true)
    code=$(curl -o /dev/null -s -w '%{http_code}' "http://localhost:${TEST_BACKEND_PORT}/api/docs")
    if [[ "$docs_enabled" == "true" || "$docs_enabled" == "1" || "$docs_enabled" == "yes" ]]; then
        as_assert_http "API docs reachable post-upgrade (opted in)" 200 "$code"
    else
        as_assert_http "API docs NOT exposed post-upgrade (hardened)" 404 "$code"
    fi

    # Same class as B-8 below and #617/#618: guard the curl so a transient
    # failure here is recorded as a FAIL rather than crashing the whole
    # script under set -e and silently truncating every phase after it
    # (phases 11-18 never ran the first time this fired -- no assertion
    # summary, no .phase/10.done marker, just a bare "rehearse failed").
    local frontend_url="http://localhost:${TEST_FRONTEND_PORT}/"
    ac_wait_for_frontend "$frontend_url" 900 || true
    local code
    if code="$(curl -o /dev/null -s -w '%{http_code}' "$frontend_url")"; then
        as_assert_http "frontend reachable post-upgrade" 200 "$code"
    else
        as_record FAIL "frontend reachable post-upgrade" "curl failed to reach $frontend_url"
    fi

    # ── The upgrade is running the NEW code ────────────────────────────────
    #
    # Without this, everything above only proves "a stack came up after the
    # compose swap". With pull_policy:never plus local tag pinning, a silently
    # stale image is genuinely reachable, and every data assertion would still
    # pass against the OLD binary.
    local running_version
    running_version=$(curl -fsS --max-time 10 "$API_BASE/version" 2>/dev/null \
        | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4 || echo "")
    # De-vacuum: "${running_version:-none}" defaults an EMPTY response to the
    # string "none", and "unknown" != "none" is trivially true — so a curl
    # failure or an unparseable body silently satisfied the build-arg-contract
    # check below without the endpoint ever having answered anything.
    as_assert "running version returned a version field" '[[ -n "$running_version" ]]'
    as_assert_eq "running version is the version under test" \
        "$TO_VERSION" "$(ver_normalize "${running_version:-none}" 2>/dev/null || echo "${running_version:-none}")"
    as_assert_ne "running version is not 'unknown' (build-arg contract)" "unknown" "${running_version:-none}"

    # ── API contract: no route silently disappeared ────────────────────────
    #
    # This replaced a hardcoded "the MFA endpoint was 404 in v0.3.3" probe, which
    # asserted one fact about one pair of releases and rotted the moment FROM
    # moved. Diffing the OpenAPI route sets needs no maintenance AND catches a
    # class the old probe could not: an endpoint REMOVED between releases, which
    # breaks every existing client.
    local before_routes="$TEST_ROOT/snapshots/before/routes.txt"
    local after_routes="$TEST_ROOT/snapshots/after/routes.txt"
    if [[ -s "$before_routes" && -s "$after_routes" ]]; then
        local removed added
        removed=$(comm -23 "$before_routes" "$after_routes" | head -20)
        added=$(comm -13 "$before_routes" "$after_routes" | wc -l)

        as_assert "no API route removed by the upgrade" '[[ -z "$removed" ]]'
        [[ -n "$removed" ]] && gr_warn "routes gone after upgrade:"$'\n'"$removed"

        # A release that adds nothing to the API is not necessarily wrong, so
        # this is informational — it is the cheap sanity check that the new
        # image really is different from the old one.
        gr_log "API routes added by this upgrade: $added"
    else
        as_record SKIP "API route diff" "openapi.json not served (hardened: ENABLE_API_DOCS unset) — set it to exercise the route diff"
    fi

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
}

# ─── Phase 11: does the upgraded stack still do its JOB? ────────────────────
#
# Everything up to here proves the OLD data survived and the new code answers
# HTTP. Neither proves the upgraded deployment can still process NEW work —
# and that is the failure a user actually notices. The paths this exercises are
# the ones an upgrade is most likely to break and the assertions above cannot
# see: Celery workers picking up a task under the new image, the ASR model
# loading against a possibly-changed cache layout, the OpenSearch index
# accepting writes under the new mapping, and the new row satisfying the
# migrated schema's constraints rather than merely the old rows doing so.
#
# A migration that leaves existing rows intact but makes every INSERT fail is
# a complete upgrade failure that phases 06-10 would report as a clean pass.
phase_11_new_data_post_upgrade() {
    API_BASE="${API_BASE:-http://localhost:${TEST_BACKEND_PORT}/api}"
    export API_BASE
    TEST_REPORT_FILE="${TEST_REPORT_FILE:-$TEST_ROOT/REPORT.md}"
    export TEST_REPORT_FILE

    ac_login "$TEST_ADMIN_EMAIL" "$TEST_ADMIN_PASSWORD" || {
        as_record FAIL "login to upgraded stack for new-data test"
        return 0
    }

    # Deliberately a DIFFERENT file from the ones phase 05 seeded, so a pass
    # cannot come from re-reading pre-upgrade state.
    local seeded="$TEST_ROOT/seeded-media-names.txt"
    local new_media=""
    while IFS= read -r f; do
        if [[ -f "$seeded" ]] && grep -Fxq "${f##*/}" "$seeded"; then
            continue
        fi
        new_media="$f"; break
    done < <(find "$TEST_MEDIA_DIR" -maxdepth 1 -type f \
                \( -iname "*.mp3" -o -iname "*.m4a" -o -iname "*.mp4" \
                   -o -iname "*.wav" -o -iname "*.flac" -o -iname "*.ogg" \) \
                 -size "-$TEST_MEDIA_MAX_SIZE" | sort)

    if [[ -z "$new_media" ]]; then
        as_record SKIP "new upload post-upgrade" "no suitable media in $TEST_MEDIA_DIR"
        return 0
    fi

    local fid
    if ! fid=$(ac_upload_file "$new_media"); then
        as_record FAIL "new upload accepted post-upgrade" "$(basename "$new_media")"
        return 0
    fi
    as_record PASS "new upload accepted post-upgrade" "$(basename "$new_media") uuid=$fid"
    # Recorded so phase 15's R-5 can assert this POST-backup file is ABSENT
    # after a restore to the pre-upgrade backup — a restore that leaves it
    # merged rather than replaced.
    echo "$fid" > "$TEST_ROOT/post-upgrade-new-file-id.txt"

    # The real proof: a task queued AFTER the upgrade runs to completion under
    # the new image. This is what exercises the workers, the ASR stack and the
    # post-migration INSERT path.
    if ac_wait_for_file_status "$fid" 1800; then
        as_record PASS "NEW transcription completed on upgraded stack" "$fid"
    else
        as_record FAIL "NEW transcription completed on upgraded stack" \
            "file $fid did not reach completed within 1800s"
        return 0
    fi

    local seg_count
    seg_count=$(ac_segment_count "$fid")
    as_assert_ge "NEW transcript has segments" "$seg_count" 1

    # Which engine actually diarized this file? "completed" above proves
    # nothing — the fallback to in-process PyAnnote is SILENT BY DESIGN, so an
    # upgrade whose diarizer is dead can still pass every assertion above
    # (issue #670: "test-upgrade.sh seeds diar-native and fails if
    # diarization is dead"). This check is deliberately only on the UPGRADED
    # (TO) side: phase 05's seed upload runs against the FROM release, which
    # may predate diar-native entirely and would never log either the native
    # or the fallback line — asserting there would fail on correct behaviour
    # from an old release. The current codebase always ships diar-native
    # support, so there is no equivalent excuse here.
    #
    # Two independent, non-redundant signals — see diar-native-smoke.sh's and
    # ac_diar_engine_verdict's own headers for why neither is sufficient
    # alone. GPU residency is only meaningful when this run actually asked
    # for a GPU deployment (TEST_USE_GPU=false legitimately runs diar-native
    # on CPU, holding zero device memory).
    if [[ "$TEST_USE_GPU" == "true" ]]; then
        # Wrapped in `if`, never called bare: diar-native-smoke.sh exits
        # non-zero on both FAIL (1) and NOT MEASURED (4), and this script
        # runs under `set -euo pipefail` — an unwrapped non-fatal-by-design
        # helper call here would silently truncate every phase after it
        # (issues #617/#618; see scripts/CLAUDE.md's "bare helper call"
        # gotcha).
        local diar_smoke_rc=0 diar_smoke_out=""
        if diar_smoke_out=$("$REPO_ROOT/scripts/diar-native-smoke.sh" --json 2>&1); then
            diar_smoke_rc=0
        else
            diar_smoke_rc=$?
        fi
        case "$diar_smoke_rc" in
            0) as_record PASS "diar-native sidecar GPU residency on upgraded stack (diar-native-smoke.sh)" ;;
            4) as_record SKIP "diar-native sidecar GPU residency on upgraded stack" "NOT MEASURED: $diar_smoke_out" ;;
            *) as_record FAIL "diar-native sidecar GPU residency on upgraded stack" "$diar_smoke_out" ;;
        esac
    fi

    # Verdict is per-file (issue #706's diarization_provider column) and
    # keyed to THIS file's own uuid, not a whole-worker-log grep in a
    # 30-minute window — this is the TO (post-upgrade) side, which always
    # ships #706, so the DB path is expected to answer every time; the
    # log-fallback branches below exist only as a defensive net, not the
    # expected path here (see ac_diar_engine_verdict's header for why the
    # FROM side is never checked this way at all).
    # This is the TO (post-upgrade) side, which — per ac_diar_engine_verdict's own
    # header and the phase-11 comment above — always ships #706's
    # diarization_provider column. The genuine old-stack case the function's log
    # fallback exists for is the FROM side, which this check deliberately never
    # runs against. So on THIS side a `:log` verdict is never the legitimate
    # old-schema case either, and `native:log` must not get an unqualified PASS —
    # an unscoped 30-minute log grep can report PASS from an unrelated earlier
    # job/phase, which is exactly the false-pass hole #706 closed (issue #707).
    local diar_verdict diar_verdict_source
    diar_verdict=$(ac_diar_engine_verdict "$fid" "opentranscribe-celery-worker" "30m")
    diar_verdict_source="${diar_verdict#*:}"
    case "$diar_verdict" in
        native:db)
            as_record PASS "native diarizer served the post-upgrade file ($fid, source=$diar_verdict_source)"
            ;;
        native:log)
            as_record FAIL "native diarizer served the post-upgrade file ($fid)" \
                "verdict=native:log — an unscoped legacy worker-log grep, not the per-file diarization_provider column. The upgraded (TO) stack should always carry that column; a :log result here means the DB-backed check was unavailable, not that diarization succeeded, and may reflect an unrelated job from an earlier phase of this same run. Never trusted as a pass (issue #707)"
            ;;
        pyannote:db)
            as_record FAIL "native diarizer served the post-upgrade file ($fid)" \
                "media_file.diarization_provider=pyannote — PyAnnote served this job (direct config or a silent native fallback) after the upgrade"
            ;;
        fallback:log)
            as_record FAIL "native diarizer served the post-upgrade file ($fid)" \
                "worker log shows a 'falling back to PyAnnote' line — the sidecar degraded silently after the upgrade (no diarization_provider column on this API; legacy log-based check)"
            ;;
        none:db)
            as_record FAIL "native diarizer served the post-upgrade file ($fid)" \
                "media_file.diarization_provider is NULL after completion on the upgraded stack — diarization never resolved a provider"
            ;;
        error:request)
            as_record FAIL "native diarizer served the post-upgrade file ($fid)" \
                "ac_diar_engine_verdict's request to /api/files/$fid failed or returned unparseable JSON (verdict=error:request) on the upgraded stack — this is a request failure, not evidence the diarization_provider column is absent, and is never silently downgraded to the log fallback"
            ;;
        absent:none)
            as_record FAIL "native diarizer served the post-upgrade file ($fid)" \
                "file record unreachable and opentranscribe-celery-worker is not running on the upgraded stack"
            ;;
        unknown:db|unknown:log)
            as_record FAIL "native diarizer served the post-upgrade file ($fid)" \
                "could not determine which engine served this job on the upgraded stack (verdict=$diar_verdict)"
            ;;
        *)
            as_record FAIL "native diarizer served the post-upgrade file ($fid)" \
                "unrecognized verdict from ac_diar_engine_verdict: $diar_verdict"
            ;;
    esac

    # And the new content must be reachable through search — proving the
    # upgraded stack INDEXED it, not merely stored it.
    local new_hits=0 waited=0
    while [ "$waited" -lt 300 ]; do
        new_hits=$(ac_search "the" 2>/dev/null | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d.get("total_results") or len(d.get("results") or d.get("hits") or []))
' 2>/dev/null || echo 0)
        [ "$new_hits" -ge 1 ] && break
        sleep 10
        waited=$((waited + 10))
    done
    as_assert_ge "NEW content indexed and searchable post-upgrade" "$new_hits" 1
}

phase_12_assert_rollback_precondition() {
    TEST_REPORT_FILE="${TEST_REPORT_FILE:-$TEST_ROOT/REPORT.md}"
    export TEST_REPORT_FILE

    # Cheap, and it is what makes phase 16's `--rollback` possible at all
    # (issue #598 §2.4): a bare `update` never wrote this, so a --rollback
    # invoked at the end of this scenario used to exit 1 with "no previous
    # version recorded". Phase 08's real `update --version` writes it.
    local env_file="$TEST_ROOT/after/.env"
    local prev_tag current_tag
    # prev_tag reads a commented-out marker line (`^# *KEY=`), which is not a real
    # dotenv key -- python-dotenv (and read_env_value) would never see it, so this
    # one deliberately keeps its own grep rather than gaining a second helper
    # parameter for a single caller.
    prev_tag="$(grep -E '^# *OT_PREVIOUS_IMAGE_TAG=' "$env_file" 2>/dev/null | cut -d= -f2 | tr -d ' "' | head -1)"
    # python-dotenv, not grep/cut (issue #590).
    current_tag="$(python3 "$SCRIPT_DIR/../lib/env_reader.py" "$env_file" OT_IMAGE_TAG)"
    as_assert_eq "rollback precondition: # OT_PREVIOUS_IMAGE_TAG recorded as FROM" "$FROM_VERSION" "${prev_tag:-<absent>}"
    as_assert_eq "rollback precondition: OT_IMAGE_TAG now pinned to TO" "$LOCAL_IMAGE_TAG" "${current_tag:-<absent>}"

    if [[ "$ROLLBACK_REHEARSAL" != "1" ]]; then
        as_record SKIP "backup/restore + update --rollback tail (phases 13-17)" \
            "ROLLBACK_REHEARSAL=0 / --no-rollback"
    fi
}

# ─── Phases 13-17: the backup/restore + update --rollback tail (issue #598) ─
#
# Everything up to phase 12 proves the FORWARD path: real data survives a real
# migration. Nothing above it ever exercised the documented recovery path —
# opentranscribe.sh backup/restore and `update --rollback` — which is exactly the
# thing a user reaches for in an actual emergency. These phases restore the
# phase 06b backup over damage inflicted through the real API, then run the
# real `update --rollback`, and assert the FROM image serves the restored FROM
# database through its real API — not merely that commands exited 0.
phase_13_stage_rollback_tree() {
    local stage_after="$TEST_ROOT/after"
    local stage_rollback="$TEST_ROOT/rollback"
    mkdir -p "$stage_rollback"

    # Same compose set as the after-stack, but WITHOUT cp_pin_image_tag's
    # hardcoded per-service pins. Those pin backend/frontend/9 celery services
    # to $LOCAL_IMAGE_TAG literally, so `update --rollback`'s .env rewrite
    # would move only the services outside that list (docs + 3 GPU worker
    # variants) and produce a mixed-version stack — a rollback rehearsal that
    # mostly did not roll back. docker-compose.prod.yml already resolves
    # every service through ${OT_IMAGE_TAG:-latest}
    # (test_every_prod_service_image_is_tag_pinnable guards this statically),
    # so removing the pins here is what a real deployment does, not a
    # workaround. This means the tail rehearses the `update-full` variant (new
    # compose + old images moving via .env alone) rather than plain `update`.
    cp "$stage_after/docker-compose.yml" "$stage_rollback/docker-compose.yml"
    cp "$REPO_ROOT/docker-compose.prod.yml" "$stage_rollback/docker-compose.prod.yml"

    # Same fix as phase 07's $stage_after (issue #909bfc17): docker-compose.prod.yml
    # declares `build: context: ./docs-site` for the docs service. With pull_policy
    # forced to never below and no local docs-site/ to build from, `docker compose up`
    # fails to prepare the docs build context. Unlike the after-stack case — where a
    # stale docs container was already running and the build failure only meant its
    # own upgrade was silently skipped — phase 15's restore has already stopped the
    # WHOLE app (issue #610), so here there is nothing to fall back to: the single
    # `docker compose up` batch covering frontend + celery workers + flower + docs
    # aborts entirely on the docs build failure and NONE of them start (issue #618) —
    # not just docs. A real user always has docs-site/ in their checkout; only this
    # staged rehearsal tree needs it copied in explicitly.
    if [[ -d "$REPO_ROOT/docs-site" ]]; then
        rm -rf "$stage_rollback/docs-site"
        cp -r "$REPO_ROOT/docs-site" "$stage_rollback/docs-site"
    fi

    cp_inject_labels "$stage_rollback/docker-compose.prod.yml" "$TEST_LABEL"
    cp_force_pull_policy "$stage_rollback/docker-compose.prod.yml" never
    if [[ "$TEST_USE_GPU" == "true" && -f "$stage_after/docker-compose.gpu.yml" ]]; then
        cp "$stage_after/docker-compose.gpu.yml" "$stage_rollback/docker-compose.gpu.yml"
    fi
    cp "$REPO_ROOT/opentranscribe.sh" "$stage_rollback/opentranscribe.sh"
    chmod +x "$stage_rollback/opentranscribe.sh"
    cp "$stage_after/.env" "$stage_rollback/.env"

    TEST_REPORT_FILE="${TEST_REPORT_FILE:-$TEST_ROOT/REPORT.md}"
    {
        echo ""
        echo "## Rollback rehearsal scope"
        echo ""
        echo "The staged rollback tree removes \`cp_pin_image_tag\`'s hardcoded"
        echo "per-service image pins, so phases 13-17 rehearse the \`update-full\`"
        echo "variant (new compose + old images move via \`.env\` alone) rather"
        echo "than plain \`update\` (old compose, only \`OT_IMAGE_TAG\` moves) —"
        echo "deliberately: it is both the more common upgrade path and the one"
        echo "an unpinned service could silently miss on a real deployment."
        echo ""
    } >> "$TEST_REPORT_FILE"

    # The table-list oracle R-6 (phase 15) needs the OTHER endpoint of: what
    # did this migration ADD relative to FROM? Captured here (the after-stack
    # is fully migrated and running) rather than in phase 09, so it lives
    # beside the backup/restore artifacts it exists to support.
    local pg="opentranscribe-postgres"
    dbs_table_list "$pg" postgres opentranscribe > "$TEST_ROOT/snapshots/after/tables.txt"
    # ROLLBACK_INJECT_FAULT=stale-oracle (phase 15) compares the restore
    # against THIS directory on purpose, to prove the harness's own oracle
    # can be wrong. That self-check is only real if the directory exists —
    # measured: it didn't, so every digest read from it fell back to "?" and
    # mismatched everything, making the fault "work" for the wrong reason
    # (a missing file) rather than by actually exercising the diff logic.
    dbs_fingerprint "$pg" postgres opentranscribe "$TEST_ROOT/snapshots/after/db-fingerprint"

    # Verify the TO-side backup too — needed as phase 17's restore point and
    # it proves `backup` works on the MIGRATED schema, not just the
    # pre-migration one phase 06b already checked.
    local backups_dir="$TEST_ROOT/backups"
    local to_dump="$backups_dir/post-upgrade-${LOCAL_IMAGE_TAG}.sql"
    if ! dbs_dump "$pg" postgres opentranscribe "$to_dump"; then
        gr_die "could not take the post-upgrade (TO-side) backup"
    fi
    local restored_rows
    if restored_rows=$(dbs_verify_dump_restores "$pg" postgres "$to_dump" opentranscribe_verify_post); then
        as_assert_ge "post-upgrade backup restores cleanly into a scratch database" "${restored_rows:-0}" 1
    else
        as_record FAIL "post-upgrade backup restores cleanly into a scratch database" "replay failed — see log"
    fi
    dbs_scratch_drop "$pg" postgres opentranscribe_verify_post

    gr_ok "rollback tree staged at $stage_rollback; TO-side backup verified"
}

phase_14_damage_database() {
    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    TEST_REPORT_FILE="${TEST_REPORT_FILE:-$TEST_ROOT/REPORT.md}"
    export TEST_REPORT_FILE
    ac_login "$TEST_ADMIN_EMAIL" "$TEST_ADMIN_PASSWORD"

    if [[ "${ROLLBACK_INJECT_FAULT:-}" == "no-damage" ]]; then
        gr_warn "ROLLBACK_INJECT_FAULT=no-damage — damage step intentionally skipped (self-check)"
    else
        # Delete one of phase 05's seeded files — a user losing data, the
        # scenario a rollback exists for. Through the real API, not a raw SQL
        # poke: a poke tests postgres, not OpenTranscribe.
        local deleted_fid
        deleted_fid="$(head -1 "$TEST_ROOT/seeded-file-ids.txt")"
        echo "$deleted_fid" > "$TEST_ROOT/damage-deleted-file-id.txt"
        ac_curl -X DELETE "$API_BASE/files/$deleted_fid" >/dev/null \
            || gr_die "could not delete seeded file $deleted_fid to simulate damage"

        # Rename a speaker — a second, independent mutation shape (UPDATE, not
        # DELETE).
        local speaker_json speaker_uuid old_name new_name
        speaker_json="$(ac_curl "$API_BASE/speakers" 2>/dev/null || echo '{}')"
        speaker_uuid="$(echo "$speaker_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
items = d.get("speakers") if isinstance(d, dict) else d
items = items if isinstance(items, list) else []
print(items[0]["uuid"] if items else "")
' 2>/dev/null || echo "")"
        if [[ -n "$speaker_uuid" ]]; then
            old_name="$(echo "$speaker_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
items = d.get("speakers") if isinstance(d, dict) else d
items = items if isinstance(items, list) else []
print(items[0].get("name", "") if items else "")
' 2>/dev/null || echo "")"
            new_name="rollback-damage-rename-$(date +%s)"
            if ac_curl -X PATCH "$API_BASE/speakers/$speaker_uuid" \
                -H "Content-Type: application/json" \
                -d "{\"name\":\"$new_name\"}" >/dev/null 2>&1; then
                printf '%s\n%s\n%s\n' "$speaker_uuid" "$old_name" "$new_name" > "$TEST_ROOT/damage-speaker.txt"
            else
                gr_warn "could not rename speaker $speaker_uuid to simulate damage — R-4 will SKIP"
            fi
        else
            gr_warn "no speaker found to rename for damage simulation — R-4 will SKIP"
        fi
        # The phase-11 post-upgrade upload is left in place deliberately — it
        # is POST-backup data and phase 15's R-5 asserts it is ABSENT after
        # restoring to the pre-upgrade backup.
    fi

    local pg="opentranscribe-postgres"
    dbs_fingerprint "$pg" postgres opentranscribe "$TEST_ROOT/snapshots/damaged/db-fingerprint"

    # The damage must be real before the restore assertion means anything —
    # otherwise R-2 could pass by never having anything to fix.
    #
    # `before` was fingerprinted pre-upgrade (FROM schema) and `damaged` is
    # fingerprinted here post-upgrade (TO schema, 15 more media_file columns
    # by v0.5.0) — a bare whole-row digest comparison across that boundary
    # always differs regardless of whether phase 14 actually damaged
    # anything, the same schema-vs-damage conflation F-4 has (see its
    # comment in phase 18). Restrict both sides to before's column set so a
    # PASS here means the DAMAGE SIMULATION really changed a value, not that
    # the schema moved out from under an unrestricted comparison.
    local before_cols="$TEST_ROOT/snapshots/before/db-fingerprint/media_file.columns"
    local before_digest damaged_digest_restricted
    before_digest="$(cat "$TEST_ROOT/snapshots/before/db-fingerprint/media_file.digest" 2>/dev/null || echo '?')"
    if [[ -s "$before_cols" ]] && \
       damaged_digest_restricted="$(dbs_digest_baseline_columns "$pg" postgres opentranscribe media_file "$before_cols")"; then
        as_assert_ne "damage precondition: media_file digest changed by phase 14" \
            "$before_digest" "$damaged_digest_restricted"
    else
        as_record SKIP "damage precondition: media_file digest changed by phase 14" \
            "before-schema column list unavailable or no longer a subset of the current schema — cannot compute a schema-comparable digest"
    fi
}

phase_15_restore_and_assert() {
    TEST_REPORT_FILE="${TEST_REPORT_FILE:-$TEST_ROOT/REPORT.md}"
    export TEST_REPORT_FILE
    local pg="opentranscribe-postgres"
    local shipped_dump="$TEST_ROOT/backups/pre-upgrade-${FROM_VERSION}.sql"
    [[ -s "$shipped_dump" ]] || gr_die "pre-upgrade backup missing: $shipped_dump (phase 06b did not complete)"

    # Fault injection: corrupt the oracle the restore is about to load, so the
    # tail's own failure detection is exercised for real (issue #598 §9.3).
    #
    # MEASURED, not assumed (selftest-rollback-fault-injection.sh case 2): psql
    # reading a plain-format dump from a FILE treats an unterminated
    # `COPY ... FROM stdin` at EOF as simply ending the copy — no error, exit
    # 0 — rather than the parse failure a first guess would expect. A dump
    # truncated after the last statement it happens to complete therefore
    # still replays "cleanly" and silently drops whatever rows came after the
    # cut: the SAME failure shape issue #598 originally measured in
    # `restore_database` (reports success, changed nothing), reproduced structurally.
    # So the cut targets media_file's own COPY block specifically — its
    # header plus exactly one data row — so R-2/R-8 below are the assertions
    # expected to catch it, never R-1's exit code.
    local restore_source="$shipped_dump"
    if [[ "${ROLLBACK_INJECT_FAULT:-}" == "truncate" ]]; then
        restore_source="$TEST_ROOT/backups/pre-upgrade-${FROM_VERSION}.truncated.sql"
        local copy_line
        copy_line="$(grep -n '^COPY public\.media_file ' "$shipped_dump" | head -1 | cut -d: -f1)"
        [[ -n "$copy_line" ]] || gr_die "ROLLBACK_INJECT_FAULT=truncate: could not find media_file's COPY line in $shipped_dump"
        head -n "$(( copy_line + 1 ))" "$shipped_dump" > "$restore_source"
        gr_warn "ROLLBACK_INJECT_FAULT=truncate — restoring a dump cut mid-way through media_file's COPY block; R-1 is expected to still report success (0), R-2/R-8 below are EXPECTED to FAIL"
    fi

    local minio_pre="$TEST_ROOT/snapshots/damaged/minio_etags.json"
    _capture_minio_etags "$minio_pre"

    # Guardrail: the first genuinely destructive statement in the rollback
    # tail is the DROP DATABASE inside restore_database (scripts/common.sh,
    # invoked here through opentranscribe.sh — the shipped front end, issue #613).
    # All four conditions of gr_assert_target_is_test_database must hold or it dies.
    gr_assert_target_is_test_database "$pg" opentranscribe "$TEST_ROOT/after/.env"

    local manager_stage
    manager_stage="$(_stage_manager_at "$TEST_ROOT/after")"
    gr_assert_not_repo_cwd "$manager_stage"

    pushd "$manager_stage" >/dev/null
    local restore_rc=0
    ./opentranscribe.sh restore --yes "$restore_source" || restore_rc=$?
    popd >/dev/null

    # R-1 is asserted the SAME way regardless of ROLLBACK_INJECT_FAULT=truncate:
    # per the measured note above, a mid-COPY truncation genuinely reports exit
    # 0 too. The corruption is caught below, by content (R-2/R-8), not here.
    as_assert_eq "R-1: restore command exits 0 on success" "0" "$restore_rc"

    # R-13 (issue #610): the restore must NOT have restarted the application. This is
    # THE direct regression assertion for #610 — a running backend here would
    # immediately run `alembic upgrade head` over the dump we just installed, silently
    # migrating the FROM-release backup forward before anyone (operator or this test)
    # ever gets to see it in its original restored form. `restore --yes` against a
    # schema-head mismatch (the FROM backup vs. the still-running TO image, exactly
    # this scenario) now leaves services stopped by design — see
    # scripts/common.sh's pg_restore_restart_decision.
    local backend_running
    backend_running="$(docker ps --format '{{.Names}}' --filter 'name=^opentranscribe-backend$' | head -1)"
    as_assert_eq "R-13: restore left the application stopped (no auto-migration window)" "" "${backend_running:-}"

    as_record SKIP "R-12: no live writer at the moment of the drop" \
        "enforced inside scripts/common.sh's restore_database via DROP DATABASE ... WITH (FORCE) and its own client-stop sequence (#599) — not independently observable from outside that function without instrumenting it"

    dbs_fingerprint "$pg" postgres opentranscribe "$TEST_ROOT/snapshots/restored/db-fingerprint"
    local fp_dir="$TEST_ROOT/snapshots/before/db-fingerprint"
    if [[ "${ROLLBACK_INJECT_FAULT:-}" == "stale-oracle" ]]; then
        fp_dir="$TEST_ROOT/snapshots/after/db-fingerprint"
        gr_warn "ROLLBACK_INJECT_FAULT=stale-oracle — comparing against the AFTER (post-upgrade) fingerprint on purpose; digest assertions below are EXPECTED to FAIL"
    fi
    # R-2 (media_file), R-7 (alembic_version, cross-checked again below), R-9
    # (transcript_segment), R-10 (user) — one content-digest diff per table.
    # dbs_diff_fingerprints already records PASS/FAIL per table via as_record
    # (see its own doc comment); its non-zero return is informational, NOT
    # meant to be fatal. Called bare under `set -euo pipefail` (line 55) a
    # single mismatch would kill the whole script on the spot, silently
    # truncating phases 16-18 (issue #617) — guard it like
    # selftest-rollback-fault-injection.sh already does.
    if dbs_diff_fingerprints "$fp_dir" "$TEST_ROOT/snapshots/restored/db-fingerprint" "restore"; then
        gr_log "restore: all table content digests match the pre-restore snapshot"
    else
        gr_warn "restore: at least one table content digest differs from the pre-restore snapshot — recorded as FAIL above, continuing"
    fi

    # R-6: no table introduced by a post-FROM migration survives the restore —
    # derived from the table-list snapshots (after MINUS before), never a
    # hardcoded name.
    local new_tables leaked
    new_tables="$(comm -23 <(sort "$TEST_ROOT/snapshots/after/tables.txt") <(sort "$TEST_ROOT/snapshots/before/tables.txt"))"
    leaked="$(comm -12 <(echo "$new_tables") <(sort "$TEST_ROOT/snapshots/restored/tables.txt"))"
    as_assert "R-6: no post-FROM-migration table survives the restore" '[[ -z "$leaked" ]]'
    [[ -n "$leaked" ]] && gr_warn "post-FROM tables that survived the restore: $leaked"

    # R-7: alembic_version restored to the FROM release's OWN head, derived
    # from that release's migration chain in the phase-03 worktree — the same
    # measured-vs-derived pair phase 10 already uses, replayed here after a
    # restore instead of after a forward migration.
    local restored_head derived_from_head
    restored_head="$(docker exec "$pg" psql -tA -U postgres opentranscribe \
        -c "SELECT version_num FROM alembic_version;" 2>/dev/null | tr -d '[:space:]')"
    local from_worktree="$TEST_ROOT/worktree-${FROM_VERSION}"
    if derived_from_head="$(ver_alembic_head "$from_worktree/backend" 2>/dev/null)"; then
        as_assert_eq "R-7: alembic_version restored to the FROM release's own head" "$derived_from_head" "$restored_head"
    else
        as_record SKIP "R-7: alembic head" "FROM worktree chain is not single-headed"
    fi

    # R-8: no duplicate rows — the append-instead-of-replace failure mode
    # (issue #598 §3) produces exactly this on a table without a PK conflict.
    local t label total distinct_ct
    for t in media_file transcript_segment speaker '"user"'; do
        label="${t//\"/}"
        total="$(docker exec "$pg" psql -tA -U postgres opentranscribe -c "SELECT count(*) FROM ${t};" 2>/dev/null | tr -d '[:space:]')"
        distinct_ct="$(docker exec "$pg" psql -tA -U postgres opentranscribe -c "SELECT count(DISTINCT id) FROM ${t};" 2>/dev/null | tr -d '[:space:]')"
        as_assert_eq "R-8: no duplicate rows in ${label}" "${total:-?}" "${distinct_ct:-?}"
    done

    # R-11: the DB restore must not disturb MinIO — the honest scoping
    # statement that media is not part of what this mechanism restores.
    local minio_post="$TEST_ROOT/snapshots/restored/minio_etags.json"
    _capture_minio_etags "$minio_post"
    as_assert_diff_files "R-11: MinIO ETag list unchanged by the DB-only restore" "$minio_pre" "$minio_post"

    # R-3/R-4/R-5 moved to phase 16 (issue #610): they assert that restored data is
    # visible THROUGH THE APPLICATION, but per R-13 above, nothing is up here to serve
    # it any more — restore now leaves the app stopped on purpose. Serving a
    # FROM-schema database is the FROM image's job (this phase's subject is whatever
    # was running before the restore, i.e. TO), so the checks belong in phase 16,
    # after `update --rollback` has put the FROM image in front of it. Before this
    # fix they ran here and passed only because the STILL-RUNNING TO image
    # force-migrated the data into a shape it could read — i.e. they were green
    # BECAUSE of the bug.
}

phase_16_rollback_and_assert() {
    TEST_REPORT_FILE="${TEST_REPORT_FILE:-$TEST_ROOT/REPORT.md}"
    export TEST_REPORT_FILE
    if [[ "${ROLLBACK_INJECT_FAULT:-}" == "truncate" ]]; then
        as_record SKIP "B-1..B-8: update --rollback" \
            "ROLLBACK_INJECT_FAULT=truncate: phase 15 already proved the corruption is caught (R-2/R-8 FAILed); the database is NOT empty (the truncated dump replays with exit 0 and partial data — see phase 15's comment), so continuing to roll images back onto known-corrupt fixture data would only carry it forward without exercising anything --rollback-specific"
        # R-3/R-4/R-5 (issue #610) now live in THIS phase, not phase 15 — without an
        # explicit record here, this early return would silently drop three
        # assertions from the fault-injection run instead of skipping them visibly.
        as_record SKIP "R-3: deleted file restored" "ROLLBACK_INJECT_FAULT=truncate: phase 16 (which now runs R-3/R-4/R-5) was itself skipped, see above"
        as_record SKIP "R-4: speaker rename reverted" "ROLLBACK_INJECT_FAULT=truncate: phase 16 (which now runs R-3/R-4/R-5) was itself skipped, see above"
        as_record SKIP "R-5: post-backup upload absent" "ROLLBACK_INJECT_FAULT=truncate: phase 16 (which now runs R-3/R-4/R-5) was itself skipped, see above"
        return 0
    fi

    local stage_rollback="$TEST_ROOT/rollback"
    pushd "$stage_rollback" >/dev/null
    gr_log "running './opentranscribe.sh update --rollback' (real user recovery path)"
    local rollback_output rollback_rc=0
    rollback_output="$(./opentranscribe.sh update --rollback 2>&1)" || rollback_rc=$?
    echo "$rollback_output" > "$TEST_ROOT/rollback-output.txt"
    popd >/dev/null

    as_assert_eq "B-1: update --rollback exits 0" "0" "$rollback_rc"

    local env_tag
    # python-dotenv, not grep/cut (issue #590).
    env_tag="$(python3 "$SCRIPT_DIR/../lib/env_reader.py" "$stage_rollback/.env" OT_IMAGE_TAG)"
    as_assert_eq "B-2: staged .env now pins OT_IMAGE_TAG to FROM" "$FROM_VERSION" "$env_tag"

    as_assert "B-3: rollback warns about the one-way migration chain" \
        '[[ "$rollback_output" == *"ONE-WAY"* ]]'
    as_assert "B-3: rollback tells the operator to restore a pre-upgrade backup" \
        '[[ "$rollback_output" == *backup* ]]'

    # B-4: EVERY APP-OWNED opentranscribe-* container resolves :$FROM_VERSION,
    # enumerated from `docker ps` — not a hardcoded service list (the same
    # drift class test_every_prod_service_image_is_tag_pinnable guards
    # statically). Scoped to images from the davidamacey/opentranscribe-*
    # Docker Hub repo (backend/frontend/celery-*/flower/docs — every service
    # that resolves `${OT_IMAGE_TAG:-latest}` in docker-compose.prod.yml), NOT
    # every container whose NAME starts with opentranscribe-: postgres,
    # opensearch, minio, and redis also get that container_name prefix (see
    # docker-compose.yml) but are third-party infra images with their own
    # independent version pins (postgres:17.5-alpine etc.) that never move
    # per OpenTranscribe release — checking them against FROM_VERSION was
    # always going to fail regardless of whether the rollback worked.
    local mismatched=() app_containers=() cname image
    while IFS= read -r cname; do
        [[ -n "$cname" ]] || continue
        image="$(docker inspect "$cname" --format '{{.Config.Image}}' 2>/dev/null || echo "")"
        case "$image" in
            davidamacey/opentranscribe-*)
                app_containers+=("$cname")
                case "$image" in
                    *":${FROM_VERSION}") ;;
                    *) mismatched+=("$cname=$image") ;;
                esac
                ;;
            *) ;; # third-party infra image, not app-versioned — see comment above
        esac
    done < <(printf '%s\n%s\n' \
        "$(docker ps --format '{{.Names}}' --filter 'name=^opentranscribe-')" \
        "$(docker ps --format '{{.Names}}' \
            --filter 'label=com.docker.compose.project=opentranscribe' \
            --filter 'label=com.docker.compose.service=diar-native')" \
        | sed '/^$/d' | sort -u)
    # `diar-native` has no `container_name`, so its default compose name
    # (`opentranscribe-diar-native-<n>`, since this rehearsal pins
    # COMPOSE_PROJECT_NAME=opentranscribe) already matches the name filter above in THIS
    # rehearsal — the extra project-label filter is defense-in-depth against that pin ever
    # changing, since diar-native runs `davidamacey/opentranscribe-backend` (the shared
    # image) and is exactly the kind of app-versioned container this loop must not miss.

    # #619's own root cause, guarded against here: with ZERO app containers running,
    # `mismatched` is trivially empty and the assertion below PASSES having checked
    # nothing at all. Assert containers actually exist BEFORE trusting an empty
    # mismatch list.
    if (( ${#app_containers[@]} == 0 )); then
        gr_warn "B-4: no app-owned opentranscribe-* containers found via docker ps — $(docker ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null | tr '\n' '; ')"
    fi
    as_assert "B-4a: app-owned opentranscribe-* containers are running at all" \
        '(( ${#app_containers[@]} > 0 ))'
    as_assert "B-4a: backend and frontend containers are present" \
        '[[ " ${app_containers[*]} " == *" opentranscribe-backend "* && " ${app_containers[*]} " == *" opentranscribe-frontend "* ]]'
    as_assert "B-4: every app-owned opentranscribe-* image resolves :${FROM_VERSION}" \
        '(( ${#mismatched[@]} == 0 ))'
    [[ ${#mismatched[@]} -gt 0 ]] && gr_warn "app images not on ${FROM_VERSION}: ${mismatched[*]}"

    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    ac_wait_for_health 900

    # B-5 assumes the FROM image can even answer /api/version -- it can't for
    # any FROM predating v0.5.0. The endpoint itself (commit 5d4f9164) and the
    # ARG APP_VERSION build-arg contract it depends on (commit c8a332e8) were
    # both added IN v0.5.0. Measured directly: v0.4.1's worktree has no
    # backend/app/api/endpoints/version.py and no ARG line in Dockerfile.prod,
    # and the phase-06 `before` snapshot (captured from the live FROM stack,
    # before any upgrade ran) already recorded {"version":"unavailable"} --
    # so this was never about the rollback, the route is simply a 404 on that
    # image. Gate on a capability derived from the FROM worktree rather than
    # hardcoding a version cutoff, matching R-6/R-7/ver_alembic_head's own
    # "derive, don't hardcode" discipline. This self-heals for the next
    # release (v0.5.0 -> v0.6.0): FROM will then ship both, and the else
    # branch runs unmodified.
    local from_worktree="$TEST_ROOT/worktree-${FROM_VERSION}"
    local from_has_endpoint=false from_has_buildarg=false
    [[ -f "$from_worktree/backend/app/api/endpoints/version.py" ]] && from_has_endpoint=true
    grep -qE '^ARG[[:space:]]+APP_VERSION' "$from_worktree/backend/Dockerfile.prod" 2>/dev/null \
        && from_has_buildarg=true

    if [[ "$from_has_endpoint" != true ]]; then
        as_record SKIP "B-5: /api/version reports FROM after rollback" \
            "${FROM_VERSION} predates the /api/version endpoint (added in v0.5.0, commit 5d4f9164) -- the published image has no such route and cannot be changed retroactively. B-4 already proves every running app image resolves :${FROM_VERSION}."
        as_record SKIP "B-5: /api/version is not 'unknown' (build-arg contract)" \
            "same reason"
        # Positive proof the running binary really is the old one: a TO image
        # would answer 200, not 404.
        #
        # No -f: it makes curl treat the (expected, correct) 404 as a
        # request FAILURE and exit non-zero -- but -w's write-out has
        # already printed "404" to stdout by then, so the `|| echo "000"`
        # fallback this used to have would ALSO fire and get concatenated
        # onto it, producing the literal string "404000". Measured live.
        local version_status
        version_status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$API_BASE/version" 2>/dev/null)"
        as_assert_eq "B-5: /api/version 404s on a FROM image predating the endpoint" "404" "${version_status:-000}"
    elif [[ "$from_has_buildarg" != true ]]; then
        as_record SKIP "B-5: /api/version reports FROM after rollback" \
            "${FROM_VERSION}'s Dockerfile.prod has no ARG APP_VERSION (added in v0.5.0, commit c8a332e8) -- the published image reports 'unknown' by construction"
        as_record SKIP "B-5: /api/version is not 'unknown' (build-arg contract)" "same reason"
    else
        local running_version
        running_version="$(curl -fsS --max-time 10 "$API_BASE/version" 2>/dev/null \
            | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4 || echo "")"
        # De-vacuum: an empty response must not satisfy the "not unknown" check below.
        as_assert "B-5: /api/version returned a version field" '[[ -n "$running_version" ]]'
        as_assert_eq "B-5: /api/version reports FROM after rollback" \
            "$FROM_VERSION" "$(ver_normalize "${running_version:-none}" 2>/dev/null || echo "${running_version:-none}")"
        as_assert_ne "B-5: /api/version is not 'unknown' (build-arg contract)" "unknown" "${running_version:-none}"
    fi

    local login_ok=false
    if ac_login "$TEST_ADMIN_EMAIL" "$TEST_ADMIN_PASSWORD"; then
        login_ok=true
        as_record PASS "B-6: login succeeds — the FROM image serves the restored FROM database"

        local files_json file_count expected_count
        files_json="$(ac_list_files 2>/dev/null || echo '{}')"
        file_count="$(echo "$files_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
items = d.get("files") if isinstance(d, dict) else d
items = items if isinstance(items, list) else (d.get("items") if isinstance(d, dict) else [])
print(len(items or []))
' 2>/dev/null || echo -1)"
        expected_count="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
items = d.get("files") if isinstance(d, dict) else d
items = items if isinstance(items, list) else (d.get("items") if isinstance(d, dict) else [])
print(len(items or []))
' "$TEST_ROOT/snapshots/before/files.json" 2>/dev/null || echo -2)"
        as_assert_eq "B-6: GET /api/files returns exactly the pre-upgrade file set" "$expected_count" "$file_count"

        # B-7: transcript text/timing for every seeded file matches the
        # pre-upgrade snapshot EXACTLY (not merely a prefix, unlike phase 10's
        # forward-upgrade check — a rollback claims to reproduce the pre-backup
        # state, not extend it).
        local fid pre_transcript post_transcript
        while IFS= read -r fid; do
            [[ -n "$fid" ]] || continue
            pre_transcript="$TEST_ROOT/snapshots/before/transcript-$fid.json"
            [[ -s "$pre_transcript" ]] || continue
            post_transcript="$(mktemp)"
            ac_get_segments "$fid" > "$post_transcript" 2>/dev/null || true
            local detail
            detail=""
            if detail="$(python3 - "$pre_transcript" "$post_transcript" "$fid" <<'PY'
import json, sys
pre, post, fid = sys.argv[1:4]
def segs(p):
    try:
        d = json.load(open(p))
    except Exception:
        return None
    return d.get("segments") or d.get("transcript_segments") or []
pre_segs = segs(pre)
post_segs = segs(post)
ok = pre_segs is not None and post_segs is not None and len(post_segs) == len(pre_segs)
if ok:
    for s, ps in zip(pre_segs, post_segs):
        if s.get("text") != ps.get("text") or abs((s.get("start") or 0) - (ps.get("start") or 0)) > 0.01:
            ok = False
            break
print(f"pre={len(pre_segs or [])} post={len(post_segs or [])}")
sys.exit(0 if ok else 1)
PY
)"; then
                as_record PASS "B-7: transcript for file $fid matches the pre-upgrade snapshot exactly"
            else
                as_record FAIL "B-7: transcript for file $fid matches the pre-upgrade snapshot exactly" "$detail"
            fi
            rm -f "$post_transcript"
        done < "$TEST_ROOT/seeded-file-ids.txt"
    else
        as_record FAIL "B-6: login to the rolled-back stack"
    fi

    # R-3/R-4/R-5 (issue #610), relocated here from phase 15: these assert that data a
    # restore brought back is visible THROUGH THE APPLICATION, and per phase 15's R-13,
    # nothing was up there to serve it any more — restore now leaves the app stopped on
    # purpose. The application that should serve a FROM-schema database is the FROM
    # image, i.e. THIS phase, after `update --rollback` has put it in front of the
    # restored database — not whatever was running when phase 15's restore ran. Before
    # this fix these ran in phase 15 and passed only because the STILL-RUNNING TO image
    # force-migrated the restored data into a shape it could read — green BECAUSE of
    # the bug, not despite it. Reuses B-6's own login rather than logging in twice.
    if [[ "$login_ok" == true ]]; then
        # R-3: the file deleted in phase 14 is back, addressable via the API.
        local deleted_fid
        deleted_fid="$(cat "$TEST_ROOT/damage-deleted-file-id.txt" 2>/dev/null || echo "")"
        if [[ -n "$deleted_fid" ]]; then
            local code
            code="$(curl -o /dev/null -s -w '%{http_code}' -H "Authorization: Bearer ${API_TOKEN:-}" "$API_BASE/files/$deleted_fid")"
            as_assert_http "R-3: file deleted in phase 14 is back, addressable via the API" 200 "$code"
        else
            as_record SKIP "R-3: deleted file restored" "no deleted-file id recorded (phase 14's damage step was skipped)"
        fi

        # R-4: the renamed speaker has its old name back.
        if [[ -f "$TEST_ROOT/damage-speaker.txt" ]]; then
            local sp_uuid sp_old sp_new current_name
            { read -r sp_uuid; read -r sp_old; read -r sp_new; } < "$TEST_ROOT/damage-speaker.txt"
            current_name="$(ac_curl "$API_BASE/speakers/$sp_uuid" 2>/dev/null | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d.get("name") or (d.get("speaker") or {}).get("name", ""))
' 2>/dev/null || echo "")"
            as_assert_eq "R-4: renamed speaker has its pre-damage name" "$sp_old" "$current_name"
            [[ "$current_name" == "$sp_new" ]] && gr_warn "speaker still carries the phase-14 damage name ($sp_new) — the restore left it in place"
        else
            as_record SKIP "R-4: speaker rename reverted" "no speaker was renamed in phase 14"
        fi

        # R-5: the phase-11 POST-backup upload is ABSENT — a restore that leaves
        # it is a merge, not a replace. This is the assertion that fails under a
        # `--clean`-only fix (issue #598 §3).
        if [[ -f "$TEST_ROOT/post-upgrade-new-file-id.txt" ]]; then
            local post_fid post_code
            post_fid="$(cat "$TEST_ROOT/post-upgrade-new-file-id.txt")"
            post_code="$(curl -o /dev/null -s -w '%{http_code}' -H "Authorization: Bearer ${API_TOKEN:-}" "$API_BASE/files/$post_fid")"
            as_assert_http "R-5: post-backup upload (phase 11) is ABSENT after restore" 404 "$post_code"
        else
            as_record SKIP "R-5: post-backup upload absent" "phase 11 recorded no new-file id (skipped or no suitable media?)"
        fi
    else
        as_record SKIP "R-3: deleted file restored" "login to the rolled-back stack failed, see B-6"
        as_record SKIP "R-4: speaker rename reverted" "login to the rolled-back stack failed, see B-6"
        as_record SKIP "R-5: post-backup upload absent" "login to the rolled-back stack failed, see B-6"
    fi

    # B-8: nothing above waits for the FROM image's frontend container
    # specifically (only the backend, via ac_wait_for_health above) — wait for
    # it to answer before checking it, then guard the actual check too so any
    # curl failure here is recorded as a FAIL rather than crashing the whole
    # script under set -e (issue #618, same class as #617's
    # dbs_diff_fingerprints crash).
    local frontend_url="http://localhost:${TEST_FRONTEND_PORT}/"
    ac_wait_for_frontend "$frontend_url" 900 || true
    local frontend_code
    if frontend_code="$(curl -o /dev/null -s -w '%{http_code}' "$frontend_url")"; then
        as_assert_http "B-8: frontend reachable on the FROM image" 200 "$frontend_code"
    else
        as_record FAIL "B-8: frontend reachable on the FROM image" "curl failed to reach $frontend_url"
    fi
}

phase_17_roll_forward_again() {
    TEST_REPORT_FILE="${TEST_REPORT_FILE:-$TEST_ROOT/REPORT.md}"
    export TEST_REPORT_FILE
    if [[ "${ROLLBACK_INJECT_FAULT:-}" == "truncate" ]]; then
        as_record SKIP "F-1..F-5: roll forward again" \
            "ROLLBACK_INJECT_FAULT=truncate — phase 16 was itself skipped (see its own reason); nothing to roll forward from"
        return 0
    fi

    local stage_rollback="$TEST_ROOT/rollback"
    pushd "$stage_rollback" >/dev/null
    gr_log "running './opentranscribe.sh update --version ${LOCAL_IMAGE_TAG}' again (recovery loop: roll back -> investigate -> re-upgrade)"
    ./opentranscribe.sh update --version "$LOCAL_IMAGE_TAG" || gr_die "recovery re-upgrade failed"
    popd >/dev/null

    API_BASE="http://localhost:${TEST_BACKEND_PORT}/api"
    export API_BASE
    ac_wait_for_health 900
    as_record PASS "F-1: health reached within the 900s budget after the recovery re-upgrade"

    local pg="opentranscribe-postgres"
    local post_head expected_head
    post_head="$(docker exec "$pg" psql -tA -U postgres opentranscribe -c "SELECT version_num FROM alembic_version;" 2>/dev/null | tr -d '[:space:]')"
    expected_head="$(ver_alembic_head "$REPO_ROOT/backend")"
    as_assert_eq "F-2: alembic head re-migrated to the current head" "$expected_head" "$post_head"

    local running_version
    running_version="$(curl -fsS --max-time 10 "$API_BASE/version" 2>/dev/null \
        | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4 || echo "")"
    as_assert_eq "F-3: /api/version reports TO after the recovery re-upgrade" \
        "$TO_VERSION" "$(ver_normalize "${running_version:-none}" 2>/dev/null || echo "${running_version:-none}")"

    # F-4: the restored data survived a SECOND migration. alembic_version is
    # deliberately excluded from the comparison — it is SUPPOSED to differ
    # (FROM head before, TO head now); that is schema advancement, not damage.
    #
    # But excluding alembic_version is necessary, not sufficient: dbs_fingerprint
    # hashes the WHOLE row, and TO's schema added 15 columns to media_file
    # between v0.4.1 (61 columns) and v0.5.0 (76). Adding a column changes
    # every row's t::text with zero stored values touched, so the plain
    # dbs_diff_fingerprints comparison below always "failed" once the schema
    # advanced — measured: restricting the comparison to the 61 columns that
    # existed when `before` was captured reproduces that exact digest
    # (01ce171b86eecd9a6a8e0a0830016251) against this same recovered database,
    # proving the data survived byte-for-byte. Scoped to media_file
    # deliberately, not widened to the other fingerprinted tables:
    # tag.normalized_name is a SHARED column the upgrade legitimately
    # backfills (NULL -> a computed value on the seeded system tags), so an
    # unrestricted or column-restricted diff on `tag` would still show a
    # real, expected change that is not damage either way.
    dbs_fingerprint "$pg" postgres opentranscribe "$TEST_ROOT/snapshots/recovered/db-fingerprint"

    local before_cols="$TEST_ROOT/snapshots/before/db-fingerprint/media_file.columns"
    local before_digest recovered_digest
    before_digest="$(cat "$TEST_ROOT/snapshots/before/db-fingerprint/media_file.digest" 2>/dev/null || echo '?')"
    if [[ -s "$before_cols" ]] && \
       recovered_digest="$(dbs_digest_baseline_columns "$pg" postgres opentranscribe media_file "$before_cols")"; then
        as_assert_eq "F-4: media_file content digest unchanged (FROM-schema columns)" \
            "$before_digest" "$recovered_digest"
    else
        as_record SKIP "F-4: media_file content digest unchanged" \
            "a column present at ${FROM_VERSION} no longer exists at ${TO_VERSION} (DROP/RENAME), or the pre-upgrade column list was never captured -- the pre-upgrade whole-row digest is not reproducible; compare by column set, not by digest"
    fi

    # F-5: hybrid search returns hits (reindex recovered).
    local hits=0 waited=0
    while [ "$waited" -lt 300 ]; do
        hits="$(ac_search "the" 2>/dev/null | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d.get("total_results") or len(d.get("results") or d.get("hits") or []))
' 2>/dev/null || echo 0)"
        [ "$hits" -ge 1 ] && break
        sleep 10
        waited=$((waited + 10))
    done
    as_assert_ge "F-5: hybrid search returns hits after recovery" "$hits" 1

    gr_log "recovery complete — stack is back at TO=${TO_VERSION}, matching the scenario's leave-behind contract"
}

phase_18_summary() {
    TEST_REPORT_FILE="${TEST_REPORT_FILE:-$TEST_ROOT/REPORT.md}"
    # as_summary deliberately returns 1 when any assertion FAILed — that is
    # how 65-rehearse.sh's `... test-upgrade.sh --yes || upgrade_rc=$?` knows
    # the run failed. But under set -o pipefail (set -euo pipefail, line 55),
    # a non-zero return from EITHER stage of `as_summary | tee -a ...` trips
    # set -e right here, on the spot — same class of bug as #617/#618, just at
    # the very last phase: it skipped the "Finished:" line, 18.done, and the
    # driver's closing banner below, discovered only because #618's other two
    # fixes let a real run finally reach this phase. RELEASE_TEST_EXIT_CODE
    # (global, read by the driver after this phase returns) carries the
    # intended non-zero verdict forward WITHOUT letting it abort the script
    # before the report is finished and phase 18 is marked done.
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

echo "OpenTranscribe Release Test — Scenario B (upgrade ${FROM_VERSION} → ${LOCAL_IMAGE_TAG})"
echo "Started: $(date -Iseconds)"
echo "Repo:    $REPO_ROOT (commit $(git -C "$REPO_ROOT" rev-parse --short HEAD))"
echo

phase 00 phase_00_preflight
phase 01 phase_01_build_local_images
phase 01b phase_01b_build_docs_image
phase 02 phase_02_verify_from_version
phase 03 phase_03_prepare_v033_compose
phase 04 phase_04_start_from_stack
phase 05 phase_05_seed_data
phase 06 phase_06_snapshot_pre
phase 06b phase_06b_pre_upgrade_backup
phase 07 phase_07_swap_to_new
phase 08 phase_08_start_new
phase 09 phase_09_snapshot_post
phase 10 phase_10_assert_and_report
phase 11 phase_11_new_data_post_upgrade
phase 12 phase_12_assert_rollback_precondition
if [[ "$ROLLBACK_REHEARSAL" == "1" ]]; then
    phase 13 phase_13_stage_rollback_tree
    phase 14 phase_14_damage_database
    phase 15 phase_15_restore_and_assert
    phase 16 phase_16_rollback_and_assert
    phase 17 phase_17_roll_forward_again
else
    gr_warn "rollback rehearsal tail SKIPPED (ROLLBACK_REHEARSAL=0 / --no-rollback)"
fi
phase 18 phase_18_summary

echo
echo "Done. Report: $TEST_ROOT/REPORT.md"
echo "Stack left running for inspection. Tear down with: $0 --cleanup"

# Propagate phase 18's assertion verdict as the script's own exit code (see
# phase_18_summary's comment) — deferred to here, after the report is fully
# written and every phase is marked done, rather than however set -e would
# have aborted mid-summary. Defaults to 0 for a resumed run where phase 18
# was already marked done and skipped (phase_check short-circuits it, so
# RELEASE_TEST_EXIT_CODE is never assigned).
exit "${RELEASE_TEST_EXIT_CODE:-0}"
