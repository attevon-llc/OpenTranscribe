# auth/oidc — the OIDC surface

Moved intact from `backend/app/auth/CLAUDE.md`.

## The OIDC surface is named `oidc_*`, and a test enforces it

A user in the field reported that "Keycloak" support looked hardcoded to Keycloak.
Discovery (#353) made a generic provider work; the rename (`v379`/`v380`) made that
*visible*, because every field an Authentik admin typed into was still named for
someone else's product.

- Config keys, schema, service, routes (`/api/auth/oidc/*`), admin panel and i18n are
  all `oidc_*`. The IdP redirect URI points at the SPA's `/login`, never at these
  routes, so the rename needed no identity-provider reconfiguration.
- **`KEYCLOAK_*` environment variables keep working forever**, translated onto the
  canonical `OIDC_*` names by `core/legacy_auth_env.py` before `Settings` is built. The
  legacy spelling **wins** when both are set. That module is an *input adapter*, not a
  second implementation — nothing downstream, including
  `auth_config_keys.ENV_TO_CONFIG_MAPPING`, ever sees the old name.
- `tests/unit/test_oidc_naming_invariant.py` fails the build if the retired noun
  appears in any Python file under `backend/app/` outside a three-entry allow-list,
  each carrying a written reason: the env adapter, `db/migrations.py` (historical
  schema fingerprints for pre-`v380` databases), and the deprecated
  `AuthMethodsResponse.keycloak_enabled` computed field, which is emitted for one
  minor release so a cached SPA bundle keeps rendering the SSO button.

- **Only the ID token authenticates an OIDC login.** `validate_token` used to try the
  ID token and then fall back to the access token, accepting whichever verified — so an
  ID token failing `aud`/`iss` silently downgraded to a credential RFC 9068 §6 forbids
  the client from inspecting, whose `aud` means something else entirely, and which is
  opaque on Okta/Google/Entra. The fallback is deleted; a missing or invalid ID token is
  a hard 401. `openid` is forced into the requested scopes for the same reason.
  The access token is still used — as a **bearer credential** against `userinfo`, which
  is what it is for.
- **The OIDC ID token lives on the session row, never in a cookie**
  (`refresh_token.oidc_id_token`, `v380`, encrypted at rest). RP-Initiated Logout needs
  it as `id_token_hint`, so it has to outlive the callback; a cookie would expose the
  full identity claim set to anything that reaches the cookie jar and outlive the
  session that justified it. On the session row, rotation/revocation/the concurrent
  cap already delete it.
- **`user.oidc_subject` is a `sub`, unique only per ISSUER.** The UNIQUE index on it is
  sound only while exactly one provider is configured; multi-provider means keying on
  `(iss, sub)`. The old column name asserted a global identifier, which is why it was
  renamed rather than left alone.
