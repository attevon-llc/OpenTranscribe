#!/bin/bash
#
# Test PKI Authentication
#
# Tests the PKI authentication endpoint using generated certificates, through the
# mTLS nginx (PKI_E2E_URL, written by generate-test-env.sh; default
# https://localhost:${PKI_HTTPS_PORT:-5182}). curl presents the client certificate and
# nginx forwards the DN it verified, exactly as a browser login does.
#
# It no longer sends a DN header straight to the backend port: that request arrives
# from the docker gateway, which the backend never trusts to vouch for an identity
# (backend/app/auth/header_trust.py), because anyone on the host could forge one.
#
# Usage: ./scripts/pki/test-pki-auth.sh [username]
#        ./scripts/pki/test-pki-auth.sh testuser
#        ./scripts/pki/test-pki-auth.sh admin
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKI_DIR="${SCRIPT_DIR}/test-certs"
# shellcheck source=scripts/lib/stack-target.sh
source "${SCRIPT_DIR}/../lib/stack-target.sh"
if [[ -z "${API_URL:-}" ]]; then st_target || exit $?; fi
API_URL="${API_URL:-http://localhost:${BACKEND_PORT}/api}"
# The fragment generate-test-env.sh wrote for the running --with-pki stack knows its port.
if [[ -z "${PKI_E2E_URL:-}" && -f "${PKI_DIR}/pki-test.env" ]]; then
    PKI_E2E_URL="$(sed -n "s/^PKI_E2E_URL='\(.*\)'\$/\1/p" "${PKI_DIR}/pki-test.env")"
fi
PKI_URL="${PKI_E2E_URL:-https://localhost:${PKI_HTTPS_PORT:-5182}}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Check if certificates exist
if [ ! -d "${PKI_DIR}/clients" ]; then
    echo -e "${RED}Error: Test certificates not found.${NC}"
    echo "Run ./scripts/pki/setup-test-pki.sh first"
    exit 1
fi

# Function to test PKI auth with a certificate
test_pki_auth() {
    local cert_name=$1
    local cert_file="${PKI_DIR}/clients/${cert_name}.crt"
    local key_file="${PKI_DIR}/clients/${cert_name}.key"

    if [ ! -f "$cert_file" ]; then
        echo -e "${RED}Certificate not found: ${cert_file}${NC}"
        return 1
    fi

    # Extract DN in RFC2253 format (what Nginx would provide)
    local dn
    dn=$(openssl x509 -in "$cert_file" -noout -subject -nameopt RFC2253 | sed 's/subject=//')

    echo -e "${CYAN}Testing PKI auth for: ${cert_name}${NC}"
    echo -e "  Certificate: ${cert_file}"
    echo -e "  DN: ${dn}"
    echo ""

    # -k: the test server certificate is a self-signed localhost one (docker-compose.pki.yml
    # setup step 2). The client certificate is what this exercises, and nginx verifies it.
    echo -e "${YELLOW}Calling: POST ${PKI_URL}/api/auth/pki/authenticate (mTLS)${NC}"
    echo ""

    response=$(curl -s -w "\n%{http_code}" -X POST "${PKI_URL}/api/auth/pki/authenticate" \
        --cert "$cert_file" --key "$key_file" -k \
        -H "Content-Type: application/json")

    # Split response body and status code
    local http_code
    local body
    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')

    if [ "$http_code" -eq 200 ]; then
        echo -e "${GREEN}✓ Authentication successful (HTTP ${http_code})${NC}"
        echo ""
        echo -e "${YELLOW}Response:${NC}"
        echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body"

        # Decode the JWT token to show user info
        local token
        token=$(echo "$body" | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null)
        if [ -n "$token" ]; then
            echo ""
            echo -e "${YELLOW}Token payload:${NC}"
            # Decode JWT payload (base64)
            echo "$token" | cut -d. -f2 | base64 -d 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "(could not decode)"
        fi
    else
        echo -e "${RED}✗ Authentication failed (HTTP ${http_code})${NC}"
        echo ""
        echo -e "${YELLOW}Response:${NC}"
        echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body"
    fi

    echo ""
    echo "----------------------------------------"
    echo ""
}

# Function to check auth methods
check_auth_methods() {
    echo -e "${CYAN}Checking available authentication methods...${NC}"
    echo ""

    response=$(curl -s "${API_URL}/auth/methods")
    echo -e "${YELLOW}Response from /auth/methods:${NC}"
    echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
    echo ""
    echo "----------------------------------------"
    echo ""
}

# Function to list available certificates
list_certs() {
    echo -e "${CYAN}Available test certificates:${NC}"
    echo ""
    for cert in "${PKI_DIR}"/clients/*.crt; do
        if [ -f "$cert" ]; then
            local name
            local dn
            name=$(basename "$cert" .crt)
            dn=$(openssl x509 -in "$cert" -noout -subject -nameopt RFC2253 | sed 's/subject=//')
            echo -e "  ${GREEN}${name}${NC}"
            echo "    DN: ${dn}"
            echo ""
        fi
    done
}

# Main
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  OpenTranscribe PKI Authentication Test${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check auth methods first
check_auth_methods

if [ -n "$1" ]; then
    # Test specific certificate
    test_pki_auth "$1"
else
    # List available certs and test all
    list_certs

    echo -e "${CYAN}Testing all certificates...${NC}"
    echo ""

    for cert in "${PKI_DIR}"/clients/*.crt; do
        if [ -f "$cert" ]; then
            name=$(basename "$cert" .crt)
            test_pki_auth "$name"
        fi
    done
fi

echo -e "${GREEN}PKI testing complete!${NC}"
