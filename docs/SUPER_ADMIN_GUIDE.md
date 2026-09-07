# Super Admin Guide

This guide covers the super admin role and authentication configuration management in OpenTranscribe.

## Overview

The **super admin** role provides full system access including:
- Authentication method configuration (LDAP, Keycloak, PKI)
- User account management (lock/unlock, password reset, MFA reset)
- Session management (view/terminate user sessions)
- Audit log access and export
- Role management (promote users to admin/super_admin)

## Accessing Super Admin Features

### First-Time Setup

Bootstrap the first super admin using one of these methods:

**Option 1: Environment Variable**
```bash
BOOTSTRAP_SUPER_ADMIN_EMAIL=admin@example.com
```
The first user with this email will automatically be assigned super_admin role.

**Option 2: Manual Database Update**
```sql
UPDATE "user" SET role = 'super_admin' WHERE email = 'admin@example.com';
```

### Role Hierarchy

| Role | Permissions |
|------|-------------|
| `user` | Standard access to transcription features |
| `admin` | User management, system statistics, task monitoring |
| `super_admin` | All admin permissions + auth configuration + audit logs |

## Super Admin UI Navigation

### Accessing the Settings UI

The Settings interface is accessible only to super admin users:

1. **Log in** with a super admin account
2. **Click the gear icon** (⚙️) in the top-right corner or bottom-left of the sidebar
3. **Select "Settings"** from the dropdown menu
4. You will be taken to the Settings dashboard

### Settings Dashboard

The Settings dashboard provides navigation to all administrative functions:

```
Settings
├── Authentication (main auth config tab)
│   ├── Local Auth
│   ├── LDAP/Active Directory
│   ├── Keycloak/OIDC
│   ├── PKI/Certificate
│   ├── MFA Settings
│   ├── Password Policy
│   ├── Sessions
│   └── Audit Log
├── Users (user management)
├── Groups (user group management)
├── Admin
│   └── File Retention (auto-deletion policies for old transcriptions)
├── AI
│   ├── Organization Context (context injected into LLM summary prompts)
│   └── Per-Collection Default AI Prompt
├── ASR
│   └── Local Model (pin warm model in GPU VRAM, restart GPU worker)
├── Collections (sharing and admin controls)
├── System (system-wide configuration)
└── Audit Logs (compliance view — structured JSON and CEF export)
```

### Permission Requirements

**Super Admin access is required for:**
- Viewing the Settings page
- Modifying any authentication configuration
- Accessing the Audit Log viewer
- Resetting user MFA
- Changing user roles
- Modifying password policies
- Configuring PKI certificates
- Managing user groups
- Configuring file retention policies
- Setting ASR local model and restarting the GPU worker
- Setting organization AI context and per-collection prompts

**Admin access is sufficient for:**
- Locking/unlocking user accounts
- Terminating user sessions
- Viewing active users
- Basic user search and filtering

### Navigation Flow

**To configure authentication:**
1. Settings (gear icon) → Settings
2. Click "Authentication" tab
3. Select the method you want to configure (Local, LDAP, Keycloak, PKI)
4. Click "Edit" button to enable modification mode
5. Fill in required fields with test connection button
6. Click "Save Configuration"

**To view audit logs:**
1. Settings (gear icon) → Settings
2. Click "Audit Logs" tab or section
3. Use filters (date range, event type, outcome, user)
4. Click "Export CSV" or "Export JSON" for compliance reporting

**To manage users:**
1. Settings (gear icon) → Settings
2. Click "Users" tab
3. Search by email, name, or role
4. Click user row to see actions (lock, reset MFA, change role)
5. Click "View Sessions" to see active login sessions

## Database-Driven Configuration System

### How Configuration Works

As of v0.4.0, OpenTranscribe uses a **database-driven configuration model** that eliminates the need to restart services when changing authentication settings:

1. **Configuration Storage**: All authentication settings are stored in the `auth_config` table
2. **Encryption**: Sensitive values (passwords, API keys, secrets) are encrypted with **AES-256-GCM**
3. **Real-Time Application**: Configuration changes take effect immediately without service restart
4. **Precedence**: Database settings override environment variables (`.env` file)
5. **Fallback**: If database has no configuration, system falls back to `.env` values

### Configuration Table Structure

```
auth_config table
├── config_type (VARCHAR) - Type of configuration (local, ldap, keycloak, pki, mfa, etc.)
├── key (VARCHAR) - Configuration key
├── value (TEXT) - Configuration value (encrypted if sensitive)
├── is_encrypted (BOOLEAN) - Whether value is encrypted
├── updated_by (VARCHAR) - Super admin email who made change
├── updated_at (TIMESTAMP) - When configuration was changed
└── comments (TEXT) - Admin notes about the configuration
```

### Multi-Method Support

The system supports **enabling multiple authentication methods simultaneously** (hybrid authentication):

- All enabled methods are presented to users on the login screen
- Users can log in with any enabled method
- Each method can have independent configuration
- Users' authentication method is tracked for security auditing
- Same email can be used across different auth methods

### Encryption of Sensitive Values

Sensitive configuration values are automatically encrypted when saved:

**Encrypted fields include:**
- LDAP bind password
- Keycloak client secret
- API keys for external services
- PKI certificate paths containing sensitive data

**Non-encrypted fields:**
- Server URLs and hostnames
- LDAP search filters
- Certificate paths (non-sensitive)
- Policy numbers and limits

Encryption provides security while allowing administrators to manage configuration without needing access to the underlying `.env` file.

## Authentication Configuration

### Accessing Settings

1. Log in as super admin
2. Click Settings (gear icon)
3. Select "Authentication" tab

### Configuration Workflow

The authentication configuration UI provides a tabbed interface for each method:

1. **Select Method Tab** (Local, LDAP, Keycloak, PKI, etc.)
2. **Click Edit Button** to enable modification mode
3. **Fill in Configuration Fields**
   - Required fields are marked with asterisk (*)
   - Help text explains each field's purpose
4. **Test Connection** (for remote methods like LDAP/Keycloak)
   - Click "Test Connection" button
   - Verify successful connection before saving
5. **Save Configuration**
   - Click "Save Configuration"
   - Changes take effect immediately
6. **Configuration confirmation** appears with timestamp and admin name

## Step-by-Step Authentication Configuration

### Local Authentication Setup

Local authentication uses username/password stored in the database with bcrypt hashing.

**Configuration Steps:**

1. Navigate to Settings → Authentication → Local Auth tab
2. Click "Edit" button
3. Configure these settings:

| Setting | Description | Default | Range |
|---------|-------------|---------|-------|
| **Enabled** | Turn local authentication on/off | true | true/false |
| **Enforce Password Complexity** | Require uppercase, lowercase, numbers, symbols | true | true/false |
| **Minimum Password Length** | Minimum characters required | 12 | 8-128 |
| **Password Expiration Days** | Days until password expires (0 = never) | 0 | 0-365 |
| **Password History Count** | Number of previous passwords to prevent reuse | 5 | 0-24 |
| **Require MFA for Admins** | Mandate MFA for admin/super_admin accounts | true | true/false |

**Password Complexity Requirements** (when enforced):
- Minimum 1 uppercase letter (A-Z)
- Minimum 1 lowercase letter (a-z)
- Minimum 1 digit (0-9)
- Minimum 1 special character (!@#$%^&*)
- Cannot contain username
- Cannot contain commonly guessed patterns (123456, password, admin, etc.)

**After Configuration:**
1. Click "Save Configuration"
2. Changes apply immediately to new login attempts
3. Existing sessions are not affected

### LDAP/Active Directory Configuration

Connect OpenTranscribe to your organization's LDAP or Active Directory server.

**Configuration Steps:**

1. Navigate to Settings → Authentication → LDAP/AD tab
2. Click "Edit" button
3. Fill in LDAP Connection Settings:

| Setting | Description | Required | Example |
|---------|-------------|----------|---------|
| **LDAP Server URL** | Server address and protocol | Yes | `ldap://ldap.example.com:389` |
| **Use TLS/SSL** | Secure connection | Yes | true (recommended) or false |
| **Bind Distinguished Name** | Admin account DN for querying | Yes | `cn=admin,dc=example,dc=com` |
| **Bind Password** | Password for bind account | Yes | (encrypted in database) |
| **Search Base DN** | Base location for user searches | Yes | `ou=users,dc=example,dc=com` |
| **User Object Class** | LDAP object class for users | No | `person` or `inetOrgPerson` |
| **User ID Attribute** | Attribute containing username | Yes | `uid` or `sAMAccountName` |
| **User Email Attribute** | Attribute containing email | Yes | `mail` or `userPrincipalName` |
| **User Display Name Attribute** | Attribute for full name | No | `displayName` or `cn` |
| **Admin Group DN** | Group DN for admin users (optional) | No | `cn=admins,ou=groups,dc=example,dc=com` |
| **Super Admin Group DN** | Group DN for super admin users (optional) | No | `cn=super-admins,ou=groups,dc=example,dc=com` |
| **Member Attribute** | Attribute for group membership | No | `member` or `memberUid` |

**Testing LDAP Connection:**

1. Fill in all required fields
2. Click "Test Connection" button
3. System will:
   - Attempt to bind with provided credentials
   - Search for a test user (if provided)
   - Display success/failure message
4. If successful, proceed to save
5. If failed, check error message for common issues:
   - "Connection refused" → Verify server URL and port
   - "Invalid credentials" → Verify bind DN and password
   - "Search failed" → Verify search base DN and user attributes

**User Auto-Provisioning:**

When enabled, LDAP users are automatically created in OpenTranscribe on first login:
- Email, name, and preferred role are populated from LDAP attributes
- User role defaults to `user` (can be set via admin group DN)
- User can immediately access transcription features

**After Configuration:**
1. Click "Save Configuration"
2. LDAP login option appears on login screen
3. Users can log in with their LDAP credentials
4. First login automatically provisions account

**Common LDAP Schemas:**

**OpenLDAP:**
```
Base DN: o=example,c=com
User Class: inetOrgPerson
ID Attribute: uid
Email: mail
Display Name: displayName
Admin Group: cn=admins,ou=groups,o=example,c=com
```

**Active Directory:**
```
Base DN: ou=users,dc=example,dc=com
User Class: person
ID Attribute: sAMAccountName
Email: userPrincipalName or mail
Display Name: displayName
Admin Group: cn=Admins,ou=Groups,dc=example,dc=com
```

### Keycloak/OIDC Setup

Integrate with Keycloak or any OpenID Connect (OIDC) provider.

**Prerequisites:**
- Running Keycloak instance (or OIDC-compatible provider)
- Created realm or application
- Client credentials with appropriate grants

**Keycloak Client Configuration** (if using Keycloak):

In Keycloak Admin Console:
1. Navigate to Clients
2. Create new client: `opentranscribe` (or your chosen name)
3. Set Access Type: `confidential`
4. Enable `Standard Flow Enabled` and `Direct Access Grants Enabled`
5. Set Valid Redirect URIs: `https://your-domain.com/login` — the **frontend login page**, not a
   backend `/api/...` route. The provider navigates the browser here, and `/api/auth/oidc/callback`
   returns JSON rather than a page, so registering it strands users on a JSON document after a
   successful sign-in. See [OIDC_SETUP.md](OIDC_SETUP.md) for the full list of origins to register.
6. Set Web Origins: `https://your-domain.com`
7. Go to Credentials tab
8. Copy the `Client Secret` (you'll need this)

**OpenTranscribe Configuration Steps:**

1. Navigate to Settings → Authentication → Keycloak/OIDC tab
2. Click "Edit" button
3. Fill in OIDC Configuration:

| Setting | Description | Required | Example |
|---------|-------------|----------|---------|
| **OIDC Provider URL** | Base URL of your OIDC provider | Yes | `https://keycloak.example.com/realms/master` |
| **Client ID** | Application client ID | Yes | `opentranscribe` |
| **Client Secret** | Client secret (keep secure) | Yes | (encrypted) |
| **Authorization Endpoint** | OAuth2 authorization URL | Auto | `{oidc_url}/protocol/openid-connect/auth` |
| **Token Endpoint** | OAuth2 token URL | Auto | `{oidc_url}/protocol/openid-connect/token` |
| **Userinfo Endpoint** | OIDC user info URL | Auto | `{oidc_url}/protocol/openid-connect/userinfo` |
| **JWKS URI** | JSON Web Key Set endpoint | Auto | `{oidc_url}/protocol/openid-connect/certs` |
| **Scopes** | OAuth2 scopes (space-separated) | Yes | `openid profile email` |
| **Use PKCE** | Enable PKCE for enhanced security | No | true (recommended) |
| **Email Claim** | JWT claim containing user email | No | `email` |
| **Name Claim** | JWT claim containing user name | No | `name` |
| **Admin Group Claim** | JWT claim for admin group membership | No | `groups` |
| **Admin Group Value** | Value indicating admin role | No | `opentranscribe-admins` |

**Testing OIDC Connection:**

1. Fill in Provider URL, Client ID, and Client Secret
2. Click "Test Connection"
3. System will:
   - Query the provider's metadata endpoint
   - Verify JWKS accessibility
   - Display provider configuration details
4. If successful, proceed to save
5. If failed, check error message:
   - "Invalid metadata URL" → Verify provider URL format
   - "Client authentication failed" → Verify Client ID and Secret
   - "JWKS unreachable" → Verify network connectivity

**Default Scopes:**
- `openid` - Required for OIDC
- `profile` - Provides name, picture, profile information
- `email` - Provides email and email_verified

**Additional Scopes** (if your provider supports):
- `groups` - User group memberships (for admin role mapping)
- `roles` - User roles from provider
- `offline_access` - Refresh token support

**JWT Claims Mapping:**

The system expects these standard claims (customizable):
```json
{
  "sub": "user-id",
  "email": "user@example.com",
  "name": "User Name",
  "groups": ["opentranscribe-admins"],
  "iat": 1234567890,
  "exp": 1234571490
}
```

**After Configuration:**
1. Click "Save Configuration"
2. Keycloak login option appears on login screen
3. Users click "Login with Keycloak"
4. Browser redirects to your Keycloak provider
5. After successful authentication, user is provisioned in OpenTranscribe

### PKI/Certificate Authentication

Authenticate users via X.509 certificates (PKI) for high-security environments.

**Prerequisites:**
- PKI infrastructure (CA, intermediate certificates)
- Client certificates (.p12 or .pem format)
- NGINX configured for mTLS (mutual TLS)
- OCSP responder or CRL endpoint (for revocation checking)

**Certificate Preparation:**

Create or obtain:
1. **Root CA Certificate** - Your certificate authority's root certificate
2. **Intermediate Certificates** - Any intermediate CAs in the chain
3. **CRL or OCSP Endpoint** - For certificate revocation checking

**OpenTranscribe PKI Configuration:**

1. Navigate to Settings → Authentication → PKI/Certificate tab
2. Click "Edit" button
3. Configure certificate settings:

| Setting | Description | Required | Notes |
|---------|-------------|----------|-------|
| **PKI Enabled** | Enable PKI authentication | Yes | Requires NGINX mTLS |
| **CA Certificate Path** | Path to CA certificate(s) | Yes | Can be PEM bundle |
| **Enable Certificate Validation** | Validate client certificates | Yes | true (recommended) |
| **Require Certificate CN** | Enforce specific CN format | No | Regex pattern |
| **Certificate CN Format** | Regular expression for CN | No | e.g., `^CN=[^,]+,OU=Users` |
| **Extract Email from SAN** | Extract email from certificate | Yes | true |
| **Email SAN Type** | Subject Alternative Name type | Yes | `rfc822Name` or `email` |
| **Enable OCSP** | Check certificate revocation (OCSP) | No | Requires OCSP responder |
| **OCSP Responder URL** | OCSP endpoint URL | No | e.g., `http://ocsp.example.com` |
| **Enable CRL** | Check certificate revocation (CRL) | No | Requires CRL distribution point |
| **CRL Endpoint URL** | CRL distribution point URL | No | e.g., `http://pki.example.com/crl` |
| **CRL Refresh Hours** | How often to refresh CRL | No | 24 (hours) |
| **Admin Certificate DNs** | DNs authorized as admins | No | Pipe-separated DN list |
| **Super Admin Certificate DNs** | DNs authorized as super_admin | No | Pipe-separated DN list |

**Certificate DN Format:**

Distinguished Names (DNs) are specified in RFC 2253 format:
```
CN=John Doe,OU=Users,O=Example Corp,C=US
```

For multiple DNs (admin/super admin), use pipe separator:
```
CN=Admin User,OU=Admins,O=Example,C=US|CN=Super Admin,OU=Super-Admins,O=Example,C=US
```

**OCSP Configuration:**

OCSP (Online Certificate Status Protocol) checks if certificates are revoked in real-time:

1. Obtain OCSP responder URL from your CA
2. Enable OCSP in configuration
3. Enter OCSP responder URL
4. System will check certificate status on each login
5. Revoked certificates are denied access immediately

**CRL Configuration:**

CRL (Certificate Revocation List) is a periodic list of revoked certificates:

1. Obtain CRL endpoint from your CA
2. Enable CRL in configuration
3. Enter CRL endpoint URL
4. Configure refresh interval (default: 24 hours)
5. System caches CRL and refreshes periodically
6. Certificates in CRL are denied access

**NGINX mTLS Configuration:**

PKI authentication requires NGINX to be configured for mutual TLS. This is automatically handled when using:
```bash
./opentr.sh start prod --with-pki
```

This loads `docker-compose.pki.yml` which configures NGINX with client certificate verification.

**After Configuration:**
1. Click "Save Configuration"
2. NGINX is configured (if PKI deployment used)
3. Users access via HTTPS with valid client certificate
4. Certificate is automatically validated and email extracted
5. User is provisioned in OpenTranscribe with certificate email

**Certificate Testing:**

Test PKI authentication with a client certificate:
```bash
curl --cert client-cert.pem --key client-key.pem https://your-domain.com/api/auth/pki-status
```

### MFA (Multi-Factor Authentication) Configuration

Enable TOTP-based multi-factor authentication for additional security.

**Configuration Steps:**

1. Navigate to Settings → Authentication → MFA tab
2. Click "Edit" button
3. Configure MFA settings:

| Setting | Description | Required | Default |
|---------|-------------|----------|---------|
| **Enable MFA** | Turn MFA on/off globally | Yes | true |
| **Require MFA for Admins** | Mandate MFA for admins | Yes | true |
| **Require MFA for All Users** | Mandate MFA for everyone | No | false |
| **MFA Issuer Name** | Display name in authenticator apps | No | `OpenTranscribe` |
| **TOTP Time Window** | Seconds valid per code | No | 30 |
| **Recovery Codes Per Setup** | Number of recovery codes | No | 10 |
| **Max Recovery Code Uses** | Uses allowed per code (0=unlimited) | No | 0 |

**TOTP Details:**

TOTP (Time-Based One-Time Password) is the standard for authenticator apps:
- Algorithm: HMAC-SHA1 with RFC 4226/6238 compliance
- Time Window: 30 seconds (default)
- Code Length: 6 digits
- Compatible with: Google Authenticator, Microsoft Authenticator, Authy, FreeOTP, 1Password, etc.

**Recovery Codes:**

Recovery codes provide backup access if authenticator is lost:
- Generated during MFA setup
- Each code is single-use
- Should be stored securely by user
- Admin can reset MFA and re-generate codes

**Enforcing MFA:**

**For Admins Only:**
- Enable "Require MFA for Admins"
- All admin/super_admin accounts must set up MFA
- Regular users are prompted but not required

**For All Users:**
- Enable "Require MFA for All Users"
- All accounts must set up MFA on next login
- User sees MFA setup screen before accessing app
- Can skip setup temporarily with "Setup Later" button (within 24 hours)

**User MFA Setup Flow:**
1. User logs in (with password)
2. If MFA required, user sees "Setup MFA" screen
3. User scans QR code with authenticator app
4. User enters 6-digit code from app
5. User saves recovery codes in secure location
6. MFA is now active

**Per-User MFA Control:**

Admins can reset or disable MFA per user:
1. Settings → Users
2. Find user in list
3. Click "Reset MFA" button
4. Confirm action
5. User's MFA is cleared
6. User must re-setup MFA on next login (if required)

**After Configuration:**
1. Click "Save Configuration"
2. Changes apply to next login attempt
3. If MFA newly required, users see setup screen on login

### Password Policy Configuration

Set password requirements for local authentication.

**Configuration Steps:**

1. Navigate to Settings → Authentication → Password Policy tab
2. Click "Edit" button
3. Configure password rules:

| Setting | Description | Required | Default | Range |
|---------|-------------|----------|---------|-------|
| **Minimum Length** | Minimum characters | Yes | 12 | 8-128 |
| **Maximum Length** | Maximum characters | No | 128 | 12-256 |
| **Require Uppercase** | Require A-Z | Yes | true | true/false |
| **Require Lowercase** | Require a-z | Yes | true | true/false |
| **Require Numbers** | Require 0-9 | Yes | true | true/false |
| **Require Special Chars** | Require !@#$%^&* etc. | Yes | true | true/false |
| **Special Characters** | Define allowed special chars | No | `!@#$%^&*()_+-=[]{}` | custom |
| **Expiration Days** | Days until password expires | No | 0 (never) | 0-365 |
| **History Count** | Password history size | Yes | 5 | 0-24 |
| **Min Days Between Changes** | Minimum days to change again | No | 0 | 0-30 |
| **Check Common Patterns** | Detect weak patterns | Yes | true | true/false |

**Password Strength Meter:**

When users create/change passwords, they see real-time feedback:
- Password length indicator
- Character type indicators
- Forbidden pattern warnings
- Overall strength score (Weak/Fair/Good/Strong)

**Forbidden Patterns:**

When "Check Common Patterns" is enabled, system rejects:
- Sequential numbers: `123456`, `987654`
- Sequential letters: `abcdef`, `qwerty`
- Username in password
- Email address in password
- Dictionary words (if dictionary available)
- Repeated characters: `aaaaaa`, `111111`

**Password History:**

Users cannot reuse recent passwords:
- History count defines how many previous passwords are tracked
- If history is 5, user cannot reuse last 5 passwords
- Set history to 0 to allow any reuse

**Password Expiration:**

When enabled, users must change password periodically:
- Expiration days defines interval (e.g., 90 days)
- User receives warning email 14 days before expiration
- On login, user sees "Change Password" prompt if expired
- User cannot proceed until password is changed

**After Configuration:**
1. Click "Save Configuration"
2. New requirements apply to next password change
3. Existing passwords are not retroactively checked
4. Users see new requirements on password change screen

### Session Management Configuration

Control how user sessions and tokens work.

**Configuration Steps:**

1. Navigate to Settings → Authentication → Sessions tab
2. Click "Edit" button
3. Configure session settings:

| Setting | Description | Required | Default |
|---------|-------------|----------|---------|
| **Access Token Expiration (minutes)** | How long access tokens are valid | Yes | 60 |
| **Refresh Token Expiration (days)** | How long refresh tokens are valid | Yes | 30 |
| **Idle Timeout (minutes)** | Logout if inactive | No | 0 (disabled) |
| **Absolute Timeout (hours)** | Max session duration | No | 0 (unlimited) |
| **Max Concurrent Sessions Per User** | Sessions allowed simultaneously | No | 0 (unlimited) |
| **Require Device ID** | Track unique devices | No | true |
| **Rotate Refresh Tokens** | Issue new token on use | No | true |

**Access Tokens:**

Short-lived tokens sent with API requests:
- Contains user identity and permissions
- Expires after configured minutes
- Invalid after expiration (user must refresh)
- Typical range: 15-120 minutes

**Refresh Tokens:**

Long-lived tokens used to obtain new access tokens:
- Stored securely on client (httpOnly cookie)
- Expires after configured days
- Can be revoked by admin
- Used to keep session alive without re-login

**Idle Timeout:**

Automatic logout after inactivity:
- If 0, idle timeout is disabled
- If set (e.g., 30 minutes), user auto-logged out if inactive
- Activity includes: API calls, page navigation, user input
- Warning shown before logout (usually 5 minutes before)

**Absolute Timeout:**

Maximum session duration regardless of activity:
- If 0, sessions can be indefinitely long
- If set (e.g., 8 hours), user must re-login after duration
- Applied in addition to idle timeout
- Useful for high-security environments

**Concurrent Sessions:**

Limit simultaneous sessions per user:
- If 0, unlimited concurrent sessions allowed
- If set (e.g., 3), user can only have 3 simultaneous logins
- Additional login from new device logs out oldest session
- Prevents account sharing across multiple users

**Refresh Token Rotation:**

When enabled, each refresh token use issues new token:
- Provides security against stolen tokens
- Tokens become useless if used twice
- May cause issues with concurrent API clients
- Recommended: enabled

**After Configuration:**
1. Click "Save Configuration"
2. New token expiration applies to tokens issued after save
3. Existing active tokens are not retroactively invalidated
4. Session limits apply to new logins

### Audit Logging Configuration

Configure what authentication events are logged and where.

**Configuration Steps:**

1. Navigate to Settings → Authentication → Audit Log tab
2. Click "View Configuration" button
3. Audit logging settings:

| Setting | Description | Notes |
|---------|-------------|-------|
| **Log All Auth Events** | Capture every login attempt | Includes failures, MFA, etc. |
| **Log Configuration Changes** | Track admin config updates | Always includes: who, what, when |
| **Retention Days** | How long to keep logs | 0 = permanent |
| **Encryption** | Encrypt sensitive data in logs | Masking of passwords, tokens |
| **Export Formats** | CSV, JSON, Syslog | Choose what's supported |

**Logged Events:**

| Event | Captures |
|-------|----------|
| Authentication attempts | Username, method, success/failure, IP, timestamp |
| MFA setup/changes | User email, MFA method, admin if reset |
| Password changes | User email, method, admin if forced reset |
| Account lock/unlock | User email, reason, admin action |
| Configuration changes | Admin user, setting changed, old/new values (masked) |
| Session creation | User email, device, IP, timestamp |
| Session termination | User email, reason (logout/timeout/admin) |

**Sensitive Data Masking:**

Audit logs automatically mask:
- LDAP bind passwords (shown as `***`)
- API keys and secrets (shown as `***`)
- OAuth tokens (shown as first 8 chars + `***`)
- User passwords (never logged)

## Configuration Precedence and Priority

### Configuration Hierarchy

OpenTranscribe follows this precedence order for authentication configuration:

1. **Database Configuration** (highest priority)
   - Settings stored in `auth_config` table
   - Set via Settings UI by super admin
   - Takes effect immediately
   - Survives container restarts

2. **Environment Variables** (fallback)
   - Settings in `.env` file
   - Set on deployment/startup
   - Used only if database has no value
   - Requires container restart to change

3. **Default Values** (lowest priority)
   - Built-in reasonable defaults
   - Used if neither database nor ENV is set

### Example Priority Resolution

If LDAP configuration is partially defined:

**Database has:**
```
LDAP_SERVER = "ldap://company.com:389"
LDAP_BASE = "ou=users,dc=company,dc=com"
```

**Environment (`.env`) has:**
```
LDAP_SERVER = "ldap.example.com:389"
LDAP_BASE = "ou=people,dc=example,dc=com"
LDAP_BIND_DN = "cn=admin,dc=example,dc=com"
```

**Result:**
- `LDAP_SERVER` = "ldap://company.com:389" (from database)
- `LDAP_BASE` = "ou=users,dc=company,dc=com" (from database)
- `LDAP_BIND_DN` = "cn=admin,dc=example,dc=com" (from environment, no database value)

### When Changes Take Effect

**Database Changes (Settings UI):**
- Apply immediately to new requests
- No service restart required
- Existing connections/sessions not affected
- Recommended for all configuration

**Environment Variable Changes (.env):**
- Require container restart to take effect
- Restart command:
  ```bash
  docker compose restart backend
  ```
- All active connections affected
- Not recommended for runtime changes — use the Admin UI instead

## Multi-Method Hybrid Authentication Examples

OpenTranscribe supports enabling multiple authentication methods simultaneously, allowing users to choose their preferred login method.

### Example 1: Local + LDAP (Basic Hybrid)

**Use Case:** Organization transitioning from local accounts to LDAP
- Existing local accounts still work
- New employees use LDAP
- Gradual migration without disrupting access

**Configuration:**

**Local Auth:**
```
Enabled: true
Minimum Password Length: 12
Enforce Complexity: true
Require MFA for Admins: true
```

**LDAP/AD:**
```
Enabled: true
Server URL: ldap://company-dc.example.com:389
Base DN: ou=users,dc=company,dc=com
User ID Attribute: sAMAccountName
Email Attribute: userPrincipalName
Admin Group: cn=IT-Admins,ou=groups,dc=company,dc=com
```

**Login Screen:**

```
Welcome to OpenTranscribe
─────────────────────────

[Email Address: ____________]
[Password:      ____________]

[X] Remember me

  [Login]

─ OR ─

[Login with LDAP/Active Directory]
```

User can:
- Enter local account email/password → logs in as local user
- Click "Login with LDAP" → redirected to LDAP authentication

**User Experience:**
- LDAP users have transparent login (single sign-on from domain)
- Local users enter credentials each time
- Both types can coexist in same system

### Example 2: LDAP + Keycloak (Advanced Hybrid)

**Use Case:** Enterprise with Keycloak as identity broker
- LDAP users authenticate via Keycloak
- External OAuth providers (Google, GitHub) via Keycloak
- Centralized user management

**Configuration:**

**LDAP/AD:**
```
Enabled: true
Server URL: ldap://internal-dc.company.com:389
Base DN: ou=users,dc=company,dc=com
User ID Attribute: sAMAccountName
Email Attribute: mail
```

**Keycloak/OIDC:**
```
Enabled: true
Provider URL: https://keycloak.company.com/realms/company
Client ID: opentranscribe
Client Secret: (encrypted)
Scopes: openid profile email groups
Email Claim: email
Admin Group Claim: groups
Admin Group Value: company-admins
```

**Login Screen:**

```
Welcome to OpenTranscribe
─────────────────────────

[Email Address: ____________]
[Password:      ____________]

  [Login Locally]

─ OR ─

[Login with Keycloak]

─ OR ─

[Login with LDAP]
```

**Architecture:**
```
User Login
├── Direct LDAP → LDAP server
├── Direct Keycloak → Keycloak → (LDAP backend OR OAuth)
└── Local → Database (bcrypt)
```

**Admin Setup in Keycloak:**
- Create group: `company-admins`
- Add LDAP users to group
- Map group claim in OpenTranscribe config
- Users in group auto-promoted to admin

### Example 3: All Four Methods Enabled

**Use Case:** Large enterprise with heterogeneous authentication needs
- Local accounts for service accounts, legacy systems
- LDAP for internal employees
- Keycloak for contractors, partners
- PKI for high-security teams

**Configuration:**

```
Authentication Methods Enabled:
├── Local (for service accounts)
├── LDAP/AD (internal employees)
├── Keycloak/OIDC (partners, contractors)
└── PKI/Certificates (secure team)
```

**Login Options:**

```
OpenTranscribe Login
────────────────────

[Email: ___________]
[Password: ________]

[Local Login]

─────────────────────

[Login with LDAP/Active Directory]
[Login with Keycloak]
[Use Certificate (PKI)]
```

**User Routing:**

| User Type | Method | Example |
|-----------|--------|---------|
| Internal Employee | LDAP | jsmith@company.com (AD login) |
| Contractor | Keycloak | contractor@partner.com (OAuth) |
| Service Account | Local | transcribe-bot@local |
| Security Team | PKI | Certificate with CN=secure-user |

**MFA Policy:**
```
├── Local: Require MFA for all users
├── LDAP: Defer to AD MFA if available
├── Keycloak: Keycloak handles MFA
└── PKI: Certificate is 2FA equivalent
```

**Admin Assignment:**
```
Sources of Admin Users:
├── Local: Manual admin assignment via Settings UI
├── LDAP: Members of "admins" group auto-provisioned as admin
├── Keycloak: "admin" claim triggers admin role
└── PKI: Admin DNs configuration
```

**Session Behavior:**
- Each login method creates separate session
- Same email across methods = same user account
- Switching methods keeps sessions separate
- Admin can terminate all sessions for user

### Example 4: PKI-Only High-Security Setup

**Use Case:** Classified environment, no password authentication
- All users authenticate via X.509 client certificates
- OCSP revocation checking for immediate access control
- No passwords stored in system

**Configuration:**

**PKI/Certificate (Only Auth Method):**
```
Enabled: true
CA Certificate Path: /etc/ssl/certs/company-ca.pem
Enable Certificate Validation: true
Extract Email from SAN: true
Email SAN Type: rfc822Name
Enable OCSP: true
OCSP Responder URL: http://ocsp.pki.company.com
Admin Certificate DNs: CN=admin-user,OU=PKI,O=Company,C=US
Super Admin Certificate DNs: CN=super-admin,OU=PKI,O=Company,C=US
```

**All Other Methods:** Disabled

**Access Flow:**
1. User connects to HTTPS
2. NGINX requests client certificate
3. NGINX validates against CA
4. NGINX checks OCSP for revocation status
5. If valid, proxies to backend with certificate info
6. Backend creates session for certificate email
7. User granted access

**Revocation Workflow:**
- When employee leaves, certificate added to OCSP revocation list
- Next login: OCSP check fails
- Access denied immediately (within seconds)
- No need to reset passwords or database updates

## Security Best Practices for Admin Access

### Protecting Super Admin Accounts

**Account Hardening:**

1. **Strong Credentials**
   - Use 20+ character passwords
   - Avoid predictable patterns
   - Don't share credentials
   - Use unique email for admin account

2. **Mandatory MFA**
   - Enable "Require MFA for Admins" in settings
   - Super admin must set up TOTP
   - Keep recovery codes in secure location
   - Don't share recovery codes

3. **Limit Super Admin Count**
   - Only essential personnel
   - Typical organizations: 2-3 super admins
   - Each super admin should be well-known individual
   - Document purpose of each super admin account

4. **Account Monitoring**
   - Check Settings > Users for admin list
   - Verify all admins are known personnel
   - Alert if unknown admin accounts created
   - Review user creation audit logs regularly

**Safe Credential Management:**

```bash
# Good: Use password manager
- Super admin credentials stored in organization's password manager
- Multiple admins can access in emergency
- Credentials rotated quarterly

# Bad: Shared credentials
- Super admin account shared across team
- Cannot audit who made changes
- Terminating employee loses access mechanism
- Impossible to identify unauthorized changes
```

### Audit Log Monitoring

**Regular Review Schedule:**

```
Daily:
├── Check for failed login attempts
├── Monitor for unusual IP addresses
└── Alert on 5+ failed logins in 1 hour

Weekly:
├── Review all configuration changes
├── Check for new admin promotions
├── Export and verify auth events

Monthly:
├── Full audit log analysis
├── User access report
├── Terminated user follow-up (ensure sessions revoked)
└── Password policy compliance check
```

**Critical Events to Monitor:**

| Event | Action | Threshold |
|-------|--------|-----------|
| Failed Login Attempts | Review | 5+ in 1 hour from single IP |
| Admin Promotion | Verify | Every promotion - confirm authorized |
| Configuration Changes | Document | Every change by whom and why |
| Account Lockout | Investigate | Multiple lockouts = attack |
| MFA Reset | Approve | Only for known requests |
| Session Termination | Check | If by admin, verify reason |

**Exporting Audit Logs:**

1. Settings → Audit Logs
2. Set date range (e.g., last 30 days)
3. Filter by event type if needed
4. Click "Export CSV" or "Export JSON"
5. Download file for external storage/analysis
6. Archive for compliance (keep 7+ years)

**Suspicious Patterns:**

Watch for:
- Same user multiple failed attempts → brute force
- Unusual IP addresses → compromised account
- Multiple users from same IP → shared account
- High volume of MFA resets → account takeover
- Configuration changes by unfamiliar admin → unauthorized access
- Sessions from suspicious locations → credential compromise

### Regular Configuration Review

**Monthly Configuration Audit:**

```
Checklist:
□ Review LDAP/Keycloak server URLs are correct
□ Verify admin group DNs haven't changed
□ Check password policy is still appropriate
□ Confirm MFA requirements still match policy
□ Review session timeout values
□ Verify only authorized methods enabled
□ Check that encryption is enabled for secrets
```

**Configuration Version Control:**

Treat configuration like code:
1. Document each change in audit comments
2. Keep backup of previous configuration
3. Test changes in staging before production
4. Use version numbers (v1.0, v1.1, etc.)
5. Maintain changelog of auth configuration

**Example Change Documentation:**

```
Configuration Change Log
========================

2024-02-07 v1.2 - Added PKI Support
- By: admin@company.com
- Changed: PKI enabled, OCSP revocation checking enabled
- Reason: Deploying high-security access for classified team
- Tested: Yes (2024-02-06)
- Impact: Adds second factor authentication

2024-01-15 v1.1 - Tightened Password Policy
- By: admin@company.com
- Changed: Min length 12→16, history 3→5, expiration 90→60 days
- Reason: Security audit findings
- Impact: Existing passwords still valid, new policy on change
```

### Incident Response

**Compromised Admin Account:**

If you suspect super admin credentials are compromised:

1. **Immediate Actions (within 1 minute):**
   ```bash
   # Terminate all sessions for compromised admin
   Settings → Users → [Find admin]
   Click "Terminate All Sessions"
   Confirm
   ```

2. **Within 1 hour:**
   - Force password reset: Settings → Users → [Find admin] → Reset Password
   - Reset MFA: Settings → Users → [Find admin] → Reset MFA
   - Review recent configuration changes: Settings → Audit Logs
   - Check for unauthorized users created: Settings → Users, filter by creation date

3. **Within 24 hours:**
   - Export full audit log for incident investigation
   - Review all admin actions in past 7 days
   - Check if unauthorized API keys created
   - Verify no unauthorized role promotions
   - Review all active sessions for anomalies

4. **Ongoing:**
   - Monitor for re-compromise indicators
   - Force additional admin password rotation
   - Temporarily require re-authentication for settings changes
   - Consider temporarily disabling the account

**Unauthorized Access Detected:**

If you detect unauthorized configuration changes:

1. **Identify change:**
   - Settings → Audit Logs
   - Find suspicious "ADMIN_SETTINGS_CHANGE" entry
   - Note: who made change, when, what changed, from what IP

2. **Assess impact:**
   - What was changed? (auth method, admin list, policy, etc.)
   - Could this expose user data? (check if export happened)
   - Could this create backdoor access? (check if admin users added)

3. **Remediate:**
   - Revert configuration to known-good state
   - Check if suspicious admin accounts exist: Settings → Users
   - Delete unauthorized accounts
   - Force password resets for all admins
   - Enable MFA if not already enabled
   - Reset sessions for all affected users

4. **Prevent recurrence:**
   - Review access logs to find compromise vector
   - Update network security (firewall rules)
   - Consider audit-only mode: require approval for config changes
   - Increase monitoring frequency
   - Review file permissions on configuration backup

### Password Policy Compliance

**Monitoring User Compliance:**

Settings → Audit Logs filter shows:
- Users with expired passwords
- Users who reuse passwords
- Accounts with weak passwords
- Users failing password complexity requirements

**Enforcement Strategy:**

| Environment | Policy | Duration |
|-------------|--------|----------|
| Dev | Relaxed (8 char, no expiration) | N/A |
| Staging | Moderate (12 char, 120 days) | Full enforcement |
| Production | Strict (16 char, 60 days, MFA) | Gradual rollout |

**Rolling Out New Policy:**

1. Configure new policy in Settings
2. Announce to users (email, login banner)
3. Grace period: 7-14 days before enforcement
4. Monitor compliance via audit logs
5. After grace period, enforce on next login
6. Follow up with non-compliant users

### Regular Access Reviews

**Quarterly Access Certification:**

Every 3 months:
1. Export user list: Settings → Users → "Export Users CSV"
2. Verify with department managers:
   - Does user still work here?
   - Should user have admin access?
   - Are users in correct roles?
3. Remove access for terminated employees
4. Demote users no longer needing admin privileges
5. Document review completion

**Terminated Employee Checklist:**

When employee leaves organization:
```
□ Disable account: Settings → Users → Lock Account
□ Terminate all sessions: Terminate All Sessions
□ Reset MFA: Reset MFA
□ Remove from admin group (if LDAP): Remove LDAP group membership
□ Revoke refresh tokens: Settings → Sessions → Revoke User Tokens
□ Export user data if needed (compliance)
□ Review audit logs for their actions
□ Archive their configuration if applicable
□ Update organization documentation
```

---

## v0.4.0 Admin Features

### File Retention

Configure automatic deletion of old transcriptions to manage storage:

1. Navigate to Settings → Admin → File Retention
2. Set retention policies (e.g., delete files older than 90 days)
3. Policies apply per-user or globally
4. Users receive warnings before their files are deleted

### ASR Model Management

The super admin pins a single ASR model that all workers share (GPU VRAM is loaded once at worker startup):

1. Navigate to Settings → ASR → Local Model
2. Select the desired model (e.g., `large-v3-turbo`, `large-v3`)
3. Click "Set Model" — the setting is saved immediately
4. Click "Restart GPU Worker" to perform a graceful drain restart
   - The worker finishes all current tasks before stopping
   - Docker automatically restarts the container with the new model loaded

The model selection is stored in the database (`asr.local_model` system setting) and takes precedence over the `WHISPER_MODEL` environment variable.

### Organization Context for LLM Summaries

Inject organization-specific context into every AI summary prompt:

1. Navigate to Settings → AI → Organization Context
2. Enter context such as your organization name, domain terminology, common speakers, or acronyms
3. This context is prepended to LLM summary requests for all users

You can also set a per-collection default AI prompt under Settings → Collections → select collection → Default AI Prompt.

### User Groups Management

Manage logical groupings of users for collection sharing and access control:

1. Navigate to Settings → Groups
2. Create groups and assign members
3. Groups can be granted access to shared collections
4. LDAP group membership is synced on each LDAP login (separate from manually managed groups)

### Collection Sharing Admin Controls

Admins can view and modify sharing permissions on any collection:

1. Navigate to Settings → Collections
2. Filter by owner or sharing status
3. Remove unwanted shares or transfer ownership when an employee leaves

### Audit Log Viewer

The audit log viewer supports structured export for SIEM integration:

1. Navigate to Settings → Audit Logs
2. Filter by date range, event type, outcome, user, or source IP
3. Export as **JSON** (structured, includes all fields) or **CSV** (for spreadsheet analysis)
4. Logs are also written in **CEF (Common Event Format)** for syslog/SIEM ingestion
5. If `AUDIT_LOG_TO_OPENSEARCH=true`, logs are indexed in OpenSearch for full-text search

---

**Last Updated:** 2026-03-22
**Documentation Version:** 2.1 (v0.4.0)

## Account Management

### User Search

Navigate to Settings > Users to search and filter users by:
- Email or name
- Role (user, admin, super_admin)
- Authentication type (local, ldap, keycloak, pki)
- Account status (active, inactive)
- Last login date
- MFA status (enabled/disabled)

### Account Actions

| Action | Description | Required Role |
|--------|-------------|---------------|
| Reset Password | Set new password, optionally force change | super_admin |
| Lock Account | Disable account with reason | admin |
| Unlock Account | Re-enable locked account | admin |
| Terminate Sessions | Force logout from all devices | admin |
| Reset MFA | Remove TOTP configuration | super_admin |
| Change Role | Promote/demote user role | super_admin |

### Viewing User Sessions

1. Click user row in table
2. Select "View Sessions"
3. See all active sessions with:
   - Device/browser info
   - IP address
   - Session start time
   - Last activity
   - Access token expiration

### Terminating Sessions

To force logout a user:
1. Select user
2. Click "Terminate All Sessions"
3. Confirm action

User's refresh tokens are revoked and they must re-authenticate.

## Audit Log Management

### Viewing Logs

Super admins can access audit logs at Settings > Audit Logs:

- Filter by date range
- Filter by event type
- Filter by outcome (success/failure)
- Search by user
- Search by IP address
- Export to CSV or JSON

### Event Types

| Event Type | Description |
|------------|-------------|
| AUTH_LOGIN_SUCCESS | Successful login |
| AUTH_LOGIN_FAILURE | Failed login attempt |
| AUTH_LOGOUT | User logout |
| AUTH_MFA_SETUP | MFA enabled |
| AUTH_MFA_VERIFY | MFA verification |
| AUTH_PASSWORD_CHANGE | Password changed |
| AUTH_ACCOUNT_LOCKOUT | Account locked |
| AUTH_ACCOUNT_UNLOCK | Account unlocked |
| ADMIN_USER_CREATE | New user created |
| ADMIN_ROLE_CHANGE | User role changed |
| ADMIN_SETTINGS_CHANGE | Auth settings modified |
| AUTH_SESSION_REVOKE | Admin forced logout |
| AUTH_RATE_LIMIT | Login rate limit exceeded |

### Exporting Logs

Export audit logs for compliance:
1. Set date range filter
2. Apply event type filters if needed
3. Click "Export CSV" or "Export JSON"
4. Download file
5. Archive securely (retain 7+ years for compliance)

## Reports

### Account Status Dashboard

View summary statistics:
- Total users
- Active vs inactive
- MFA adoption rate
- Password expiration status
- Users by authentication method
- User role distribution

### Failed Login Report

Monitor security events:
- Failed attempts by user
- Failed attempts by IP
- Lockout events
- Suspicious patterns
- Account compromise indicators

## Best Practices

### Security
- Limit super_admin accounts to essential personnel
- Use strong, unique passwords
- Enable MFA for all admin accounts
- Review audit logs regularly
- Monitor failed login attempts
- Conduct quarterly access reviews
- Document all administrative changes
- Use strong certificate passwords if using PKI

### Configuration
- Test auth method connections before saving
- Document configuration changes
- Keep .env as backup for critical settings
- Use descriptive audit comments
- Version your configuration changes
- Maintain change log for compliance
- Review multi-method priority rules
- Test failover scenarios (what if LDAP is down?)

### Incident Response
- Know how to lock accounts quickly
- Have process for password reset requests
- Maintain audit log exports for compliance
- Document security incidents
- Establish incident response playbook
- Define escalation procedures
- Test incident response regularly
- Maintain contact list for security team

### Hybrid Authentication Maintenance
- Document which methods are enabled and why
- Maintain separate documentation for each method
- Test all methods regularly (monthly)
- Monitor each method's logs separately
- Plan for method retirement (transition users)
- Keep admin credentials synchronized across methods
- Test user scenarios (login with each method)
- Validate that role mapping works correctly
