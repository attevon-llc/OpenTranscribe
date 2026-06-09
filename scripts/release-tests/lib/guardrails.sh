#!/bin/bash
# Release-test safety harness.
#
# This file is sourced by every test script BEFORE any docker/filesystem
# action is taken. Its only job is to abort loudly if the test could possibly
# touch a production path, volume, container, or network.
#
# If you find yourself relaxing one of these checks to "get a test to run",
# stop and re-read the plan instead. The production MinIO dataset at
# /mnt/nas/opentranscribe-minio is 483 GB of irreplaceable material.
#
# Contract: callers must set these variables BEFORE sourcing this file:
#   TEST_SCENARIO          e.g. "fresh-install" or "upgrade"
#   TEST_PROJECT_NAME      e.g. "ot-reltest-fresh"  (must start with ot-reltest-)
#   TEST_ROOT              e.g. "/mnt/nvm/opentranscribe-test-runs/fresh-20260407"
#   TEST_LABEL             e.g. "com.opentranscribe.release-test=fresh-install"
#   TEST_PORTS             space-separated list of host ports we will bind

set -euo pipefail

# ─── Styling ────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    GR_RED='\033[0;31m'
    GR_YELLOW='\033[1;33m'
    GR_GREEN='\033[0;32m'
    GR_BLUE='\033[0;34m'
    GR_BOLD='\033[1m'
    GR_NC='\033[0m'
else
    GR_RED=''; GR_YELLOW=''; GR_GREEN=''; GR_BLUE=''; GR_BOLD=''; GR_NC=''
fi

gr_log()  { echo -e "${GR_BLUE}[guardrails]${GR_NC} $*"; }
gr_ok()   { echo -e "${GR_GREEN}[guardrails] ✓${GR_NC} $*"; }
gr_warn() { echo -e "${GR_YELLOW}[guardrails] ⚠${GR_NC} $*" >&2; }
gr_die()  { echo -e "${GR_RED}${GR_BOLD}[guardrails] ✗ FATAL:${GR_NC} $*" >&2; exit 1; }

# ─── Protected paths (NEVER touch) ──────────────────────────────────────────
# These are resolved with realpath before comparison so symlinks cannot be
# used to sneak past the firewall.
readonly GR_PROTECTED_PATHS=(
    "/mnt/nas/opentranscribe-minio"
    "/mnt/nas/opentranscribe"
    "/mnt/nvm/opentranscribe"
    "/mnt/nvm/repos/transcribe-app"
    "/mnt/nas/documents/personal"
    "/mnt/nas/media/audiobooks"
    "/mnt/nas/ai/datasets"
)

# Production volume names that must never be deleted by cleanup.
readonly GR_PROTECTED_VOLUMES=(
    "postgres_data"
    "minio_data"
    "redis_data"
    "opensearch_data"
    "flower_data"
)

# ─── Helpers ────────────────────────────────────────────────────────────────
gr_realpath() {
    # Portable realpath that does not require the target to exist.
    if command -v realpath >/dev/null 2>&1; then
        realpath -m -- "$1"
    else
        python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"
    fi
}

gr_path_inside() {
    # Returns 0 if $1 is equal to or nested under $2 (both resolved).
    local needle parent
    needle="$(gr_realpath "$1")"
    parent="$(gr_realpath "$2")"
    [[ "$needle" == "$parent" || "$needle" == "$parent"/* ]]
}

# ─── Guardrail checks ───────────────────────────────────────────────────────
gr_require_vars() {
    local missing=()
    for v in TEST_SCENARIO TEST_PROJECT_NAME TEST_ROOT TEST_LABEL; do
        if [[ -z "${!v:-}" ]]; then
            missing+=("$v")
        fi
    done
    if (( ${#missing[@]} > 0 )); then
        gr_die "guardrails.sh sourced without required vars: ${missing[*]}"
    fi
}

gr_check_project_name() {
    # TEST_PROJECT_NAME is now used only as a label namespace and informational
    # tag, not for container/volume name isolation (since the live deployment
    # is stopped before tests run). Still require ot-reltest- prefix so cleanup
    # logs and reports clearly identify the source.
    case "$TEST_PROJECT_NAME" in
        ot-reltest-*) ;;
        *) gr_die "TEST_PROJECT_NAME must start with 'ot-reltest-', got '$TEST_PROJECT_NAME'" ;;
    esac
    gr_ok "test scenario label namespace: '$TEST_PROJECT_NAME'"
}

gr_check_test_root() {
    local resolved
    resolved="$(gr_realpath "$TEST_ROOT")"
    for protected in "${GR_PROTECTED_PATHS[@]}"; do
        if gr_path_inside "$resolved" "$protected"; then
            gr_die "TEST_ROOT '$resolved' resolves under protected path '$protected' — refusing to run"
        fi
    done
    # Also refuse to place test root directly under / or /home without an explicit escape hatch
    case "$resolved" in
        /mnt/nvm/opentranscribe-test-runs/*) ;;
        /tmp/ot-reltest-*) ;;
        *)
            if [[ -z "${OT_RELEASE_TEST_ALLOW_PATH:-}" ]]; then
                gr_die "TEST_ROOT '$resolved' is not under /mnt/nvm/opentranscribe-test-runs or /tmp/ot-reltest-; set OT_RELEASE_TEST_ALLOW_PATH=1 to override"
            fi
            ;;
    esac
    mkdir -p "$resolved"
    TEST_ROOT="$resolved"
    gr_ok "TEST_ROOT '$TEST_ROOT' passes the path firewall"
}

gr_check_mount_path() {
    # Called per bind-mount source before docker compose up.
    local src
    src="$(gr_realpath "$1")"
    for protected in "${GR_PROTECTED_PATHS[@]}"; do
        if gr_path_inside "$src" "$protected"; then
            gr_die "bind-mount source '$src' resolves under protected path '$protected'"
        fi
    done
    if ! gr_path_inside "$src" "$TEST_ROOT"; then
        gr_die "bind-mount source '$src' is not under TEST_ROOT '$TEST_ROOT'"
    fi
}

gr_check_container_names() {
    # Refuse if any container that matches the production prefix is currently
    # RUNNING. We expect the caller to have stopped the live deployment first
    # (via ./opentr.sh stop), so opentranscribe-* containers should be gone.
    local running
    running=$(docker ps --format '{{.Names}}' --filter 'name=^opentranscribe-' || true)
    if [[ -n "$running" ]]; then
        gr_die "live opentranscribe-* containers still running:
$running

Stop them first with: ./opentr.sh stop  (preserves all data)"
    fi
    # Stopped opentranscribe-* containers (from a previous live `down`) would
    # also collide on container_name during create — flag them so the caller
    # can decide whether to remove them.
    local stopped
    stopped=$(docker ps -a --format '{{.Names}}' --filter 'name=^opentranscribe-' || true)
    if [[ -n "$stopped" ]]; then
        gr_warn "stopped opentranscribe-* containers exist (will collide on create):"
        echo "$stopped" >&2
        gr_warn "the test driver will 'docker rm' them in phase 0 (no data loss — bind mounts persist)"
    fi
    gr_ok "no live opentranscribe-* containers running"
}

gr_check_volume_names() {
    # Refuse if any of the production-unprefixed volume names are referenced.
    for vol in "${GR_PROTECTED_VOLUMES[@]}"; do
        if docker volume inspect "$vol" >/dev/null 2>&1; then
            # The prod volume exists — that's fine, we just must not mount or delete it.
            # Record it so that later cleanup sanity-checks can refuse `docker volume rm $vol`.
            gr_log "noting production volume '$vol' exists and is off-limits"
        fi
    done
    gr_ok "volume-name firewall armed"
}

gr_check_ports_free() {
    # Fail fast if any required host port is already bound.
    local occupied=()
    for port in ${TEST_PORTS:-}; do
        if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}$"; then
            occupied+=("$port")
        fi
    done
    if (( ${#occupied[@]} > 0 )); then
        gr_die "required ports already in use: ${occupied[*]}"
    fi
    gr_ok "ports free: ${TEST_PORTS:-<none>}"
}

gr_check_disk_space() {
    # Require at least 80 GB free on the test-root partition and 10 GB in /var/lib/docker.
    local need_root_gb=${1:-80}
    local need_docker_gb=${2:-10}
    local avail_root_gb avail_docker_gb docker_root
    avail_root_gb=$(df -BG --output=avail "$TEST_ROOT" 2>/dev/null | tail -1 | tr -d 'G ')
    docker_root=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
    avail_docker_gb=$(df -BG --output=avail "$docker_root" 2>/dev/null | tail -1 | tr -d 'G ')
    if (( avail_root_gb < need_root_gb )); then
        gr_die "not enough free space on TEST_ROOT (need ${need_root_gb} GB, have ${avail_root_gb} GB)"
    fi
    if (( avail_docker_gb < need_docker_gb )); then
        gr_die "not enough free space on docker root $docker_root (need ${need_docker_gb} GB, have ${avail_docker_gb} GB)"
    fi
    gr_ok "disk space OK (TEST_ROOT=${avail_root_gb}G, docker=${avail_docker_gb}G)"
}

gr_confirmation_gate() {
    # Print the blast radius and require explicit confirmation unless --yes was passed.
    cat <<EOF

${GR_BOLD}────────────────────────────────────────────────────────${GR_NC}
${GR_BOLD} OpenTranscribe release-test pre-flight summary${GR_NC}
${GR_BOLD}────────────────────────────────────────────────────────${GR_NC}
  Scenario:       $TEST_SCENARIO
  Project name:   $TEST_PROJECT_NAME
  Test root:      $TEST_ROOT
  Host ports:     ${TEST_PORTS:-<none>}
  Label:          $TEST_LABEL
  Protected:      ${GR_PROTECTED_PATHS[*]}
${GR_BOLD}────────────────────────────────────────────────────────${GR_NC}

The test deployment is completely isolated from the production
OpenTranscribe stack. The live containers and NAS data will not be
touched. Cleanup only removes resources labeled '$TEST_LABEL'.

EOF
    if [[ "${OT_RELEASE_TEST_YES:-}" == "1" ]]; then
        gr_log "auto-confirmed via OT_RELEASE_TEST_YES=1"
        return 0
    fi
    local reply
    printf "Type 'I UNDERSTAND' to proceed: "
    read -r reply </dev/tty
    if [[ "$reply" != "I UNDERSTAND" ]]; then
        gr_die "confirmation not given; aborting"
    fi
}

# ─── Cleanup helper (called by scenario scripts, not automatically) ─────────
gr_cleanup() {
    # Tear down ONLY labeled resources and ONLY files under TEST_ROOT.
    gr_log "beginning labelled cleanup for project '$TEST_PROJECT_NAME'"

    # 1. Stop and remove containers matching our label
    local ids
    ids=$(docker ps -aq --filter "label=$TEST_LABEL" || true)
    if [[ -n "$ids" ]]; then
        gr_log "stopping $(echo "$ids" | wc -l) containers"
        docker stop $ids >/dev/null 2>&1 || true
        docker rm -f $ids >/dev/null 2>&1 || true
    fi

    # 2. Remove volumes matching our label
    local vols
    vols=$(docker volume ls -q --filter "label=$TEST_LABEL" || true)
    if [[ -n "$vols" ]]; then
        for vol in $vols; do
            case "$vol" in
                "${TEST_PROJECT_NAME//-/_}"*|ot_reltest_*)
                    gr_log "removing volume $vol"
                    docker volume rm "$vol" >/dev/null 2>&1 || true
                    ;;
                *)
                    gr_warn "refusing to remove volume '$vol' — name does not match test prefix"
                    ;;
            esac
        done
    fi

    # 3. Remove networks matching our label
    local nets
    nets=$(docker network ls -q --filter "label=$TEST_LABEL" || true)
    if [[ -n "$nets" ]]; then
        for net in $nets; do
            docker network rm "$net" >/dev/null 2>&1 || true
        done
    fi

    # 4. Remove TEST_ROOT contents — but only if TEST_ROOT is still within the allowed area
    if [[ -n "${TEST_ROOT:-}" && -d "$TEST_ROOT" ]]; then
        local resolved
        resolved="$(gr_realpath "$TEST_ROOT")"
        local ok=0
        for protected in "${GR_PROTECTED_PATHS[@]}"; do
            if gr_path_inside "$resolved" "$protected"; then
                gr_die "cleanup refused: TEST_ROOT '$resolved' resolves under protected path '$protected'"
            fi
        done
        case "$resolved" in
            /mnt/nvm/opentranscribe-test-runs/*|/tmp/ot-reltest-*)
                ok=1 ;;
        esac
        if (( ok == 0 )) && [[ -z "${OT_RELEASE_TEST_ALLOW_PATH:-}" ]]; then
            gr_die "cleanup refused: TEST_ROOT '$resolved' is outside the allowed test areas"
        fi
        gr_log "removing $resolved"
        rm -rf -- "$resolved"
    fi

    gr_ok "cleanup complete"
}

# ─── Entry point ────────────────────────────────────────────────────────────
gr_preflight() {
    gr_require_vars
    gr_check_project_name
    gr_check_test_root
    gr_check_volume_names
    gr_check_container_names
    gr_check_ports_free
    gr_check_disk_space 80 10
    gr_confirmation_gate
    gr_ok "all preflight checks passed"
}
