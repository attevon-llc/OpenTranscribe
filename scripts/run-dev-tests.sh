#!/bin/bash
#
# scripts/run-dev-tests.sh — chained local dev-cycle test runner, one command, one report.
#
# NOT the same job as scripts/test-matrix.sh. This is the quick "does my current branch
# work" check (backend suite + e2e + frontend, current dev stack, minutes not hours).
# test-matrix.sh is the exhaustive deployment-mode REHEARSAL (dev/prod/lite/PKI/GPU-scale/
# fresh-install/upgrade, stages 1-4, up to hours) — run that before cutting a release, run
# this one constantly during normal development.
#
# THIS SCRIPT OWNS NO TEST LOGIC OF ITS OWN — same convention as scripts/test-matrix.sh.
# Every phase wraps an existing script:
#   backend  -> scripts/run-integration-tests.sh
#   e2e      -> scripts/e2e/run-e2e.sh (or run-e2e-smoke.sh for --fast)
#   frontend -> scripts/frontend-check.sh --no-claude --check-only
#
# If a phase needs new behaviour, the behaviour belongs in the wrapped script, not here.
#
# Usage:
#   scripts/run-dev-tests.sh --full                    # backend gate + full e2e + frontend
#   scripts/run-dev-tests.sh --fast                     # backend gate (--e2e-smoke) + frontend
#   scripts/run-dev-tests.sh --backend-only
#   scripts/run-dev-tests.sh --e2e-only
#   scripts/run-dev-tests.sh --frontend-only
#   scripts/run-dev-tests.sh --full --all-overlays      # + watch + mock-asr overlays too
#   scripts/run-dev-tests.sh --full --with-gpu-scale    # + multi-GPU worker topology (auto-skips
#                                                        #   on a single-project-GPU host)
#   scripts/run-dev-tests.sh --full --no-overlays       # assume the stack is already configured
#   scripts/run-dev-tests.sh --full --list-overlays     # print the resolved overlay set, start nothing
#   scripts/run-dev-tests.sh --full --dry-run           # + the exact opentr.sh command, start nothing
#   scripts/run-dev-tests.sh --with-gpu-diarization     # + the 3 container-only GPU diarization
#                                                        #   suites (run-diarization-gpu-tests.sh);
#                                                        #   builds a test image, several minutes
#   scripts/run-dev-tests.sh --with-mutation-tests      # + a single-module mutation-testing run
#                                                        #   (default: spans, ~1-3 min; override
#                                                        #   with MUTATION_TEST_MODULE=<module>)
#   scripts/run-dev-tests.sh --with-pipeline-smoke      # + the real upload->ASR/diarize->
#                                                        #   search->chat live smoke test, against
#                                                        #   a real local LLM (--with-llm-test);
#                                                        #   several minutes, needs a visible GPU
#
# Mode flags (--full/--fast/--backend-only/--e2e-only/--frontend-only) are composable — pass more
# than one to union their phases. --fast additionally selects the e2e-smoke subset unless
# --e2e-only is also given, in which case the full e2e suite runs. --with-gpu-diarization,
# --with-mutation-tests, and --with-pipeline-smoke are STRICT opt-in: never included by
# --full/--fast, and each also counts as a phase selector on its own (so a bare
# `--with-pipeline-smoke` is a valid invocation).
#
# Requires: ./opentr.sh start dev (live stack up) for any phase but --frontend-only.
#
# Exit codes (matches scripts/release.sh / scripts/test-matrix.sh):
#   0 pass · 1 gate failed · 2 misuse · 3 precondition unmet

# shellcheck disable=SC2034
# VENV_PY, AUTH_CONFIG_CLI, and ALL_OVERLAYS below are consumed by scripts/lib/dev-test-overlays.sh,
# sourced further down -- the pre-commit shellcheck hook runs without -x, so it never follows the
# `source` line to see the real usage despite the `# shellcheck source=` directive there (that
# directive only helps a manual `shellcheck -x` run / editor tooling, not this hook's fixed args).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 1

EXIT_GATE=1
EXIT_MISUSE=2
EXIT_PRECONDITION=3

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

REPORT_DIR="$(mktemp -d /tmp/ot-run-dev-tests.XXXXXX)"
mkdir -p "$REPORT_DIR"

VENV_PY="$REPO_ROOT/backend/venv/bin/python"
AUTH_CONFIG_CLI="$REPO_ROOT/scripts/dev-test-auth-config.py"

RUN_BACKEND=false
RUN_E2E=false
RUN_FRONTEND=false
E2E_SMOKE=false
E2E_ONLY_EXPLICIT=false
ALL_OVERLAYS=false
WITH_GPU_SCALE=false
NO_OVERLAYS=false
LIST_OVERLAYS=false
DRY_RUN=false
WITH_GPU_DIARIZATION=false
WITH_MUTATION_TESTS=false
MUTATION_TEST_MODULE="${MUTATION_TEST_MODULE:-spans}"
WITH_PIPELINE_SMOKE=false
# Passed straight through to run-integration-tests.sh. It exists so the full-test-matrix
# Cycle 2A leg can be run by ONE command that matches the doc's leg 1 exactly
# (`run-integration-tests.sh --coverage --search-quality --cleanup`) instead of the matrix
# calling run-integration-tests.sh separately and losing this script's overlay orchestration.
SEARCH_QUALITY=false

usage() {
    cat <<'EOF'
Usage:
  scripts/run-dev-tests.sh --full           backend gate + full e2e + frontend check
  scripts/run-dev-tests.sh --fast           backend gate (+e2e-smoke) + frontend check
  scripts/run-dev-tests.sh --backend-only   just scripts/run-integration-tests.sh
  scripts/run-dev-tests.sh --e2e-only       just the full e2e suite
  scripts/run-dev-tests.sh --frontend-only  just the frontend check

Mode flags are composable (pass more than one to union their phases).

Overlay flags:
  --all-overlays     also bring up --with-watch / --with-mock-asr (needed for full
                      coverage but not for a bare --full)
  --with-gpu-scale    exercise the --gpu-scale multi-GPU worker topology; auto-skips
                      with a clear message when this project has fewer than 2 GPUs
                      configured (never auto-started under any other flag)
  --search-quality    add run-integration-tests.sh's --search-quality phase to the backend
                      gate (self-seeding 6-meeting corpus; several extra minutes). This is
                      what full-test-matrix.md's Cycle 2A leg 1 asks for, so the matrix can
                      run that leg through this script and keep its overlay orchestration
  --no-overlays       escape hatch: assume the stack is already configured as desired,
                      skip all overlay auto-detection/starting/DB reconciliation
  --list-overlays     print the resolved overlay set and exit, start nothing
  --dry-run           print the resolved overlay set + the exact opentr.sh command that
                      would run, start nothing

Strict opt-in phases (never included by --full/--fast; each also counts as a phase
selector on its own):
  --with-gpu-diarization  the 3 container-only GPU diarization suites
                          (run-diarization-gpu-tests.sh) — builds a dedicated test
                          image, several minutes, needs a visible GPU + the
                          gitignored benchmark/test_audio/*.wav fixtures
  --with-mutation-tests   a single-module mutation-testing run (run-mutation-tests.sh
                          --module), default module "spans" (~1-3 min); override with
                          MUTATION_TEST_MODULE=<module>. Never --all (hours) through
                          this flag — run scripts/run-mutation-tests.sh --all by hand
                          for that.
  --with-pipeline-smoke   the ONLY test that pushes a real fixture through upload ->
                          real WhisperX/diarization -> search -> a real local LLM
                          chat answer, start to finish, and asserts on the result
                          (tests/e2e/test_full_pipeline_smoke.py). Brings up
                          --with-llm-test itself if not already running (a real
                          GPU-backed model on LLM_TEST_GPU_DEVICE_ID, default GPU 2
                          — several minutes to become healthy the first time it
                          needs to download) and stops it again on exit if this run
                          was the one that started it (a container --with-llm-test
                          was already running before this run is left alone).

Report and per-phase logs are written to a fresh temp dir, printed at the end.
Requires the live dev stack up (./opentr.sh start dev) for any phase but
--frontend-only.
EOF
}

if [[ $# -eq 0 ]]; then
    usage
    exit "$EXIT_MISUSE"
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --full)          RUN_BACKEND=true; RUN_E2E=true; RUN_FRONTEND=true ;;
        --fast)          RUN_BACKEND=true; RUN_FRONTEND=true; E2E_SMOKE=true ;;
        --backend-only)  RUN_BACKEND=true ;;
        --e2e-only)      RUN_E2E=true; E2E_ONLY_EXPLICIT=true ;;
        --frontend-only) RUN_FRONTEND=true ;;
        --all-overlays)  ALL_OVERLAYS=true ;;
        --with-gpu-scale) WITH_GPU_SCALE=true ;;
        --search-quality) SEARCH_QUALITY=true ;;
        --no-overlays)   NO_OVERLAYS=true ;;
        --list-overlays) LIST_OVERLAYS=true ;;
        --dry-run)       DRY_RUN=true ;;
        --with-gpu-diarization) WITH_GPU_DIARIZATION=true ;;
        --with-mutation-tests)  WITH_MUTATION_TESTS=true ;;
        --with-pipeline-smoke)  WITH_PIPELINE_SMOKE=true ;;
        -h|--help)       usage; exit 0 ;;
        *) echo -e "${RED}error:${NC} unknown option: $1" >&2; usage; exit "$EXIT_MISUSE" ;;
    esac
    shift
done

# --e2e-only always wants the full suite, even under --fast's smoke default.
if $E2E_ONLY_EXPLICIT; then
    E2E_SMOKE=false
fi

if ! $RUN_BACKEND && ! $RUN_E2E && ! $RUN_FRONTEND && ! $WITH_GPU_DIARIZATION && ! $WITH_MUTATION_TESTS && ! $WITH_PIPELINE_SMOKE; then
    if $LIST_OVERLAYS || $DRY_RUN; then
        echo -e "${YELLOW}==>${NC} no phase flag given — resolving overlays as if --full were passed"
        RUN_BACKEND=true; RUN_E2E=true; RUN_FRONTEND=true
    else
        echo -e "${RED}error:${NC} no phase selected" >&2
        usage
        exit "$EXIT_MISUSE"
    fi
fi


# shellcheck source=scripts/lib/dev-test-overlays.sh
source "$REPO_ROOT/scripts/lib/dev-test-overlays.sh"


# ---------------------------------------------------------------------------- --list-overlays
if $LIST_OVERLAYS || $DRY_RUN; then
    resolve_needed_overlays
    if ! $NO_OVERLAYS && [[ ${#OVERLAYS_NEEDED[@]} -gt 0 ]]; then
        detect_overlay_state
    fi
    print_overlay_plan
    if $WITH_GPU_SCALE; then
        echo ""
        echo "  --with-gpu-scale: project GPU count = $(project_gpu_count)" \
             "(GPU_DEVICE_ID vs GPU_SCALE_DEVICE_ID distinct values in .env)"
    fi
    if $WITH_GPU_DIARIZATION; then
        echo ""
        echo "  --with-gpu-diarization: would run scripts/run-diarization-gpu-tests.sh"
    fi
    if $WITH_MUTATION_TESTS; then
        echo ""
        echo "  --with-mutation-tests: would run scripts/run-mutation-tests.sh --module $MUTATION_TEST_MODULE"
    fi
    if $WITH_PIPELINE_SMOKE; then
        echo ""
        echo "  --with-pipeline-smoke: would ensure --with-llm-test is up, then run" \
             "RUN_PIPELINE_SMOKE=1 against tests/e2e/test_full_pipeline_smoke.py"
    fi
    echo ""
    echo "(--list-overlays/--dry-run: nothing started)"
    exit 0
fi

# --------------------------------------------------------------------------- preconditions
resolve_needed_overlays

if [[ "$RUN_BACKEND" == "true" || "$RUN_E2E" == "true" || "$WITH_GPU_DIARIZATION" == "true" || "$WITH_MUTATION_TESTS" == "true" || "$WITH_PIPELINE_SMOKE" == "true" ]]; then
    if ! curl -sf http://localhost:5174/health >/dev/null 2>&1; then
        echo -e "${RED}error:${NC} dev backend not reachable at :5174 — run ./opentr.sh start dev first" >&2
        exit "$EXIT_PRECONDITION"
    fi

    # Queue-liveness preflight (issue #630 / B6): the backend HTTP health check above proves the
    # web process answers — it does NOT prove the cpu queue is being consumed. A wedged prefork
    # worker leaves /health green for hours while every task dispatched to the cpu queue sits
    # forever; that produced a 51-minute run of confusing test failures with no clear cause.
    # Cheapest honest check: dispatch the existing, already-lightweight system.update_gpu_stats
    # task (backend/app/tasks/utility.py, routed to the "cpu" queue, already fired every 5
    # minutes by celery beat) and require it completes within a few seconds, not the 300s
    # file-processing timeout — this must fail FAST, not eventually.
    CPU_WORKER_CONTAINER="$(overlay_container_name celery-cpu-worker)"
    if [[ -z "$CPU_WORKER_CONTAINER" ]]; then
        echo -e "${RED}error:${NC} celery-cpu-worker container not found/running — run ./opentr.sh start dev first" >&2
        exit "$EXIT_PRECONDITION"
    fi
    echo -e "${YELLOW}==>${NC} checking the cpu queue is actually being consumed (not just backend HTTP up)"
    if ! docker exec "$CPU_WORKER_CONTAINER" python -c '
import sys
import time

from app.core.celery import celery_app

r = celery_app.send_task("system.update_gpu_stats", queue="cpu")
for _ in range(20):
    if r.ready():
        sys.exit(0 if r.successful() else 1)
    time.sleep(0.5)
sys.exit(2)
' >/dev/null 2>&1; then
        echo -e "${RED}error:${NC} the cpu queue did not consume a trivial task within 10s — likely a wedged worker." >&2
        echo -e "  Check: ${YELLOW}./opentr.sh logs celery-cpu-worker${NC}" >&2
        echo -e "  And:   ${YELLOW}docker exec $CPU_WORKER_CONTAINER celery -A app.core.celery inspect stats${NC}" >&2
        exit "$EXIT_PRECONDITION"
    fi

    setup_overlays
fi
if [[ ( "$RUN_E2E" == "true" && "$E2E_SMOKE" == "false" ) || "$WITH_PIPELINE_SMOKE" == "true" ]]; then
    if ! curl -sf http://localhost:5173 >/dev/null 2>&1; then
        echo -e "${RED}error:${NC} dev frontend not reachable at :5173 — run ./opentr.sh start dev first" >&2
        exit "$EXIT_PRECONDITION"
    fi
fi
# RUN_BACKEND/RUN_E2E precondition block above already calls setup_overlays when either phase
# runs; if only RUN_FRONTEND is set, no overlay is ever needed (OVERLAY_TIER has no frontend-only
# entries), so nothing further to do here.

# --with-gpu-scale (B4): explicit opt-in only, never auto-started under any other flag. Detects
# how many GPUs THIS PROJECT has configured (not the host's raw GPU count) and either exercises
# the real topology or skips cleanly with a stated reason — same command works unmodified on a
# single-project-GPU host (this one) and a future multi-GPU one.
if $WITH_GPU_SCALE; then
    PROJECT_GPU_COUNT="$(project_gpu_count)"
    if [[ "$PROJECT_GPU_COUNT" -lt 2 ]]; then
        echo -e "${YELLOW}==>${NC} --with-gpu-scale: only 1 GPU available for this project" \
             "(GPU_DEVICE_ID == GPU_SCALE_DEVICE_ID in .env) — skipping the multi-GPU smoke test"
        export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} --deselect=tests/integration/test_gpu_scale_smoke_live.py"
    else
        echo -e "${YELLOW}==>${NC} --with-gpu-scale: $PROJECT_GPU_COUNT distinct project GPUs configured" \
             "— bringing up the --gpu-scale worker topology"
        start_stack_or_die "the --gpu-scale worker topology" "gpu-scale-bringup" --gpu-scale
        echo -e "  ${YELLOW}NOTE:${NC} this run does not revert the --gpu-scale topology automatically —" \
             "run './opentr.sh start dev' (no --gpu-scale) afterward to drop back to the single default worker."
    fi
fi

# --with-pipeline-smoke: explicit opt-in only, reserves a real GPU (LLM_TEST_GPU_DEVICE_ID,
# default 2 — an idle secondary card, never this project's own GPU 1). Same "bring it up if
# not already there" shape as --with-gpu-scale above, but unlike that one this container is
# cheap to tear back down, so it does — only if THIS run was the one that started it.
LLM_TEST_STARTED_BY_US=false
if $WITH_PIPELINE_SMOKE; then
    LLM_TEST_PORT="${LLM_TEST_PORT:-5195}"
    LLM_TEST_CONTAINER="$(overlay_container_name llm-test-vllm)"
    if [[ -n "$LLM_TEST_CONTAINER" ]]; then
        echo -e "${YELLOW}==>${NC} --with-pipeline-smoke: llm-test-vllm already up ($LLM_TEST_CONTAINER) — leaving it"
    else
        echo -e "${YELLOW}==>${NC} --with-pipeline-smoke: bringing up --with-llm-test (real GPU-backed model," \
             "can take several minutes on a cold model download)"
        start_stack_or_die "--with-llm-test (GPU-backed vLLM)" "llm-test-bringup" --with-llm-test
        LLM_TEST_STARTED_BY_US=true
        echo -e "${YELLOW}==>${NC} waiting for the vLLM OpenAI-compatible endpoint on :$LLM_TEST_PORT..."
        LLM_TEST_DEADLINE=$(( $(date +%s) + 600 ))
        until curl -sf "http://localhost:$LLM_TEST_PORT/v1/models" >/dev/null 2>&1; do
            if [[ $(date +%s) -ge $LLM_TEST_DEADLINE ]]; then
                echo -e "${RED}error:${NC} llm-test-vllm did not become healthy within 10 min — check" \
                     "'./opentr.sh logs llm-test-vllm'" >&2
                exit "$EXIT_PRECONDITION"
            fi
            sleep 5
        done
        echo -e "${GREEN}==>${NC} llm-test-vllm is healthy"
    fi
    export RUN_PIPELINE_SMOKE=1
fi

declare -a PHASE_NAMES=()
declare -a PHASE_STATUS=()
declare -a PHASE_LOGS=()
declare -a PHASE_SECONDS=()

run_phase() {
    local name="$1"; shift
    local log
    log="$REPORT_DIR/$(echo "$name" | tr ' /' '__').log"
    echo -e "${YELLOW}==>${NC} $name"
    local start end elapsed
    start=$(date +%s)
    if "$@" 2>&1 | tee "$log"; then
        local rc=${PIPESTATUS[0]}
    else
        local rc=${PIPESTATUS[0]}
    fi
    end=$(date +%s)
    elapsed=$((end - start))

    PHASE_NAMES+=("$name")
    PHASE_LOGS+=("$log")
    PHASE_SECONDS+=("$elapsed")
    if [[ "$rc" -eq 0 ]]; then
        PHASE_STATUS+=("PASS")
        echo -e "${GREEN}<==${NC} $name — PASS (${elapsed}s)"
    else
        PHASE_STATUS+=("FAIL (exit $rc)")
        echo -e "${RED}<==${NC} $name — FAIL exit $rc (${elapsed}s)"
    fi
}

if [[ "$RUN_BACKEND" == "true" ]]; then
    backend_flags=(--cleanup)
    if [[ "$E2E_SMOKE" == "true" ]]; then
        backend_flags=(--e2e-smoke "${backend_flags[@]}")
    else
        backend_flags=(--coverage "${backend_flags[@]}")
    fi
    $SEARCH_QUALITY && backend_flags=(--search-quality "${backend_flags[@]}")
    run_phase "backend (run-integration-tests.sh ${backend_flags[*]})" \
        "$REPO_ROOT/scripts/run-integration-tests.sh" "${backend_flags[@]}"
fi

if [[ "$RUN_E2E" == "true" ]]; then
    run_phase "e2e (run-e2e.sh, full suite)" \
        "$REPO_ROOT/scripts/e2e/run-e2e.sh"
fi

if [[ "$RUN_FRONTEND" == "true" ]]; then
    run_phase "frontend (frontend-check.sh --check-only)" \
        "$REPO_ROOT/scripts/frontend-check.sh" --no-claude --check-only
fi

if $WITH_GPU_DIARIZATION; then
    run_phase "GPU diarization suites (run-diarization-gpu-tests.sh)" \
        "$REPO_ROOT/scripts/run-diarization-gpu-tests.sh"
fi

if $WITH_MUTATION_TESTS; then
    run_phase "mutation testing ($MUTATION_TEST_MODULE, run-mutation-tests.sh)" \
        "$REPO_ROOT/scripts/run-mutation-tests.sh" --module "$MUTATION_TEST_MODULE"
fi

if $WITH_PIPELINE_SMOKE; then
    run_phase "pipeline smoke (upload->ASR/diarize->search->real-LLM chat)" \
        "$REPO_ROOT/scripts/e2e/run-e2e.sh" backend/tests/e2e/test_full_pipeline_smoke.py -v
fi

if $LLM_TEST_STARTED_BY_US; then
    echo -e "${YELLOW}==>${NC} --with-pipeline-smoke: stopping llm-test-vllm (this run started it)"
    docker stop "$(overlay_container_name llm-test-vllm)" >/dev/null 2>&1 || true
fi

echo ""
echo "=============================================================="
echo " run-dev-tests report — $REPORT_DIR"
echo "=============================================================="
overall_rc=0
for i in "${!PHASE_NAMES[@]}"; do
    status="${PHASE_STATUS[$i]}"
    printf "  %-55s %s (%ss)\n" "${PHASE_NAMES[$i]}" "$status" "${PHASE_SECONDS[$i]}"
    [[ "$status" == PASS ]] || overall_rc=$EXIT_GATE
done
echo "=============================================================="
# Overlay audit trail (B7): so a green run is auditable — was Keycloak actually up for real, or
# did the tests just skip clean because the DB flag was off and nobody noticed?
echo " overlays this run resolved as needed"
echo "=============================================================="
if $NO_OVERLAYS; then
    echo "  --no-overlays given — auto-detection/reconciliation skipped entirely"
elif [[ ${#OVERLAYS_NEEDED[@]} -eq 0 ]]; then
    echo "  (none needed)"
else
    for flag in "${OVERLAYS_NEEDED[@]}"; do
        state="already up"
        for f in "${OVERLAYS_STARTED_BY_US[@]}"; do [[ "$f" == "$flag" ]] && state="started by this run"; done
        printf "  %-16s %s\n" "--with-$flag" "$state"
    done
    for key in "${!AUTH_KEYS_TOUCHED[@]}"; do
        echo "  auth_config.$key forced true for this run (was ${AUTH_PRIOR_VALUE[$key]}, restored on exit)"
    done
fi
echo "=============================================================="
if [[ "$overall_rc" -eq 0 ]]; then
    echo -e "${GREEN}ALL PHASES PASSED${NC}"
else
    echo -e "${RED}ONE OR MORE PHASES FAILED${NC} — see logs above for the failing phase(s)"
fi
echo "Full logs: $REPORT_DIR"

exit "$overall_rc"
