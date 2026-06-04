#!/bin/bash
# OpenTranscribe — canonical local test gate (issue #21)
#
# Runs the COMPLETE backend test suite against the live dev stack:
#   1. ungated unit/API tests (includes S3/OpenSearch tests via auto-detection)
#   2. the security-gated suites (RUN_* env vars) in both FIPS modes
#   3. integration-marked tests (-m integration)
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
for arg in "$@"; do
    case "$arg" in
        --coverage) COVERAGE=true ;;
        --e2e-smoke) E2E_SMOKE=true ;;
        --search-quality) SEARCH_QUALITY=true ;;
        --cleanup) CLEANUP=true ;;
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

GATES=(RUN_PKI_TESTS=true RUN_MFA_TESTS=true RUN_LLM_TESTS=true
       RUN_FEDRAMP_TESTS=true RUN_FIPS_TESTS=true
       RUN_AUTH_CONFIG_TESTS=true RUN_ADVANCED_ADMIN_TESTS=true)
GATED_FILES=(tests/test_pki_auth.py tests/test_mfa_security.py
             tests/test_llm_settings.py tests/test_fedramp_compliance.py
             tests/test_fedramp_controls.py tests/test_fips_140_3.py
             tests/test_auth_config_service.py tests/test_admin_security.py
             tests/test_admin_endpoints.py)

FAILED_PHASES=()
run_phase() {
    local title=$1; shift
    echo -e "${BLUE}--- $title ---${NC}"
    if "$@"; then
        echo -e "${GREEN}✓ $title passed${NC}\n"
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
run_phase "Integration-marked tests" \
    "$VENV_PY" -m pytest tests/ -o addopts="" -m integration -q --tb=short

# 4. Optional: corpus-dependent search relevance harness
if $SEARCH_QUALITY; then
    run_phase "Search quality harness (corpus-dependent)" \
        env RUN_SEARCH_QUALITY_TESTS=true "$VENV_PY" -m pytest tests/test_search_quality.py -o addopts="" -q --tb=short
fi

# 5. Optional: browser smoke tests against the live stack
if $E2E_SMOKE; then
    run_phase "E2E smoke (browser)" \
        "$VENV_PY" -m pytest tests/e2e/test_settings_modal.py tests/e2e/test_a11y.py \
            tests/e2e/test_file_detail_transcript.py tests/e2e/test_media_download.py -q --tb=short
fi

# 6. Optional: orphaned test-user report (dry run — pass --execute manually to apply)
if $CLEANUP; then
    echo -e "${BLUE}--- Orphaned test users (dry run) ---${NC}"
    "$VENV_PY" "$PROJECT_ROOT/scripts/cleanup-test-users.py" || true
    echo ""
fi

# --- Summary -----------------------------------------------------------------
echo -e "${BLUE}========================================${NC}"
if [ ${#FAILED_PHASES[@]} -eq 0 ]; then
    echo -e "${GREEN}All selected phases passed.${NC}"
    exit 0
else
    echo -e "${RED}Failed phases:${NC}"
    for phase in "${FAILED_PHASES[@]}"; do echo -e "  ${RED}✗ $phase${NC}"; done
    exit 1
fi
