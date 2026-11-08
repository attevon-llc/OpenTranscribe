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

⚠️ **The fixture accounts' passwords live in `backend/tests/e2e/ldap_fixture_users.py` and are
transcribed here only as documentation.** They were previously spelled in three places that
disagreed, and the loser of that race fed `ldap-admin`'s progressive lockout bucket. Read the
module (or `python backend/tests/e2e/ldap_fixture_users.py`) if this table and the code differ.

| Username | Password | Role | Email |
|----------|----------|------|-------|
| `ldap-admin` | `LdapAdmin123` | admin | ldap-admin@example.com |
| `ldap-user` | `LdapUser123` | user | ldap-user@example.com |
| `ldap-negative` | (never binds successfully — reserved for the wrong-password test) | — | ldap-negative@example.com |
| `admin` | `admin_password` | LLDAP admin (built-in bootstrap) | (built-in) |

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
./opentr.sh start dev --with-ldap-test
```
The overlay (`docker-compose.ldap-test.yml` + `docker-compose.ldap-test-trust.yml`) serves LDAPS
on `localhost:6360` (certificate from the repo test CA, issued by
`scripts/pki/issue-test-server-cert.sh`) and keeps plaintext `ldap://localhost:3890` for
host-side seeding only. The backend refuses plaintext binds and verifies the certificate, so it
connects with `LDAP_SERVER=ldaps://lldap-test`, `LDAP_PORT=6360` and
`LDAP_CA_CERT_FILE=/etc/opentranscribe/ldap-test-ca.crt` (mounted by the trust overlay).

The same command creates the test users (via
`scripts/lib/dev-test-overlays.sh`'s `seed_ldap_fixture_users`) already does this, reading the
passwords from `backend/tests/e2e/ldap_fixture_users.py` — prefer that over the loop below, which
is here to show the mechanism:

```bash
# Reset passwords for test users, from the single source of truth
backend/venv/bin/python backend/tests/e2e/ldap_fixture_users.py \
  | awk -F'\t' '$1=="USER"{print $2, $3}' \
  | while read -r uid pass; do
      docker exec lldap-test /app/lldap_set_password \
        --base-url "http://localhost:17170" \
        --admin-username admin --admin-password admin_password \
        --username "$uid" --password "$pass"
    done
```

### Start Keycloak
```bash
./opentr.sh start dev --with-keycloak-test   # pre-built image, ~25 s to healthy
```

The realm, client and test users are described in `docs/KEYCLOAK_SETUP.md`.

---

## Running Tests

### Backend Unit/Integration Tests

```bash
source backend/venv/bin/activate

# Auth config integration tests (database round-trip, LdapConfig/KeycloakConfig from_db)
pytest backend/tests/test_auth_config_integration.py -v

# PKI auth tests (certificate parsing, endpoint integration)
pytest backend/tests/pki_auth -v

# MFA security tests and auth config service tests (run by default; the old
# RUN_MFA_TESTS / RUN_AUTH_CONFIG_TESTS gates are gone, see backend/tests/CLAUDE.md)
pytest backend/tests/test_mfa_security.py backend/tests/test_auth_config_service.py -v

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
./opentr.sh start prod --build --with-pki
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
s = Server('localhost', port=3890)  # plaintext host-side port, seeding/inspection only
c = Connection(s, 'uid=admin,ou=people,dc=example,dc=com', 'admin_password', auto_bind=True)
c.search('ou=people,dc=example,dc=com', '(objectClass=person)', attributes=['uid', 'mail'])
for e in c.entries: print(e.uid, e.mail)
c.unbind()
"
```
