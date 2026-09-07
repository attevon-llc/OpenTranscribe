#!/bin/bash
#
# scripts/lib/compose-project.sh — resolve the LIVE dev stack's compose project, and any
# container in it, by LABEL rather than by guessing.
#
# Sourced, not a standalone entry point. Requires only $REPO_ROOT (falls back to the repo
# root derived from this file's own location). Installs no trap and needs none of the
# caller globals scripts/lib/dev-test-overlays.sh expects, so any dev script can source it.
#
# WHY THIS EXISTS
#
# Two guesses about the compose project have both shipped bugs in this repo:
#
#   1. `basename "$REPO_ROOT"` — wrong from a git worktree (.claude/worktrees/<name>), where
#      it yields the WORKTREE's directory name, never the live stack's project. Every lookup
#      then silently finds nothing.
#   2. A hardcoded `container_name` — e.g. `docker ps --filter name=^opentranscribe-mock-llm$`,
#      when the compose file actually declares `${MOCK_LLM_CONTAINER_NAME:-opentranscribe-mock-llm}`
#      and `--fresh` re-pins it to `otfresh-<name>-*` (issue #347). Keycloak declares no
#      container_name at all, so a name filter cannot find it under any spelling.
#
# Filtering on the compose PROJECT + SERVICE labels is correct in every one of those cases.
# dev-test-overlays.sh worked this out first (issue #630); this file is that logic extracted so
# it has ONE implementation instead of one per dev script — the same reason
# release-manifest.txt exists on the deployment side.
#
# ⚠️ A COROLLARY worth stating, because it has already caused a real failure: never bring an
# aux overlay up with a bare `docker compose -f docker-compose.<overlay>.yml up -d`. Compose
# derives the project from the CURRENT DIRECTORY's name, so unless that happens to equal the
# live stack's project, the container lands in a brand-new project on a brand-new network and
# is unreachable from the backend. Measured with a throwaway project: an aux service brought up
# that way joined `base_default` instead of `<project>_default` and could not resolve a sibling
# container by name at all. Both aux overlays say so in their own headers — "`./opentr.sh` is
# the only supported entry point". Use `./opentr.sh start dev --with-<overlay>`, which composes
# the whole chain and therefore the right project.

# The project names this repo's LIVE stack can legitimately run under, in preference order.
#   `opentranscribe`  — every service with an explicit container_name
#   `transcribe-app`  — the checkout directory's basename, which compose uses for services that
#                       declare none (docker-compose.diar-native.yml's, notably)
# These are the same two literals `opentr.sh`'s straggler-cleanup loop defaults to
# (OPENTR_STOP_PROJECT_LABEL / _ALT), for the same reason: they are what "this project" means.
# Overridable so a rename does not need an edit here.
OPENTR_LIVE_PROJECTS=(
    "${OPENTR_STOP_PROJECT_LABEL:-opentranscribe}"
    "${OPENTR_STOP_PROJECT_LABEL_ALT:-transcribe-app}"
)

# compose_project_name
#   The live stack's compose project. $COMPOSE_PROJECT_NAME wins; otherwise detected from a
#   running postgres container's label; otherwise the old directory-name guess, which is only
#   reached when no stack is up (in which case every lookup below would find nothing anyway).
#
# ⚠️ The detection is an ALLOWLIST, not "the first postgres docker lists", and both halves of
# that were learned from real failures on 2026-09-07:
#
#   1. It used to take `head -1` of every running postgres. With six `--fresh` stacks up for a
#      parallel mutation run, SEVEN postgres containers matched and docker's ordering decided
#      the answer — it returned `otfresh-mut-session`, so `diar-native-smoke.sh` hunted for the
#      sidecar in a mutation stack, did not find one, and failed the gate's diar-native phase.
#      A live stack that was working perfectly was reported as broken.
#   2. Excluding `otfresh-*` was NOT enough. This host also runs unrelated compose projects, and
#      with the fresh stacks gone the very next answer was `dsva` — a different application
#      entirely, which happens to have a postgres. Any exclusion list is a guess about what else
#      exists on the machine; only an allowlist of what THIS repo runs under is sound.
#
# The blast radius is every caller, not just that one check: `overlay_container_name` below, and
# through it `dev-test-overlays.sh`'s overlay orchestration. Same class as the unscoped
# `docker ps` in `opentr.sh stop` that destroyed an unrelated container (issue #693) — a filter
# loose enough to match a neighbour's containers.
#
# Falling through to the directory-name guess when no LIVE project is running is correct: it
# means the stack is down, and every lookup should then find nothing rather than something
# belonging to somebody else's deployment.
compose_project_name() {
    if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
        echo "$COMPOSE_PROJECT_NAME"
        return
    fi

    local running=() line root candidate
    while IFS= read -r line; do
        [[ -n "$line" ]] && running+=("$line")
    done < <(docker ps \
        --filter "label=com.docker.compose.service=postgres" \
        --filter "status=running" \
        --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null)

    # Preference order is OPENTR_LIVE_PROJECTS', not docker's listing order — otherwise the
    # answer still depends on which container happened to start first.
    for candidate in "${OPENTR_LIVE_PROJECTS[@]}"; do
        for line in ${running[@]+"${running[@]}"}; do
            if [[ "$line" == "$candidate" ]]; then
                echo "$candidate"
                return
            fi
        done
    done

    root="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
    basename "$root"
}

# overlay_container_name SERVICE
#   The running container for a compose SERVICE in the live project, or "" if none.
#   Works regardless of container_name — including services that declare none.
#   ⚠️ Deliberately NOT `docker ps ... | head -1`. A pipeline is this function's last command,
#   so the PIPELINE's status becomes the FUNCTION's return status — and `head -1` exits after
#   the first line, which under `set -o pipefail` hands the caller SIGPIPE's 141 whenever the
#   filters match more than one container (a scaled service: `celery-worker-gpu-scaled` under
#   --gpu-scale is exactly that shape). Almost every caller writes
#   `X="$(overlay_container_name svc)"`, and an assignment from a 141 aborts the calling script
#   under `set -e` with no error message — measured: `bash -c 'set -euo pipefail;
#   g(){ seq 1 200000 | head -1; }; x="$(g)"; echo REACHED'` exits 141 and never prints.
#   Capturing into a variable and trimming in-shell leaves no pipe to break, and returns 0
#   for "none" exactly as the value contract above says.
#   ⚠️ `rc` is captured and re-returned EXPLICITLY, and that is not defensive noise. Every
#   caller writes `X="$(overlay_container_name svc)"`, and bash's `inherit_errexit` is OFF by
#   default — so `set -e` does NOT apply inside a command substitution's subshell, and the only
#   thing that reaches the caller is the subshell's FINAL exit status. The old pipeline
#   propagated a genuine `docker ps` failure for free, because the pipeline WAS the function's
#   last command. Rewriting it as capture-then-`printf` silently made `printf`'s 0 the answer,
#   which turns "the daemon is unreachable" into "this service has no container" at all fifteen
#   call sites. Measured: `bash -c 'set -euo pipefail; f(){ local v; v="$(false)"; echo AFTER; };
#   X="$(f)"; echo REACHED'` prints REACHED and exits 0.
overlay_container_name() {
    local service="$1" names="" rc=0
    names="$(docker ps \
        --filter "label=com.docker.compose.project=$(compose_project_name)" \
        --filter "label=com.docker.compose.service=${service}" \
        --filter "status=running" \
        --format '{{.Names}}' 2>/dev/null)" || rc=$?
    printf '%s\n' "${names%%$'\n'*}"
    return "$rc"
}
