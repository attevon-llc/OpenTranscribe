#!/bin/bash
# OpenTranscribe — canonical local test gate (issue #21)
#
# Runs the COMPLETE backend test suite against the live dev stack:
#   1. ungated unit/API tests (includes S3/OpenSearch tests via auto-detection)
#   2. the security-gated suites (RUN_* env vars) in both FIPS modes
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

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="$PROJECT_ROOT/backend/venv/bin/python"

COVERAGE=false
E2E_SMOKE=false
SEARCH_QUALITY=false
CLEANUP=false
RUN_GPU=true
for arg in "$@"; do
    case "$arg" in
        --coverage) COVERAGE=true ;;
        --e2e-smoke) E2E_SMOKE=true ;;
        --search-quality) SEARCH_QUALITY=true ;;
        --cleanup) CLEANUP=true ;;
        --skip-gpu) RUN_GPU=false ;;
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

# NOTE: RUN_SCHEMA_DRIFT_TESTS is deliberately NOT in this array — it gates a file that is
# not in GATED_FILES, so setting it here would look like coverage while changing nothing.
# It gets its own phase below.
GATES=(RUN_PKI_TESTS=true RUN_MFA_TESTS=true RUN_LLM_TESTS=true
       RUN_FEDRAMP_TESTS=true RUN_FIPS_TESTS=true
       RUN_AUTH_CONFIG_TESTS=true RUN_ADVANCED_ADMIN_TESTS=true)
GATED_FILES=(tests/test_pki_auth.py tests/test_mfa_security.py
             tests/test_llm_settings.py tests/test_fedramp_compliance.py
             tests/test_fedramp_controls.py tests/test_fips_140_3.py
             tests/test_auth_config_service.py tests/test_admin_security.py
             tests/test_admin_endpoints.py)

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

#: Skips this phase may legitimately report. Measured against the live dev stack on
#: 2026-08-18: `101 passed, 3 skipped`. The three are runtime guards inside tests whose
#: services ARE up (e.g. a model that is not deployed), not absent infrastructure.
#: Raising this number is how the trap above comes back — re-measure before you do,
#: and say what the new skips are.
INTEGRATION_SKIP_CEILING="${INTEGRATION_SKIP_CEILING:-5}"

# Like run_phase, but a phase that SKIPPED more than the ceiling is NOT MEASURED.
#
# Output is teed rather than captured, so the run still streams; PIPESTATUS carries
# pytest's real exit code past the pipe.
run_phase_watching_skips() {
    local title=$1; shift
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

    if (( rc == 0 )) && (( skipped > INTEGRATION_SKIP_CEILING )); then
        echo -e "${YELLOW}⊘ $title NOT MEASURED — $skipped test(s) skipped, ceiling is ${INTEGRATION_SKIP_CEILING}${NC}"
        echo -e "  Exit 0 with mass skips is indistinguishable from a real pass. Something the"
        echo -e "  suite needs is unreachable, or a gate has started skipping silently.\n"
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

# 1. Ungated suite (default config: -n auto, -m 'not integration')
run_phase "Unit/API suite" "$VENV_PY" -m pytest tests/ "${COV_ARGS[@]}"

# 2. Security-gated suites — non-FIPS then FIPS mode
run_phase "Gated security suites (FIPS off)" \
    env "${GATES[@]}" "$VENV_PY" -m pytest "${GATED_FILES[@]}" -o addopts="" -n auto --dist loadgroup -q --tb=short
run_phase "Gated security suites (FIPS_MODE=true)" \
    env "${GATES[@]}" FIPS_MODE=true "$VENV_PY" -m pytest "${GATED_FILES[@]}" -o addopts="" -n auto --dist loadgroup -q --tb=short

# 3. Integration-marked tests (need the live stack)
#
# Collected from the paths that hold them, not all of tests/: there are 20 such tests and
# sweeping the 5,200-test tree to find them cost ~23 s of pure collection. Deliberately still
# SERIAL — `-o addopts=""` drops the inherited `-n auto`, which is correct here because these
# talk to the live stack and share its state (uploads, reprocessing, mirror state); running
# them concurrently would make them interfere rather than faster.
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
    run_phase_watching_skips "Integration-marked tests" \
        "$VENV_PY" -m pytest tests/integration/ tests/test_selective_reprocess.py tests/eval/ \
        -o addopts="" -m integration -q --tb=short --timeout="${INTEGRATION_TEST_TIMEOUT:-900}"
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
# ⚠️ This phase runs in the VENV, so the three container-only diarization suites
# (test_diarizer_lifecycle / test_diarization_perf_gates / test_diarization_regression)
# report as SKIPS here, not passes: their `ensure_container` fixture needs /.dockerenv,
# and their audio/RTTM fixtures live at /app paths that only exist inside the benchmark
# container. They have their own entry point — see the pointer printed below.
if $RUN_GPU; then
    run_phase "GPU-marked tests" \
        "$VENV_PY" -m pytest tests/ -o addopts="" -m gpu -q --tb=short \
        --timeout="${GPU_TEST_TIMEOUT:-1800}"

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
# Its two tests spawn ./scripts/check-schema-drift.py, which resolves the DB from the repo
# root, so this phase runs from anywhere the rest of the gate does.
run_phase "Model-vs-schema drift" \
    env RUN_SCHEMA_DRIFT_TESTS=true "$VENV_PY" -m pytest tests/unit/test_schema_drift.py \
    -o addopts="" -q --tb=short

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
run_phase "Collection determinism (two processes, same test ids)" \
    bash -c '
        set -uo pipefail
        a=$(mktemp) && b=$(mktemp)
        trap "rm -f $a $b" EXIT
        "'"$VENV_PY"'" -m pytest --collect-only -q -o addopts= -p no:cacheprovider \
            2>/dev/null | grep "::" | sort > "$a"
        "'"$VENV_PY"'" -m pytest --collect-only -q -o addopts= -p no:cacheprovider \
            2>/dev/null | grep "::" | sort > "$b"
        if [[ ! -s $a ]]; then
            echo "collected nothing — the probe did not run" >&2
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
if $SEARCH_QUALITY; then
    run_phase "Search quality harness (corpus-dependent)" \
        env RUN_SEARCH_QUALITY_TESTS=true "$VENV_PY" -m pytest tests/test_search_quality.py -o addopts="" -q --tb=short
fi

# 9. Optional: browser smoke tests against the live stack
if $E2E_SMOKE; then
    run_phase "E2E smoke (browser)" \
        "$VENV_PY" -m pytest tests/e2e/test_settings_modal.py tests/e2e/test_a11y.py \
            tests/e2e/test_file_detail_transcript.py tests/e2e/test_media_download.py -q --tb=short
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
    else
        echo -e "${GREEN}All selected phases passed.${NC}"
    fi
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
