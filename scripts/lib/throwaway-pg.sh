#!/bin/bash
# Throwaway Postgres for tests: free loopback port, tmpfs data, labelled, removable by name.
# Source it; it defines functions only. Used by scripts/test-in-worktree.sh.
#
# Every docker call here names the ONE container this lib started (TPG_NAME). Nothing lists,
# filters, prunes or signals any other container -- the host docker daemon is shared
# (cvat_*, tritonserver) and a name-pattern cleanup has destroyed an unrelated one before.
#
# Label: ot-test-artifact=1 (backend/tests/docker_ops.py TEST_ARTIFACT_LABEL), the label
# scripts/lib/test_artifact_sweep.py recognises as a test leftover.

TPG_IMAGE="${OT_WT_PG_IMAGE:-postgres:17.5-alpine}"   # the image the recipe/throwaway_pg use
TPG_LABEL="ot-test-artifact=1"
TPG_TMPFS_SIZE="${OT_WT_PG_TMPFS_SIZE:-2g}"
TPG_READY_TIMEOUT="${OT_WT_READY_TIMEOUT:-120}"
TPG_USER="postgres"
TPG_DB="transcribe_test"

TPG_NAME=""
TPG_PORT=""
TPG_PASSWORD=""

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
    (( port >= ${STOCK_PORT_LOW:-5173} && port <= ${STOCK_PORT_HIGH:-5180} ))
}

# tpg_start -> sets TPG_NAME/TPG_PORT/TPG_PASSWORD; returns 1 with a message on failure.
tpg_start() {
    local attempt out
    TPG_NAME="ot-wt-test-$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    TPG_PASSWORD="pw$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    for attempt in 1 2 3; do
        TPG_PORT="$(tpg_free_port)" || { echo "cannot pick a free port" >&2; return 1; }
        if tpg_port_is_stock "$TPG_PORT"; then
            echo "refusing stock port ${TPG_PORT} (${STOCK_PORT_LOW:-5173}-${STOCK_PORT_HIGH:-5180} belongs to the dev stack)" >&2
            return 1
        fi
        if out="$(docker run -d --name "$TPG_NAME" --label "$TPG_LABEL" \
            -e POSTGRES_USER="$TPG_USER" -e POSTGRES_PASSWORD="$TPG_PASSWORD" -e POSTGRES_DB="$TPG_DB" \
            -p "127.0.0.1:${TPG_PORT}:5432" \
            --tmpfs "/var/lib/postgresql/data:rw,size=${TPG_TMPFS_SIZE}" \
            "$TPG_IMAGE" 2>&1)"; then
            return 0
        fi
        # A port lost to a race between probe and bind: the failed create left a name behind.
        docker rm -f "$TPG_NAME" >/dev/null 2>&1 || true
        echo "docker run attempt ${attempt} failed: ${out}" >&2
    done
    return 1
}

# tpg_wait_ready -> 0 once two consecutive queries succeed (the image restarts postgres once
# during init, so one success is not proof), 1 after TPG_READY_TIMEOUT seconds.
tpg_wait_ready() {
    local deadline=$((SECONDS + TPG_READY_TIMEOUT)) ok=0
    while (( SECONDS < deadline )); do
        if docker exec "$TPG_NAME" psql -U "$TPG_USER" -d "$TPG_DB" -tAc 'SELECT 1' >/dev/null 2>&1; then
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

# tpg_remove -> remove exactly the container this lib started; harmless if absent
tpg_remove() {
    [[ -n "$TPG_NAME" ]] && docker rm -f "$TPG_NAME" >/dev/null 2>&1
    return 0
}
