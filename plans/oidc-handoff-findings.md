# Handoff — OIDC / OAuth findings for a fresh plan

**Purpose.** Everything I know about the OIDC/OAuth surface, written for an agent who will
research it independently and produce a full implementation plan. **Verify all of it.** I mark
below what I confirmed by reading code versus what I inferred, because some of my earlier
conclusions in this area turned out to be wrong and were corrected.

**Scope boundary.** The `security/auth-identity-overhaul` branch already landed generic OIDC
discovery (issue #353). That work is done and should be treated as the *starting state*, not as
something to redo. The new plan covers what remains.

---

## 1. Where this came from

Politiezone MIDOW (a Belgian police zone, Authentik + LDAP, behind a Zoraxy reverse proxy) filed
issue #353: *"Generic OIDC / Authentik does not work because OpenTranscribe hardcodes Keycloak
realm URLs."* They then asked directly:

> "OIDC appears to be hardcoded to use Keycloak. Is this intentional, or is it supposed to use
> the standard OpenID service instead?"

That question is the brief. The honest answer is *partly yes* — and the part that is still true
is the naming and the protocol gaps below.

Two things from their follow-up that correct my earlier reporting:

- The `/flower/` and `/docs/` 404s they reported were **their own reverse proxy** (a virtual
  directory pointing at the wrong port), not our bug. Do not carry the earlier "real bug, fixed"
  framing forward.
- They **declined a hotfix** and have blocked `/api/auth/register` at the proxy as a workaround.
  Nothing is blocking them, so this work can be done properly rather than quickly.

---

## 2. What the security branch already fixed (starting state — verified)

In `backend/app/auth/oidc_discovery.py` (new) and `backend/app/auth/keycloak_auth.py`:

- **Discovery**: `keycloak_discovery_url` (env aliases `OIDC_DISCOVERY_URL` / `KEYCLOAK_DISCOVERY_URL`).
  When set, `authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`, `jwks_uri`,
  `end_session_endpoint` and `issuer` all come from the provider's metadata. The realm-based
  construction remains the fallback and is pinned by a test asserting the exact literal URLs.
- **The public/internal URL split is preserved in both branches** and is load-bearing: the
  authorization endpoint must stay browser-reachable while token/JWKS/logout go over the Docker
  network when `keycloak_internal_url` is set. `to_internal()` handles the discovered case.
- **ID token validated in preference to the access token.** Keycloak issues JWT access tokens;
  OIDC only guarantees that of the ID token. This was the second reason non-Keycloak providers
  failed.
- **Configurable roles claim** — `keycloak_roles_claim`, a dotted path, default
  `realm_access.roles`. Authentik/Okta use `groups`, Entra uses `roles`.
- **Configurable scopes** — `keycloak_scopes`, default `openid email profile`.
- TTL-cached discovery documents and JWKS (only successes cached). This also removed a full JWKS
  refetch on *every* token validation.
- `keycloak_verify_audience` defaulted to `False` in three places in `keycloak_auth.py` while
  `core/config.py` said `True`; aligned to `True`.
- The admin "Test connection" button now resolves the metadata URL the way the login path does,
  and gained an SSRF guard it never had.

---

## 3. Confirmed remaining gaps

I verified each of these by reading the code in the branch. Re-confirm before planning around
them — line numbers will drift.

### 3a. Protocol conformance

| Gap | Where | Consequence |
|---|---|---|
| `algorithms=["RS256"]` hardcoded | `keycloak_auth.py:582` | a provider signing ID tokens with ES256/PS256 fails outright. `id_token_signing_alg_values_supported` is in every discovery document and is ignored. |
| No `nonce` generated, sent, or validated | grep found zero occurrences | OIDC Core §3.1.2.1 says SHOULD for the code flow. It is the defence against ID-token replay/injection. We do have PKCE and `state`, which cover adjacent but not identical attacks. |
| No `at_hash` validation | — | OIDC Core §3.1.3.6. Optional for code flow, but it is the binding between the ID token and the access token. |
| Token endpoint auth method fixed | `exchange_code_for_tokens` posts `client_secret` in the body | that is `client_secret_post`. Providers configured for `client_secret_basic` or `private_key_jwt` cannot connect. `token_endpoint_auth_methods_supported` is in the discovery document. |
| No public-client support | client secret is effectively required | PKCE-only public clients are the modern default. Also **there is no way to clear a stored secret** from the UI — the backend treats `""` as "no change" by design (`SENSITIVE_NO_CHANGE_VALUES`), so migrating confidential → public is impossible without direct DB access. |
| Single provider only | `keycloak_enabled` is one boolean; `KeycloakConfig` is one object | a deployment cannot offer Entra **and** Okta, or migrate from one to the other with overlap. |

### 3b. Naming — this is what Max actually noticed

Counted, not estimated: **87** `keycloak` references in `backend/app/auth/keycloak_auth.py`,
**30** in `backend/app/services/auth_config_service.py`, **9** in `backend/app/core/config.py`,
plus the `keycloak_*` config-key namespace, the `KeycloakSettings.svelte` panel, the
`settings.keycloak.*` i18n namespace across 8 locales, `frontend/src/lib/api/authConfig.ts`, the
`AUTH_TYPE_KEYCLOAK` user `auth_type` value, and `user.keycloak_id` / `user.keycloak_refresh_token`
columns.

An Authentik administrator configures this product by typing values into fields named
`keycloak_*`. Discovery working does not fix that impression.

**The naming decision was explicitly deferred to this plan.** Options previously sketched, for
you to evaluate rather than inherit:
1. Rename to `oidc_*` with `keycloak_*` accepted as aliases (no migration; permanent alias layer —
   note the repo's CLAUDE.md says "if you replace an implementation, delete the old one").
2. Rename with an alembic migration and no aliases (clean, breaks `.env`-configured deployments).
3. Behaviour-only fixes, keep the names (least churn, does not answer the reported confusion).

Consider also whether `user.auth_type` and the `keycloak_id` / `keycloak_refresh_token` **columns**
are in scope — they are the part that requires a migration and touches the JIT-provisioning path.

### 3c. Group and claim mapping

From the earlier audit (verified then; re-verify): LDAP captures the full group list into
`LdapUserData.groups` and **discards it**; Keycloak does the same with `roles`. Only a single
`is_admin` boolean reaches the database. `UserGroup`/`UserGroupMember` — the app's sharing groups —
are referenced by **no** auth code at all.

So `CN=Legal-Team` cannot become an OpenTranscribe sharing group; teams are rebuilt by hand and
drift immediately, and nothing revokes membership when the directory group changes. A proper OIDC
implementation usually offers claim → role mapping *and* claim → group mapping.

### 3d. Documentation

`docs-site/docs/authentication/keycloak.md` has been corrected and gained an Authentik worked
example. Still outstanding: `docs/KEYCLOAK_SETUP.md` (repo root, a separate document) very likely
repeats the Keycloak-only assumptions, and `docs/SEARCH_ARCHITECTURE.md` is unrelated but was
found stale in the same sweep. Provider guides for Okta, Entra ID and Google do not exist.

---

## 4. Constraints the plan must respect

- **Backwards compatibility is non-negotiable for existing Keycloak deployments.** There is a test
  asserting the exact literal URLs the realm-based builder produces, and that no discovery request
  is made when no discovery URL is set. Keep it passing.
- **The public/internal URL split** (§2) breaks Docker-network deployments if flattened.
- **External IdPs grant at most `admin`.** `super_admin` is local-only by design — see
  `backend/app/auth/CLAUDE.md`. Group mapping must not become a path to `super_admin`.
- **JIT provisioning links by email**, and the cloud seam refuses that unless the IdP asserts
  `email_verified` (`backend/app/auth/constants.py`). The core LDAP/PKI paths do **not** yet apply
  that guard — it is an open finding from the audit, and group mapping makes it more dangerous.
- **Sequencing**: `feat/rag-chat` merges to master first, then `security/auth-identity-overhaul`
  rebases onto it (renumbering its alembic `v375` → `v376`; both branches currently define `v375`).
  This new OIDC work is **third**, off the resulting master.
- The repo's own conventions in `CLAUDE.md`, `backend/app/auth/CLAUDE.md` and
  `backend/alembic/CLAUDE.md` — idempotent migrations, one implementation per rule, files under
  ~300 lines, no vendor nouns in core.

---

## 5. What I am *not* confident about

Stated plainly so you re-derive rather than inherit:

- Whether `nonce` is genuinely required or merely advisable given we already enforce PKCE and
  single-use `state`. I believe it is worth adding; I have not done the threat analysis.
- Whether multi-provider support is wanted at all, or whether one-provider-at-a-time is a
  deliberate product decision. Nobody has asked for it — I inferred the gap from the code shape.
- Whether the `keycloak_id` / `keycloak_refresh_token` **columns** should be renamed. That is a
  migration touching the JIT path, and the cost may exceed the benefit.
- Whether group → sharing-group mapping belongs in this issue or its own. It is the largest single
  item here and is arguably a separate feature.

---

## 6. Suggested deliverable

A GitHub issue (draft first, do not post — outward-facing) plus a full implementation plan:
scope, phases, the naming decision with its reasoning, the migration story, a provider-conformance
matrix (Keycloak / Authentik / Okta / Entra ID / Google), the test strategy, and what is
explicitly out of scope.

Useful reading, in order: `backend/app/auth/oidc_discovery.py`, `backend/app/auth/keycloak_auth.py`,
`backend/app/auth/CLAUDE.md`, `backend/app/api/endpoints/auth/keycloak.py`,
`backend/app/services/auth_config_service.py`, `backend/app/schemas/auth_config.py`,
`frontend/src/components/settings/KeycloakSettings.svelte`, and issue #353 itself
(`gh issue view 353`).
