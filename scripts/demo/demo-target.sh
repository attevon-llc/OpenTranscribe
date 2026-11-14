#!/usr/bin/env bash
# demo-target.sh: sourced by demo-reset.sh and demo-upgrade.sh. Resolves WHICH compose project the
# demo scripts act on and asserts it BEFORE any `down -v` (they delete every volume).
#
# Project choice follows docs.docker.com/compose/how-tos/project-name/: DEMO_FRESH_NAME
# (an `./opentr.sh start ... --fresh NAME` stack, project otfresh-NAME, so the scripts can be
# exercised without touching the real demo), else COMPOSE_PROJECT_NAME (environment, then the
# install's .env), else the install directory's basename. The choice is exported as
# COMPOSE_PROJECT_NAME so every later `docker compose` uses exactly the asserted project.
#
# The assertion is scripts/lib/stack-target.sh (compose project LABELS, never a name substring):
# the project must have containers, and their compose working_dir label must be this install
# directory. Exit codes are stack_target's contract: 20 NO_PROJECT, 21 WRONG_PROJECT, 28 DOCKER.
# Needs INSTALL_DIR set by the caller.
#
# A fresh stack is addressed through opentr.sh's generated .fresh/<slug>.yml, which re-pins every
# container_name (otherwise any `up` collides with the live stack's names). It is exported as
# OPENTRANSCRIBE_EXTRA_COMPOSE_FILES, which opentranscribe.sh's get_compose_files appends last, so
# `compose-files`, `update` and `update-full` all resolve the fresh stack's chain. Missing: exit 2.

demo_assert_target() {
    local lib here project want have rc=0
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    lib="$here/../lib/stack-target.sh"
    if [ ! -r "$lib" ] || [ ! -r "$here/../lib/stack_target.py" ]; then
        echo "scripts/lib/stack-target.sh is missing from $here/..: run ./opentranscribe.sh update-full" >&2
        return 2
    fi
    if [ -n "${DEMO_FRESH_NAME:-}" ]; then
        # opentr.sh fresh_sanitize_name: the slug names the project and the .fresh/ files.
        DEMO_FRESH_SLUG="$(printf '%s' "$DEMO_FRESH_NAME" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-//;s/-$//')"
        project="otfresh-${DEMO_FRESH_SLUG:=default}"
    else
        project="${COMPOSE_PROJECT_NAME:-}"
        [ -n "$project" ] || project="$(grep -E '^COMPOSE_PROJECT_NAME=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' "'"'")"
        [ -n "$project" ] || project="$(basename "$INSTALL_DIR" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-' | sed 's/^[-_]*//')"
    fi
    # shellcheck source=scripts/lib/stack-target.sh
    source "$lib"
    st_target --project "$project" || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "demo: refusing to touch compose project '$project': it did not resolve (exit $rc)" >&2
        return "$rc"
    fi
    want="$(cd "$INSTALL_DIR" && pwd -P)"
    have="$(cd "${ST_WORKING_DIR:-/nonexistent}" 2>/dev/null && pwd -P || true)"
    if [ "$have" != "$want" ]; then
        echo "demo: refusing: project '$project' runs from '${ST_WORKING_DIR:-?}', not this install ($want)" >&2
        return 21
    fi
    if [ -n "${DEMO_FRESH_NAME:-}" ]; then
        if [ ! -f "$INSTALL_DIR/.fresh/${DEMO_FRESH_SLUG}.yml" ]; then
            echo "demo: refusing: .fresh/${DEMO_FRESH_SLUG}.yml (the fresh stack's container names) is missing" >&2
            return 2
        fi
        # A `start prod --build` fresh stack records its build-from-source overlay in .aux (opentr.sh
        # fresh_prod_dispatch): its images exist only on this host, so `up` must never pull them, or
        # a published tag of the same name would replace the bytes under test.
        local built_overlay=""
        built_overlay="$(grep -x 'docker-compose.local.yml' "$INSTALL_DIR/.fresh/${DEMO_FRESH_SLUG}.aux" 2>/dev/null)" || built_overlay=""
        export OPENTRANSCRIBE_EXTRA_COMPOSE_FILES="-f .fresh/${DEMO_FRESH_SLUG}.yml${built_overlay:+ -f $built_overlay}"
    fi
    export COMPOSE_PROJECT_NAME="$project"
    echo "demo: target project $project ($want)"
}
