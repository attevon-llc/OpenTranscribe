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
#                              default: "opensearch_data:48g postgres_data:4g"; empty disables
# Why tmpfs: this host's docker root (/mnt/nas/docker) is network storage, so a named volume's
# fsync takes seconds. OpenSearch persists cluster state on every create-index and answered 503
# `process_cluster_event_timeout_exception` (10-20 s per index even idle), which failed 76
# search/speaker/summary tests and stalled the integration phase. The data is throwaway.
# The OpenSearch size is NOT arbitrary: docker-compose.yml sets ABSOLUTE disk watermarks
# (low 30gb / high 20gb / flood 10gb free). An 8g tmpfs sat below all three, so OpenSearch put a
# cluster-wide create-index block and read-only-allow-delete on every index the moment it
# started (403 index_create_block_exception on 53 setup steps). tmpfs only uses RAM for
# what is written, so the cap must simply exceed the low watermark.
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
    # Main stack + the mock LLM/ASR overlays the gate starts (run-dev-tests.sh).
    sed -n '/^FRESH_\(MOCK_LLM_\|MOCK_ASR_\)\{0,1\}PORT_VARS=(/,/^)/p' "$repo/opentr.sh" \
        | sed -n 's/^[[:space:]]*"\([A-Z_]*\)=\([0-9]*\)".*/\1 \2/p' \
        | while read -r name base; do
            echo "${name}=$((base + offset))"
        done
}

# Volumes this lib pre-creates on tmpfs; compose adopts a volume of the project-scoped name.
fresh_stack_fast_volumes() {
    echo "${RELEASE_TEST_FAST_VOLUMES-opensearch_data:48g postgres_data:4g}"
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

# fresh_stack_seed_models <name>
# A --fresh stack gets an ISOLATED, initially empty diar-native model dir
# (.fresh/<name>/diar-native-models) and expects its backend to provision it from the network
# AFTER startup. But the diar-native sidecar exits 8 on an empty dir and `up --wait` fails
# first, so the stack never comes up (release.sh test exit 3). Seed the isolated dir from the
# existing export (read-only copy; the live export is never mounted or written) so every run
# starts with the same, complete model set. Source: RELEASE_TEST_DIAR_MODELS_SRC, else
# DIAR_NATIVE_MODELS_DIR, else ./models/diar-native. No source or an empty one => skip, and
# the stack falls back to provisioning exactly as before.
fresh_stack_seed_models() {
    local name="$1" repo="${REPO_ROOT:-.}" slug src dest
    slug="$(echo "$name" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-//;s/-$//')"
    src="${RELEASE_TEST_DIAR_MODELS_SRC:-${DIAR_NATIVE_MODELS_DIR:-$repo/models/diar-native}}"
    dest="$repo/.fresh/${slug:-default}/diar-native-models"
    if [[ ! -d "$src" || -z "$(ls -A "$src" 2>/dev/null)" ]]; then
        echo "no diar-native export at $src to seed from; the stack will provision its own" >&2
        return 0
    fi
    mkdir -p "$dest"
    cp -a "$src"/. "$dest"/
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
    fresh_stack_seed_models "$name" || return 1
    "$repo/opentr.sh" start dev --fresh "$name" --port-offset "$offset" >&2 || return 1
    fresh_stack_seed_library
}

# fresh_stack_seed_library: the same small corpus on every run (scripts/seed-fresh-deployment.sh
# picks the SEED_MAX_FILES smallest files; idempotent per file). Without it the library is empty
# and tests that need a completed file skip or fail. Non-fatal by that script's own contract.
fresh_stack_seed_library() {
    local repo="${REPO_ROOT:-.}"
    [[ -f "$repo/scripts/seed-fresh-deployment.sh" ]] || return 0
    BACKEND_URL="http://localhost:${BACKEND_PORT:-5174}" bash "$repo/scripts/seed-fresh-deployment.sh" >&2 || true
}

# ── Preflight ────────────────────────────────────────────────────────────────────────────────
# Cheap checks that turn "an hour into the gate, 76 tests errored" into "exit 3 in seconds,
# with the reason". Each check prints ONE line on failure and returns 1; none changes anything
# (nothing is cleared, killed or repaired). Every host-specific value comes from the
# environment (ports exported by fresh_stack_up, RELEASE_TEST_* knobs) or is derived.
#   RELEASE_TEST_MAX_LOAD        1-min load ceiling              default 40
#   RELEASE_TEST_OS_MAX_SECONDS  create+delete index ceiling     default 5
#   RELEASE_TEST_LOADAVG_FILE    where to read load from         default /proc/loadavg

# fresh_stack_check_no_competitors <name>: no other gate process, no other otfresh stack.
fresh_stack_check_no_competitors() {
    local name="$1" pid args mine=" " p busy="" other
    p=$$
    local hops=0
    while [[ "$p" =~ ^[0-9]+$ && "$p" -gt 1 && $hops -lt 64 ]]; do
        mine+="$p "
        p="$(sed -n 's/^PPid:[[:space:]]*//p' "/proc/$p/status" 2>/dev/null)"
        hops=$((hops + 1))
    done
    while read -r pid args; do
        [[ "$mine" == *" $pid "* ]] && continue
        case "$args" in
            *release.sh*|*run-dev-tests.sh*|*run-integration-tests.sh*) busy+="${pid}(${args%% *}) " ;;
        esac
    done < <(ps -eo pid=,args= 2>/dev/null | grep -Ev " (rg|pgrep|grep) |bash -c")
    if [[ -n "$busy" ]]; then
        echo "another gate process is running: $busy" >&2
        return 1
    fi
    other="$(docker ps --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
        | sort -u | grep "^otfresh-" | grep -vx "otfresh-${name}" | tr '\n' ' ' || true)"
    if [[ -n "$other" ]]; then
        echo "another fresh stack is running: $other" >&2
        return 1
    fi
}

# fresh_stack_check_opensearch: scratch index round-trip is fast and no blocks are set.
fresh_stack_check_opensearch() {
    local base="http://127.0.0.1:${OPENSEARCH_PORT:?OPENSEARCH_PORT not exported}"
    local max="${RELEASE_TEST_OS_MAX_SECONDS:-5}" idx="zz_preflight_$$" t0 t1 body
    t0=$(date +%s.%N)
    if ! curl -sf --max-time 30 -XPUT "$base/$idx" >/dev/null \
        || ! curl -sf --max-time 30 -XDELETE "$base/$idx" >/dev/null; then
        echo "OpenSearch could not create+delete a scratch index (blocked or unhealthy)" >&2
        return 1
    fi
    t1=$(date +%s.%N)
    if python3 -c "import sys; sys.exit(0 if float('$t1')-float('$t0') <= float('$max') else 1)"; then :; else
        echo "OpenSearch create+delete index took more than ${max}s (slow storage?)" >&2
        return 1
    fi
    body="$(curl -sf --max-time 30 "$base/_cluster/settings?flat_settings=true" || true)"
    if ! BODY="$body" python3 - <<'PY'
import json, os, sys
try:
    s = json.loads(os.environ["BODY"])
except ValueError:
    print("OpenSearch _cluster/settings unreadable", file=sys.stderr); sys.exit(1)
bad = [k for scope in ("persistent", "transient") for k in s.get(scope, {}) if ".blocks." in k or k.startswith("cluster.blocks")]
if bad:
    print("OpenSearch has cluster block settings: " + ", ".join(bad), file=sys.stderr); sys.exit(1)
PY
    then return 1; fi
    body="$(curl -sf --max-time 30 "$base/_all/_settings?flat_settings=true&filter_path=*.settings.index.blocks*" || true)"
    if [[ "$body" == *read_only_allow_delete* && "$body" == *true* ]]; then
        echo "OpenSearch has indices with a read_only_allow_delete block (disk watermark?)" >&2
        return 1
    fi
}

# fresh_stack_check_volumes <name>: the fast volumes really are tmpfs, not network storage.
fresh_stack_check_volumes() {
    local name="$1" spec vol opts
    for spec in $(fresh_stack_fast_volumes); do
        vol="otfresh-${name}_${spec%%:*}"
        opts="$(docker volume inspect "$vol" --format '{{.Options}}' 2>/dev/null || true)"
        if [[ "$opts" != *type:tmpfs* ]]; then
            echo "volume $vol is not tmpfs (options: ${opts:-none}); compose did not adopt the fast volume" >&2
            return 1
        fi
    done
}

# fresh_stack_check_load: 1-minute load average below the ceiling.
fresh_stack_check_load() {
    local max="${RELEASE_TEST_MAX_LOAD:-40}" file="${RELEASE_TEST_LOADAVG_FILE:-/proc/loadavg}" load
    load="$(cut -d' ' -f1 "$file" 2>/dev/null)"
    if [[ -z "$load" ]] || ! python3 -c "import sys; sys.exit(0 if float('$load') < float('$max') else 1)" 2>/dev/null; then
        echo "host 1-min load ${load:-unknown} is not below ${max} (RELEASE_TEST_MAX_LOAD)" >&2
        return 1
    fi
}

# fresh_stack_preflight_before <name>: cheap, runs before the (long) stack build.
fresh_stack_preflight_before() {
    fresh_stack_check_no_competitors "$1" && fresh_stack_check_load
}

# fresh_stack_preflight_after <name>: runs once the stack is healthy, before the long phases.
fresh_stack_preflight_after() {
    fresh_stack_check_no_competitors "$1" \
        && fresh_stack_check_volumes "$1" \
        && fresh_stack_check_opensearch \
        && fresh_stack_check_load
}
