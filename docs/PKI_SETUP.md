# PKI/X.509 Certificate Authentication Setup

This guide covers setting up PKI (Public Key Infrastructure) certificate-based authentication with OpenTranscribe.

## Overview

PKI authentication allows users to authenticate using X.509 client certificates instead of passwords. This is commonly used in:
- Government and military environments (CAC/PIV cards)
- Enterprise environments with certificate-based security
- High-security deployments requiring mutual TLS

> **v0.4.0 Change**: PKI configuration is now managed via the Super Admin UI (Settings → Authentication → PKI/X.509). Settings are stored encrypted (AES-256-GCM) in the database. Environment variables continue to work as a fallback seed.
>
> **OCSP/CRL**: v0.4.0 adds OCSP (Online Certificate Status Protocol) and CRL (Certificate Revocation List) checking. Configure via Admin UI for real-time or periodic revocation checking.
>
> **Super admin password fallback**: Even when PKI is the only enabled auth method, the super admin account can always authenticate with a password. This ensures emergency access and the ability to reconfigure authentication settings if PKI infrastructure fails.

## How It Works

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│                 │     │                  │     │                 │
│  Client         │────▶│    Nginx         │────▶│  OpenTranscribe │
│  (with cert)    │     │  (mTLS termination)    │    Backend      │
│                 │     │                  │     │                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
        │                        │                       │
        │                        │                       │
   Client Cert            Extract DN/Cert         Validate & Auth
   Presented              Pass via Headers        Create User
```

1. Client presents X.509 certificate during TLS handshake
2. Nginx validates certificate against CA
3. Nginx extracts certificate info and passes via headers
4. Backend authenticates user based on certificate DN
5. User is created/updated in database

## Testing Options

| Method | Complexity | Use Case |
|--------|------------|----------|
| API Testing | Easy | Verify PKI logic works without browser |
| Browser Testing | Medium | Full end-to-end with certificate selection |
| Production | Advanced | Enterprise deployment with real CA |

---

## Quick Start: API Testing (No Browser)

For quick API testing without setting up HTTPS/mTLS:

### Step 1: Generate Test Certificates

```bash
# Generate test CA and client certificates
./scripts/pki/setup-test-pki.sh
```

This creates:
- Root CA at `scripts/pki/test-certs/ca/ca.crt`
- Client certificates for: testuser, admin, john.doe, jane.smith
- PKCS12 files for browser import (password: `changeit`)

### Step 2: Configure OpenTranscribe

Fastest path — generates the values below from the certificates on disk and never touches
`.env`:

```bash
./scripts/pki/generate-test-env.sh --print
```

Or by hand:

```bash
# .env settings
PKI_ENABLED=true
PKI_CA_CERT_PATH=/mnt/nvm/repos/transcribe-app/scripts/pki/test-certs/ca/ca.crt
PKI_VERIFY_REVOCATION=false
PKI_CERT_HEADER=X-Client-Cert
PKI_CERT_DN_HEADER=X-Client-Cert-DN
# SEMICOLON-separated — a DN itself contains commas, so ',' cannot delimit multiple entries
PKI_ADMIN_DNS=emailAddress=pkiadmin@example.com,CN=PKI Admin User,OU=Users,O=OpenTranscribe Admins,L=Arlington,ST=Virginia,C=US
```

### Step 3: Test via API

```bash
# Using the test script
./scripts/pki/test-pki-auth.sh pkiadmin   # Gets admin role
./scripts/pki/test-pki-auth.sh testuser   # Gets user role

# Or manually with curl (simulates what Nginx does)
# Get the DN from the certificate
ADMIN_DN=$(openssl x509 -in scripts/pki/test-certs/clients/pkiadmin.crt -noout -subject | sed 's/subject=//')

# Authenticate
curl -X POST http://localhost:5174/api/auth/pki/authenticate \
  -H "Content-Type: application/json" \
  -H "X-Client-Cert-DN: $ADMIN_DN"
```

This simulates what Nginx would do in production by passing the `X-Client-Cert-DN` header.

**Note:** API testing only verifies the backend PKI logic. For full browser-based testing with certificate selection prompts, see "Browser-Based PKI Testing" below.

### Step 4: Browser Testing (Requires Nginx mTLS)

For browser-based PKI login (clicking "Sign in with Certificate"), you need HTTPS with mutual TLS.

---

## Browser-Based PKI Testing (Quick Setup)

This is the easiest way to test PKI authentication with a real browser.

### Step 1: Generate Test Certificates (if not done)

```bash
./scripts/pki/setup-test-pki.sh
```

### Step 2: Enable PKI in `.env` or Admin UI

**Admin UI (recommended — v0.4.0+):**
1. Log in as super_admin
2. Navigate to Settings → Authentication → PKI/X.509
3. Enable PKI, set CA Certificate Path, and optionally configure OCSP/CRL

**Environment variables (fallback / initial seed):**

```bash
PKI_ENABLED=true
PKI_CA_CERT_PATH=/app/scripts/pki/test-certs/ca/ca.crt
PKI_VERIFY_REVOCATION=false
PKI_CERT_HEADER=X-Client-Cert
PKI_CERT_DN_HEADER=X-Client-Cert-DN
# SEMICOLON-separated — a DN itself contains commas, so ',' cannot delimit multiple entries
PKI_ADMIN_DNS=emailAddress=pkiadmin@example.com,CN=PKI Admin User,OU=Users,O=OpenTranscribe Admins,L=Arlington,ST=Virginia,C=US
```

Fastest path: skip the manual config entirely. `./scripts/pki/generate-test-env.sh` derives all of
the above from the certificates on disk and writes a compose overlay that `--with-pki` (below)
loads automatically — `.env` is never opened, read, or written.

### Step 3: Start with PKI Overlay

PKI authentication needs nginx in front of the backend to terminate mTLS and verify client
certificates — the default dev mode's Vite dev server can't do this. `--with-pki` works in
**both** modes; in dev it loads `docker-compose.pki-dev.yml`, which swaps in a built nginx
(`Dockerfile.prod`) in front of the normal bind-mounted, hot-reloading dev backend, so a backend
fix stays testable with no image rebuild (only the PKI frontend is a built image — a frontend
change still needs `./opentr.sh rebuild-frontend`).

**Recommended Method (using opentr.sh):**
```bash
# Dev mode with PKI (bind-mounted backend, built PKI-nginx frontend)
./opentr.sh start dev --with-pki

# Production mode with PKI (test before push)
./opentr.sh start prod --build --with-pki

# Production mode with PKI (Docker Hub images)
./opentr.sh start prod --with-pki
```

**Advanced Method (manual docker compose):**
```bash
# Production mode with PKI (local images)
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.local.yml -f docker-compose.pki.yml up -d --build

# Production mode with PKI (local images)
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.local.yml -f docker-compose.pki.yml up -d --build
```

### Step 4: Import Certificate to Browser

Import one of the `.p12` files from `scripts/pki/test-certs/clients/`:

**macOS:**
```bash
# Import to keychain (password: changeit)
security import scripts/pki/test-certs/clients/pkiadmin.p12 -k ~/Library/Keychains/login.keychain-db -P changeit -A
```

Then in **Keychain Access**:
1. Find the private key under "Keys"
2. Right-click → Get Info → Access Control
3. Select "Allow all applications to access this item"
4. Save Changes

**Windows/Linux Chrome:**
1. Settings → Privacy and security → Security → Manage certificates
2. Import → Select `pkiadmin.p12`
3. Password: `changeit`

**Firefox:**
1. Settings → Privacy & Security → Certificates → View Certificates
2. Your Certificates → Import
3. Select `pkiadmin.p12`, password: `changeit`

### Step 5: Access via HTTPS

Open: **https://localhost:5182**

- Accept the self-signed certificate warning
- Click "Sign in with Certificate"
- Browser will prompt you to select a certificate
- Select the imported certificate
- You'll be authenticated!

### Available Test Users

| Certificate | Email | Role |
|-------------|-------|------|
| pkiadmin.p12 | pkiadmin@example.com | Admin |
| testuser.p12 | testuser@example.com | User |
| john.doe.p12 | john.doe@example.com | User |
| jane.smith.p12 | jane.smith@example.com | User |

Password for all `.p12` files: `changeit`

`admin.p12` (admin@example.com) also exists but **do not use it for admin-role testing**: its
email collides with the seeded dev `super_admin` account, and account-linking refuses any
email-matched link onto a super_admin unconditionally — that login always fails. `pkiadmin.p12`
has no pre-existing account, so PKI JIT-provisions a fresh one at the `admin` role.

---

## Full Development Environment Setup (Alternative)

### Step 1: Start Step CA (Certificate Authority)

```bash
# Start Step CA for PKI testing
docker compose -f docker-compose.yml -f docker-compose.keycloak.yml --profile pki up -d step-ca

# View CA initialization logs
docker compose -f docker-compose.yml -f docker-compose.keycloak.yml logs step-ca
```

### Step 2: Initialize Step CA

```bash
# Get the CA fingerprint
docker exec step-ca step ca health
docker exec step-ca step ca bootstrap --ca-url https://localhost:9000 --fingerprint <fingerprint>
```

### Step 3: Generate Client Certificate

```bash
# Generate a client certificate
docker exec step-ca step ca certificate "user@example.com" user.crt user.key --not-after=8760h

# Copy certificates from container
docker cp step-ca:/home/step/user.crt ./certs/
docker cp step-ca:/home/step/user.key ./certs/

# Export as PKCS#12 for browser import
openssl pkcs12 -export -out user.p12 -inkey certs/user.key -in certs/user.crt
```

### Step 4: Configure Nginx for mTLS

Create or update Nginx configuration for mutual TLS:

```nginx
# /etc/nginx/conf.d/pki.conf

server {
    listen 443 ssl;
    server_name localhost;

    # Server certificate
    ssl_certificate /etc/nginx/certs/server.crt;
    ssl_certificate_key /etc/nginx/certs/server.key;

    # Client certificate verification
    ssl_client_certificate /etc/nginx/certs/ca.crt;
    ssl_verify_client optional;  # 'required' for mandatory PKI
    ssl_verify_depth 2;

    # PKI authentication endpoint
    location /api/auth/pki {
        # Pass certificate info to backend
        proxy_set_header X-Client-Cert $ssl_client_escaped_cert;
        proxy_set_header X-Client-Cert-Verify $ssl_client_verify;
        proxy_set_header X-Client-Cert-DN $ssl_client_s_dn;

        proxy_pass http://backend:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # Other API routes (no PKI required)
    location /api/ {
        proxy_pass http://backend:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # Frontend
    location / {
        proxy_pass http://frontend:5173;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### Step 5: Configure OpenTranscribe

Add to your `.env` file:

```bash
# PKI/X.509 Configuration
PKI_ENABLED=true
PKI_CA_CERT_PATH=/etc/ssl/certs/ca.crt
PKI_VERIFY_REVOCATION=false
PKI_CERT_HEADER=X-Client-Cert
PKI_CERT_DN_HEADER=X-Client-Cert-DN
PKI_ADMIN_DNS=CN=Admin User,O=OpenTranscribe,C=US
```

### Step 6: Import Certificate to Browser

**Chrome/Edge (Windows/Linux):**
1. Settings → Privacy and security → Security → Manage certificates
2. Import → Select user.p12 file
3. Enter password: `changeit`

**Firefox:**
1. Settings → Privacy & Security → Certificates → View Certificates
2. Your Certificates → Import
3. Select user.p12 file, password: `changeit`

**macOS (Safari/Chrome):**
1. Import via terminal (recommended):
   ```bash
   security import admin.p12 -k ~/Library/Keychains/login.keychain-db -P changeit -A
   ```
2. Or double-click the .p12 file to open Keychain Access
3. **Important:** After import, open Keychain Access:
   - Find the private key under "Keys"
   - Right-click → Get Info → Access Control
   - Select **"Allow all applications to access this item"**
   - Click Save Changes
4. This prevents the browser from freezing on repeated keychain prompts

### Step 7: Test PKI Authentication

1. Open OpenTranscribe in your browser
2. Click "Sign in with Certificate"
3. Browser will prompt to select certificate
4. Select your imported certificate
5. You'll be authenticated and redirected

## Production Configuration

### Using Enterprise CA

For production, use your organization's Certificate Authority. Configure via the Admin UI (Settings → Authentication → PKI/X.509) for encrypted storage:

```bash
# .env for production (used only if no database config exists)
PKI_ENABLED=true
PKI_CA_CERT_PATH=/etc/ssl/certs/enterprise-ca.crt
PKI_VERIFY_REVOCATION=true
PKI_CERT_HEADER=X-Client-Cert
PKI_CERT_DN_HEADER=X-Client-Cert-DN
PKI_ADMIN_DNS=CN=Admin1,OU=IT,O=Company,C=US;CN=Admin2,OU=IT,O=Company,C=US
```

### OCSP Revocation Checking (Recommended)

OCSP provides real-time certificate revocation status. When a certificate is added to the OCSP responder's revocation list, access is denied on the next login attempt:

| Setting | Description | Example |
|---------|-------------|---------|
| **Enable OCSP** | Turn on OCSP checking | `true` |
| **OCSP Responder URL** | Your CA's OCSP endpoint | `http://ocsp.pki.company.com` |

Configure via Admin UI: Settings → Authentication → PKI/X.509 → Enable OCSP.

### CRL Revocation Checking (Alternative)

CRL downloads and caches a list of revoked certificates, refreshed periodically:

| Setting | Description | Default |
|---------|-------------|---------|
| **Enable CRL** | Turn on CRL checking | `false` |
| **CRL Endpoint URL** | CRL distribution point | `http://pki.company.com/crl` |
| **CRL Refresh Hours** | How often to refresh the CRL | `24` |

CRL checking is suitable when an OCSP responder is not available. The system caches the CRL locally and refreshes on the configured interval.

**OCSP vs CRL:**
- OCSP: Real-time check on every login — revocation takes effect immediately
- CRL: Periodic check — revocation takes effect within the refresh interval (up to 24 hours)
- Both can be enabled simultaneously for defence-in-depth

### Nginx Production Configuration

```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;

    # Strong TLS configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;
    ssl_prefer_server_ciphers off;

    # Server certificate
    ssl_certificate /etc/nginx/certs/server.crt;
    ssl_certificate_key /etc/nginx/certs/server.key;

    # Client certificate verification
    ssl_client_certificate /etc/nginx/certs/ca-bundle.crt;
    ssl_verify_client optional_no_ca;  # Verify if provided
    ssl_verify_depth 4;

    # CRL checking (if enabled)
    # ssl_crl /etc/nginx/certs/ca.crl;

    location /api/auth/pki {
        # Only allow if client cert was verified
        if ($ssl_client_verify != SUCCESS) {
            return 403;
        }

        proxy_set_header X-Client-Cert $ssl_client_escaped_cert;
        proxy_set_header X-Client-Cert-Verify $ssl_client_verify;
        proxy_set_header X-Client-Cert-DN $ssl_client_s_dn;

        proxy_pass http://backend:8080;
    }
}
```

### Certificate Requirements

Client certificates should include:
- **Common Name (CN)**: User's full name
- **Email Address**: User's email (in subject or SAN)
- **Key Usage**: Digital Signature, Key Encipherment
- **Extended Key Usage**: TLS Web Client Authentication

Example certificate subject:
```
CN=John Doe,emailAddress=john.doe@company.com,OU=Engineering,O=Company Inc,C=US
```

## Admin Configuration

### Designating PKI Admins

Admins are designated by their certificate Distinguished Name (DN):

```bash
# Single admin
PKI_ADMIN_DNS=CN=John Doe,O=Company,C=US

# Multiple admins (SEMICOLON-separated — a DN itself contains commas, so ','
# cannot be the delimiter between entries)
PKI_ADMIN_DNS=CN=John Doe,O=Company,C=US;CN=Jane Smith,O=Company,C=US
```

**Note**: DN must match exactly as it appears in the certificate.

### User Creation

When a user authenticates via PKI for the first time:
1. User is automatically created in the database
2. Email is extracted from certificate (or generated from CN if not present)
3. Full name is extracted from CN
4. Role is set based on PKI_ADMIN_DNS match

## Smart Card / CAC / PIV Support

For DoD CAC or PIV card authentication:

1. Ensure card reader drivers are installed
2. Browser must have PKCS#11 module configured
3. Insert smart card before navigating to login page
4. Select certificate when prompted

### Chrome CAC Setup (Linux)

```bash
# Install OpenSC
sudo apt install opensc opensc-pkcs11

# Configure Chrome to use PKCS#11
# Settings → Privacy → Security → Manage certificates → Security Devices
# Add: /usr/lib/x86_64-linux-gnu/opensc-pkcs11.so
```

### Firefox CAC Setup

```bash
# Settings → Privacy & Security → Certificates → Security Devices
# Load module: /usr/lib/x86_64-linux-gnu/opensc-pkcs11.so
```

## Troubleshooting

### Common Issues

**"PKI authentication is not enabled"**
- If using Admin UI: verify PKI is enabled in Settings → Authentication → PKI/X.509
- If using .env: ensure `PKI_ENABLED=true` and restart the backend
- Database config takes precedence — an explicit `enabled=false` in the database overrides .env

**"Invalid or missing client certificate"**
- Verify certificate is imported in browser
- Check Nginx is passing headers correctly
- Verify certificate is not expired

**"Certificate not accepted"**
- Verify CA certificate matches issuer
- Check certificate validity dates
- Ensure certificate has correct key usage

### Debug Logging

Check Nginx logs for certificate verification:
```bash
# Nginx error log
tail -f /var/log/nginx/error.log

# Look for ssl_client_verify status
```

Check backend logs:
```bash
./opentr.sh logs backend
```

### Verify Certificate Chain

```bash
# Verify certificate against CA
openssl verify -CAfile ca.crt user.crt

# View certificate details
openssl x509 -in user.crt -text -noout

# Check certificate DN format
openssl x509 -in user.crt -subject -noout
```

## Security Considerations

1. **CA Security**: Protect your CA private key — compromise of the CA allows issuing fraudulent certificates
2. **Certificate Revocation**: Enable OCSP or CRL checking in production to enforce immediate certificate revocation
3. **Certificate Lifecycle**: Plan for certificate renewal before expiry; expired certificates will fail authentication
4. **Key Storage**: Use hardware tokens (CAC, PIV, YubiKey) for high-security environments
5. **DN Validation**: DN matching is case-sensitive and exact — copy DN strings directly from certificate details
6. **Super Admin Fallback**: Ensure the super admin password is documented in a secure location; it is the only non-PKI access path when PKI-only mode is active
7. **mTLS Requirement**: PKI authentication requires NGINX with mTLS (`--with-pki` flag). Both
   dev (`docker-compose.pki-dev.yml`) and prod (`docker-compose.pki.yml`) support it.

## Certificate Revocation

### OCSP (Real-Time, Recommended)

Enable via Admin UI (Settings → Authentication → PKI/X.509 → Enable OCSP) or environment variable:

```bash
PKI_OCSP_ENABLED=true
PKI_OCSP_RESPONDER_URL=http://ocsp.your-ca.com
```

### CRL (Periodic Cache)

Enable via Admin UI or environment variable:

```bash
PKI_CRL_ENABLED=true
PKI_CRL_URL=http://pki.your-ca.com/crl
PKI_CRL_REFRESH_HOURS=24
```

Nginx CRL configuration (optional — additional layer at the TLS termination point):
```nginx
ssl_crl /etc/nginx/certs/ca.crl;
```

Generate CRL for testing:
```bash
# Using Step CA
step ca crl > ca.crl

# Using OpenSSL
openssl ca -gencrl -out ca.crl -config openssl.cnf
```
