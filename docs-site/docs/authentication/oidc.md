---
sidebar_label: OpenID Connect (OIDC)
sidebar_position: 3
---

# OpenID Connect (OIDC)

OpenTranscribe authenticates against **any conforming OpenID Connect provider** — Keycloak,
Authentik, Authelia, Okta, Entra ID, Auth0, Zitadel, Google — using the authorization-code flow
with PKCE.

:::info Renamed in v0.5.0
The whole surface used to be named for one vendor. Configuration keys are now `oidc_*`, the
admin panel tab is **OIDC**, and the backend routes are `/api/auth/oidc/login` and
`/api/auth/oidc/callback`.

**Nothing needs reconfiguring at your identity provider**: the registered redirect URI points
at the SPA's `/login` page, never at those backend routes.

**Every `KEYCLOAK_*` environment variable keeps working, permanently.** They are translated
onto the canonical `OIDC_*` names before settings are built, and the legacy spelling *wins*
when both are set. The backend logs one line at startup naming any legacy variables it found.
:::

## How endpoints are resolved

There are two modes, and which one you get depends on whether **Discovery URL** is set:

| Discovery URL | Endpoints come from | Use for |
|---|---|---|
| empty (default) | the realm URL template `<server>/realms/<realm>/protocol/openid-connect/{auth,token,userinfo,logout,certs}` | Keycloak |
| set | the provider's `.well-known/openid-configuration` document (`authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`, `jwks_uri`, `end_session_endpoint`, `issuer`) | everyone else |

The realm template is one vendor's URL shape. Pointing it at Authentik produced a 404 on the
login redirect ([issue #353](https://github.com/attevon-llc/OpenTranscribe/issues/353)) — which
is what discovery fixes.

**When a discovery URL is set, `oidc_realm` is ignored.**

The discovery document and the JWKS are cached in-process for **15 minutes**. If discovery
cannot be fetched, the backend logs a warning and falls back to the realm URLs rather than
failing the login outright, so a metadata blip never takes down a working deployment. A
discovery response that is missing required endpoints is not cached, so a broken configuration
is not pinned for the whole TTL.

**Internal URL still applies.** When set, back-channel calls (token exchange, JWKS, userinfo,
logout) are re-pointed at the internal host while the authorization URL — the one the *browser*
follows — stays on the public server URL. That holds for discovered endpoints too.

The discovery URL is admin-supplied and is validated against SSRF, but with private addresses
**allowed**: an IdP on the LAN or on the compose network (`http://idp:8080`) is the normal case
here, not an attack.

## Token validation

**Only the ID token authenticates.** OIDC Core guarantees the ID token is a JWT audienced to
your client. A missing or invalid ID token is a hard 401.

:::warning Changed in v0.5.0
Validation used to try the ID token and then **fall back to the access token**, accepting
whichever verified. That is an attacker-influenceable downgrade onto a credential RFC 9068 §6
forbids the client from inspecting, whose `aud` means something else entirely, and which is
opaque on Okta, Google and Entra ID. The fallback is deleted. `openid` is now forced into the
requested scopes for the same reason — without it the provider issues no ID token at all.
:::

The access token is still used, as a **bearer credential against `userinfo`**, which is what it
is for: when the configured roles claim is absent from the ID token, OpenTranscribe reads it
from userinfo before giving up.

| Setting | Default | Effect |
|---|---|---|
| `oidc_verify_audience` | `true` | Reject a token minted for another client of the same IdP. Compared against `oidc_audience` if set, else `oidc_client_id`. |
| `oidc_verify_issuer` | `true` | Compared against the discovery document's `issuer`, or `oidc_issuer` if you override it. |
| `oidc_use_pkce` | `true` | |

Both validation controls default **on**, and an unparseable stored value falls back to the
declared default rather than to `false`.

## Configuration reference

Settings → Authentication → **OIDC** (super_admin). Environment names in the third column are
the canonical spelling; the historical `KEYCLOAK_*` name of each also still works.

| Field | Config key | Environment |
|---|---|---|
| Enabled | `oidc_enabled` | `OIDC_ENABLED` |
| Server URL | `oidc_server_url` | `OIDC_SERVER_URL` |
| Internal URL | `oidc_internal_url` | `OIDC_INTERNAL_URL` |
| Discovery URL | `oidc_discovery_url` | `OIDC_DISCOVERY_URL` |
| Realm *(ignored when Discovery URL is set)* | `oidc_realm` | `OIDC_REALM` |
| Client ID | `oidc_client_id` | `OIDC_CLIENT_ID` |
| Client Secret *(sensitive)* | `oidc_client_secret` | `OIDC_CLIENT_SECRET` |
| Callback URL | `oidc_callback_url` | `OIDC_CALLBACK_URL` |
| Admin Role | `oidc_admin_role` | `OIDC_ADMIN_ROLE` |
| Roles Claim | `oidc_roles_claim` | `OIDC_ROLES_CLAIM` |
| Issuer override | `oidc_issuer` | `OIDC_ISSUER` |
| Scopes | `oidc_scopes` | `OIDC_SCOPES` |
| Timeout (s) | `oidc_timeout` | `OIDC_TIMEOUT` |
| Verify audience | `oidc_verify_audience` | `OIDC_VERIFY_AUDIENCE` |
| Audience | `oidc_audience` | `OIDC_AUDIENCE` |
| Use PKCE | `oidc_use_pkce` | `OIDC_USE_PKCE` |
| Verify issuer | `oidc_verify_issuer` | `OIDC_VERIFY_ISSUER` |

Defaults: `oidc_realm=opentranscribe`, `oidc_admin_role=admin`,
`oidc_roles_claim=realm_access.roles`, `oidc_scopes=openid email profile`, `oidc_timeout=30`.

**The Callback URL is the frontend login page**, e.g. `https://yourdomain.com/login` — not a
backend API path. Pointing it at the backend is the cause of "the browser shows raw JSON
instead of logging in".

## Per-provider settings

**Roles Claim** is the dotted path to the claim carrying group or role names. The default
`realm_access.roles` is realm-provider–specific; leaving it unchanged elsewhere means the admin
role never matches and every user lands as a plain `user`.

| Provider | Discovery URL | Roles Claim |
|---|---|---|
| Keycloak | *(leave empty — realm URLs are used)* | `realm_access.roles` |
| Authentik | `https://auth.example.com/application/o/<slug>/.well-known/openid-configuration` | `groups` |
| Okta | `https://<org>.okta.com/.well-known/openid-configuration` | `groups` |
| Entra ID | `https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration` | `roles` |
| Authelia | `https://auth.example.com/.well-known/openid-configuration` | `groups` |

Some providers only issue group membership when a dedicated scope is requested (Authentik's
`groups` scope, Okta's `groups`). Add it to the provider **and** to **Scopes**, or the claim
will be absent from the token.

## Local development against Keycloak

```bash
./opentr.sh start dev --with-keycloak-test
```

This starts a Keycloak at `http://localhost:8180` (`admin` / `admin`) purely as an OIDC
provider to test against.

1. Create a realm `opentranscribe`.
2. Create a client: **Client type** OpenID Connect, **Client ID** `opentranscribe-app`,
   **Client authentication** ON (confidential).
   **Valid redirect URIs**: `http://localhost:5173/login` (and your LAN/production origins).
   **Valid post logout redirect URIs**: `+`. **Web origins**: `+`.
3. Copy the secret from **Credentials**.
4. Create realm roles `user` and `admin`.
5. Create a test user with **Email verified: ON**, set a non-temporary password, and assign a
   role.
6. In OpenTranscribe → Settings → Authentication → OIDC:
   - **Server URL** `http://localhost:8180` (must be reachable from the *browser*)
   - **Internal URL** `http://transcribe-app-keycloak-1:8080` (backend → Keycloak)
   - **Realm** `opentranscribe`, **Client ID** `opentranscribe-app`, **Client Secret** from
     step 3
   - **Callback URL** `http://localhost:5173/login`
   - **Admin Role** `admin`

For LAN access use the server's IP for both Server URL and Callback URL.

## Worked example: Authentik

1. Create an **OAuth2/OpenID Provider** (confidential, authorization-code flow) and note its
   **slug**; bind an **Application** to it.
2. Redirect URI: `https://yourdomain.com/login` — the OpenTranscribe frontend login page.
3. Add Authentik's `groups` scope mapping to the provider's **Scopes**.
4. In OpenTranscribe → Settings → Authentication → OIDC:
   - **Discovery URL**
     `https://auth.example.com/application/o/opentranscribe/.well-known/openid-configuration`
   - **Server URL** `https://auth.example.com` (still the public base for the internal-URL swap)
   - **Client ID** / **Client Secret** from the provider
   - **Callback URL** `https://yourdomain.com/login`
   - **Roles Claim** `groups`
   - **Admin Role** the Authentik group name, e.g. `opentranscribe-admins`
   - **Scopes** `openid email profile groups`

Equivalent `.env` seed:

```bash
OIDC_ENABLED=true
OIDC_DISCOVERY_URL=https://auth.example.com/application/o/opentranscribe/.well-known/openid-configuration
OIDC_SERVER_URL=https://auth.example.com
OIDC_CLIENT_ID=<authentik-client-id>
OIDC_CLIENT_SECRET=<authentik-client-secret>
OIDC_CALLBACK_URL=https://yourdomain.com/login
OIDC_ROLES_CLAIM=groups
OIDC_ADMIN_ROLE=opentranscribe-admins
OIDC_SCOPES="openid email profile groups"
```

## Admission control

Point OIDC at a corporate realm and, without these, **every identity in that realm gets an
account here** on first login — JIT provisioning was unconditional, and `oidc_admin_role`
*elevates* rather than *admits*.

| Setting | Meaning |
|---|---|
| `oidc_allowed_groups` | A login must carry at least one of these values in the roles claim. **Empty admits everyone** |
| `oidc_blocked_groups` | These values deny outright. Evaluated **first** — "blocked" means refused, not "exempt from the allow-list" |
| `require_account_approval` *(Authentication → Local)* | New accounts land `pending` and need an administrator to approve them |

Both lists are **semicolon-delimited**, because a group value brokered from a directory is a DN
and contains commas. Matching is case-insensitive exact, against the claim named by
`oidc_roles_claim`.

The check runs before the account is created *and* before the email-match link, and re-runs on
every login — so removing someone from the group locks them out rather than only affecting new
users. A refusal returns the same generic 401 an unusable token gets; the reason goes to the
audit log.

## Provisioning, roles and groups

A user who authenticates via OIDC is created on first login (`auth_type='oidc'`,
`oidc_subject` = the token's `sub`).

- **`oidc_admin_role`** is the legacy single-role signal: a caller carrying that value in the
  roles claim becomes `admin`.
- **Group mappings** ([IdP group mapping](./groups)) map any claim value onto an in-app group
  and/or a role, and are applied on every login. The two are OR-ed, so a deployment with no
  mappings behaves exactly as it did before.
- **An identity provider can grant at most `admin`.** `super_admin` is local-only.
- OIDC group mappings are applied **at login only**. There is no provider-neutral "list users"
  primitive, so unlike LDAP there is no periodic OIDC sweep.

### Account linking

An OIDC login only takes over a pre-existing account with the same email address when the
provider asserts `email_verified: true`, and **never** when that account is a `super_admin`.

:::warning Behaviour change in v0.5.0
**Authentik hardcodes `email_verified` to `false`; Entra ID omits the claim.** On those
providers the takeover path is closed. If you need an OIDC identity attached to an existing
account, link it deliberately — set that account's subject from the admin UI — or change one of
the two addresses. A refusal fails the login with the same generic error as a bad credential
and is audited with `error_code ACCOUNT_LINK_REFUSED`.
:::

## Sessions and logout

The provider's **ID token is stored on the session row** (`refresh_token.oidc_id_token`,
encrypted at rest), never in a cookie. RP-Initiated Logout needs it as `id_token_hint`, so it
has to outlive the callback — but a cookie would expose the full identity claim set to anything
that reaches the cookie jar, and would outlive the session that justified it. On the session
row, rotation, revocation and the concurrent-session cap already delete it.

Logging out of OpenTranscribe attempts a back-channel logout against the provider's
`end_session_endpoint`. A provider whose metadata omits that endpoint has no back-channel
logout; the local session is still cleared either way, and a failed federated logout is
reported rather than presented as a clean sign-out.

## MFA

An OIDC user bypasses OpenTranscribe's local MFA **only when they authenticated through the
provider** — configure MFA at the IdP. If the account has `allow_local_fallback` and signs in
with a local password instead, local MFA still applies.

## Troubleshooting

**"OIDC authentication is not enabled"**
Check Settings → Authentication → OIDC. Database configuration takes precedence, so an explicit
`enabled=false` there overrides `true` in `.env`.

**Redirected to `/realms/<something>/protocol/openid-connect/auth` and the provider 404s**
That is the realm URL shape. Set **Discovery URL** to your provider's
`.well-known/openid-configuration`. If the URL still looks like the realm form afterwards,
discovery could not be fetched and the backend fell back — check the log for
`OIDC discovery failed for …`, and verify the *backend container* can reach the URL
(`./opentr.sh shell backend`, then `curl <discovery-url>`).

**Users log in but nobody gets admin**
- **Roles Claim** is still `realm_access.roles`. Set it to your provider's claim.
- **Admin Role** must match the group/role name exactly, case-sensitively.
- Confirm the claim is actually issued: add the group scope to the provider *and* list it in
  **Scopes**.

**Login fails with "Invalid access token" for a user who already has a local account**
The email-match link was refused. See [Account linking](#account-linking).

**"Invalid or expired state parameter"**
State tokens expire after 10 minutes. Retry, and clear cookies if it persists.

**"Failed to exchange authorization code"**
Verify the client secret and that the callback URL matches exactly.

**The browser shows raw JSON instead of logging in**
The callback URL points at the backend API. It must be the frontend page
(`https://your-domain/login`).

## Related

- Long-form setup guide with more provider walkthroughs: `docs/OIDC_SETUP.md` in the repository.
- [IdP group mapping](./groups)
- [Authentication overview](./overview)
