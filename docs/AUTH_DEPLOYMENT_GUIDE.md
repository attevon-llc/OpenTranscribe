# Authentication Deployment Guide

This guide provides quick-start commands for deploying OpenTranscribe with different authentication methods.

## Overview

OpenTranscribe v0.4.0 supports four authentication methods that can all be active simultaneously (hybrid authentication):

| Method | Use Case | Test Container | Dev Mode | Prod Mode |
|--------|----------|----------------|----------|-----------|
| **Local** | Default username/password | N/A (built-in) | ✅ Yes | ✅ Yes |
| **LDAP/AD** | Enterprise directory integration | ✅ LLDAP | ✅ Yes | ✅ Yes |
| **Keycloak/OIDC** | SSO with external identity providers | ✅ Keycloak | ✅ Yes | ✅ Yes |
| **PKI/X.509** | Certificate-based (CAC/PIV cards) | ✅ Self-signed certs | ❌ No* | ✅ Yes |

*PKI requires nginx with mTLS for client certificate verification. Dev mode uses Vite dev server which cannot handle this.

> **v0.4.0 Change**: Authentication settings are now stored encrypted (AES-256-GCM) in the database and managed exclusively via the Super Admin UI (Settings → Authentication). Environment variables continue to work as an initial fallback seed but database configuration always takes precedence.

## Quick Start Commands

### Development Mode

**IMPORTANT:** PKI authentication requires production mode (nginx with mTLS). It cannot work in dev mode which uses Vite dev server.

**Local Authentication Only (Default):**
```bash
./opentr.sh start dev
```

**With LDAP Test Container:**
```bash
./opentr.sh start dev --with-ldap-test
```

**With Keycloak Test Container:**
```bash
./opentr.sh start dev --with-keycloak-test
```

**LDAP + Keycloak Testing:**
```bash
./opentr.sh start dev --with-ldap-test --with-keycloak-test
```

### Production Mode

**Standard Production (Docker Hub images):**
```bash
./opentr.sh start prod
```

**Production with Local Build (Test Before Push):**
```bash
./opentr.sh start prod --build
```

**Production with PKI (HTTPS + Client Certificates):**
```bash
# PKI only works in production mode (requires nginx with mTLS)
./opentr.sh start prod --build --with-pki
```

**Production with All Auth Methods:**
```bash
# All test containers including PKI
./opentr.sh start prod --build --with-pki --with-ldap-test --with-keycloak-test
```

## Authentication Configuration

### Super Admin Account

OpenTranscribe always maintains a local super_admin account for emergency access and authentication configuration:

- **Username:** `admin@example.com`
- **Password:** `password` (change this immediately in production!)
- **Role:** `super_admin`
- **Purpose:** Configure authentication methods, manage users

**Why super_admin exists:**
- Configure LDAP/Keycloak/PKI settings via Admin UI
- "Break glass" account if external IdP systems fail
- PKI mode includes a password fallback so the super admin can always log in with a password even when PKI is the primary method
- Regular admins (from LDAP/Keycloak/PKI) cannot configure authentication settings

### Creating additional super admins

There is **no secret key, bootstrap password, or environment variable** for this, and there
should not be — a shared secret that mints platform owners is worse than the problem it solves.

An existing super_admin promotes another account:

**Settings → Users → (row) → Role → Super Admin.** The option only appears when *you* are a
super_admin, and promoting asks for confirmation because the tier grants authentication
configuration, role changes, and the audit log.

Two guard rails:

- **The last super_admin cannot be demoted or deleted.** Auth configuration is super_admin-gated,
  so losing the last one is unrecoverable without direct database access.
- **External IdPs grant at most `admin`.** `super_admin` is local-only by design, which is what
  keeps the break-glass account independent of the identity provider it exists to rescue you from.

If Settings → Authentication does not appear for you at all, your account is `admin`, not
`super_admin` — that is the single most common cause. On startup the app now repairs any legacy
account carrying the old `role='admin' + is_superuser=true` shape.

### Choosing which methods may authenticate

| Setting | Effect | Where |
|---|---|---|
| `local_enabled` | accounts with a local password may sign in | Settings → Authentication → Local |
| `allow_registration` | anyone may create their own account | Settings → Authentication → Local |
| `ldap_enabled` / `keycloak_enabled` / `pki_enabled` | that method is offered | its own tab |

For a deployment where the directory owns identity, the combination is:
**`ldap_enabled = true`, `local_enabled = false`, `allow_registration = false`.**

Two behaviours to expect:

- The username/password form stays visible. LDAP authenticates through that same form, so it is
  shown whenever local **or** LDAP is enabled.
- Your active super_admin can still sign in with its password even with `local_enabled = false`.
  That exemption is deliberate — see above.

### Configuration Methods

**Precedence order: Database > Environment Variables > Built-in defaults**

1. **Admin UI (Recommended — v0.4.0+):**
   - Log in as super_admin
   - Navigate to Settings → Authentication
   - Configure LDAP, Keycloak, PKI, MFA settings
   - Settings are stored encrypted (AES-256-GCM) in the database
   - Changes take effect immediately without restart

2. **Environment Variables (.env file):**
   - Used as the initial seed on first startup or when no database value exists
   - Database configuration always takes precedence once saved
   - See `.env.example` for all options
   - Changing `.env` after database config is saved has no effect until the database entry is cleared

### Hybrid Authentication (Multiple Methods Simultaneously)

All four authentication methods can be enabled at once. Users see all enabled options on the login screen and can choose their preferred method.

- Each method has independent configuration
- Same email address across methods maps to the same user account
- MFA applies to local and LDAP users; PKI and Keycloak users bypass local MFA (their IdP handles it)

### DEPLOYMENT_MODE for API-Lite Deployments

For deployments that use cloud ASR providers and do not require a local GPU, set:

```bash
DEPLOYMENT_MODE=lite
```

This mode disables the GPU worker requirement and is suitable for cloud-only transcription setups.

## Test Container Details

### LLDAP Test Container

**Access:**
- LDAP server: `localhost:3890`
- Web UI: `http://localhost:17170`
- Admin credentials: `admin` / `admin_password`
- Base DN: `dc=example,dc=com`

**Test Users (create via Web UI or API):**
- Admin: `ldap-admin` / `LdapAdmin123`
- Regular: `ldap-user` / `LdapUser123`

**Configuration in OpenTranscribe:**
- Server: `lldap-test` (or `localhost` for external access)
- Port: `3890`
- Use SSL: `false`
- Bind DN: `uid=admin,ou=people,dc=example,dc=com`
- Bind Password: `admin_password`
- Search Base: `dc=example,dc=com`
- Username Attribute: `uid`
- Admin Users: `ldap-admin`

### Keycloak Test Container

**Access:**
- Keycloak URL: `http://localhost:8180`
- Admin console credentials: `admin` / `admin`
- Realm: `opentranscribe`

**Test Users (create via Admin Console):**
- Create users in the `opentranscribe` realm
- Assign roles: `user` or `admin`
- Set passwords (disable "Temporary" flag)

**Configuration in OpenTranscribe:**
- Server URL: `http://localhost:8180` (or `http://[server-ip]:8180` for LAN access)
- Internal URL: `http://transcribe-app-keycloak-1:8080`
- Realm: `opentranscribe`
- Client ID: `opentranscribe-app`
- Client Secret: (from Keycloak client Credentials tab)
- Callback URL: `http://localhost:5173/login` (FRONTEND page, not backend API!)
- Admin Role: `admin`

### PKI Test Certificates

**Location:** `scripts/pki/test-certs/clients/`

**Available Certificates:**
- `admin.p12` - Admin User (admin@example.com) - **Admin Role**
- `testuser.p12` - Test User (testuser@example.com) - **User Role**
- `john.doe.p12` - John Doe (john.doe@gov.example.com) - **User Role**
- `jane.smith.p12` - Jane Smith (jane.smith@gov.example.com) - **User Role**

**Password:** `changeit` (for all .p12 files)

**Browser Import:**
- **macOS:** Double-click .p12 file, enter password, imported to Keychain
- **Windows/Chrome:** Settings → Security → Manage certificates → Import
- **Firefox:** Settings → Certificates → Your Certificates → Import

**Access:** `https://localhost:5182` (or `https://[server-ip]:5182` for LAN access)

**Configuration in OpenTranscribe:**
- PKI Enabled: `true`
- CA Certificate Path: `/app/scripts/pki/test-certs/ca/ca.crt`
- Admin DNs: `emailAddress=admin@example.com,CN=Admin User,OU=Users,O=OpenTranscribe Admins,L=Arlington,ST=Virginia,C=US`

## Production Deployment

### With Enterprise Active Directory

```bash
# Start OpenTranscribe (no test containers)
./opentr.sh start prod --build

# Configure via Admin UI:
# Settings → Authentication → LDAP
# - Server: ldaps://your-ad-server.domain.com
# - Port: 636
# - Use SSL: true
# - Bind DN: CN=service-account,CN=Users,DC=domain,DC=com
# - Search Base: DC=domain,DC=com
# - Username Attribute: sAMAccountName
# - Admin Users: admin1,admin2,john.doe
```

### With Enterprise Keycloak

```bash
# Start OpenTranscribe (no test containers)
./opentr.sh start prod --build

# Configure via Admin UI:
# Settings → Authentication → Keycloak
# - Server URL: https://keycloak.yourdomain.com
# - Realm: your-realm
# - Client ID: opentranscribe-app
# - Client Secret: (from your Keycloak admin)
# - Callback URL: https://yourdomain.com/login
# - Admin Role: admin
```

### With Production PKI (CAC/PIV Cards)

```bash
# Start with PKI overlay
./opentr.sh start prod --build --with-pki

# Configure via Admin UI:
# Settings → Authentication → PKI/X.509
# - PKI Enabled: true
# - CA Certificate: (upload your organization's CA cert)
# - Admin DNs: (pipe-separated list of admin certificate DNs)
# - Enable OCSP: true (recommended — real-time revocation checking)
# - OCSP Responder URL: https://ocsp.your-ca.domain.com
# - Enable CRL: true (optional — periodic revocation list)
# - CRL Endpoint URL: https://your-ca.domain.com/crl
```

**Note:** OCSP provides real-time certificate revocation checking. When a certificate is revoked, the next login attempt is denied immediately without waiting for a CRL refresh cycle. CRL checking is also available and caches the revocation list locally (refreshed every 24 hours by default).

**Super admin password fallback:** Even when PKI is the only enabled auth method, the super admin account can always log in with a password for emergency access and configuration management.

## Transactional auth email (REQUIRED for password reset and invitations)

Password reset, admin invitations, address verification, and account-security
notices are all email. **Neither `SMTP_HOST` nor `FRONTEND_URL` is set in any of
the compose files**, so a deployment that changes nothing has no working mail path
and no usable link — configure both before enabling local accounts.

### 1. Point `FRONTEND_URL` at the deployment

```bash
# .env — the public base URL users reach the SPA on
FRONTEND_URL=https://transcribe.yourdomain.com
```

Every credential link is built from this value. It defaults to
`http://localhost:5173`, which would mail every user a link to their own machine.
The backend **refuses to send** a credential link still built from that default
once a mail transport is configured, and logs the refusal at `CRITICAL`:

```
CRITICAL FRONTEND_URL is still http://localhost:5173, so '... - Password Reset
Request' would carry a link to the recipient's own machine. Refusing to send.
```

Requires a container **recreate**, not `restart-backend` (env changes do not
survive a plain restart).

### 2. Choose a mail transport

**Preferred — DB-backed provider config (admin UI, no restart, encrypted creds):**

1. Settings → Watch Sources → **Email Configurations** → add a config.
   Providers: `smtp` (STARTTLS or implicit SSL on 465), `m365` (Microsoft Graph
   `sendMail` via client-credentials OAuth2, for tenants with SMTP basic auth
   disabled), `exchange` (authenticated submission to on-prem Exchange).
   Secrets are AES-256-GCM encrypted at rest with `ENCRYPTION_KEY`.
2. Use **Test Connection** to confirm auth and reachability.
3. In the same panel, under **Authentication email** (super_admin only), pick
   that config in the dropdown and **Save**. It takes effect immediately — no
   restart. The panel states what is designated and warns when the designation
   is dangling.

   Nothing is auto-selected. Email configs are created for specific notification
   purposes, and sending password resets out of an unrelated mailbox would leak
   the deployment's auth mail through it — so the designation is always an
   explicit super_admin act, recorded in the `SystemSettings` key
   `email.auth_config_uuid` and written to the audit log.

   The API behind the control is `GET`/`PUT
   /api/admin/auth-config/email/designation` (super_admin). A UUID that names no
   config, or a disabled one, is refused with a 400 rather than stored; an empty
   `config_uuid` clears the designation and falls back to env SMTP. Deleting or
   disabling the designated config is refused with a 409 — clear or move the
   designation first. Should the row disappear another way (direct SQL), auth
   mail degrades to the env SMTP transport below with an error log, never to
   some other config, and the panel reports the designation as dangling.

**Fallback — env SMTP** (used only when nothing is designated):

```bash
SMTP_HOST=smtp.yourdomain.com
SMTP_PORT=587          # 587 = STARTTLS (honours SMTP_USE_TLS); 465 = implicit SSL
SMTP_USER=svc-transcribe
SMTP_PASSWORD=...
SMTP_FROM=noreply@yourdomain.com
SMTP_USE_TLS=true
```

### 3. Verify

Request a reset for a known account and watch the backend log. Delivery is
reported per message with the recipient **masked**; the link itself is never
logged, because a reset URL is a single-use credential.

```bash
./opentr.sh logs backend | grep -i "Sent '\|Failed to send\|mail transport"
```

With no transport at all, a credential-bearing send fails rather than silently
succeeding:

```
ERROR No mail transport configured — '... - Password Reset Request' was NOT
delivered to v***@example.com. Designate an email config
(email.auth_config_uuid) or set SMTP_HOST.
```

## Troubleshooting

### Common Issues

**LDAP: "Invalid credentials"**
- Verify bind DN and password are correct
- Test with `ldapsearch` from command line
- Check service account has read access to user objects

**Keycloak: Shows raw JSON instead of logging in**
- Callback URL must point to FRONTEND (`/login`), not backend API
- Update via Admin UI → Settings → Authentication → Keycloak

**Keycloak: Login page slow to load from remote device**
- Server URL must be accessible from user's browser
- Use server IP address instead of `localhost` for LAN access
- Update Keycloak client redirect URIs to include all access URLs

**PKI: Browser doesn't prompt for certificate**
- Verify certificate is imported to correct keychain/store
- Check browser settings allow client certificate prompts
- macOS: Set private key to "Allow all applications to access"

**Email: users never receive a password reset**
- `SMTP_HOST` is empty and no email config is designated — see
  "Transactional auth email" above. The backend logs
  `No mail transport configured — ... was NOT delivered`.
- The link is never printed to the log by design (it is a live single-use
  credential), so an empty log is not evidence the mail was sent.

**Email: the panel says the designated configuration is missing or disabled**
- The row was deleted or disabled outside the UI (the API refuses both with a
  409). Auth mail has fallen back to env SMTP, which is unset by default — so
  resets and invitations stop. Designate a working config, or clear the
  designation and set `SMTP_HOST`.

**Email: "This email configuration is designated to carry authentication email"**
- You tried to delete or disable the auth mailer. Point the **Authentication
  email** dropdown at another config (or clear it) first, then retry.

**Email: reset links point at `localhost:5173`**
- `FRONTEND_URL` is unset. Set it in `.env` and **recreate** the backend
  container. The send is refused (CRITICAL log) rather than delivered.

**Email: `SMTPServerDisconnected` / TLS handshake errors on port 465**
- 465 is implicit SSL, negotiated before the greeting. Set `SMTP_PORT=465` and
  the backend uses `SMTP_SSL` automatically; `SMTP_USE_TLS` does not apply there.

**PKI: "Certificate verification failed"**
- Ensure CA certificate is correctly configured in admin UI
- Verify client certificate was issued by the configured CA
- Check certificate is not expired or revoked
- If OCSP is enabled, verify the OCSP responder is reachable from the backend container
- If CRL is enabled, verify the CRL endpoint URL is accessible and the CRL has not expired

### Logs

```bash
# Backend authentication logs
docker compose logs -f backend | grep -i "auth\|ldap\|keycloak\|pki"

# LDAP container logs
docker compose logs -f lldap

# Keycloak container logs
docker compose logs -f keycloak
```

## Advanced Usage

### Manual Docker Compose (Without opentr.sh)

**Development with LDAP and Keycloak:**
```bash
docker compose -f docker-compose.yml \
  -f docker-compose.ldap-test.yml \
  -f docker-compose.keycloak.yml \
  up -d --build
```

**Production with PKI (requires nginx + mTLS):**
```bash
docker compose -f docker-compose.yml \
  -f docker-compose.prod.yml \
  -f docker-compose.local.yml \
  -f docker-compose.pki.yml \
  up -d --build
```

**Production with all test containers:**
```bash
docker compose -f docker-compose.yml \
  -f docker-compose.prod.yml \
  -f docker-compose.local.yml \
  -f docker-compose.pki.yml \
  -f docker-compose.ldap-test.yml \
  -f docker-compose.keycloak.yml \
  up -d --build
```

### Hybrid Authentication

Enable multiple methods simultaneously:

```bash
# Development: LDAP + Keycloak (no PKI)
./opentr.sh start dev --with-ldap-test --with-keycloak-test

# Production: All auth methods including PKI
./opentr.sh start prod --build --with-pki --with-ldap-test --with-keycloak-test

# Configure via Admin UI:
# Settings → Authentication
# - Enable: Local, LDAP, Keycloak, PKI
```

Users can then choose their login method on the login page.

**Note:** PKI requires production mode because dev mode uses Vite dev server which cannot handle client certificate verification (mTLS).

### Reset and Test Each Method

Systematically test each authentication method:

```bash
# Test 1: Local only
./opentr.sh reset dev
# Disable all except local in Admin UI
# Test: admin@example.com / password

# Test 2: LDAP only
./opentr.sh reset dev --with-ldap-test
# Enable LDAP, disable others in Admin UI
# Test: ldap-admin / LdapAdmin123

# Test 3: Keycloak only
./opentr.sh reset dev --with-keycloak-test
# Enable Keycloak, disable others in Admin UI
# Test: (Keycloak user) / (password)

# Test 4: PKI only (REQUIRES PRODUCTION MODE)
./opentr.sh reset prod --build --with-pki
# Enable PKI, disable others in Admin UI
# Test: Import admin.p12, access https://localhost:5182
# Note: PKI requires nginx with mTLS, cannot use dev mode
```

## Documentation References

- **PKI Detailed Setup:** `docs/PKI_SETUP.md`
- **LDAP/AD Detailed Setup:** `docs/LDAP_AUTH.md`
- **OIDC Detailed Setup:** `docs/OIDC_SETUP.md`
- **Super Admin Guide:** `docs/SUPER_ADMIN_GUIDE.md`
- **Security Policy:** `docs/SECURITY.md`
- **FIPS Compliance:** `docs/FIPS_140_3_COMPLIANCE.md`
- **Development Guide:** `CLAUDE.md`
- **Environment Variables:** `.env.example`
