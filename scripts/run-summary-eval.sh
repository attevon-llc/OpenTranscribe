#!/bin/bash
# Measure the multi-slice summary scope and merge rules on a real local model: the quality gate
# for the rewrite of SECTION_SCOPE_RULES / SECTION_MERGE_RULES.
# Design and the reasons for every step: docs/design/summary_quality_eval_plan.md.
#
#   scripts/run-summary-eval.sh --fresh sumeval \
#     --arm old=@module --arm new=~/ot-cleanup-work/summary-eval/arms/new.json \
#     [--inputs gate|dev|smoke] [--runs 5] [--judge haiku|none] [--keep-stack] \
#     [--llm-base-url URL]      # a vLLM is already up: skip stack and GPU management
#     [--port-offset N] [--concurrency N] [--budget-hours 4] [--resume-dir DIR] [--remote-leg] [--dry-run]
#
# --remote-leg adds Q2: one I1 summary per arm through the Anthropic dialect (Haiku 4.5, which still
# accepts the assistant prefill and temperature the app sends), as an output-shape check. It needs
# $ANTHROPIC_API_KEY (used by name, never printed), sends public Tier A corpus text only, and is
# reported as a wiring check, not a verdict.
#
# In order, stopping at the first failure:
#   1. GPU check     GPU ${LLM_TEST_GPU_DEVICE_ID:-2} must be idle (exit 4 = NOT MEASURED);
#                    GPU 0 is refused outright. Nothing is ever killed.
#   2. Stack         ./opentr.sh start dev --fresh NAME --with-llm-test, then wait for /v1/models
#   3. Smoke         one I1 summary per arm; projects the full matrix and refuses over budget
#   4. The run       the slice lock (planned once, verified every run), run, replay-merge, judge, report
#   5. Teardown      (EXIT trap) diagnostics first, then ./opentr.sh stop --fresh NAME
#
# Exit codes: 0 pass, 1 failed, 2 misuse, 3 precondition unmet, 4 NOT MEASURED.
# Arm text never reaches this script's output: the arms are JSON files outside the repo.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${SUMMARY_EVAL_PY:-$REPO_ROOT/backend/venv/bin/python}"
BENCH="$REPO_ROOT/scripts/benchmark_summary.py"
WORK_DIR="${SUMMARY_EVAL_WORK_DIR:-$HOME/ot-cleanup-work/summary-eval}"

EXIT_MISUSE=2
EXIT_PRECONDITION=3
EXIT_NOT_MEASURED=4

FRESH_NAME=""
PORT_OFFSET=""
INPUTS="gate"
RUNS=5
JUDGE="haiku"
KEEP_STACK=false
EXTERNAL_URL=""
BUDGET_HOURS=4
RESUME_DIR=""
CONCURRENCY=1
REMOTE_LEG=false
DRY_RUN=false
declare -a ARMS=()

usage() { sed -n '2,/^# Arm text never/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fresh)          FRESH_NAME="${2:?--fresh needs a name}"; shift ;;
        --port-offset)    PORT_OFFSET="${2:?--port-offset needs a number}"; shift ;;
        --arm)            ARMS+=("${2:?--arm needs NAME=SPEC}"); shift ;;
        --inputs)         INPUTS="${2:?}"; shift ;;
        --runs)           RUNS="${2:?}"; shift ;;
        --judge)          JUDGE="${2:?}"; shift ;;
        --keep-stack)     KEEP_STACK=true ;;
        --llm-base-url)   EXTERNAL_URL="${2:?}"; shift ;;
        --budget-hours)   BUDGET_HOURS="${2:?}"; shift ;;
        --concurrency)    CONCURRENCY="${2:?--concurrency needs a number}"; shift ;;
        --resume-dir)     RESUME_DIR="${2:?}"; shift ;;
        --remote-leg)     REMOTE_LEG=true ;;
        --dry-run)        DRY_RUN=true ;;
        -h|--help)        usage; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; usage >&2; exit "$EXIT_MISUSE" ;;
    esac
    shift
done

case "$INPUTS" in gate|dev|smoke) ;; *) echo "error: --inputs must be gate, dev or smoke" >&2; exit "$EXIT_MISUSE" ;; esac
[[ "$CONCURRENCY" =~ ^[1-9][0-9]*$ ]] || { echo "error: --concurrency must be a positive integer" >&2; exit "$EXIT_MISUSE"; }
case "$JUDGE" in haiku|none) ;; *) echo "error: --judge must be haiku or none" >&2; exit "$EXIT_MISUSE" ;; esac
if [[ ${#ARMS[@]} -lt 2 ]]; then
    echo "error: --arm old=... and --arm new=... are both required" >&2
    exit "$EXIT_MISUSE"
fi
if [[ -z "$EXTERNAL_URL" && -z "$FRESH_NAME" ]]; then
    echo "error: pass --fresh NAME (an isolated stack) or --llm-base-url URL (a vLLM already up)." >&2
    echo "       Never develop against the live data stack: see root CLAUDE.md, Fresh deployments." >&2
    exit "$EXIT_MISUSE"
fi

GPU_ID="${LLM_TEST_GPU_DEVICE_ID:-2}"
BASE_PORT="${LLM_TEST_PORT:-5195}"
LLM_PORT=$(( BASE_PORT + ${PORT_OFFSET:-0} ))
LLM_URL="${EXTERNAL_URL:-http://localhost:$LLM_PORT/v1}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${RESUME_DIR:-$WORK_DIR/runs/$STAMP-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)}"
LOCK_DIR="$WORK_DIR/lock/$( [[ "$INPUTS" == smoke ]] && echo gate || echo "$INPUTS" )"
SMOKE_DIR="$RUN_DIR/smoke"

ARM_ARGS=()
for a in "${ARMS[@]}"; do ARM_ARGS+=(--arm "$a"); done
OPENTR_PORT_ARGS=()
[[ -n "$PORT_OFFSET" ]] && OPENTR_PORT_ARGS=(--port-offset "$PORT_OFFSET")

STACK_STARTED_BY_US=false
STEP=0

say() { echo "==> $*"; }
# Every command goes through run(): under --dry-run it is printed and not executed.
run() {
    if $DRY_RUN; then
        printf '    $'; printf ' %q' "$@"; printf '\n'
        return 0
    fi
    "$@"
}
step() { STEP=$((STEP + 1)); echo; say "step $STEP: $*"; }

# ------------------------------------------------------------------------------ teardown
teardown() {
    local rc=$?
    $DRY_RUN && return
    if [[ -d "$RUN_DIR" ]]; then
        # Diagnostics BEFORE teardown: a stopped container's logs are the first thing lost.
        mkdir -p "$RUN_DIR/diagnostics"
        if $STACK_STARTED_BY_US || [[ -n "$FRESH_NAME" ]]; then
            local c
            c="$(docker ps -a --filter "label=com.docker.compose.project=otfresh-$FRESH_NAME" \
                 --filter "name=llm-test-vllm" --format '{{.Names}}' 2>/dev/null | head -1)"
            [[ -n "$c" ]] && docker logs --tail 200 "$c" >"$RUN_DIR/diagnostics/vllm.tail" 2>&1
        fi
        [[ -f "$RUN_DIR/records.jsonl" ]] && tail -n 3 "$RUN_DIR/records.jsonl" \
            | "$PY" -c 'import json,sys
for l in sys.stdin:
    r=json.loads(l); print(r["cell_id"], r["status"], r["void_reason"], r["error"])' \
            >"$RUN_DIR/diagnostics/last_records.txt" 2>&1
        say "diagnostics written to $RUN_DIR/diagnostics (exit $rc)"
    fi
    if $STACK_STARTED_BY_US && ! $KEEP_STACK; then
        say "stopping the fresh stack (volumes kept; ./opentr.sh fresh-destroy $FRESH_NAME removes them)"
        "$REPO_ROOT/opentr.sh" stop --fresh "$FRESH_NAME" || true
    fi
}
trap teardown EXIT

# ------------------------------------------------------------------------------ 1. GPU check
gpu_check() {
    if [[ -n "$EXTERNAL_URL" ]]; then
        say "GPU check skipped: --llm-base-url given, the server is not managed here"
        return 0
    fi
    if [[ "$GPU_ID" == "0" ]]; then
        echo "error: LLM_TEST_GPU_DEVICE_ID=0 resolves to GPU 0, which runs unrelated work and is never used" >&2
        return "$EXIT_MISUSE"
    fi
    if $DRY_RUN; then
        printf '    $ nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader   (read-only; run for the dry-run too)\n'
    fi
    local row used util apps
    row="$(nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader,nounits 2>/dev/null \
           | awk -F', *' -v id="$GPU_ID" '$1 == id')"
    if [[ -z "$row" ]]; then
        echo "error: nvidia-smi reports no GPU $GPU_ID" >&2
        return "$EXIT_PRECONDITION"
    fi
    used="$(echo "$row" | awk -F', *' '{print $2}')"
    util="$(echo "$row" | awk -F', *' '{print $3}')"
    apps="$(nvidia-smi --query-compute-apps=gpu_bus_id,pid --format=csv,noheader 2>/dev/null | wc -l)"
    say "GPU $GPU_ID: ${used} MiB used, ${util}% utilisation (${apps} compute process(es) on the host)"
    if [[ "$used" -gt 1024 ]]; then
        echo "NOT MEASURED: GPU $GPU_ID has ${used} MiB in use (limit 1024). Wait for it to be idle;" >&2
        echo "nothing is killed to make room." >&2
        return "$EXIT_NOT_MEASURED"
    fi
    return 0
}

# ------------------------------------------------------------------------------ 2. stack
# The image the llm-test overlay serves, tag resolved the way compose resolves it, for provenance.
vllm_image() {
    local compose="$REPO_ROOT/docker-compose.llm-test.yml" ref default
    ref="$(grep -m1 'image:.*vllm' "$compose" 2>/dev/null | awk '{print $2}')"
    default="$(echo "$ref" | sed -n 's/.*LLM_TEST_VLLM_TAG:-\([^}]*\)}.*/\1/p')"
    echo "${ref%%:*}:${LLM_TEST_VLLM_TAG:-${default:-unknown}}"
}

vllm_up() { curl -sf "$LLM_URL/models" >/dev/null 2>&1; }

start_stack() {
    if [[ -n "$EXTERNAL_URL" ]]; then
        if $DRY_RUN; then echo "    (would require a vLLM answering at $LLM_URL)"; return 0; fi
        vllm_up || { echo "error: no vLLM answering at $LLM_URL" >&2; return "$EXIT_PRECONDITION"; }
        say "using the vLLM at $LLM_URL (not managed by this script)"
        return 0
    fi
    if vllm_up; then
        say "a vLLM already answers at $LLM_URL: leaving the stack alone"
        return 0
    fi
    STACK_STARTED_BY_US=true
    run "$REPO_ROOT/opentr.sh" start dev --fresh "$FRESH_NAME" ${OPENTR_PORT_ARGS[@]+"${OPENTR_PORT_ARGS[@]}"} --with-llm-test || return "$EXIT_PRECONDITION"
    if $DRY_RUN; then echo "    (then waits up to 15 min for $LLM_URL/models)"; return 0; fi
    local deadline=$(( $(date +%s) + 900 ))
    until vllm_up; do
        if [[ $(date +%s) -ge $deadline ]]; then
            echo "error: the vLLM did not answer within 15 min at $LLM_URL" >&2
            return "$EXIT_PRECONDITION"
        fi
        sleep 5
    done
    say "vLLM is up"
}

# ------------------------------------------------------------------------------ 3/4. measure
bench() { run "$PY" "$BENCH" "$@"; }

ensure_lock() {
    if [[ -f "$LOCK_DIR/inputs.lock.json" ]]; then
        say "using the existing slice lock $LOCK_DIR/inputs.lock.json (run verifies every chunk hash against it)"
        return 0
    fi
    bench plan-slices --inputs "$( [[ "$INPUTS" == smoke ]] && echo gate || echo "$INPUTS" )" --out "$LOCK_DIR"
}

smoke() {
    # A resumed run already passed its smoke and budget projection; re-running it would refuse on
    # the records it left, so the resume would never reach the cells it exists to finish.
    if [[ -n "$RESUME_DIR" && -s "$SMOKE_DIR/records.jsonl" ]]; then
        say "resuming: the smoke in $SMOKE_DIR already ran, not repeating it"
        return 0
    fi
    local image
    image="$(vllm_image)"
    bench run --inputs smoke --runs 1 --no-controls "${ARM_ARGS[@]}" --lock "$LOCK_DIR" \
        --llm-base-url "$LLM_URL" --vllm-image "${image:-unknown}" --run-dir "$SMOKE_DIR" || return $?
    bench project --run-dir "$SMOKE_DIR" --runs "$RUNS" --budget-hours "$BUDGET_HOURS"
}

measure() {
    local resume=() noctl=() strict=()
    if [[ "$INPUTS" == smoke ]]; then
        # A wiring check: no control arms, no replay or judge, and an error cell is a failure.
        noctl=(--no-controls); strict=(--fail-on-error)
    fi
    [[ -n "$RESUME_DIR" ]] && resume=(--resume)
    local image
    image="$(vllm_image)"
    bench run --inputs "$INPUTS" --runs "$RUNS" "${ARM_ARGS[@]}" --lock "$LOCK_DIR" \
        --llm-base-url "$LLM_URL" --vllm-image "${image:-unknown}" --run-dir "$RUN_DIR" --concurrency "$CONCURRENCY" \
        ${noctl[@]+"${noctl[@]}"} ${resume[@]+"${resume[@]}"} || return $?
    if [[ "$INPUTS" != smoke ]]; then
        bench replay-merge --inputs "$INPUTS" "${ARM_ARGS[@]}" --lock "$LOCK_DIR" --llm-base-url "$LLM_URL" --run-dir "$RUN_DIR" || return $?
        bench judge --inputs "$INPUTS" --judge "$JUDGE" --lock "$LOCK_DIR" --run-dir "$RUN_DIR" || return $?
    fi
    bench report --inputs "$INPUTS" --lock "$LOCK_DIR" --run-dir "$RUN_DIR" \
        ${noctl[@]+"${noctl[@]}"} ${strict[@]+"${strict[@]}"}
}

remote_leg() {
    local dir="$RUN_DIR/remote-haiku"
    if ! $DRY_RUN && [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
        echo "error: --remote-leg needs ANTHROPIC_API_KEY in the environment" >&2
        return "$EXIT_PRECONDITION"
    fi
    bench run --inputs smoke --runs 1 --no-controls "${ARM_ARGS[@]}" --lock "$LOCK_DIR" \
        --provider anthropic --model claude-haiku-4-5 --run-dir "$dir" || return $?
    bench report --inputs smoke --lock "$LOCK_DIR" --run-dir "$dir" --no-controls --fail-on-error
}

# ------------------------------------------------------------------------------ main
echo "summary quality evaluation: inputs=$INPUTS runs=$RUNS judge=$JUDGE model server=$LLM_URL"
echo "run dir: $RUN_DIR   lock: $LOCK_DIR"
$DRY_RUN && echo "(--dry-run: every command below is printed, none is executed)"

step "GPU check"
gpu_check; rc=$?
if [[ $rc -eq $EXIT_MISUSE ]]; then
    exit "$rc"  # a configuration error, not a state of the host: refused even for a dry-run
elif [[ $rc -ne 0 ]]; then
    $DRY_RUN && echo "    (dry-run: a real run would stop here with exit $rc)" || exit "$rc"
fi

step "stack"
start_stack || exit $?

step "slice lock (CPU only)"
ensure_lock || exit $?

if [[ "$INPUTS" != smoke ]]; then
    step "smoke: one I1 summary per arm, then project the matrix against the ${BUDGET_HOURS} h budget"
    smoke || exit $?
fi

step "the run: run, replay-merge, judge, report"
measure || exit $?

if $REMOTE_LEG; then
    step "remote leg: one I1 summary per arm through the Anthropic dialect (Haiku 4.5)"
    remote_leg || exit $?
fi

echo
say "done: $RUN_DIR/metrics.md   (verdict is in $RUN_DIR/metrics.json)"
exit 0
