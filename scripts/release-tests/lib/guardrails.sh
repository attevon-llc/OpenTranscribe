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
# An operator declining a confirmation is NOT a gate failure, and the difference is the shared
# exit-code contract scripts/release.sh and scripts/test-matrix.sh both publish: 0 pass, 1 gate
# failed, 2 misuse, 3 precondition unmet, 4 OPERATOR ABORT. The confirmation gate used to
# gr_die (exit 1), so declining the `I UNDERSTAND` prompt was reported all the way up as a
# failed rehearsal — a matrix leg 3 FAIL that nothing had actually tested.
gr_abort() { echo -e "${GR_YELLOW}${GR_BOLD}[guardrails] aborted:${GR_NC} $*" >&2; exit 4; }

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

# ─── EXIT-check registry ────────────────────────────────────────────────────
#
# Several guardrails need to run a check on EVERY exit path (a scenario that
# dies halfway is exactly when a stray write is most likely and least
# expected). `trap` REPLACES rather than stacks, so a second `trap ... EXIT`
# would silently drop the first one's check — this is what let that actually
# happen once. Register a function name instead of building a bigger trap
# string each time; gr_run_exit_checks is the single EXIT handler and runs
# every registered check in registration order.
GR_EXIT_CHECKS=()

gr_run_exit_checks() {
    trap - EXIT
    local check
    for check in "${GR_EXIT_CHECKS[@]}"; do
        "$check"
    done
}

gr_register_exit_check() {
    GR_EXIT_CHECKS+=("$1")
    trap gr_run_exit_checks EXIT
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
        gr_abort "confirmation not given"
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

    # 3b. Remove stock-NAMED resources this run is recorded as owning.
    # These carry production names and so are invisible to the label filters
    # above — the installer's compose project has no idea it is under test.
    # Without this the run leaves its database behind and the NEXT fresh
    # install silently inherits it (issue #408).
    gr_cleanup_owned_stock_resources

    # 3c. Opt-in reset of stale stock-named volumes left by an EARLIER run this
    # one does not own (gr_cleanup_owned_stock_resources deliberately leaves
    # those alone — "leaving $vol alone — it existed before this run" — which is
    # correct in general but is exactly what let leg "3"'s stock volumes survive
    # into "3-lite"/"3-pki": those legs' own --cleanup calls could stop leg "3"'s
    # containers but not remove volumes leg "3" never recorded owning, so the very
    # next fresh-install's preflight found the previous run's Postgres credentials
    # still there and refused. This reuses gr_check_stale_stock_volumes — the same
    # live-marker-verified removal gr_preflight already runs — rather than a raw
    # `docker volume rm`, and only fires when the caller explicitly opts in via
    # OT_RELEASE_TEST_RESET_VOLUMES=1 (test-matrix.sh's inter-leg cleanup does this;
    # a plain `--cleanup` a human runs by hand is unaffected).
    if [[ "${OT_RELEASE_TEST_RESET_VOLUMES:-}" == "1" ]]; then
        gr_check_stale_stock_volumes
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
# ─── Stale stock volumes (issue #408) ───────────────────────────────────────
#
# The fresh-install scenario runs under the installer's stock compose project, so
# its Postgres lands in `opentranscribe_postgres_data` — the same name a real
# deployment uses, and one this file refuses to delete. The volume therefore
# survives --cleanup, and the NEXT run generates fresh credentials into a new
# .env while Postgres still holds the previous run's password. The stack then
# fails five minutes later with an unhealthy backend and an opaque
# "password authentication failed", having silently inherited an old database.
#
# So: detect it up front and say so. Removal is opt-in and re-verified, never
# implicit — on a machine whose live deployment uses named volumes, that same
# name IS production.
gr_check_stale_stock_volumes() {
    local proj="${GR_STOCK_PROJECT:-opentranscribe}"
    local found=()
    local vol
    for vol in "${GR_PROTECTED_VOLUMES[@]}"; do
        if docker volume inspect "${proj}_${vol}" >/dev/null 2>&1; then
            found+=("${proj}_${vol}")
        fi
    done
    [[ ${#found[@]} -eq 0 ]] && { gr_ok "no stale stock volumes"; return 0; }

    if [[ "${OT_RELEASE_TEST_RESET_VOLUMES:-}" != "1" ]]; then
        gr_warn "pre-existing stock volumes found:"
        printf '           %s
' "${found[@]}" >&2
        gr_warn "A fresh-install test against these is NOT a fresh install: the"
        gr_warn "database still holds the previous run's credentials, so the"
        gr_warn "backend will fail to authenticate in phase 04."
        gr_die "Re-run with OT_RELEASE_TEST_RESET_VOLUMES=1 to remove them (each is
           re-checked for the .opentranscribe-live-data marker first), or remove
           them by hand after confirming they are not your live deployment."
    fi

    gr_log "OT_RELEASE_TEST_RESET_VOLUMES=1 — verifying each volume is not live data"
    for vol in "${found[@]}"; do
        # Probed from inside a container: the Mountpoint is root-owned, so a
        # host-side test silently reports "no marker" for every volume.
        if gr_volume_has_live_marker "$vol"; then
            gr_die "$vol carries the .opentranscribe-live-data marker — REFUSING to remove it.
           This is a live deployment's storage, not release-test residue."
        fi
    done
    for vol in "${found[@]}"; do
        if docker volume rm "$vol" >/dev/null 2>&1; then
            gr_ok "removed stale volume $vol"
        else
            gr_die "could not remove $vol (still in use?)"
        fi
    done
}

# ─── Ownership stamp for stock-named resources (issue #408, part 2) ─────────
#
# Detecting a stale volume on the NEXT run is only half the fix; it still leaves
# the residue behind. Cleanup has to remove it — but the volume carries a
# production name, so "remove anything called opentranscribe_postgres_data" is
# precisely the rule that must never exist here.
#
# The invariant that makes this safe: gr_check_stale_stock_volumes has already
# run and either found NO stock volumes or removed them under an explicit
# opt-in. So at the moment preflight completes, every stock volume is absent,
# and any that exists afterwards was created BY THIS RUN. We record that fact
# while it is still true, rather than inferring it later from a name.
#
# The stamp lives outside TEST_ROOT because gr_cleanup deletes TEST_ROOT, and a
# standalone `--cleanup` invocation needs to read it after the fact.
GR_OWNED_STAMP="${GR_OWNED_STAMP:-/mnt/nvm/opentranscribe-test-runs/.owned-stock-resources}"

# Does this volume carry the live-data marker?
#
# It must be read from INSIDE a container. A volume's Mountpoint is under the
# Docker root (here /mnt/nas/docker/volumes/...), which is root-owned 0755, so
# an unprivileged `[[ -e "$mp/.opentranscribe-live-data" ]]` cannot stat it and
# returns false — indistinguishable from "no marker". The self-test caught this
# doing precisely the wrong thing: it deleted a volume that WAS marked live.
#
# Returns 0 = marker present (or undetermined). FAILS CLOSED: if docker cannot
# tell us, we claim the marker is there, because the cost of a false positive
# is a volume left behind and the cost of a false negative is data loss.
gr_volume_has_live_marker() {
    local vol="$1" rc
    docker run --rm -v "$vol:/probe:ro" alpine \
        test -e /probe/.opentranscribe-live-data >/dev/null 2>&1
    rc=$?
    case "$rc" in
        0) return 0 ;;   # marker present
        1) return 1 ;;   # ran cleanly, no marker
        *)               # could not run the probe at all
           gr_warn "could not probe $vol for the live-data marker (docker rc=$rc) — assuming it IS live"
           return 0 ;;
    esac
}

gr_stamp_owned_resources() {
    local proj="${GR_STOCK_PROJECT:-opentranscribe}"
    local dir
    dir="$(dirname "$GR_OWNED_STAMP")"
    mkdir -p "$dir" 2>/dev/null || { gr_warn "cannot write ownership stamp in $dir"; return 0; }

    # Record the stock project's volumes that ALREADY EXIST, rather than a list
    # of names the test expects to create.
    #
    # A hand-maintained name list is the failure mode this whole harness exists
    # to avoid: GR_PROTECTED_VOLUMES covers the five data volumes, so
    # `pipeline_scratch` and `transcription-temp` were created by every run and
    # cleaned up by none — dangling residue that grows per release. Any volume
    # the stack gains in future would join them silently.
    #
    # Inverting it makes the rule self-maintaining: cleanup removes any
    # `${proj}_*` volume that was NOT here before the run. New volumes are
    # covered automatically; anything pre-existing is somebody else's and is
    # never touched.
    {
        echo "# Written by gr_stamp_owned_resources at preflight."
        echo "# 'preexisting' volumes belong to whatever was here BEFORE this run"
        echo "# and must never be removed. Any other ${proj}_* volume found at"
        echo "# cleanup time was created by this test and may be removed."
        echo "# Delete this file and cleanup will not touch stock-named resources."
        echo "scenario=${TEST_SCENARIO:-unknown}"
        echo "test_root=${TEST_ROOT:-unknown}"
        echo "project=${proj}"
        local vol
        while IFS= read -r vol; do
            [[ -n "$vol" ]] && echo "preexisting=$vol"
        done < <(docker volume ls -q 2>/dev/null | grep "^${proj}_" || true)
        echo "network=${proj}_default"
    } > "$GR_OWNED_STAMP"

    gr_ok "recorded test ownership of stock-named resources"
}

# ─── The repo's own .env is never a test artifact ───────────────────────────
#
# Every .env the harness writes lives under TEST_ROOT (the staged install dirs)
# or is the gitignored .env.test-secrets. The repo's own .env — which carries
# the live deployment's credentials and NAS paths — must come out of a release
# test byte-identical.
#
# That is an easy claim to make and an easy one to be wrong about, so it is
# measured rather than asserted: fingerprint before, verify after, fail loudly
# on any difference.
GR_REPO_ENV="${GR_REPO_ENV:-/mnt/nvm/repos/transcribe-app/.env}"
GR_REPO_ENV_FINGERPRINT=""

gr_fingerprint_repo_env() {
    if [[ -f "$GR_REPO_ENV" ]]; then
        GR_REPO_ENV_FINGERPRINT="$(sha256sum "$GR_REPO_ENV" | awk '{print $1}')"
        gr_ok "fingerprinted repo .env (must be unchanged at exit)"
    else
        GR_REPO_ENV_FINGERPRINT="absent"
        gr_log "no repo .env to fingerprint"
    fi
    gr_register_exit_check gr_assert_repo_env_untouched
}

gr_assert_repo_env_untouched() {
    [[ -n "$GR_REPO_ENV_FINGERPRINT" ]] || return 0

    local now
    if [[ -f "$GR_REPO_ENV" ]]; then
        now="$(sha256sum "$GR_REPO_ENV" | awk '{print $1}')"
    else
        now="absent"
    fi

    if [[ "$now" != "$GR_REPO_ENV_FINGERPRINT" ]]; then
        gr_die "the repo's .env CHANGED during this release test.
           before: $GR_REPO_ENV_FINGERPRINT
           after:  $now
           A release test must never write to the live deployment's .env.
           Restore it from git or your backup before starting the stack."
    fi
    gr_ok "repo .env unchanged"
}

# Remove ONLY resources this run is recorded as owning. Three independent
# conditions must all hold for each removal, because the name alone proves
# nothing:
#   1. the ownership stamp lists it (verified absent at preflight),
#   2. it carries no .opentranscribe-live-data marker,
#   3. no container is currently using it.
gr_cleanup_owned_stock_resources() {
    if [[ ! -f "$GR_OWNED_STAMP" ]]; then
        gr_log "no ownership stamp — leaving stock-named resources untouched"
        return 0
    fi

    local vol net users proj="" line
    local preexisting=()
    while IFS= read -r line; do
        case "$line" in
            project=*)     proj="${line#project=}" ;;
            preexisting=*) preexisting+=("${line#preexisting=}") ;;
        esac
    done < "$GR_OWNED_STAMP"

    # Everything under the stock project that was not here before the run.
    if [[ -n "$proj" ]]; then
        local candidates=()
        while IFS= read -r vol; do
            [[ -n "$vol" ]] || continue
            local is_pre=0 p
            for p in ${preexisting[@]+"${preexisting[@]}"}; do
                [[ "$p" == "$vol" ]] && { is_pre=1; break; }
            done
            if (( is_pre )); then
                gr_log "leaving $vol alone — it existed before this run"
            else
                candidates+=("$vol")
            fi
        done < <(docker volume ls -q 2>/dev/null | grep "^${proj}_" || true)

        for vol in ${candidates[@]+"${candidates[@]}"}; do
            if gr_volume_has_live_marker "$vol"; then
                gr_warn "refusing to remove $vol — carries the live-data marker"
                continue
            fi
            users=$(docker ps -a --filter "volume=$vol" --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')
            if [[ -n "${users// /}" ]]; then
                gr_warn "refusing to remove $vol — still used by: ${users% }"
                continue
            fi
            if docker volume rm "$vol" >/dev/null 2>&1; then
                gr_ok "removed test-owned volume $vol"
            fi
        done
    fi

    while IFS= read -r line; do
        case "$line" in
            volume=*)
                vol="${line#volume=}"
                docker volume inspect "$vol" >/dev/null 2>&1 || continue

                if gr_volume_has_live_marker "$vol"; then
                    gr_warn "refusing to remove $vol — carries the live-data marker"
                    continue
                fi

                users=$(docker ps -a --filter "volume=$vol" --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')
                if [[ -n "${users// /}" ]]; then
                    gr_warn "refusing to remove $vol — still used by: ${users% }"
                    continue
                fi

                if docker volume rm "$vol" >/dev/null 2>&1; then
                    gr_ok "removed test-owned volume $vol"
                fi
                ;;
            network=*)
                net="${line#network=}"
                docker network inspect "$net" >/dev/null 2>&1 || continue
                users=$(docker network inspect "$net" --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null || echo "")
                if [[ -n "${users// /}" ]]; then
                    gr_warn "refusing to remove network $net — still has: ${users% }"
                    continue
                fi
                if docker network rm "$net" >/dev/null 2>&1; then
                    gr_ok "removed test-owned network $net"
                fi
                ;;
        esac
    done < "$GR_OWNED_STAMP"

    rm -f "$GR_OWNED_STAMP"
}

gr_preflight() {
    gr_require_vars
    gr_check_project_name
    gr_check_test_root
    gr_check_volume_names
    gr_check_container_names
    gr_check_stale_stock_volumes
    gr_stamp_owned_resources
    gr_fingerprint_repo_env
    gr_check_ports_free
    gr_check_disk_space 80 10
    gr_confirmation_gate
    gr_ok "all preflight checks passed"
}

# ─── Rollback-tail guardrails (issue #598) ──────────────────────────────────
#
# test-upgrade.sh's rollback phases run `DROP DATABASE`, which none of the
# checks above needed to guard against — the fresh-install and forward-upgrade
# scenarios never destroy a database, only create or migrate one. These three
# are called explicitly by the rollback phases, not from gr_preflight, because
# they are not needed by test-fresh-install.sh or test-lite-mode.sh.

# gr_assert_target_is_test_database CONTAINER EXPECTED_DB ENV_FILE
#   Called immediately before every destructive DB operation in the rollback
#   tail (the DROP DATABASE inside `restore_database`, invoked here through
#   `opentranscribe.sh restore` — the shipped production command, issue #613 —
#   and the swap `opentranscribe.sh update --rollback` performs). All four
#   conditions below
#   must hold or it dies — any inability to determine an answer counts as
#   "this is live" (fail closed, same policy as gr_volume_has_live_marker).
gr_assert_target_is_test_database() {
    local container="$1" expected_db="$2" env_file="$3"

    # (a) the container must carry our release-test label — the same label
    # cp_inject_labels stamps on every service this run creates.
    local label
    label="$(docker inspect "$container" \
        --format '{{index .Config.Labels "com.opentranscribe.release-test"}}' 2>/dev/null || echo "")"
    if [[ -z "$label" ]]; then
        gr_die "gr_assert_target_is_test_database: container '$container' carries no
           com.opentranscribe.release-test label — refusing a destructive DB
           operation against a container this run cannot prove it owns"
    fi

    # (b) the volume backing postgres must not be one this run found
    # PRE-EXISTING at preflight (gr_stamp_owned_resources' 'preexisting=' list)
    # — that is someone else's database, not this run's.
    local vol
    vol="$(docker inspect "$container" \
        --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' \
        2>/dev/null || echo "")"
    if [[ -z "$vol" ]]; then
        gr_die "gr_assert_target_is_test_database: could not resolve the postgres data
           volume for container '$container' — refusing (fail closed)"
    fi
    if [[ -f "$GR_OWNED_STAMP" ]] && grep -qxF "preexisting=$vol" "$GR_OWNED_STAMP"; then
        gr_die "gr_assert_target_is_test_database: volume '$vol' existed BEFORE this run
           started — refusing to drop a database this run does not own"
    fi

    # (c) no live-data marker, probed from inside a container per
    # gr_volume_has_live_marker's own doc comment (a host-side stat on the
    # root-owned mountpoint would silently report "no marker" for every
    # volume, which is the exact mistake that once deleted a live one).
    if gr_volume_has_live_marker "$vol"; then
        gr_die "gr_assert_target_is_test_database: volume '$vol' carries the
           .opentranscribe-live-data marker — REFUSING a destructive operation"
    fi

    # (d) the resolved POSTGRES_DB must match the staged .env this run wrote
    # — catches a stage pointed at the wrong directory before it drops the
    # wrong database.
    local configured_db
    # python-dotenv, not grep/cut (issue #590).
    configured_db="$(python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/env_reader.py" "$env_file" POSTGRES_DB)"
    configured_db="${configured_db:-opentranscribe}"
    if [[ "$configured_db" != "$expected_db" ]]; then
        gr_die "gr_assert_target_is_test_database: staged .env at '$env_file' has
           POSTGRES_DB='$configured_db', expected '$expected_db'"
    fi

    gr_ok "target database '$expected_db' in container '$container' verified as this run's own test database"
}

# ─── The repo's own ./backups/ is never a test artifact ─────────────────────
#
# `backup`/`restore` (scripts/common.sh's shared implementation, invoked here through
# the staged `opentranscribe.sh` — issue #613; `opentr.sh` uses the identical
# relative-CWD write but a bare `docker compose` with no `-f` chain of its own)
# write ./backups relative to CWD. A staging mistake that ran the staged
# opentranscribe.sh from the repo root — or forgot to stage it at all — would write
# this run's dumps (containing every seeded user's transcripts in plaintext)
# into the developer's own checkout. Same measured-not-asserted pattern as
# gr_fingerprint_repo_env: fingerprint before, verify after, fail loudly on any
# difference, checked on every exit path via gr_register_exit_check (which
# ADDS this check to whatever gr_fingerprint_repo_env already registered,
# rather than a second `trap ... EXIT` clobbering it).
GR_REPO_BACKUPS_DIR="${GR_REPO_BACKUPS_DIR:-/mnt/nvm/repos/transcribe-app/backups}"
GR_REPO_BACKUPS_FINGERPRINT=""

gr_fingerprint_repo_backups() {
    if [[ -d "$GR_REPO_BACKUPS_DIR" ]]; then
        GR_REPO_BACKUPS_FINGERPRINT="$(find "$GR_REPO_BACKUPS_DIR" -type f -printf '%P %s\n' 2>/dev/null \
            | LC_ALL=C sort | sha256sum | awk '{print $1}')"
        gr_ok "fingerprinted repo ./backups (must be unchanged at exit)"
    else
        GR_REPO_BACKUPS_FINGERPRINT="absent"
        gr_log "no repo ./backups directory to fingerprint — will refuse if one appears"
    fi

    gr_register_exit_check gr_assert_repo_backups_untouched
}

gr_assert_repo_backups_untouched() {
    [[ -n "$GR_REPO_BACKUPS_FINGERPRINT" ]] || return 0

    local now
    if [[ -d "$GR_REPO_BACKUPS_DIR" ]]; then
        now="$(find "$GR_REPO_BACKUPS_DIR" -type f -printf '%P %s\n' 2>/dev/null \
            | LC_ALL=C sort | sha256sum | awk '{print $1}')"
    else
        now="absent"
    fi

    if [[ "$now" != "$GR_REPO_BACKUPS_FINGERPRINT" ]]; then
        gr_die "the repo's ./backups directory CHANGED during this release test.
           before: $GR_REPO_BACKUPS_FINGERPRINT
           after:  $now
           A release test must stage 'opentranscribe.sh backup'/'restore' under TEST_ROOT
           and never invoke them from the repo root. See gr_assert_not_repo_cwd."
    fi
    gr_ok "repo ./backups directory unchanged"
}

# gr_assert_not_repo_cwd [DIR]
#   Refuses to proceed if DIR (default: $PWD) resolves to the repo root, any
#   other GR_PROTECTED_PATHS entry, or anywhere outside TEST_ROOT. Call this
#   immediately before invoking a staged copy of opentranscribe.sh (or opentr.sh):
#   both write ./backups relative to CWD, so running either from the wrong
#   directory would drop the database of — or write dumps into — the live
#   deployment's own tree.
gr_assert_not_repo_cwd() {
    local dir="${1:-$PWD}"
    local resolved
    resolved="$(gr_realpath "$dir")"
    local protected
    for protected in "${GR_PROTECTED_PATHS[@]}"; do
        if gr_path_inside "$resolved" "$protected"; then
            gr_die "gr_assert_not_repo_cwd: refusing to run a staged opentr.sh from
               '$resolved' — resolves under protected path '$protected'"
        fi
    done
    if ! gr_path_inside "$resolved" "${TEST_ROOT:-/nonexistent-test-root}"; then
        gr_die "gr_assert_not_repo_cwd: refusing to run a staged opentr.sh from
           '$resolved' — it must be inside TEST_ROOT ('${TEST_ROOT:-<unset>}')"
    fi
    gr_ok "staged opentr.sh CWD '$resolved' is inside TEST_ROOT, not the repo"
}
