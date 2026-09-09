#!/bin/bash
#
# update-visual-baselines.sh — the ONE supported way to refresh the committed
# visual-regression baselines under backend/tests/e2e/__screenshots__/.
#
# WHY THIS EXISTS
#
# Until this script, the only discoverable mechanism was the env var
# `UPDATE_SCREENSHOTS=1` (test_visual_regression.py), and the only place the
# CORRECT full procedure was written down was a one-line hint string inside an
# unrelated release stage (scripts/release/30-verify.sh). So the easy path was
# the wrong one: run `UPDATE_SCREENSHOTS=1 pytest ...` against the shared dev
# stack and commit whatever came out. That bakes the developer's own library
# content into the reference image, and it is not a hypothetical —
#
#     shared-stack run 1 vs shared-stack run 2      0.0253%
#     shared-stack run   vs committed baseline      3.06%
#
# measured on `chat_trace` on 2026-09-07. A surface that reproduces itself to
# 0.03% is not flaky; the 3.06% was entirely the corpus, via masked chips whose
# digit counts changed the flex-wrap point of the row they sit in. Every one of
# this suite's historical baseline failures has this shape: the image was
# measuring how much data the deployment holds, not what the UI looks like.
#
# The script therefore makes the correct path the easy path, and makes the
# incorrect one unavailable:
#
#   1. It REFUSES to capture against the shared dev stack. This is the single
#      most important property in the file — it is how baselines rot. Refused
#      three independent ways (declared offset, resolved ports, and the compose
#      project that actually owns those ports once the stack is up), because the
#      first two are statements of intent and only the third is an observation.
#   2. It stands up the isolated seeded stack itself, so nobody has to remember
#      `--fresh visual --port-offset 100 --seed-benchmark --with-mock-llm`, and
#      stops it again afterwards.
#   3. It SHOWS the operator what changed before anything is accepted, per
#      surface, with the actual/diff images written somewhere discoverable. A
#      regenerated baseline nobody looked at is a visual suite that has stopped
#      testing.
#   4. It supports a SUBSET (`--surface chat_trace`). Regenerating all ten
#      images to fix one is how unrelated drift gets accepted in silence.
#   5. It refuses a stack that is not serving LOCAL source. A prod/nginx/PKI
#      overlay runs pre-built `davidamacey/opentranscribe-*` images from Docker
#      Hub, so a capture there is a picture of the PUBLISHED UI while the
#      operator believes they are photographing their branch. Detected mode is
#      printed either way.
#   6. It RECORDS why AND from which code, as a JSON sidecar per baseline.
#
# WHY A SIDECAR PER BASELINE, AND WHY NOT IN __screenshots__/
#
# A reference image is a claim about how the UI looks AT A SPECIFIC COMMIT.
# Without the commit recorded, nobody can later tell whether a baseline predates
# a feature or postdates it — which is exactly the confusion that let the
# `chat_trace` baseline go stale unnoticed. So each image gets its own
# `<surface>-<theme>.json` under backend/tests/e2e/screenshot-provenance/,
# carrying the reason, the HEAD sha, and whether the tree was DIRTY at capture.
#
# Per-surface rather than one manifest for the set, because `--surface` makes
# subset capture a first-class operation: a single set-wide manifest would
# restate a sha for nine images that were not recaptured, i.e. it would lie
# about precisely the thing it exists to record.
#
# ⚠️ The sidecars deliberately do NOT live inside __screenshots__/. The release
# gate `visual-baselines-fresh` (scripts/release/30-verify.sh) reads
# `git log -1 -- backend/tests/e2e/__screenshots__`, so a text file in that
# directory could clear a staleness gate without a single pixel being recaptured.
#
# Usage:
#   ./scripts/e2e/update-visual-baselines.sh --reason "settings modal redesign"
#   ./scripts/e2e/update-visual-baselines.sh --reason "..." --surface chat_trace
#   ./scripts/e2e/update-visual-baselines.sh --reason "..." --yes      # no tty
#   ./scripts/e2e/update-visual-baselines.sh --help
#
# Exit codes (the repo-standard contract):
#   0 accepted (or nothing to accept)   1 the capture itself failed
#   2 misuse (bad flags)                3 precondition unmet
#   4 operator abort / declined

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

EXIT_FAIL=1
EXIT_MISUSE=2
EXIT_PRECONDITION=3
EXIT_ABORT=4

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OPENTR="$REPO_ROOT/opentr.sh"
TEST_MODULE="$REPO_ROOT/backend/tests/e2e/test_visual_regression.py"
SCREENSHOT_DIR="$REPO_ROOT/backend/tests/e2e/__screenshots__"
#: Sibling of __screenshots__, never inside it — see the header for why.
PROVENANCE_DIR="$REPO_ROOT/backend/tests/e2e/screenshot-provenance"
VENV_PY="$REPO_ROOT/backend/venv/bin/python"

# Defaults. NAME/OFFSET match the incantation scripts/release/30-verify.sh has
# been printing as a hint string; keeping them identical means an operator who
# followed that hint and an operator who runs this script land on the same stack.
FRESH_NAME="${OT_VISUAL_FRESH_NAME:-visual}"
PORT_OFFSET="${OT_VISUAL_PORT_OFFSET:-100}"
#: How many times a +100 bump is retried when the chosen offset's ports are
#: taken. `--fresh` refuses to start on a bound port and tells you to pick
#: another offset; doing that by hand is the step people skip.
MAX_OFFSET_RETRIES=3
GPU_DEVICE=""          # empty = inherit the project's card from .env (never GPU 0)
REASON=""
ASSUME_YES=false
DRY_RUN=false
KEEP_STACK=false
DESTROY_STACK=false
SELECTED_SURFACES=()

usage() {
    cat <<'EOF'
update-visual-baselines.sh — refresh backend/tests/e2e/__screenshots__/ safely.

Stands up an ISOLATED, seeded stack (never the shared dev stack), captures the
selected surfaces, shows you what changed, and only then accepts. Records the
reason and the commit each image was taken from.

Usage:
  ./scripts/e2e/update-visual-baselines.sh --reason "settings modal redesign"
  ./scripts/e2e/update-visual-baselines.sh --reason "..." --surface chat_trace
  ./scripts/e2e/update-visual-baselines.sh --reason "..." --yes    # no tty

Options:
  --reason "<text>"     REQUIRED. Why the UI legitimately changed. Recorded.
  --surface <name>      Capture only this surface (repeatable). Default: all.
  --name <name>         Fresh deployment name (default: visual).
  --port-offset N       Port offset for the isolated stack (default: 100).
                        0 is REFUSED — that is the shared dev stack's ports.
  --gpu-device N        Pin the capture stack's AI workers to host GPU N.
                        Default: inherit .env (this project's card).
                        GPU 0 is REFUSED — it runs unrelated work on this host.
  --yes                 Accept without the interactive prompt (backgrounded /
                        non-interactive shells have no tty to read from).
  --keep                Leave the capture stack running afterwards.
  --destroy             Remove the capture stack's volumes too (default: stop,
                        which keeps the seeded corpus for the next run).
  --dry-run             Print the plan and the resolved safety checks; start
                        nothing, capture nothing, write nothing.
  -h, --help            This message.
EOF
}

log()  { echo -e "$*" >&2; }
die()  { log "${RED}❌ $1${NC}"; exit "${2:-$EXIT_FAIL}"; }

# ---------------------------------------------------------------------------
# Values DERIVED from the code that owns them, never transcribed.
#
# Every constant below has exactly one home elsewhere in the tree. A copy here
# would be correct on the day it was written and silently wrong afterwards —
# which is the same failure this whole script exists to stop, one level up.
# ---------------------------------------------------------------------------

#: Surfaces the suite knows about, read from test_visual_regression.py's own
#: SURFACES list so `--surface` cannot accept a name pytest will not select
#: (an unmatched -k expression collects nothing and exits 5, which reads as
#: "captured everything you asked for" if nobody checks).
ALL_SURFACES=()
derive_surfaces() {
    local line
    line="$(awk '/^SURFACES = \[/ { print; exit }' "$TEST_MODULE")"
    [[ -n "$line" ]] || die "no 'SURFACES = [' list in $TEST_MODULE — was it renamed? \
Refusing to guess the surface names." "$EXIT_PRECONDITION"
    line="${line#*[}"
    line="${line%%]*}"
    line="${line//\"/}"
    line="${line//,/ }"
    read -r -a ALL_SURFACES <<< "$line"
    [[ ${#ALL_SURFACES[@]} -gt 0 ]] || die "SURFACES parsed empty from $TEST_MODULE" \
        "$EXIT_PRECONDITION"
}

#: The host ports that mean "the shared dev stack". Read from the SAME constant
#: the test module refuses to compare against (`_SHARED_STACK_HOSTS`), so the
#: script and the suite can never disagree about what "shared" means.
SHARED_PORTS=()
derive_shared_ports() {
    local line rest port seen
    line="$(awk '/^_SHARED_STACK_HOSTS = \(/ { print; exit }' "$TEST_MODULE")"
    [[ -n "$line" ]] || die "no '_SHARED_STACK_HOSTS = (' tuple in $TEST_MODULE — was it \
renamed? Refusing to run without knowing which ports are the shared stack's." "$EXIT_PRECONDITION"
    rest="$line"
    while [[ "$rest" =~ :([0-9]+) ]]; do
        port="${BASH_REMATCH[1]}"
        seen=false
        for existing in ${SHARED_PORTS[@]+"${SHARED_PORTS[@]}"}; do
            [[ "$existing" == "$port" ]] && seen=true
        done
        $seen || SHARED_PORTS+=("$port")
        rest="${rest#*:"$port"}"
    done
    [[ ${#SHARED_PORTS[@]} -gt 0 ]] || die "_SHARED_STACK_HOSTS parsed no ports" \
        "$EXIT_PRECONDITION"
}

#: Base (offset 0) published ports, read out of opentr.sh's own FRESH_*_PORT_VARS
#: tables. Those tables are already documented as the single source of truth for
#: what --port-offset moves; a second copy here would be the bug that table's
#: header warns about.
BASE_FRONTEND_PORT=""
BASE_BACKEND_PORT=""
BASE_MOCK_LLM_PORT=""
# The BACKING SERVICES the test process talks to directly (not through the app).
# Without these the pytest process falls back to conftest.py's defaults, which are the
# SHARED dev stack's ports — see the export block by the pytest call for what that cost.
BASE_POSTGRES_PORT=""
BASE_MINIO_PORT=""
BASE_OPENSEARCH_PORT=""
opentr_default_port() {
    local var="$1"
    awk -v pat="\"${var}=" '
        { i = index($0, pat)
          if (i > 0) {
              rest = substr($0, i + length(pat))
              sub(/[^0-9].*$/, "", rest)
              if (rest != "") { print rest; exit }
          } }
    ' "$OPENTR"
}
derive_base_ports() {
    BASE_FRONTEND_PORT="$(opentr_default_port FRONTEND_PORT)"
    BASE_BACKEND_PORT="$(opentr_default_port BACKEND_PORT)"
    BASE_MOCK_LLM_PORT="$(opentr_default_port MOCK_LLM_PORT)"
    BASE_POSTGRES_PORT="$(opentr_default_port POSTGRES_PORT)"
    BASE_MINIO_PORT="$(opentr_default_port MINIO_PORT)"
    BASE_OPENSEARCH_PORT="$(opentr_default_port OPENSEARCH_PORT)"
    local var
    for var in BASE_FRONTEND_PORT BASE_BACKEND_PORT BASE_MOCK_LLM_PORT \
               BASE_POSTGRES_PORT BASE_MINIO_PORT BASE_OPENSEARCH_PORT; do
        [[ -n "${!var}" ]] || die "could not derive ${var#BASE_} from $OPENTR's \
FRESH_*_PORT_VARS tables — refusing to fall back to a hardcoded port" "$EXIT_PRECONDITION"
    done
}

# ---------------------------------------------------------------------------
# SAFETY: refuse the shared dev stack. Three independent checks.
# ---------------------------------------------------------------------------

# 1. The DECLARED intent. `--port-offset 0` is the shared stack's ports by
#    definition, and a resolved port landing on one of _SHARED_STACK_HOSTS' is
#    the same thing arrived at by arithmetic.
#
# Args: OFFSET FRONTEND_PORT BACKEND_PORT
assert_offset_is_not_the_shared_stack() {
    local offset="$1" frontend="$2" backend="$3" port collision=""

    if [[ "$offset" == "0" ]]; then
        log "${RED}❌ --port-offset 0 puts the capture stack on the SHARED dev stack's ports.${NC}"
        log "   Baselines captured there record the developer's own library content, which is"
        log "   how this suite's baselines rotted for 28 frontend commits. Use a non-zero offset:"
        log "     $0 --reason \"...\" --port-offset 100"
        return 1
    fi

    for port in "$frontend" "$backend"; do
        for shared in "${SHARED_PORTS[@]}"; do
            [[ "$port" == "$shared" ]] && collision="$collision $port"
        done
    done
    if [[ -n "$collision" ]]; then
        log "${RED}❌ Resolved capture ports${collision} are the SHARED dev stack's.${NC}"
        log "   test_visual_regression.py's _SHARED_STACK_HOSTS names them for the same reason."
        log "   Pick a different --port-offset."
        return 1
    fi
    return 0
}

# 2. The compose PROJECT must not be one this repo's live stack runs under.
#    scripts/lib/compose-project.sh owns that allowlist (OPENTR_LIVE_PROJECTS);
#    resolving it there rather than spelling "opentranscribe" here means a rename
#    cannot leave this check pointing at a project nothing uses.
#
# Args: PROJECT
assert_project_is_not_live() {
    local project="$1" live
    for live in "${OPENTR_LIVE_PROJECTS[@]}"; do
        if [[ "$project" == "$live" ]]; then
            log "${RED}❌ Capture project '${project}' is one of this repo's LIVE stack projects.${NC}"
            log "   A '--fresh <name>' deployment must run under otfresh-<name>. Refusing."
            return 1
        fi
    done
    return 0
}

# 3. The OBSERVATION, run after the stack is up: whatever is actually listening
#    on the ports we are about to point pytest at must belong to the capture
#    project. Checks 1 and 2 are statements of intent — this is the only one
#    that can catch a fresh stack that failed to come up while something else
#    (an older deployment, an unrelated project) holds the port. Capturing
#    against that would be silently wrong in exactly the way this file exists
#    to prevent.
#
# Args: PROJECT FRONTEND_PORT BACKEND_PORT
assert_ports_are_owned_by_the_capture_stack() {
    local project="$1" frontend="$2" backend="$3" port owner
    for port in "$frontend" "$backend"; do
        owner="$(port_owner_project "$port")"
        if [[ -z "$owner" ]]; then
            log "${RED}❌ Nothing in a compose project is publishing port ${port}.${NC}"
            log "   The capture stack is not up, or a non-Docker process holds the port."
            return 1
        fi
        if [[ "$owner" != "$project" ]]; then
            log "${RED}❌ Port ${port} is owned by compose project '${owner}', not '${project}'.${NC}"
            log "   Capturing against it would bake THAT deployment's content into the baseline."
            log "   Stop the other stack, or pick a different --port-offset."
            return 1
        fi
    done
    return 0
}

# The compose project of whatever container publishes a host port ("" if none).
#
# ⚠️ Deliberately not `docker ps ... | grep ... | head -1`. `docker` queries a
# daemon, so the gaps between its writes are RPC round-trips: a reader that
# leaves early SIGPIPEs the producer, and under `set -o pipefail` that 141
# becomes the pipeline's status — measured at ~1 in 250 on `docker info` in this
# repo, on 1.6 KB of output. In a CONDITION that inverts a match into a
# non-match, i.e. it would report the shared stack as safe. Capture, then match
# in-shell; there is no pipe to break.
port_owner_project() {
    local port="$1" listing="" line name ports
    listing="$(docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null)" || return 0
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        name="${line%%|*}"
        ports="${line#*|}"
        # "127.0.0.1:5273->5173/tcp" — anchor on the ':' so 15273 cannot match 5273.
        if [[ "$ports" == *":${port}->"* ]]; then
            docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' \
                "$name" 2>/dev/null || true
            return 0
        fi
    done <<< "$listing"
    return 0
}

# The first running container of SERVICE in PROJECT ("" if none).
#
# Project-scoped on purpose, and NOT compose-project.sh's `overlay_container_name`
# — that one resolves the LIVE stack by allowlist, which is the opposite of what
# is wanted here. Same no-pipe discipline as port_owner_project above.
project_container_name() {
    local project="$1" service="$2" names=""
    names="$(docker ps \
        --filter "label=com.docker.compose.project=${project}" \
        --filter "label=com.docker.compose.service=${service}" \
        --filter "status=running" \
        --format '{{.Names}}' 2>/dev/null)" || return 0
    printf '%s\n' "${names%%$'\n'*}"
}

# 4. The stack must be serving THIS CHECKOUT'S SOURCE, not a published image.
#
# Root CLAUDE.md's "Local Code vs Docker Hub Images" section: the prod / nginx /
# PKI overlays run pre-built `davidamacey/opentranscribe-*` images and local
# changes are invisible until a rebuild. Point this script at a stack of that
# shape and every symptom of success is present — the stack is up, isolated,
# seeded, on the right ports — while the images being captured are of the
# PUBLISHED UI. That is unfalsifiable from the pixels alone, so it is checked
# structurally instead.
#
# The check is the MECHANISM by which local code reaches the container, not a
# proxy for it: docker-compose.override.yml bind-mounts `./backend:/app` and
# `./frontend:/app` (Dockerfile.dev + `npm run dev`, i.e. Vite from source). No
# bind mount of this checkout means the container is running baked code,
# whatever its tag says.
#
# Args: PROJECT
assert_stack_serves_local_code() {
    local project="$1" service host_dir container mounts wanted missing=""
    for service in backend frontend; do
        host_dir="$REPO_ROOT/$service"
        container="$(project_container_name "$project" "$service")"
        if [[ -z "$container" ]]; then
            log "${RED}❌ No running '${service}' container in project '${project}'.${NC}"
            return 1
        fi
        mounts="$(docker inspect \
            --format '{{range .Mounts}}{{.Type}}:{{.Source}}->{{.Destination}} {{end}}' \
            "$container" 2>/dev/null)" || mounts=""
        wanted="bind:${host_dir}->/app"
        if [[ "$mounts" == *"$wanted"* ]]; then
            log "${GREEN}✔ ${service}: serving LOCAL source (${host_dir} bind-mounted at /app)${NC}"
        else
            missing="${missing} ${service}"
        fi
    done

    if [[ -n "$missing" ]]; then
        log "${RED}❌ Stack mode: BAKED IMAGE — these services have no bind mount of this"
        log "   checkout at /app:${missing}${NC}"
        log "   This looks like a prod / nginx / PKI shaped stack, which serves pre-built"
        log "   davidamacey/opentranscribe-* images from Docker Hub. Baselines captured there"
        log "   are pictures of the PUBLISHED UI, not of your branch, and nothing in the"
        log "   resulting images would say so. Refusing."
        log "   Capture from a dev-shaped stack: ${OPENTR} start dev --fresh ${FRESH_NAME} …"
        return 1
    fi
    log "${GREEN}✔ Stack mode: DEV (local source, hot-reloaded) — safe to capture from.${NC}"
    return 0
}

# GPU 0 on this host runs unrelated work (tritonserver, a standalone diar-server)
# and root CLAUDE.md is explicit that it must never be touched. The DEFAULT here
# is no --gpu-device at all, which inherits .env's GPU_DEVICE_ID — this project's
# own card — so the index is never hardcoded in either direction.
assert_gpu_device_allowed() {
    local device="$1"
    [[ -z "$device" ]] && return 0
    if ! [[ "$device" =~ ^[0-9]+$ ]]; then
        log "${RED}❌ --gpu-device must be a non-negative integer (got '${device}')${NC}"
        return 1
    fi
    if [[ "$device" == "0" ]]; then
        log "${RED}❌ --gpu-device 0 is refused. GPU 0 runs unrelated work on this host and"
        log "   must never be touched (root CLAUDE.md). Omit the flag to use this project's"
        log "   card from .env, or name a different index.${NC}"
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reason)      REASON="${2:-}"; shift 2 ;;
            --surface)     SELECTED_SURFACES+=("${2:-}"); shift 2 ;;
            --name)        FRESH_NAME="${2:-}"; shift 2 ;;
            --port-offset) PORT_OFFSET="${2:-}"; shift 2 ;;
            --gpu-device)  GPU_DEVICE="${2:-}"; shift 2 ;;
            --yes|-y)      ASSUME_YES=true; shift ;;
            --keep)        KEEP_STACK=true; shift ;;
            --destroy)     DESTROY_STACK=true; shift ;;
            --dry-run)     DRY_RUN=true; shift ;;
            -h|--help)     usage; exit 0 ;;
            *)             usage >&2; die "unknown argument: $1" "$EXIT_MISUSE" ;;
        esac
    done
}

validate_args() {
    local surface known

    # A baseline change is a CLAIM that the UI legitimately changed. Requiring
    # the claim in writing is the cheapest thing that keeps "refresh the
    # baselines until they pass" from becoming the reflex.
    [[ -n "$REASON" ]] || die "--reason is required. A baseline changing is a claim that the \
UI legitimately changed; say what changed." "$EXIT_MISUSE"

    [[ "$PORT_OFFSET" =~ ^[0-9]+$ ]] || die "--port-offset must be a non-negative integer \
(got '${PORT_OFFSET}')" "$EXIT_MISUSE"
    [[ "$FRESH_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "--name must be a lowercase compose-safe \
identifier (got '${FRESH_NAME}')" "$EXIT_MISUSE"

    assert_gpu_device_allowed "$GPU_DEVICE" || exit "$EXIT_MISUSE"

    if [[ ${#SELECTED_SURFACES[@]} -eq 0 ]]; then
        SELECTED_SURFACES=("${ALL_SURFACES[@]}")
    else
        for surface in "${SELECTED_SURFACES[@]}"; do
            known=false
            for candidate in "${ALL_SURFACES[@]}"; do
                [[ "$surface" == "$candidate" ]] && known=true
            done
            $known || die "unknown surface '${surface}'. Known: ${ALL_SURFACES[*]}" "$EXIT_MISUSE"
        done
    fi

    if $KEEP_STACK && $DESTROY_STACK; then
        die "--keep and --destroy are mutually exclusive" "$EXIT_MISUSE"
    fi
}

check_preconditions() {
    [[ -x "$OPENTR" ]] || die "$OPENTR not found or not executable" "$EXIT_PRECONDITION"
    [[ -f "$TEST_MODULE" ]] || die "$TEST_MODULE not found" "$EXIT_PRECONDITION"
    [[ -x "$VENV_PY" ]] || die "backend/venv not found — create it per CLAUDE.md's \
'Backend / venv' section first" "$EXIT_PRECONDITION"
    command -v docker >/dev/null 2>&1 || die "docker not on PATH" "$EXIT_PRECONDITION"
    docker info >/dev/null 2>&1 || die "docker daemon unreachable" "$EXIT_PRECONDITION"
}

# ---------------------------------------------------------------------------
# Stack lifecycle. ⚠️ Everything goes through ./opentr.sh — a hand-assembled
# `docker compose` invocation skips the overlay chain and can attach to a
# differently-configured stack (root CLAUDE.md's first rule).
# ---------------------------------------------------------------------------

STACK_STARTED_BY_US=false

start_capture_stack() {
    local attempt=0 start_args out rc

    while true; do
        FRONTEND_PORT=$((BASE_FRONTEND_PORT + PORT_OFFSET))
        BACKEND_PORT=$((BASE_BACKEND_PORT + PORT_OFFSET))
        MOCK_LLM_PORT=$((BASE_MOCK_LLM_PORT + PORT_OFFSET))
        POSTGRES_PORT=$((BASE_POSTGRES_PORT + PORT_OFFSET))
        MINIO_PORT=$((BASE_MINIO_PORT + PORT_OFFSET))
        OPENSEARCH_PORT=$((BASE_OPENSEARCH_PORT + PORT_OFFSET))
        FRESH_PROJECT="otfresh-${FRESH_NAME}"

        assert_offset_is_not_the_shared_stack "$PORT_OFFSET" "$FRONTEND_PORT" "$BACKEND_PORT" \
            || exit "$EXIT_MISUSE"
        assert_project_is_not_live "$FRESH_PROJECT" || exit "$EXIT_MISUSE"

        start_args=(start dev --fresh "$FRESH_NAME" --port-offset "$PORT_OFFSET"
                    --seed-benchmark --with-mock-llm)
        [[ -n "$GPU_DEVICE" ]] && start_args+=(--gpu-device "$GPU_DEVICE")

        log "${BLUE}▶ ${OPENTR} ${start_args[*]}${NC}"
        if $DRY_RUN; then
            log "${YELLOW}(dry run — nothing started)${NC}"
            return 0
        fi

        rc=0
        # Captured rather than streamed so the port-conflict message can be matched
        # in-shell. `--fresh` refuses to start on a bound port and tells the operator
        # to pick another offset; that manual step is the one people skip, so the
        # bump is done here.
        out="$("$OPENTR" "${start_args[@]}" 2>&1)" || rc=$?
        printf '%s\n' "$out" >&2

        if [[ $rc -eq 0 ]]; then
            STACK_STARTED_BY_US=true
            return 0
        fi

        if [[ "$out" == *"already bound"* && $attempt -lt $MAX_OFFSET_RETRIES ]]; then
            attempt=$((attempt + 1))
            PORT_OFFSET=$((PORT_OFFSET + 100))
            log "${YELLOW}⚠ Ports were taken — retrying at --port-offset ${PORT_OFFSET} \
(attempt ${attempt}/${MAX_OFFSET_RETRIES})${NC}"
            continue
        fi

        die "the capture stack did not start (opentr.sh exited ${rc})" "$EXIT_PRECONDITION"
    done
}

stop_capture_stack() {
    $DRY_RUN && return 0
    $STACK_STARTED_BY_US || return 0
    if $KEEP_STACK; then
        log "${YELLOW}Capture stack left running (--keep): \
${OPENTR} stop --fresh ${FRESH_NAME}${NC}"
        return 0
    fi
    # `stop --fresh` keeps volumes: the seeded corpus is what makes the NEXT
    # capture reproduce this one, so throwing it away by default would be
    # actively counterproductive. --destroy is the opt-in.
    log "${BLUE}▶ ${OPENTR} stop --fresh ${FRESH_NAME}${NC}"
    "$OPENTR" stop --fresh "$FRESH_NAME" || log "${YELLOW}⚠ stop --fresh reported an error${NC}"
    if $DESTROY_STACK; then
        log "${BLUE}▶ ${OPENTR} fresh-destroy ${FRESH_NAME}${NC}"
        "$OPENTR" fresh-destroy "$FRESH_NAME" \
            || log "${YELLOW}⚠ fresh-destroy reported an error${NC}"
    else
        log "   Volumes kept. Remove them with: ${OPENTR} fresh-destroy ${FRESH_NAME}"
    fi
}

# Wait until the seeded media has left "processing". The module docstring is
# explicit that the suite needs completed, transcribed files; capturing while a
# file is still processing records a spinner as the reference image.
wait_for_seeded_files() {
    $DRY_RUN && return 0
    log "${BLUE}⏳ Waiting for seeded media to finish processing…${NC}"
    OT_BACKEND_URL="http://localhost:${BACKEND_PORT}" "$VENV_PY" - <<'PYEOF' || return 1
import os
import sys
import time

import requests

backend = os.environ["OT_BACKEND_URL"]
deadline = time.time() + float(os.environ.get("OT_VISUAL_SEED_TIMEOUT", "1800"))
token = None
while time.time() < deadline and token is None:
    try:
        resp = requests.post(
            f"{backend}/api/auth/token",
            data={"username": "admin@example.com", "password": "password"},
            timeout=15,
        )
        if resp.status_code == 200:
            token = resp.json()["access_token"]
            break
    except requests.RequestException:
        pass
    time.sleep(5)
if token is None:
    print("could not authenticate against the capture stack", file=sys.stderr)
    sys.exit(1)

headers = {"Authorization": f"Bearer {token}"}
while time.time() < deadline:
    try:
        files = requests.get(f"{backend}/api/files", headers=headers, timeout=30).json()
    except (requests.RequestException, ValueError):
        time.sleep(5)
        continue
    if isinstance(files, dict):
        files = files.get("items") or files.get("files") or []
    busy = [f for f in files if str(f.get("status", "")).lower() in {"processing", "pending"}]
    done = [f for f in files if str(f.get("status", "")).lower() == "completed"]
    if files and not busy:
        print(f"{len(done)} completed file(s), none processing")
        sys.exit(0)
    print(f"  {len(busy)} still processing, {len(done)} completed…", flush=True)
    time.sleep(10)
print("timed out waiting for seeded media to finish processing", file=sys.stderr)
sys.exit(1)
PYEOF
}

# ---------------------------------------------------------------------------
# Capture + report
# ---------------------------------------------------------------------------

WORKDIR=""
BEFORE_DIR=""
ACCEPTED=false
#: Set the instant the capture is about to overwrite the baselines. The trap
#: restores only when this is true — a misuse exit (bad flag, bound port) must
#: not touch the tree at all.
CAPTURE_ATTEMPTED=false

# Snapshot the current baselines. A HARD precondition, not best-effort: the
# restore path below is the only thing between an interrupted run and a
# directory of unreviewed images, and a restore from a snapshot that silently
# failed to be taken is indistinguishable from "there were no baselines".
snapshot_baselines() {
    mkdir -p "$BEFORE_DIR" "$SCREENSHOT_DIR"
    local png count=0
    for png in "$SCREENSHOT_DIR"/*.png; do
        [[ -e "$png" ]] || continue
        cp -p "$png" "$BEFORE_DIR/" || die "could not snapshot ${png} — refusing to run a \
capture that overwrites baselines it cannot put back" "$EXIT_PRECONDITION"
        count=$((count + 1))
    done
    log "${BLUE}📸 Snapshotted ${count} existing baseline(s) to ${BEFORE_DIR}${NC}"
}

# Restore the previous baselines unless the operator explicitly accepted the new
# ones. UPDATE_SCREENSHOTS=1 overwrites in place, so without this an interrupted
# run (or a declined review) leaves unreviewed images on disk looking exactly
# like reviewed ones.
#
# ⚠️ Restores FILE BY FILE, never `rm -f *.png` followed by a bulk copy back.
# The blanket form is one silently-failed snapshot away from deleting every
# committed baseline, leaving `git checkout` as the only recovery — a
# destructive step whose safety depends on an earlier step having succeeded is
# the shape this repo has been burned by before (issue #693).
restore_baselines_unless_accepted() {
    local rc=$?
    if ! $ACCEPTED && $CAPTURE_ATTEMPTED && [[ -n "$BEFORE_DIR" && -d "$BEFORE_DIR" ]]; then
        log "${YELLOW}↩ Restoring the previous baselines (nothing was accepted).${NC}"
        local png name
        # Put every snapshotted image back...
        for png in "$BEFORE_DIR"/*.png; do
            [[ -e "$png" ]] || continue
            cp -p "$png" "$SCREENSHOT_DIR/" || log "${RED}   could not restore ${png}${NC}"
        done
        # ...and drop only the images THIS RUN created, which have no snapshot.
        for png in "$SCREENSHOT_DIR"/*.png; do
            [[ -e "$png" ]] || continue
            name="$(basename "$png")"
            [[ -e "$BEFORE_DIR/$name" ]] || rm -f "$png"
        done
    fi
    stop_capture_stack
    exit "$rc"
}

capture() {
    local k_expr="" surface

    for surface in "${SELECTED_SURFACES[@]}"; do
        [[ -n "$k_expr" ]] && k_expr="${k_expr} or "
        k_expr="${k_expr}${surface}"
    done

    log ""
    log "${BLUE}▶ Capturing: ${SELECTED_SURFACES[*]}${NC}"
    log "  base-url    http://localhost:${FRONTEND_PORT}"
    log "  backend-url http://localhost:${BACKEND_PORT}"
    log "  MOCK_LLM_PORT=${MOCK_LLM_PORT}"
    log "  POSTGRES_PORT=${POSTGRES_PORT} MINIO_PORT=${MINIO_PORT} OPENSEARCH_PORT=${OPENSEARCH_PORT}"

    $DRY_RUN && return 0

    # ⚠️ `--base-url`/`--backend-url` only redirect what the BROWSER talks to. Fixtures that
    # reach a backing service DIRECTLY read `conftest.py`'s env vars, which default to the
    # SHARED dev stack (`OPENSEARCH_PORT` -> 5180, `POSTGRES_PORT` -> 5176). Omitting them does
    # not fail loudly — it silently points half the run at the wrong deployment.
    #
    # Measured 2026-09-09: `file_detail` failed with "owned file <uuid> never got transcript
    # chunks indexed within 420.0s" while the capture stack's own worker log showed
    # `Indexed 1 chunks for file <that uuid> (mode: neural)` and the task succeeding. The
    # chunks existed; `_wait_for_chunks_indexed` was counting them in the shared dev stack's
    # index, where that file has never existed and never will. No timeout can fix that, and
    # raising one only buys a slower identical failure.
    #
    # This is the trap `backend/tests/CLAUDE.md` records for isolated stacks — export the
    # service ports or you hit the shared one — applied to the one tool whose entire purpose
    # is to run against an isolated stack.
    MOCK_LLM_PORT="$MOCK_LLM_PORT" \
    POSTGRES_PORT="$POSTGRES_PORT" \
    MINIO_PORT="$MINIO_PORT" \
    OPENSEARCH_PORT="$OPENSEARCH_PORT" \
    UPDATE_SCREENSHOTS=1 \
        "$VENV_PY" -m pytest "$TEST_MODULE" -v -rs \
        -k "$k_expr" \
        --base-url="http://localhost:${FRONTEND_PORT}" \
        --backend-url="http://localhost:${BACKEND_PORT}" \
        --junitxml="$WORKDIR/capture.xml"
}

# Per-surface old-vs-new percentages plus an actual/diff image per changed
# surface. `_visual_diff.diff_fraction` is IMPORTED, never re-implemented: a
# second copy of the comparison would let the report and the suite disagree
# about whether a change is a change.
report_changes() {
    $DRY_RUN && return 0
    OT_BEFORE_DIR="$BEFORE_DIR" OT_AFTER_DIR="$SCREENSHOT_DIR" OT_OUT_DIR="$WORKDIR" \
    OT_E2E_DIR="$REPO_ROOT/backend/tests/e2e" OT_SURFACES="${SELECTED_SURFACES[*]}" \
    "$VENV_PY" - <<'PYEOF'
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["OT_E2E_DIR"])

import numpy as np
from _visual_diff import CHANNEL_NOISE_THRESHOLD, DIFF_TOLERANCE, diff_fraction
from PIL import Image

before = Path(os.environ["OT_BEFORE_DIR"])
after = Path(os.environ["OT_AFTER_DIR"])
out = Path(os.environ["OT_OUT_DIR"])
surfaces = os.environ["OT_SURFACES"].split()
out.mkdir(parents=True, exist_ok=True)


def load(path: Path) -> np.ndarray:
    with Image.open(path) as img:
        return np.asarray(img.convert("RGB"), dtype=np.uint8)


def write_diff(a: np.ndarray, b: np.ndarray, path: Path) -> None:
    """Red overlay on the NEW image wherever a pixel differs beyond the noise floor."""
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    canvas = a.copy()
    delta = np.abs(a[:h, :w].astype(np.int16) - b[:h, :w].astype(np.int16))
    mask = np.any(delta > CHANNEL_NOISE_THRESHOLD, axis=-1)
    region = canvas[:h, :w]
    region[mask] = (region[mask] * 0.35).astype(np.uint8)
    region[mask, 0] = 255
    Image.fromarray(canvas).save(path)


rows = []
for name in sorted(f"{s}-{t}" for s in surfaces for t in ("light", "dark")):
    new_path = after / f"{name}.png"
    old_path = before / f"{name}.png"
    if not new_path.exists():
        rows.append((name, "NOT CAPTURED", "", ""))
        continue
    if not old_path.exists():
        rows.append((name, "NEW", "", str(new_path)))
        continue
    new_img, old_img = load(new_path), load(old_path)
    fraction = diff_fraction(new_img, old_img)
    if fraction == 0.0:
        rows.append((name, "identical", "0.0000%", ""))
        continue
    diff_path = out / f"{name}.diff.png"
    write_diff(new_img, old_img, diff_path)
    (out / f"{name}.new.png").write_bytes(new_path.read_bytes())
    (out / f"{name}.old.png").write_bytes(old_path.read_bytes())
    status = "CHANGED" if fraction > DIFF_TOLERANCE else "changed (within tolerance)"
    rows.append((name, status, f"{fraction:.4%}", str(diff_path)))

width = max(len(r[0]) for r in rows) if rows else 20
print()
print(f"{'surface':<{width}}  {'status':<28}  {'differs':>10}")
print("-" * (width + 44))
for name, status, pct, _ in rows:
    print(f"{name:<{width}}  {status:<28}  {pct:>10}")
print()
paths = [r[3] for r in rows if r[3]]
if paths:
    print("Images written for review (old / new / diff side by side):")
    print(f"  {out}")
    for p in paths:
        print(f"    {p}")
else:
    print("No pixels changed on any selected surface.")
print()
(out / "report.tsv").write_text(
    "\n".join("\t".join(r[:3]) for r in rows) + "\n", encoding="utf-8"
)
PYEOF
}

confirm() {
    $DRY_RUN && return 1
    if $ASSUME_YES; then
        log "${YELLOW}--yes: accepting without an interactive review.${NC}"
        return 0
    fi
    if [[ ! -t 0 ]]; then
        log "${RED}❌ No tty to read a confirmation from (backgrounded or piped shell).${NC}"
        log "   Review the images above, then re-run with --yes, or run interactively."
        return 1
    fi
    log "${YELLOW}Look at the images listed above before answering.${NC}"
    log -n "Accept these baselines? Type ACCEPT to confirm: "
    local answer=""
    read -r answer
    [[ "$answer" == "ACCEPT" ]]
}

# ---------------------------------------------------------------------------
# Provenance: WHICH CODE produced this image, not only why it changed.
# ---------------------------------------------------------------------------
GIT_SHA=""
GIT_BRANCH=""
GIT_DIRTY="false"
GIT_DIRTY_COUNT=0

resolve_git_provenance() {
    local porcelain
    GIT_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
    GIT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    porcelain="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null || true)"
    if [[ -n "$porcelain" ]]; then
        GIT_DIRTY="true"
        GIT_DIRTY_COUNT="$(printf '%s\n' "$porcelain" | grep -c '' || true)"
    fi
}

# A dirty tree is WARNED about and recorded, not refused.
#
# Refusing would be wrong: the normal shape of this work is "change the UI, look
# at it, recapture" and demanding a commit first only encourages a throwaway one.
# But a baseline captured from uncommitted source is not reproducible by anyone
# else — the sha in its sidecar does not describe the pixels — so it must never
# be silently indistinguishable from a clean capture. Hence: loud here, and
# `git_dirty: true` in every sidecar the run writes.
warn_if_dirty() {
    $GIT_DIRTY || return 0
    log ""
    log "${YELLOW}⚠ The working tree is DIRTY (${GIT_DIRTY_COUNT} changed path(s)).${NC}"
    log "${YELLOW}  Baselines captured now cannot be reproduced from ${GIT_SHA:0:12} by anyone"
    log "  else, because the pixels come partly from uncommitted work. This will be"
    log "  recorded as \"git_dirty\": true in each sidecar. Commit first if you want a"
    log "  baseline someone else can regenerate.${NC}"
    log ""
}

# One JSON sidecar per captured baseline. See the header for why per-surface and
# why not inside __screenshots__/.
record_provenance() {
    local surface theme name status pct captured_at row_name row_status row_pct
    captured_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    mkdir -p "$PROVENANCE_DIR"

    for surface in "${SELECTED_SURFACES[@]}"; do
        for theme in light dark; do
            name="${surface}-${theme}"
            [[ -f "$SCREENSHOT_DIR/${name}.png" ]] || continue
            status=""
            pct=""
            if [[ -f "$WORKDIR/report.tsv" ]]; then
                while IFS=$'\t' read -r row_name row_status row_pct; do
                    if [[ "$row_name" == "$name" ]]; then
                        status="$row_status"
                        pct="$row_pct"
                    fi
                done < "$WORKDIR/report.tsv"
            fi
            cat > "$PROVENANCE_DIR/${name}.json" <<EOF
{
  "baseline": "__screenshots__/${name}.png",
  "captured_at": "${captured_at}",
  "git_sha": "${GIT_SHA}",
  "git_branch": "${GIT_BRANCH}",
  "git_dirty": ${GIT_DIRTY},
  "git_dirty_paths": ${GIT_DIRTY_COUNT},
  "reason": $(json_string "$REASON"),
  "captured_by": "scripts/e2e/update-visual-baselines.sh",
  "stack": {
    "compose_project": "otfresh-${FRESH_NAME}",
    "port_offset": ${PORT_OFFSET},
    "gpu_device": $([[ -n "$GPU_DEVICE" ]] && echo "$GPU_DEVICE" || echo 'null'),
    "seeded": true,
    "mode": "dev (local source bind-mounted)"
  },
  "change_from_previous": {
    "status": $(json_string "${status:-unknown}"),
    "differing_pixels": $(json_string "${pct:-}")
  }
}
EOF
        done
    done
    log "${GREEN}✔ Provenance written to ${PROVENANCE_DIR#"$REPO_ROOT"/}/${NC}"
    $GIT_DIRTY && log "${YELLOW}  …recorded as captured from a DIRTY tree.${NC}"
    return 0
}

# Minimal JSON string escaping for operator-supplied text (--reason).
json_string() {
    local s="${1//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/ }"
    s="${s//$'\t'/ }"
    printf '"%s"' "$s"
}

# ---------------------------------------------------------------------------
main() {
    parse_args "$@"

    # shellcheck source=scripts/lib/compose-project.sh
    source "$REPO_ROOT/scripts/lib/compose-project.sh"

    derive_surfaces
    derive_shared_ports
    derive_base_ports
    validate_args
    check_preconditions
    resolve_git_provenance
    warn_if_dirty

    cd "$REPO_ROOT"

    WORKDIR="${OT_VISUAL_WORKDIR:-/tmp/ot-visual-baselines/$(date +%Y%m%d-%H%M%S)}"
    BEFORE_DIR="$WORKDIR/baseline-before"
    snapshot_baselines

    trap restore_baselines_unless_accepted EXIT

    start_capture_stack

    if $DRY_RUN; then
        log "${GREEN}Dry run: declared-intent checks passed (offset ${PORT_OFFSET}, project \
otfresh-${FRESH_NAME}).${NC}"
        log "  Not evaluated, because they are OBSERVATIONS of a running stack:"
        log "    · which compose project owns ports ${FRONTEND_PORT}/${BACKEND_PORT}"
        log "    · whether that stack serves local source or a baked image"
        ACCEPTED=true   # nothing was overwritten, so nothing needs restoring
        exit 0
    fi

    # The two OBSERVATIONS. Everything above is a statement of intent; only these
    # can catch a stack that is up but is not the one we think it is.
    assert_ports_are_owned_by_the_capture_stack \
        "$FRESH_PROJECT" "$FRONTEND_PORT" "$BACKEND_PORT" || exit "$EXIT_PRECONDITION"
    assert_stack_serves_local_code "$FRESH_PROJECT" || exit "$EXIT_PRECONDITION"

    wait_for_seeded_files || die "seeded media never finished processing — capturing now \
would record a spinner as the reference image" "$EXIT_PRECONDITION"

    # Set BEFORE the capture, not after: the failure mode the trap exists for is
    # the run dying part way through, with some baselines already overwritten.
    CAPTURE_ATTEMPTED=true
    capture || die "the capture run failed; the previous baselines are being restored"
    report_changes

    if confirm; then
        ACCEPTED=true
        record_provenance
        log "${GREEN}✔ Baselines updated. Commit ${SCREENSHOT_DIR#"$REPO_ROOT"/} and \
${PROVENANCE_DIR#"$REPO_ROOT"/} TOGETHER — an image without its sidecar is a baseline \
nobody can attribute to a commit.${NC}"
        log "  Verify by re-running WITHOUT update mode:"
        log "    ${VENV_PY#"$REPO_ROOT"/} -m pytest ${TEST_MODULE#"$REPO_ROOT"/} \\"
        log "      --base-url=http://localhost:${FRONTEND_PORT} \\"
        log "      --backend-url=http://localhost:${BACKEND_PORT}   # needs --keep"
        exit 0
    fi

    log "${YELLOW}Declined — the previous baselines are being restored.${NC}"
    exit "$EXIT_ABORT"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
