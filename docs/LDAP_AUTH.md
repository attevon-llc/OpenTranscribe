# LDAP/Active Directory Authentication

## Overview

OpenTranscribe supports hybrid authentication combining LDAP/Active Directory with local database users:

- **LDAP users**: Auto-created on first login with role based on `LDAP_ADMIN_USERS`
- **Local admin users**: Created manually via registration endpoint with database passwords
- **Flexible auth**: System supports both email-based (local) and **username-based (LDAP)** login — users can log in with their sAMAccountName, uid, or any configured username attribute
- **Priority**: Local users checked first when `auth_type='local'` in database

> **v0.4.0 Change**: LDAP configuration is now managed via the Super Admin UI (Settings → Authentication → LDAP/Active Directory). Settings are stored encrypted (AES-256-GCM) in the database. Environment variables (`LDAP_SERVER`, `LDAP_BASE_DN`, etc.) continue to work as an initial seed fallback but database configuration takes precedence once saved.

## Configuration

### Primary Method: Super Admin UI (v0.4.0+)

Configure LDAP via the Admin UI for immediate, encrypted, restart-free configuration:

1. Log in as super_admin (`admin@example.com`)
2. Navigate to Settings → Authentication → LDAP/Active Directory
3. Click "Edit" and fill in all fields (see table in SUPER_ADMIN_GUIDE.md)
4. Click "Test Connection" to verify before saving
5. Click "Save Configuration"

Settings saved via the UI are encrypted with AES-256-GCM and take effect immediately.

### Environment Variables (Fallback / Initial Seed)

These variables are used only when no database configuration exists for LDAP. Once configuration is saved via the Admin UI, these env vars are ignored.

Add the following to your `.env` file:

```env
# Enable LDAP authentication
LDAP_ENABLED=true

# LDAP/AD Server
LDAP_SERVER=ldaps://your-ad-server.domain.com
LDAP_PORT=636
LDAP_USE_SSL=true
LDAP_USE_TLS=false

# Service account for LDAP search (read-only user)
LDAP_BIND_DN=CN=service-account,CN=Users,DC=domain,DC=com
LDAP_BIND_PASSWORD=your-service-account-password

# Search base and filter
LDAP_SEARCH_BASE=DC=domain,DC=com
LDAP_USERNAME_ATTR=sAMAccountName  # Username attribute (sAMAccountName, uid, etc.)
LDAP_USER_SEARCH_FILTER=({username_attr}={username})  # Uses LDAP_USERNAME_ATTR

# User attributes
LDAP_EMAIL_ATTR=mail
LDAP_NAME_ATTR=cn

# Timeout (seconds)
LDAP_TIMEOUT=10

# Optional: Comma-separated list of AD usernames that should be admins
LDAP_ADMIN_USERS=admin1,admin2,john.doe
```

### Configuration Details

- **LDAP_SERVER**: Full LDAP URL (ldaps:// for secure LDAP)
- **LDAP_PORT**: 389 for standard LDAP, 636 for LDAPS
- **LDAP_USE_SSL**: Enable LDAPS (recommended for production)
- **LDAP_USE_TLS**: Alternative to LDAPS (StartTLS)
- **LDAP_BIND_DN**: Distinguished Name of service account (must have read access to user objects)
- **LDAP_BIND_PASSWORD**: Password for service account (stored in .env)
- **LDAP_SEARCH_BASE**: Base DN for user searches (e.g., DC=domain,DC=com)
- **LDAP_USERNAME_ATTR**: Username attribute for user search (default: `sAMAccountName`)
  - Active Directory: `sAMAccountName`
  - OpenLDAP: `uid`
- **LDAP_USER_SEARCH_FILTER**: Filter to find users by username
  - Format: `({username_attr}={username})` where `{username_attr}` is replaced by `LDAP_USERNAME_ATTR`
  - Active Directory: `(sAMAccountName={username})`
  - OpenLDAP: `(uid={username})`
- **LDAP_EMAIL_ATTR**: Attribute containing user email (mail, userPrincipalName, etc.)
- **LDAP_NAME_ATTR**: Attribute containing full name (cn, displayName, etc.)
- **LDAP_ADMIN_USERS**: List of AD usernames that should automatically be admins

### Username Attribute Configuration

The `LDAP_USERNAME_ATTR` setting provides flexibility for different directory services:

**Active Directory (default):**
```env
LDAP_USERNAME_ATTR=sAMAccountName
LDAP_USER_SEARCH_FILTER=(sAMAccountName={username})
```

**OpenLDAP:**
```env
LDAP_USERNAME_ATTR=uid
LDAP_USER_SEARCH_FILTER=(uid={username})
```

**Custom Attribute:**
```env
LDAP_USERNAME_ATTR=employeeId
LDAP_USER_SEARCH_FILTER=(employeeId={username})
```

The system automatically replaces `{username_attr}` in `LDAP_USER_SEARCH_FILTER` with the value of `LDAP_USERNAME_ATTR`, allowing consistent filter configuration across different directory services.

## Authentication Flow

### Hybrid Authentication Strategy

The system uses a flexible authentication flow based on the user's `auth_type` in the database:

#### 1. Local User (Database Password)

```python
User exists in DB + auth_type = 'local'
  → Try direct database authentication (bypasses ORM for reliability)
  → If direct fails, try ORM authentication
  → Success: Return JWT token
  → Failure: Try LDAP (as fallback if user exists)
```

#### 2. LDAP User

```python
LDAP Enabled OR user not found locally
  → Bind to AD with service account
  → Search for user by LDAP_USERNAME_ATTR (or email if username contains @)
  → If not found by username, try search by email address
  → Extract email, username, and cn attributes
  → Bind as user to verify password
  → Create/update user in database
  → Return JWT token
```

#### 3. First-Time LDAP Login

```python
User not in database
  → Authenticate via LDAP
  → Create user record:
    - email: from AD
    - full_name: from AD
    - ldap_uid: sAMAccountName
    - auth_type: 'ldap'
    - role: 'admin' if in LDAP_ADMIN_USERS else 'user'
  → Return JWT token
```

### Login Input Options

Users can login with:
- **Email address** (for local users, also supported for LDAP users)
- **Username/sAMAccountName** (for LDAP users)

The system automatically handles both formats:

```bash
# Local user login (email-based)
curl -X POST http://localhost:5174/api/auth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin@example.com&password=local_password"

# LDAP user login (username-based)
curl -X POST http://localhost:5174/api/auth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=john.doe&password=ad_password"

# LDAP user login (email-based - extracts username before @)
curl -X POST http://localhost:5174/api/auth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=john.doe@example.com&password=ad_password"
```

**LDAP Email Login Support**:
- When username contains `@`, the system extracts the part before `@` as the LDAP username
- System first searches by `LDAP_USERNAME_ATTR` using extracted username
- If not found, system searches by `LDAP_EMAIL_ATTR` using the full email
- This provides flexibility for users who prefer email-based login

## User Creation

### LDAP Users

- **Auto-created**: On first successful LDAP authentication
- **No registration required**: Users login with AD credentials
- **Synchronized**: Email and name updated on each login
- **Role management**: Admin status determined by `LDAP_ADMIN_USERS` list

### Local Admin Users

```bash
# Via API registration (admin must create manually)
curl -X POST http://localhost:5174/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin@example.com",
    "password": "secure_password",
    "full_name": "Local Admin",
    "role": "admin"
  }'
```

**Important**: Local registration should be done carefully in production to prevent unauthorized user creation.

## Security Features

### LDAP Injection Protection

The system escapes special characters in LDAP filter values to prevent injection attacks:

```python
_escaped_username = _escape_ldap_filter(username)
# Escapes: ( ) * \ NUL characters
```

### Service Account Permissions

- Create a read-only service account in AD
- Grant only necessary permissions (read user attributes)
- Use strong password and regular rotation
- Store password securely in `.env` (not in code)

### SSL/TLS

- **LDAPS** (LDAP over SSL) is recommended for production
- Use port 636 with `LDAP_USE_SSL=true`
- Ensure valid SSL certificate on AD server
- Test connection: `openssl s_client -connect ad-server:636`

### JWT Token Security

- Tokens use UUID as user identifier (not integer ID)
- Role information included in token for immediate permission updates
- Tokens expire based on `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` setting

### Audit Logging

All authentication attempts are logged:

```python
logger.info(f"LDAP authentication successful for user: {username}")
logger.warning(f"LDAP authentication failed for user: {username}")
logger.info(f"New local user registered: {email}")
logger.info(f"Direct authentication successful for local user: {username}")
```

Check logs for:
- Failed authentication attempts
- New user creations
- Role changes
- Authentication method used (direct vs LDAP)

### Password Change Restrictions

For security and consistency with directory services:

- **LDAP users cannot change passwords** in OpenTranscribe
  - Password changes must be done in AD/LDAP directory
  - Frontend (`SettingsModal`) hides password change section for LDAP users
  - API endpoint (`update_current_user`, `update_user`) rejects password changes for `auth_type='ldap'`

- **Local users only** can change their password
  - Both password change UI and API check user's `auth_type`
  - Attempted password changes by LDAP users are rejected with appropriate error

- **LDAP user password authentication blocked**
  - Direct authentication checks user's `auth_type` before attempting password verification
  - If `auth_type='ldap'`, authentication fails immediately with logged message
  - LDAP users must use LDAP authentication flow

**Frontend Implementation:**
- `SettingsModal` component checks `user.auth_type === 'local'` before showing password change section
- `UserManagementTable` component conditionally displays password reset options based on `auth_type`
- Users with `auth_type='ldap'` see no password change options in UI

**Backend Implementation:**
- `direct_auth.py`: `direct_authenticate_user()` rejects LDAP users with clear log message
- `users.py`: `update_current_user()` and `update_user()` validate `auth_type` before allowing password updates
- Frontend receives appropriate error messages if password change is attempted for LDAP users

This ensures:
1. Passwords remain synchronized with AD/LDAP
2. No confusion about password management responsibilities
3. Security by preventing workarounds for password policies
4. Clear user experience (LDAP users don't see password change options)

## Troubleshooting

### LDAP Connection Issues

```bash
# Test connection with ldap3
python3 -c "
from ldap3 import Server, Connection
server = Server('ldaps://ad-server.domain.com', port=636, use_ssl=True)
conn = Connection(server, 'CN=service,CN=Users,DC=domain,DC=com', 'password', auto_bind=True)
print('Connected successfully')
"
```

### Search Filter Issues

```bash
# Test user search with ldapsearch
ldapsearch -H ldaps://ad-server.domain.com:636 \
  -D 'CN=service,CN=Users,DC=domain,DC=com' \
  -W \
  -b 'DC=domain,DC=com' \
  '(sAMAccountName=john.doe)' \
  mail cn
```

### Common Errors

1. **"Failed to bind to LDAP server"**
   - Check LDAP_SERVER and LDAP_PORT
   - Verify service account credentials
   - Check firewall rules

2. **"User not found in LDAP"**
   - Verify LDAP_SEARCH_BASE
   - Check LDAP_USER_SEARCH_FILTER
   - Ensure user exists in AD

3. **"User has no email attribute"**
   - Verify LDAP_EMAIL_ATTR
   - Ensure users have email populated in AD

4. **"LDAP authentication failed"**
   - Verify user password
   - Check account is not locked/disabled
   - Verify user can log in to AD normally

5. **"Direct auth failed, trying ORM auth"**
    - This is expected fallback behavior
    - Indicates direct DB connection issue
    - ORM auth will be tried automatically

6. **"Authentication failed: user is LDAP type, cannot use password auth"**
    - User has `auth_type='ldap'` in database
    - Password authentication is blocked for LDAP users
    - Use LDAP authentication flow (not local password)
    - If user should use local passwords, update `auth_type` to 'local' in database

7. **"Password change not allowed for LDAP users"**
    - LDAP users cannot change passwords in OpenTranscribe
    - Password changes must be done in AD/LDAP directory
    - Frontend hides password change options for LDAP users
    - If user needs local password, update `auth_type` to 'local' in database

## Deployment

### Development/Testing with LDAP Test Container

For development and testing, use the built-in LLDAP test container:

```bash
# Start OpenTranscribe with LDAP test container
./opentr.sh start dev --with-ldap-test

# Or for production mode (testing before push)
./opentr.sh start prod --build --with-ldap-test
```

**Test Container Details:**
- LDAP server: `localhost:3890`
- Web UI: `http://localhost:17170`
- Admin credentials: `admin` / `admin_password`
- Base DN: `dc=example,dc=com`

**Configure OpenTranscribe to use test LDAP:**
1. Log in as super_admin (admin@example.com / password)
2. Go to Settings → Authentication → LDAP/Active Directory
3. Configure:
   - Server: `lldap-test` (network alias — stays valid even when `--fresh` renames the container)
   - Port: `3890`
   - Use SSL: `false`
   - Bind DN: `uid=admin,ou=people,dc=example,dc=com`
   - Bind Password: `admin_password`
   - Search Base: `dc=example,dc=com`
   - Username Attribute: `uid`

### Production Deployment with Active Directory

For production with your organization's Active Directory:

```bash
# Production mode (Docker Hub images)
./opentr.sh start prod

# Or with local builds (test before push)
./opentr.sh start prod --build
```

**Configure OpenTranscribe:**
1. Log in as super_admin
2. Go to Settings → Authentication → LDAP/Active Directory
3. Configure with your AD settings:
   - Server: `ldaps://your-ad-server.domain.com`
   - Port: `636`
   - Use SSL: `true`
   - Bind DN: `CN=service-account,CN=Users,DC=domain,DC=com`
   - Bind Password: (your service account password)
   - Search Base: `DC=domain,DC=com`
   - Username Attribute: `sAMAccountName`
   - Admin Users: (comma-separated list of AD usernames)

**Advanced (manual docker compose):**
```bash
# With LDAP test container
docker compose -f docker-compose.yml -f docker-compose.ldap-test.yml up -d

# Production mode
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

## Testing

### Test LDAP Authentication

```python
from app.auth.ldap_auth import ldap_authenticate

result = ldap_authenticate("jdoe", "password")
print(result)
# Output: {'username': 'jdoe', 'email': 'jdoe@domain.com', 'full_name': 'John Doe', 'is_admin': False}
```

### Test Direct Authentication

```python
from app.auth.direct_auth import direct_authenticate_user

result = direct_authenticate_user("admin@example.com", "password")
print(result)
# Output: {'id': 1, 'email': 'admin@example.com', 'full_name': 'Admin User', 'role': 'admin', 'is_active': True, 'is_superuser': True}
```

### Test Hybrid Authentication

```bash
# Test LDAP user
curl -X POST http://localhost:5174/api/auth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=jdoe&password=ad_password"

# Test local user
curl -X POST http://localhost:5174/api/auth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin@example.com&password=local_password"
```

## Database Schema

### User Table (Updated)

```sql
CREATE TABLE "user" (
    id SERIAL PRIMARY KEY,
    uuid UUID UNIQUE NOT NULL DEFAULT gen_random_uuid(),  -- Public identifier
    email VARCHAR(255) UNIQUE NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_superuser BOOLEAN NOT NULL DEFAULT FALSE,
    role VARCHAR(50) DEFAULT 'user',
    auth_type VARCHAR(10) DEFAULT 'local' NOT NULL,  -- 'local' or 'ldap'
    ldap_uid VARCHAR(255) UNIQUE NULL,  -- sAMAccountName
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_user_uuid ON "user"(uuid);
CREATE INDEX idx_user_ldap_uid ON "user"(ldap_uid);
```

### Key Design Decisions

- **UUID as public identifier**: Used in JWT tokens and API responses
- **Integer ID for internal operations**: Faster database joins and queries
- **auth_type field**: Determines authentication method to try first
- **ldap_uid indexed**: Fast lookups for LDAP users

## Migration from Local to LDAP

For existing systems with local users:

1. Enable LDAP in `.env`
2. Users with existing accounts continue using local passwords (`auth_type='local'`)
3. New users can use LDAP credentials
4. Optional: Update existing users to LDAP:

```sql
-- Set auth_type for existing users to use LDAP
UPDATE "user"
SET auth_type = 'ldap',
    ldap_uid = SPLIT_PART(email, '@', 1),
    hashed_password = ''  -- Empty since auth handled by LDAP
WHERE email LIKE '%@domain.com';

-- Note: Users must have matching sAMAccountName in AD
```

### Important Migration Considerations

**Existing Local Users Protection:**
- When an LDAP user logs in with an email matching an existing local user, the system does NOT convert them to LDAP
- The local user's `auth_type` remains 'local', preserving their local password
- This prevents accidental account takeover by LDAP users with matching emails
- To convert, manually update `auth_type` in the database after verification

**Admin Role Management:**
- Admin status is determined by `LDAP_ADMIN_USERS` on each login
- If a user is removed from `LDAP_ADMIN_USERS`, they are demoted to regular user on next login
- If a user is added to `LDAP_ADMIN_USERS`, they are promoted to admin on next login
- Changes take effect immediately upon next successful LDAP authentication

**Race Condition Handling:**
- Concurrent first-time logins by the same LDAP user are handled gracefully
- If two logins occur simultaneously, one creates the user, the other fetches the existing record
- No duplicate users will be created due to IntegrityError handling
- Both sessions will succeed and receive valid tokens

**Token and Role Synchronization:**
- JWT tokens contain the role at time of login
- Database is the source of truth for roles (not the token)
- If role changes while token is valid, user should re-login to get updated token
- Token manipulation cannot escalate privileges (database role is always checked)

## Performance Considerations

- **LDAP search caching**: ldap3 caches connections
- **Direct database auth**: Bypasses ORM for faster local authentication
- **UUID indexing**: Fast lookups for token validation
- **Database lookup**: First check for local user by email or ldap_uid
- **Connection pooling**: Re-use LDAP connections for multiple requests
- **Timeout**: Set `LDAP_TIMEOUT` to appropriate value (default: 10s)

## Production Checklist

- [ ] LDAPS enabled with valid SSL certificate
- [ ] Read-only service account created
- [ ] Service account password stored securely in Admin UI (encrypted AES-256-GCM)
- [ ] LDAP config saved via Super Admin UI (Settings → Authentication → LDAP/AD)
- [ ] LDAP_ADMIN_USERS configured for admins
- [ ] LDAP_USERNAME_ATTR configured correctly for your directory
- [ ] Firewall allows outbound LDAP connections from backend container
- [ ] Test authentication with real AD users (username-based — e.g., `jdoe`)
- [ ] Test authentication with real AD users (email-based — e.g., `jdoe@domain.com`)
- [ ] Test authentication with local users
- [ ] Verify audit logging is working
- [ ] Monitor LDAP connection errors in logs
- [ ] Verify direct auth fallback works
- [ ] Check UUID-based token validation
- [ ] Test role updates via LDAP_ADMIN_USERS
- [ ] Verify LDAP users cannot change passwords in UI
- [ ] Verify LDAP users cannot change passwords via API
- [ ] Verify password change UI only shows for local users
- [ ] Test LDAP authentication with email containing @ symbol

## Implementation Details

### Directory Structure

```
backend/app/auth/
├── direct_auth.py       # Direct DB authentication (bypasses ORM)
├── ldap_auth.py         # LDAP/Active Directory authentication
├── keycloak_auth.py     # OIDC/Keycloak integration
├── pki_auth.py          # PKI/X.509 certificate authentication
├── mfa.py               # TOTP multi-factor authentication (RFC 6238)
├── password_policy.py   # Password strength enforcement (FedRAMP IA-5)
├── password_history.py  # Password reuse prevention
├── rate_limit.py        # Per-IP and per-user rate limiting
├── lockout.py           # Account lockout (NIST AC-7 compliant)
├── session.py           # Session/token management
├── token_service.py     # JWT token operations
├── audit.py             # Structured JSON and CEF audit logging
└── constants.py         # Auth-related constants

backend/app/api/endpoints/
└── auth.py              # Authentication endpoints (login, register)
```

### Key Functions

**ldap_auth.py:**
- `_escape_ldap_filter()`: Prevents LDAP injection attacks
- `_get_ldap_server()`: Creates configured LDAP server object
- `ldap_authenticate()`: Authenticates user against AD (supports username or email)
- `sync_ldap_user_to_db()`: Creates/updates user from LDAP data

**direct_auth.py:**
- `direct_authenticate_user()`: Direct DB authentication (bypasses ORM, includes auth_type check)
- `verify_password()`: Password verification with bcrypt_sha256
- `create_access_token()`: JWT token generation

**auth.py:**
- `_authenticate_ldap_user()`: LDAP authentication handler
- `_authenticate_production_user()`: Hybrid authentication flow
- `get_current_user()`: JWT token validation with UUID
- `login_for_access_token()`: OAuth2 token endpoint

**users.py:**
- `update_current_user()`: Includes auth_type validation for password changes
- `update_user()`: Includes auth_type validation for password changes (admin-only)

### Password Hashing

The system uses `bcrypt_sha256` for password hashing:
- Pre-hashes passwords with SHA256 (overcomes bcrypt's 72-byte limitation)
- Supports existing `bcrypt` hashes (will auto-upgrade on verify)
- Default rounds: 12 for both schemes
