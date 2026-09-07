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

# compose_project_name
#   The live stack's compose project. $COMPOSE_PROJECT_NAME wins; otherwise detected from a
#   running postgres container's label; otherwise the old directory-name guess, which is only
#   reached when no stack is up (in which case every lookup below would find nothing anyway).
compose_project_name() {
    if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
        echo "$COMPOSE_PROJECT_NAME"
        return
    fi
    local detected root
    detected="$(docker ps \
        --filter "label=com.docker.compose.service=postgres" \
        --filter "status=running" \
        --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | head -1)"
    root="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
    echo "${detected:-$(basename "$root")}"
}

# overlay_container_name SERVICE
#   The running container for a compose SERVICE in the live project, or "" if none.
#   Works regardless of container_name — including services that declare none.
overlay_container_name() {
    local service="$1"
    docker ps \
        --filter "label=com.docker.compose.project=$(compose_project_name)" \
        --filter "label=com.docker.compose.service=${service}" \
        --filter "status=running" \
        --format '{{.Names}}' 2>/dev/null | head -1
}
