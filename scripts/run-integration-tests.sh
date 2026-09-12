#!/bin/bash
# OpenTranscribe — canonical local test gate (issue #21)
#
# Runs the COMPLETE backend test suite against the live dev stack:
#   1. unit/API tests (includes S3/OpenSearch tests via auto-detection)
#   2. the security suites re-run under FIPS_MODE=true (phase 1 already ran them with it off)
#   3. integration-marked tests (-m integration)
#   4. gpu-marked tests (-m gpu) — deselected everywhere else, so this is their
#      only run; each module keeps its own runtime skip guard for CPU-only hosts
#   5. model-vs-schema drift (RUN_SCHEMA_DRIFT_TESTS) — needs the migrated DB
#
# GitHub Actions only runs the subset that fits a bare runner — THIS script
# is the pre-merge source of truth. Requires: ./opentr.sh start dev
#
# Usage:
#   ./scripts/run-integration-tests.sh                # full gate
#   ./scripts/run-integration-tests.sh --coverage     # + coverage report
#   ./scripts/run-integration-tests.sh --e2e-smoke    # + browser smoke tests
#   ./scripts/run-integration-tests.sh --search-quality  # + corpus relevance harness
#   ./scripts/run-integration-tests.sh --cleanup      # + orphaned test-user dry run
#   ./scripts/run-integration-tests.sh --skip-gpu     # drop the GPU phase
#   ./scripts/run-integration-tests.sh --export-capability  # + real diar-native model export (~150s, needs HUGGINGFACE_TOKEN)

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="$PROJECT_ROOT/backend/venv/bin/python"

# Where per-phase junit XML lands, so `scripts/analyze-test-timing.py` has something to read
# and a skip can be attributed after the fact. Overridable so run-dev-tests.sh can point it at
# its own per-run report dir.
GATE_ARTIFACT_DIR="${GATE_ARTIFACT_DIR:-${REPORT_DIR:-/tmp/ot-integration-gate}}"
mkdir -p "$GATE_ARTIFACT_DIR"

# ⚠️ `-rs` on EVERY pytest phase, deliberately.
#
# No phase printed skip reasons, so the 2026-09-06 gate's 21 + 18 + 78 + 56 skips had no
# recorded cause anywhere — including the 21 that made the biggest phase report NOT MEASURED.
# Diagnosing them meant re-running a 733-second phase by hand. That is the #431 trap in its
# purest form and it is one flag per phase to fix, at zero runtime cost.
#
# `--durations` is here for the same reason: the phases that pass `-o addopts=""` drop
# pyproject's `--durations=25` along with `-n auto`, so the slowest phase in the gate was also
# the only one with no attribution of where its time went.
SKIP_REASONS=(-rs)
DURATIONS=(--durations=25)

COVERAGE=false
E2E_SMOKE=false
SEARCH_QUALITY=false
CLEANUP=false
RUN_GPU=true
EXPORT_CAPABILITY=false
for arg in "$@"; do
    case "$arg" in
        --coverage) COVERAGE=true ;;
        --e2e-smoke) E2E_SMOKE=true ;;
        --search-quality) SEARCH_QUALITY=true ;;
        --cleanup) CLEANUP=true ;;
        --skip-gpu) RUN_GPU=false ;;
        --export-capability) EXPORT_CAPABILITY=true ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo -e "${RED}Unknown option: $arg${NC}"; exit 2 ;;
    esac
done

port_open() { (exec 3<>"/dev/tcp/localhost/$1") 2>/dev/null && exec 3>&- && return 0 || return 1; }

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  OpenTranscribe local test gate${NC}"
echo -e "${BLUE}========================================${NC}"

# --- Preconditions -----------------------------------------------------------
if [ ! -x "$VENV_PY" ]; then
    echo -e "${RED}backend/venv not found — create it per CLAUDE.md first.${NC}"
    exit 1
fi

MISSING=()
port_open 5176 || MISSING+=("Postgres (5176)")
if [ ${#MISSING[@]} -gt 0 ]; then
    echo -e "${RED}Dev stack not reachable: ${MISSING[*]}${NC}"
    echo -e "Start it with: ${YELLOW}./opentr.sh start dev${NC}"
    exit 1
fi

echo -e "Postgres:   ${GREEN}up (5176)${NC}"
STACK_INCOMPLETE=()
if port_open 5178; then
    echo -e "MinIO:      ${GREEN}up (5178) — S3 tests enabled${NC}"
else
    echo -e "MinIO:      ${YELLOW}down — S3-backed tests cannot run${NC}"
    STACK_INCOMPLETE+=("MinIO (5178)")
fi
if port_open 5180; then
    echo -e "OpenSearch: ${GREEN}up (5180) — search tests enabled${NC}"
else
    echo -e "OpenSearch: ${YELLOW}down — search-backed tests cannot run${NC}"
    STACK_INCOMPLETE+=("OpenSearch (5180)")
fi
echo ""

cd "$PROJECT_ROOT/backend"

COV_ARGS=()
if $COVERAGE; then
    COV_ARGS=(--cov=app --cov-report=term-missing)
fi

# The security suites re-run under FIPS_MODE=true.
#
# ⚠️ There is deliberately NO `GATES=(RUN_PKI_TESTS=true ...)` array here any more, and
# re-adding one is a regression. It exported seven `RUN_*` variables that **no test read**:
# the module-level `skipif` gates were removed from all eight files (each now opens
# `# Runs by DEFAULT. This module was gated behind RUN_<X>_TESTS...`) and the array was left
# behind. Setting a variable nothing reads is not coverage — it is a phase name that describes
# a mechanism that no longer exists.
#
# `tests/unit/test_gate_run_env_vars_are_live.py` fails on any `RUN_*` this script sets that
# no test reads through a LIVE expression. Its predecessor could not catch this: it matched the
# variable name as a plain substring over the whole file, and every one of these files still
# *mentions* its dead variable in the comment quoted above — a guard that could not fail, in
# the file written to prevent tests that cannot fail.
#
# ⚠️ `FIPS_MODE=true` IS live (`app/core/config.py` reads it at import), so the FIPS pass below
# is a real claim and stays. What went with the dead variables is the *second* pass: with the
# gates gone these files are ordinary members of the Unit/API suite, so a FIPS-**off** pass over
# them re-ran, byte for byte, tests phase 1 had just run. Measured on the 2026-09-07 gate's own
# junit artifacts: all 394 ids in `gated-fips-off.xml` also appear in `unit.xml` — 0 missing —
# so deleting that phase removed 394 duplicate EXECUTIONS and zero tests.
FIPS_MODE_SUITES=(tests/test_pki_auth.py tests/test_mfa_security.py
                  tests/test_llm_settings.py tests/test_fedramp_compliance.py
                  tests/test_fedramp_controls.py tests/test_fips_140_3.py
                  tests/test_auth_config_service.py tests/test_admin_security.py)

# --- diar-native "sidecar expected" predicate --------------------------------
#
# diar_native_sidecar_expected() used to be defined here. It moved to
# scripts/lib/diar-native-expected.sh when run-dev-tests.sh needed the same question (to
# decide whether --with-diar-native belongs in its auto-started overlay set) — see that
# file's header. Sourced rather than copied, for the reason this block already gave: a
# second copy of "is native diarization configured?" is how this repo's env-var drift
# usually starts.
# shellcheck source=lib/diar-native-expected.sh
source "$SCRIPT_DIR/lib/diar-native-expected.sh"

FAILED_PHASES=()
SKIPPED_PHASES=()   # phases that exited 4 = NOT MEASURED (verified nothing, but did not fail)
run_phase() {
    local title=$1; shift
    local rc=0
    echo -e "${BLUE}--- $title ---${NC}"
    "$@" || rc=$?
    # Exit 4 is "NOT MEASURED", distinct from both pass and fail. Only the mutation ratchet
    # emits it today: it skips modules with no run log on purpose (a measurement is 30-90
    # minutes and stays opt-in), but with no logs at all it was exiting 0 and this function
    # printed "passed" for a check that examined nothing. A phase that verified nothing must
    # not read as a phase that verified everything.
    if (( rc == 0 )); then
        echo -e "${GREEN}✓ $title passed${NC}\n"
    elif (( rc == 4 )); then
        echo -e "${YELLOW}⊘ $title NOT MEASURED — proves nothing, not counted as a pass${NC}\n"
        SKIPPED_PHASES+=("$title")
    else
        echo -e "${RED}✗ $title FAILED${NC}\n"
        FAILED_PHASES+=("$title")
    fi
}

#: Skips this phase may legitimately report — RE-DERIVED 2026-09-07, not raised.
#:
#: Measured with `-rs` on the live dev stack with the mock-llm/mock-asr/keycloak/diar-native
#: overlays up (the configuration run-dev-tests.sh --full brings up): 170 passed, 15 skipped,
#: 922 s. The previous run's 21 was 15 + the 6 lite-mode tests that the unit suite's
#: mock-llm kill had made unrunnable — fixed separately, and the 6 now pass.
#:
#: Of those 15, EIGHT are now DESELECTED rather than skipped, because they are not runnable on
#: this deployment shape at all and a skip only inflates the total:
#:   6  multi_gpu   — test_gpu_scale_smoke_live (2), test_diar_native_cross_card_placement_live
#:                    (2), test_diar_native_multigpu_provider_live (2): need a running
#:                    celery-worker-gpu-scaled/-gpu-diarize. Selected back in automatically
#:                    when that topology IS up (see MULTI_GPU_FILTER below).
#:   1  opt_in_gate — test_export_toolchain_in_shipped_images (RUN_EXPORT_CAPABILITY_TEST;
#:                    ~150 s + gated weights). `--export-capability` selects it back in.
#:   1  opt_in_gate — test_speaker_label_index_drift (RUN_INDEX_AUDIT): an audit of a
#:                    DEPLOYMENT's accumulated data, not a regression test of this code.
#:   1  opt_in_gate — test_chat_cache_share_revocation: the cache under test IS Redis, and
#:                    the dev stack's Redis is password-protected and in-network only, so a
#:                    host-run gate can NEVER satisfy its reachability gates. It needs an
#:                    isolated stack. Marked rather than ceiling-raised — adding this file
#:                    took the phase 7 -> 8 skips and turned it NOT MEASURED, which is the
#:                    ceiling doing its job, not a number that wanted incrementing.
#:
#: That leaves SEVEN honest skips, all one class — this deployment's corpus does not hold the
#: data the assertions need, which is issue #403 / the plan's P2-2 "seed it" work, not a gate
#: misconfiguration:
#:   4  test_fusion_strategy_switch.py:209,222,234,252 — 7 matching chunks, needs >=10
#:   3  test_rag_eval_harness.py:61,73,100            — the QMSum manifest's files were
#:                                                      injected into a different cluster
#:
#: ⚠️ RE-DERIVE this number, never raise it to make a phase pass, and never raise it without
#: updating the list above. Every skip is either a gate that should be running (fix the gate),
#: a test that cannot run here (give it a NAMED marker and deselect it, so it is visibly absent
#: rather than silently counted), or dead (delete it). A skip is the one outcome that looks
#: like a pass and proves nothing. When P2-2 seeds the corpus this drops to 0.
INTEGRATION_SKIP_CEILING="${INTEGRATION_SKIP_CEILING:-7}"

#: The GPU phase gets the same treatment. It previously had NO ceiling at all and reported
#: `✓ passed` on 8 passed / 18 skipped — 69% of the phase unmeasured, under a green tick.
#: Measured 2026-09-06: 18 skips, of which 6 were multi_gpu (now deselected) leaving 12, all
#: the container-only diarization suites documented in backend/tests/CLAUDE.md
#: (test_diarization_perf_gates 6, test_diarization_regression 2, test_diarizer_lifecycle 3,
#: test_worker_shutdown_vram 1) — they need /.dockerenv and /app fixtures, and their real
#: entry point is ./scripts/run-diarization-gpu-tests.sh.
GPU_SKIP_CEILING="${GPU_SKIP_CEILING:-12}"

#: ⚠️ EVERY pytest phase in this script needs a ceiling, and for a long time only two had one.
#: The three biggest — Unit/API (13,622 tests), the FIPS pass (394) and the drift phase (3) —
#: went through plain `run_phase`, so **99% of the gate by test count could mass-skip and still
#: print `✓ Unit/API suite passed`**. That is the same silent-skip trap the integration phase's
#: ceiling exists for, on twenty times the surface. `tests/unit/test_gate_phase_skip_accounting.py`
#: now fails if a pytest-invoking phase is added here without one.
#:
#: All three numbers below are DERIVED from the junit artifacts of the 2026-09-07 gate run
#: (`$GATE_ARTIFACT_DIR/*.xml`, 05:55-06:40, host load average 15.9), not chosen. Re-derive with:
#:   python3 -c "import xml.etree.ElementTree as E,sys;r=E.parse(sys.argv[1]).getroot();
#:               print(sum(int(t.get('skipped')) for t in r.iter('testsuite')))" /tmp/ot-integration-gate/unit.xml
#: Never raise one to make a phase pass.

#: Unit/API: 13,622 collected, **151 skipped**, every one attributed by `-rs`:
#:   38  RUN_SEARCH_QUALITY_TESTS  — corpus harness, has its own opt-in phase (--search-quality)
#:   73  route-coverage backlog    — "No frontend call site found" (33 + 26 + 8 + 4 + 2)
#:   14  HF_TOKEN / HUGGINGFACE_TOKEN not set
#:    7  need live Redis+OpenSearch+MinIO from outside the pytest conftest
#:    4  need backend/venv-eval + an OpenAI-compatible server on :5195
#:    3  RUN_SCHEMA_DRIFT_TESTS    — has its own phase below
#:    3  AUDIT_LOG_TO_OPENSEARCH=false, forced off by conftest (savepoints can't undo index writes)
#:    3  Redis cache db=1 / real eviction not reachable
#:    2  an OpenAI-compatible server on :5195
#:    2  release stages that predate criteria-lib.sh and carry inline copies
#:    1  MinIO reachable, so the real storage path is covered elsewhere
#:    1  no CUDA device on this host
#: A DEGRADED stack pushes this over the ceiling on purpose: with MinIO or OpenSearch down,
#: dozens of suites skip and exit 0, which is precisely the outcome that must not read as a pass.
#:
#: ⚠️ It is EXACT-today, like the other two ceilings, so the first new skip trips it. That is
#: deliberate and the correct response is to look, not to add one. The volatile component is the
#: 73-skip route-coverage backlog: adding an API route with no frontend caller legitimately adds
#: a skip here. The fix for that class is the one backend/tests/CLAUDE.md already prescribes —
#: give a permanently-skipping test a NAMED marker and DESELECT it, so it is visibly absent
#: rather than silently counted toward this number — not a bigger ceiling.
UNIT_SKIP_CEILING="${UNIT_SKIP_CEILING:-151}"

#: FIPS pass: 394 collected, **2 skipped** — both the AUDIT_LOG_TO_OPENSEARCH pair above.
#: Identical in the FIPS-off artifact, so this is a property of the suites, not of FIPS mode.
FIPS_SKIP_CEILING="${FIPS_SKIP_CEILING:-2}"

#: Model-vs-schema drift: 3 tests, and the phase SETS the variable that gates them, so a skip
#: here means the gate it opens has stopped working. Zero is the only honest ceiling — this
#: phase's whole purpose is that `RUN_SCHEMA_DRIFT_TESTS` used to be set nowhere pre-merge.
SCHEMA_DRIFT_SKIP_CEILING="${SCHEMA_DRIFT_SKIP_CEILING:-0}"

#: Search quality (opt-in, --search-quality): same shape — the phase sets
#: RUN_SEARCH_QUALITY_TESTS itself, so any skip is the gate failing to open. The suite
#: self-seeds its own corpus (tests/fixtures/search_corpus.py), so it does not depend on
#: whatever happens to be in the deployment.
SEARCH_QUALITY_SKIP_CEILING="${SEARCH_QUALITY_SKIP_CEILING:-0}"

#: Tests that cannot run on THIS deployment are DESELECTED by marker, not skipped: a
#: deselected test is visibly absent from the count, a skipped one silently inflates it
#: toward the ceiling and buries the skips that mean something. The marker is registered in
#: backend/pyproject.toml with the reason it exists.
#:
#: ⚠️ Deselection is CONDITIONAL, not permanent. `multi_gpu` tests need a running
#: celery-worker-gpu-scaled / celery-worker-gpu-diarize — a topology `--gpu-scale` /
#: `--gpu-split` creates — and NOT merely a second card, so deselecting them unconditionally
#: would hide real coverage on a host that can run them (root CLAUDE.md: GPU 2 is usable by
#: this project, and treating this host as single-GPU is a documented cost, not a safe
#: default). So the topology is DETECTED, and the choice is printed: a deselection nobody can
#: see is the same failure as a silent skip.
# shellcheck source=lib/compose-project.sh
source "$SCRIPT_DIR/lib/compose-project.sh"
multi_gpu_topology_present() {
    [[ -n "$(overlay_container_name celery-worker-gpu-scaled)" ]] && return 0
    [[ -n "$(overlay_container_name celery-worker-gpu-diarize)" ]] && return 0
    return 1
}
if multi_gpu_topology_present; then
    MULTI_GPU_FILTER=""
    echo -e "${GREEN}multi-GPU worker topology detected — multi_gpu tests WILL run${NC}"
else
    MULTI_GPU_FILTER=" and not multi_gpu"
    echo -e "${YELLOW}no celery-worker-gpu-scaled/-gpu-diarize container — multi_gpu tests"
    echo -e "  DESELECTED (not skipped). Run them with: ./opentr.sh start dev --gpu-scale${NC}"
fi
#: `opt_in_gate` tests are deselected unless the run explicitly asks for them. Today only
#: --export-capability does; RUN_INDEX_AUDIT's audit is driven by hand
#: (`-m "integration and opt_in_gate"`).
if $EXPORT_CAPABILITY; then
    # ⚠️ NOT `OPT_IN_FILTER=""`. Clearing the filter re-selects EVERY opt_in_gate test, not
    # just the export one -- including RUN_INDEX_AUDIT's deployment audit, which then SKIPS
    # (its env var is unset) and counts against this phase's ceiling. Measured 2026-09-08 on
    # the release `test` stage: 14 skips against a ceiling of 7, so the stage reported
    # NOT MEASURED and the release gate could not be counted at all. That is the exact
    # failure the opt_in_gate marker's own docstring says it exists to prevent -- "a
    # permanently-skipping test inflates the skip total toward the ceiling and buries the
    # skips that mean something".
    OPT_IN_FILTER=" and (not opt_in_gate or export_capability)"
else
    OPT_IN_FILTER=" and not opt_in_gate"
fi
INTEGRATION_SELECTION="${INTEGRATION_SELECTION:-integration$MULTI_GPU_FILTER$OPT_IN_FILTER}"
GPU_SELECTION="${GPU_SELECTION:-gpu$MULTI_GPU_FILTER}"

# Like run_phase, but a phase that SKIPPED more than the ceiling is NOT MEASURED.
#
#   run_phase_watching_skips <title> <ceiling> <command...>
#
# ⚠️ The ceiling is a POSITIONAL PARAMETER, never a `PHASE_SKIP_CEILING=N
# run_phase_watching_skips ...` command prefix, and the difference is not cosmetic.
# **A prefix assignment on a shell FUNCTION call is exported into that function's child
# processes** — it is not scoped to the call the way it is for an external command. Measured:
#
#     $ bash -c 'f(){ env | grep -c "^LEAK="; }; LEAK=151 f; echo "after:[${LEAK:-unset}]"'
#     1
#     after:[unset]
#
# So every phase below ran **pytest** with `PHASE_SKIP_CEILING=<this phase's ceiling>` in its
# environment. That leaked into the gate's own self-test: `tests/unit/
# test_integration_gate_skip_ceiling.py` drives this function through `bash -c`, its harness
# inherited the ambient 151 rather than using its own 5, and three of its cases were red in
# the gate for a reason that had nothing to do with what they check. A positional parameter
# cannot leak anywhere — the same shape `scripts/e2e/run-e2e.sh`'s `enforce_skip_ceiling`
# already has, which is immune by construction.
#
# The ceiling comes SECOND, after the title, on purpose: `tests/unit/
# test_gate_phase_coverage.py` identifies a phase by matching `run_phase\w* "<title>"`, so
# putting it first would break a guard in a file this change does not own.
#
# Output is teed rather than captured, so the run still streams; PIPESTATUS carries
# pytest's real exit code past the pipe.
run_phase_watching_skips() {
    local title=$1 ceiling=$2; shift 2
    # A missing or garbled ceiling would otherwise shift the whole command left by one
    # argument and run something nobody wrote — most likely `pytest` with the ceiling's
    # intended value as a path. Loud, and recorded as a FAILED phase: a misconfigured
    # dispatcher must never be able to report a pass.
    if [[ ! "$ceiling" =~ ^[0-9]+$ ]]; then
        echo -e "${RED}✗ $title MISCONFIGURED — argument 2 must be the skip ceiling," \
            "got '$ceiling'${NC}\n"
        FAILED_PHASES+=("$title (bad skip-ceiling argument)")
        return
    fi
    local rc=0
    local out
    out=$(mktemp)
    echo -e "${BLUE}--- $title ---${NC}"
    # `|| true` here would CLOBBER PIPESTATUS — it becomes the status of `true`,
    # so a genuinely failing phase was recorded as neither failed nor skipped.
    # Caught by this function's own self-test; disable errexit around the pipe
    # instead, which leaves PIPESTATUS intact.
    set +e
    "$@" 2>&1 | tee "$out"
    rc=${PIPESTATUS[0]}
    set -e

    local skipped
    skipped=$(grep -oE '[0-9]+ skipped' "$out" | tail -1 | grep -oE '^[0-9]+' || echo 0)
    rm -f "$out"

    if (( rc == 0 )) && (( skipped > ceiling )); then
        echo -e "${YELLOW}⊘ $title NOT MEASURED — $skipped test(s) skipped, ceiling is ${ceiling}${NC}"
        echo -e "  Exit 0 with mass skips is indistinguishable from a real pass. Something the"
        echo -e "  suite needs is unreachable, or a gate has started skipping silently."
        echo -e "  Every phase runs with -rs: grep the output above for 'SKIPPED' to see why.\n"
        SKIPPED_PHASES+=("$title")
        return
    fi
    if (( rc == 0 )); then
        echo -e "${GREEN}✓ $title passed${NC} (${skipped} skipped)\n"
    else
        echo -e "${RED}✗ $title FAILED${NC}\n"
        FAILED_PHASES+=("$title")
    fi
}

# 0. Signature-scoped sweep of orphaned test data (issue #629) — unconditional (not
# gated behind --cleanup, unlike phase 10's dry-run report below), so leftovers from a
# PREVIOUS killed run are cleared before this run adds its own. Deletes Tier A
# (unambiguous-signature) candidates only; escape hatch: OT_SKIP_TEST_DATA_SWEEP=1.
# Registers this run as a live testrun marker first, so anything IT creates is
# protected by the same liveness cutoff that protects any other concurrently-running
# suite's data.
source "$PROJECT_ROOT/scripts/testrun-registry.sh"
testrun_begin
if [ "${OT_SKIP_TEST_DATA_SWEEP:-}" = "1" ]; then
    echo -e "${YELLOW}--- Test-data sweep: skipped (OT_SKIP_TEST_DATA_SWEEP=1) ---${NC}\n"
else
    run_phase "Test-data sweep (Tier A)" \
        "$VENV_PY" "$PROJECT_ROOT/scripts/cleanup-test-data.py" --execute-unambiguous
fi

# 1. The main suite (default config: -n auto, -m 'not integration and not gpu')
#
# ⚠️ `run_phase_watching_skips`, not `run_phase`. This is 13,622 of the gate's ~14,200 tests and
# it had NO skip ceiling at all, so a stack outage that made thousands of suites skip still
# printed `✓ Unit/API suite passed` and exited 0. It is also the phase whose `-rs` output is the
# only attribution any of those skips has.
run_phase_watching_skips "Unit/API suite" "$UNIT_SKIP_CEILING" \
    "$VENV_PY" -m pytest tests/ "${COV_ARGS[@]}" "${SKIP_REASONS[@]}" \
    --junitxml="$GATE_ARTIFACT_DIR/unit.xml"

# 2. The security suites again, this time under FIPS_MODE=true.
#
# ONE pass, not two. `FIPS_MODE` is read at import by app/core/config.py, so "these suites behave
# the same in both FIPS modes" is a real claim — but phase 1 above IS the FIPS-off half of it,
# since these files carry no gate and are ordinary members of tests/. The separate FIPS-off phase
# that used to sit here re-executed 394 tests phase 1 had already run (proved by node-id diff of
# the two junit artifacts: 0 of 394 absent from unit.xml) under `env` variables no test reads.
run_phase_watching_skips "Security suites (FIPS_MODE=true)" "$FIPS_SKIP_CEILING" \
    env FIPS_MODE=true "$VENV_PY" -m pytest "${FIPS_MODE_SUITES[@]}" -o addopts="" -n auto --dist loadgroup -q --tb=short \
    "${SKIP_REASONS[@]}" --junitxml="$GATE_ARTIFACT_DIR/gated-fips-on.xml"

# 3. Integration-marked tests (need the live stack)
#
# Collected from the paths that hold them, not all of tests/: there are 20 such tests and
# sweeping the 5,200-test tree to find them cost ~23 s of pure collection. Deliberately still
# SERIAL — `-o addopts=""` drops the inherited `-n auto`, which is correct here because these
# talk to the live stack and share its state (uploads, reprocessing, mirror state); running
# them concurrently would make them interfere rather than faster.
#
# ⚠️ `-n <N> --dist loadfile` was evaluated for this phase and REJECTED. Three things were
# checked before concluding that, so a future attempt starts from facts rather than repeating
# them:
#   * `--dist loadfile` IS the right distribution here if anyone tries again — every
#     container-provisioning fixture in this selection is module- or session-scoped, and both
#     `xdist_group` marks in it (`tests/test_selective_reprocess.py`,
#     `test_lite_mode_mocked_providers.py`) are MODULE-level `pytestmark`, which keeping a file
#     on one worker already satisfies. There are zero `ddl_exclusive` tests in this selection,
#     so the advisory-lock barrier is not a factor either.
#   * The blocker is not the container suites — those are self-contained throwaways and are
#     trivially parallel-safe. It is the LIVE-STACK modules, and it has a name:
#     `test_fusion_strategy_switch.py::test_nothing_in_this_module_wrote_to_the_index` reads
#     `indices.stats(transcript_chunks)["_all"]["total"]["indexing"]["index_total"]` before and
#     after a search and asserts it is UNCHANGED. That is a cluster-wide counter for an index
#     that eight other selected modules write to (`test_rename_propagation_chunks`,
#     `test_chunk_pruning_opensearch`, `test_digest_plane_opensearch`, `test_corpus_injection_e2e`,
#     `test_corpus_injection_synthetic_e2e`, `test_reindex_on_mutation_666`,
#     `test_speaker_rename_service_chunks`, `test_speaker_label_index_drift`). Any overlap makes
#     it fail for a reason that is not about the code — a flaky gate, which is worse than a slow
#     one. `test_redaction_pipeline.py` and `test_lite_mode_mocked_providers.py` are a second,
#     independent instance: both mutate global `SystemSettings` that other modules read.
#   * So the phase's cost and the phase's interference risk sit in DIFFERENT modules. If this
#     is revisited, the shape that works is splitting the selection — the throwaway-container
#     suites in parallel, the live-stack suites serial — not turning `-n` on over the whole
#     thing. That costs a second file list here, which this repo has been bitten by before
#     (see the `--e2e-smoke` note further down), so it needs a guard, not just a flag.
#
# The waste that WAS removable here was per-test container provisioning, and it was removed in
# the suites themselves rather than in this script: see `tests/integration/conftest.py` and
# `tests/unit/test_{throwaway_pg_sharing,integration_container_sharing}.py`.
#
# The narrowing is guarded: tests/unit/test_gate_phase_coverage.py fails if an
# `integration`-marked test appears outside these paths, so one added elsewhere cannot go
# silently unrun the way `gpu` did before #297 (issue #431).
#
# `--timeout` is restated explicitly because `-o addopts=""` drops pyproject's `--timeout=300`
# along with `-n auto`. Without it NOTHING bounds a stuck test: these poll the live stack, so a
# stage that never settles hangs the phase indefinitely rather than failing it (issue #493).
# The value is deliberately generous — a real reprocess of a long recording is legitimately
# minutes — the point is that a ceiling exists at all.
# ⚠️ A mass-SKIPPED phase must not read as a passed phase (issue #491 follow-up).
#
# `SKIP_S3` / `SKIP_OPENSEARCH` are set by the root conftest from a TCP probe, so with
# either service down the tests that need it SKIP rather than fail — and pytest exits
# **0**. Measured on this gate:
#
#     stack up    101 passed,  3 skipped   exit 0   ✓ "passed"
#     stack down   34 passed, 71 skipped   exit 0   ✓ "passed"
#
# Identical verdict, 67 fewer tests actually executed. That is the documented
# silent-skip trap, and it sat directly under the evidence for #400/#435 and
# #405/#432 — the only tests that exercise real OpenSearch semantics for either.
#
# Two guards, because either alone is insufficient: the ports can be open while a
# suite has quietly started skipping for some other reason.
if [ ${#STACK_INCOMPLETE[@]} -gt 0 ]; then
    echo -e "${BLUE}--- Integration-marked tests ---${NC}"
    echo -e "${YELLOW}⊘ Integration-marked tests NOT MEASURED — proves nothing, not counted as a pass${NC}"
    echo -e "  ${STACK_INCOMPLETE[*]} unreachable, so every test needing them would SKIP and"
    echo -e "  the phase would still exit 0. Start the full stack: ${YELLOW}./opentr.sh start dev${NC}\n"
    SKIPPED_PHASES+=("Integration-marked tests")
else
    # test_export_toolchain_in_shipped_images.py::test_the_running_backend_actually_completes_a_real_export
    # lives in tests/integration/ and is therefore already COLLECTED here — but it
    # self-gates on RUN_EXPORT_CAPABILITY_TEST and, unset, always skips ("NOT MEASURED",
    # never a pass). Nothing else in this repo set that variable, so the real ~150s,
    # gated-weights export it drives had never actually run in any automated path
    # (issue: the headline "provision on first boot" capability was covered by nothing).
    # --export-capability is deliberately opt-in here (a 150s download is too heavy for
    # the everyday inner loop) but IS unconditionally wired into the release pipeline —
    # scripts/release/60-test.sh always passes it — so the real check still runs
    # somewhere automated rather than depending on an operator remembering the flag. A
    # HUGGINGFACE_TOKEN with the pyannote/speaker-diarization-community-1 gate accepted
    # is required for a real verdict; without one the test skips loudly, distinguishably
    # from a pass (see that test's own skip message).
    EXPORT_ENV=()
    if $EXPORT_CAPABILITY; then
        EXPORT_ENV=(env RUN_EXPORT_CAPABILITY_TEST=1)
    fi
    # -m integration is one of only two selections (with -m gpu below) that the CUDA
    # device guard (issue #719, tests/conftest.py's pytest_configure) leaves un-blinded:
    # two files under tests/integration/ carry BOTH `integration` and `gpu` markers
    # (test_diar_native_smoke_live.py, test_gpu_scale_smoke_live.py), so this phase
    # legitimately needs real device visibility and must stay in that set.
    # The ceiling is a required argument for EVERY phase, including this one, whose number
    # used to be the dispatcher's silent fallback. An implicit ceiling is invisible at the
    # call site, and "no prefix means the integration number" is exactly the coupling that had
    # to be broken the moment the GPU phase needed a different one.
    run_phase_watching_skips "Integration-marked tests" "$INTEGRATION_SKIP_CEILING" \
        "${EXPORT_ENV[@]}" "$VENV_PY" -m pytest tests/integration/ tests/test_selective_reprocess.py tests/eval/ \
        -o addopts="" -m "$INTEGRATION_SELECTION" -q --tb=short --strict-markers \
        --timeout="${INTEGRATION_TEST_TIMEOUT:-900}" \
        "${SKIP_REASONS[@]}" "${DURATIONS[@]}" --junitxml="$GATE_ARTIFACT_DIR/integration.xml"
fi

# 3b. The venv this gate runs in must install what the image ships (#492).
#
# Every requirements file is exactly pinned, so the venv and the container are two
# installs of the same text and should agree. When they did not — 120 packages apart,
# 18 at a MAJOR version — this gate spent its whole runtime validating a program that
# was not the one shipping, which is how the NLTK `pathsec` breakage reached production
# green.
#
# Checked HERE rather than in CI because it needs the running container to compare
# against, which this gate already requires. Read-only; it never modifies either side.
run_phase "Dependency parity: venv vs container" \
    "$PROJECT_ROOT/scripts/check-dependency-parity.sh"

# 4. GPU-marked tests. Deselected from the fast suite and from CI (both CPU-only), so
# this gate is the ONLY place they run — they were silently ungated before #297.
# Each module still carries its own runtime skip guard, so this is a no-op on a
# machine without CUDA; pass --skip-gpu to drop the phase entirely.
#
# `-o addopts="" -m gpu` is the other selection (with -m integration above) that the
# CUDA device guard (issue #719) leaves un-blinded — CUDA_VISIBLE_DEVICES is left as
# inherited rather than forced to -1, because `-m gpu` satisfies
# tests/conftest.py's `_selection_may_run_gpu_tests`. Running this phase's pytest
# invocation with a DIFFERENT -m expression that happens not to select `gpu` would
# make every gpu test in it error with `cuda-device-guard` instead of running.
#
# ⚠️ This phase runs in the VENV, so the three container-only diarization suites
# (test_diarizer_lifecycle / test_diarization_perf_gates / test_diarization_regression)
# report as SKIPS here, not passes: their `ensure_container` fixture needs /.dockerenv,
# and their audio/RTTM fixtures live at /app paths that only exist inside the benchmark
# container. They have their own entry point — see the pointer printed below.
if $RUN_GPU; then
    # Collected from the paths that hold `gpu`-marked tests, not all of tests/ — the same
    # narrowing (and the same guard) the integration phase above already has. `-m gpu` over
    # the whole tree paid a full-tree collection to find tests in 12 files, and
    # tests/unit/test_gate_phase_coverage.py now fails if a `gpu`-marked test appears outside
    # these paths, so one added elsewhere cannot go silently unrun the way `gpu` itself did
    # before #297.
    run_phase_watching_skips "GPU-marked tests" "$GPU_SKIP_CEILING" \
        "$VENV_PY" -m pytest tests/integration/ tests/unit/test_cuda_device_guard.py \
        -o addopts="" -m "$GPU_SELECTION" -q --tb=short --strict-markers \
        --timeout="${GPU_TEST_TIMEOUT:-1800}" \
        "${SKIP_REASONS[@]}" "${DURATIONS[@]}" --junitxml="$GATE_ARTIFACT_DIR/gpu.xml"

    echo -e "${YELLOW}NOTE: the diarization lifecycle/perf-gate/RTTM-regression suites skip in the${NC}"
    echo -e "${YELLOW}      phase above (container-only). Run them with:${NC}"
    echo -e "${YELLOW}        ./scripts/run-diarization-gpu-tests.sh${NC}"

    # The diar-native sidecar is a separate container running a Rust binary, so no
    # pytest module can inspect it — its execution provider is only observable from
    # outside, via device-memory residency (issue #520). Exits 4 when the sidecar is
    # not running.
    #
    # issue #669: this used to go through run_phase like every other phase, which maps
    # exit 4 to NOT MEASURED unconditionally — so this, the pre-merge gate, was green on
    # a stack whose diarizer never ran, on EVERY machine, including ones where the
    # sidecar was fully configured and simply not started. That is too strict to fix by
    # making exit 4 fatal everywhere (a frontend dev's laptop with no sidecar configured
    # would fail a gate it has no way to satisfy) and too lax to leave as a silent skip
    # (a machine where native diarization IS configured deserves a real gate). So: fail
    # only when diar_native_sidecar_expected() says the sidecar should be running on
    # THIS deployment; otherwise report it — loudly, by name, same as every other NOT
    # MEASURED phase — but do not fail the gate over it.
    diar_native_rc=0
    bash "$PROJECT_ROOT/scripts/diar-native-smoke.sh" || diar_native_rc=$?
    echo -e "${BLUE}--- diar-native CUDA execution provider ---${NC}"
    if (( diar_native_rc == 0 )); then
        echo -e "${GREEN}✓ diar-native CUDA execution provider passed${NC}\n"
    elif (( diar_native_rc == 4 )); then
        if diar_native_sidecar_expected; then
            echo -e "${RED}✗ diar-native CUDA execution provider NOT MEASURED, but engine.diarizer_backend"
            echo -e "  resolves to native and an export or HUGGINGFACE_TOKEN is configured — this"
            echo -e "  deployment was expected to be running the sidecar. Treating as a FAILURE.${NC}\n"
            FAILED_PHASES+=("diar-native CUDA execution provider (expected, NOT MEASURED)")
        else
            echo -e "${YELLOW}⊘ diar-native CUDA execution provider NOT MEASURED — sidecar not expected on"
            echo -e "  this deployment (backend is not native, or no export/HUGGINGFACE_TOKEN is"
            echo -e "  configured to produce one)${NC}\n"
            SKIPPED_PHASES+=("diar-native CUDA execution provider (not expected on this deployment)")
        fi
    else
        echo -e "${RED}✗ diar-native CUDA execution provider FAILED${NC}\n"
        FAILED_PHASES+=("diar-native CUDA execution provider")
    fi
else
    echo -e "${YELLOW}Skipping GPU-marked tests (--skip-gpu).${NC}"
fi

# 5. Model-vs-schema drift. Needs the live migrated DB, so it is env-gated like the security
# suites — and until now that gate was set in exactly ONE place (the release pipeline's
# `schema-drift` criterion, at severity `warn`), meaning the check never ran pre-merge at all.
# A model or column that exists on one side only raises at runtime; catching it after the
# release candidate is built is too late.
#
# Its three tests spawn ./scripts/check-schema-drift.py, which resolves the DB from the repo
# root, so this phase runs from anywhere the rest of the gate does.
#
# ⚠️ `-rs` and a ceiling of 0, both missing until now. This phase SETS the very variable that
# unblocks its three tests, so a skip here does not mean "not applicable" — it means the gate
# this phase exists to close has quietly stopped opening, which is exactly the state it was
# already in for months. Without `-rs` the skip would also have had no recorded reason: the
# `-o addopts=""` drops pyproject's flags, and this phase never restored them.
run_phase_watching_skips "Model-vs-schema drift" "$SCHEMA_DRIFT_SKIP_CEILING" \
    env RUN_SCHEMA_DRIFT_TESTS=true "$VENV_PY" -m pytest tests/unit/test_schema_drift.py \
    -o addopts="" -q --tb=short "${SKIP_REASONS[@]}"

# 5b. DB session lifetime. A session held across slow non-DB work keeps a transaction open,
# and a plain SELECT holds ACCESS SHARE for its life — so it queues ALTER TABLE (an Alembic
# upgrade hanging mid-release), pins the VACUUM horizon on transcript_segment, and burns a
# pool connection. Measured live twice in one day on two workers: 48 min and 1h26m
# idle-in-transaction, found only because the DDL tests started failing with LockNotAvailable.
#
# Static and fast (no stack, no DB) — it is a phase here as well as a pre-commit hook because
# pre-commit only fires on the files a commit touches, and this rule is about a shape that
# spreads by passing `db` into a callee, i.e. across files a given commit may not include.
run_phase "DB session lifetime (no transaction across slow work)" \
    python3 "$SCRIPT_DIR/audit-session-lifetime.py" "$PROJECT_ROOT/backend/app"

# 6. Collection determinism. Two independent processes must collect the SAME test ids.
#
# This exists because a single parametrize argument built from `uuid4()` at import time made
# the ENTIRE suite fail collection: xdist runs one import per worker, each got a different
# id, and xdist aborted with "Different tests were collected between gw1 and gw0" — every
# worker, zero tests run. It passed when its own file was run alone, which is exactly how it
# reached the shared suite.
#
# Tests the property directly rather than blocklisting the causes, so it also catches
# collection that varies with time, locale, filesystem order or a stray environment read.
# Lives here rather than in the fast suite: two full collections cost ~30 s, and this branch
# spent a lot of effort getting that suite down to ~2 min.
# ⚠️ TWO PROCESSES, run in PARALLEL. Keeping them as two separate processes IS the
# measurement — collapsing them into one collection compared against itself would prove
# nothing. But they are independent, so running them one after the other only doubled the wall
# clock: measured 78.3 s serial for a single collection of 39.3 s.
run_phase "Collection determinism (two processes, same test ids)" \
    bash -c '
        set -uo pipefail
        a=$(mktemp) && b=$(mktemp)
        trap "rm -f $a $b" EXIT
        "'"$VENV_PY"'" -m pytest --collect-only -q -o addopts= -p no:cacheprovider \
            2>/dev/null | grep "::" | sort > "$a" &
        pid_a=$!
        "'"$VENV_PY"'" -m pytest --collect-only -q -o addopts= -p no:cacheprovider \
            2>/dev/null | grep "::" | sort > "$b" &
        pid_b=$!
        # Wait on both regardless of order, and do not let a crashed collector look like an
        # empty-but-equal pair — the emptiness check below is the backstop for that.
        wait "$pid_a" || true
        wait "$pid_b" || true
        if [[ ! -s $a || ! -s $b ]]; then
            # BOTH, now that they run concurrently: one collector dying leaves an empty file,
            # and two empty files diff clean. "Identical" over nothing is the silent-skip trap
            # wearing a determinism check as a hat.
            echo "collected nothing — a probe did not run (a=$(wc -l < "$a") b=$(wc -l < "$b"))" >&2
            exit 1
        fi
        if ! diff -u "$a" "$b" > /tmp/ot-collection-diff.txt; then
            echo "Test ids differ between two collections of the SAME tree." >&2
            echo "Under -n auto this makes xdist abort the whole run. First 20 lines:" >&2
            head -20 /tmp/ot-collection-diff.txt >&2
            exit 1
        fi
        echo "$(wc -l < "$a") test ids, identical across both collections"
    '

# 7. Mutation ratchet — cheap, and it reads results the operator already produced.
#
# Does NOT run mutmut (that is 30-90 minutes per module and stays opt-in). It compares the
# LAST run's survivor count for each module against scripts/mutation-baselines.tsv and fails
# if a count rose or a module's test-selection coverage fell. Modules with no prior run are
# skipped, so this never blocks a gate on a benchmark nobody asked for — but they are now
# NAMED, and a run that measured nothing exits 4 and reports "NOT MEASURED" instead of
# "passed". It previously exited 0 in silence, so a gate with no evidence at all was
# indistinguishable in the output from a clean six-module ratchet.
#
# The ratchet exists because "kill every mutant" is not finishable: lockout's 149 survivors
# include 77 log-string edits no caller can observe. Down is progress, up is a regression, and
# that is a gate you can actually pass.
run_phase "Mutation ratchet (last run vs baselines)" \
    "$SCRIPT_DIR/run-mutation-tests.sh" --check-baseline

# 8. Optional: corpus-dependent search relevance harness
#
# Same treatment as the drift phase: this one sets RUN_SEARCH_QUALITY_TESTS itself, so a skip is
# the gate failing to open, not a legitimate abstention. `-rs` was missing here too.
if $SEARCH_QUALITY; then
    run_phase_watching_skips "Search quality harness (corpus-dependent)" \
        "$SEARCH_QUALITY_SKIP_CEILING" \
        env RUN_SEARCH_QUALITY_TESTS=true "$VENV_PY" -m pytest tests/test_search_quality.py \
        -o addopts="" -q --tb=short "${SKIP_REASONS[@]}"
fi

# 9. Optional: browser smoke tests against the live stack.
#
# Through scripts/e2e/run-e2e-smoke.sh rather than a bare pytest, for two reasons that are
# really one: this file listed the same four e2e files as that script, so the list had two
# homes and could drift — and because the gate bypassed run-e2e.sh, it also bypassed
# `resolve_phase`, the workers, and the stack preflight. Notably, the gate's own bypass is
# WHY nobody noticed that run-e2e-smoke.sh always exited non-zero (its phase 2 collects no
# `visual` test, exits 5, and only resolve_phase forgives that) — see run-e2e.sh's comment.
# Running the smoke the way a developer runs it is the point of a smoke phase.
if $E2E_SMOKE; then
    run_phase "E2E smoke (browser)" "$SCRIPT_DIR/e2e/run-e2e-smoke.sh"
fi

# 10. Optional: orphaned test-user report (dry run — pass --execute manually to apply)
if $CLEANUP; then
    echo -e "${BLUE}--- Orphaned test users (dry run) ---${NC}"
    "$VENV_PY" "$PROJECT_ROOT/scripts/cleanup-test-users.py" || true
    echo ""
fi

# --- Summary -----------------------------------------------------------------
echo -e "${BLUE}========================================${NC}"
if [ ${#FAILED_PHASES[@]} -eq 0 ]; then
    # "All selected phases passed" must not absorb a phase that verified nothing. Naming the
    # NOT MEASURED phases here is the difference between a gate and a green light.
    if (( ${#SKIPPED_PHASES[@]} > 0 )); then
        echo -e "${YELLOW}Phases that PASSED NOTHING (not measured — no evidence available):${NC}"
        for phase in "${SKIPPED_PHASES[@]}"; do echo -e "  ${YELLOW}⊘ $phase${NC}"; done
        echo -e "${GREEN}All other selected phases passed.${NC}"
        # ⚠️ EXIT 4, not 0. A NOT MEASURED phase is not a pass, and for three months this
        # script said so in its own output and then exited 0 anyway — so run-dev-tests.sh
        # recorded the backend phase PASS and scripts/release/60-test.sh recorded
        # `integration-gate pass` for a release, on a run where the largest phase (733 s,
        # 21 skips) had explicitly declined to be counted. Printing a warning nobody's exit
        # code reads is the same as not printing it.
        #
        # 4 is this script's existing NOT-MEASURED code (run_phase, above), so callers that
        # already distinguish it need no change; those that do not now see non-zero, which is
        # the conservative direction.
        echo -e "${YELLOW}Exiting 4 (NOT MEASURED) — not a failure, and not a pass.${NC}"
        exit 4
    fi
    echo -e "${GREEN}All selected phases passed.${NC}"
    exit 0
else
    echo -e "${RED}Failed phases:${NC}"
    for phase in "${FAILED_PHASES[@]}"; do echo -e "  ${RED}✗ $phase${NC}"; done
    if (( ${#SKIPPED_PHASES[@]} > 0 )); then
        echo -e "${YELLOW}Not measured:${NC}"
        for phase in "${SKIPPED_PHASES[@]}"; do echo -e "  ${YELLOW}⊘ $phase${NC}"; done
    fi
    exit 1
fi
