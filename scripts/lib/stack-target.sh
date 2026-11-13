#!/usr/bin/env bash
# Sourced front end for scripts/lib/stack_target.py. No logic of its own: the Python module is
# the one implementation (so pytest and shell cannot drift). Read its docstring for the
# requirement grammar and the exit-code contract (20-28; scripts/CLAUDE.md repeats it).
#
#   source "$REPO_ROOT/scripts/lib/stack-target.sh"
#   st_target --fresh "$NAME"          # or --project P | --stock; exports the OBSERVED ports
#   st_require schema-head code=bind healthy asset:frontend-fonts
#   st_container backend               # label-scoped container name in $ST_PROJECT ("" if none)
#   st_prepare pg_stat_statements      # fresh stacks only
#
# Rules: every function captures then parses (no `| head -1`, see compose-project.sh's
# SIGPIPE notes), re-returns rc explicitly, and never `exit`s: the caller decides. Release
# stages map any non-zero to their own contract with `st_require ... || exit 3`.

_ST_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stack_target.py"

# st_target [--fresh NAME | --project P | --stock]
#   Evals `--emit-env` into the caller's shell only on success, so nothing half-applies.
st_target() {
    local out rc=0
    out="$(python3 "$_ST_PY" resolve --emit-env "$@")" || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        eval "$out"
    fi
    return "$rc"
}

# st_require REQ...   (target = the project st_target resolved; ST_PROJECT must be set)
st_require() {
    local args=() req rc=0
    [[ -n "${ST_PROJECT:-}" ]] || { echo "stack-target: st_require before st_target" >&2; return 2; }
    for req in "$@"; do args+=(--require "$req"); done
    python3 "$_ST_PY" check --project "$ST_PROJECT" --no-banner "${args[@]}" || rc=$?
    return "$rc"
}

# st_container SERVICE -> first running container of the service in $ST_PROJECT
st_container() {
    local out rc=0
    out="$(python3 "$_ST_PY" container "$1" --project "${ST_PROJECT:?st_container before st_target}" --no-banner)" || rc=$?
    printf '%s\n' "$out"
    return "$rc"
}

# st_prepare pg_stat_statements   (fresh only; never the live or stock stack)
st_prepare() {
    [[ "${1:-}" == "pg_stat_statements" ]] || { echo "st_prepare: only pg_stat_statements" >&2; return 2; }
    [[ -n "${ST_FRESH_NAME:-}" ]] || { echo "stack-target: prepare needs a --fresh target" >&2; return 2; }
    python3 "$_ST_PY" prepare --fresh "$ST_FRESH_NAME" --pg-stat-statements
}
