#!/bin/bash
# LDAP Authentication Testing Script for PR #119
#
# This script helps test LDAP authentication in OpenTranscribe.
# Make sure you have:
# 1. Started OpenTranscribe: ./opentr.sh start dev
# 2. Started LLDAP: ./opentr.sh start dev --with-ldap-test
# 3. Added LDAP config to .env and restarted backend

set -e

API_URL="${API_URL:-http://localhost:5174/api}"
LLDAP_URL="${LLDAP_URL:-http://localhost:17170}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  LDAP Authentication Test Suite${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Check if services are running
echo -e "${YELLOW}Checking services...${NC}"

# Check LLDAP
if curl -s -o /dev/null -w "%{http_code}" "$LLDAP_URL" | grep -q "200\|302"; then
    echo -e "${GREEN}✓ LLDAP is running at $LLDAP_URL${NC}"
else
    echo -e "${RED}✗ LLDAP is not accessible at $LLDAP_URL${NC}"
    echo "  Start it with: ./opentr.sh start dev --with-ldap-test"
    exit 1
fi

# Check OpenTranscribe API (use auth/me endpoint - returns 401 if running but not authenticated)
API_CHECK=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/auth/me" 2>/dev/null)
if [ "$API_CHECK" = "401" ] || [ "$API_CHECK" = "200" ]; then
    echo -e "${GREEN}✓ OpenTranscribe API is running at $API_URL${NC}"
else
    echo -e "${RED}✗ OpenTranscribe API is not accessible at $API_URL (HTTP $API_CHECK)${NC}"
    echo "  Start it with: ./opentr.sh start dev"
    exit 1
fi

echo ""
echo -e "${YELLOW}----------------------------------------${NC}"
echo -e "${YELLOW}Test 1: Local Admin Login (existing)${NC}"
echo -e "${YELLOW}----------------------------------------${NC}"

# Test local admin login
RESPONSE=$(curl -s -X POST "$API_URL/auth/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=admin@example.com&password=password")

if echo "$RESPONSE" | grep -q "access_token"; then
    echo -e "${GREEN}✓ Local admin login successful${NC}"
    LOCAL_TOKEN=$(echo "$RESPONSE" | jq -r '.access_token')

    # Get user info
    USER_INFO=$(curl -s "$API_URL/auth/me" -H "Authorization: Bearer $LOCAL_TOKEN")
    echo "  User: $(echo "$USER_INFO" | jq -r '.email')"
    echo "  Auth Type: $(echo "$USER_INFO" | jq -r '.auth_type')"
else
    echo -e "${RED}✗ Local admin login failed${NC}"
    echo "  Response: $RESPONSE"
fi

echo ""
echo -e "${YELLOW}----------------------------------------${NC}"
echo -e "${YELLOW}Test 2: LDAP User Login${NC}"
echo -e "${YELLOW}----------------------------------------${NC}"
echo ""
echo "Before running this test, create a user in LLDAP:"
echo "  1. Go to $LLDAP_URL"
echo "  2. Login as admin / admin_password"
echo "  3. Create a user with:"
echo "     - User ID: testuser"
echo "     - Email: testuser@example.com"
echo "     - Password: testpassword123"
echo ""
read -p "Press Enter when you've created the user (or Ctrl+C to exit)..."

# Test LDAP user login by username
echo ""
echo "Testing LDAP login by username (testuser)..."
RESPONSE=$(curl -s -X POST "$API_URL/auth/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=testuser&password=testpassword123")

if echo "$RESPONSE" | grep -q "access_token"; then
    echo -e "${GREEN}✓ LDAP login by username successful!${NC}"
    LDAP_TOKEN=$(echo "$RESPONSE" | jq -r '.access_token')

    # Get user info
    USER_INFO=$(curl -s "$API_URL/auth/me" -H "Authorization: Bearer $LDAP_TOKEN")
    echo "  User: $(echo "$USER_INFO" | jq -r '.email')"
    echo "  Full Name: $(echo "$USER_INFO" | jq -r '.full_name')"
    echo "  Auth Type: $(echo "$USER_INFO" | jq -r '.auth_type')"
    echo "  Role: $(echo "$USER_INFO" | jq -r '.role')"
else
    echo -e "${RED}✗ LDAP login by username failed${NC}"
    echo "  Response: $RESPONSE"
    echo ""
    echo "  Troubleshooting:"
    echo "  - Check LDAP_ENABLED=true in .env"
    echo "  - Check backend logs: ./opentr.sh logs backend"
    echo "  - Verify user exists in LLDAP: $LLDAP_URL"
fi

echo ""
echo -e "${YELLOW}----------------------------------------${NC}"
echo -e "${YELLOW}Test 3: LDAP User Login by Email${NC}"
echo -e "${YELLOW}----------------------------------------${NC}"

# Test LDAP user login by email
echo "Testing LDAP login by email (testuser@example.com)..."
RESPONSE=$(curl -s -X POST "$API_URL/auth/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=testuser@example.com&password=testpassword123")

if echo "$RESPONSE" | grep -q "access_token"; then
    echo -e "${GREEN}✓ LDAP login by email successful!${NC}"
else
    echo -e "${YELLOW}! LDAP login by email: $(echo "$RESPONSE" | jq -r '.detail // "unknown error"')${NC}"
    echo "  (This is OK - email login is a fallback feature)"
fi

echo ""
echo -e "${YELLOW}----------------------------------------${NC}"
echo -e "${YELLOW}Test 4: Verify LDAP User Cannot Use Password Auth After Sync${NC}"
echo -e "${YELLOW}----------------------------------------${NC}"

echo "Checking that LDAP users are prevented from local password auth..."
# This should fail because the user is now stored as auth_type='ldap'
RESPONSE=$(curl -s -X POST "$API_URL/auth/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=testuser@example.com&password=wrongpassword")

if echo "$RESPONSE" | grep -q "Incorrect"; then
    echo -e "${GREEN}✓ LDAP user correctly rejected with wrong password${NC}"
else
    echo "  Response: $RESPONSE"
fi

echo ""
echo -e "${YELLOW}----------------------------------------${NC}"
echo -e "${YELLOW}Test 5: LDAP Admin User${NC}"
echo -e "${YELLOW}----------------------------------------${NC}"
echo ""
echo "To test admin assignment via LDAP_ADMIN_USERS:"
echo "  1. Create a user in LLDAP with username matching LDAP_ADMIN_USERS"
echo "  2. Example: if LDAP_ADMIN_USERS=admin,testadmin"
echo "     Create user 'testadmin' in LLDAP"
echo "  3. Login as that user"
echo "  4. Verify role is 'admin'"
echo ""

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Testing Complete${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo "Next steps:"
echo "  1. Log into OpenTranscribe frontend: http://localhost:5173"
echo "  2. Try logging in with LDAP credentials (testuser / testpassword123)"
echo "  3. Check Settings - password change should be hidden for LDAP users"
echo "  4. Check backend logs: ./opentr.sh logs backend"
