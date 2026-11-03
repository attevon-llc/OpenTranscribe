#!/bin/bash
# Isolated --fresh stack for release stages that need a running app (currently `test`).
#
# Why: a gate run against the long-lived dev stack depends on whatever that database holds
# (the seeded admin's password aged past the 60-day expiry, set must_change_password, and
# every authenticated test 403'd `password_change_required`). A `--fresh` deployment has its
# own compose project + named volumes, a clean migrated schema, a new bootstrap admin, and
# opentr.sh NEVER loads the NAS/bind overlay for it. This lib only drives opentr.sh's existing
# --fresh machinery; it owns no compose knowledge of its own.
#
# Configuration (env), all optional:
#   RELEASE_TEST_FRESH_NAME   deployment name   default: rel-<VERSION with dots as dashes>, e.g. rel-0-6-1
#   RELEASE_TEST_PORT_OFFSET  host-port offset  default: 400
#   RELEASE_TEST_KEEP=1       leave the stack up at exit (diagnosis)
# The stock port set is READ from opentr.sh's FRESH_PORT_VARS, never copied here.

# fresh_stack_name <version>  -> deployment name
fresh_stack_name() {
    if [[ -n "${RELEASE_TEST_FRESH_NAME:-}" ]]; then
        echo "$RELEASE_TEST_FRESH_NAME"
        return
    fi
    local v="${1#v}"
    v="${v//[^0-9]/-}"
    echo "rel-${v:-gate}"
}

# fresh_stack_port_exports <offset> -> lines `NAME=value` (stock port + offset), from opentr.sh
fresh_stack_port_exports() {
    local offset="$1" repo="${REPO_ROOT:-.}" name base
    # Entries look like:  "POSTGRES_PORT=5176"          # postgres → :5432
    sed -n '/^FRESH_PORT_VARS=(/,/^)/p' "$repo/opentr.sh" \
        | sed -n 's/^[[:space:]]*"\([A-Z_]*\)=\([0-9]*\)".*/\1 \2/p' \
        | while read -r name base; do
            echo "${name}=$((base + offset))"
        done
}

# fresh_stack_teardown <name>  -> same path as `opentr.sh fresh-destroy` (never signals a process)
fresh_stack_teardown() {
    local name="$1" repo="${REPO_ROOT:-.}"
    "$repo/opentr.sh" fresh-destroy "$name" --yes >/dev/null 2>&1 || true
}

# fresh_stack_up <name> <offset>
# Destroys any leftover deployment of that name first (stale state => identical seed every
# run), exports the offset ports + COMPOSE_PROJECT_NAME for the gate, then starts it.
fresh_stack_up() {
    local name="$1" offset="$2" repo="${REPO_ROOT:-.}" kv
    fresh_stack_teardown "$name"
    while IFS= read -r kv; do
        [[ -n "$kv" ]] && declare -gx "$kv"
    done < <(fresh_stack_port_exports "$offset")
    export COMPOSE_PROJECT_NAME="otfresh-${name}"
    "$repo/opentr.sh" start dev --fresh "$name" --port-offset "$offset" >&2
}
