#!/bin/bash
# OpenTranscribe — sweep what test runs left behind, once or on a timer (issue #1195).
#
# One entry point an operator can leave running: it resolves ONE stack from its compose
# project label, then runs scripts/cleanup-test-data.py's Tier A sweep against exactly that
# stack — registered-shape test media, collections, tags, watch sources, speaker profiles,
# chat conversations and users, plus empty leaked test indexes, test buckets/objects and test
# /tmp trees. Tier B (anything a person might want) is never touched from here; run
# cleanup-test-data.py --execute by hand for that, after reading its dry run.
#
# Usage:
#   scripts/cleanup-test-leftovers.sh --project <compose-project> [mode] [options]
#   scripts/cleanup-test-leftovers.sh --fresh <name>              [mode] [options]
#   ./opentr.sh test-cleanup ...                                   (same flags)
#
# Modes (one):
#   --once                    one pass now (default)
#   --background              start a background loop (one pass every --interval-minutes)
#   --status                  is the loop running, and the last pass's summary
#   --stop                    stop the loop: TERM to the pid in its pid file, ONLY if that pid
#                             is this script's own loop for this project
# Options:
#   --dry-run                 report what a pass would remove; delete nothing
#   --interval-minutes N      background pass interval (default 60)
#   --min-age-minutes N       never touch anything younger than N minutes (default 30), so a
#                             test run happening right now keeps its in-flight data
#
# Safety:
#   * No stack is guessed. Without --project/--fresh it refuses and lists the candidates.
#     Every port, credential and bucket name is read from that project's own containers
#     (selected by the com.docker.compose.project label), so a pass cannot read one stack's
#     database and another stack's indexes.
#   * A lock file makes passes mutually exclusive (a --once during a background pass waits
#     for nothing: it refuses). A pid file stops a second background loop for one project.
#   * --stop never uses pkill/killall/kill -9: it checks /proc/<pid>/cmdline names this script
#     and this project, then sends a plain TERM to that one pid.
#
# Files (OT_TEST_CLEANUP_DIR overrides the directory, default <repo>/.test-cleanup/):
#   <project>.log  one summary paragraph per pass     <project>.pid   the background loop
#   <project>.lock pass mutual exclusion
#
# Exit codes: 0 ok · 1 a pass failed · 2 misuse / no resolvable stack · 3 another pass or
# loop is already running.

set -uo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${OT_TEST_CLEANUP_DIR:-$REPO_ROOT/.test-cleanup}"

PROJECT=""
MODE="once"
DRY_RUN=false
INTERVAL_MINUTES=60
MIN_AGE_MINUTES=30

usage() {
    sed -n '2,/^set -uo pipefail/p' "$SCRIPT_PATH" | sed -e '/^set -uo/d' -e 's/^# \{0,1\}//'
}

die() {
    echo "cleanup-test-leftovers: $1" >&2
    exit "${2:-2}"
}

is_count() {
    case "${1:-}" in
        '' | *[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --project)
            [ $# -ge 2 ] || die "--project needs a compose project name"
            PROJECT="$2"
            shift 2
            ;;
        --fresh)
            [ $# -ge 2 ] || die "--fresh needs a deployment name"
            PROJECT="otfresh-$2"
            shift 2
            ;;
        --once) MODE="once"; shift ;;
        --background) MODE="background"; shift ;;
        --loop) MODE="loop"; shift ;;
        --status) MODE="status"; shift ;;
        --stop) MODE="stop"; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --interval-minutes)
            [ $# -ge 2 ] && is_count "$2" && [ "$2" -gt 0 ] || die "--interval-minutes needs N >= 1"
            INTERVAL_MINUTES="$2"
            shift 2
            ;;
        --min-age-minutes)
            [ $# -ge 2 ] && is_count "$2" || die "--min-age-minutes needs N >= 0"
            MIN_AGE_MINUTES="$2"
            shift 2
            ;;
        -h | --help) usage; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done

# --------------------------------------------------------------------------- stack resolution

# Compose projects that have a running postgres service: the candidates a refusal lists.
candidate_projects() {
    docker ps --filter "label=com.docker.compose.service=postgres" --filter "status=running" \
        --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | sort -u
}

if [ -z "$PROJECT" ]; then
    {
        echo "cleanup-test-leftovers: refusing to guess which stack to sweep."
        echo "  Pass --project <compose-project> or --fresh <name>. Running stacks:"
        candidate_projects | sed 's/^/    /'
    } >&2
    exit 2
fi
case "$PROJECT" in
    *[!A-Za-z0-9_.-]*) die "invalid project name: $PROJECT" ;;
esac

SAFE_PROJECT="$PROJECT"
PID_FILE="$STATE_DIR/$SAFE_PROJECT.pid"
LOCK_FILE="$STATE_DIR/$SAFE_PROJECT.lock"
LOG_FILE="$STATE_DIR/$SAFE_PROJECT.log"

# The running container for one service of THIS project, selected by labels only.
service_container() {
    docker ps --filter "label=com.docker.compose.project=$PROJECT" \
        --filter "label=com.docker.compose.service=$1" --filter "status=running" \
        --format '{{.Names}}' 2>/dev/null | sed -n '1p'
}

# The host port a container publishes for <container-port>/tcp.
published_port() {
    local mapping
    mapping="$(docker port "$1" "$2/tcp" 2>/dev/null | sed -n '1p')"
    [ -n "$mapping" ] || return 1
    printf '%s\n' "${mapping##*:}"
}

container_env() {
    docker exec "$1" printenv "$2" 2>/dev/null
}

resolve_python() {
    local candidate common
    candidate="${OT_VENV_PY:-$REPO_ROOT/backend/venv/bin/python}"
    if [ -x "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
    fi
    # A git worktree has no venv of its own; use the main checkout's.
    common="$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
    if [ -n "$common" ] && [ -x "$(dirname "$common")/backend/venv/bin/python" ]; then
        printf '%s\n' "$(dirname "$common")/backend/venv/bin/python"
        return 0
    fi
    return 1
}

# Exports everything cleanup-test-data.py needs for THIS project and sets SWEEP_PLANES.
# Returns non-zero (with a message) when the stack cannot be resolved.
resolve_stack() {
    local pg backend os minio port
    pg="$(service_container postgres)"
    backend="$(service_container backend)"
    if [ -z "$pg" ] || [ -z "$backend" ]; then
        echo "cleanup-test-leftovers: compose project '$PROJECT' has no running postgres" \
            "and backend; refusing (running stacks: $(candidate_projects | tr '\n' ' '))" >&2
        return 1
    fi
    port="$(published_port "$pg" 5432)" || {
        echo "cleanup-test-leftovers: $pg publishes no postgres port" >&2
        return 1
    }
    export POSTGRES_HOST=localhost POSTGRES_PORT="$port"
    POSTGRES_USER="$(container_env "$pg" POSTGRES_USER)"
    POSTGRES_PASSWORD="$(container_env "$pg" POSTGRES_PASSWORD)"
    POSTGRES_DB="$(container_env "$pg" POSTGRES_DB)"
    export POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB
    port="$(published_port "$backend" 8080)" || {
        echo "cleanup-test-leftovers: $backend publishes no API port" >&2
        return 1
    }
    export E2E_BACKEND_URL="http://localhost:$port"
    MEDIA_BUCKET_NAME="$(container_env "$backend" MEDIA_BUCKET_NAME)"
    CACHE_BUCKET_NAME="$(container_env "$backend" CACHE_BUCKET_NAME)"
    export MEDIA_BUCKET_NAME CACHE_BUCKET_NAME

    SWEEP_PLANES="records,databases,tmp"
    os="$(service_container opensearch)"
    if [ -n "$os" ] && port="$(published_port "$os" 9200)"; then
        export OPENSEARCH_HOST=localhost OPENSEARCH_PORT="$port"
        SWEEP_PLANES="$SWEEP_PLANES,indexes"
    else
        unset OPENSEARCH_PORT
    fi
    minio="$(service_container minio)"
    if [ -n "$minio" ] && port="$(published_port "$minio" 9000)"; then
        export MINIO_HOST=localhost MINIO_PORT="$port"
        MINIO_ROOT_USER="$(container_env "$minio" MINIO_ROOT_USER)"
        MINIO_ROOT_PASSWORD="$(container_env "$minio" MINIO_ROOT_PASSWORD)"
        export MINIO_ROOT_USER MINIO_ROOT_PASSWORD
        SWEEP_PLANES="$SWEEP_PLANES,buckets"
    else
        unset MINIO_PORT
    fi
    return 0
}

# ------------------------------------------------------------------------------------ a pass

log() {
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$1" >>"$LOG_FILE"
}

# Prints the one-paragraph summary of a cleanup-test-data.py --json run read on stdin.
summarize() {
    "$1" -c '
import json, sys
text = sys.stdin.read()
marker = "--- JSON report ---"
if marker not in text:
    print("no report (the sweep did not finish; see the log)")
    raise SystemExit(0)
report = json.loads(text.split(marker, 1)[1])
parts = []
for name, entry in sorted(report.get("resources", {}).items()):
    if not isinstance(entry, dict):
        continue
    found = entry.get("count")
    if found is None:
        found = len(entry.get("tier_a", []))
    deleted = entry.get("deleted", 0)
    if found or deleted:
        parts.append(f"{name} {found}" + (f" (deleted {deleted})" if deleted else ""))
print(", ".join(parts) if parts else "nothing to remove")
'
}

run_pass() {
    local py output rc summary verb started pass_lock_fd
    started="$(date '+%Y-%m-%d %H:%M:%S')"
    mkdir -p "$STATE_DIR"
    exec {pass_lock_fd}>>"$LOCK_FILE"
    if ! flock -n "$pass_lock_fd"; then
        exec {pass_lock_fd}>&-
        echo "cleanup-test-leftovers: another pass for $PROJECT holds $LOCK_FILE; not overlapping" >&2
        return 3
    fi
    if ! resolve_stack; then
        exec {pass_lock_fd}>&-
        return 2
    fi
    if ! py="$(resolve_python)"; then
        exec {pass_lock_fd}>&-
        echo "cleanup-test-leftovers: no backend venv python (set OT_VENV_PY)" >&2
        return 2
    fi
    if $DRY_RUN; then
        verb="dry run, Tier A found"
        output="$("$py" "$REPO_ROOT/scripts/cleanup-test-data.py" --json \
            --planes "$SWEEP_PLANES" --min-age-minutes "$MIN_AGE_MINUTES" 2>&1)"
    else
        verb="Tier A found"
        output="$("$py" "$REPO_ROOT/scripts/cleanup-test-data.py" --execute-unambiguous --json \
            --planes "$SWEEP_PLANES" --min-age-minutes "$MIN_AGE_MINUTES" 2>&1)"
    fi
    rc=$?
    exec {pass_lock_fd}>&-
    summary="$(printf '%s\n' "$output" | summarize "$py")"
    log "[$started] $PROJECT (planes $SWEEP_PLANES, age floor ${MIN_AGE_MINUTES}m, exit $rc): $verb: $summary"
    printf '%s\n' "$output" | sed -n '/--- JSON report ---/q;p' >>"$LOG_FILE"
    echo "[$started] $PROJECT: $verb: $summary (exit $rc; details in $LOG_FILE)"
    [ "$rc" -eq 0 ] || return 1
    return 0
}

# ----------------------------------------------------------------------------- loop control

pid_alive() {
    [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

# True only when <pid> is THIS script's loop for THIS project.
pid_is_our_loop() {
    local cmdline
    [ -r "/proc/$1/cmdline" ] || return 1
    cmdline="$(tr '\0' ' ' <"/proc/$1/cmdline")"
    case "$cmdline" in
        *cleanup-test-leftovers.sh*--loop*) ;;
        *) return 1 ;;
    esac
    case " $cmdline " in
        *" --project $PROJECT "*) return 0 ;;
        *) return 1 ;;
    esac
}

read_pid() {
    [ -f "$PID_FILE" ] && sed -n '1p' "$PID_FILE"
}

loop() {
    local stopping=false sleeper=""
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$$" >"$PID_FILE"
    trap 'stopping=true; if [ -n "$sleeper" ]; then kill -TERM "$sleeper" 2>/dev/null; fi' TERM INT
    trap 'if [ "$(read_pid)" = "$$" ]; then rm -f "$PID_FILE"; fi' EXIT
    log "[$(date '+%Y-%m-%d %H:%M:%S')] loop started (pid $$, every ${INTERVAL_MINUTES}m)"
    while ! $stopping; do
        run_pass || true
        $stopping && break
        sleep "$((INTERVAL_MINUTES * 60))" &
        sleeper=$!
        wait "$sleeper" 2>/dev/null || true
        sleeper=""
    done
    log "[$(date '+%Y-%m-%d %H:%M:%S')] loop stopped (pid $$)"
}

case "$MODE" in
    once)
        run_pass
        exit $?
        ;;
    loop)
        loop
        exit 0
        ;;
    background)
        existing="$(read_pid || true)"
        if pid_alive "$existing" && pid_is_our_loop "$existing"; then
            die "a loop for $PROJECT is already running (pid $existing)" 3
        fi
        # Fail fast in the foreground rather than leaving a loop that can never pass.
        resolve_stack || exit 2
        mkdir -p "$STATE_DIR"
        loop_args="--project $PROJECT --loop --interval-minutes $INTERVAL_MINUTES --min-age-minutes $MIN_AGE_MINUTES"
        if $DRY_RUN; then
            loop_args="$loop_args --dry-run"
        fi
        # shellcheck disable=SC2086 # loop_args is a fixed, validated word list
        nohup "$SCRIPT_PATH" $loop_args >>"$LOG_FILE" 2>&1 </dev/null &
        started_pid=$!
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            [ "$(read_pid || true)" = "$started_pid" ] && break
            sleep 0.2
        done
        echo "cleanup-test-leftovers: loop for $PROJECT started (pid $started_pid, every" \
            "${INTERVAL_MINUTES}m, age floor ${MIN_AGE_MINUTES}m); log: $LOG_FILE"
        exit 0
        ;;
    status)
        existing="$(read_pid || true)"
        if pid_alive "$existing" && pid_is_our_loop "$existing"; then
            echo "running: pid $existing ($PID_FILE)"
        else
            echo "not running"
        fi
        if [ -f "$LOG_FILE" ]; then
            echo "last pass:"
            grep '^\[' "$LOG_FILE" | grep -v 'loop st' | tail -n 1 | sed 's/^/  /'
        fi
        exit 0
        ;;
    stop)
        existing="$(read_pid || true)"
        if [ -z "$existing" ]; then
            echo "not running (no $PID_FILE)"
            exit 0
        fi
        if ! pid_alive "$existing"; then
            rm -f "$PID_FILE"
            echo "not running (removed stale pid file for $existing)"
            exit 0
        fi
        if ! pid_is_our_loop "$existing"; then
            die "pid $existing in $PID_FILE is not this project's cleanup loop; refusing to signal it" 1
        fi
        kill -TERM "$existing"
        for _ in $(seq 1 150); do
            pid_alive "$existing" || break
            sleep 0.2
        done
        if pid_alive "$existing"; then
            # TERM is honoured between passes; a pass in progress is allowed to finish.
            echo "sent TERM to loop pid $existing; it stops when its current pass finishes"
            exit 0
        fi
        echo "stopped loop pid $existing"
        exit 0
        ;;
esac
