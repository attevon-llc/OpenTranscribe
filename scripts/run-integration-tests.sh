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
if port_open 5178; then echo -e "MinIO:      ${GREEN}up (5178) — S3 tests enabled${NC}"; else echo -e "MinIO:      ${YELLOW}down — S3 tests will skip${NC}"; fi
if port_open 5180; then echo -e "OpenSearch: ${GREEN}up (5180) — search tests enabled${NC}"; else echo -e "OpenSearch: ${YELLOW}down — search tests will skip${NC}"; fi
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
run_phase "Integration-marked tests" \
    "$VENV_PY" -m pytest tests/integration/ tests/test_selective_reprocess.py \
    -o addopts="" -m integration -q --tb=short

# 4. GPU-marked tests. Deselected from the fast suite and from CI (both CPU-only), so
# this gate is the ONLY place they run — they were silently ungated before #297.
# Each module still carries its own runtime skip guard, so this is a no-op on a
# machine without CUDA; pass --skip-gpu to drop the phase entirely.
if $RUN_GPU; then
    run_phase "GPU-marked tests" \
        "$VENV_PY" -m pytest tests/ -o addopts="" -m gpu -q --tb=short
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
