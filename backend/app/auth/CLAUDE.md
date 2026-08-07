# app/auth — hybrid authentication (local · LDAP · OIDC · PKI)

## Purpose

Multiple auth methods run **simultaneously**, selected per-user by `User.auth_type`
(`local`, `ldap`, `oidc`, `pki` — `constants.py:VALID_AUTH_TYPES`, enforced by a DB CHECK,
`v375`, value set swapped by `v378`). Configure in the Admin UI (Settings → Authentication):
**DB `auth_config` wins over `.env`, which wins over the coded default**
(`services/auth_config_service.py`). Endpoints live in the `api/endpoints/auth/` package +
`auth_config.py`, not here.

> There is **no `AUTH_TYPE` setting**. It appears in older docs and is described there as
> informational; nothing ever read it. What methods are *available* is decided per-method by
> `local_enabled` / `ldap_enabled` / `oidc_enabled` / `pki_enabled`.

## The identity-source model (issue #354)

Three settings, and they are the answer to "our IdP owns identity, why can users still get in
with a password / still self-register?":

| Setting | Meaning | Safety rule |
|---|---|---|
| `local_enabled` | may accounts holding a local password authenticate at all | **never applies to an active `super_admin`** |
| `allow_registration` | may anyone create their own account | cannot be true while `local_enabled` is false |
| per-user `auth_type` + `allow_local_fallback` | which method authenticates *this* account | `allow_local_fallback` is pki/oidc-only, super_admin-settable |
| `pki_allow_password_fallback` | deployment **ceiling** over the per-user flag for pki accounts | effective = per-user **AND** this; defaults `True` so it adds no restriction on upgrade |

- `local_enabled` does **not** hide the username/password form — LDAP authenticates through the
  same form. The login page renders it on `local_enabled || ldap_enabled`.
- The super_admin exemption is load-bearing, not a convenience: auth configuration is
  super_admin-gated, so without it a deployment that disabled local auth while its IdP was
  misconfigured would have no way back in. Enforced in
  `api/endpoints/auth/authenticators.py:_local_auth_permitted`.
- `allow_registration` is read by `api/endpoints/auth/registration.py` through
  `get_auth_settings(db)`. It previously read `settings.ALLOW_OPEN_REGISTRATION` directly while
  the admin UI wrote the DB key, and `ALLOW_OPEN_REGISTRATION` was missing from
  `ENV_TO_CONFIG_MAPPING` — which is exactly why the toggle did nothing.

## Privilege tiers

`role` ∈ {`user`, `admin`, `super_admin`}. The dividing rule:

> **Anything that changes how the deployment runs, or that stores infrastructure credentials,
> is `super_admin`. Anything that manages users and their content is `admin`.**

| Tier | Dependency | Covers |
|---|---|---|
| user | `get_current_active_user` | own content, own settings, own MFA |
| admin | `get_current_admin_user` | user accounts, tasks, search/speaker maintenance |
| super_admin | `get_current_active_superuser` | auth config, role changes, audit log, ASR/engine settings, backups, media mirror, watch sources, redaction policy |

- **One definition each.** `get_current_super_admin_user` in `api/endpoints/admin.py` and
  `api/endpoints/auth_config.py` are re-export aliases; the real thing lives in
  `api/endpoints/auth/dependencies.py`. It used to be declared three times, each comparing
  against its own `"super_admin"` literal.
- `tests/unit/test_route_privilege_tiers.py` walks the live dependency tree and fails if a new
  route lands at the wrong tier or is accidentally public. Add genuinely-public routes to its
  `KNOWN_PUBLIC` set **with a reason**.
- **Creating more super_admins is a UI action, not a secret**: Settings → Users, role select
  (super_admin-only). `PUT /api/admin/users/{uuid}/role` is the audited endpoint behind it.
  Demoting or deleting the last remaining super_admin is refused.

## Key files

- `oidc/` — the OpenID Connect package, split by protocol stage: `config.py`
  (`OIDCConfig`, DB > .env > default), `discovery.py` (`.well-known` + JWKS, TTL-cached),
  `endpoints.py` (discovery-or-realm resolution), `flow.py` (PKCE, authorization URL,
  token exchange, federated logout), `claims.py` (ID-token verification), `provisioning.py`
  (JIT). It replaced a single ~900-line module named for one vendor.
- `account_linking.py` — **the single** "may this external identity take over an existing
  account?" rule, used by LDAP, PKI and OIDC alike. Do not add a fourth.
- `roles.py` — the authorization contract (read this first, it's 35 lines).
- `token_service.py` — JWT issue/verify, `rotate_refresh_token`, `revoke_all_user_tokens`,
  Redis JTI revocation list + `refresh_token.revoked_at`, **and session lifetime**
  (`_session_within_lifetime`).
- `mfa.py` (`MFAService`) · `lockout.py` (progressive, per-identifier) · `rate_limit.py`
  (slowapi, per-IP with trusted-proxy parsing) · `session.py` (`OIDCStateStore`,
  `InMemoryStore`, `get_redis_client` — **OIDC login state only, not sessions**) ·
  `audit.py` (`AuditLogger`, `AuditEventType`).
- `provider_registry.py` — **cloud-edition seam**, empty in community. `constants.py`
  carries `CLOUD_SEAM_VERSION`; bump it on any seam signature change.
- `direct_auth.py`, `external_sync.py`, `cookies.py`, `password_policy.py`,
  `password_history.py`, `password_reset.py`.

## Conventions / patterns

- **`User.role` ∈ {`user`, `admin`, `super_admin`} is the SOLE authorization truth.**
  `is_superuser` is a **derived mirror** of `role == super_admin`, enforced by CHECK
  constraint `ck_user_superuser_matches_role` (migration `v369`). Never write `is_superuser`
  independently — always via `roles.role_implies_superuser()`. External IdPs may grant at
  most `admin`; `super_admin` is local-only.
- Short-lived JWT access token + long-lived refresh token with **rotation on every use**
  (OAuth 2.1); the old JTI is revoked in the same call.
- **A session IS a `refresh_token` row.** Concurrent-session limits, rotation, revocation,
  the #324 fail-closed fallback and (since `v375`) idle/absolute timeouts all key off those
  rows. `session.py` used to carry a Redis `SessionManager` doing the timeout half with zero
  call sites; it was **deleted**, not wired up — two owners would enforce against different
  session sets the moment Redis and Postgres diverged, and #324 already established that
  Redis is a cache here, not the system of record. Rationale:
  `plans/session-ownership-decision.md`.
- TOTP per RFC 6238/4226 (Google Authenticator / Authy compatible). MFA tokens are
  single-use — the JTI is blacklisted in Redis after verification.
- Auth events go to the **audit log, which is OpenSearch-backed** (`audit.py`), not a table.
- DB models: `UserMFA`, `PasswordHistory`, `RefreshToken` (`app/models/`).

## How it connects

- Frontend auth is **httpOnly-cookie based — there is no JS-readable token.** In-page calls
  use `fetch(..., {credentials: 'same-origin'})`.
- `GET /api/auth/session` is the SPA's session probe: **200 for anonymous, never 401**
  (a 401 there caused a spurious logout cascade). It returns `refreshable` when only a
  refresh cookie is present.

## The OIDC surface is named `oidc_*`, and a test enforces it

A user in the field reported that "Keycloak" support looked hardcoded to Keycloak.
Discovery (#353) made a generic provider work; the rename (`v377`/`v378`) made that
*visible*, because every field an Authentik admin typed into was still named for
someone else's product.

- Config keys, schema, service, routes (`/api/auth/oidc/*`), admin panel and i18n are
  all `oidc_*`. The IdP redirect URI points at the SPA's `/login`, never at these
  routes, so the rename needed no identity-provider reconfiguration.
- **`KEYCLOAK_*` environment variables keep working forever**, translated onto the
  canonical `OIDC_*` names by `core/legacy_auth_env.py` before `Settings` is built. The
  legacy spelling **wins** when both are set. That module is an *input adapter*, not a
  second implementation — nothing downstream, including
  `AuthConfigService.ENV_TO_CONFIG_MAPPING`, ever sees the old name.
- `tests/unit/test_oidc_naming_invariant.py` fails the build if the retired noun
  appears in any Python file under `backend/app/` outside a three-entry allow-list,
  each carrying a written reason: the env adapter, `db/migrations.py` (historical
  schema fingerprints for pre-`v378` databases), and the deprecated
  `AuthMethodsResponse.keycloak_enabled` computed field, which is emitted for one
  minor release so a cached SPA bundle keeps rendering the SSO button.

## Gotchas

- **Every token carries a `type` claim and every consumer verifies it.** Access, refresh and
  MFA tokens are signed with the same key, so `type` is the only thing separating them —
  without the check the MFA half-token (handed out BEFORE the second factor) was a full
  session, i.e. a complete MFA bypass. Never widen `get_current_user` to accept another type;
  the forced-enrolment flow uses a separate narrow dependency
  (`mfa_tokens.get_user_for_enrollment`) scoped to `/mfa/setup` + `/mfa/verify-setup` only, and
  distinguishes an `enroll`-scoped half-token from a `verify`-scoped one.
- **"May this account use a local password?" has exactly one implementation** —
  `auth/utils.py:local_password_allowed`, keyed off `AUTH_TYPES_SUPPORT_LOCAL_FALLBACK` /
  `AUTH_TYPES_NO_LOCAL_FALLBACK`. It existed twice and the two disagreed: only the raw-SQL path
  hard-blocked LDAP, so an LDAP account with `allow_local_fallback` authenticated against a
  local hash via the ORM path. The write side is guarded too
  (`services/account_security_service.py`).
- **PKI/OIDC users bypass MFA only when they used their native method.** If they fall
  back to a local password, MFA still applies (`api/endpoints/auth/login.py`, the
  `actual_auth_method` check).
  `AUTH_TYPES_SUPPORT_LOCAL_FALLBACK = [pki, oidc]`; LDAP never has a local password.
- **Changing a credential or a privilege must revoke sessions.** Go through
  `services/account_security_service.py` — it also applies the password policy and writes the
  audit event, all three of which were previously applied inconsistently across
  `users.py` / `admin.py` / `password_reset.py`. Access tokens are stateless, so revocation
  reaches them via a **per-user epoch** (`token_service.stamp_user_revocation_epoch`), not a
  JTI list.
- **`absolute_expires_at` is carried forward, never recomputed.** It is the only thing that
  caps a client which refreshes forever; `expires_at` moves with each rotation and therefore
  caps nothing. `last_activity_at` is the half that does move. Both are **nullable and
  un-backfilled** — NULL is "no cap recorded", treated as valid and stamped on the row's
  first rotation, so upgrading does not sign everyone out a second time.
- **Idle timeout is checked at refresh, not per request.** Deliberate: polling endpoints
  (progress, notifications, task status) and WebSocket keepalives would reset a per-request
  activity clock continuously, so the control would read as satisfied and never fire. The
  error is bounded by the access-token lifetime. True per-request idle timeout needs an
  explicit non-activity denylist and is its own change.
- **PKI header trust fails closed.** With `PKI_ENABLED` and no `PKI_TRUSTED_PROXIES`, header-
  sourced authentication is refused outright (and `main.py` refuses to start when hardened).
  A DN header is only trusted from a configured proxy or alongside a validated certificate —
  the proxy is what terminates mTLS and vouches for the DN.
- **`pki_mode` is `header` | `mutual_tls`, and `pki_auth.py` reads it.** It used to be
  `direct`/`broker`/`hybrid` in the schema and `header`/`mutual_tls` in the UI — no value
  could match, so **every save of the PKI tab was rejected**, and no backend code branched
  on it either way. `mutual_tls` refuses a DN-header-only assertion even from a trusted
  proxy: the certificate itself must be forwarded so this process validates it.
  `pki_support_cac` / `pki_support_piv` were deleted (v375 also drops their rows) — the CAC
  and PIV CN formats are parsed unconditionally for every certificate.
- **The login banner is enforced, not just displayed** (AC-8). `get_current_active_user`
  refuses with `detail.code == "banner_acknowledgment_required"` while
  `login_banner_enabled` and the user has no `banner_acknowledged_at` **or** theirs predates
  the last edit of `login_banner_text` (compared against that config row's `updated_at`, so
  changing the wording re-asks). `BANNER_EXEMPT_PATHS` keeps `/auth/banner`,
  `/auth/banner/acknowledge` and both logout routes reachable. Deliberately **not** audited
  per request — it would fire on every request of every pre-acknowledgment session; the
  acknowledgment itself is the audit artefact.
- **Only the ID token authenticates an OIDC login.** `validate_token` used to try the
  ID token and then fall back to the access token, accepting whichever verified — so an
  ID token failing `aud`/`iss` silently downgraded to a credential RFC 9068 §6 forbids
  the client from inspecting, whose `aud` means something else entirely, and which is
  opaque on Okta/Google/Entra. The fallback is deleted; a missing or invalid ID token is
  a hard 401. `openid` is forced into the requested scopes for the same reason.
  The access token is still used — as a **bearer credential** against `userinfo`, which
  is what it is for.
- **The OIDC ID token lives on the session row, never in a cookie**
  (`refresh_token.oidc_id_token`, `v378`, encrypted at rest). RP-Initiated Logout needs
  it as `id_token_hint`, so it has to outlive the callback; a cookie would expose the
  full identity claim set to anything that reaches the cookie jar and outlive the
  session that justified it. On the session row, rotation/revocation/the concurrent
  cap already delete it.
- **`user.oidc_subject` is a `sub`, unique only per ISSUER.** The UNIQUE index on it is
  sound only while exactly one provider is configured; multi-provider means keying on
  `(iss, sub)`. The old column name asserted a global identifier, which is why it was
  renamed rather than left alone.
- **`user.auth_type` had TWO CHECK constraints** — `ck_user_auth_type_valid` (v375) and
  a legacy `users_auth_type_check` (v200, re-asserted by v367/v371, still in
  `database/init_db.sql`). Swapping only the first would not have failed the migration;
  it would have failed every OIDC login afterwards with a CheckViolation on JIT
  provisioning. `v378` drops the duplicate — one rule, one owner — and
  `test_v378_migration_consistency.py` pins that exactly one remains.
- **Secrets never leave the auth-config API.** `config_value` is `None` for a sensitive key and
  `is_set` carries the signal. Do not reintroduce a placeholder: returning `***REDACTED***`
  meant the admin panel bound it into the password field and the next Save encrypted it over
  the real LDAP bind password.
- **Negative login tests MUST use a nonexistent account** — never a wrong password for
  `admin@example.com`. Lockout is progressive per-account and poisons the whole suite.
- **Dev relaxes auth limits** (`docker-compose.override.yml`: `RATE_LIMIT_AUTH_PER_MINUTE`
  120, `ACCOUNT_LOCKOUT_THRESHOLD` 100, `..._DURATION_MINUTES` 1; `DEV_*` tunable in `.env`).
  Prod keeps strict values — the override is never loaded there. Env changes need a
  container **recreate**, not `restart-backend`.
- Local IdPs for testing: `--with-ldap-test` (LDAP :3890, UI :17170, `admin`/`admin_password`),
  `--with-keycloak-test` (a Keycloak to test OIDC against, :8180, `admin`/`admin`),
  `./opentr.sh start prod --build --with-pki`
  (mTLS at https://localhost:5182 — **prod-only, Vite can't do mTLS**). Client certs:
  `scripts/pki/test-certs/clients/*.p12`.
- Setup docs: `docs/PKI_SETUP.md`, `docs/LDAP_AUTH.md`, `docs/OIDC_SETUP.md`,
  `docs-site/docs/authentication/{overview,pki,ldap,keycloak}.md` (the docs-site page is
  still named for the old provider — Phase 5 of `plans/oidc-conformance-plan.md`).
