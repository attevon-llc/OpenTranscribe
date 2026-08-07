# OpenID Connect (OIDC) Authentication Setup

OpenTranscribe authenticates against **any conforming OpenID Connect provider** —
Keycloak, Authentik, Okta, Microsoft Entra ID, Google Workspace and others. Endpoints
are read from the provider's `.well-known/openid-configuration` document whenever a
discovery URL is configured (issue #353); without one, the legacy
`<server>/realms/<realm>/protocol/openid-connect/...` layout is used, which only
realm-shaped providers such as Keycloak serve.

> **Naming.** Every setting is now called `oidc_*` (admin UI) / `OIDC_*` (environment).
> The historical `KEYCLOAK_*` environment-variable names **keep working permanently**
> and take precedence when both spellings are set, so no deployment has to edit its
> `.env` on upgrade. The backend logs one line at startup naming any retired variables
> it found. Database config keys were migrated automatically by revision `v377`, and
> the stored client secret is carried across as ciphertext without being decrypted.

> **Provider-specific traps** are collected at the end of this document. The two that
> produce the most support tickets: a provider whose signing key is an EC key issues
> **ES256** ID tokens, and Keycloak does **not** put realm roles in the ID token by
> default.

The worked example below uses Keycloak because it is the provider the test overlay
ships (`./opentr.sh start dev --with-keycloak-test`). The steps are the same
elsewhere; only the console navigation differs.

## Development Environment Setup

### Step 1: Start Keycloak

**Recommended Method (using opentr.sh):**
```bash
# Start OpenTranscribe with Keycloak test container
./opentr.sh start dev --with-keycloak-test

# Or for production mode (testing before push)
./opentr.sh start prod --build --with-keycloak-test
```

**Advanced Method (manual docker compose):**
```bash
# Start Keycloak dev server (from OpenTranscribe root directory)
docker compose -f docker-compose.yml -f docker-compose.keycloak.yml up -d keycloak

# Check status
docker compose -f docker-compose.yml -f docker-compose.keycloak.yml ps keycloak

# View logs
docker compose -f docker-compose.yml -f docker-compose.keycloak.yml logs -f keycloak
```

Access the admin console at http://localhost:8180

Default credentials: `admin` / `admin`

### Step 2: Create Realm

1. Log in to Keycloak admin console
2. Click the dropdown at the top-left (shows "master")
3. Click "Create Realm"
4. Realm name: `opentranscribe`
5. Click "Create"

### Step 3: Create Client

1. Navigate to **Clients** in the left sidebar
2. Click **Create client**
3. Configure the client:
   - **Client type**: OpenID Connect
   - **Client ID**: `opentranscribe-app`
   - Click **Next**
4. Capability config:
   - **Client authentication**: ON (confidential client)
   - **Authorization**: OFF
   - Click **Next**
5. Login settings:
   - **Valid redirect URIs**:
     - `http://localhost:5173/login` (local dev)
     - `http://your-server-ip/login` (LAN access - replace with your IP)
     - `https://yourdomain.com/login` (production)
   - **Valid post logout redirect URIs**: `+`
   - **Web origins**: `+` (allows all origins from redirect URIs)

   > **IMPORTANT**: Redirect URIs must point to the FRONTEND login page, not the backend API endpoint.
6. Click **Save**

### Step 4: Get Client Secret

1. Navigate to **Clients** → **opentranscribe-app**
2. Go to the **Credentials** tab
3. Copy the **Client secret** value

### Step 5: Create Roles

1. Navigate to **Realm roles** in the left sidebar
2. Click **Create role**
3. Create role with name: `user`
4. Click **Save**
5. Repeat to create role: `admin`

### Step 6: Create Test User

1. Navigate to **Users** in the left sidebar
2. Click **Add user**
3. Fill in the details:
   - **Username**: `testuser`
   - **Email**: `testuser@example.com`
   - **Email verified**: ON
   - **First name**: `Test`
   - **Last name**: `User`
4. Click **Create**
5. Go to the **Credentials** tab
6. Click **Set password**
   - Enter a password
   - **Temporary**: OFF
   - Click **Save**
7. Go to the **Role mapping** tab
8. Click **Assign role**
9. Select `user` role (or `admin` for admin access)
10. Click **Assign**

### Step 7: Configure OpenTranscribe

**Recommended Method: Via Admin UI** (stores config encrypted in database — takes precedence over .env)

1. Log in to OpenTranscribe as a super admin
2. Go to **Settings** → **Authentication** → **OIDC**
3. Enable **OIDC**
4. Configure the following settings:
   - **Server URL**: `http://localhost:8180` (must be accessible from user's browser)
   - **Internal URL**: `http://transcribe-app-keycloak-1:8080` (for backend-to-Keycloak communication)
   - **Realm**: `opentranscribe`
   - **Client ID**: `opentranscribe-app`
   - **Client Secret**: Paste the secret from Step 4
   - **Callback URL**: `http://localhost:5173/login` (FRONTEND login page, NOT backend API)
   - **Admin Role**: `admin`
5. Click **Save**

> **CRITICAL - For LAN Access**: If accessing OpenTranscribe from other devices on your network (e.g., from Mac on local network), use your server's IP address:
> - **Server URL**: `http://192.168.x.x:8180` (replace with your server IP)
> - **Callback URL**: `http://192.168.x.x/login` (match your server IP)
>
> The callback URL must be accessible from the user's browser and must point to the frontend login page.

**Alternative Method: Via .env file** (initial seed fallback — only used when no database config exists)

If the Admin UI is not yet accessible (e.g., first-time setup), environment variables can seed the initial configuration:

```bash
# OIDC configuration (KEYCLOAK_* spellings also still work)
OIDC_ENABLED=true
OIDC_SERVER_URL=http://localhost:8180           # Must be accessible from browser
OIDC_INTERNAL_URL=http://transcribe-app-keycloak-1:8080
OIDC_REALM=opentranscribe
OIDC_CLIENT_ID=opentranscribe-app
OIDC_CLIENT_SECRET=<paste-client-secret-from-step-4>
OIDC_CALLBACK_URL=http://localhost:5173/login   # Frontend login page, NOT backend API
OIDC_ADMIN_ROLE=admin
OIDC_TIMEOUT=30
```

> **Note**: Database configuration (via admin UI) takes precedence over .env variables.

### Step 8: Apply Configuration

**If configured via Admin UI**: Changes take effect immediately — no restart required.

**If configured via .env only**: Restart the backend to load the new environment variables:

```bash
./opentr.sh stop
./opentr.sh start dev
```

## Testing the Integration

1. Open http://localhost:5173 (OpenTranscribe frontend)
2. Click "Sign in with SSO"
3. You'll be redirected to the provider's login page
4. Enter test user credentials
5. After successful login, you'll be redirected back to OpenTranscribe

## Production Configuration

### Keycloak Server

For production, use a properly secured Keycloak instance:

```bash
# .env for production
OIDC_ENABLED=true
OIDC_SERVER_URL=https://keycloak.yourdomain.com
OIDC_REALM=opentranscribe
OIDC_CLIENT_ID=opentranscribe-app
OIDC_CLIENT_SECRET=<secure-secret>
OIDC_CALLBACK_URL=https://yourdomain.com/login  # Frontend login page
OIDC_ADMIN_ROLE=admin
```

### Security Considerations

1. **HTTPS Required**: Always use HTTPS in production
2. **Client Secret**: Stored encrypted (AES-256-GCM) in database when configured via Admin UI; never commit to git
3. **Token Validation**: OpenTranscribe validates tokens using Keycloak's JWKS endpoint (auto-discovered via OIDC metadata)
4. **Role Mapping**: Only users with the configured `OIDC_ADMIN_ROLE` get admin access
5. **MFA**: Keycloak users bypass OpenTranscribe's local MFA — configure MFA enforcement in your Keycloak realm
6. **Federated Logout**: When a user logs out of OpenTranscribe, the logout is propagated to Keycloak so the Keycloak SSO session is also terminated

### LDAP/AD Federation

To use Keycloak with your existing Active Directory:

1. Go to **User Federation** in Keycloak admin
2. Add **LDAP** provider
3. Configure your AD connection settings
4. Users can then log in to OpenTranscribe using their AD credentials via Keycloak

### Social Login

To enable Google, GitHub, etc.:

1. Go to **Identity Providers** in Keycloak admin
2. Add desired provider (e.g., Google)
3. Configure OAuth credentials from the provider
4. Users can then use social login to access OpenTranscribe

## Troubleshooting

### Common Issues

**"Keycloak authentication is not enabled"**
- If using Admin UI: verify Keycloak/OIDC is enabled in Settings → Authentication → OIDC
- If using .env: ensure `OIDC_ENABLED=true` and restart the backend
- Database config takes precedence — an explicit `enabled=false` in the database overrides a `true` in .env

**"Invalid or expired state parameter"**
- Try the login again (state tokens expire after 10 minutes)
- Clear browser cookies and try again

**"Failed to exchange authorization code"**
- Verify client secret is correct
- Check Keycloak logs: `docker compose logs keycloak`
- Ensure callback URL matches exactly

**Keycloak login page doesn't load or loads slowly**
- Check that `OIDC_SERVER_URL` is accessible from your browser (not just from the server)
- For LAN access, use server IP address (e.g., `http://192.168.x.x:8180`) instead of `localhost`
- Update Keycloak client redirect URIs to include your access URL
- Verify with: `curl http://your-server-ip:8180/realms/opentranscribe/.well-known/openid-configuration`

**Browser shows raw JSON instead of logging in**
- This means the callback URL is pointing to the backend API instead of frontend
- Callback URL MUST be: `http://your-domain/login` (frontend page)
- NOT: `http://your-domain/api/auth/oidc/callback` (backend API)
- Update via Admin UI → Settings → Authentication → Keycloak → Callback URL

**User created but has wrong role**
- Verify role mapping in Keycloak user's "Role mapping" tab
- Check `OIDC_ADMIN_ROLE` matches your Keycloak role name exactly

### Debug Logging

Enable debug logging in OpenTranscribe:

```bash
# In backend container
export LOG_LEVEL=DEBUG
```

Check backend logs for authentication details:
```bash
./opentr.sh logs backend
```

## Government / FedRAMP: Keycloak as X.509 PKI Broker

For government deployments (DoD, federal agencies) Keycloak can act as the
X.509/PKI broker — it validates CAC/PIV certificates via mTLS at the edge and
injects certificate metadata into the OIDC token as claims. OpenTranscribe
consumes those claims to populate the user record and enforce PKI-based admin
access.

### How it works

```
Browser/CAC Reader → Keycloak (mTLS) → OIDC token with cert claims → OpenTranscribe
```

1. The user authenticates to Keycloak using their CAC/PIV certificate
2. Keycloak's X.509 authenticator validates the certificate chain
3. Cert metadata is injected into the OIDC access token as claims
4. OpenTranscribe extracts those claims and stores them on the user record
5. Admin status is granted if the cert DN is in `PKI_ADMIN_DNS` **or** the
   Keycloak realm role matches `OIDC_ADMIN_ROLE`

### Keycloak X.509 Authenticator Setup

1. In the Keycloak admin console, go to **Authentication → Flows**
2. Copy the **browser** flow and name it (e.g. `browser-x509`)
3. Add the **X509/Validate Username Form** execution to the form
4. Set it to **ALTERNATIVE** (allows fallback to password for non-PKI users)
5. Configure the execution:
   - **User Identity Source**: Subject's Common Name
   - **Canonical DN representation enabled**: off (pass DN as-is)
   - **Certificate revocation checking**: OCSP or CRL per your PKI policy
6. Bind the flow to your realm under **Authentication → Bindings → Browser Flow**

### Certificate claim names

Keycloak injects cert metadata using either the short names or the `x509_cert_*`
prefix — OpenTranscribe handles both automatically:

| Short name | x509_cert_* alias | Stored on user |
|---|---|---|
| `cert_dn` | `x509_cert_dn` | `pki_subject_dn` |
| `cert_serial` | `x509_cert_serial` | `pki_serial_number` |
| `cert_issuer` | `x509_cert_issuer` | `pki_issuer_dn` |
| `cert_org` | `x509_cert_org` | `pki_organization` |
| `cert_ou` | `x509_cert_ou` | `pki_organizational_unit` |
| `cert_valid_from` | `x509_cert_not_before` | `pki_not_before` |
| `cert_valid_until` | `x509_cert_not_after` | `pki_not_after` |
| `cert_fingerprint` | `x509_cert_sha256_fingerprint` | `pki_fingerprint_sha256` |

Add a **Protocol Mapper** to your client to include these claims in the access token.

### Government DN format

Government certificates use a space-separated CN: `CN=LastName FirstName emailusername`

Example: `CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US`

OpenTranscribe parses this format automatically — the display name will be
rendered as `John Doe` and the email username token (`jdoe`) is used for
account lookup only.

### PKI-based admin access

Users whose certificate DN appears in `PKI_ADMIN_DNS` receive admin access
regardless of their Keycloak realm role. This allows government system owners
to control admin access via PKI policy rather than requiring manual Keycloak
role assignments.

**Important:** Use semicolons to separate multiple DNs — commas are part of DN
syntax and cannot be used as a list separator:

```env
# Single admin DN
PKI_ADMIN_DNS=CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US

# Multiple admin DNs — semicolon separated
PKI_ADMIN_DNS=CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US;CN=Smith Jane jsmith,OU=Agency,O=U.S. Government,C=US
```

### Environment variables

| Variable | Description |
|---|---|
| `PKI_ADMIN_DNS` | Semicolon-separated list of cert DNs that grant admin access |
| `OIDC_ADMIN_ROLE` | Keycloak realm role that grants admin (default: `admin`) |
| `OIDC_VERIFY_ISSUER` | Validate token issuer against realm URL (default: `true`) |
| `OIDC_VERIFY_AUDIENCE` | Validate token audience against client ID (default: `false`) |

---

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│                 │     │                  │     │                 │
│  OpenTranscribe │────▶│    Keycloak      │────▶│   LDAP/AD/      │
│    Frontend     │     │   (OIDC IdP)     │     │   Social Login  │
│                 │     │                  │     │                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
        │                        │
        │                        │
        ▼                        ▼
┌─────────────────┐     ┌──────────────────┐
│                 │     │                  │
│  OpenTranscribe │◀────│  Token Validation│
│    Backend      │     │  (JWKS/JWT)      │
│                 │     │                  │
└─────────────────┘     └──────────────────┘
```

1. User clicks "Sign in with SSO"
2. Frontend requests authorization URL from backend
3. User is redirected to the provider's login page
4. After login, the provider redirects back with an authorization code
5. Backend exchanges code for tokens
6. Backend validates the **ID token** against the provider's JWKS
7. User is created/updated in OpenTranscribe database
8. OpenTranscribe issues its own JWT for session management


---

## Provider-specific gotchas

These are the ones that produce support tickets. None of them is a bug in
OpenTranscribe; all of them look like one.

**Any provider**
- The redirect URI you register at the provider is the **frontend** `/login` route,
  never a backend path. `/api/auth/oidc/{login,callback}` are internal to this
  application and no provider ever calls them.
- `openid` must be in the requested scopes. It is added automatically if you leave it
  out, because without it the provider issues no ID token — and the ID token is the
  only credential this application authenticates on. There is deliberately no fallback
  to the access token: on most providers it is opaque, and RFC 9068 §6 forbids a client
  inspecting one.

**Keycloak**
- Realm roles are **not in the ID token by default**. Enable "Add to ID token" on the
  `roles` client-scope mappers, or point `oidc_roles_claim` somewhere that is populated.
- The default Audience Resolve mapper emits `aud: account` and does not add your own
  client id.
- The `/auth` path prefix was removed in Keycloak 17 — read `issuer` from discovery
  rather than assuming a path.

**Authentik**
- Discovery is **per application**:
  `https://<host>/application/o/<slug>/.well-known/openid-configuration`.
- The signing algorithm follows the signing **key type**: an RSA key gives RS256, an
  EC P-256 key gives **ES256**. With no signing key selected at all it signs HS256 with
  the client secret and serves an empty JWKS — always select a signing key.
- Groups arrive in a flat `groups` claim emitted by the `profile` scope. Set
  `oidc_roles_claim=groups`.
- `email_verified` is hardcoded `false`, which matters for account linking (see below).

**Okta**
- The org authorization server and a custom authorization server have **literally
  different issuer strings**; a misconfigured RP fails `iss` validation.
- Groups need a Groups claim on the authorization server **and** the `groups` scope.
- Access tokens from the org AS are opaque. Irrelevant here — only the ID token is
  validated — but it is why other integrations break.

**Microsoft Entra ID**
- Multitenant metadata returns the literal placeholder
  `https://login.microsoftonline.com/{tenantid}/v2.0` as the issuer. Use a
  tenant-specific discovery URL.
- App roles arrive in `roles`; `groups` holds object GUIDs unless `optionalClaims`
  asks for names. Above 200 groups the claim is **omitted entirely** and replaced by
  `_claim_names`/`_claim_sources` — so a naive reader sees zero groups for exactly the
  most privileged users.
- `email_verified` is not emitted at all.

**Google**
- No groups claim of any kind, and **no `end_session_endpoint`** — federated logout
  degrades to clearing the local session.
- Two valid issuer spellings (`https://accounts.google.com` and `accounts.google.com`).
- A refresh token is issued only on the **first** code exchange; recover with
  `prompt=consent`.

### Account linking and `email_verified`

When an OIDC identity's `sub` is unknown but its email matches an existing account,
OpenTranscribe links them **only if the provider asserts the address is verified**, and
never for a `super_admin` account. This is the same single rule LDAP and PKI use
(`backend/app/auth/account_linking.py`); a refused link fails the login rather than
creating a duplicate account, and is audited as `ACCOUNT_LINK_REFUSED`.

Practical consequence: on Authentik (hardcoded `false`) and Entra ID (claim absent),
an OIDC login will **not** take over a pre-existing local account with the same
address. Link it deliberately instead — set the account's subject identifier from the
admin UI, or change one of the two addresses. That is an administrator's decision, not
a directory's.
