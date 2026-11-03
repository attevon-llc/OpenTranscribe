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
#   RELEASE_TEST_FAST_VOLUMES  space-separated `volume:size` pairs backed by tmpfs
#                              default: "opensearch_data:8g postgres_data:4g"; empty disables
# Why tmpfs: this host's docker root (/mnt/nas/docker) is network storage, so a named volume's
# fsync takes seconds. OpenSearch persists cluster state on every create-index and answered 503
# `process_cluster_event_timeout_exception` (10-20 s per index even idle), which failed 76
# search/speaker/summary tests and stalled the integration phase. The data is throwaway.
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

# Volumes this lib pre-creates on tmpfs; compose adopts a volume of the project-scoped name.
fresh_stack_fast_volumes() {
    echo "${RELEASE_TEST_FAST_VOLUMES-opensearch_data:8g postgres_data:4g}"
}

# fresh_stack_teardown <name>  -> same path as `opentr.sh fresh-destroy` (never signals a process)
fresh_stack_teardown() {
    local name="$1" repo="${REPO_ROOT:-.}" spec
    "$repo/opentr.sh" fresh-destroy "$name" --yes >/dev/null 2>&1 || true
    # fresh-destroy only knows volumes compose created; ours carry no compose label.
    for spec in $(fresh_stack_fast_volumes); do
        docker volume rm "otfresh-${name}_${spec%%:*}" >/dev/null 2>&1 || true
    done
}

# fresh_stack_up <name> <offset>
# Destroys any leftover deployment of that name first (stale state => identical seed every
# run), exports the offset ports + COMPOSE_PROJECT_NAME for the gate, then starts it.
fresh_stack_up() {
    local name="$1" offset="$2" repo="${REPO_ROOT:-.}" kv spec
    fresh_stack_teardown "$name"
    while IFS= read -r kv; do
        [[ -n "$kv" ]] && declare -gx "$kv"
    done < <(fresh_stack_port_exports "$offset")
    export COMPOSE_PROJECT_NAME="otfresh-${name}"
    for spec in $(fresh_stack_fast_volumes); do
        docker volume create --driver local --opt type=tmpfs --opt device=tmpfs \
            --opt "o=size=${spec##*:},mode=1777" "otfresh-${name}_${spec%%:*}" >&2 || return 1
    done
    # The e2e runner and the gate address the app by URL; point them at the offset ports.
    export E2E_FRONTEND_URL="http://localhost:${FRONTEND_PORT:-5173}"
    export E2E_BACKEND_URL="http://localhost:${BACKEND_PORT:-5174}"
    "$repo/opentr.sh" start dev --fresh "$name" --port-offset "$offset" >&2
}
