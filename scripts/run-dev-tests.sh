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
#   0 pass · 1 gate failed · 2 misuse · 3 precondition unmet · 5 NOT MEASURED
# 5, not 4: under the standard contract 4 already means operator abort — see EXIT_NOT_MEASURED.

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
# A phase that verified nothing is neither a pass nor a failure, and must not be absorbed into
# either. ⚠️ **5, not 4** — matching scripts/test-matrix.sh:46's own EXIT_NOT_MEASURED, not
# run-integration-tests.sh's internal 4. In the repo-wide "standard" exit contract (release.sh,
# and test-matrix.sh's leg contract, which is what runs THIS script as leg 2a) 4 already means
# **operator abort**, so returning 4 here would have made a not-measured backend phase report as
# `ABORT — the leg reported an operator abort`. run-integration-tests.sh can use 4 because it
# has no prompt and no abort path; this script is invoked under the standard contract and
# cannot. The translation happens at the boundary, in run_phase below.
EXIT_NOT_MEASURED=5
# The code this script RECEIVES for the same verdict, which is a different number from the one
# it EMITS above. Both wrapped test scripts use 4: scripts/e2e/run-e2e.sh's own
# EXIT_NOT_MEASURED, and scripts/run-integration-tests.sh's summary block. Named rather than
# spelled inline at the comparison because there was previously nothing tying the three
# together — change run-e2e.sh's constant and a NOT MEASURED e2e phase would have rendered
# here as FAIL with every test green. backend/tests/unit/test_e2e_runner_skip_accounting.py
# parses all three and fails if they disagree.
PHASE_NOT_MEASURED_EXIT=4

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
# Passed straight through to run-integration-tests.sh, for the same reason as
# --search-quality above: so scripts/release/60-test.sh can get the release gate's flags
# AND this script's overlay orchestration from ONE command. Calling
# run-integration-tests.sh directly loses the overlays, and that is not cosmetic —
# measured 2026-09-08, the release `test` stage skipped all 6
# test_lite_mode_mocked_providers tests ("mock-asr and/or mock-llm containers not
# running"), which pushed the integration phase to 14 skips against a ceiling of 7 and
# made the whole stage report NOT MEASURED. The release gate could not be counted at all.
EXPORT_CAPABILITY=false
# Also passed straight through, and ADDITIVE — unlike --fast, which swaps --coverage FOR
# --e2e-smoke. The release gate has always asked run-integration-tests.sh for
# `--coverage --e2e-smoke --export-capability` together, so delegating had to be able to
# reproduce that exact set rather than an approximation of it.
GATE_E2E_SMOKE=false

# `--no-coverage`: drop --coverage from the backend gate.
#
# NOT the default, and the measurement is why. Measured on this host, 13,558 tests, idle,
# same tree: without --cov **168.1 s**, with --cov **173.7 s** — the instrumentation costs
# **5.6 s**, about 3% of the unit phase and well under 1% of a --full run. That is inside the
# run-to-run noise this suite is documented to have (root CLAUDE.md records 21-28 s of
# same-config variation), so flipping the default would trade a report produced every run for
# a saving that cannot reliably be observed. The flag is for tight iteration on one phase.
NO_COVERAGE=false

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
  --e2e-smoke         add run-integration-tests.sh's --e2e-smoke phase to the backend gate,
                      WITHOUT dropping --coverage the way --fast does. For the release gate,
                      which asks for both.
  --export-capability add run-integration-tests.sh's --export-capability phase (a REAL
                      diar-native model export). Used by the release gate, which needs it
                      AND this script's overlay orchestration.
  --search-quality    add run-integration-tests.sh's --search-quality phase to the backend
                      gate (self-seeding 6-meeting corpus; several extra minutes). This is
                      what full-test-matrix.md's Cycle 2A leg 1 asks for, so the matrix can
                      run that leg through this script and keep its overlay orchestration
  --no-coverage       drop --coverage from the backend gate. Measured cost of coverage:
                      5.6 s of a 168 s unit phase (168.1 -> 173.7), inside this suite's
                      run-to-run noise — for tight iteration, not a default worth changing
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
        --export-capability) EXPORT_CAPABILITY=true ;;
        --e2e-smoke) GATE_E2E_SMOKE=true ;;
        --no-coverage)   NO_COVERAGE=true ;;
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

# Gate on project_gpu_count's output being a COUNT before anything compares it numerically.
#
# A separate function purely so it can be driven directly by
# backend/tests/unit/test_dev_test_gpu_count_validation.py — the call site below is inside an
# `if $WITH_GPU_SCALE` block that would otherwise need the whole stack to reach. Echoes the
# count on success, and on failure echoes what it got instead (for the caller's message) and
# returns 1. Empty, "0", "two" and " " are all failures; "1" is a legitimate answer, so the
# caller — not this function — decides what a count of 1 means.
validate_project_gpu_count() {
    local count="${1:-}"
    if [[ "$count" =~ ^[1-9][0-9]*$ ]]; then
        printf '%s\n' "$count"
        return 0
    fi
    printf 'not a positive integer: %q\n' "$count"
    return 1
}

# --with-gpu-scale (B4): explicit opt-in only, never auto-started under any other flag. Detects
# how many GPUs THIS PROJECT has configured (not the host's raw GPU count) and either exercises
# the real topology or skips cleanly with a stated reason — same command works unmodified on a
# single-project-GPU host (this one) and a future multi-GPU one.
if $WITH_GPU_SCALE; then
    # ⚠️ An unvalidated count is a hardware CLAIM this script never measured. `project_gpu_count`
    # runs python + python-dotenv against .env; in a git worktree (no .env, no backend/venv —
    # the case this script's own docs call out) it prints NOTHING, and `[[ "" -lt 2 ]]` is TRUE
    # in bash. So a failed probe printed "only 1 GPU available for this project" and silently
    # dropped the multi-GPU leg — on a host whose CLAUDE.md says that leg must run, and where
    # treating the machine as single-GPU is a documented cost, not a safe default.
    if ! PROJECT_GPU_COUNT="$(validate_project_gpu_count "$(project_gpu_count)")"; then
        echo -e "${RED}error:${NC} --with-gpu-scale: could not determine this project's GPU count" \
             "($PROJECT_GPU_COUNT)." >&2
        echo -e "  project_gpu_count needs python-dotenv in ${YELLOW}backend/venv${NC} and a" \
             "readable ${YELLOW}.env${NC} — a git worktree has neither unless they were linked in." >&2
        echo -e "  Refusing to guess: an unmeasured '1 GPU' would skip the multi-GPU leg silently." >&2
        exit "$EXIT_PRECONDITION"
    fi
    if [[ "$PROJECT_GPU_COUNT" -lt 2 ]]; then
        echo -e "${YELLOW}==>${NC} --with-gpu-scale: only 1 GPU available for this project" \
             "(GPU_DEVICE_ID == GPU_SCALE_DEVICE_ID in .env) — not starting the --gpu-scale topology"
        # ⚠️ NO `--deselect=tests/integration/test_gpu_scale_smoke_live.py` here, deliberately.
        # A FILE-level deselect is wider than the condition it is reacting to: two of that
        # file's three tests carry `multi_gpu` (they need a running celery-worker-gpu-scaled),
        # but test_default_worker_is_registered_in_dual_gpu_mode deliberately does NOT — commit
        # 48fc6593 says so explicitly, because it asserts the DEFAULT worker is registered,
        # which is true on a single-GPU deployment too and carries its own
        # GPU_SCALE_DEFAULT_WORKER skipif. The marker-based deselection in
        # run-integration-tests.sh (MULTI_GPU_FILTER, which DETECTS the topology) already
        # removes exactly the two that need it, and only when it is absent. Deselecting the
        # file as well removed a test that had been deliberately left selectable.
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
    elif [[ "$rc" -eq "$PHASE_NOT_MEASURED_EXIT" ]]; then
        # NOT MEASURED is its own verdict, distinct from both. Both wrapped test scripts exit
        # 4 when one of their phases declined to be counted — run-integration-tests.sh (mass
        # skips past a ceiling, or a check with no evidence) and, since the e2e phase grew the
        # same accounting, scripts/e2e/run-e2e.sh. Folding that into PASS is what let a
        # 733-second phase that had printed
        # "NOT MEASURED" for itself be reported here as a green backend phase; folding it into
        # FAIL would be a lie in the other direction and would train people to ignore it.
        # Recorded here, re-emitted as EXIT_NOT_MEASURED (5) at the bottom — see that constant.
        PHASE_STATUS+=("NOT MEASURED (phase exit $rc)")
        echo -e "${YELLOW}<==${NC} $name — NOT MEASURED, exit $rc (${elapsed}s)"
    else
        PHASE_STATUS+=("FAIL (exit $rc)")
        echo -e "${RED}<==${NC} $name — FAIL exit $rc (${elapsed}s)"
    fi
}

# --------------------------------------------------------------------------- quiesce
# Wait, bounded, for the stack to go quiet before handing it to the browser suite.
#
# ⚠️ This is the single largest cause of e2e failures in a chained run, and it is not a
# property of the e2e tests. Natural experiment, same tree, same day:
#
#   run-e2e.sh standalone (19:58)            323 passed,  0 failed,               551 s
#   the same suite as phase 2 here (20:50)   333 passed,  7 failed,               611 s
#   the same suite as phase 2 here (01:31)   314 passed, 13 failed + 13 errors,   919 s
#
# The backend phase is a ~25-minute 48-worker suite; when it returns, the reindex /
# search_index_maintenance work it dispatched is still running. Direct evidence from the
# 01:31 log, before this existed:
#   AssertionError: the chunk index stayed unavailable across 4 attempts
#                   (TransportError(503, 'search_phase_execution_exception'))
#
# Four legs, one shared budget, and NON-FATAL: a stack that will not settle is reported and
# the suite runs anyway. Turning "still busy" into a gate failure would replace a diagnosable
# pile of timeouts with an undiagnosable red phase, and the e2e suite's own session preflight
# (backend/tests/e2e/conftest.py::e2e_stack_preflight) still refuses a genuinely broken stack.
#
# The budget is SOFT: each leg finishes the probe it is in (a Celery `inspect` broadcast has
# its own 3 s timeout), so a 60 s budget was measured taking 68 s. It bounds the wait, it is
# not a deadline the function meets to the second — do not write a test that asserts it is.
QUIESCE_BUDGET_S="${QUIESCE_BUDGET_S:-180}"

await_stack_quiesce() {
    # Budget comes from the environment (QUIESCE_BUDGET_S), not a parameter: there is one
    # call site and a test needs to shorten it without this growing an argument contract.
    local budget="$QUIESCE_BUDGET_S"
    local started
    started=$(date +%s)
    echo -e "${YELLOW}==>${NC} quiesce: waiting for the stack to settle before the browser suite" \
        "(budget ${budget}s)"

    # Legs 1 and 2: backend /health steady, then OpenSearch settled.
    #
    # Leg 1 REUSES backend/tests/e2e/conftest.py::_await_stable_backend rather than
    # re-implementing "N consecutive 200s" here — that helper is the one the e2e suite's own
    # preflight uses, and a second copy in bash would be free to drift from it. It is loaded by
    # path (the e2e tree is its own pytest rootdir, so there is no importable package name).
    # If it cannot be loaded, this leg is SKIPPED with a loud message rather than silently
    # replaced by a lookalike: the suite's preflight still runs the real thing.
    local py_out py_rc=0
    py_out=$("$VENV_PY" - "$budget" "${REPO_ROOT:-$PWD}" 2>&1 <<'PYEOF'
import contextlib
import importlib.util
import io
import os
import pathlib
import re
import sys
import time

import requests

budget = float(sys.argv[1])
repo_root = pathlib.Path(sys.argv[2])
deadline = time.monotonic() + budget
backend_url = os.environ.get("E2E_BACKEND_URL", "http://localhost:5174")
opensearch = "http://localhost:" + os.environ.get("OPENSEARCH_PORT", "5180")
failed = False

conftest = (repo_root / "backend/tests/e2e/conftest.py").resolve()
await_stable = None
noise = io.StringIO()
try:
    sys.path.insert(0, str(conftest.parent))
    spec = importlib.util.spec_from_file_location("_ot_e2e_conftest", conftest)
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stderr(noise), contextlib.redirect_stdout(noise):
        spec.loader.exec_module(module)
    await_stable = module._await_stable_backend
except Exception as exc:
    print(f"  backend : SKIPPED — could not load {conftest}::_await_stable_backend ({exc})")

if await_stable is not None:
    t0 = time.monotonic()
    # Capped at 60% of the budget, deliberately. A backend that is flapping (see the
    # --reload-dir note in docker-compose.override.yml) never produces 3 consecutive 200s,
    # and an uncapped leg 1 would eat the whole budget and leave OpenSearch reported as
    # "NOT SETTLED after 0s" having never been probed — measured, on the first live run.
    problem = await_stable(backend_url, required=3, budget=max(5.0, budget * 0.6))
    if problem:
        print(f"  backend : NOT STEADY after {time.monotonic() - t0:.0f}s — {problem}")
        failed = True
    else:
        print(f"  backend : steady (3 consecutive /health 200s) in {time.monotonic() - t0:.0f}s")

# Leg 2: OpenSearch. `status != red` alone is not settled — a cluster relocating or
# initialising shards, or with queued cluster tasks, answers a search with the
# search_phase_execution_exception above.
t0 = time.monotonic()
last = "no probe completed"
settled = False
first = True
# At least one probe, always: an earlier leg overrunning the shared deadline must not turn
# this one into a verdict it never measured.
while first or time.monotonic() < deadline:
    first = False
    try:
        health = requests.get(f"{opensearch}/_cluster/health", timeout=5).json()
        busy = (
            int(health.get("initializing_shards", 0))
            + int(health.get("relocating_shards", 0))
            + int(health.get("number_of_pending_tasks", 0))
        )
        last = f"status={health.get('status')} busy={busy}"
        if health.get("status") in ("green", "yellow") and busy == 0:
            settled = True
            break
    except Exception as exc:
        last = f"unreachable ({type(exc).__name__})"
    time.sleep(2.0)
if settled:
    print(f"  opensearch: settled ({last}) in {time.monotonic() - t0:.0f}s")
else:
    print(f"  opensearch: NOT SETTLED after {time.monotonic() - t0:.0f}s — {last}")
    failed = True

# Leg 3: the FRONTEND is warm enough to serve an app shell.
#
# ⚠️ A 200 on `/` proves nothing here, which is why this leg is not a port check. The dev
# frontend is Vite, which answers `/` immediately with a nearly-empty index.html and then
# transforms the module graph ON DEMAND, per request. So the first navigation after a
# container recreate pays for compiling the whole SPA — and that cost lands on whichever
# test happens to go first, as a fixture timeout rather than a failure anyone can read.
#
# Measured 2026-09-09: a `--e2e-only` run whose overlay batch had just rebuilt and recreated
# the containers reported `341 passed, 23 skipped, 1 error`, the error being
# `gallery_page` timing out on `.gallery-action-buttons` at APP_SHELL_READY_MS (30 s) —
# while the other three legs all reported settled. The backend was fine; nothing waited for
# the frontend. That is the whole of the "e2e collapses on the FIRST run against a freshly
# started stack" shape: every earlier leg watches a service the browser is not blocked on.
#
# The probe fetches `/` and then fetches the entry module the HTML actually references, so
# Vite is made to do the transform HERE, on the quiesce's budget, instead of inside a test's
# 30 s fixture. Requiring two consecutive FAST responses distinguishes "compiled and cached"
# from "answered once, slowly, while still compiling".
frontend = "http://localhost:" + os.environ.get("FRONTEND_PORT", "5173")
t0 = time.monotonic()
last = "no probe completed"
warm = False
streak = 0
first = True
while first or time.monotonic() < deadline:
    first = False
    try:
        shell = requests.get(frontend, timeout=30)
        # The entry is whatever the shell declares; never hardcode /src/main.ts, which is a
        # SvelteKit layout detail that has moved before.
        entry = re.search(r'<script[^>]+src="([^"]+)"[^>]*type="module"', shell.text) or \
                re.search(r'<script[^>]+type="module"[^>]*src="([^"]+)"', shell.text)
        if shell.status_code != 200:
            last = f"shell HTTP {shell.status_code}"
        elif entry is None:
            # A shell with no module script is a served-but-not-a-SPA answer (an nginx error
            # page, or the prod overlay). Report it rather than calling it warm.
            last = "shell has no <script type=module> — not the Vite dev server?"
        else:
            url = entry.group(1)
            if url.startswith("/"):
                url = frontend + url
            probe_started = time.monotonic()
            mod = requests.get(url, timeout=60)
            took = time.monotonic() - probe_started
            last = f"entry {mod.status_code} in {took:.1f}s"
            if mod.status_code == 200 and took < 2.0:
                streak += 1
                if streak >= 2:
                    warm = True
                    break
            else:
                streak = 0
    except Exception as exc:
        streak = 0
        last = f"unreachable ({type(exc).__name__})"
    time.sleep(2.0)
if warm:
    print(f"  frontend: warm ({last}) in {time.monotonic() - t0:.0f}s")
else:
    # Non-fatal, like every other leg: a cold frontend makes the suite slow and flaky, it
    # does not make the stack broken, and the e2e suite's own preflight still refuses a
    # genuinely dead one.
    print(f"  frontend: NOT WARM after {time.monotonic() - t0:.0f}s — {last}")
    failed = True

sys.exit(1 if failed else 0)
PYEOF
) || py_rc=$?
    echo "$py_out"

    # Leg 3: Celery idle. One `docker exec` holding a poll loop, not a probe per sample —
    # measured 15 s for a single cold exec, which would dominate the budget.
    #
    # active + reserved only. `scheduled` holds ETA/countdown tasks (retries, beat work due
    # later) and is NOT zero on an idle stack — measured 5 sitting on cpu-processor with
    # nothing running — so requiring it to drain would burn the whole budget every run.
    local worker="${CPU_WORKER_CONTAINER:-}"
    [[ -z "$worker" ]] && worker="$(overlay_container_name celery-cpu-worker)"
    local celery_rc=0
    if [[ -z "$worker" ]]; then
        echo "  celery  : SKIPPED — no celery-cpu-worker container found"
    else
        local remaining=$(( budget - ( $(date +%s) - started ) ))
        [[ "$remaining" -lt 5 ]] && remaining=5
        local celery_out
        # ⚠️ `-i` is load-bearing. Without it docker exec does not forward stdin, so
        # `python -` reads EOF, runs an EMPTY program and exits 0 — measured while writing
        # this: a green leg that had inspected nothing. The empty-output guard below is the
        # second half of that fix, so a future regression is reported rather than silent.
        celery_out=$(docker exec -i "$worker" python - "$remaining" 2>&1 <<'PYEOF'
import sys
import time

from app.core.celery import celery_app

deadline = time.monotonic() + float(sys.argv[1])
inspector = celery_app.control.inspect(timeout=3.0)
streak = 0
busy: dict[str, int] = {}
t0 = time.monotonic()
while time.monotonic() < deadline:
    busy = {}
    for kind in ("active", "reserved"):
        for worker, tasks in (getattr(inspector, kind)() or {}).items():
            if tasks:
                busy[worker] = busy.get(worker, 0) + len(tasks)
    if busy:
        streak = 0
    else:
        streak += 1
        if streak >= 2:
            print(f"  celery  : idle (active+reserved == 0, twice) in {time.monotonic() - t0:.0f}s")
            sys.exit(0)
    time.sleep(2.0)
detail = ", ".join(f"{w}:{n}" for w, n in sorted(busy.items())) or "unknown"
print(f"  celery  : STILL BUSY after {time.monotonic() - t0:.0f}s — {detail}")
sys.exit(1)
PYEOF
) || celery_rc=$?
        if [[ -z "${celery_out// /}" ]]; then
            echo "  celery  : NOT MEASURED — the inspect probe produced no output"
            celery_rc=1
        else
            echo "$celery_out"
        fi
    fi

    local elapsed=$(( $(date +%s) - started ))
    if [[ "$py_rc" -eq 0 && "$celery_rc" -eq 0 ]]; then
        echo -e "${GREEN}<==${NC} quiesce: stack settled in ${elapsed}s (budget ${budget}s)"
    else
        echo -e "${YELLOW}<==${NC} quiesce: NOT fully settled within ${elapsed}s of a ${budget}s" \
            "budget — running the e2e suite anyway. The lines above name what was still busy;" \
            "treat e2e failures in this run as suspect until it is."
    fi
}

if [[ "$RUN_BACKEND" == "true" ]]; then
    backend_flags=(--cleanup)
    if [[ "$E2E_SMOKE" == "true" ]]; then
        backend_flags=(--e2e-smoke "${backend_flags[@]}")
    elif [[ "$NO_COVERAGE" != "true" ]]; then
        backend_flags=(--coverage "${backend_flags[@]}")
    fi
    $SEARCH_QUALITY && backend_flags=(--search-quality "${backend_flags[@]}")
    $EXPORT_CAPABILITY && backend_flags=(--export-capability "${backend_flags[@]}")
    # Additive, and guarded against duplicating --fast's own --e2e-smoke.
    if $GATE_E2E_SMOKE && [[ "$E2E_SMOKE" != "true" ]]; then
        backend_flags=(--e2e-smoke "${backend_flags[@]}")
    fi
    run_phase "backend (run-integration-tests.sh ${backend_flags[*]})" \
        "$REPO_ROOT/scripts/run-integration-tests.sh" "${backend_flags[@]}"
fi

if [[ "$RUN_E2E" == "true" ]]; then
    # The browser suite must not start while the stack is still draining the backend phase's
    # work — see await_stack_quiesce above for the measurements.
    await_stack_quiesce

    # Per-run junit XML for the three e2e phases, beside this run's other logs. run-e2e.sh
    # OWNS the skip accounting and the per-phase ceilings (this script owns no test logic —
    # see the header), and reports a phase that skipped past its ceiling by exiting 4, which
    # run_phase above already renders as NOT MEASURED. So the e2e phase reaches the same
    # verdict the backend phase does, without a second copy of the rule living here.
    export E2E_ARTIFACT_DIR="$REPORT_DIR/e2e-xml"
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
    # Its own artifact dir: this also goes through run-e2e.sh's three phases, so under
    # `--full --with-pipeline-smoke` it would otherwise overwrite the full suite's junit
    # reports with a one-file run's.
    export E2E_ARTIFACT_DIR="$REPORT_DIR/pipeline-smoke-xml"
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
    case "$status" in
        PASS) ;;
        "NOT MEASURED"*)
            # A real failure anywhere still wins: NOT MEASURED must never downgrade a FAIL.
            [[ "$overall_rc" -eq 0 ]] && overall_rc=$EXIT_NOT_MEASURED ;;
        *) overall_rc=$EXIT_GATE ;;
    esac
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
elif [[ "$overall_rc" -eq "$EXIT_NOT_MEASURED" ]]; then
    echo -e "${YELLOW}NO PHASE FAILED, BUT ONE OR MORE VERIFIED NOTHING${NC} — exit $EXIT_NOT_MEASURED."
    echo -e "${YELLOW}A NOT MEASURED phase is not a green run. The phase log names what it${NC}"
    echo -e "${YELLOW}declined to count. Every phase that CAN report NOT MEASURED — the backend${NC}"
    echo -e "${YELLOW}gate's skip-watched phases and all three e2e phases — runs with -rs, so${NC}"
    echo -e "${YELLOW}grep that phase's log for SKIPPED. Both scripts also write junit XML${NC}"
    echo -e "${YELLOW}(backend: \$GATE_ARTIFACT_DIR, e2e: $REPORT_DIR/e2e-xml).${NC}"
else
    echo -e "${RED}ONE OR MORE PHASES FAILED${NC} — see logs above for the failing phase(s)"
fi
echo "Full logs: $REPORT_DIR"

exit "$overall_rc"
