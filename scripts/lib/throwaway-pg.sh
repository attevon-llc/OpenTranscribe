#!/bin/bash
# Throwaway Postgres for tests: start, migrate, export env, clean up. ONE engine, used by
# `scripts/run-backend-tests.sh --throwaway-db`. Source it; it defines functions only.
#
#   tpg_provision <python> <backend_dir>   start + wait + export env + `alembic upgrade head`
#                                          returns 0 · 3 precondition (docker, port, readiness)
#                                          · 4 migration failed
#   tpg_cleanup                            idempotent teardown; safe from EXIT/INT/TERM traps
#
# Safety properties (each pinned by backend/tests/unit/test_run_backend_tests_throwaway_db.py):
#   * a kernel-assigned free loopback port, never one of the stock-stack ports 5173-5180
#   * a random per-run container name and password, tmpfs data
#   * a private compose project `opentranscribe-pytest-<hex32>`: no project-scoped command
#     against the live stack can match it, and it is the namespace shape
#     scripts/lib/test_artifact_sweep.py (`./opentr.sh test-cleanup`) recognises as a test
#     leftover. A compose-project label of any OTHER shape makes the sweep refuse the container.
#   * every docker call names the ONE container this lib started. Nothing lists, filters, prunes
#     or signals any other container: the host docker daemon is shared (cvat_*, tritonserver)
#     and a name-pattern cleanup has destroyed an unrelated one before.

_TPG_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=stock-port-owners.sh
source "$_TPG_LIB_DIR/stock-port-owners.sh"

TPG_IMAGE="${OT_TEST_PG_IMAGE:-postgres:17.5-alpine}"
TPG_LABEL="ot-test-artifact=1"
TPG_PURPOSE_LABEL="org.opentranscribe.purpose=throwaway-test-db"
TPG_TMPFS_SIZE="${OT_TEST_PG_TMPFS_SIZE:-2g}"
# Postgres's default (100) is exhausted by `-n auto` on a 48-core host: every xdist worker keeps two
# connections (tests/conftest.py's engine and the app's) for the whole session, 96 in all, so any
# test that briefly opens a third on each of a few workers fails unrelated tests with "too many
# clients already". docker-compose.yml sizes the live database the same way (PG_MAX_CONNECTIONS).
TPG_MAX_CONNECTIONS="${OT_TEST_PG_MAX_CONNECTIONS:-300}"
TPG_READY_TIMEOUT="${OT_TEST_PG_READY_TIMEOUT:-120}"
# Bound on retries after losing the probe-then-bind race to a concurrent run.
TPG_RUN_ATTEMPTS=5
TPG_USER="postgres"
TPG_DB="transcribe_test"

TPG_NAME=""        # set only once `docker run` succeeded: the container this run owns
TPG_PORT=""
TPG_PASSWORD=""
TPG_WORK_DIR=""
TPG_KEEP=0         # 1 = leave the container and temp dir, print how to remove them

# tpg_free_port -> a currently-unbound loopback port (kernel-assigned, so never hard-coded)
tpg_free_port() {
    python3 -c 'import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()'
}

# tpg_port_is_stock <port> -> 0 when it lies in the stock-stack range (stock-port-owners.sh)
tpg_port_is_stock() {
    local port="$1"
    (( port >= STOCK_PORT_LOW && port <= STOCK_PORT_HIGH ))
}

# tpg_start -> sets TPG_NAME/TPG_PORT/TPG_PASSWORD; returns 1 with a message on failure.
tpg_start() {
    local attempt out candidate project platform platform_args=()
    candidate="ot-testdb-$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    project="opentranscribe-pytest-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    TPG_PASSWORD="pw$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    # Anchored: a prefix match must neither refuse nor select anybody else's container.
    if [[ -n "$(docker ps -a --filter "name=^${candidate}\$" --format '{{.Names}}')" ]]; then
        echo "container ${candidate} already exists; refusing to touch it" >&2
        return 1
    fi
    # Pin the image platform to the daemon's own: a foreign-arch image left in the local store
    # by a multi-arch build would otherwise be picked.
    if platform="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null)" \
        && [[ -n "$platform" && "$platform" != "/" ]]; then
        platform_args=(--platform "$platform")
    fi
    for attempt in $(seq 1 "$TPG_RUN_ATTEMPTS"); do
        TPG_PORT="$(tpg_free_port)" || { echo "cannot pick a free port" >&2; return 1; }
        if tpg_port_is_stock "$TPG_PORT"; then
            echo "refusing stock port ${TPG_PORT} (${STOCK_PORT_LOW}-${STOCK_PORT_HIGH} belongs to the dev stack)" >&2
            return 1
        fi
        if out="$(docker run -d --rm --name "$candidate" ${platform_args[@]+"${platform_args[@]}"} \
            --label "$TPG_LABEL" --label "$TPG_PURPOSE_LABEL" \
            --label "com.docker.compose.project=${project}" \
            -e POSTGRES_USER="$TPG_USER" -e POSTGRES_PASSWORD="$TPG_PASSWORD" -e POSTGRES_DB="$TPG_DB" \
            -p "127.0.0.1:${TPG_PORT}:5432" \
            --tmpfs "/var/lib/postgresql/data:rw,size=${TPG_TMPFS_SIZE}" \
            "$TPG_IMAGE" postgres -c "max_connections=${TPG_MAX_CONNECTIONS}" 2>&1)"; then
            TPG_NAME="$candidate"
            return 0
        fi
        # A name conflict means the container is somebody else's: never remove it.
        [[ "$out" == *"already in use by container"* ]] && { echo "$out" >&2; return 1; }
        # The failed create may have left our own container behind.
        docker rm -f "$candidate" >/dev/null 2>&1 || true
        if [[ "$out" == *"port is already allocated"* || "$out" == *"address already in use"* ]] \
            && (( attempt < TPG_RUN_ATTEMPTS )); then
            echo "port ${TPG_PORT} was taken by a concurrent run; retrying on another port" >&2
            continue
        fi
        echo "$out" >&2
        return 1
    done
    return 1
}

# tpg_wait_ready -> 0 once two consecutive TCP queries succeed (the image restarts postgres once
# during init and the init-time server listens on the unix socket only, so one success, or a
# socket probe, is not proof), 1 after TPG_READY_TIMEOUT seconds.
tpg_wait_ready() {
    local deadline=$((SECONDS + TPG_READY_TIMEOUT)) ok=0
    while (( SECONDS < deadline )); do
        if docker exec "$TPG_NAME" psql -h 127.0.0.1 -U "$TPG_USER" -d "$TPG_DB" -tAc 'SELECT 1' >/dev/null 2>&1; then
            ok=$((ok + 1))
            (( ok >= 2 )) && return 0
        else
            ok=0
        fi
        sleep 0.5
    done
    echo "postgres in ${TPG_NAME} not ready after ${TPG_READY_TIMEOUT}s" >&2
    docker logs --tail 20 "$TPG_NAME" >&2 2>&1 || true
    return 1
}

# tpg_keep -> leave the container and temp dir in place on exit (tpg_cleanup prints how to remove)
tpg_keep() { TPG_KEEP=1; }

# tpg_export_env -> point the app and alembic at the throwaway database.
# POSTGRES_HOST is explicit because alembic does not import conftest (which forces localhost)
# and would resolve the compose service name; DATA/MODELS/TEMP_DIR because config.py defaults
# them to /app/... and mkdirs them; SKIP_S3/SKIP_OPENSEARCH because conftest would otherwise
# enable S3-backed tests if anything at all answers on :5178/:5180.
tpg_export_env() {
    TPG_WORK_DIR="$(mktemp -d -t ot-throwaway-db.XXXXXX)" || return 1
    mkdir -p "$TPG_WORK_DIR"/{data,models,temp}
    export DATA_DIR="$TPG_WORK_DIR/data" MODELS_DIR="$TPG_WORK_DIR/models" TEMP_DIR="$TPG_WORK_DIR/temp"
    export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT="$TPG_PORT" POSTGRES_USER="$TPG_USER" \
           POSTGRES_PASSWORD="$TPG_PASSWORD" POSTGRES_DB="$TPG_DB"
    export SKIP_S3=True SKIP_OPENSEARCH=True TESTING=True
    # Settings derives both URLs from POSTGRES_*. An exported URL would leak into every Settings()
    # a unit test builds, and an ambient one (or a role split) would point this run elsewhere.
    unset DATABASE_URL MIGRATION_DATABASE_URL POSTGRES_APP_USER POSTGRES_APP_PASSWORD POSTGRES_SSLMODE
}

# tpg_provision <python> <backend_dir>
# The migration runs from <backend_dir>, i.e. THIS checkout's alembic, so the schema matches the
# branch under test rather than whatever the dev stack happens to run.
tpg_provision() {
    local py="$1" backend_dir="$2"
    command -v docker >/dev/null 2>&1 || { echo "error: docker not found on PATH" >&2; return 3; }
    command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found on PATH" >&2; return 3; }
    tpg_start || return 3
    tpg_wait_ready || return 3
    tpg_export_env || { echo "error: cannot create a temp dir" >&2; return 3; }
    echo ">> ${TPG_NAME} on 127.0.0.1:${TPG_PORT}; alembic upgrade head from ${backend_dir}" >&2
    (cd "$backend_dir" && "$py" -m alembic upgrade head >&2) \
        || { echo "error: alembic upgrade head failed" >&2; return 4; }
}

# tpg_cleanup -> teardown for EXIT/INT/TERM traps. Idempotent: INT/TERM handlers `exit`, which
# fires the EXIT trap a second time, and the second pass must find nothing left to do.
tpg_cleanup() {
    local name="$TPG_NAME" work="$TPG_WORK_DIR"
    TPG_NAME=""
    TPG_WORK_DIR=""
    if (( TPG_KEEP )); then
        if [[ -n "$name" || -n "$work" ]]; then
            echo "--keep: left container '${name}' and ${work}. Remove with:" >&2
            echo "  docker rm -f ${name}; rm -rf ${work}" >&2
        fi
        return 0
    fi
    if [[ -n "$name" ]]; then
        docker rm -f "$name" >/dev/null 2>&1 || true
    fi
    if [[ -n "$work" ]]; then
        rm -rf -- "$work"
    fi
    return 0
}
