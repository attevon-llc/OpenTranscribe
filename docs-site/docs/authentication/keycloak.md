---
sidebar_label: Keycloak / OIDC
sidebar_position: 3
---

# Keycloak / OIDC Authentication Setup

This guide covers setting up Keycloak — or any other OpenID Connect provider — for authentication with OpenTranscribe.

## Overview

OpenTranscribe authenticates against an OIDC provider, allowing:
- Single Sign-On (SSO) with your organization's identity provider
- Role-based access control synchronized from the provider
- Support for LDAP/AD federation through Keycloak
- Social login providers (Google, GitHub, etc.) via Keycloak
- Federated logout — when a user's OpenTranscribe session ends, the logout is propagated to the provider (when it publishes an end-session endpoint)

### How endpoints are resolved

There are two modes, and which one you get depends on whether **Discovery URL** is set:

| Discovery URL | Endpoints come from | Use for |
|---|---|---|
| empty (default) | Keycloak's realm layout: `<server>/realms/<realm>/protocol/openid-connect/{auth,token,userinfo,logout,certs}` | Keycloak |
| set | the provider's `.well-known/openid-configuration` document (`authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`, `jwks_uri`, `end_session_endpoint`, `issuer`) | Authentik, Authelia, Okta, Entra ID, Auth0, Zitadel, … |

:::note Corrected in a later release
Earlier versions of this page claimed "full OIDC discovery support (endpoints auto-populated from provider metadata)". That was not true: the realm URL template was the *only* code path, so any non-Keycloak provider returned 404 on the login redirect ([issue #353](https://github.com/attevon-llc/OpenTranscribe/issues/353)). Discovery is now actually implemented and is what the table above describes.
:::

The discovery document is cached in-process for 15 minutes, as is the JWKS. If discovery cannot be fetched, OpenTranscribe logs a warning and falls back to the realm URLs rather than failing the login outright — so an existing Keycloak deployment is never taken down by a metadata blip.

**Internal URL still applies.** When set, back-channel calls (token exchange, JWKS, userinfo, logout) are re-pointed at the internal host while the authorization URL — the one the *browser* follows — stays on the public server URL. That holds for discovered endpoints too.

**ID token first.** Tokens are validated by trying the ID token before the access token. OIDC guarantees the ID token is a JWT audienced to your client; a JWT access token is a Keycloak convenience that most providers do not offer (several issue opaque access tokens that no JWKS can verify).

> **v0.4.0 Change**: Keycloak configuration is now managed via the Super Admin UI (Settings → Authentication → Keycloak/OIDC). Settings are stored encrypted (AES-256-GCM) in the database. Environment variables continue to work as an initial fallback but database config takes precedence.
>
> **MFA Note**: Keycloak users bypass local MFA — their identity provider is responsible for multi-factor authentication. Configure MFA enforcement directly in Keycloak.

## Development Environment Setup

### Step 1: Start Keycloak

```bash
# Start OpenTranscribe with Keycloak test container
./opentr.sh start dev --with-keycloak-test
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
     - `http://your-server-ip/login` (LAN access)
     - `https://yourdomain.com/login` (production)
   - **Valid post logout redirect URIs**: `+`
   - **Web origins**: `+`

   > **IMPORTANT**: Redirect URIs must point to the FRONTEND login page, not the backend API endpoint.

6. Click **Save**

### Step 4: Get Client Secret

1. Navigate to **Clients** → **opentranscribe-app**
2. Go to the **Credentials** tab
3. Copy the **Client secret** value

### Step 5: Create Roles

1. Navigate to **Realm roles** in the left sidebar
2. Create role: `user`
3. Create role: `admin`

### Step 6: Create Test User

1. Navigate to **Users** → **Add user**
2. Fill in username, email, first/last name; set **Email verified**: ON
3. Go to **Credentials** tab → **Set password** (Temporary: OFF)
4. Go to **Role mapping** tab → **Assign role** → select `user` or `admin`

### Step 7: Configure OpenTranscribe

**Recommended Method: Via Admin UI** (stores config encrypted in database)

1. Log in to OpenTranscribe as a super admin
2. Go to **Settings** → **Authentication** → **Keycloak/OIDC**
3. Enable **Keycloak/OIDC** and configure:
   - **Server URL**: `http://localhost:8180` (must be accessible from user's browser)
   - **Internal URL**: `http://transcribe-app-keycloak-1:8080` (backend-to-Keycloak)
   - **Realm**: `opentranscribe`
   - **Client ID**: `opentranscribe-app`
   - **Client Secret**: Paste the secret from Step 4
   - **Callback URL**: `http://localhost:5173/login` (FRONTEND login page, NOT backend API)
   - **Admin Role**: `admin`
4. Click **Save**

> **For LAN Access**: Use your server's IP address for both Server URL and Callback URL:
> - **Server URL**: `http://192.168.x.x:8180`
> - **Callback URL**: `http://192.168.x.x/login`

**Alternative Method: Via .env file** (initial seed fallback)

```bash
KEYCLOAK_ENABLED=true
KEYCLOAK_SERVER_URL=http://localhost:8180
KEYCLOAK_INTERNAL_URL=http://transcribe-app-keycloak-1:8080
KEYCLOAK_REALM=opentranscribe
KEYCLOAK_CLIENT_ID=opentranscribe-app
KEYCLOAK_CLIENT_SECRET=<paste-client-secret>
KEYCLOAK_CALLBACK_URL=http://localhost:5173/login
KEYCLOAK_ADMIN_ROLE=admin
KEYCLOAK_TIMEOUT=30
```

## Other OIDC providers (Authentik, Okta, Entra ID, …)

Any provider that publishes a discovery document works. Two settings matter beyond the usual client ID/secret/callback:

- **Discovery URL** — the provider's `.well-known/openid-configuration`. Setting it disables the Keycloak realm URL template entirely, so **Realm is ignored**.
- **Roles Claim** — the dotted path to the claim carrying group/role names. Default `realm_access.roles` is *Keycloak-specific*; leaving it unchanged on another provider means the admin role never matches and every user lands as a plain user. If the claim is missing from the token, OpenTranscribe falls back to the userinfo endpoint before giving up.

| Provider | Discovery URL | Roles Claim |
|---|---|---|
| Keycloak | *(leave empty — realm URLs are used)* | `realm_access.roles` |
| Authentik | `https://auth.example.com/application/o/<provider-slug>/.well-known/openid-configuration` | `groups` |
| Okta | `https://<org>.okta.com/.well-known/openid-configuration` | `groups` |
| Entra ID | `https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration` | `roles` |

### Worked example: Authentik

1. In Authentik, create an **OAuth2/OpenID Provider** (confidential client, authorization code flow) and note its **slug**. Add an **Application** bound to that provider.
2. Redirect URI: `https://yourdomain.com/login` — the OpenTranscribe **frontend login page**, not the backend API.
3. Make sure the group membership is actually issued. Authentik ships a `groups` scope mapping; add it to the provider's **Scopes**, then request it (see **Scopes** below).
4. In OpenTranscribe → **Settings → Authentication → Keycloak/OIDC**:

   - **Enabled**: on
   - **Discovery URL**: `https://auth.example.com/application/o/opentranscribe/.well-known/openid-configuration`
   - **Server URL**: `https://auth.example.com` *(still used as the public base for the internal-URL swap)*
   - **Realm**: *(ignored when a discovery URL is set)*
   - **Client ID** / **Client Secret**: from the Authentik provider
   - **Callback URL**: `https://yourdomain.com/login`
   - **Roles Claim**: `groups`
   - **Admin Role**: the Authentik **group name** that should map to OpenTranscribe admin, e.g. `opentranscribe-admins`
   - **Scopes**: `openid email profile groups`

Equivalent `.env` seed:

```bash
KEYCLOAK_ENABLED=true
OIDC_DISCOVERY_URL=https://auth.example.com/application/o/opentranscribe/.well-known/openid-configuration
KEYCLOAK_SERVER_URL=https://auth.example.com
KEYCLOAK_CLIENT_ID=<authentik-client-id>
KEYCLOAK_CLIENT_SECRET=<authentik-client-secret>
KEYCLOAK_CALLBACK_URL=https://yourdomain.com/login
KEYCLOAK_ROLES_CLAIM=groups
KEYCLOAK_ADMIN_ROLE=opentranscribe-admins
KEYCLOAK_SCOPES="openid email profile groups"
```

`OIDC_DISCOVERY_URL` and `OIDC_ISSUER` are accepted as aliases of `KEYCLOAK_DISCOVERY_URL` / `KEYCLOAK_ISSUER` — the settings block is named "Keycloak/OIDC" for historical reasons and is not Keycloak-only.

> External identity providers can grant at most the `admin` role. `super_admin` is local-only, by design.

## Testing the Integration

1. Open http://localhost:5173 (OpenTranscribe frontend)
2. Click "Sign in with Keycloak"
3. You'll be redirected to Keycloak login
4. Enter test user credentials
5. After successful login, you'll be redirected back to OpenTranscribe

## Production Configuration

```bash
KEYCLOAK_ENABLED=true
KEYCLOAK_SERVER_URL=https://keycloak.yourdomain.com
KEYCLOAK_REALM=opentranscribe
KEYCLOAK_CLIENT_ID=opentranscribe-app
KEYCLOAK_CLIENT_SECRET=<secure-secret>
KEYCLOAK_CALLBACK_URL=https://yourdomain.com/login
KEYCLOAK_ADMIN_ROLE=admin
```

### Security Considerations

1. **HTTPS Required**: Always use HTTPS in production
2. **Client Secret**: Stored encrypted (AES-256-GCM) in database when configured via Admin UI
3. **Token Validation**: The ID token is verified against the provider's JWKS (from `jwks_uri` when a discovery URL is configured, otherwise the realm `certs` endpoint), with the access token as fallback. Keys are cached for 15 minutes rather than refetched per login
4. **MFA**: Keycloak users bypass OpenTranscribe's local MFA — configure MFA in your Keycloak realm
5. **Federated Logout**: Logout is propagated to Keycloak to terminate the SSO session

### LDAP/AD Federation

To use Keycloak with your existing Active Directory:

1. Go to **User Federation** in Keycloak admin
2. Add **LDAP** provider
3. Configure your AD connection settings
4. Users can then log in to OpenTranscribe using their AD credentials via Keycloak

## Troubleshooting

**"Keycloak authentication is not enabled"**
- Verify Keycloak/OIDC is enabled in Settings → Authentication → Keycloak/OIDC
- Database config takes precedence — an explicit `enabled=false` overrides `true` in .env

**"Invalid or expired state parameter"**
- Try the login again (state tokens expire after 10 minutes)
- Clear browser cookies and try again

**"Failed to exchange authorization code"**
- Verify client secret is correct
- Check Keycloak logs: `docker compose logs keycloak`
- Ensure callback URL matches exactly

**Redirected to `/realms/<something>/protocol/openid-connect/auth` and the provider returns 404**
- That is the Keycloak-only URL shape. Your provider is not Keycloak — set **Discovery URL** to its `.well-known/openid-configuration` and re-try
- If the URL still looks like the realm form afterwards, discovery could not be fetched and OpenTranscribe fell back. Check the backend log for `OIDC discovery failed for …`, and verify the backend container itself can reach the URL (`./opentr.sh shell backend`, then `curl <discovery-url>`)

**Users log in but nobody gets admin**
- **Roles Claim** is still `realm_access.roles` (Keycloak-specific). Set it to your provider's claim — `groups` for Authentik/Okta, `roles` for Entra ID
- **Admin Role** must match the group/role name exactly, case-sensitively
- Make sure the claim is actually issued: add the group scope to the provider *and* list it in **Scopes**

**Keycloak login page doesn't load**
- Check that `KEYCLOAK_SERVER_URL` is accessible from your browser (not just the server)
- For LAN access, use server IP address instead of `localhost`
- Verify: `curl http://your-server-ip:8180/realms/opentranscribe/.well-known/openid-configuration`

**Browser shows raw JSON instead of logging in**
- Callback URL is pointing to the backend API instead of frontend
- Callback URL MUST be: `http://your-domain/login` (frontend page)
- NOT: `http://your-domain/api/auth/keycloak/callback` (backend API)

## Architecture

```
 OpenTranscribe  ────▶  Keycloak (OIDC IdP)  ────▶  LDAP/AD / Social Login
    Frontend                    │
        │                       │
        ▼                       ▼
 OpenTranscribe  ◀────  Token Validation (JWKS/JWT)
    Backend
```

1. User clicks "Sign in with Keycloak"
2. Frontend requests authorization URL from backend
3. User is redirected to Keycloak login
4. After login, Keycloak redirects back with authorization code
5. Backend exchanges code for tokens
6. Backend validates tokens using Keycloak's JWKS
7. User is created/updated in OpenTranscribe database
8. OpenTranscribe issues its own JWT for session management
