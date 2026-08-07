# Authentication Test Setup Guide

Quick reference for setting up and running authentication tests across all auth methods.

## Test Credentials

### Local Auth
| Field | Value |
|-------|-------|
| Email | `admin@example.com` |
| Password | `password` |
| Role | super_admin |

### LDAP (LLDAP Container)

| Username | Password | Role | Email |
|----------|----------|------|-------|
| `ldap-admin` | `admin_password` | admin | ldap-admin@example.com |
| `ldap-user` | `user_password` | user | ldap-user@example.com |
| `testadmin` | `admin_password` | admin | testadmin@example.com |
| `testuser` | `user_password` | user | testuser@example.com |
| `admin` | `admin_password` | LLDAP admin | (built-in) |

**LLDAP Admin UI:** http://localhost:17170 (admin / admin_password)

### Keycloak (OIDC Container)

| Username | Password | Role | Realm |
|----------|----------|------|-------|
| `admin` | `admin` | Keycloak admin | master |
| `testuser` | `testpass` | realm user | opentranscribe |
| `testadmin` | `testpass` | realm admin | opentranscribe |

**Keycloak Admin Console:** http://localhost:8180
**Client ID:** `opentranscribe`
**Client Secret:** (see `KEYCLOAK_CLIENT_SECRET` in `.env`)

### PKI Certificates

Test certificates at `scripts/pki/test-certs/clients/`:

| Certificate | DN | Role | PFX Password |
|---|---|---|---|
| `admin.crt` / `admin.p12` | CN=Admin User, O=OpenTranscribe Admins | admin | `changeit` |
| `testuser.crt` / `testuser.p12` | CN=Test User, O=OpenTranscribe Users | user | `changeit` |
| `john.doe.crt` / `john.doe.p12` | CN=John Doe, O=Department of Testing | user | `changeit` |
| `jane.smith.crt` / `jane.smith.p12` | CN=Jane Smith, O=Department of Testing | user | `changeit` |

**CA Certificate:** `scripts/pki/test-certs/ca/ca.crt`

---

## Container Setup

### Start Dev Environment
```bash
./opentr.sh start dev
```

### Start LLDAP (LDAP)
```bash
docker run -d --name lldap-test \
  --network transcribe-app_default \
  -p 3890:3890 -p 17170:17170 \
  -e LLDAP_LDAP_BASE_DN="dc=example,dc=com" \
  -e LLDAP_LDAP_USER_PASS="admin_password" \
  -e LLDAP_JWT_SECRET="test-jwt-secret-key-change-me" \
  lldap/lldap:stable
```

Create test users after LLDAP starts:
```bash
# Reset passwords for test users
docker exec lldap-test /app/lldap_set_password \
  --base-url "http://localhost:17170" \
  --admin-username admin --admin-password admin_password \
  --username ldap-admin --password admin_password

docker exec lldap-test /app/lldap_set_password \
  --base-url "http://localhost:17170" \
  --admin-username admin --admin-password admin_password \
  --username ldap-user --password user_password
```

### Start Keycloak
```bash
docker run -d --name keycloak-test \
  --network transcribe-app_default \
  -p 8180:8080 \
  -e KEYCLOAK_ADMIN=admin \
  -e KEYCLOAK_ADMIN_PASSWORD=admin \
  quay.io/keycloak/keycloak:latest start-dev
```

Then configure the realm, client, and test users (see `docs/KEYCLOAK_SETUP.md`).

---

## Running Tests

### Backend Unit/Integration Tests

```bash
source backend/venv/bin/activate

# Auth config integration tests (database round-trip, LdapConfig/KeycloakConfig from_db)
pytest backend/tests/test_auth_config_integration.py -v

# PKI auth tests (certificate parsing, endpoint integration)
pytest backend/tests/test_pki_auth.py -v

# MFA security tests
RUN_MFA_TESTS=true pytest backend/tests/test_mfa_security.py -v

# Auth config service tests (mock-based)
RUN_AUTH_CONFIG_TESTS=true pytest backend/tests/test_auth_config_service.py -v

# All backend tests (excludes E2E)
pytest backend/tests/ --ignore=backend/tests/e2e -v
```

### E2E Browser Tests (Playwright)

Requires dev environment running with all services up.

```bash
source backend/venv/bin/activate

# Login buttons and auth flow tests (always runs)
pytest backend/tests/e2e/test_auth_buttons.py -v

# LDAP and Keycloak config + login tests (requires containers)
RUN_AUTH_E2E=true pytest backend/tests/e2e/test_ldap_oidc.py -v

# PKI E2E tests (requires PKI nginx overlay with TLS)
RUN_PKI_E2E=true pytest backend/tests/e2e/test_pki.py -v --headed

# MFA E2E tests
pytest backend/tests/e2e/test_mfa.py -v

# Run with visible browser (XRDP display)
DISPLAY=:11 pytest backend/tests/e2e/test_auth_buttons.py -v --headed

# All E2E tests
pytest backend/tests/e2e/ -v
```

### E2E Test Environment Flags

| Flag | Description |
|------|-------------|
| `RUN_AUTH_E2E=true` | Enable LDAP/Keycloak E2E config and login tests |
| `RUN_PKI_E2E=true` | Enable PKI cert E2E tests (requires TLS overlay) |
| `RUN_MFA_TESTS=true` | Enable MFA unit/integration tests |
| `RUN_AUTH_CONFIG_TESTS=true` | Enable mock auth config service tests |

---

## Troubleshooting

### LDAP Account Locked Out
Too many failed login attempts trigger Redis-based lockout. To clear:
```bash
REDIS_PASS=$(grep '^REDIS_PASSWORD=' .env | cut -d= -f2)
docker exec opentranscribe-redis redis-cli -a "$REDIS_PASS" --no-auth-warning KEYS '*lockout*'
docker exec opentranscribe-redis redis-cli -a "$REDIS_PASS" --no-auth-warning DEL lockout:ldap-admin lockout:ldap-user
```

### Reset LDAP User Password
```bash
docker exec lldap-test /app/lldap_set_password \
  --base-url "http://localhost:17170" \
  --admin-username admin --admin-password admin_password \
  --username USERNAME --password NEW_PASSWORD
```

### PKI Tests Need TLS
PKI E2E tests require the nginx PKI overlay running on HTTPS:
```bash
docker compose -f docker-compose.yml -f docker-compose.pki-dev.yml up -d
```
PKI URL: `https://localhost:5182`

### Check Auth Methods Status
```bash
curl -s http://localhost:5174/api/auth/methods | python3 -m json.tool
```

### Verify LDAP Connectivity
```bash
python3 -c "
from ldap3 import Server, Connection
s = Server('localhost', port=3890)
c = Connection(s, 'uid=admin,ou=people,dc=example,dc=com', 'admin_password', auto_bind=True)
c.search('ou=people,dc=example,dc=com', '(objectClass=person)', attributes=['uid', 'mail'])
for e in c.entries: print(e.uid, e.mail)
c.unbind()
"
```
