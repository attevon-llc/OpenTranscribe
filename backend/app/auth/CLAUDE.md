# app/auth — hybrid authentication (local · LDAP · OIDC · SAML · PKI · proxy)

## Purpose

Multiple auth methods run **simultaneously**, selected per-user by `User.auth_type`
(`local`, `ldap`, `oidc`, `pki`, `proxy`, `saml` — `core/auth_types.py:VALID_AUTH_TYPES`, enforced by a
DB CHECK, `v377`, value set swapped by `v380`, widened by `v383`). The value set the CHECK admits has ONE home, `core/auth_types.py:DB_ADMITTED_AUTH_TYPES` (`auth/constants.py` only re-exports it for the cloud seam) (a superset of
`VALID_AUTH_TYPES`); the model CHECK bodies are rendered from it and
`tests/unit/test_auth_type_allowlist_single_source.py` pins the newest migration to it. Configure in the Admin UI
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
- With `local_enabled` off, the anonymous local-account ROUTES are gated
  (`api/endpoints/auth/local_auth_gate.py`, issue #997): `verify-email` / `verify-email/resend`
  404, `register` 403s even if the env fallback says open, and `password-reset/request` /
  `confirm` serve **only the active `super_admin` break-glass account** — anyone else gets the
  usual generic answer with no reset work and no audit write. The *service*
  (`auth/password_reset.py`) is still ungated: it checks `auth_type == 'local'` and
  `is_active`, never `local_enabled`. The break-glass chain completing is the point: an
  operator who disabled local auth while their IdP was misconfigured, and who has also lost
  the break-glass password, has no other way back into the super_admin-gated auth config
  (issue #910; pinned by `tests/unit/test_identity_source_policy.py` and, over HTTP, by
  `tests/api/test_local_auth_disabled_surface.py`). `/token` and the invitation routes are
  NOT gated: LDAP signs in through `/token`, and invitations also provision external-IdP
  accounts.
- The super_admin exemption is load-bearing, not a convenience: auth configuration is
  super_admin-gated, so without it a deployment that disabled local auth while its IdP was
  misconfigured would have no way back in. Enforced in
  `api/endpoints/auth/authenticators.py:_local_auth_permitted`.
- `allow_registration` is read by `api/endpoints/auth/registration.py` through
  `get_auth_settings(db)`. It previously read `settings.ALLOW_OPEN_REGISTRATION` directly while
  the admin UI wrote the DB key, and `ALLOW_OPEN_REGISTRATION` was missing from
  `ENV_TO_CONFIG_MAPPING` — which is exactly why the toggle did nothing.

## Admission control — who gets an account at all (`v381`)

Group allow/block lists decide admission: an empty allow-list admits everyone, blocked means denied and is evaluated first, semicolons delimit both lists, and the bootstrap super_admin is never pending. Rules and the migration contract: `backend/app/auth/notes/admission-control.md`.

## Trusted-header auth (`auth_type='proxy'`) — one trust check, two callers

The header is read at sign-in only; per-request consistency revokes on mismatch; absent groups header is not empty groups. Rules: `backend/app/auth/proxy/CLAUDE.md`.
## SCIM 2.0 provisioning (`/scim/v2`, RFC 7643/7644)

No SCIM call writes a role or touches a `super_admin`; the endpoint is deliberately not rate limited; the filter and PATCH surfaces are closed. Rules: `backend/app/api/endpoints/scim/CLAUDE.md`.
## Privilege tiers

`role` ∈ {`user`, `admin`, `super_admin`}. The dividing rule:

> **Anything that changes how the deployment runs, or that stores infrastructure credentials,
> is `super_admin`. Anything that manages users and their content is `admin`.**

| Tier | Dependency | Covers |
|---|---|---|
| user | `get_current_active_user` | own content, own settings, own MFA |
| admin | `get_current_admin_user` | user accounts, tasks, search/speaker maintenance |
| super_admin | `get_current_active_superuser` | auth config, role changes, audit log, ASR/engine settings, backups, media mirror, watch sources, redaction policy |

admin/super_admin manage the platform; in multi-tenant mode they reach tenant content only
through a support-access grant (`services/platform_bypass.py`, `api/endpoints/support_access.py`;
issue #1122). In single-tenant mode nothing changes. A grant never confers `org:admin`, and every
`/org-admin/*` route resolves the base context, which ignores the grant header.

- **Every platform-role path is a row in `docs/security/platform-access-matrix.tsv`** (#1281),
  derived from the route table and an AST scan by `tests/fixtures/platform_access_matrix.py`.
  Adding a route behind an admin dependency, a `.allows(` call, a `sees_all_in_scope` read or
  anything that builds a bypass fails `tests/unit/test_platform_access_matrix.py` until you run
  `scripts/generate-platform-access-matrix.py --write`, read the diff and, for a platform-op
  route, classify it in `PLATFORM_OP_RULES` with a reason. A platform route that returns another
  tenant's filenames must call `audit_metadata_access`; one that acts on a row takes
  `ctx.bypass`. `tests/api/test_support_access_matrix.py` proves no/pending/expired/revoked/wrong
  tenant per chokepoint kind and sweeps every parameterless GET for leaks.
- **One definition each.** The super-admin gate is `get_current_active_superuser` in
  `api/endpoints/auth/dependencies.py`, imported under that name everywhere (no aliases;
  `tests/unit/test_route_privilege_tiers.py` checks the admin and auth-config modules). It used to be declared three times, each comparing
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
  **Two refusals, two remedies, and neither self-heals — by design** (issue #867). The
  provider-ID branch refuses when the source's asserted email no longer matches the stored one,
  and that check runs *before* every provider's profile-refresh code, so an ordinary IdP-side
  rename is a **permanent** lockout until an administrator acts:
  | Refusal | What is wrong | Remedy (both super_admin, both audited, both refuse a `super_admin` target) |
  |---|---|---|
  | email-match | the account carries no provider identifier | `PUT /api/admin/users/{uuid}/link-identity` |
  | provider-ID | the identifier matches, the stored email is stale | `PUT /api/admin/users/{uuid}/external-email` |
  Both are reachable from the same "Link an external identity" modal in Settings → Users.
  The email remedy **refuses an account carrying no external identifier**, so it is a remedy for
  a linked identity and not a general "rewrite anyone's login email" power — for a `local`
  account the email *is* the credential identity. The comparison itself is
  `account_linking.emails_agree` (case- and whitespace-insensitive, the same rule
  `dependencies._enforce_proxy_identity_consistency` already applied): it was a byte-exact
  compare, so an IdP that merely re-cased an address locked the account out.
  ⚠️ **Both remedies now also refuse an `auth_type == local` target (issue #912).**
  `link-identity` used to set the provider column without touching `auth_type`, so a `local`
  account could end up carrying an `ldap_uid`/`oidc_subject`/`pki_subject_dn` while every other
  reader of `auth_type` still believed it authenticated with a local password — the account
  resolved through the provider-ID login branch above while its own row said otherwise.
  `link-identity` now **converts** `auth_type` to the provider being linked (and revokes
  sessions) whenever the account's `auth_type` is exactly `local` at call time; an
  already-external `auth_type` (a `pki` account with `allow_local_fallback`, or a downstream
  registry provider) is left untouched, so this is a one-way door out of `local`, never a
  demotion between two external methods. `external-email` symmetrically **refuses** an
  `auth_type == local` target outright — even one carrying an identifier column, which is a
  legitimate SCIM-provisioned state (`scim_service.create_user` deliberately stamps
  `external_id` onto a `local` row without guessing an `auth_type`) that neither endpoint
  touches. The check is a plain equality against `AUTH_TYPE_LOCAL`, never a membership test
  against the valid-auth-type set — issue #866's allow-list problem gets no second instance here.
- `approval.py` — the `approval_status` state machine and `initial_approval_status`, the one
  function every account-creation path asks "does this start pending?".
- `roles.py` — the authorization contract (read this first, it's about 35 lines).
- `token_service.py` — JWT issue/verify, revocation (`revoke_token`, `revoke_all_user_tokens_in_transaction`, Redis JTI list,
  user epoch); mixes in `refresh_tokens.py` (create/verify/`rotate_refresh_token`, **session lifetime** `_session_within_lifetime`).
  `session_ceiling.py`: `_session_lifetime_minutes`, `enforce_session_ceiling`; `revocation_error.py`: `RevocationError`.
- `mfa.py` (`MFAService`) · `lockout.py` (progressive, per-identifier; store `lockout_store.py`, admin `lockout_admin.py`) · `rate_limit.py`
  (slowapi, per-IP with trusted-proxy parsing) · `session.py` (`OIDCStateStore`,
  `InMemoryStore`, `get_redis_client` — **OIDC login state only, not sessions**) ·
  `audit.py` (`AuditLogger` writer) · `audit_events.py` (`AuditEventType`, `AuditOutcome`) · `audit_query.py` (read path).
- `provider_registry.py` — **cloud-edition seam**, empty in community. `constants.py`
  carries `CLOUD_SEAM_VERSION`; bump it on any seam signature change. The import paths an
  extension may rely on are listed in `tests/fixtures/extension_surface.txt` and enforced by
  `tests/unit/test_extension_import_surface.py`: moving one needs a bump, a `moved` line and a
  row in the release's `docs/design/v<release>_downstream_changes.md`.
  ⚠️ `get_optional_current_user` (`api/endpoints/auth/dependencies.py`) must stay in step with
  `get_current_user` on the revocation check: both pass the token's `iat` as `issued_at`, which
  is what lets the per-user epoch cancel a still-valid stateless access token
  (`tests/api/test_optional_auth_revocation_epoch.py`). It now also feeds
  `GET /system/capabilities`, whose staff overlay would otherwise survive a logout-all.
  It also asks **every** account-lifecycle gate `get_current_active_user` applies (proxy
  identity, approval, expiry, banner, forced password change) through
  `lifecycle_gates.admitted_on_optional_auth`, and serves a refused account as **anonymous**
  (never a 403/500). The WebSocket handshake asks the same function and closes with 4003; the
  SPA reconnects the socket and re-reads capabilities when a banner/password hold clears.
  `tests/unit/test_optional_auth_lifecycle_gates.py::TestNoGateIsLeftOut` fails when a new
  `_enforce_*` gate is added to the strict dependency without a predicate on this path.
- `direct_auth.py`, `external_sync.py`, `cookies.py`, `password_policy.py`,
  `password_history.py`, `password_reset.py`.

## Conventions / patterns

- **`User.role` ∈ {`user`, `admin`, `super_admin`} is the SOLE authorization truth.**
  `is_superuser` is a **derived mirror** of `role == super_admin`, enforced by CHECK
  constraint `ck_user_superuser_matches_role` (migration `v369`). Never write `is_superuser`
  independently — always via `roles.role_implies_superuser()`. External IdPs may grant at
  most `admin`; `super_admin` is local-only.
- Refresh tokens **rotate on every use** via one conditional claim (`UPDATE ... WHERE revoked_at IS NULL`), so a token is spent once;
  a spent token presented again revokes its session's family (rows sharing `absolute_expires_at`; RFC 9700 §4.14.2, `auth.token.reuse`).
- **A session IS a `refresh_token` row.** Concurrent-session limits, rotation, revocation,
  the #324 fail-closed fallback and (since `v377`) idle/absolute timeouts all key off those
  rows. `session.py` used to carry a Redis `SessionManager` doing the timeout half with zero
  call sites; it was **deleted**, not wired up — two owners would enforce against different
  session sets the moment Redis and Postgres diverged, and #324 already established that
  Redis is a cache here, not the system of record. Rationale: commit
  `900cad2a6` (its cited `plans/session-ownership-decision.md` was untracked in `d8e1e4c7b`).
- TOTP per RFC 6238/4226 (Google Authenticator / Authy compatible). MFA tokens are
  single-use — the JTI is blacklisted in Redis after verification.
- Auth events go to the **audit log, which is OpenSearch-backed** (`audit.py`), not a table; reads: `audit_query.py`.
- **A resource event names its OWN tenant** (#1277): `log(..., resource_type=, resource_uuid=,
  tenant_scope="org"|"personal")` are typed top-level keywords (a pre-existing monthly index gets them
  by `put_mapping`, once per process; a conflict is a WARNING and the next month is right).
  `build_org_scope_clause`'s legacy branch (events with no `organization_id`, attributed by the actor's
  membership) refuses any event that carries `tenant_scope`, so a member's personal-file event is never
  readable by their org admins (finding F5). Content events go through
  `services/content_audit.record_content_event`, not `audit_logger.log` directly; governance events (membership,
  visibility, holds, held-file edits) through `stage_content_event` (`audit_outbox` row, v459, #1329).
- **`user_id` is the ACTOR; the subject goes in `target_user_id` / `target_username`**
  (issue #443). This is an access-control invariant, not a style rule: `query_audit_logs`
  filters on both `user_id` (actor) and `target_user_id` (subject, issue #828 — the write-side
  fix was shipped first and briefly had no read path; both `/audit-logs` endpoints now expose
  it, org-scoped identically to `user_id`) and `build_org_scope_clause` attributes un-stamped
  events to an org by member user-id, so keying it on the subject changes **who can see the
  record**. Five emitters
  of `auth.account.disabled` disagreed three ways and `admin.role.change` two, which meant
  "actions Bob performed" returned Bob's own privilege escalation while filtering by the acting
  admin missed every IdP-driven promotion. For a **system** actor — the directory-sync sweep,
  IdP reconciliation, a SCIM token — `user_id` is `None`, never the subject. The target fields
  are omitted when unset (absent, not null) so pre-#443 documents stay shape-compatible.
  `tests/unit/test_audit_actor_target.py` enforces it by AST over every administrative emitter;
  putting the subject only in `details` is what failed before, because `details` is a dynamic
  mapping no single query can search.
- **Privacy audit events (#1280):** `ORG_POLICY_CHANGE` (`org.policy.change`, organization admin PATCH; `details` is
  `{"fields": [names]}`, NEVER a value, because a forced custom word can be sensitive), and `PRIVACY_VOICEPRINT_PURGE` /
  `PRIVACY_GENDER_PURGE` (`privacy.voiceprints.purge` / `privacy.gender.purge`), one per target per phase
  (`requested`, `pass_N_completed`, `pass_N_aborted`, `pass_N_failed`, `not_queued`) with counts only. All are stamped with the
  organization (`organization_id=`); a purge of another person's data sets `target_user_id`. Instance switch changes reuse
  `ADMIN_SETTINGS_CHANGE`. Emit purge events through `services/privacy/purge.audit_purge`, not `audit_logger.log` directly.
- DB models: `UserMFA`, `PasswordHistory`, `RefreshToken` (`app/models/`).

## How it connects

- Frontend auth is **httpOnly-cookie based — there is no JS-readable token.** In-page calls
  use `fetch(..., {credentials: 'same-origin'})`.
- `GET /api/auth/session` is the SPA's session probe: **200 for anonymous, never 401**
  (a 401 there caused a spurious logout cascade). It returns `refreshable` when only a
  refresh cookie is present.

## The OIDC surface is named `oidc_*`, and a test enforces it

`KEYCLOAK_*` environment variables keep working forever (translated onto the `oidc_*` names). The naming rule, the ID-token and subject gotchas: `backend/app/auth/oidc/CLAUDE.md`.
## Gotchas

- **`cookies.py`'s `_SECURE` module constant is computed once at import time from
  `settings.is_hardened` and `settings.ALLOW_INSECURE_COOKIES`, in that order — hardened first,
  override second.** A hardened deployment (`ENVIRONMENT` unset or `production`, the default)
  sets `Secure` on every auth cookie, which a browser silently drops when sent over plain HTTP to
  anything other than `localhost`/`127.0.0.1`. That made a LAN-IP login (a homelab/small-business
  deployment with no TLS-terminating reverse proxy, e.g. `http://10.10.10.20:5173`) answer 200
  with a real `access_token` and then never actually hold a session — the next request 401s,
  which looks exactly like a wrong password and gives no hint why. `ALLOW_INSECURE_COOKIES`
  (default `false`, `core/config.py`) is the one explicit, narrow opt-out; enabling it on a
  hardened deployment logs a `logging.warning` at import time so the relaxation is visible in
  the boot log, not silent. Because it is a module-level constant, a test that needs a different
  combination must `importlib.reload(cookies)` after monkeypatching `settings` — see
  `tests/unit/test_cookie_secure_flag.py`. Self-signed HTTPS, a real reverse proxy, and future
  cloud/EC2 deployments all already work under the default and need no override — `Secure` only
  requires the `https:` scheme, not a browser-trusted CA.
  `stores/auth.ts:login()` on the frontend treats a failed `/auth/me` immediately after a 200
  `/auth/login` as this exact failure mode (the cookie never came back) and reports
  `auth.error.sessionCookieRejected` instead of a generic failure — safe to be specific there
  because that branch is only reachable after the password was already verified correct, so it
  tells a genuine operator something actionable without giving an unauthenticated attacker any
  signal they didn't already have.
- **Every token carries a `type` claim and every consumer verifies it.** Access, refresh and
  MFA tokens are signed with the same key, so `type` is the only thing separating them —
  without the check the MFA half-token (handed out BEFORE the second factor) was a full
  session, i.e. a complete MFA bypass. Never widen `get_current_user` to accept another type;
  the forced-enrolment flow uses a separate narrow dependency
  (`api/endpoints/auth/mfa_enrollment.py:get_user_for_enrollment`) scoped to `/mfa/setup` + `/mfa/verify-setup` only, and
  distinguishes an `enroll`-scoped half-token from a `verify`-scoped one.
- **"May this account use a local password?" has exactly one implementation** —
  `auth/utils.py:local_password_allowed`, keyed off `AUTH_TYPES_SUPPORT_LOCAL_FALLBACK` /
  `AUTH_TYPES_NO_LOCAL_FALLBACK`. It existed twice and the two disagreed: only the raw-SQL path
  hard-blocked LDAP, so an LDAP account with `allow_local_fallback` authenticated against a
  local hash via the ORM path. The write side is guarded too
  (`services/account_security_service.py`).
- **PKI/OIDC users bypass MFA only when they used their native method.** If they fall
  back to a local password, MFA still applies (`api/endpoints/auth/login_completion.py`, the
  `actual_auth_method` check).
  `AUTH_TYPES_SUPPORT_LOCAL_FALLBACK = [pki, oidc]`; LDAP never has a local password.
- **Changing a credential or a privilege must revoke sessions.** Go through
  `services/account_security_service.py` — it also applies the password policy and writes the
  audit event, all three of which were previously applied inconsistently across
  `users.py` / `admin.py` / `password_reset.py`. Access tokens are stateless, so revocation
  reaches them via a **per-user epoch** (`token_service.stamp_user_revocation_epoch`), not a
  JTI list.
  **The WebSocket handshake applies the same verdict** (`websockets.py` calls
  `token_service.access_token_revoked(payload, db)`, one function shared with the HTTP
  dependencies, plus the lifecycle gate `admitted_on_optional_auth`); a copy that dropped `iat`
  once let logout-all miss the handshake. Sockets already open are closed with code 4004
  (`SESSION_REVOKED_CLOSE`) when the epoch is stamped (`ConnectionManager.close_revoked`, fed by
  `publish_session_revocation`), judged by the socket token's own `iat` so host clock skew
  cannot keep one open; a single-session `/logout` closes the sockets of the token it
  blacklists (`publish_token_revocation` -> `close_token`); and every socket closes when the
  access token that opened it expires (`_receive_until`), which bounds any socket a
  best-effort broadcast missed. The token store re-probes Redis after a degraded fall-back
  (`REDIS_REPROBE_SECONDS`, at most one probe per interval); this replica keeps the database
  verdict for tokens issued before recovery (`_predates_recovery`), and the user epochs it could
  not write are replayed into Redis on recovery (`auth/revocation_backlog.py`, a Lua write that
  never lowers a newer epoch) so the other replicas refuse those tokens too. The backlog is in
  memory: a restart of the stamping process before Redis returns loses it.
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
- **Registry (external-IdP) sessions have no row, so the timeouts live elsewhere** (#1106).
  Idle is enforced by the SPA (`frontend/src/lib/auth/idleGuard.ts`, fed by
  `/auth/methods`); absolute is also enforced here by `external_session.py` on the token's
  `auth_time` (never `iat`) at all three verifier call sites, BEFORE JIT sync. No `auth_time`
  from the verifier means not enforced (one warning), not refused.
- **PKI header trust fails closed.** With `PKI_ENABLED` and no `PKI_TRUSTED_PROXIES`, header-
  sourced authentication is refused outright (and `main.py` refuses to start when hardened).
  A DN header is only trusted from a configured proxy or alongside a validated certificate —
  the proxy is what terminates mTLS and vouches for the DN.
- **`pki_mode` is `header` | `mutual_tls`, and `pki_auth.py` reads it.** It used to be
  `direct`/`broker`/`hybrid` in the schema and `header`/`mutual_tls` in the UI — no value
  could match, so **every save of the PKI tab was rejected**, and no backend code branched
  on it either way. `mutual_tls` refuses a DN-header-only assertion even from a trusted
  proxy: the certificate itself must be forwarded so this process validates it.
  `pki_support_cac` / `pki_support_piv` were deleted (v377 also drops their rows) — the CAC
  and PIV CN formats are parsed unconditionally for every certificate.
- **The login banner is enforced, not just displayed** (AC-8). `get_current_active_user`
  refuses with `detail.code == "banner_acknowledgment_required"` while
  `login_banner_enabled` and the user has no `banner_acknowledged_at` **or** theirs predates
  the last edit of `login_banner_text` (compared against that config row's `updated_at`, so
  changing the wording re-asks). `BANNER_EXEMPT_PATHS` keeps `/auth/banner`,
  `/auth/banner/acknowledge` and both logout routes reachable. Deliberately **not** audited
  per request — it would fire on every request of every pre-acknowledgment session; the
  acknowledgment itself is the audit artefact.
- **OIDC gotchas**: only the ID token authenticates a login; the ID token lives on the session row, never in a cookie; `user.oidc_subject` is a `sub` unique only per ISSUER. Full text: `backend/app/auth/oidc/CLAUDE.md`.
- **Registry-based providers and the `auth_type` CHECK (#866).** `external_sync` writes `identity.provider` into the CHECK-constrained `user.auth_type`; a provider outside `DB_ADMITTED_AUTH_TYPES` needs the registering edition to widen `ck_user_auth_type_valid` and `ck_user_invitation_auth_type_valid` in its own additive migration, and `user.auth_type` had TWO CHECK constraints. Rules and the pinning test: `backend/app/auth/notes/auth-type-checks.md`.
- **Secrets never leave the auth-config API.** `config_value` is `None` for a sensitive key and
  `is_set` carries the signal. Do not reintroduce a placeholder: returning `***REDACTED***`
  meant the admin panel bound it into the password field and the next Save encrypted it over
  the real LDAP bind password.
- **Negative login tests MUST use a nonexistent account** — never a wrong password for
  `admin@example.com`. Lockout is progressive per-account and poisons the whole suite.
- **Dev relaxes auth limits** (`docker-compose.override.yml`: `RATE_LIMIT_AUTH_PER_MINUTE`
  120, `ACCOUNT_LOCKOUT_THRESHOLD` 100, `..._DURATION_MINUTES` 1, and — since issue #632 —
  `MAX_CONCURRENT_SESSIONS` 1000 (exactly `auth_config.py`'s `le=1000` ceiling, so the relaxed
  value stays settable through the admin UI); all `DEV_*` tunable in `.env`). Prod keeps strict
  values — the override is never loaded there. Env changes need a container **recreate**, not
  `restart-backend`. `MAX_CONCURRENT_SESSIONS` is the one exception to "each var lives on the
  service that reads it": it sits on the `x-dev-environment` **anchor**, not on `backend`
  alone, because `celery-cpu-worker` runs the nightly `session.cap_sweep` (utility queue) —
  if that process and `backend` disagreed about the cap, the sweep would silently evict the
  dev stack's sessions back down to 5 every night.
- Local IdPs for testing: `--with-ldap-test` (LDAPS :6360 with a test-CA certificate the backend verifies via `ldap_ca_cert_file=/etc/opentranscribe/ldap-test-ca.crt`; plain LDAP :3890 is host-side seeding only; UI :17170, `admin`/`admin_password`),
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
  `docs/KEYCLOAK_SETUP.md` is a redirect stub — a few inbound links (`rg KEYCLOAK_SETUP`), including
  `scripts/test-all-auth.sh`, so don't delete it), and
  `docs-site/docs/authentication/{overview,ldap,oidc,saml,pki,proxy,groups}.md`.
  `docs-site/docs/authentication/keycloak.md` is likewise a stub pointing at `oidc.md`;
  Docusaurus has no client-redirects plugin here, so the stub *is* the redirect.
- **Surface that exists but has no admin UI** — say so when documenting it, don't imply a
  panel. Directory sync (six `SystemSettings` rows; `/api/admin/directory-sync`) now has one too — `DirectorySyncSettings.svelte`, the "directorySync" tab of `AuthenticationSettings` — though the sweep itself is beat-driven. IdP group mappings (`/api/admin/group-mappings`) now has one —
  `GroupMappingSettings.svelte`, the "mappings" tab of `AuthenticationSettings` — though it
  only offers `ldap`/`oidc` as the source, so a `proxy`-sourced mapping still has to be created
  through the API. `require_email_verification` also gained a control, in
  `LocalAuthSettings.svelte`.

- **Audit export** — `audit_export.py` is the one writer for `GET /admin/audit-logs/export` and
  `GET /org-admin/audit-logs/export` (columns `EXPORT_COLUMNS`, formula-neutral CSV via `utils/csv_safe`,
  `X-Export-Truncated`, filename from a fixed prefix and the date). Both read through `query_audit_logs`,
  capped at `AUDIT_EXPORT_MAX_ROWS` (OpenSearch's `max_result_window`), and both write `audit.export`.
  Don't add a third hand-rolled export.
