#!/bin/bash
#
# Setup Test PKI Infrastructure
#
# Creates a Certificate Authority and client certificates for testing
# PKI/X.509 authentication similar to government CAC/PIV systems.
#
# Usage: ./scripts/pki/setup-test-pki.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKI_DIR="${SCRIPT_DIR}/test-certs"
CA_DAYS=3650  # 10 years for CA
CERT_DAYS=365 # 1 year for client certs

# Colors for output
# shellcheck disable=SC2034
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  OpenTranscribe Test PKI Setup${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Create PKI directory structure
echo -e "${YELLOW}Creating PKI directory structure...${NC}"
mkdir -p "${PKI_DIR}"/{ca,clients,nginx}
cd "${PKI_DIR}"

# ============================================
# Step 1: Create Root CA
# ============================================
echo ""
echo -e "${GREEN}Step 1: Creating Root Certificate Authority${NC}"

if [ -f "ca/ca.key" ]; then
    echo -e "${YELLOW}CA already exists, skipping creation${NC}"
else
    # Generate CA private key
    openssl genrsa -out ca/ca.key 4096

    # Create CA certificate
    openssl req -new -x509 -days ${CA_DAYS} \
        -key ca/ca.key \
        -out ca/ca.crt \
        -subj "/C=US/ST=Virginia/L=Arlington/O=OpenTranscribe Test CA/OU=PKI Testing/CN=OpenTranscribe Test Root CA"

    echo -e "${GREEN}✓ Root CA created${NC}"
fi

# ============================================
# Step 2: Create Client Certificates
# ============================================
echo ""
echo -e "${GREEN}Step 2: Creating Client Certificates${NC}"

create_client_cert() {
    local name=$1
    local cn=$2
    local email=$3
    local org=$4

    echo -e "${YELLOW}Creating certificate for: ${cn}${NC}"

    # Generate client private key
    openssl genrsa -out "clients/${name}.key" 2048

    # Create certificate signing request (CSR)
    openssl req -new \
        -key "clients/${name}.key" \
        -out "clients/${name}.csr" \
        -subj "/C=US/ST=Virginia/L=Arlington/O=${org}/OU=Users/CN=${cn}/emailAddress=${email}"

    # Create extensions file for SAN (Subject Alternative Name)
    cat > "clients/${name}.ext" << EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth, emailProtection
subjectAltName = email:${email}
EOF

    # Sign the certificate with CA
    openssl x509 -req -days ${CERT_DAYS} \
        -in "clients/${name}.csr" \
        -CA ca/ca.crt \
        -CAkey ca/ca.key \
        -CAcreateserial \
        -out "clients/${name}.crt" \
        -extfile "clients/${name}.ext"

    # Create PKCS12 bundle with OpenSSL 3.x's modern default encryption
    # (AES-256/PBKDF2). This is the one Playwright/Node and any automated
    # test tooling must use — Node's bundled OpenSSL 3.x cannot load a
    # -legacy (RC2-40-CBC) bundle without the legacy provider enabled, which
    # it does not have. Confirmed failure mode: "Browser.new_context: Failed
    # to load client certificate: Unsupported TLS certificate."
    openssl pkcs12 -export \
        -out "clients/${name}.p12" \
        -inkey "clients/${name}.key" \
        -in "clients/${name}.crt" \
        -certfile ca/ca.crt \
        -passout pass:changeit \
        -name "${cn}"

    # Separate -legacy bundle for manual macOS Keychain import only (macOS's
    # Keychain/Security framework rejects the modern PBES2 bundle above).
    # Never used by automated tests.
    openssl pkcs12 -export \
        -out "clients/${name}-macos.p12" \
        -inkey "clients/${name}.key" \
        -in "clients/${name}.crt" \
        -certfile ca/ca.crt \
        -passout pass:changeit \
        -legacy \
        -name "${cn}"

    # Clean up CSR and ext files
    rm -f "clients/${name}.csr" "clients/${name}.ext"

    echo -e "${GREEN}✓ Certificate created for ${cn}${NC}"
}

# Create test user certificates
create_client_cert "testuser" "Test User" "testuser@example.com" "OpenTranscribe Users"
create_client_cert "admin" "Admin User" "admin@example.com" "OpenTranscribe Admins"
# pkiadmin (issue #593): a PKI-only admin identity, deliberately NOT admin@example.com.
# The E2E admin-cert fixture (backend/tests/e2e/test_pki.py) used to reuse the "admin" cert
# above, whose email matches the shared dev super_admin account. account_linking.
# assert_email_link_permitted() refuses ANY email-matched link onto a super_admin
# unconditionally (auth/account_linking.py — "a platform-owner account is never acquired
# through an external directory"), so that fixture's login always 401'd once that guard
# shipped. pkiadmin@example.com has no pre-existing DB row, so PKI JIT-provisions a brand
# new account and grants it at most 'admin' (external IdPs never grant super_admin) — no
# collision, and it exercises the real admin-role UI path the fixture is actually for.
create_client_cert "pkiadmin" "PKI Admin User" "pkiadmin@example.com" "OpenTranscribe Admins"
create_client_cert "john.doe" "John Doe" "john.doe@gov.example.com" "Department of Testing"
create_client_cert "jane.smith" "Jane Smith" "jane.smith@gov.example.com" "Department of Testing"

# ============================================
# Step 3: Prepare Nginx Configuration Files
# ============================================
echo ""
echo -e "${GREEN}Step 3: Preparing Nginx configuration${NC}"

# Copy CA cert for Nginx
cp ca/ca.crt nginx/ca.crt

echo -e "${GREEN}✓ CA certificate copied to nginx directory${NC}"

# ============================================
# Step 4: Fix File Permissions
# ============================================
echo ""
echo -e "${GREEN}Step 4: Setting file permissions${NC}"

# Make all certificates and keys readable (needed for Docker volume mounts)
chmod 644 ca/ca.crt ca/ca.key 2>/dev/null || true
chmod 644 nginx/*.crt nginx/*.key 2>/dev/null || true
chmod 644 clients/*.crt clients/*.key clients/*.p12 2>/dev/null || true

echo -e "${GREEN}✓ File permissions set (644 for all cert files)${NC}"

# ============================================
# Step 5: Display Certificate Information
# ============================================
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  PKI Setup Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${YELLOW}Certificate Authority:${NC}"
echo "  Location: ${PKI_DIR}/ca/ca.crt"
openssl x509 -in ca/ca.crt -noout -subject -issuer | sed 's/^/  /'
echo ""

echo -e "${YELLOW}Client Certificates:${NC}"
for cert in clients/*.crt; do
    name=$(basename "$cert" .crt)
    echo ""
    echo "  ${name}:"
    openssl x509 -in "$cert" -noout -subject | sed 's/^/    /'
    echo "    PKCS12: ${PKI_DIR}/clients/${name}.p12 (password: changeit)"
done

echo ""
echo -e "${YELLOW}Distinguished Names for PKI_ADMIN_DNS:${NC}"
echo ""
# Extract DNs in the format the backend expects
for cert in clients/*.crt; do
    dn=$(openssl x509 -in "$cert" -noout -subject -nameopt RFC2253 | sed 's/subject=//')
    echo "  $dn"
done

echo ""
echo -e "${GREEN}Next Steps:${NC}"
echo ""
echo "1. Update your .env file with the CA cert path:"
echo "   PKI_CA_CERT_PATH=${PKI_DIR}/ca/ca.crt"
echo ""
echo "2. Set admin DNs (use the DN format above):"
echo "   PKI_ADMIN_DNS=emailAddress=admin@example.com,CN=Admin User,OU=Users,O=OpenTranscribe Admins,L=Arlington,ST=Virginia,C=US"
echo ""
echo "3. Import a .p12 file into your browser:"
echo "   - Chrome: Settings → Privacy → Security → Manage certificates → Import"
echo "   - Firefox: Settings → Privacy → View Certificates → Your Certificates → Import"
echo "   Password: changeit"
echo ""
echo "4. For curl testing, use:"
echo "   curl --cert ${PKI_DIR}/clients/testuser.crt --key ${PKI_DIR}/clients/testuser.key ..."
echo ""
