#!/bin/bash
# Shared "take the live dev stack down, put it back" logic for the stages that cannot run
# beside it (65-rehearse.sh, 85-smoke.sh) and for release.sh's early-exit restore. Sourced,
# never executed. ONE implementation: before this file the rehearse stage owned it and
# smoke had only a refusal, so `release.sh run X --yes` always exited 3 at smoke.
#
# Approval is `--yes` (ASSUME_YES), RELEASE_AUTO_STOP_STACK=true, or an interactive prompt.
# With none of those and no tty the caller must refuse: an unattended run never stops a stack.
#
# Deferral (RELEASE_DEFER_STACK_RESTORE=1, set by `release.sh run` when rehearse AND smoke are
# both selected): a stage that stopped the stack leaves it down and records the fact in
# `.release/<version>/stack-stopped-by-release`. A later stage, or release.sh's exit path,
# restores it. A stack the operator stopped by hand has no marker and is never started here.
#
# Always `./opentr.sh stop`, never a kill: a SIGKILLed CUDA context has wedged this host twice.
# Expects REPO_ROOT. Colors are optional.

LSL_FAIL_REASON=""; LSL_FAIL_FIX=""; export LSL_FAIL_REASON LSL_FAIL_FIX
: "${RED:=}"; : "${GREEN:=}"; : "${YELLOW:=}"; : "${BLUE:=}"; : "${NC:=}"

# The compose project labels (issue #783): a repo clone runs as `transcribe-app`, a one-liner
# install as `opentranscribe`. A git worktree's stack is a third live name (STACK_TARGETING_PLAN
# L8/R1), found by the `project.working_dir` LABEL equal to this checkout, not by guessing a project
# name from a directory. Captured, not piped into `grep -q` (SIGPIPE under pipefail).
lsl_live_stack_names() {
    {
        docker ps --filter "label=com.docker.compose.project=${OPENTR_STOP_PROJECT_LABEL:-opentranscribe}" --format '{{.Names}}'
        docker ps --filter "label=com.docker.compose.project=${OPENTR_STOP_PROJECT_LABEL_ALT:-transcribe-app}" --format '{{.Names}}'
        docker ps --filter "label=com.docker.compose.project.working_dir=${REPO_ROOT}" --format '{{.Names}}'
    } | sort -u
}

# What is about to be stopped: the compose project and the directory it was started from, so the
# restore can refuse to start dev from a different checkout (a stack stopped from one worktree
# and "restored" from another is a different stack under the same name).
LSL_STOPPED_PROJECT=""; LSL_STOPPED_WORKDIR=""
lsl_record_what_is_stopping() {
    local project="" workdir="" filter row
    for filter in "label=com.docker.compose.project=${OPENTR_STOP_PROJECT_LABEL:-opentranscribe}" \
                  "label=com.docker.compose.project=${OPENTR_STOP_PROJECT_LABEL_ALT:-transcribe-app}" \
                  "label=com.docker.compose.project.working_dir=${REPO_ROOT}"; do
        row="$(docker ps --filter "$filter" \
            --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
        row="${row%%$'\n'*}"
        if [[ "$row" == *"|"* ]]; then project="${row%%|*}"; workdir="${row#*|}"; break; fi
    done
    LSL_STOPPED_PROJECT="$project"; LSL_STOPPED_WORKDIR="$workdir"
}

# $1 = what the stack is being stopped for ("the scenarios", "the Hub install smoke").
# Returns 0 when a human approved, 1 otherwise (the caller prints its own refusal).
lsl_approved_to_stop() {
    local names="$1" purpose="${2:-this stage}" auto_stop=false reply
    if [[ "${ASSUME_YES:-false}" == "true" || "${RELEASE_AUTO_STOP_STACK:-false}" == "true" ]]; then
        auto_stop=true
    elif [[ -t 0 ]]; then
        echo -e "${YELLOW}The live stack is running; ${purpose} cannot run beside it.${NC}" >&2
        echo -e "${YELLOW}  $(echo "$names" | wc -l) container(s). Stopping PRESERVES all data,${NC}" >&2
        echo -e "${YELLOW}  and it is restarted when the release run is done with it.${NC}" >&2
        echo -e "${RED}  ⚠ Any transcription in flight will be interrupted.${NC}" >&2
        read -r -p "Stop the stack? [y/N] " reply
        [[ "$reply" == "y" || "$reply" == "Y" ]] && auto_stop=true
    fi
    [[ "$auto_stop" == "true" ]]
}

# Stops the stack and VERIFIES it: `stop` returning 0 is not the same as the ports being free.
# Returns 0 stopped; 3 otherwise, with LSL_FAIL_REASON / LSL_FAIL_FIX set for `record`.
lsl_stop_stack() {
    echo -e "${BLUE}Stopping the live stack (data preserved)...${NC}" >&2
    lsl_record_what_is_stopping
    if ! (cd "$REPO_ROOT" && ./opentr.sh stop >/dev/null 2>&1); then
        LSL_FAIL_REASON="./opentr.sh stop failed"; LSL_FAIL_FIX="stop it by hand, then re-run"
        return 3
    fi
    local still_up
    still_up="$(lsl_live_stack_names)"
    if [[ -n "$still_up" ]]; then
        LSL_FAIL_REASON="containers survived ./opentr.sh stop"; LSL_FAIL_FIX="investigate: $still_up"
        return 3
    fi
    echo -e "${GREEN}live stack stopped${NC}" >&2
    return 0
}

# $1 = working_dir the stack was stopped from ("" = unrecorded, e.g. a marker from an older run).
# Returns 3 with LSL_FAIL_REASON when this checkout is not that directory: starting dev here would
# bring up a DIFFERENT stack and leave the stopped one down.
lsl_start_dev_stack() {
    local recorded_dir="${1:-}"
    if [[ -n "$recorded_dir" && "$recorded_dir" != "$REPO_ROOT" ]]; then
        LSL_FAIL_REASON="the stack was stopped from ${recorded_dir}, this checkout is ${REPO_ROOT}"
        LSL_FAIL_FIX="cd ${recorded_dir} && ./opentr.sh start dev"
        echo -e "${YELLOW}not restarting: ${LSL_FAIL_REASON}${NC}" >&2
        echo -e "${YELLOW}  run: ${LSL_FAIL_FIX}${NC}" >&2
        return 3
    fi
    echo -e "${BLUE}Restarting the dev stack this release run stopped...${NC}" >&2
    if (cd "$REPO_ROOT" && ./opentr.sh start dev >/dev/null 2>&1); then
        echo -e "${GREEN}dev stack restarted${NC}" >&2
        return 0
    fi
    # Never a verdict about the release: a failed local restart is a fact about the HOST.
    echo -e "${YELLOW}could not restart the dev stack — run: ./opentr.sh start dev${NC}" >&2
    return 1
}

lsl_marker() { echo "$REPO_ROOT/.release/${1:?version}/stack-stopped-by-release"; }

# $1 version, $2 "true" when THIS stage stopped the stack. Idempotent; never fails the stage.
# A pending marker from an earlier deferring stage counts as "we stopped it".
lsl_restore_after_stage() {
    local version="$1" stopped_by_us="$2" marker
    marker="$(lsl_marker "$version")"
    if [[ "$stopped_by_us" != "true" && ! -f "$marker" ]]; then
        return 0
    fi
    if [[ "${RELEASE_DEFER_STACK_RESTORE:-0}" == "1" ]]; then
        mkdir -p "$(dirname "$marker")"
        # A later stage that did not itself stop anything must not erase what an earlier one recorded.
        if [[ -n "$LSL_STOPPED_PROJECT" || ! -f "$marker" ]]; then
            printf 'stopped_at=%s\nproject=%s\nworking_dir=%s\n' \
                "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LSL_STOPPED_PROJECT" "$LSL_STOPPED_WORKDIR" > "$marker"
        fi
        echo -e "${BLUE}Leaving the dev stack down for the next stage (marker: ${marker})${NC}" >&2
        return 0
    fi
    local recorded_dir="$LSL_STOPPED_WORKDIR"
    [[ -f "$marker" ]] && recorded_dir="$(sed -n 's/^working_dir=//p' "$marker")"
    # On a refusal the marker stays, so a restore from the right directory still finds it.
    local start_rc=0
    lsl_start_dev_stack "$recorded_dir" || start_rc=$?
    (( start_rc == 3 )) || rm -f "$marker"
    return 0
}

# release.sh's exit path: restore only when a deferring stage left a marker.
lsl_restore_if_marked() {
    local version="$1"
    [[ -f "$(lsl_marker "$version")" ]] || return 0
    RELEASE_DEFER_STACK_RESTORE=0 lsl_restore_after_stage "$version" false
}
