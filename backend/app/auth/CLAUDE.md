# app/auth — hybrid authentication (local · LDAP · OIDC · SAML · PKI · proxy)

## Purpose

Multiple auth methods run **simultaneously**, selected per-user by `User.auth_type`
(`local`, `ldap`, `oidc`, `pki`, `proxy`, `saml` — `constants.py:VALID_AUTH_TYPES`, enforced by a
DB CHECK, `v375`, value set swapped by `v378`, widened by `v381`). Configure in the Admin UI
(Settings → Authentication): **DB `auth_config` wins over `.env`, which wins over the coded
default** (`services/auth_config_service.py`). Endpoints live in the `api/endpoints/auth/`
package + `auth_config.py`, not here.

> There is **no `AUTH_TYPE` setting**. It appears in older docs and is described there as
> informational; nothing ever read it. What methods are *available* is decided per-method by
> `local_enabled` / `ldap_enabled` / `oidc_enabled` / `pki_enabled` / `proxy_enabled` /
> `saml_enabled`.

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

## Admission control — who gets an account at all (`v379`)

Authentication answers "are you who you say you are". Admission answers "does this deployment
want you". They were the same question for OIDC, which is how JIT provisioning ended up
minting an account for **every identity in a corporate tenant** on first login.

| Control | Applies to | Empty/off means |
|---|---|---|
| `ldap_user_groups` → `ldap_auth._check_group_access` | LDAP | no group requirement |
| `oidc_allowed_groups` / `oidc_blocked_groups` → `oidc/admission.py` | OIDC | admit everyone |
| `require_account_approval` → `auth/approval.py` | self-registration **and** every JIT path | accounts are usable immediately |

- **Semicolons delimit both group lists**, matching `ldap_auth._parse_group_list`: a group value
  brokered from a directory is a DN and contains commas. Matching is case-insensitive exact.
- **An empty allow-list admits everyone.** Reading it as "admit nobody" would lock out every
  existing OIDC deployment on upgrade. Only a non-empty list restricts.
- **Blocked means denied**, not "exempt from the allow-list", and is evaluated first.
- The check runs at the **top of `sync_oidc_user_to_db`** — before the create branch and before
  the email-match link. Creating first would leave a row behind for a refused identity;
  linking first would hand one a foothold on an existing account. It re-runs on **every** login,
  so removing someone from the group locks them out rather than only affecting new users.
- Refusals return the **same generic 401** an unusable token gets
  (`provisioning.LINK_REFUSED_DETAIL`) — a distinct message is an account-existence oracle. The
  reason goes to the audit log as `AUTH_LOGIN_FAILURE` / `OIDC_ADMISSION_REFUSED`.
- `approval_status` ∈ `pending`/`approved`/`rejected` is **not** `is_active`: deactivation
  revokes an account that was once usable, approval gates one that never has been. Enforcement
  is a lifecycle gate in `api/endpoints/auth/dependencies.py` (`detail.code ==
  "account_pending_approval"` / `"account_rejected"`), not a second mechanism. Turning the
  setting off releases pending accounts; **rejected stays rejected**, and rejection never
  deletes the row. Admin queue: `GET/POST /api/admin/user-approvals` (**admin** tier — managing
  users; the switch that creates the queue is auth config, hence super_admin).
- **The bootstrap super_admin is never pending** (`initial_data._ensure_admin_user` writes it
  explicitly). Only a signed-in administrator can clear the queue.

## Trusted-header auth (`auth_type='proxy'`) — one trust check, two callers

An authenticating reverse proxy (oauth2-proxy, Authelia, Cloudflare Access, an SSO gateway)
asserts the identity in a header. `auth/header_trust.py` is **the** answer to "may this peer
assert an identity?", and `pki_mode='header'` is now a specialisation of it — a proxy vouching
for a subject DN instead of an email. `pki_auth`'s `_pki_*` names are bindings onto those
functions; there is no second copy, and `test_proxy_header_auth.py` fails if one reappears.

| Rule | Where |
|---|---|
| Empty allowlist **refuses every assertion** (not "warn and continue") | `header_trust.header_assertion_permitted` |
| `main.py` refuses to boot hardened with `PROXY_ENABLED` and no allowlist | same shape as the PKI guard |
| The **immediate peer** decides — never `X-Forwarded-For` | `header_trust.immediate_peer_ip` |
| Optional shared secret, constant-time (`X-OpenTranscribe-Proxy-Secret`) | `header_trust.shared_secret_matches` |
| Role header **opt-in and capped at `admin`** | `proxy/assertion._role_from_header` |
| `proxy_allowed_domains`: empty admits everyone | `proxy/assertion.domain_admitted` |
| Every refusal audited, including from an untrusted peer | `proxy/assertion._audit_refusal` |

- **The header is read at sign-in only.** `POST /api/auth/proxy/authenticate` mints a normal
  session (a `refresh_token` row), so idle/absolute timeout, the concurrent cap, the revocation
  epoch and the sessions UI apply unchanged.
- **Per-request consistency**: `dependencies._enforce_proxy_identity_consistency` **revokes**
  (not just 401s) when a trusted peer asserts a different address than the session's user. Three
  narrowings, each of which would otherwise be a DoS: only `auth_type='proxy'` accounts, only a
  header from a trusted peer, and **absence is not an assertion**.
- **Groups: absent ≠ empty.** An absent groups header means "I do not manage your groups" and
  skips membership reconciliation entirely; an empty one reconciles to empty. That is
  `reconcile_user(..., reconcile_memberships=...)`, and `apply_role=False` when neither a role
  header nor a group assertion is present — so a deployment that never opted into header-driven
  privilege cannot have an existing `admin` silently demoted by a login.
- `PROXY_ASSERTS_EMAIL_VERIFIED = True`, unlike PKI's `False`. Here the address **is** the
  assertion (there is no second identifier to bind on), so reading it as unverified would refuse
  every pre-existing account forever. `account_linking`'s super_admin rule still applies
  unconditionally.

## SCIM 2.0 provisioning (`/scim/v2`, RFC 7643/7644)

Mounted at **root**, not under `/api`: RFC 7644 §3.1 fixes the base path and every connector
appends `/Users` to it. Bearer-token authenticated against a hashed `scim_token` row (`v380`)
that a super_admin issues at `/api/admin/scim-tokens` and can revoke.

- Writes go through `services/scim_service.py` → `account_security_service` /
  `idp_group_mapping_service`, never straight to the ORM. `active: false` and `DELETE` both
  **disable and revoke sessions**; `DELETE` is a soft-disable, because a connector dropping
  someone from its assignment scope must not erase their transcripts.
- **No SCIM call writes a role**, and none may touch a `super_admin` (mirrors `directory_sync`
  rule 2). Group membership is written `source='scim'`, which
  `MEMBERSHIP_SOURCES_PROTECTED` shields from directory reconciliation and vice versa.
- **Not rate limited, deliberately** — the credential is 256 random bits, an IdP bursts
  hundreds of requests from a small egress pool, and a per-IP limit would throttle the tenant.
  No handler there carries `@limiter.limit`, hence none declares `response: Response`.
- **Supported filter**: exactly `<attribute> eq "<value>"` on `userName`/`externalId` (Users)
  and `displayName` (Groups). Anything else is `400 invalidFilter` — a partial filter
  implementation returns wrong answers a client acts on.
- **The `PATCH` surface is closed and documented** in `api/endpoints/scim/patch_ops.py`, with
  the unsupported half named there. Unsupported paths are `400 invalidPath`, never a 200 for a
  change that did not happen.

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
  (JIT), `admission.py` (group allow/deny — see above). It replaced a single ~900-line module
  named for one vendor.
- `saml/` — SAML 2.0 SP support (#35), split the same way: `config.py` (`SAMLConfig`,
  DB > .env > default), `sp.py` (the python3-saml settings/request bridge — **signature
  verification is python3-saml's, never hand-rolled**), `assertion.py` (reads attributes off an
  already-verified assertion into `SAMLUserData`), `admission.py` (reuses
  `oidc.admission.check_group_admission` — the group-list syntax is protocol-agnostic),
  `provisioning.py` (JIT). Endpoints (`api/endpoints/auth/saml.py`) are `GET /saml/metadata`
  (public, no secret in it), `GET /saml/login` (SP-initiated), `POST /saml/acs` and
  `GET|POST /saml/sls` — the latter two are the IdP's own POST/redirect targets, so unlike
  OIDC's callback they finish with an HTTP redirect + cookies, not JSON for a `fetch()` caller.
  **Deliberately narrower than OIDC's provisioning**: it does not extend
  `services/idp_group_mapping_service`'s `group_mapping` table (that table's `source` column is
  CHECK-constrained to a closed set — widening it is a separate, independently reviewable schema
  change) or track `(NameID, SessionIndex)` per session, so SP-initiated logout only ends the
  local session rather than also notifying the IdP. Both are documented follow-up scope, not
  silent gaps.
- `header_trust.py` — the trusted-peer allowlist, the immediate-peer resolver, the
  fail-closed refusal and the constant-time shared-secret compare. **Two callers: `proxy/`
  and `pki_auth`.** Do not add a third implementation.
- `proxy/` — trusted-header authentication, split by stage: `config.py` (`ProxyConfig`,
  DB > .env > default), `assertion.py` (trust, secret, admission, role cap),
  `provisioning.py` (JIT + reconciliation).
- `account_linking.py` — **the single** "may this external identity take over an existing
  account?" rule, used by LDAP, PKI, OIDC and SAML alike. Do not add a fifth. SAML always passes
  `email_verified=False` (`saml/assertion.py:SAML_ASSERTS_EMAIL_VERIFIED`, matching PKI/LDAP) —
  SAML has no standard "this address is verified" assertion, so an email-match link is refused
  unconditionally rather than being an admin-togglable setting someone could open by mistake.
- `approval.py` — the `approval_status` state machine and `initial_approval_status`, the one
  function every account-creation path asks "does this start pending?".
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
  `--with-authentik-test` (an Authentik to test OIDC against, :9022, bootstrap
  `admin@example.com`/`admin_password` — the `AUTHENTIK_BOOTSTRAP_*` env vars create
  a working admin account non-interactively, unlike Keycloak's fixed admin/admin
  baked into the image),
  `./opentr.sh start prod --build --with-pki`
  (mTLS at https://localhost:5182 — **prod-only, Vite can't do mTLS**). Client certs:
  `scripts/pki/test-certs/clients/*.p12`.
  **No `--with-saml-test` container yet** — a local test IdP for SAML (SimpleSAMLphp or a
  Keycloak SAML client) is deferred scope, matching the other providers' real-login E2E
  verification round (task #20/#14) rather than shipped ahead of it.
- Setup docs: `docs/PKI_SETUP.md`, `docs/LDAP_AUTH.md`, `docs/OIDC_SETUP.md` (the old
  `docs/KEYCLOAK_SETUP.md` is a redirect stub — ~17 inbound links, including
  `scripts/test-all-auth.sh`, so don't delete it), and
  `docs-site/docs/authentication/{overview,ldap,oidc,saml,pki,proxy,groups}.md`.
  `docs-site/docs/authentication/keycloak.md` is likewise a stub pointing at `oidc.md`;
  Docusaurus has no client-redirects plugin here, so the stub *is* the redirect.
- **Surface that exists but has no admin UI** — say so when documenting it, don't imply a
  panel: directory sync (six `SystemSettings` rows, no endpoint and no panel — beat-driven
  only). IdP group mappings (`/api/admin/group-mappings`) now has one —
  `GroupMappingSettings.svelte`, the "mappings" tab of `AuthenticationSettings` — though it
  only offers `ldap`/`oidc` as the source, so a `proxy`-sourced mapping still has to be created
  through the API. `require_email_verification` also gained a control, in
  `LocalAuthSettings.svelte`.
