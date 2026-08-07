# Standards-complete OpenID Connect — implementation plan

**Status:** plan only. No source file was modified while writing it.
**Base:** `security/auth-identity-overhaul` (which already landed generic OIDC discovery, #353).
**Sequence:** third in the queue — `feat/rag-chat` → `security/auth-identity-overhaul` → this, off the
resulting master.
**Supersedes:** `plans/oidc-handoff-findings.md` (kept for provenance; its §3 line numbers and counts
are corrected below).

---

## 1. Context

Politiezone MIDOW (Authentik + LDAP behind a Zoraxy proxy) filed [#353] — "Generic OIDC / Authentik
does not work because OpenTranscribe hardcodes Keycloak realm URLs" — and then asked the question
that defines this work:

> "OIDC appears to be hardcoded to use Keycloak. Is this intentional, or is it supposed to use the
> standard OpenID service instead?"

They are **not blocked** (they declined a hotfix and blocked `/api/auth/register` at their proxy), so
this can be done properly.

The product claims support for "Keycloak or any OpenID Connect provider". The discovery work on the
security branch made that *possible*. It did not make it *true*, and it did not make it *verifiable*:

- Every configuration field an Authentik administrator types into is named `keycloak_*`.
- The token validator hardcodes `algorithms=["RS256"]`. Authentik advertises **exactly one** signing
  algorithm and it is **ES256 whenever the provider's signing key is an EC key** — so the reporter's
  own product can produce tokens this RP cannot verify.
- Federated logout POSTs `client_id` + `client_secret` + `refresh_token` to `end_session_endpoint`.
  That is a **Keycloak-proprietary legacy message** that works on exactly one of the five providers
  in the matrix, and Keycloak's own documentation says not to use it.
- Separately, and not previously reported: the OIDC `state` is **not bound to the initiating
  browser**, which makes the callback vulnerable to login-CSRF (§2.3), and JIT provisioning links an
  IdP identity to an existing local account **by email with no verification check** (§2.4).

Two of those are security defects that exist today regardless of provider. They set the phase order.

[#353]: https://github.com/attevon-llc/OpenTranscribe/issues/353

---

## 2. Findings

All citations are `path:line` against the worktree at
`.claude/worktrees/authoverhaul` (branch `security/auth-identity-overhaul`).

### 2.1 Handoff §3a claims — verified, corrected, or dismissed

| Handoff claim | Verdict | Evidence |
|---|---|---|
| `algorithms=["RS256"]` hardcoded at `keycloak_auth.py:582` | **CONFIRMED, line exact, severity understated** | `backend/app/auth/keycloak_auth.py:582`. The handoff called it "a provider signing with ES256/PS256 fails outright". It is worse than hypothetical: Authentik derives the alg from the key type (EC P-256 → ES256) and advertises only that one value. The reporter's provider is the failure case. |
| No `nonce` generated, sent, or validated | **CONFIRMED (zero occurrences)** | `rg nonce backend/app frontend/src` returns only `utils/encryption.py` (AES-GCM nonce) and one French i18n string. **But the handoff's framing is wrong** — see §2.2. |
| No `at_hash` validation | **CONFIRMED, and correctly described as low priority** | No occurrence anywhere in `backend/app`. |
| Token endpoint auth method fixed to `client_secret_post` | **CONFIRMED, severity understated** | `backend/app/auth/keycloak_auth.py:418-424` — `client_secret` in the POST body, unconditionally. RFC 6749 §2.3.1 calls this "**NOT RECOMMENDED** and SHOULD be limited to clients unable to directly utilize the HTTP Basic authentication scheme", and `client_secret_basic` is the **default** an OP is entitled to assume when `token_endpoint_auth_methods_supported` is absent (OIDC Core §9, Discovery §3, RFC 8414 §2, Registration §2 — all four say so). We rely on universal vendor leniency, not on the spec. *Mitigating:* all five providers in the matrix do accept `client_secret_post`, so this is a conformance debt, not a live outage. |
| No public-client support; no way to clear a stored secret | **CONFIRMED, two independent bugs** | (a) `keycloak_auth.py:422` sends `client_secret=""` rather than omitting the parameter — several OPs reject an empty credential. (b) `services/auth_config_service.py:54-56` puts `""` in `SENSITIVE_NO_CHANGE_VALUES` and `:633-636` skips the write, so a stored secret is **unclearable** through the API. Note (b) is deliberate and correct for the "leave blank to keep current" UX — the fix is an explicit clear action, not removing `""` from the set. |
| Single provider only | **CONFIRMED as a fact, DISMISSED as a gap for this plan** | `KeycloakConfig` is one frozen dataclass; `keycloak_enabled` is one boolean. See §4 — recommended out of scope. |

### 2.2 The `nonce` question — threat analysis and conclusion

The handoff author said "I believe it is worth adding; I have not done the threat analysis." Here it
is, and the conclusion is more nuanced than "add it".

**Normative level.** OIDC Core §3.1.2.1 lists `nonce` as **OPTIONAL** for `response_type=code`. It is
REQUIRED only for the implicit flow (§3.2.2.1) and for hybrid responses containing `id_token`
(§3.3.2.1). The validation rule in §3.1.3.7 item 11 is conditional — "*If* a `nonce` value was sent
… a `nonce` Claim MUST be present". An RP that never sends one is **literally conformant** for the
code flow. (`state` itself is only RECOMMENDED, not REQUIRED, in §3.1.2.1.)

**PKCE and nonce are alternatives, not layers.** RFC 9700 §4.5.3 presents them as "two good technical
solutions" to the *same* problem (authorization-code injection), and §2.1.1 says clients must prevent
it using **"one of the following options"**. OAuth 2.1 draft-13 §7.5.2 states plainly that PKCE is the
stronger of the two, because the AS can refuse pre-emptively where nonce only lets the client reject
afterwards. **FAPI 2.0 §5.3.3.2 requires PKCE S256 and the RFC 9207 `iss` check, and does not require
nonce at all** — that is the strongest available evidence against treating nonce as mandatory.

**What nonce still buys us here.** PKCE binds the *authorization code* to our verifier. It does not
formally bind the *ID Token* to the browser session. In a pure code flow over TLS the two collapse —
Core §3.3.3.6 says as much when explaining why `at_hash` may be omitted from token-endpoint ID Tokens
("already cryptographically bound together by the TLS encryption performed by the Token Endpoint").
So the residual cryptographic gain is small.

**Conclusion — implement it, as a SHOULD, and do not sell it as the security fix.**
1. It is cheap: one CSPRNG value stored beside the existing `code_verifier` in `OIDCStateStore`, one
   equality check.
2. It is an **interop** requirement in practice. Google's own OIDC guide lists `nonce` as *Required*
   (its API reference says "required only if requesting an ID Token", which for `scope=openid` is
   always); HashiCorp Vault's OP, Gluu oxAuth and OpenIddict reject nonce-less code-flow requests
   outright. Zero downside against permissive OPs.
3. **It is not what is actually broken in this codebase.** The gap the handoff was groping towards is
   §2.3 below — the `state` is unguessable and single-use but *not bound to a browser*, and nonce
   does not fix that either. If only one of the two is built, build the state binding.
4. If implemented, RFC 9700 §4.5.3.2 imposes two MUSTs that are commonly missed: validate the nonce
   in the ID Token from the **token endpoint**, and treat **all** tokens as unusable until that check
   passes.

### 2.3 NEW — login CSRF: `state` is not bound to the initiating browser

Not in the handoff. This is the most serious finding in the report.

- `backend/app/api/endpoints/auth/keycloak.py:73-92` mints `state = secrets.token_urlsafe(32)` and
  stores `{code_verifier}` under it in Redis. **No cookie is set.**
- `backend/app/api/endpoints/auth/keycloak.py:135` retrieves it with
  `_oidc_state_store.get_state(state)` — keyed on the state value alone
  (`backend/app/auth/session.py:361-382`). Any browser presenting a valid state wins.
- `backend/app/middleware/csrf.py:38` exempts the whole `/api/auth/keycloak/` prefix.
- `frontend/src/routes/login/+page.svelte:99-137` auto-processes `?code=&state=` on page load.

Attack: the attacker starts a login at the victim's OpenTranscribe (learning `state` S), authenticates
at the IdP as *themselves* (obtaining code C), then induces the victim's browser to load
`https://ot.example/login?code=C&state=S`. The SPA calls the callback, the backend exchanges C, and
sets httpOnly session cookies for the **attacker's** account in the **victim's** browser. The victim
then uploads recordings into an account the attacker controls.

`state` is supposed to prevent exactly this, and it does not, because it is global server-side state
rather than per-browser state. OIDC Core §15.5.2 spells out the correct construction — "store a
cryptographically random value as an HttpOnly session cookie and use a cryptographic hash of the
value as the `nonce` parameter" — and RFC 9700 §2.1.1 requires the challenge to be "securely bound to
the client **and the user agent** in which the transaction was started". PKCE does not help: the
verifier lives server-side, keyed by the same unbound state.

This is aggravated by the SPA-mediated callback (`KEYCLOAK_CALLBACK_URL` points at the frontend
`/login`, not the backend), which is why the fix is a cookie rather than a redirect-URI change — see
§7 for why moving the callback is out of scope.

### 2.4 NEW — JIT account linking by unverified email

`backend/app/auth/keycloak_auth.py:894-906`: identity resolution is `keycloak_id` first, then
**`User.email` with no `email_verified` check**, and a matching `auth_type == 'local'` row is
converted to Keycloak auth with its password cleared (`_convert_local_user_to_keycloak`, `:845-879`).

The cloud seam already refuses this — `backend/app/auth/external_sync.py:84` requires
`identity.email_verified`, documented at `backend/app/auth/provider_registry.py:39` — but the core
Keycloak path does not go through `external_sync`. The handoff listed this as "an open finding from
the audit"; it is confirmed and it belongs in this issue because the provider matrix makes it acute:

- **Authentik hardcodes `email_verified: false`** as of 2025.10 (it has no authoritative source), so a
  naive "require `email_verified`" would lock out the very reporter who filed #353.
- **Entra ID does not emit `email_verified` at all** — it is absent from `claims_supported`, and
  Microsoft documents `email` itself as "not guaranteed to be correct and is mutable".
- **Okta** emits it but tells ISVs not to rely on it, and under the code flow it moves out of the ID
  token into `/userinfo`.
- **Google** is authoritative only when the address is `@gmail.com`, or `email_verified` is true *and*
  `hd` is present.

So the fix cannot be a single boolean. It has to be an explicit, admin-chosen linking policy (§5,
Phase 1c).

### 2.5 NEW — the access-token fallback is a silent downgrade path

`backend/app/auth/keycloak_auth.py:654-662` loops `for token in (id_token, access_token)` and accepts
the first that validates. The branch introduced this as backwards compatibility, and the intent was
right, but the shape is wrong:

- If the **ID token fails validation** (bad audience, wrong alg, expired), the loop silently falls
  through to the access token. That is an attacker-influenceable downgrade, not a fallback.
- RFC 9068 §6 is explicit: "**The client MUST NOT inspect the content of the access token** … The
  OAuth 2.0 framework assumes that access tokens are treated as opaque by clients."
- The `aud` semantics differ: an ID Token's `aud` is our `client_id`; an access token's `aud` is a
  resource indicator. Accepting one as the other can admit a token carrying **no authentication
  semantics at all** (a client-credentials token has `sub` = the client).
- It is textbook cross-JWT confusion (RFC 8725 §2.8, §3.12); RFC 9068 §5 exists specifically to let
  consumers tell the two apart via `typ: at+jwt`.
- Empirically it only ever worked on Keycloak: Okta org-AS, Google and Entra all issue access tokens
  the RP cannot validate, and Microsoft says so in as many words — "client applications … should
  treat them as opaque strings. The client application shouldn't attempt to validate access tokens."

### 2.6 NEW — federated logout is Keycloak-proprietary

`backend/app/auth/keycloak_auth.py:488-492` POSTs `client_id` + `client_secret` + `refresh_token` to
`end_session_endpoint`. OIDC RP-Initiated Logout 1.0 defines a **front-channel redirect** with
`id_token_hint` / `post_logout_redirect_uri` / `client_id` / `state` / `logout_hint` / `ui_locales`.
The spec has **no `refresh_token` and no `client_secret` parameter**. Keycloak's own documentation
calls the form we use "a **non-standard legacy format** … supported only because of the legacy
Keycloak OIDC Java adapters … not recommended to use it directly from your applications".

Verified support: **Keycloak only**. Authentik's `EndSessionView` never reads a `refresh_token`
parameter; Okta uses `GET /oauth2/v1/logout?id_token_hint=…`; Entra's end-session takes only
`post_logout_redirect_uri` + `logout_hint`; **Google publishes no `end_session_endpoint` at all**.
The graceful-degradation comment at `keycloak_auth.py:483-486` means this fails quietly today rather
than breaking login — so it is a correctness/expectation bug, not an outage.

### 2.7 NEW — `.env.example` ships audience validation OFF

`.env.example:1223` is `KEYCLOAK_VERIFY_AUDIENCE=false`, contradicting `core/config.py:689`
(`"true"` default), `schemas/auth_config.py:177` (`True`) and `keycloak_auth.py:95` (`True`). The
handoff §2 said this was "aligned to `True`" — it was aligned in the *code*, and the template was
missed. Any deployment seeded from `.env.example` explicitly disables the control that stops a token
minted for a different client of the same IdP being accepted here. One-line fix, but it silently
defeats the branch's own hardening.

### 2.8 Other verified ID-token validation gaps

`_decode_token` (`keycloak_auth.py:558-589`) passes `verify_aud` / `verify_iss` to python-jose and
nothing else. Against the OIDC Core §3.1.3.7 checklist:

| Rule | Status |
|---|---|
| §3.1.3.7 #3 — reject if `aud` "contains additional audiences not trusted by the Client" | **Missing.** python-jose's `audience=` implements only "our id is present". Directly relevant to Keycloak, whose default `roles` scope routinely produces `aud: account` without our own client id. |
| §3.1.3.7 #4/#5 — verify `azp == client_id` when present | **Missing.** Google emits `azp` routinely; it is load-bearing when `aud` is multi-valued. |
| §3.1.3.7 #9/#10 — `exp`, and an `iat` freshness window | `exp` is checked by jose; **no `iat` window**, and **no leeway configured anywhere** (`rg leeway backend/app/auth` → nothing). Core specifies no leeway; RFC 9068 §4 is the precedent for "no more than a few minutes". |
| §16.15 — issuer compared **exactly**, path significant | Exact compare is correct per spec, but breaks two real providers: Entra's `common`/`organizations` metadata returns the *literal* `https://login.microsoftonline.com/{tenantid}/v2.0`, and Google documents **two** valid issuers (`https://accounts.google.com` **or** `accounts.google.com`). `keycloak_issuer` is a single-value override — it cannot express either. |
| `algorithms=` allow-list | Present but hardcoded. **Do not "fix" this by passing the discovery list** — Discovery §3 explicitly permits `none` in `id_token_signing_alg_values_supported`, and OIDC Core §3.1.3.7 #8 makes `HS*` forgeable by anyone holding the client secret (RFC 8725 §2.1). |

### 2.9 Handoff §3b — naming: counts corrected

The handoff's counts are stale (they were line counts or a narrower pattern). Current occurrence
counts, case-insensitive:

| File | Occurrences |
|---|---|
| `backend/app/auth/keycloak_auth.py` (911 lines) | **231** |
| `frontend/src/components/settings/KeycloakSettings.svelte` (634 lines) | **160** |
| `backend/app/core/config.py` | **67** |
| `backend/app/services/auth_config_service.py` | **53** |
| `backend/app/schemas/auth_config.py` | **31** |
| each of 8 i18n locale files | **66–67** |

Plus: `AUTH_TYPE_KEYCLOAK` (`backend/app/auth/constants.py:11`, in `VALID_AUTH_TYPES` and
`AUTH_TYPES_SUPPORT_LOCAL_FALLBACK:40`), `user.keycloak_id` / `user.keycloak_refresh_token`
(`backend/app/models/user.py:71,80`), the DB CHECK `ck_user_auth_type_valid`
(`backend/alembic/versions/v375_harden_user_auth_invariants.py:106`), the `/api/auth/keycloak/*`
routes, `AuthMethodsResponse.keycloak_enabled` (`backend/app/api/endpoints/auth/methods.py`),
`frontend/src/lib/api/authConfig.ts:66-92`, `frontend/src/stores/auth.ts:46,61,493,539`,
`frontend/src/routes/login/+page.svelte:738` ("Sign in with Keycloak"), and
`frontend/src/lib/search/settingsSearchIndex.ts:48`.

Note also that `keycloak_auth.py` at **911 lines** is 3× the repo's ~300-line ceiling
(root `CLAUDE.md`, "Conventions"), which is an independent reason to touch it.

### 2.10 Handoff §3c — group/claim mapping: verified

`rg -l UserGroup backend/app` returns `models/group.py`, `models/sharing.py`, `models/user.py`,
`models/__init__.py`, `api/endpoints/groups.py`, `api/endpoints/media_collections.py`,
`services/permission_service.py` — and **nothing under `backend/app/auth/`**. Confirmed: the app's
sharing groups are untouched by any auth code. LDAP captures `LdapUserData.groups`
(`backend/app/auth/ldap_auth.py:154`) and uses it only for admission/admin decisions; the OIDC path
does the same with `roles` (`keycloak_auth.py:668-671`). Only `is_admin` reaches the database.

Recommendation on placement: **its own issue** — see §7.

### 2.11 Handoff §3d — documentation: verified

- `docs-site/docs/authentication/keycloak.md` **has** been corrected on the branch and carries a
  provider table plus an Authentik worked example (lines 140-188). Good.
- `docs/KEYCLOAK_SETUP.md` (384 lines, repo root) was **not touched by the branch** and is
  Keycloak-only end to end: no discovery-URL section, no roles-claim guidance, no non-Keycloak
  provider anywhere, and it is still the target of the `.env.example:1168` and root `CLAUDE.md`
  pointers.
- `docs-site/docs/configuration/environment-variables.md:439-443` documents five `KEYCLOAK_*` vars
  and **no** `OIDC_DISCOVERY_URL` / `OIDC_ISSUER` / `KEYCLOAK_ROLES_CLAIM` / `KEYCLOAK_SCOPES`.
- No provider guides exist for Okta, Entra ID or Google.

### 2.12 What I could not verify

- **Whether the login-CSRF path is exploitable end-to-end in a live stack.** I did not run the stack
  (the task forbade it). The reasoning is from code alone; the session-storage guard at
  `+page.svelte:107-114` is keyed on the state value and would not stop a first-time attack, but a
  live proof-of-concept should be the first task of Phase 1.
- **Whether any existing deployment relies on the access-token fallback** (i.e. runs with `openid`
  absent from `keycloak_scopes`). The default includes it; a DB override could remove it. Phase 1
  mitigates by forcing `openid` into the scope set rather than by assuming.
- **Google's actual behaviour on a nonce-less code-flow request.** Google's guide says Required, its
  API reference says conditionally optional; nobody documents the enforcement. Sending one always is
  the safe read.
- **Okta's literal discovery arrays.** Okta's API reference is client-side rendered and could not be
  fetched; the Okta column in §6 is assembled from prose documentation.
- **Auth0 / "Okta Customer Identity Cloud"** is a different product from Okta Workforce Identity and
  was not researched. Do not infer its behaviour from the Okta column.

---

## 3. The naming recommendation

**Recommendation: Option 1, modified — rename the whole surface to `oidc_*`, and confine backwards
compatibility to two places that are *adapters*, not alternative implementations.**

### The decision

| | Recommendation |
|---|---|
| DB `auth_config.config_key` values | **Rename** `keycloak_*` → `oidc_*` by an idempotent Alembic **data** migration. Afterwards exactly one namespace exists in the database. |
| `.env` variables | **`KEYCLOAK_*` keeps working forever**, translated by the existing `AuthConfigService.ENV_TO_CONFIG_MAPPING` (`backend/app/services/auth_config_service.py:146-240`). New canonical spelling is `OIDC_*`. A deprecation warning is logged once at startup. |
| Pydantic schema, service, auth module, API DTO, frontend panel, i18n | **Rename, no aliases.** |
| API routes `/api/auth/keycloak/*` | **Rename** to `/api/auth/oidc/*`. Verified sole consumer is our own SPA (`frontend/src/stores/auth.ts:493,539`) — the IdP redirect URI points at the frontend `/login`, not at these paths, so **no IdP reconfiguration is required**. |
| `AuthMethodsResponse` | Emit `oidc_enabled`; keep `keycloak_enabled` as a duplicated field for **one minor release** with a `# deprecated` comment and a removal ticket, because a browser holding a cached SPA bundle against a freshly upgraded backend is a real deployment state. |
| `user.auth_type == 'keycloak'`, `user.keycloak_id`, `user.keycloak_refresh_token` | **In scope, but a separate, later phase** (§5 Phase 4). See below. |

### Why this and not the alternatives

**Against Option 3 (behaviour-only).** It does not answer the question that was asked. The reporter's
complaint was not "discovery is broken" — that is #353 and it is fixed. It was "this is hardcoded to
Keycloak", and the evidence they see is a settings panel of `keycloak_*` fields.

**Against Option 2 (rename, no aliases at all).** `.env` is user-owned and the repo's own rule is that
it "is never overwritten without confirmation" (root `CLAUDE.md`). Breaking `KEYCLOAK_ENABLED` would
silently disable SSO on upgrade for every existing deployment — the exact failure mode the constraints
forbid.

**On the CLAUDE.md rule** ("if you replace an implementation, **delete the old one** — never leave two
paths doing the same job"). That rule targets duplicated *implementations*. An env-name→config-key
entry in a table that already performs precisely this translation (it already maps
`ALLOW_OPEN_REGISTRATION` → `allow_registration` and `OIDC_DISCOVERY_URL` →
`keycloak_discovery_url`) is an **input adapter**, not a second implementation. To keep that
distinction honest rather than rhetorical, the plan enforces it structurally:

> **Invariant (test-enforced):** after Phase 2, the string `keycloak` may appear in
> `backend/app/` in exactly two files — `services/auth_config_service.py` (the env ingestion map) and
> the Alembic revision that performs the data migration. Every other occurrence is a build failure.
> A unit test greps for it, in the same spirit as the existing CI seam-guard for `clerk|stripe`.

That is a stronger guarantee than "we renamed things", and it is what makes the claim *verifiable*.

### The migration story

**Config keys (Phase 2).** One revision, `v37N_rename_keycloak_config_to_oidc`, following the guarded
shape of `v371`:

```
UPDATE auth_config SET config_key = 'oidc_' || substring(config_key from 10),
                       category   = 'oidc'
 WHERE config_key LIKE 'keycloak\_%'
   AND NOT EXISTS (SELECT 1 FROM auth_config a2
                    WHERE a2.config_key = 'oidc_' || substring(auth_config.config_key from 10));
DELETE FROM auth_config WHERE config_key LIKE 'keycloak\_%';   -- only rows the UPDATE skipped
UPDATE auth_config_audit SET config_key = 'oidc_' || substring(config_key from 10)
 WHERE config_key LIKE 'keycloak\_%';
```

- `config_key` is globally UNIQUE, hence the `NOT EXISTS` guard: re-running must not raise, and a DB
  that already has both (impossible today, but cheap to defend) keeps the new row.
- **Encrypted values are carried across unchanged** — `keycloak_client_secret` is stored as
  ciphertext under `ENCRYPTION_KEY` and the rename must not decrypt/re-encrypt it. `is_sensitive`
  stays `true`; `AuthConfigService.SENSITIVE_KEYS` gains `oidc_client_secret` and loses the old name.
- The audit table is renamed too, or `GET /audit/oidc` returns nothing for pre-migration history
  (`get_audit_log` filters `config_key IN CONFIG_CATEGORIES[category]`,
  `auth_config_service.py:746-752`).
- `downgrade()` mirrors it.
- Add the detection arm in `_detect_schema_version()` (`backend/app/db/migrations.py`), keyed on
  `EXISTS (SELECT 1 FROM auth_config WHERE config_key = 'oidc_enabled')` — mandatory per
  `backend/alembic/CLAUDE.md`, or untracked production DBs are mis-stamped and never get the DDL.

**Identity columns and `auth_type` (Phase 4).** I recommend doing this, but *separately and last*, and
I am explicit that it is the part to cut if the work has to be trimmed:

- **`auth_type = 'keycloak'` → `'oidc'`: do it.** It is user-visible in the admin Users table and in
  `frontend/src/stores/auth.ts:46`. One revision: drop `ck_user_auth_type_valid`, `UPDATE "user" SET
  auth_type='oidc' WHERE auth_type='keycloak'`, re-add the CHECK with the new value set. Must be a
  single transaction — a half-applied state either locks out every OIDC user
  (`local_password_allowed` in `backend/app/auth/utils.py` keys off `AUTH_TYPES_*`) or, worse, exempts
  them from MFA (`api/endpoints/auth/mfa_tokens.py` treats an unrecognised `auth_type` specially —
  the exact hazard `v375` was written to close).
- **`keycloak_id` → `oidc_subject`: do it, in the same revision.** Not for cosmetics — the current
  name asserts a global identifier, but the value is an OIDC `sub`, which is only unique **per
  issuer**. Renaming it makes the eventual `(iss, sub)` key obvious instead of a trap, and it is a
  guarded `ALTER TABLE … RENAME COLUMN` with the `v371` shape.
- **`keycloak_refresh_token` → `oidc_refresh_token`: do it, same revision.** Trivial rider.
- **Cost to acknowledge:** this touches the JIT provisioning path, `backend/app/auth/external_sync.py`
  (cardinality mapping at `:122`), and therefore `CLOUD_SEAM_VERSION` in
  `backend/app/auth/constants.py` — **bump it**, and coordinate with the pinned submodule in the
  private cloud repo before merging.

---

## 4. Multi-provider — recommended **out of scope**

Nobody has asked for it; the handoff inferred it from code shape and said so. It is a large change
(a providers table, `(issuer, subject)` identity keying, N login buttons, per-provider secrets, an
admin list UI) and there is no evidence of demand. **Ship one provider, done properly.**

Two cheap pieces of groundwork are in scope, because they are correct on their own merits and because
they are what makes multi-provider *possible* later without a second rename:

1. **Bind the provider identity and its expected issuer into the `state` record** (Phase 1a). Even
   single-provider, this means the callback validates against the issuer that *started this
   transaction* rather than "whatever is configured now" — which closes the window where an admin
   changes the IdP mid-flight. RFC 9700 §4.4.2: "When an OAuth client can only interact with one
   authorization server, a mix-up defense is not required" — so RFC 9207 `iss` validation is
   **correctly deferred**; note it in the follow-up issue as the thing that becomes a MUST the moment
   a second provider is supported.
2. **Name everything `oidc_*`, not `authentik_*` or `provider1_*`** (Phase 2), so adding providers
   later is a container change, not another rename.

File a follow-up issue: *"Support more than one OIDC provider simultaneously"*, referencing RFC 9207
and RFC 9700 §4.4.2 as the security work it drags in.

---

## 5. Phased implementation plan

Each phase is independently shippable and independently revertible. Phase 1 is security and goes
first; Phase 2 is a mechanical rename with **no behaviour change**, deliberately placed before the
protocol work so that work lands once, in correctly-named and correctly-sized modules.

### Phase 1 — Fail-closed security fixes (no config-surface rename)

Smallest possible diff; back-portable to a patch release if needed.

**1a. Bind the OIDC transaction to the browser.** *(fixes §2.3)*
- `backend/app/api/endpoints/auth/keycloak.py` — on `/login`, set a short-lived, `httponly`,
  `samesite=lax`, `secure`-per-environment cookie carrying a fresh CSPRNG value; store its
  **SHA-256** (not the value) in the state record. On `/callback`, require the cookie, recompute,
  compare in constant time, and delete the cookie. Reject with 400 + an audit
  `INVALID_STATE`-equivalent event when absent or mismatched. `samesite=lax` is correct: the IdP
  returns via a top-level GET navigation.
- **Reuse** `backend/app/auth/cookies.py` (`_SECURE`, the existing set/clear helpers) rather than
  writing new cookie code.
- Store alongside `code_verifier` in the existing `OIDCStateStore`
  (`backend/app/auth/session.py:315-359`) — no new store.
- Also record `issuer` and a provider key in the state record (the §4 groundwork).
- This is the OIDC Core §15.5.2 construction and satisfies RFC 9700 §2.1.1's "bound to … the user
  agent in which the transaction was started".

**1b. Remove the access-token downgrade path.** *(fixes §2.5)*
- `backend/app/auth/keycloak_auth.py:654-662` — validate the **ID token only**. Absent or invalid ID
  token ⇒ hard 401. Delete the loop; do not leave both paths (CLAUDE.md).
- Guarantee an ID token exists by normalising the configured scopes: force `openid` into
  `cfg.scopes` in `get_authorization_url` (`:385`) rather than trusting an admin's edit. Log once if
  it had to be added.
- Keep `_roles_from_userinfo` (`:592-619`) — that path uses the access token as a **bearer credential**
  against `userinfo`, which is correct and unaffected.

**1c. Explicit identity-linking policy.** *(fixes §2.4)*
- New setting `oidc_link_by_email` (`strict` | `verified_only` | `never`), default **`verified_only`**
  for new installs; **`never` is the safe ceiling**, `strict` reproduces today's behaviour and is the
  upgrade default so nobody is locked out.
- `verified_only` requires an `email_verified: true` claim. Because Authentik hardcodes `false` and
  Entra omits it entirely (§2.4), the admin panel must say so next to the control, and `strict` must
  remain reachable with a warning.
- Enforce in `sync_keycloak_user_to_db` (`keycloak_auth.py:894-906`) — the email-fallback lookup and
  the local→OIDC conversion at `:900-906` are the two places that must consult it.
- Add `email_verified` to `KeycloakUserData` (`:243-259`) and read it from the ID token.
- Audit every link/convert decision.

**1d. `.env.example` correction.** *(fixes §2.7)* — `.env.example:1223` → `true`; add a comment
explaining what it protects against. Also add the missing `KEYCLOAK_ROLES_CLAIM` / `KEYCLOAK_SCOPES` /
`OIDC_DISCOVERY_URL` rows to `docs-site/docs/configuration/environment-variables.md:439-443`.

**1e. Pin the "don't widen `algorithms`" rule now, before Phase 3 touches it.** A unit test asserting
that neither `none` nor any `HS*` can ever enter the accepted-algorithm set, so the Phase 3 change
cannot regress it (RFC 8725 §2.1; Discovery §3 permits `none` in the advertised list).

**Test additions:** callback without the state cookie → 400; callback with a *different* browser's
cookie → 400; token response without `id_token` → 401 (previously succeeded); ID token that fails
`aud` no longer falls through to the access token; `verified_only` refuses to link an unverified
address; `strict` still links (upgrade path).

### Phase 2 — Rename to `oidc_*` and split the module (zero behaviour change)

Reviewable as a mechanical diff. Nothing here should alter a single HTTP response body except the
added `oidc_enabled` field.

- **New package `backend/app/auth/oidc/`**, replacing `keycloak_auth.py` (911 lines → four files under
  the ~300 ceiling):
  - `config.py` — `OIDCConfig` (was `KeycloakConfig`), `from_env` / `from_db`.
  - `endpoints.py` — `resolve_endpoints`, `_get_realm_urls` (the Keycloak fallback keeps its
    Keycloak-shaped name because that is exactly what it is), `_endpoints_from_discovery`.
  - `flow.py` — PKCE helpers, `get_authorization_url`, `exchange_code_for_tokens`, logout.
  - `claims.py` — `_decode_token`, `_claim_by_path`, `_normalize_roles`, cert-claim extraction.
  - `provisioning.py` — `sync_oidc_user_to_db` and friends.
  - `backend/app/auth/oidc_discovery.py` moves in as `oidc/discovery.py` (199 lines, unchanged).
- **Config keys**: `backend/app/schemas/auth_config.py:142-181` (`KeycloakConfig` → `OIDCConfig`,
  `CATEGORY_SCHEMAS` key `"keycloak"` → `"oidc"`), `backend/app/core/auth_settings.py:226-280`,
  `backend/app/services/auth_config_service.py` (`SENSITIVE_KEYS:68-71`,
  `DATA_TYPE_MAPPING:86-91`, and **`ENV_TO_CONFIG_MAPPING:171-193` keeps every `KEYCLOAK_*` key,
  now mapping to `oidc_*`** — this is the one permitted compatibility site).
- **Alembic** `v37N_rename_keycloak_config_to_oidc` + the `_detect_schema_version()` arm (§3).
- **API**: `backend/app/api/endpoints/auth/keycloak.py` → `oidc.py`, routes `/api/auth/oidc/{login,
  callback}`; `backend/app/api/endpoints/auth/__init__.py`;
  `backend/app/api/endpoints/auth/methods.py` (emit `oidc_enabled`, keep `keycloak_enabled` for one
  release); `backend/app/schemas/user.py` (`AuthMethodsResponse`);
  `backend/app/api/endpoints/auth_config.py` (`_test_keycloak_connection` → `_test_oidc_connection`);
  `backend/app/middleware/csrf.py:38` exempt prefix.
- **`core/config.py:671-715`**: canonical `OIDC_*` attributes; `KEYCLOAK_*` retained as declared
  aliases so `_env_first` keeps resolving them, with a startup deprecation log.
- **Frontend**: `KeycloakSettings.svelte` → `OIDCSettings.svelte` (634 lines — split the four
  fieldsets out while moving), `lib/api/authConfig.ts:66-92`, `stores/auth.ts:46,61,493,539`,
  `routes/login/+page.svelte` (button + handler + `.keycloak-button` CSS),
  `components/settings/AuthenticationSettings.svelte`, `lib/search/settingsSearchIndex.ts:48`, and
  the `settings.keycloak.*` / `settings.authentication.keycloak.*` / `auth.*Keycloak*` namespaces in
  **all 8 locales**. Default login-button label becomes "Sign in with SSO" with a new
  `oidc_display_name` setting so an Authentik shop can put its own name on it.
- **Rename the existing tests** (`test_keycloak_auth.py`, `test_oidc_discovery.py`,
  `KeycloakSettings.oidcFields.test.ts`, `e2e/test_ldap_keycloak.py`) **without altering their
  assertions** — in particular `TestRealmFallbackUnchanged` must still assert the byte-identical
  realm URLs.
- **Add the vendor-noun invariant test** (§3).

### Phase 3 — Protocol conformance and provider interop

Lands in the Phase 2 module layout.

**3a. Signing algorithms.** New setting `oidc_id_token_signing_algs`, default `RS256`, multi-select
over `RS256/384/512`, `ES256/384/512`, `PS256/384/512`. `none` and `HS*` are **not offerable**.
Auto-suggest from `id_token_signing_alg_values_supported` in the admin panel and in the Test
Connection result, but **never** feed the discovery list into `jwt.decode(algorithms=…)` — intersect
with our allow-list and fail loudly if empty. `python-jose[cryptography]>=3.5.0`
(`backend/requirements.txt:11`) already supports all of these; no new dependency.
*This is the single change that unblocks Authentik-with-an-EC-key.*

**3b. Token-endpoint authentication.** Read `token_endpoint_auth_methods_supported`; **default to
`client_secret_basic` when absent** (Discovery §3, RFC 8414 §2, Core §9, Registration §2 all agree).
Preference order `client_secret_basic` → `client_secret_post`, with an `oidc_token_endpoint_auth_method`
override (`auto` | `basic` | `post` | `none`). Add `private_key_jwt` / mTLS to the follow-up issue,
not here.

**3c. Public clients.** When no secret is stored, send **no** `client_secret` parameter at all rather
than an empty string, and require PKCE. Add an explicit "Clear client secret" action to the panel and
a matching backend path — the current design (`auth_config_service.py:54-56,633-636`) is correct for
"leave blank to keep current" and must stay; what is missing is a deliberate clear, not a change to
the blank semantics.

**3d. Complete the ID-token checks** in `oidc/claims.py`:
- Reject `aud` values containing audiences not in a configured trust list (§3.1.3.7 #3, second
  clause). Default trust list = `{client_id} ∪ oidc_audience`. **Keycloak's `aud: account` makes this
  a real upgrade hazard — ship it behind `oidc_verify_audience` and document it loudly.**
- Verify `azp == client_id` when `azp` is present (#4/#5).
- `iat` freshness window, default 600 s, configurable.
- Clock-skew leeway, default **60 s**, hard-capped at 300 s.
- Turn `oidc_issuer` into an issuer **allow-list** (comma-separated) so Google's two spellings and
  Entra's `{tenantid}`-substituted value can both be expressed. Compare exactly, no normalisation
  (Core §16.15 — path is significant).
- Validate the discovery document's own `issuer` against the URL it was fetched from (RFC 8414 §3.3)
  in `oidc/discovery.py`.

**3e. `nonce`.** Generate per transaction, store beside `code_verifier`, send in the authorization
request, verify against the **token-endpoint** ID Token, and treat *all* tokens as unusable until it
matches (RFC 9700 §4.5.3.2). No setting — always on.

**3f. `at_hash`.** Validate **if present**; never require it (Core §3.1.3.6 OPTIONAL, §3.1.3.8 MAY).
~15 lines in `oidc/claims.py`.

**3g. Standards-compliant logout.** *(fixes §2.6)* Replace the proprietary POST with:
1. **RP-Initiated Logout 1.0** — front-channel redirect to `end_session_endpoint` with
   `id_token_hint` + `post_logout_redirect_uri` + `state`. This requires storing the ID token (or at
   least keeping it for the session), which is a change to what `oidc_refresh_token` holds — decide
   and document.
2. **RFC 7009 token revocation** against `revocation_endpoint` from discovery, for the back-channel
   teardown the current code was reaching for.
3. Keep the Keycloak legacy POST **only** as an explicit `oidc_logout_mode=keycloak_legacy` opt-in, or
   drop it entirely — do not leave both running unconditionally.
   Google has **no** `end_session_endpoint`; degrade to local session clear (already the behaviour at
   `keycloak_auth.py:483-486`).

### Phase 4 — Identity columns and `auth_type`

One Alembic revision, `v37N+1`, per §3's migration story: `auth_type` `'keycloak'`→`'oidc'` with the
CHECK swap in a single transaction, `keycloak_id`→`oidc_subject`, `keycloak_refresh_token`→
`oidc_refresh_token`. Touches `backend/app/models/user.py:62-80`, `backend/app/auth/constants.py:9-43`,
`backend/app/auth/utils.py`, `backend/app/auth/external_sync.py:122`,
`backend/app/api/endpoints/auth/{login,mfa_tokens,sessions,methods}.py`,
`backend/app/schemas/user.py`, `frontend/src/stores/auth.ts:46`,
`frontend/src/components/UserManagementTable.svelte`. **Bump `CLOUD_SEAM_VERSION`** and coordinate
with the pinned submodule in the private cloud repo. Pair with a
`test_v37N_migration_consistency.py` following the `v372`/`v373` pattern.

**This is the phase to cut if the work must be trimmed** — it is the only one with no user-visible
benefit beyond the admin Users table.

### Phase 5 — Documentation and the verifiable claim

- **Rewrite `docs/KEYCLOAK_SETUP.md` → `docs/OIDC_SETUP.md`** (provider-neutral core + a Keycloak
  section), leaving a stub at the old path. Update the pointers in root `CLAUDE.md`,
  `backend/app/auth/CLAUDE.md:140`, `.env.example:1168`.
- **Rename `docs-site/docs/authentication/keycloak.md` → `oidc.md`**, promoting the existing provider
  table, and add worked examples for **Okta**, **Entra ID** and **Google** matching the Authentik one
  already there (lines 154-188).
- Per-provider gotcha callouts drawn from §6 — at minimum: Entra's `{tenantid}` placeholder and the
  **groups-overage** claim; Google's two issuers and absent groups; Okta's org-AS vs custom-AS issuer
  and opaque access tokens; Authentik's signing-key-determines-algorithm and hardcoded
  `email_verified: false`; Keycloak's roles-not-in-ID-token default and `aud: account`.
- Update `backend/app/auth/CLAUDE.md` (auth-type table, gotchas) and `CHANGELOG.md`.
- **Optional:** an `--with-authentik-test` overlay for `opentr.sh`, mirroring `--with-keycloak-test`
  and honouring the aux-overlay isolation rules from issue #347 (`scripts/CLAUDE.md`). This is what
  would make "any OIDC provider" continuously *verified* rather than merely *claimed*. Cost: an
  Authentik container needs its own Postgres and Redis, so it is a heavier overlay than the LDAP or
  Keycloak ones — worth doing, but do not let it block Phases 1-4.

---

## 6. Provider conformance matrix

Compiled from vendor documentation and, where documentation was silent, from provider source. Items
marked *unverified* could not be confirmed from an authoritative source — do not treat them as facts.

| | **Keycloak 26.x** | **Authentik** | **Okta (Workforce/OIE)** | **Entra ID v2.0** | **Google** |
|---|---|---|---|---|---|
| **Discovery URL** | `{host}/realms/{realm}/.well-known/openid-configuration` | `{host}/application/o/{slug}/.well-known/openid-configuration` (**per application**) | Org: `{domain}/.well-known/openid-configuration`<br>Custom: `{domain}/oauth2/{asId}/.well-known/...` | `login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration` | `accounts.google.com/.well-known/openid-configuration` |
| **Issuer** | `{host}{relative-path}/realms/{realm}` | per-provider (default) or global | Org: `{domain}` · Custom: `{domain}/oauth2/{asId}` | `https://login.microsoftonline.com/{tenantid}/v2.0` — **literal placeholder** on `common`/`organizations` | `https://accounts.google.com` **or** `accounts.google.com` |
| **ID-token alg** | RS256 default; RS/HS/ES/PS + EdDSA configurable per client | **Determined by the signing key**: RSA→RS256, **EC P-256→ES256**, P-384→ES384, P-521→ES512, Ed→EdDSA, **no key→HS256**. Advertises exactly one | **RS256 only** (no documented alternative) | RS256 | RS256 |
| **Access-token format** | JWT, same JWKS | **JWT** (model docstring: *"non-opaque, using a JWT as identifier"*) | Org AS: **opaque — "consider these access tokens as opaque strings"**; Custom AS: JWT, structure guaranteed | **JWT only when audienced to your own registered API.** Tokens for MS-owned APIs are proprietary. Its `userinfo_endpoint` is `graph.microsoft.com/oidc/userinfo`, so the token from `openid profile email` is a *Graph* token you cannot validate | **Opaque** |
| **Token-endpoint auth** | basic, post, secret_jwt, private_key_jwt, tls_client_auth | **post, basic only** | basic (default), post, secret_jwt, private_key_jwt, none | post, basic, private_key_jwt, self_signed_tls_client_auth | post, basic |
| **Groups / roles claim** | `realm_access.roles`, `resource_access.<client>.roles` — **access token only by default** (`idToken=false` on the built-in mapper). No built-in groups scope; `oidc-group-membership-mapper` needed, `full.path=true` gives `/mygroup` | `groups` (group **names**) — emitted by the **`profile`** scope, not a `groups` scope; in the ID token by default | `groups` — needs a Groups-type claim on the AS **and** the `groups` scope. Org AS: ID token only, never access token | `roles` (app roles); `groups` = **object GUIDs** unless `optionalClaims` requests names | **None.** `claims_supported` has no groups claim; requires Admin SDK Directory API (restricted scope) |
| **PKCE** | plain + S256; **not required by default**, enforceable per client | plain + S256; **cannot be required** — no such setting exists | **S256 only**; required when auth method is `none` | plain + S256; **`code_challenge_methods_supported` is absent from the discovery document** even though PKCE works | plain + S256, "recommended" |
| **`nonce` required?** | No (code flow) — *verified from source only, no prose statement* | No (code flow) | No (code flow) — *inferred from overview prose; API reference unfetchable* | No for `code`; **required for hybrid / `id_token`** | **Docs contradict each other** — guide says Required, reference says "only if requesting an ID Token". Send one always |
| **`email_verified`** | From the user's admin-writable `emailVerified` flag | **Hardcoded `false`** since 2025.10 | Emitted, but Okta says don't rely on it; moves to `/userinfo` under the code flow | **Not emitted** — absent from `claims_supported` | Authoritative only for `@gmail.com`, or `true` **and** `hd` present |
| **`end_session_endpoint`** | Yes | Yes (per-app `/end-session/`) | Yes (`/oauth2/v1/logout`) | Yes | **None** |
| **Accepts our `refresh_token` POST logout?** | **Only Keycloak** — and its own docs say don't | No | No | No | No |

**Gotchas that will produce support tickets, per provider:**

- **Keycloak** — roles are **not** in the ID token by default, so an RP reading them there gets
  nothing until "Add to ID token" is enabled on the `roles` client-scope mappers. The default
  Audience Resolve mapper puts `aud: account` in the token and **does not** add our own client id, so
  a strict extra-audience check (Phase 3d) will reject working deployments unless documented. The
  `/auth` path prefix was removed in Keycloak 17 — always read `issuer` from discovery.
- **Authentik** — with no signing key selected it signs **HS256 with the client secret** and serves an
  empty JWKS; our documentation must require a signing key. `post_logout_redirect_uri` only landed in
  2026.5 and is **silently ignored** if no logout URIs are configured.
- **Okta** — the org-AS and custom-AS issuers are literally different strings, so a misconfigured
  RP fails `iss` validation. Custom AS needs the API Access Management add-on in production.
- **Entra ID** — a naive `iss == metadata.issuer` comparison fails **100 %** of the time on
  multitenant; Microsoft's instruction is to substitute the request's tenant id and then compare
  exactly. And the **groups overage** claim: above 200 groups in a JWT the `groups` claim is *omitted
  entirely* and replaced by `_claim_names`/`_claim_sources` pointing at Graph — meaning an RP that
  reads `groups` naively sees **zero groups for exactly its most-privileged users**. Phase 3 must at
  minimum detect `_claim_names` / `hasgroups` and log a clear error rather than silently denying
  admin.
- **Google** — no groups at all, opaque access tokens, two valid issuer spellings, refresh token
  issued **only on the first** code exchange (recover with `prompt=consent`).

**Sources.** OIDC: [Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html) ·
[Discovery 1.0](https://openid.net/specs/openid-connect-discovery-1_0.html) ·
[Registration 1.0](https://openid.net/specs/openid-connect-registration-1_0.html) ·
[RP-Initiated Logout 1.0](https://openid.net/specs/openid-connect-rpinitiated-1_0.html) ·
[FAPI 2.0 Security Profile](https://openid.net/specs/fapi-security-profile-2_0-final.html).
IETF: [RFC 6749](https://www.rfc-editor.org/rfc/rfc6749.html) ·
[RFC 7009](https://www.rfc-editor.org/rfc/rfc7009.html) ·
[RFC 7636](https://www.rfc-editor.org/rfc/rfc7636.html) ·
[RFC 8414](https://www.rfc-editor.org/rfc/rfc8414.html) ·
[RFC 8705](https://www.rfc-editor.org/rfc/rfc8705.html) ·
[RFC 8725](https://www.rfc-editor.org/rfc/rfc8725.txt) ·
[RFC 9068](https://www.rfc-editor.org/rfc/rfc9068.txt) ·
[RFC 9207](https://www.rfc-editor.org/rfc/rfc9207.html) ·
[RFC 9700 (BCP 240)](https://www.rfc-editor.org/rfc/rfc9700.txt) ·
[draft-ietf-oauth-v2-1-13](https://www.ietf.org/archive/id/draft-ietf-oauth-v2-1-13.txt).
Vendors: [Keycloak OIDC endpoints](https://www.keycloak.org/securing-apps/oidc-layers) ·
[Keycloak admin guide](https://www.keycloak.org/docs/latest/server_admin/index.html) ·
[authentik OAuth2 provider](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/) ·
[authentik logout](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/frontchannel_and_backchannel_logout) ·
[Okta authorization servers](https://developer.okta.com/docs/concepts/auth-servers/) ·
[Okta validate access tokens](https://developer.okta.com/docs/guides/validate-access-tokens/main/) ·
[Okta groups claim](https://developer.okta.com/docs/guides/customize-tokens-groups-claim/main/) ·
[Entra access tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens) ·
[Entra ID token claims](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference) ·
[Entra optional claims](https://learn.microsoft.com/en-us/entra/identity-platform/optional-claims) ·
[Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect) ·
[Google OIDC reference](https://developers.google.com/identity/openid-connect/reference).

---

## 7. Test strategy

### How backwards compatibility is proven

1. **The pinned realm-URL contract survives untouched.**
   `backend/tests/unit/test_oidc_discovery.py::TestRealmFallbackUnchanged` (lines 294-335) asserts
   the six byte-exact Keycloak realm URLs plus the public/internal split, and
   `test_discovery_failure_falls_back_to_realm` (:279) asserts no discovery request fires without a
   discovery URL. **These assertions must not be edited in any phase** — only the module path in the
   import line may change (Phase 2). If a change requires editing them, the change is wrong.
2. **Env-var compatibility matrix.** New parametrised test: for every historical `KEYCLOAK_*` name,
   setting it alone yields the same effective `oidc_*` value as its `OIDC_*` counterpart, and
   `KEYCLOAK_*` wins when both are set (preserving today's documented precedence,
   `auth_config_service.py:175-181`).
3. **Migration consistency tests**, one per revision, on the `test_v372/v373_migration_consistency.py`
   pattern: assert `down_revision`, that the revision is head, vendor-neutrality, that
   `_detect_schema_version()` returns it against the live post-migration schema, **that re-running is
   a no-op**, and — specific to these — that a seeded encrypted `keycloak_client_secret` still
   decrypts under `oidc_client_secret` afterwards.
4. **`auth_config_audit` continuity**: history written before the rename is still returned by
   `GET /api/auth-config/audit/oidc`.
5. **E2E**: `backend/tests/e2e/test_ldap_keycloak.py` under `RUN_AUTH_E2E` with
   `./opentr.sh start dev --with-keycloak-test` must pass unchanged (modulo renamed selectors) at
   every phase boundary. This is the real-Keycloak proof.

### New coverage per phase

- **Phase 1** — callback with no state cookie → 400; callback with a foreign cookie → 400; cookie
  present but state absent → 400; token response with no `id_token` → 401; ID token failing `aud` no
  longer falls back to the access token; `oidc_link_by_email=verified_only` refuses an unverified
  address while `strict` links; audit events emitted for each.
- **Phase 2** — the **vendor-noun invariant test** (`keycloak` permitted in exactly two backend
  files); frontend vitest for the renamed panel, reusing the existing
  `KeycloakSettings.oidcFields.test.ts` assertions verbatim; a route test asserting
  `/api/auth/oidc/login` exists and, for the deprecation window, that `AuthMethodsResponse` carries
  both `oidc_enabled` and `keycloak_enabled` with the same value.
- **Phase 3 — the negative-security set, which is the point of the exercise.** Locally generated
  keys, no network:
  - `alg: none` token → rejected.
  - `HS256` token signed with the client secret → rejected (RFC 8725 §2.1 / Core §3.1.3.7 #8).
  - Discovery advertising `["none","HS256"]` → the intersection with our allow-list is empty and
    login fails loudly rather than accepting either.
  - **ES256-signed ID token accepted when `ES256` is in the allow-list** — the Authentik case, and the
    single most important new test in this plan.
  - `aud` containing an untrusted extra audience → rejected; `aud: [client_id, "account"]` accepted
    when `account` is in the trust list.
  - `azp != client_id` → rejected; `azp` absent → accepted.
  - `iat` older than the window → rejected; `exp` 30 s in the past → accepted at 60 s leeway,
    rejected at 0.
  - `nonce` mismatch → rejected, **and no token is used for any purpose** (RFC 9700 §4.5.3.2).
  - `at_hash` present-and-wrong → rejected; absent → accepted.
  - Metadata with `token_endpoint_auth_methods_supported` **absent** → Basic is used (the spec
    default); with `["client_secret_post"]` only → post is used.
  - No stored secret → the `client_secret` parameter is **absent**, not empty.
- **Provider golden fixtures.** Capture the five real discovery documents as redacted JSON under
  `backend/tests/fixtures/oidc/{keycloak,authentik,okta,entra,google}.json` and drive
  `resolve_endpoints` + the alg-negotiation + auth-method-negotiation logic through each. This is
  what turns "supports any OIDC provider" from a claim into an assertion, and it costs no network at
  test time. Register the files with a provenance comment and a capture date.
- **Phase 4** — migration consistency test; a test that no `auth_type` row can be `'keycloak'` after
  upgrade; `local_password_allowed` and the MFA-bypass rule still behave identically for `'oidc'`.

### Gates

`./scripts/run-integration-tests.sh` is the pre-merge gate (root `CLAUDE.md`). Everything above is
ungated unit/API coverage except the Keycloak E2E (`RUN_AUTH_E2E`). Negative auth tests must use a
**nonexistent account**, never a wrong password for `admin@example.com` — progressive per-account
lockout poisons the suite (`backend/tests/CLAUDE.md`).

---

## 8. Explicitly out of scope

| Item | Why |
|---|---|
| **Multiple simultaneous OIDC providers** | Nobody has asked; the gap was inferred from code shape. Large (schema, `(iss,sub)` keying, UI, RFC 9207 mix-up defence). §4 keeps the door open at near-zero cost. Separate issue. |
| **Claim → sharing-group mapping** | The largest single item in the handoff, and a distinct feature: it needs a data model (auto-created `UserGroup` vs an explicit mapping table), a **revocation** story when directory membership changes, and it multiplies the blast radius of §2.4. Separate issue. The *role*→admin half already exists and is hardened here. |
| **Any path to `super_admin` from an IdP** | Deliberate and permanent. `keycloak_auth.py:746,830-839,865-876` cap external grants at `ROLE_ADMIN` and never demote an existing `super_admin`; `backend/app/auth/CLAUDE.md` documents why. A test pins it. |
| **Moving the redirect URI from the SPA `/login` to a backend endpoint** | It would be architecturally cleaner (the authorization code would never touch the browser URL), but it invalidates **every existing deployment's registered redirect URI** — a guaranteed upgrade outage. Phase 1a fixes the security consequence instead. Note the residual limitation in the docs: the code appears in browser history and may leak via `Referer`. |
| **`private_key_jwt` / mTLS client authentication (RFC 8705)** | RFC 9700 §2.5 and FAPI 2.0 prefer them, and Entra + Okta support them, but no deployment has asked and `client_secret_basic` closes the actual conformance gap. Follow-up. |
| **RFC 9207 `iss` authorization-response validation** | RFC 9700 §4.4.2: "When an OAuth client can only interact with one authorization server, a mix-up defense is not required." Becomes a MUST with multi-provider; recorded on that issue. |
| **OIDC Back-Channel / Front-Channel Logout notification endpoints** | Separate specs, separate feature (IdP-initiated logout). Phase 3g covers *RP*-initiated logout only. |
| **Dynamic Client Registration, DPoP, FAPI 2.0, JARM, SAML** | No demand. |
| **`docs/SEARCH_ARCHITECTURE.md` staleness** | Real (found in the same sweep) but unrelated. Its own issue. |
| **Rewriting our own session JWTs** | `JWT_ALGORITHM=HS256`/`HS512` for OpenTranscribe-issued tokens is a separate subsystem (`backend/app/core/security.py:319-334`) with its own FIPS-mode logic. Untouched. |

---

## 9. Draft GitHub issue

> ⚠️ **DRAFT — NOT POSTED.** Outward-facing; a human must review and approve before this goes up.
> Trim the internal file references before posting if that level of detail is unwanted.

---

**Title:** Make "any OpenID Connect provider" true and verifiable — protocol conformance, provider-neutral naming, and two security fixes

**Labels:** `enhancement`, `security`, `authentication`
**Relates to:** #353

### Background

In #353, @<reporter> asked:

> "OIDC appears to be hardcoded to use Keycloak. Is this intentional, or is it supposed to use the standard OpenID service instead?"

The honest answer was *partly yes*. #353 fixed the immediate blocker — endpoints are now taken from the provider's `.well-known/openid-configuration` instead of being built from Keycloak's `/realms/<realm>/protocol/openid-connect/...` path shape. This issue covers everything that question exposed but that #353 did not fix.

Nobody is blocked (the reporter declined a hotfix), so this is being done properly rather than quickly.

### What is still wrong

**Protocol conformance**

- **ID tokens are only accepted if signed with RS256.** Authentik derives its signing algorithm from the key type, so an Authentik provider with an EC signing key issues **ES256** tokens that we reject outright. `id_token_signing_alg_values_supported` is in every discovery document and we ignore it.
- **The client secret is always sent in the request body** (`client_secret_post`). RFC 6749 §2.3.1 calls that "NOT RECOMMENDED", and `client_secret_basic` is the default a provider is entitled to assume when it publishes no `token_endpoint_auth_methods_supported`. We rely on vendor leniency rather than the spec.
- **Public (secret-less) clients are not supported** — we send an empty `client_secret` rather than omitting it, and there is no way to clear a stored secret through the UI.
- **Federated logout uses a Keycloak-proprietary message.** We POST `client_id` + `client_secret` + `refresh_token` to `end_session_endpoint`. OIDC RP-Initiated Logout 1.0 defines no such parameters; Keycloak's own documentation calls this form "non-standard legacy … not recommended". It works on Keycloak and on nothing else. Google publishes no end-session endpoint at all.
- **We validate the access token as a fallback when the ID token is missing or invalid.** RFC 9068 §6: "the client MUST NOT inspect the content of the access token". Okta (org authorization server), Google and Microsoft all issue access tokens a client cannot validate, and Microsoft says so explicitly. It is also a silent downgrade: an ID token that fails validation currently falls through.
- **Missing ID-token checks** required or recommended by OIDC Core §3.1.3.7: rejection of untrusted extra audiences, `azp` verification, an `iat` freshness window, and any clock-skew allowance. `nonce` and `at_hash` are not implemented (both are OPTIONAL for the authorization-code flow, but `nonce` is required in practice by several providers and is cheap).
- **The issuer check cannot express two real providers**: Microsoft Entra's multitenant metadata returns the literal placeholder `https://login.microsoftonline.com/{tenantid}/v2.0`, and Google documents *two* valid issuer spellings.

**Security (present today, provider-independent)**

- **The OIDC `state` is not bound to the browser that started the login.** It is unguessable and single-use, but stored server-side keyed only on its own value, so an attacker who starts a login, authenticates as themselves, and then induces a victim's browser to load the callback URL can plant *their* session in the victim's browser. OIDC Core §15.5.2 and RFC 9700 §2.1.1 both require the transaction to be bound to the user agent. PKCE does not cover this.
- **JIT provisioning links an IdP identity to an existing local account by email address with no verification check**, converting a local account (and clearing its password) on match. There is deliberately no single fix here — Authentik hardcodes `email_verified: false`, and Entra ID does not emit the claim at all — so this needs an explicit, admin-chosen linking policy.

**Naming — what was actually noticed**

Every configuration field an Authentik or Okta administrator types into is named `keycloak_*`: 231 occurrences in the OIDC auth module alone, plus a `keycloak_*` config namespace, a `settings.keycloak.*` i18n namespace across 8 locales, an `AUTH_TYPE_KEYCLOAK` user attribute, and `user.keycloak_id` / `user.keycloak_refresh_token` columns. Discovery working does not fix that impression.

**Documentation**

`docs/KEYCLOAK_SETUP.md` is Keycloak-only end to end. There are no setup guides for Okta, Entra ID or Google, and the environment-variable reference omits `OIDC_DISCOVERY_URL`.

### Plan

Five independently shippable phases. **Existing Keycloak deployments must keep working at every phase boundary** — there is a test pinning the exact realm URLs and the no-discovery-request behaviour, and it stays untouched.

1. **Security fixes** — bind the OIDC transaction to the browser with an httpOnly cookie; require an ID token (remove the access-token fallback); add an explicit identity-linking policy; correct `KEYCLOAK_VERIFY_AUDIENCE=false` in `.env.example`.
2. **Rename to `oidc_*`** — config keys (via an idempotent data migration), schema, service, API routes, admin panel, i18n. No behaviour change. `KEYCLOAK_*` environment variables keep working through the existing env→config mapping, which is the *only* place the old spelling survives; a test enforces that.
3. **Protocol conformance** — configurable signing-algorithm allow-list (never `none`, never `HS*` from discovery); token-endpoint auth-method negotiation defaulting to Basic; public-client support; `nonce`; `at_hash` when present; `azp` / extra-audience / `iat` / clock-skew checks; issuer allow-list; standards-compliant RP-Initiated Logout plus RFC 7009 revocation.
4. **Identity columns** — `auth_type` value `keycloak` → `oidc`, `keycloak_id` → `oidc_subject` (the value is an OIDC `sub`, unique only per issuer — the current name is actively misleading), `keycloak_refresh_token` → `oidc_refresh_token`.
5. **Documentation** — provider-neutral setup guide, plus worked examples and gotcha callouts for Keycloak, Authentik, Okta, Entra ID and Google.

### How "any OIDC provider" becomes verifiable

Not by assertion. A fixture suite of five real discovery documents (Keycloak, Authentik, Okta, Entra ID, Google) drives endpoint resolution and algorithm/auth-method negotiation offline, plus negative tests for `alg: none`, HS256-with-client-secret forgery, untrusted audiences, `nonce` mismatch and expiry — and a positive test that an **ES256**-signed ID token is accepted, which is the exact case that fails today.

### Explicitly not in this issue

- **Multiple simultaneous OIDC providers** — nobody has asked; separate issue. (It is what makes RFC 9207 `iss` validation a MUST rather than a nice-to-have.)
- **Claim → sharing-group mapping** — LDAP and OIDC both capture the full group list and discard everything but a single `is_admin` boolean. Real gap, but it is its own feature: it needs a data model and a revocation story. Separate issue.
- **Any path from an external IdP to `super_admin`** — capped at `admin` by design, permanently.
- **Moving the redirect URI off the SPA login page** — cleaner, but it would invalidate every existing deployment's registered redirect URI.

@<reporter> — the algorithm and naming items above are the direct answers to your question; the Authentik notes in the provider matrix come from your setup. If your Authentik provider uses an EC signing key, item 1 of "Protocol conformance" is why it would still not have worked even after #353.
