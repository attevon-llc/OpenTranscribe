#!/bin/bash
# FedRAMP Compliance Testing Script
# Tests all authentication and security features

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
API_BASE="${API_BASE:-http://localhost:5174/api}"
# A registered throwaway shape (cleanup-test-users.py ORPHAN_PATTERNS_UNAMBIGUOUS): this script
# registers a real account and never deletes it, so the sweep is its only cleanup. The old
# fedramp-test-<ts> shape matched no pattern at all (issue #1195).
TEST_EMAIL="fedramp-e2e-$(date +%s)@example.com"
TEST_PASSWORD="SecureP@ssw0rd123!"
REJECTED_PW="password123"

# Store tokens
ACCESS_TOKEN=""
REFRESH_TOKEN=""
# shellcheck disable=SC2034  # MFA_TOKEN reserved for future MFA flow tests
MFA_TOKEN=""

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  FedRAMP Compliance Testing Suite${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo "API Base: $API_BASE"
echo "Test Email: $TEST_EMAIL"
echo ""

# Helper function for API calls
api_call() {
    local method=$1
    local endpoint=$2
    local data=$3
    local auth_header=$4

    local curl_opts=(-s -w "\n%{http_code}" -X "$method" "${API_BASE}${endpoint}")
    curl_opts+=(-H "Content-Type: application/json")

    if [ -n "$auth_header" ]; then
        curl_opts+=(-H "Authorization: Bearer $auth_header")
    fi

    if [ -n "$data" ]; then
        curl_opts+=(-d "$data")
    fi

    curl "${curl_opts[@]}"
}

# Parse response and status code
parse_response() {
    local response=$1
    local body
    local status
    body=$(echo "$response" | head -n -1)
    status=$(echo "$response" | tail -n 1)
    echo "$body"
    return $((status < 200 || status >= 300 ? 1 : 0))
}

section() {
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  $1${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

test_pass() {
    echo -e "${GREEN}✓ PASS:${NC} $1"
}

test_fail() {
    echo -e "${RED}✗ FAIL:${NC} $1"
}

test_skip() {
    echo -e "${YELLOW}○ SKIP:${NC} $1"
}

test_info() {
    echo -e "${BLUE}ℹ INFO:${NC} $1"
}

# ============================================================================
# Phase 1: Password Policy Tests
# ============================================================================
section "Phase 1 & 2: Password Policy"

# Test 1.1: Get password policy requirements
echo "Testing: GET /auth/password-policy"
response=$(api_call GET "/auth/password-policy")
if echo "$response" | grep -q "min_length"; then
    test_pass "Password policy endpoint returns requirements"
    echo "$response" | head -n -1 | jq . 2>/dev/null || echo "$response"
else
    test_fail "Password policy endpoint failed"
fi

# Test 1.2: Register with weak password (should fail)
echo ""
echo "Testing: Registration with weak password"
response=$(api_call POST "/auth/register" "{\"email\":\"weak@test.com\",\"password\":\"$REJECTED_PW\"}")
status=$(echo "$response" | tail -n 1)
if [ "$status" == "422" ] || [ "$status" == "400" ]; then
    test_pass "Weak password rejected (status: $status)"
else
    test_fail "Weak password was accepted (status: $status)"
fi

# Test 1.3: Register with strong password (should succeed)
echo ""
echo "Testing: Registration with strong password"
response=$(api_call POST "/auth/register" "{\"email\":\"$TEST_EMAIL\",\"password\":\"$TEST_PASSWORD\",\"full_name\":\"Test User\"}")
status=$(echo "$response" | tail -n 1)
if [ "$status" == "200" ] || [ "$status" == "201" ]; then
    test_pass "Strong password accepted"
else
    test_fail "Strong password rejected (status: $status)"
    echo "$response" | head -n -1
fi

# ============================================================================
# Phase 3: Authentication & Token Tests
# ============================================================================
section "Phase 3 & 4: Authentication & Tokens"

# Test 3.1: Login and get tokens
echo "Testing: Login with credentials"
response=$(api_call POST "/auth/token" "username=$TEST_EMAIL&password=$TEST_PASSWORD" "" | sed 's/username=//' | sed 's/password=//')

# Use form data for OAuth2 token endpoint
response=$(curl -s -w "\n%{http_code}" -X POST "${API_BASE}/auth/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=$TEST_EMAIL&password=$TEST_PASSWORD")

status=$(echo "$response" | tail -n 1)
body=$(echo "$response" | head -n -1)

if [ "$status" == "200" ]; then
    ACCESS_TOKEN=$(echo "$body" | jq -r '.access_token // empty')
    REFRESH_TOKEN=$(echo "$body" | jq -r '.refresh_token // empty')

    if [ -n "$ACCESS_TOKEN" ] && [ "$ACCESS_TOKEN" != "null" ]; then
        test_pass "Login successful, access token received"
    else
        test_fail "Login succeeded but no access token"
    fi

    if [ -n "$REFRESH_TOKEN" ] && [ "$REFRESH_TOKEN" != "null" ]; then
        test_pass "Refresh token received"
    else
        test_info "No refresh token (may be disabled)"
    fi
else
    test_fail "Login failed (status: $status)"
    echo "$body"
fi

# Test 3.2: Access protected endpoint
echo ""
echo "Testing: Access protected endpoint with token"
if [ -n "$ACCESS_TOKEN" ]; then
    response=$(api_call GET "/auth/me" "" "$ACCESS_TOKEN")
    status=$(echo "$response" | tail -n 1)
    if [ "$status" == "200" ]; then
        test_pass "Protected endpoint accessible with valid token"
    else
        test_fail "Protected endpoint failed (status: $status)"
    fi
else
    test_skip "No access token available"
fi

# Test 3.3: Refresh token
echo ""
echo "Testing: Token refresh"
if [ -n "$REFRESH_TOKEN" ] && [ "$REFRESH_TOKEN" != "null" ]; then
    response=$(api_call POST "/auth/token/refresh" "{\"refresh_token\":\"$REFRESH_TOKEN\"}")
    status=$(echo "$response" | tail -n 1)
    if [ "$status" == "200" ]; then
        NEW_ACCESS_TOKEN=$(echo "$response" | head -n -1 | jq -r '.access_token // empty')
        if [ -n "$NEW_ACCESS_TOKEN" ]; then
            test_pass "Token refresh successful"
            ACCESS_TOKEN="$NEW_ACCESS_TOKEN"
        else
            test_fail "Token refresh returned no access token"
        fi
    else
        test_fail "Token refresh failed (status: $status)"
    fi
else
    test_skip "No refresh token available"
fi

# Test 3.4: Get active sessions
echo ""
echo "Testing: Get active sessions"
if [ -n "$ACCESS_TOKEN" ]; then
    response=$(api_call GET "/auth/sessions" "" "$ACCESS_TOKEN")
    status=$(echo "$response" | tail -n 1)
    if [ "$status" == "200" ]; then
        test_pass "Sessions endpoint accessible"
        echo "$response" | head -n -1 | jq . 2>/dev/null || echo "$response"
    else
        test_fail "Sessions endpoint failed (status: $status)"
    fi
else
    test_skip "No access token available"
fi

# ============================================================================
# Phase 4: MFA Tests
# ============================================================================
section "Phase 4: Multi-Factor Authentication"

# Test 4.1: Check MFA status
echo "Testing: GET /auth/mfa/status"
if [ -n "$ACCESS_TOKEN" ]; then
    response=$(api_call GET "/auth/mfa/status" "" "$ACCESS_TOKEN")
    status=$(echo "$response" | tail -n 1)
    if [ "$status" == "200" ]; then
        test_pass "MFA status endpoint accessible"
        body=$(echo "$response" | head -n -1)
        echo "$body" | jq . 2>/dev/null || echo "$body"

        # shellcheck disable=SC2034  # MFA_ENABLED reserved for future conditional logic
        MFA_ENABLED=$(echo "$body" | jq -r '.mfa_enabled // false')
        CAN_SETUP=$(echo "$body" | jq -r '.can_setup_mfa // false')

        if [ "$CAN_SETUP" == "true" ]; then
            test_info "User can set up MFA"
        else
            test_info "MFA setup not available (may be PKI/Keycloak user or MFA disabled)"
        fi
    else
        test_fail "MFA status failed (status: $status)"
    fi
else
    test_skip "No access token available"
fi

# Test 4.2: Initiate MFA setup (if available)
echo ""
echo "Testing: POST /auth/mfa/setup"
if [ -n "$ACCESS_TOKEN" ] && [ "$CAN_SETUP" == "true" ]; then
    response=$(api_call POST "/auth/mfa/setup" "" "$ACCESS_TOKEN")
    status=$(echo "$response" | tail -n 1)
    if [ "$status" == "200" ]; then
        test_pass "MFA setup initiated"
        body=$(echo "$response" | head -n -1)
        SECRET=$(echo "$body" | jq -r '.secret // empty')
        if [ -n "$SECRET" ]; then
            test_info "TOTP secret received (length: ${#SECRET})"
        fi
    elif [ "$status" == "400" ]; then
        test_info "MFA setup rejected (may be disabled or already enabled)"
    else
        test_fail "MFA setup failed (status: $status)"
    fi
else
    test_skip "MFA setup not available"
fi

# ============================================================================
# Phase 5: Auth Methods Discovery
# ============================================================================
section "Phase 5: Auth Methods Discovery"

echo "Testing: GET /auth/methods"
response=$(api_call GET "/auth/methods")
status=$(echo "$response" | tail -n 1)
if [ "$status" == "200" ]; then
    test_pass "Auth methods endpoint accessible"
    body=$(echo "$response" | head -n -1)
    echo "$body" | jq . 2>/dev/null || echo "$body"

    # Check for MFA fields
    if echo "$body" | grep -q "mfa_enabled"; then
        test_pass "MFA status included in auth methods"
    else
        test_info "MFA status not in auth methods response"
    fi
else
    test_fail "Auth methods failed (status: $status)"
fi

# ============================================================================
# Phase 6: Logout Tests
# ============================================================================
section "Phase 6: Logout & Token Revocation"

# Test 6.1: Logout current session
echo "Testing: POST /auth/logout"
if [ -n "$ACCESS_TOKEN" ]; then
    response=$(api_call POST "/auth/logout" "" "$ACCESS_TOKEN")
    status=$(echo "$response" | tail -n 1)
    if [ "$status" == "200" ]; then
        test_pass "Logout successful"
    else
        test_fail "Logout failed (status: $status)"
    fi

    # Test 6.2: Try to use token after logout
    echo ""
    echo "Testing: Token should be revoked after logout"
    sleep 1
    response=$(api_call GET "/auth/me" "" "$ACCESS_TOKEN")
    status=$(echo "$response" | tail -n 1)
    if [ "$status" == "401" ]; then
        test_pass "Token correctly revoked after logout"
    else
        test_info "Token may still work (revocation check may be disabled)"
    fi
else
    test_skip "No access token available"
fi

# ============================================================================
# Summary
# ============================================================================
section "Test Summary"

echo ""
echo "FedRAMP compliance testing complete."
echo ""
echo "Manual tests to perform:"
echo "  1. Test TOTP with authenticator app (Google Auth, Authy)"
echo "  2. Test backup codes"
echo "  3. Test login banner (set LOGIN_BANNER_ENABLED=true)"
echo "  4. Test FIPS mode (set FIPS_MODE=true)"
echo "  5. Test audit logs in OpenSearch"
echo ""
echo "Environment variables to test different configurations:"
echo "  MFA_ENABLED=true MFA_REQUIRED=true"
echo "  FIPS_MODE=true"
echo "  PASSWORD_POLICY_ENABLED=true"
echo "  LOGIN_BANNER_ENABLED=true"
echo "  TOKEN_REVOCATION_ENABLED=true"
echo ""
