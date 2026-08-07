#!/bin/bash
# shellcheck disable=SC2034
# SC2034: RESULT_* and COUNT_* variables are used via indirect expansion in print_summary()
#
# Master Auth E2E Test Runner
#
# Runs all authentication E2E tests — Local, LDAP/AD, Keycloak/OIDC, PKI, MFA —
# with zero human intervention. Starts containers, creates users, configures auth,
# runs Playwright browser tests, reports results, cleans up.
#
# Usage:
#   ./scripts/run-auth-e2e.sh                   # Run all tests
#   ./scripts/run-auth-e2e.sh --headed          # Show browser on XRDP
#   ./scripts/run-auth-e2e.sh --skip-pki        # Skip PKI tests
#   ./scripts/run-auth-e2e.sh --cleanup         # Remove test containers after
#   ./scripts/run-auth-e2e.sh --help            # Show all options
#
# Prerequisites:
#   - Dev environment running: ./opentr.sh start dev
#   - Python venv with playwright, pyotp: source backend/venv/bin/activate
#   - ldap-utils package installed: sudo apt install ldap-utils
#

set -euo pipefail

# ============================================================================
# Configuration
# ============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_URL="${E2E_BACKEND_URL:-http://localhost:5174}"
FRONTEND_URL="${E2E_FRONTEND_URL:-http://localhost:5173}"
PKI_URL="${PKI_E2E_URL:-https://localhost:5182}"
ADMIN_EMAIL="admin@example.com"
ADMIN_PASSWORD="password"
PKI_CONTAINER_NAME="opentranscribe-frontend-pki"
PKI_IMAGE_TAG="opentranscribe-frontend:pki-test"
VENV_PATH="${PROJECT_ROOT}/backend/venv"
TESTS_DIR="${PROJECT_ROOT}/backend/tests/e2e"
SCREENSHOT_DIR="${TESTS_DIR}/screenshots"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Phase results (exit codes)
RESULT_auth_buttons="SKIP"
RESULT_ldap_keycloak="SKIP"
RESULT_pki="SKIP"
RESULT_mfa="SKIP"
COUNT_auth_buttons=0
COUNT_ldap_keycloak=0
COUNT_pki=0
COUNT_mfa=0

# Flags
SKIP_AUTH_BUTTONS=false
SKIP_LDAP=false
SKIP_PKI=false
SKIP_MFA=false
HEADED=false
DISPLAY_VAL=":13"
DO_CLEANUP=false
VERBOSE=false
DEV_FRONTEND_STOPPED=false

# ============================================================================
# Argument parsing
# ============================================================================
show_help() {
    cat << 'EOF'
Usage: scripts/run-auth-e2e.sh [OPTIONS]

Runs all authentication E2E browser tests automatically.

Options:
  --skip-auth-buttons    Skip basic auth buttons tests
  --skip-ldap            Skip LDAP/Keycloak tests
  --skip-pki             Skip PKI/certificate tests
  --skip-mfa             Skip MFA/TOTP tests
  --headed               Show browser window (for XRDP debugging)
  --display=:N           Set DISPLAY for headed mode (default: :13)
  --cleanup              Remove LLDAP/Keycloak containers after tests
  --verbose              Full pytest output (-v --tb=long)
  --help                 Show this help message

Environment Variables:
  E2E_FRONTEND_URL       Frontend URL (default: http://localhost:5173)
  E2E_BACKEND_URL        Backend URL (default: http://localhost:5174)
  PKI_E2E_URL            PKI HTTPS URL (default: https://localhost:5182)

Examples:
  ./scripts/run-auth-e2e.sh                        # Run all tests
  ./scripts/run-auth-e2e.sh --headed               # Watch tests in browser
  ./scripts/run-auth-e2e.sh --skip-pki --skip-mfa  # Just local + LDAP/KC
  ./scripts/run-auth-e2e.sh --cleanup              # Tear down after tests
EOF
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --skip-auth-buttons) SKIP_AUTH_BUTTONS=true ;;
        --skip-ldap)         SKIP_LDAP=true ;;
        --skip-pki)          SKIP_PKI=true ;;
        --skip-mfa)          SKIP_MFA=true ;;
        --headed)            HEADED=true ;;
        --display=*)         DISPLAY_VAL="${arg#--display=}" ;;
        --cleanup)           DO_CLEANUP=true ;;
        --verbose)           VERBOSE=true ;;
        --help|-h)           show_help ;;
        *)                   echo -e "${RED}Unknown option: $arg${NC}"; show_help ;;
    esac
done

# Build pytest args
PYTEST_ARGS="-v"
if [ "$HEADED" = true ]; then
    export DISPLAY="$DISPLAY_VAL"
    PYTEST_ARGS="$PYTEST_ARGS --headed"
fi
if [ "$VERBOSE" = true ]; then
    PYTEST_ARGS="$PYTEST_ARGS --tb=long"
else
    PYTEST_ARGS="$PYTEST_ARGS --tb=short"
fi

# ============================================================================
# Helper functions
# ============================================================================
log_phase() { echo -e "\n${BOLD}${BLUE}════════════════════════════════════════════════════════════════${NC}"; echo -e "${BOLD}${BLUE}  $1${NC}"; echo -e "${BOLD}${BLUE}════════════════════════════════════════════════════════════════${NC}\n"; }
log_step()  { echo -e "${CYAN}  → $1${NC}"; }
log_ok()    { echo -e "${GREEN}  ✓ $1${NC}"; }
log_warn()  { echo -e "${YELLOW}  ⚠ $1${NC}"; }
log_err()   { echo -e "${RED}  ✗ $1${NC}"; }

port_open() {
    local host="${2:-localhost}"
    timeout 2 bash -c "cat < /dev/null > /dev/tcp/$host/$1" 2>/dev/null
}

wait_for_port() {
    local port=$1 max_wait=$2 label=$3
    log_step "Waiting for $label (port $port)..."
    for _i in $(seq 1 "$max_wait"); do
        if port_open "$port"; then
            log_ok "$label is ready"
            return 0
        fi
        sleep 1
    done
    log_err "$label failed to start within ${max_wait}s"
    return 1
}

get_admin_token() {
    local resp
    resp=$(curl -sf -X POST "${BACKEND_URL}/api/auth/token" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "username=${ADMIN_EMAIL}&password=${ADMIN_PASSWORD}" 2>/dev/null) || return 1

    # Check if MFA is required
    if echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('mfa_required') else 1)" 2>/dev/null; then
        log_warn "Admin login requires MFA — resetting via docker exec"
        reset_admin_mfa_db
        # Retry login
        resp=$(curl -sf -X POST "${BACKEND_URL}/api/auth/token" \
            -H "Content-Type: application/x-www-form-urlencoded" \
            -d "username=${ADMIN_EMAIL}&password=${ADMIN_PASSWORD}" 2>/dev/null) || return 1
    fi

    echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
}

restore_admin_local_auth() {
    # Restore admin user to local auth type after PKI tests convert it
    docker exec opentranscribe-backend python3 -c "
import os, sys
sys.path.insert(0, '/app')
os.environ.setdefault('POSTGRES_HOST', 'postgres')
from passlib.context import CryptContext
from app.db.base import SessionLocal
from app.models.user import User
pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')
db = SessionLocal()
user = db.query(User).filter(User.email=='${ADMIN_EMAIL}').first()
if user and user.auth_type != 'local':
    user.auth_type = 'local'
    user.hashed_password = pwd_context.hash('${ADMIN_PASSWORD}')
    db.commit()
    print('Admin restored to local auth')
elif user:
    print('Admin already has local auth')
else:
    print('Admin user not found')
db.close()
" 2>/dev/null || log_warn "Could not restore admin to local auth"
}

reset_admin_mfa_db() {
    # Reset MFA for admin user and clean up MFA test users
    docker exec opentranscribe-backend python3 -c "
import os, sys
sys.path.insert(0, '/app')
os.environ.setdefault('POSTGRES_HOST', 'postgres')
from app.db.base import SessionLocal
from app.models.user import User
from app.models.user_mfa import UserMFA
db = SessionLocal()
# Reset admin MFA
user = db.query(User).filter(User.email=='${ADMIN_EMAIL}').first()
if user:
    mfa = db.query(UserMFA).filter(UserMFA.user_id==user.id).first()
    if mfa:
        db.delete(mfa)
        print('MFA deleted for admin user')
# Clean up MFA E2E test users
for u in db.query(User).filter(User.email.like('mfa-e2e-%')).all():
    mfa = db.query(UserMFA).filter(UserMFA.user_id==u.id).first()
    if mfa: db.delete(mfa)
    db.delete(u)
    print(f'Cleaned up test user {u.email}')
db.commit()
db.close()
" 2>/dev/null || log_warn "Could not reset MFA state via docker exec"
}

extract_test_counts() {
    # Parse pytest output for pass/fail counts
    # Returns "passed" count from last line like "X passed, Y failed in Zs"
    local output="$1"
    echo "$output" | grep -oP '\d+ passed' | grep -oP '\d+' | tail -1
}

# ============================================================================
# Cleanup trap — always runs on exit
# ============================================================================
cleanup_on_exit() {
    local exit_code=$?

    echo ""
    log_phase "Cleanup"

    # 1. Remove PKI frontend container if still running
    if docker ps -q --filter "name=${PKI_CONTAINER_NAME}" 2>/dev/null | grep -q .; then
        log_step "Stopping PKI frontend container..."
        docker stop "${PKI_CONTAINER_NAME}" 2>/dev/null
        docker rm "${PKI_CONTAINER_NAME}" 2>/dev/null
        log_ok "PKI container removed"
    fi

    # 2. Restart dev frontend if it was stopped
    if [ "$DEV_FRONTEND_STOPPED" = true ]; then
        log_step "Restarting dev frontend..."
        cd "$PROJECT_ROOT" && docker compose start frontend 2>/dev/null
        log_ok "Dev frontend restarted"
    fi

    # 3. Restore auth config defaults
    local token
    token=$(get_admin_token 2>/dev/null) || true
    if [ -n "$token" ]; then
        # Disable PKI
        curl -sf -X PUT "${BACKEND_URL}/api/admin/auth-config/pki" \
            -H "Authorization: Bearer $token" \
            -H "Content-Type: application/json" \
            -d '{"pki_enabled": "false"}' >/dev/null 2>&1 || true

        # Disable MFA
        curl -sf -X PUT "${BACKEND_URL}/api/admin/auth-config/mfa" \
            -H "Authorization: Bearer $token" \
            -H "Content-Type: application/json" \
            -d '{"mfa_enabled": "false", "mfa_required": "false"}' >/dev/null 2>&1 || true

        log_ok "Auth config restored to defaults"
    fi

    # 4. Reset admin MFA state and restore local auth
    reset_admin_mfa_db
    restore_admin_local_auth

    # 5. Optional: remove LLDAP/Keycloak containers
    if [ "$DO_CLEANUP" = true ]; then
        log_step "Removing LLDAP and Keycloak containers..."
        cd "$PROJECT_ROOT"
        docker compose -f docker-compose.ldap-test.yml down 2>/dev/null || true
        docker compose -f docker-compose.keycloak.yml down 2>/dev/null || true
        log_ok "Test containers removed"
    fi

    # 6. Print summary
    print_summary

    exit $exit_code
}

trap cleanup_on_exit EXIT

# ============================================================================
# Phase 0: Prerequisites
# ============================================================================
check_prerequisites() {
    log_phase "Phase 0: Prerequisites Check"

    # Docker running
    log_step "Checking Docker..."
    if ! docker info >/dev/null 2>&1; then
        log_err "Docker is not running"
        exit 1
    fi
    log_ok "Docker is running"

    # Dev environment up
    log_step "Checking dev environment..."
    if ! curl -sf "${BACKEND_URL}/api/auth/methods" >/dev/null 2>&1; then
        log_err "Backend not responding at ${BACKEND_URL}"
        echo "  Start dev environment first: ./opentr.sh start dev"
        exit 1
    fi
    log_ok "Backend is running at ${BACKEND_URL}"

    if ! port_open 5173; then
        log_err "Frontend not responding at port 5173"
        exit 1
    fi
    log_ok "Frontend is running at ${FRONTEND_URL}"

    # Docker network
    log_step "Checking Docker network..."
    if ! docker network inspect transcribe-app_default >/dev/null 2>&1; then
        log_err "Docker network 'transcribe-app_default' does not exist"
        exit 1
    fi
    log_ok "Docker network exists"

    # Python venv
    log_step "Checking Python virtual environment..."
    if [ ! -f "${VENV_PATH}/bin/activate" ]; then
        log_err "Python venv not found at ${VENV_PATH}"
        exit 1
    fi
    # shellcheck disable=SC1091
    source "${VENV_PATH}/bin/activate"
    log_ok "Python venv activated"

    # Playwright
    log_step "Checking Playwright..."
    if ! python3 -c "import playwright" 2>/dev/null; then
        log_err "Playwright not installed. Run: pip install pytest-playwright && playwright install chromium"
        exit 1
    fi
    log_ok "Playwright available"

    # ldap-utils (needed for LDAP tests)
    if [ "$SKIP_LDAP" = false ]; then
        log_step "Checking ldap-utils..."
        if ! command -v ldappasswd >/dev/null 2>&1; then
            log_err "ldappasswd not found. Run: sudo apt install ldap-utils"
            exit 1
        fi
        log_ok "ldap-utils installed"
    fi

    # pyotp (needed for MFA tests)
    if [ "$SKIP_MFA" = false ]; then
        log_step "Checking pyotp..."
        if ! python3 -c "import pyotp" 2>/dev/null; then
            log_err "pyotp not installed. Run: pip install pyotp"
            exit 1
        fi
        log_ok "pyotp available"
    fi

    # PKI certificates
    if [ "$SKIP_PKI" = false ]; then
        log_step "Checking PKI certificates..."
        if [ ! -f "${PROJECT_ROOT}/scripts/pki/test-certs/ca/ca.crt" ]; then
            log_warn "Test CA not found, generating PKI certificates..."
            "${PROJECT_ROOT}/scripts/pki/setup-test-pki.sh"
        fi
        if [ ! -f "${PROJECT_ROOT}/scripts/pki/test-certs/nginx/server.key" ]; then
            log_warn "Server cert not found, generating..."
            openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
                -keyout "${PROJECT_ROOT}/scripts/pki/test-certs/nginx/server.key" \
                -out "${PROJECT_ROOT}/scripts/pki/test-certs/nginx/server.crt" \
                -subj "/CN=localhost" 2>/dev/null
        fi
        log_ok "PKI certificates ready"
    fi

    # Create screenshot directory
    mkdir -p "$SCREENSHOT_DIR"

    echo ""
    log_ok "All prerequisites satisfied"
}

# ============================================================================
# Phase 1: Auth Buttons
# ============================================================================
phase_1_auth_buttons() {
    log_phase "Phase 1: Auth Buttons & Local Login"

    local output
    if output=$(python3 -m pytest "${TESTS_DIR}/test_auth_buttons.py" $PYTEST_ARGS \
        --screenshot only-on-failure --output="$SCREENSHOT_DIR" 2>&1); then
        RESULT_auth_buttons=0
        COUNT_auth_buttons=$(extract_test_counts "$output")
        log_ok "Auth buttons tests passed (${COUNT_auth_buttons:-?} tests)"
    else
        RESULT_auth_buttons=$?
        COUNT_auth_buttons=$(extract_test_counts "$output")
        log_err "Auth buttons tests failed"
        if [ "$VERBOSE" = true ]; then echo "$output"; fi
    fi
}

# ============================================================================
# Phase 2: LDAP + Keycloak
# ============================================================================
phase_2_ldap_keycloak() {
    log_phase "Phase 2: LDAP/AD & Keycloak/OIDC"

    # Pre-start LLDAP if not running
    if ! port_open 3890; then
        log_step "Starting LLDAP container..."
        cd "$PROJECT_ROOT" && docker compose -f docker-compose.ldap-test.yml up -d 2>/dev/null
        wait_for_port 3890 30 "LLDAP (LDAP)" || return 1
        wait_for_port 17170 30 "LLDAP (Web UI)" || return 1
    else
        log_ok "LLDAP already running"
    fi

    # Pre-start Keycloak if not running
    if ! port_open 8180; then
        log_step "Starting Keycloak container..."
        cd "$PROJECT_ROOT" && docker compose -f docker-compose.keycloak.yml up -d keycloak 2>/dev/null
        wait_for_port 8180 120 "Keycloak" || return 1
        # Extra wait for Keycloak to fully initialize
        sleep 5
    else
        log_ok "Keycloak already running"
    fi

    # Run LDAP/Keycloak E2E tests
    # The test file's session-scoped fixtures handle user creation and auth config
    local output
    if output=$(RUN_AUTH_E2E=true python3 -m pytest "${TESTS_DIR}/test_ldap_oidc.py" $PYTEST_ARGS \
        --screenshot only-on-failure --output="$SCREENSHOT_DIR" 2>&1); then
        RESULT_ldap_keycloak=0
        COUNT_ldap_keycloak=$(extract_test_counts "$output")
        log_ok "LDAP/Keycloak tests passed (${COUNT_ldap_keycloak:-?} tests)"
    else
        RESULT_ldap_keycloak=$?
        COUNT_ldap_keycloak=$(extract_test_counts "$output")
        log_err "LDAP/Keycloak tests failed"
        if [ "$VERBOSE" = true ]; then echo "$output"; fi
    fi
}

# ============================================================================
# Phase 3: PKI
# ============================================================================
phase_3_pki() {
    log_phase "Phase 3: PKI/X.509 Certificate Authentication"

    # Step 1: Build frontend prod image from source (nginx-based, required for mTLS)
    # Always build from current source to ensure JS assets match the running frontend code
    log_step "Building frontend production image for PKI (from source)..."
    if ! docker build -t "${PKI_IMAGE_TAG}" \
        -f "${PROJECT_ROOT}/frontend/Dockerfile.prod" \
        "${PROJECT_ROOT}/frontend/"; then
        log_err "Failed to build frontend production image"
        RESULT_pki=1
        return 1
    fi
    log_ok "Frontend production image built"

    # Step 2: Stop the dev frontend container
    log_step "Stopping dev frontend for PKI overlay..."
    cd "$PROJECT_ROOT" && docker compose stop frontend 2>/dev/null
    DEV_FRONTEND_STOPPED=true
    log_ok "Dev frontend stopped"

    # Step 3: Remove any previous PKI container
    docker stop "${PKI_CONTAINER_NAME}" 2>/dev/null || true
    docker rm "${PKI_CONTAINER_NAME}" 2>/dev/null || true

    # Step 4: Run temporary PKI frontend container on the main network
    log_step "Starting PKI frontend container with mTLS..."
    if ! docker run -d \
        --name "${PKI_CONTAINER_NAME}" \
        --network transcribe-app_default \
        -v "${PROJECT_ROOT}/frontend/nginx-pki.conf:/etc/nginx/conf.d/default.conf:ro" \
        -v "${PROJECT_ROOT}/scripts/pki/test-certs/nginx/server.crt:/etc/nginx/certs/server.crt:ro" \
        -v "${PROJECT_ROOT}/scripts/pki/test-certs/nginx/server.key:/etc/nginx/certs/server.key:ro" \
        -v "${PROJECT_ROOT}/scripts/pki/test-certs/ca/ca.crt:/etc/nginx/certs/ca.crt:ro" \
        -p 5182:8443 \
        -p 5183:8080 \
        "${PKI_IMAGE_TAG}" 2>/dev/null; then
        log_err "Failed to start PKI frontend container"
        RESULT_pki=1
        return 1
    fi
    wait_for_port 5182 30 "PKI Frontend (HTTPS)" || { RESULT_pki=1; return 1; }

    # Step 5: Enable PKI in backend auth config with admin DN
    log_step "Enabling PKI authentication in backend..."
    local token
    token=$(get_admin_token) || { log_err "Failed to get admin token"; RESULT_pki=1; return 1; }

    # Get admin cert DN for PKI_ADMIN_DNS
    local admin_dn
    admin_dn=$(openssl x509 -in "${PROJECT_ROOT}/scripts/pki/test-certs/clients/admin.crt" -noout -subject -nameopt RFC2253 2>/dev/null | sed 's/^subject=//')

    curl -sf -X PUT "${BACKEND_URL}/api/admin/auth-config/pki" \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -d "{\"pki_enabled\": \"true\", \"pki_admin_dns\": \"${admin_dn}\"}" >/dev/null 2>&1
    log_ok "PKI enabled in backend (admin DN: ${admin_dn})"

    # Step 6: Run PKI E2E tests
    local output
    if output=$(RUN_PKI_E2E=true PKI_E2E_URL="${PKI_URL}" \
        python3 -m pytest "${TESTS_DIR}/test_pki.py" $PYTEST_ARGS \
        --screenshot only-on-failure --output="$SCREENSHOT_DIR" 2>&1); then
        RESULT_pki=0
        COUNT_pki=$(extract_test_counts "$output")
        log_ok "PKI tests passed (${COUNT_pki:-?} tests)"
    else
        RESULT_pki=$?
        COUNT_pki=$(extract_test_counts "$output")
        log_err "PKI tests failed"
        if [ "$VERBOSE" = true ]; then echo "$output"; fi
    fi

    # Step 7: Restore — PKI container cleanup and frontend restart happen in cleanup_on_exit
    log_step "Stopping PKI frontend container..."
    docker stop "${PKI_CONTAINER_NAME}" 2>/dev/null
    docker rm "${PKI_CONTAINER_NAME}" 2>/dev/null

    log_step "Restarting dev frontend..."
    cd "$PROJECT_ROOT" && docker compose start frontend 2>/dev/null
    DEV_FRONTEND_STOPPED=false
    wait_for_port 5173 30 "Dev Frontend" || true

    # Restore admin to local auth (PKI tests convert admin user)
    log_step "Restoring admin to local auth..."
    restore_admin_local_auth

    # Disable PKI
    token=$(get_admin_token 2>/dev/null) || true
    if [ -n "$token" ]; then
        curl -sf -X PUT "${BACKEND_URL}/api/admin/auth-config/pki" \
            -H "Authorization: Bearer $token" \
            -H "Content-Type: application/json" \
            -d '{"pki_enabled": "false"}' >/dev/null 2>&1 || true
    fi
    log_ok "Dev environment restored"
}

# ============================================================================
# Phase 4: MFA
# ============================================================================
phase_4_mfa() {
    log_phase "Phase 4: MFA/TOTP Multi-Factor Authentication"

    # Step 1: Reset admin MFA state (clean slate)
    log_step "Resetting admin MFA state..."
    reset_admin_mfa_db

    # Step 2: Enable MFA globally via admin API
    log_step "Enabling MFA globally..."
    local token
    token=$(get_admin_token) || { log_err "Failed to get admin token"; RESULT_mfa=1; return 1; }
    curl -sf -X PUT "${BACKEND_URL}/api/admin/auth-config/mfa" \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -d '{"mfa_enabled": "true", "mfa_required": "false"}' >/dev/null 2>&1
    log_ok "MFA enabled globally"

    # Step 3: Run MFA E2E tests
    local output
    if output=$(python3 -m pytest "${TESTS_DIR}/test_mfa.py" $PYTEST_ARGS \
        --screenshot only-on-failure --output="$SCREENSHOT_DIR" 2>&1); then
        RESULT_mfa=0
        COUNT_mfa=$(extract_test_counts "$output")
        log_ok "MFA tests passed (${COUNT_mfa:-?} tests)"
    else
        RESULT_mfa=$?
        COUNT_mfa=$(extract_test_counts "$output")
        log_err "MFA tests failed"
        if [ "$VERBOSE" = true ]; then echo "$output"; fi
    fi

    # Step 4: Cleanup — disable MFA and reset admin state
    log_step "Disabling MFA and resetting admin state..."
    reset_admin_mfa_db
    token=$(get_admin_token 2>/dev/null) || true
    if [ -n "$token" ]; then
        curl -sf -X PUT "${BACKEND_URL}/api/admin/auth-config/mfa" \
            -H "Authorization: Bearer $token" \
            -H "Content-Type: application/json" \
            -d '{"mfa_enabled": "false", "mfa_required": "false"}' >/dev/null 2>&1 || true
    fi
    log_ok "MFA disabled and admin state reset"
}

# ============================================================================
# Result summary
# ============================================================================
print_summary() {
    echo ""
    echo -e "${BOLD}================================================================${NC}"
    echo -e "${BOLD}  Authentication E2E Test Summary${NC}"
    echo -e "${BOLD}================================================================${NC}"
    echo ""

    local total_passed=0
    local any_failed=false

    for phase in auth_buttons ldap_keycloak pki mfa; do
        local result_var="RESULT_${phase}"
        local count_var="COUNT_${phase}"
        local result="${!result_var}"
        local count="${!count_var}"

        case "$result" in
            0)
                printf "  ${GREEN}%-22s PASSED (%s tests)${NC}\n" "$phase" "${count:-?}"
                total_passed=$((total_passed + ${count:-0}))
                ;;
            SKIP)
                printf "  ${YELLOW}%-22s SKIPPED${NC}\n" "$phase"
                ;;
            *)
                printf "  ${RED}%-22s FAILED (exit=$result)${NC}\n" "$phase"
                any_failed=true
                ;;
        esac
    done

    echo ""
    if [ "$any_failed" = true ]; then
        echo -e "  ${RED}Result: SOME TESTS FAILED${NC}"
    elif [ $total_passed -gt 0 ]; then
        echo -e "  ${GREEN}Result: ALL TESTS PASSED ($total_passed total)${NC}"
    else
        echo -e "  ${YELLOW}Result: ALL TESTS SKIPPED${NC}"
    fi
    echo ""
    echo "  Screenshots: ${SCREENSHOT_DIR}/"
    echo -e "${BOLD}================================================================${NC}"
    echo ""
}

# ============================================================================
# Main execution
# ============================================================================
main() {
    echo -e "\n${BOLD}${GREEN}================================================================${NC}"
    echo -e "${BOLD}${GREEN}  OpenTranscribe Auth E2E Test Suite${NC}"
    echo -e "${BOLD}${GREEN}================================================================${NC}"
    echo ""
    echo -e "  Phases:"
    [ "$SKIP_AUTH_BUTTONS" = false ] && echo "    1. Auth Buttons & Local Login" || echo -e "    ${YELLOW}1. Auth Buttons (SKIP)${NC}"
    [ "$SKIP_LDAP" = false ]         && echo "    2. LDAP/AD & Keycloak/OIDC"   || echo -e "    ${YELLOW}2. LDAP/Keycloak (SKIP)${NC}"
    [ "$SKIP_PKI" = false ]          && echo "    3. PKI/X.509 Certificates"    || echo -e "    ${YELLOW}3. PKI (SKIP)${NC}"
    [ "$SKIP_MFA" = false ]          && echo "    4. MFA/TOTP Authentication"   || echo -e "    ${YELLOW}4. MFA (SKIP)${NC}"
    [ "$HEADED" = true ]             && echo -e "\n  Mode: ${CYAN}Headed (DISPLAY=$DISPLAY_VAL)${NC}" || echo -e "\n  Mode: Headless"
    echo ""

    check_prerequisites

    # Phase 1
    if [ "$SKIP_AUTH_BUTTONS" = false ]; then
        phase_1_auth_buttons
    fi

    # Phase 2
    if [ "$SKIP_LDAP" = false ]; then
        phase_2_ldap_keycloak
    fi

    # Phase 3
    if [ "$SKIP_PKI" = false ]; then
        phase_3_pki
    fi

    # Phase 4
    if [ "$SKIP_MFA" = false ]; then
        phase_4_mfa
    fi

    # Summary is printed by cleanup_on_exit trap
    # Determine exit code
    local any_failed=false
    for phase in auth_buttons ldap_keycloak pki mfa; do
        local result_var="RESULT_${phase}"
        local result="${!result_var}"
        if [ "$result" != "0" ] && [ "$result" != "SKIP" ]; then
            any_failed=true
        fi
    done

    if [ "$any_failed" = true ]; then
        exit 1
    fi
    exit 0
}

main "$@"
