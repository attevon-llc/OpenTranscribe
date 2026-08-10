# Authentication & Security Testing Checklist

This document covers comprehensive testing for PKI, LDAP, OIDC (Keycloak), and FedRAMP security features.

## Quick Start

```bash
# Run automated backend tests
./scripts/test-all-auth.sh

# Run FedRAMP-specific tests
./scripts/test-fedramp.sh
```

---

## 1. Classification Banner (FedRAMP AC-8)

### Setup
```bash
# Enable in .env
LOGIN_BANNER_ENABLED=true
LOGIN_BANNER_TEXT="This is a U.S. Government system. Unauthorized access is prohibited."
LOGIN_BANNER_CLASSIFICATION=CUI

# Restart backend
docker compose restart backend
```

### Test Cases

| Test | Steps | Expected Result |
|------|-------|-----------------|
| Banner displays | Open http://localhost:5173/login | Green/Purple/Red banner at top based on classification |
| Consent modal | First visit to login | Full-screen consent dialog appears |
| Acknowledge button | Click "I Acknowledge & Consent" | Modal closes, login form visible |
| Decline button | Click "Exit System" | Browser navigates to about:blank |
| Session persistence | Acknowledge, close tab, reopen | No consent modal (session-based) |
| Classification colors | Try each: UNCLASSIFIED, CUI, SECRET | Correct color per DoD standards |

### Classification Colors
- **UNCLASSIFIED**: Green (#007a33)
- **CUI**: Purple (#502b85)
- **CONFIDENTIAL**: Blue (#0033a0)
- **SECRET**: Red (#c8102e)
- **TOP SECRET**: Orange (#ff671f)

---

## 2. MFA/TOTP (FedRAMP IA-2)

### Setup
```bash
# Enable in .env
MFA_ENABLED=true
MFA_REQUIRED=false  # Set true to require for all users

# Restart backend
docker compose restart backend
```

### Test Cases

| Test | Steps | Expected Result |
|------|-------|-----------------|
| MFA status shown | Login, go to Settings > Security | Shows "MFA not configured" |
| MFA setup | Click "Enable MFA" | QR code displayed |
| QR code scan | Scan with Google Authenticator/Authy | Account added to app |
| Verify setup | Enter 6-digit code | "MFA enabled" message, backup codes shown |
| Login with MFA | Logout, login again | MFA challenge screen appears |
| Valid TOTP code | Enter code from app | Login successful |
| Invalid TOTP code | Enter wrong code | "Invalid code" error |
| Backup code | Click "Use backup code", enter code | Login successful, code consumed |
| Disable MFA | Settings > Disable MFA, enter code | MFA disabled |
| PKI users skip MFA | Login with PKI cert | No MFA challenge (PKI is 2FA) |
| Keycloak users skip MFA | Login with Keycloak | No MFA challenge (Keycloak handles MFA) |

### MFA Flow Diagram
```
User enters username/password
         ↓
    [Password valid?] ──No──> "Invalid credentials"
         ↓ Yes
    [MFA enabled?] ──No──> Return access token
         ↓ Yes
    Return MFA token + challenge
         ↓
    User enters TOTP code
         ↓
    [Code valid?] ──No──> "Invalid code"
         ↓ Yes
    Return access token
```

---

## 3. LDAP/Active Directory

### Setup
```bash
# Start test LDAP server
docker compose -f docker-compose.ldap-test.yml up -d

# Configure in .env
LDAP_ENABLED=true
LDAP_SERVER=localhost
LDAP_PORT=636
LDAP_USE_SSL=true
LDAP_BIND_DN=cn=admin,dc=example,dc=org
LDAP_BIND_PASSWORD=admin
LDAP_SEARCH_BASE=dc=example,dc=org
LDAP_ADMIN_GROUPS=cn=admins,ou=groups,dc=example,dc=org

# Restart backend
docker compose restart backend
```

### Test Cases

| Test | Steps | Expected Result |
|------|-------|-----------------|
| LDAP login | Enter LDAP username/password | Login successful |
| User sync | Login, check database | User created with LDAP info |
| Admin group | Login with admin group member | User has admin role |
| User group | Login with regular user | User has user role |
| Invalid password | Enter wrong password | "Invalid credentials" |
| LDAP down | Stop LDAP, try login | Falls back to local auth or error |
| Local fallback | Create local user, LDAP user exists | Can login with local password |

### Run LDAP Tests
```bash
./scripts/test-ldap-auth.sh
```

---

## 4. Keycloak/OIDC

### Setup
```bash
# Start Keycloak
docker compose -f docker-compose.keycloak.yml up -d keycloak

# Wait for startup (check http://localhost:8180)
# Configure realm, client, users per docs/OIDC_SETUP.md

# Configure in .env
KEYCLOAK_ENABLED=true
KEYCLOAK_SERVER_URL=http://localhost:8180
KEYCLOAK_REALM=opentranscribe
KEYCLOAK_CLIENT_ID=opentranscribe-app
KEYCLOAK_CLIENT_SECRET=<your-secret>
KEYCLOAK_CALLBACK_URL=http://localhost:5174/api/auth/keycloak/callback
KEYCLOAK_USE_PKCE=true

# Restart backend
docker compose restart backend
```

### Test Cases

| Test | Steps | Expected Result |
|------|-------|-----------------|
| Keycloak button shown | Open login page | "Sign in with Keycloak" button visible |
| OIDC flow | Click Keycloak button | Redirect to Keycloak login |
| Login at Keycloak | Enter credentials | Redirect back to app, logged in |
| User sync | Check database | User created with Keycloak info |
| Role mapping | Login with admin role | User has admin role in app |
| PKCE enabled | Check network requests | `code_challenge` in auth URL |
| State validation | Modify state param | "Invalid state" error |
| Token refresh | Wait for token expiry | Auto-refresh works |
| Keycloak MFA | Enable MFA in Keycloak | MFA handled by Keycloak, not app |

### Keycloak Admin Console
- URL: http://localhost:8180
- Credentials: admin / admin

---

## 5. PKI/X.509 Certificate Authentication

### Setup
```bash
# Generate test certificates
./scripts/pki/setup-test-pki.sh

# Configure in .env
PKI_ENABLED=true
PKI_CA_CERT_PATH=/path/to/test-certs/ca/ca.crt
PKI_VERIFY_REVOCATION=false
PKI_ADMIN_DNS=emailAddress=admin@example.com,CN=Admin User,OU=Users,O=OpenTranscribe Admins

# Restart backend
docker compose restart backend
```

### Test Cases (API)

```bash
# Run automated PKI tests
./scripts/pki/test-pki-auth.sh
```

| Test | Steps | Expected Result |
|------|-------|-----------------|
| PKI button shown | Open login page | "Sign in with Certificate" button visible |
| Valid cert (API) | Call with X-Client-Cert-DN header | Access token returned |
| Admin cert | Use admin cert DN | User has admin role |
| User cert | Use regular cert DN | User has user role |
| No cert | Call without header | 401 "Invalid certificate" |
| User sync | Check database | User created from cert DN |

### Test Cases (Browser with mTLS)

For browser testing, you need Nginx configured with mutual TLS:

1. Configure Nginx per `docs/PKI_SETUP.md`
2. Import `.p12` certificate to browser
3. Access site through Nginx
4. Select certificate when prompted

| Test | Steps | Expected Result |
|------|-------|-----------------|
| Browser cert prompt | Click "Sign in with Certificate" | Browser prompts for cert |
| Select cert | Choose imported cert | Login successful |
| No cert available | No cert installed | "Invalid certificate" error |
| Expired cert | Use expired cert | "Certificate expired" error |
| Revoked cert (if CRL enabled) | Use revoked cert | "Certificate revoked" error |

---

## 6. Rate Limiting

### Test Cases

```bash
# Configure rate limit (default: 10/minute)
RATE_LIMIT_AUTH_PER_MINUTE=10
```

| Test | Steps | Expected Result |
|------|-------|-----------------|
| Under limit | Make 5 login attempts | All requests processed |
| At limit | Make 11 login attempts | 11th request returns 429 |
| Rate limit header | Check response headers | `X-RateLimit-*` headers present |
| Different IPs | Same user, different IPs | Each IP has own limit |

---

## 7. Account Lockout

### Test Cases

```bash
# Configure lockout (default: 5 attempts, 15 min)
ACCOUNT_LOCKOUT_THRESHOLD=5
ACCOUNT_LOCKOUT_DURATION_MINUTES=15
ACCOUNT_LOCKOUT_PROGRESSIVE=true
```

| Test | Steps | Expected Result |
|------|-------|-----------------|
| Under threshold | 4 failed attempts | Still can login |
| At threshold | 5 failed attempts | Account locked (returns same error) |
| Lockout transparent | Check response | Same "Invalid credentials" (no enumeration) |
| Progressive | Multiple lockouts | Duration doubles each time |
| Auto unlock | Wait for duration | Account unlocks automatically |

---

## 8. Token Management (FedRAMP AC-12)

### Test Cases

| Test | Steps | Expected Result |
|------|-------|-----------------|
| Refresh token | Login, check response | `refresh_token` included |
| Token refresh | POST /auth/token/refresh | New access token |
| Logout | POST /auth/logout | Token revoked |
| Revoked token | Use token after logout | 401 Unauthorized |
| Logout all | POST /auth/logout/all | All sessions terminated |
| Session list | GET /auth/sessions | List of active sessions |

---

## 9. Password Policy (FedRAMP IA-5)

### Test Cases

```bash
# Configure policy (FedRAMP defaults)
PASSWORD_MIN_LENGTH=12
PASSWORD_REQUIRE_UPPERCASE=true
PASSWORD_REQUIRE_LOWERCASE=true
PASSWORD_REQUIRE_DIGIT=true
PASSWORD_REQUIRE_SPECIAL=true
PASSWORD_HISTORY_COUNT=24
```

| Test | Steps | Expected Result |
|------|-------|-----------------|
| Policy endpoint | GET /auth/password-policy | Returns requirements |
| Short password | Register with "short" | Rejected |
| No uppercase | Register with "alllowercase123!" | Rejected |
| No special | Register with "Password123456" | Rejected |
| Valid password | Register with "SecureP@ss123!" | Accepted |
| Policy in UI | Registration form | Shows requirements |

---

## 10. Audit Logging (FedRAMP AU-2/AU-3)

### Setup
```bash
# Enable audit logging
AUDIT_LOG_ENABLED=true
AUDIT_LOG_FORMAT=json
AUDIT_LOG_TO_OPENSEARCH=true
```

### Verify Logs

```bash
# Check backend logs
docker compose logs backend | grep "auth\."

# Check OpenSearch (if enabled)
curl http://localhost:5180/audit-*/_search?pretty
```

### Events to Verify

| Event | Trigger | Fields |
|-------|---------|--------|
| auth.login.success | Successful login | user_id, username, auth_method |
| auth.login.failure | Failed login | username, error_code |
| auth.logout | User logout | user_id |
| auth.mfa.setup | MFA enabled | user_id |
| auth.mfa.verify | MFA verification | user_id, success |
| auth.password.change | Password changed | user_id |
| auth.account.lockout | Account locked | username, lockout_until |

---

## Automated Test Commands

```bash
# Full test suite
./scripts/test-all-auth.sh

# FedRAMP compliance tests
./scripts/test-fedramp.sh

# LDAP-specific tests
./scripts/test-ldap-auth.sh

# PKI-specific tests
./scripts/pki/test-pki-auth.sh
```

---

## Troubleshooting

### Backend not starting
```bash
docker compose logs backend
```

### Check auth methods
```bash
curl http://localhost:5174/api/auth/methods | jq
```

### Reset environment
```bash
./opentr.sh reset dev
```

### View all settings
```bash
docker compose exec backend env | grep -E "(LDAP|KEYCLOAK|PKI|MFA|BANNER|RATE|LOCKOUT)"
```
