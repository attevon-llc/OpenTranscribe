# Auth parity with Open WebUI — research and gap analysis

**Status:** research and planning only. No source file was modified while writing this.
**Base:** worktree `.claude/worktrees/authoverhaul`, branch `security/auth-identity-overhaul`.
**Relationship to other plans:** this document **extends** `plans/oidc-conformance-plan.md`. That plan
owns everything inside the OIDC protocol boundary (discovery, algorithms, token validation, state
binding, RP-initiated logout, the `keycloak_*` → `oidc_*` rename). This document owns everything
*outside* it: the identity-provisioning plane, the trusted-header method, in-app group/permission
mapping, and the first-run experience. §8 lists the two places where I argue the OIDC plan's
sequencing or a deferred decision should change, and why.
**Open WebUI version researched:** docs and repo `main` as of 2026-08-07; latest release **v0.11.0**
(2026-07-27). Several behaviours below changed in v0.8.0–v0.11.0, so anything you read in an older
comparison is stale.

---

## 1. Licence position (short — it is a footnote, not a framing device)

Authentication **capabilities and configuration contracts are open practice**. Generic OIDC
discovery, group-claim mapping, trusted-header auth behind a reverse proxy, invite-by-email and SCIM
provisioning are *functionality*, described in public specifications precisely so independent
implementations converge. Interoperability is the point. This document therefore catalogues Open
WebUI's capability surface — env var names, settings semantics, claim-mapping behaviour, defaults,
admin UI organisation — in as much detail as is useful, and recommends matching whatever is
genuinely good.

The one line we do not cross: **do not paste Open WebUI source into this repo.** Implement from the
specifications (OIDC Core, OAuth 2.1/RFC 9700, RFC 7239, RFC 7642/7643/7644, RFC 6238) and from the
behavioural contract documented here.

For completeness, since it will otherwise be asked: Open WebUI is BSD-3-Clause plus a branding-
retention clause added in 2025 ([LICENSE](https://github.com/open-webui/open-webui/blob/main/LICENSE),
first shipped in v0.6.6; the project [states itself](https://docs.openwebui.com/license/) that this is
not OSI-approved). Code at tag `v0.6.5` and earlier is plain BSD-3/MIT and would be AGPL-compatible;
current `main` is not. This is irrelevant to everything below, because nothing below proposes copying
code.

---

## 2. Capability matrix

`OWUI` = Open WebUI. `OT` = OpenTranscribe on this branch. Verdicts: **HAVE** (already equal or
better) · **ADOPT** (closes a real gap) · **MATCH** (parity for parity's sake — low value, do it only
when cheap) · **REJECT** (deliberately not doing it) · **DECIDE** (needs its own decision, not
resolvable here).

### 2.1 Authentication methods

| Capability | OWUI | OT | Verdict | One-line reason |
|---|---|---|---|---|
| Local password | yes, `ENABLE_LOGIN_FORM`/`ENABLE_PASSWORD_AUTH` | yes, `local_enabled` | **HAVE** | Ours is DB-backed, live-editable, and does not silently break LDAP the way `ENABLE_PASSWORD_AUTH=false` does. |
| Generic OIDC | yes, `OPENID_PROVIDER_URL` + client id/secret | yes (`keycloak_discovery_url`, #353) | **HAVE** (conformance pending) | Both do discovery; our protocol conformance gaps are already owned by `oidc-conformance-plan.md`. |
| Named provider shortcuts (Google/Microsoft/GitHub) | yes, separate code paths | no | **MATCH** | A discovery URL covers all of them; a preset *picker* in the admin UI is the cheap 90 % (§5 R6). |
| Two generic OIDC issuers at once | **no** — hard ceiling, one `OPENID_PROVIDER_URL` | no | **HAVE** | Confirms the OIDC plan's §4 "multi-provider out of scope" call: it is not a parity gap. |
| LDAP / AD | yes, single server, login-only | yes, + group admission, AD nested-group chase (OID 1.2.840.113556.1.4.1941), StartTLS, referral-aware config | **HAVE (well ahead)** | See §4.2 — their LDAP is materially weaker than ours on five axes. |
| PKI / mTLS client certs | **none at any layer** | yes, with OCSP/CRL, CAC/PIV DN parsing, `mutual_tls` mode | **HAVE (unique)** | Nobody has even filed a request for it upstream. |
| Trusted-header / reverse-proxy auth (generic) | yes — email, name, groups, role headers | **only via the PKI path** | **ADOPT (top priority)** | This is the standard way to front an app with an SSO proxy; our machinery exists but is bound to X.509 semantics. §5 R1. |
| MFA / TOTP | **none** ([#1225](https://github.com/open-webui/open-webui/issues/1225) open since Mar 2024) | yes, RFC 6238 + backup codes + forced enrolment | **HAVE (unique)** | Their only answer is "delegate to your IdP". |
| SAML 2.0 | no | no | **REJECT** | No demand on either side; OIDC covers the enterprise cases we see. |

### 2.2 OIDC / OAuth configuration contract

| Capability | OWUI | OT | Verdict | Reason |
|---|---|---|---|---|
| PKCE | `OAUTH_CODE_CHALLENGE_METHOD`, **default off**, `S256` only; extended to all providers only in v0.11.0 | on by default (`keycloak_use_pkce=True`) | **HAVE** | Ours is the safer default. |
| Role claim → admin | `ENABLE_OAUTH_ROLE_MANAGEMENT`, `OAUTH_ROLES_CLAIM` (dot-nested), `OAUTH_ADMIN_ROLES` | `keycloak_roles_claim` (dot path), `keycloak_admin_role` | **HAVE** | Equivalent. Both evaluate at login only. |
| **Admission control** — refuse login unless the user holds an allowed role/group/domain | `OAUTH_ALLOWED_ROLES` (403 on mismatch), `OAUTH_ALLOWED_DOMAINS`, `OAUTH_BLOCKED_GROUPS` | **nothing** for OIDC (LDAP has `ldap_user_groups`) | **ADOPT** | Real security gap: today *any* identity the IdP authenticates gets a provisioned OT account. §5 R3. |
| Group claim → in-app groups | `ENABLE_OAUTH_GROUP_MANAGEMENT` + `ENABLE_OAUTH_GROUP_CREATION`, strict destructive sync | groups captured then **discarded**; only `is_admin` survives (`keycloak_auth.py:672`) | **ADOPT, with a different sync model** | Our biggest known gap — but their sync design is wrong (§6.5). §5 R2. |
| Account linking by email | `OAUTH_MERGE_ACCOUNTS_BY_EMAIL`, **default `False`**, docs call it "unsafe" | today: links by email with **no** verification check | **ADOPT (already planned)** | `oidc-conformance-plan.md` §5 Phase 1c owns this. Their default-off is evidence for a stricter default than that plan's upgrade path assumes — see §8.2. |
| JIT provisioning | yes; role = `DEFAULT_USER_ROLE` (default `pending`) | yes; role = `user`, or `admin` if the role claim matches | **HAVE** + see pending-state row | |
| Claim name overrides | `OAUTH_EMAIL_CLAIM`/`USERNAME_CLAIM`/`PICTURE_CLAIM`/`SUB_CLAIM` | only the roles claim is configurable | **MATCH** | Cheap, occasionally needed (Entra emits `preferred_username`, some IdPs put email in `upn`). |
| RP-initiated logout | yes, `end_session_endpoint` + `id_token_hint` | Keycloak-proprietary POST | **ADOPT (already planned)** | `oidc-conformance-plan.md` §5 Phase 3g. |
| Back-channel logout **receiver** | `ENABLE_OAUTH_BACKCHANNEL_LOGOUT` (needs Redis to actually revoke) | no | **DECIDE** | OIDC Back-Channel Logout 1.0. Lower value for us than for them because our sessions are revocable server-side anyway; see §5 R9. |
| Provider access-token → app-token exchange | `ENABLE_OAUTH_TOKEN_EXCHANGE` | no | **REJECT** | It shipped with a full account-takeover hole (CVE-2026-70482, CVSS 8.1) and per their own advisory *has no safe configuration* for Google/GitHub/Microsoft. |

### 2.3 Roles, groups, permissions

| Capability | OWUI | OT | Verdict | Reason |
|---|---|---|---|---|
| Role vocabulary | `pending` / `user` / `admin` | `user` / `admin` / `super_admin` | **HAVE** | Their "primary administrator" is the earliest-created row, and their own docs call it *"a convenience safeguard, not a security boundary"*. Our `super_admin` is a real, CHECK-enforced tier. |
| External IdP may grant admin | yes; **and a trusted HTTP header may grant admin** (`WEBUI_AUTH_TRUSTED_ROLE_HEADER`) | yes, capped at `admin`; `super_admin` is local-only | **HAVE** | Keep the cap. |
| Pending / admin-approval state | `DEFAULT_USER_ROLE=pending`, admin flips it in Admin Panel | `allow_registration` is on/off only | **ADOPT** | The small-business ask "let people sign up, I approve them" has no answer today. §5 R5. |
| In-app groups | first-class: membership + a permissions object + a share policy | `UserGroup` exists but is **sharing-only**, owner-scoped, and untouched by any auth code | **DECIDE** | Making groups an authorization principal is a real feature, not a mapping. §5 R2 covers the mapping half; the permission half is §5 R11. |
| Granular per-group permissions | **64** `USER_PERMISSIONS_*` toggles, union-only, **no deny** | role tiers + viewer/editor/owner on shared resources | **DECIDE** | Their surface is impressive and their *semantics* are a trap (§6.6). Do not copy the model; decide ours separately. |
| Resource access grants | normalised (resource, principal, read/write); public = wildcard principal | `CollectionShare` + `PermissionService` (viewer<editor<owner) | **HAVE** | Ours is narrower in scope but structurally equivalent. |

### 2.4 User lifecycle

| Capability | OWUI | OT | Verdict | Reason |
|---|---|---|---|---|
| Invitations | **none.** Admin adds users manually or by CSV import ([#11732](https://github.com/open-webui/open-webui/issues/11732) closed unimplemented) | full flow: `auth/invitations.py`, hashed single-use token, sets target `role` **and** `auth_type` | **HAVE (well ahead)** | |
| Email verification | **none — no mail subsystem at all**; zero `SMTP_*` vars | `auth/email_verification.py`, gates local login only | **HAVE (unique)** | |
| Self-service password reset | **none.** [Documented recovery is a DB `UPDATE`](https://docs.openwebui.com/troubleshooting/password-reset/) with an `htpasswd`-generated bcrypt hash | `auth/password_reset.py` + `/forgot-password` route | **HAVE (unique)** | |
| Bulk import | CSV in Admin Panel | no | **MATCH** | Cheap; genuinely useful for a 200-seat rollout. Lower value than an invite flow, which we have. |
| SCIM 2.0 | **yes** — `/api/v1/scim/v2/`, Users + Groups, bearer token, `active:false` → `pending` | **none** | **ADOPT** | The only standards-based deprovisioning path for an OIDC deployment. §5 R4. |
| Directory-driven deprovisioning | SCIM only, and SCIM is **OIDC-linked only** (`SCIM_AUTH_PROVIDER`), so LDAP users are unreachable by it | `services/directory_sync_service.py` — scheduled LDAP reconciliation, disable-never-delete, fail-closed on ambiguity, session revocation | **HAVE for LDAP, ADOPT for OIDC** | The two products have exactly inverse coverage. |
| Deactivate vs delete | no `active` column; "disable" = set role `pending`. Delete is a **hard cascade** that destroys the user's chats | `is_active` flag + `gdpr_erasure_service` for real erasure | **HAVE** | |
| API keys / service accounts | per-user `sk-` keys inheriting the owner's full permissions; global endpoint allowlist; **no expiry**; explicitly "no service accounts — make a bot user" | **none** | **ADOPT (scoped differently)** | Real gap for automation, but do not copy "a key acts as you, forever". §5 R7. |
| Audit of auth events | `AUDIT_LOG_LEVEL`, **default `NONE`**; file/stdout only, no UI, no query; auth endpoints always logged but **no failed-login event, no OAuth/LDAP failure coverage** | `auth/audit.py`, OpenSearch-backed, ~30 typed events incl. lockout, MFA, role change, session termination, + a `auth_config_audit` table with a UI | **HAVE (well ahead)** | |

### 2.5 Sessions

| Capability | OWUI | OT | Verdict | Reason |
|---|---|---|---|---|
| Credential | stateless JWT, `JWT_EXPIRES_IN` default **`4w`**, `-1` = never | short access JWT + rotating refresh token; **a session IS a `refresh_token` row** | **HAVE (well ahead)** | |
| Where the token lives | httpOnly `token` cookie **and** localStorage in the SPA | httpOnly cookie only, no JS-readable token | **HAVE** | Their localStorage copy is what turned CVE-2025-64496 from XSS into full account takeover + RCE. |
| Server-side revocation | **optional, off by default** — needs Redis; without it logout, password change and admin deactivation do not invalidate anything | always: refresh-token rows + Redis JTI list + per-user revocation epoch, with a Postgres fail-closed fallback (#324) | **HAVE (well ahead)** | |
| Idle / absolute timeout | none | both, enforced at refresh (`v375`) | **HAVE (unique)** | |
| Concurrent-session limit | `OAUTH_MAX_SESSIONS_PER_USER` (OAuth sessions only, default 10) | `max_concurrent_sessions` + `concurrent_session_policy`, all methods | **HAVE** | |
| Session list / revoke-one UI | no | `GET /api/auth/sessions`, `ActiveSessionsPanel.svelte` | **HAVE** | |
| Multi-replica | needs shared `WEBUI_SECRET_KEY` + Postgres + **two separate Redis URLs**; Redis is mandatory for correct security | Postgres is the system of record; Redis is a cache (`plans/session-ownership-decision.md`) | **HAVE** | |
| Rate limiting / lockout | **none in-app** — hardening guide delegates to the proxy | progressive per-identifier lockout + slowapi per-IP with trusted-proxy parsing | **HAVE (unique)** | |

### 2.6 Setup experience

| Capability | OWUI | OT | Verdict | Reason |
|---|---|---|---|---|
| First run | `docker run` → signup screen → **first account is admin**, signup then auto-disables | `./opentr.sh start dev` → bootstrap `super_admin` seeded; hardened → `INITIAL_ADMIN_EMAIL` + generated password logged once | **HAVE**, but see next row | Ours avoids the first-user TOCTOU race that became CVE-2026-45675 on their side. |
| Guided first-run auth setup | none (a signup form is not a wizard) | none | **ADOPT** | Neither product has this. It is the single biggest lever on "seamless" for the small-business tier. §5 R8. |
| Auth config in the admin UI | LDAP yes; **OAuth only if `ENABLE_OAUTH_PERSISTENT_CONFIG=true`** (default `false`), and [#24033](https://github.com/open-webui/open-webui/issues/24033) disputes even that; SCIM and trusted headers are env-only | all of it: Settings → Authentication, 6 tabs, DB > env > coded default, per-key `is_set` / `requires_restart`, config audit trail | **HAVE (well ahead)** | |
| Config precedence | "ConfigVar": env read **once** on first boot, then the DB silently wins. Whole docs section on "Ignored Environment Variables" | documented DB > env > default, with a UI that shows which | **HAVE** | Do not regress this. |
| Test-connection affordance | none for LDAP (requested 2024, never built) | `POST /api/auth-config/test` for ldap + keycloak | **HAVE** | |
| Auto-redirect to SSO | `OAUTH_AUTO_REDIRECT` (+ `/auth?form=true` escape hatch) | no | **MATCH** | Small, and enterprises do ask for it. §5 R6. |
| No-auth single-user mode | `WEBUI_AUTH=False`, **irreversible** once users exist | none | **REJECT** | A one-way door that their own docs warn about, and it does not even fully work ([#15254](https://github.com/open-webui/open-webui/issues/15254)). |

---

## 3. The tiered experience, concretely

### 3.1 Local / dev — "zero configuration, just works"

**Open WebUI.** One command:
`docker run -d -p 3000:8080 -v open-webui:/app/backend/data ghcr.io/open-webui/open-webui:main`.
Visit `localhost:3000`, fill in a signup form, and that first account is admin
([quick start](https://docs.openwebui.com/getting-started/quick-start/)). Signup then auto-disables.
`WEBUI_SECRET_KEY` is auto-generated into `.webui_secret_key`. Optionally `WEBUI_AUTH=False` removes
login entirely — but only on a virgin install, and you can never go back.

**OpenTranscribe.** `./opentr.sh start dev`. `initial_data._ensure_admin_user` seeds
`admin@example.com` / `password` as `super_admin`, but **only when `settings.is_hardened` is false**
(issue #284 A0.9). No signup step, no wizard, nothing to configure.

**Verdict: parity, and ours is slightly better** — the operator does not have to invent a credential,
and the well-known one is environment-gated rather than shipped to production. The one thing they
have that we do not is no-auth mode, and we should not want it.

### 3.2 Small business — "a few clicks to proper auth, no YAML archaeology"

**Open WebUI**, for the common case *"my team signs in with Google, and I approve new people"*:

1. Stop the container. Add `WEBUI_URL`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
   `OPENID_PROVIDER_URL`, `ENABLE_OAUTH_SIGNUP=true`. Restart.
2. Discover that `ENABLE_LOGIN_FORM` must be `False` when `ENABLE_OAUTH_SIGNUP` is `True`, or you
   cannot log in at all (a `:::danger` block in the env reference).
3. Discover that changing those env vars later does nothing, because they are ConfigVars now owned by
   the DB — [and there is a whole troubleshooting page about it](https://docs.openwebui.com/troubleshooting/sso/).
4. New users land in `pending`; approve each one in Admin Panel → Users.
5. When someone forgets a password: there is no reset. You
   [`UPDATE auth SET password=...`](https://docs.openwebui.com/troubleshooting/password-reset/) with a
   hash from `htpasswd`, or you use the undocumented password field in the admin Edit User modal.
6. There are no invitations and no verification email, because there is no mail subsystem.

**OpenTranscribe**, same case:

1. Settings → Authentication → Keycloak tab (as super_admin). Paste a discovery URL, client id,
   client secret. Save. **No restart** — DB config wins over `.env` and takes effect immediately.
2. "Test Connection" tells you whether it worked.
3. Set `local_enabled=false` on the Local tab to retire passwords; the cross-field rule refuses to
   leave you with self-registration minting accounts that can never sign in.
4. Invite people: Settings → Users → invite, which sets both `role` and `auth_type` and emails a
   single-use token.
5. Password reset, email verification and MFA all exist and are on the same screen.

**Verdict: we are materially ahead on the lifecycle, and behind on discovery.** Our failure mode is
different from theirs — nothing is broken, but a first-time operator has 83 auth-related variables in
`.env.example`, four docs pages, and no signposting toward the three settings that actually matter for
their situation. That is what §5 R8 addresses.

### 3.3 Enterprise — "SSO, provisioning, deprovisioning, audit, standards"

**Open WebUI has**, and we do not: SCIM 2.0 (users *and* groups, `active:false` → deactivate), IdP
groups landing in real in-app groups, generic trusted-header auth including group and role headers,
back-channel logout, and OIDC admission control (`OAUTH_ALLOWED_ROLES` / `_DOMAINS` /
`OAUTH_BLOCKED_GROUPS`).

**We have**, and they do not: MFA, PKI/mTLS with revocation checking, progressive lockout and
in-app rate limiting, password policy/history/expiry, FedRAMP AC-8 login banner (enforced, not just
displayed), a queryable OpenSearch audit trail with a config-change audit alongside it, idle and
absolute session timeouts, per-session revocation with a UI, LDAP group admission with AD nested-group
expansion, LDAP-driven deprovisioning, and a `super_admin` tier no IdP can reach.

**The honest summary:** *they are ahead on the provisioning plane; we are ahead on the control plane.*
An enterprise evaluating both will notice SCIM and group mapping first, because those are the two
things their IAM team asks about on the first call. That ordering drives §7.

---

## 4. Two areas worth more detail

### 4.1 Trusted-header auth — what they actually do

This is the item the brief flagged, so here is the whole contract.

**The headers** ([SSO docs](https://docs.openwebui.com/features/authentication-access/auth/sso/)):

| Var | Semantics |
|---|---|
| `WEBUI_AUTH_TRUSTED_EMAIL_HEADER` | The user's email. Handles **automatic registration and login**. |
| `WEBUI_AUTH_TRUSTED_NAME_HEADER` | Display name for JIT-created users. No effect if the user exists. |
| `WEBUI_AUTH_TRUSTED_GROUPS_HEADER` | Comma-separated group **names**. Unassigns from groups not listed. Will **not** create groups (unlike the OAuth path). Empty/absent → no change. |
| `WEBUI_AUTH_TRUSTED_ROLE_HEADER` | `admin` / `user` / `pending`, re-applied **on every sign-in**. Invalid → unchanged + a warning. |

**The trust model is network isolation and nothing else.** There is no proxy IP allowlist, no shared
secret, no HMAC, no signature, and no RFC 7239 / `X-Forwarded-For` involvement in the auth path at
all. Their docs say so plainly — the SSO page carries a `:::danger` that incorrect configuration
"can allow users to authenticate as any user", and the
[hardening guide](https://docs.openwebui.com/getting-started/advanced-topics/hardening/) states:

> "When using trusted headers, your proxy must strip these headers from incoming client requests
> before injecting its own values. If the proxy does not strip them, any client can send a forged
> header and authenticate as any user. **This is the most common misconfiguration with trusted header
> auth.**"

Note that this instruction appears **only in the hardening guide**, not on the SSO page an integrator
would actually be reading. Separately, `FORWARDED_ALLOW_IPS` defaults to `*` in their entrypoint, so
the app trusts every peer for forwarded-header purposes out of the box
([#22539](https://github.com/open-webui/open-webui/issues/22539) — it was hardcoded to `*` and
ignored the env var until March 2026).

**Architecture worth stealing (and worth improving):** the header is consulted at `/signin` only; the
per-request credential is their own JWT. That is correct — you do not want to re-derive identity from
a header on every request. But it created a real bug: logging out of the upstream IdP and back in as a
different person left the old app session live
([#14406](https://github.com/open-webui/open-webui/issues/14406)). Their fix (v0.6.14) was a
**per-request equality check** — if the header identity diverges from the session identity, return
401 "User mismatch. Please sign in again." That retrofit is the design; do it up front.

**Role and group headers grant privilege over HTTP.** Their own warning: *"Allowing untrusted clients
to reach Open WebUI directly would let anyone escalate to admin."* No CVE has been filed for this
because it is documented, intended behaviour.

**Where we stand.** All the hard parts already exist in `backend/app/auth/pki_auth.py` and
`backend/app/utils/client_ip.py` — a parsed CIDR allowlist (`_parse_pki_trusted_proxies`), an
immediate-peer check (`_pki_header_source_is_trusted`), a fail-closed refusal when no allowlist is
configured (`_validate_pki_headers_source`, and `main.py:80` refuses to boot hardened without one),
and proxy-chain parsing that walks `X-Forwarded-For` from the right end. What we do not have is a way
to use any of it for an identity that is **not** an X.509 subject DN. That is R1.

### 4.2 LDAP — we are ahead on five specific axes

Their surface: `ENABLE_LDAP`, `LDAP_SERVER_LABEL/HOST/PORT`, `LDAP_ATTRIBUTE_FOR_MAIL/USERNAME`,
`LDAP_APP_DN/PASSWORD`, `LDAP_SEARCH_BASE`, `LDAP_SEARCH_FILTER(S)`, `LDAP_USE_TLS`,
`LDAP_CA_CERT_FILE`, `LDAP_VALIDATE_CERT`, `LDAP_CIPHERS`, `ENABLE_LDAP_GROUP_MANAGEMENT`,
`ENABLE_LDAP_GROUP_CREATION`, `LDAP_ATTRIBUTE_FOR_GROUPS`. UI-editable
([env reference](https://docs.openwebui.com/reference/env-configuration/)).

Where ours is better, and these are not cosmetic:

1. **Nested groups.** They read `memberOf` on the user entry and take the first `CN=`. AD does not
   expand nested groups into `memberOf`, and no chained query is issued anywhere — so a user whose
   entitlement comes through a nested group gets nothing. We implement the AD matching-rule-in-chain
   OID behind `ldap_recursive_groups`.
2. **Group matching by DN vs CN.** They match on the bare CN, so `CN=Sales,OU=EMEA` and
   `CN=Sales,OU=APAC` collapse into one group. We compare full DNs.
3. **StartTLS.** Their docs tell you to use port 389 with `LDAP_USE_TLS=true` for StartTLS; the code
   only does implicit LDAPS, so that configuration fails
   ([#8027](https://github.com/open-webui/open-webui/issues/8027), with a patch, never merged). We have
   `ldap_use_ssl` and `ldap_use_tls` as distinct settings.
4. **Group → role.** They have no `LDAP_ADMIN_GROUPS` at all
   ([#19460](https://github.com/open-webui/open-webui/issues/19460), closed same-day). We have
   `ldap_admin_groups`, `ldap_admin_users` **and** `ldap_user_groups` for admission.
5. **Deprovisioning.** They have none for LDAP — SCIM is OIDC-linked only. We have
   `directory_sync_service`.

Also worth knowing as a cautionary tale: `ENABLE_PASSWORD_AUTH=false` silently disables their LDAP
endpoint while the frontend still renders a working-looking LDAP form. We already got the equivalent
right for the right reason — `local_enabled=false` deliberately does **not** hide the credential form,
because LDAP authenticates through it (`backend/app/auth/CLAUDE.md`, identity-source model).

---

## 5. Recommendations, prioritised

Sizes: **XS** ≤1 day · **S** 1–3 days · **M** ~1 week · **L** 2–4 weeks · **XL** >1 month. Each is
scoped as backend + admin UI + tests + docs.

R-numbers are stable identifiers, not an ordering — they are grouped by verdict below and sequenced
in §7. Group A is R1–R5, R7, R8; Group B is R6 and R10; Group C is R9 and R11.

### Group A — closes a real gap

---

**R1. Generic trusted-header (reverse-proxy) authentication.** — **M**
*Build from:* RFC 7239 (`Forwarded`) for chain parsing; no standard governs the identity headers
themselves, so the contract below **is** the spec.

Promote the existing PKI header machinery into an auth method of its own (`auth_type='proxy'`),
configured on a new **Proxy** tab in Settings → Authentication.

Settings: `proxy_enabled`, `proxy_trusted_proxies` (CIDR list), `proxy_email_header`,
`proxy_name_header`, `proxy_groups_header`, `proxy_groups_separator`, `proxy_role_header`,
`proxy_shared_secret` (sensitive), `proxy_allowed_domains`, `proxy_jit_provisioning`.

Non-negotiable trust rules, and this is where we should be strictly better than Open WebUI:

- **Fail closed with no allowlist.** `proxy_enabled` with an empty `proxy_trusted_proxies` refuses
  every header-sourced assertion, and `main.py` refuses to boot hardened — exactly the rule
  `_validate_pki_headers_source` already implements. Their model is "be unreachable"; ours is "be
  unreachable *and* prove the peer".
- **Optional shared secret as defence in depth.** A constant-time compare on a configured header
  value, so a proxy misconfiguration alone is not sufficient for takeover. They have no equivalent.
- **The role header is opt-in and capped.** Off unless `proxy_role_header` is explicitly set; accepted
  values `user` and `admin` only; `super_admin` unreachable, consistent with the existing rule that
  external IdPs grant at most `admin`.
- **Header consulted at sign-in, then a per-request consistency check.** Mint a normal session
  (`refresh_token` row) so every existing control — idle/absolute timeout, concurrent-session limit,
  revocation epoch, session list UI — applies unchanged. Then verify on each request that the asserted
  identity still matches the session's user, and revoke rather than 401 if it does not. This is the
  fix they had to retrofit after #14406.
- **Audit every assertion**, including refusals from untrusted peers, using the existing
  `AuditEventType` vocabulary.

Note the relationship to PKI: `pki_mode='header'` becomes a *specialisation* of this — a proxy
asserting a subject DN rather than an email. Do not fork the trust check; both must call one
implementation, per the repo's "delete the old one" rule.

---

**R2. IdP groups → in-app groups (and stop discarding the claim).** — **L**
*Build from:* SCIM 2.0 group semantics (RFC 7643 §4.2) as the data model reference; the claim
extraction itself is already written.

Today `keycloak_auth.py:672` computes `is_admin = cfg.admin_role in roles` and throws the rest away;
`ldap_auth.py` does the same. Both already have the full list in hand.

Design, and it should differ from theirs in one important way:

- A **`group_mapping` table**: `(source, claim_value, user_group_id, grants_role)`. An admin maps
  `CN=Transcription-Editors,OU=Groups` or the OIDC group `transcribers` onto an existing OT group.
  Auto-creation is a per-source opt-in (their `ENABLE_*_GROUP_CREATION`), off by default.
- **Mark IdP-derived memberships.** Add `UserGroupMember.source` ∈ {`manual`, `idp`}. Sync reconciles
  **only `idp` rows**. This is the deliberate divergence from Open WebUI's "strict synchronization",
  which removes users from *any* group not in the claim including manually created ones — a design
  their own users hit repeatedly ([#12392](https://github.com/open-webui/open-webui/issues/12392), open)
  and which turns a typo in the claim path into silent, total privilege loss.
- **Distinguish "claim absent" from "claim empty".** Absent → do nothing and log once. Empty →
  reconcile to empty. Their LDAP path conflates these (skips sync on empty, so a user removed from
  every AD group keeps all stale memberships) and their OAuth path conflates them the other way. Both
  are wrong; the distinction is one `is None` check.
- **Validate the mapping at save time**, and surface "this claim path produced 0 groups for the test
  user" in the existing Test Connection result. The single sharpest edge on their side is that
  nothing validates the configured claim path exists.
- `grants_role` is capped at `admin`, subsuming `keycloak_admin_role` / `ldap_admin_groups` into one
  mechanism rather than adding a second.

**Ordering note:** `oidc-conformance-plan.md` §8 puts this in a separate issue. Agreed on packaging —
it is a distinct feature with its own data model and revocation story — but see §8.1 on when.

---

**R3. OIDC admission control.** — **S**
*Build from:* nothing exotic; this is an authorization decision on already-validated claims.

Add `oidc_allowed_groups`, `oidc_blocked_groups`, `oidc_allowed_email_domains`. Refuse the login (401,
audited) when a user matches none of the allowed sets or any blocked set. Empty allowed-set = allow
all, preserving current behaviour on upgrade.

This is the highest value-per-line item in the document. Today, if you point OpenTranscribe at a
corporate Keycloak realm, **every identity in that realm gets an OpenTranscribe account on first
login.** LDAP already has this control (`ldap_user_groups`); OIDC does not. Open WebUI has
`OAUTH_ALLOWED_ROLES` (403 on mismatch), `OAUTH_ALLOWED_DOMAINS` and `OAUTH_BLOCKED_GROUPS`.

Do it **with** R2's claim extraction, not before — same code path.

---

**R4. SCIM 2.0 provisioning endpoint.** — **L**
*Build from:* RFC 7642 (concepts), **RFC 7643** (core schema), **RFC 7644** (protocol).

`/scim/v2/Users` and `/scim/v2/Groups`, `ServiceProviderConfig`, `ResourceTypes`, `Schemas`; filtering
(`filter=userName eq "..."`), pagination, `PATCH` per RFC 7644 §3.5.2. Static bearer token stored as a
sensitive auth-config key, plus — and this is where we improve on theirs — an **IP allowlist reusing
`utils/client_ip.py`**, because their docs only *suggest* restricting SCIM to the IdP's addresses and
enforce nothing.

Semantics to fix relative to theirs:

- `active: false` → set `is_active=False` **and revoke sessions** via
  `services/account_security_service.py`. Setting a role to `pending` as the disable mechanism (their
  approach) is a category error; we have a real flag.
- `DELETE /Users/{id}` → **soft-disable**, not hard delete. Their endpoint is documented as
  "deactivate" and performs a cascading hard delete that destroys the user's content. Route real
  erasure through `gdpr_erasure_service` on an explicit admin action only.
- Never touch `super_admin` accounts, mirroring `directory_sync_service` rule 2.
- Bind to the identity keys we already have (`keycloak_id`/`oidc_subject`, `external_id`) via
  `externalId`. Unlike theirs, do **not** make SCIM OIDC-only — an LDAP-sourced user should be
  addressable too.

This plus `directory_sync_service` gives us complete deprovisioning coverage across both directory
types, which neither product currently has.

---

**R5. Self-registration with admin approval.** — **S**
*Build from:* nothing; a state we already model.

Add `require_admin_approval` to the Local tab. When set, `POST /api/auth/register` creates the user
with `is_active=False`; the login path returns a distinct `detail.code` the SPA renders as "awaiting
approval"; admins see a filtered list in Settings → Users with an Approve action. Emit
`ADMIN_USER_UPDATE` on approval.

This is Open WebUI's `DEFAULT_USER_ROLE=pending` in our vocabulary, and it is the missing middle
between our current all-or-nothing `allow_registration`. It also gives JIT provisioning (R1/R2/OIDC) a
consistent landing state: `oidc_jit_requires_approval` reuses the same flag and the same UI.

---

**R7. Scoped, expiring API tokens.** — **M**
*Build from:* RFC 6750 (bearer usage) + our existing token infrastructure. Do **not** copy their model.

A new `api_token` table: owner, name, SHA-256 hash at rest, `scopes`, `expires_at` (required, with a
configurable ceiling), `last_used_at`, `revoked_at`. Presented as `Authorization: Bearer ot_…`,
disambiguated by prefix. Revocation flows through `stamp_user_revocation_epoch` so a disabled user's
tokens die with their sessions.

Divergences from theirs, all deliberate:

- **Scoped, not owner-equivalent.** Their docs say plainly *"An API key acts as you. It inherits your
  role and group permissions"*, with only a global endpoint allowlist and no per-key scoping — and the
  allowlist was itself bypassable (CVE-2026-45339). Ours should carry a scope set (`files:read`,
  `files:write`, `transcripts:read`, …) enforced in the dependency layer.
- **Expiry is mandatory.** Theirs have none and must be rotated by hand.
- **Never issuable by, or for, a `super_admin`-only surface.** Auth config, role changes and backups
  stay session-only.

The genuine use case is the one they solve with a bot user: ingestion pipelines and the watch-source
integrations already in this repo.

---

**R8. First-run auth setup flow.** — **M**
*Build from:* nothing external; this is product design over settings we already have.

Neither product has this, so it is the one place we can be plainly better rather than level. The
absence is not "a missing feature" — everything is configurable already — it is that a first-time
operator faces 83 auth-related variables in `.env.example`, six admin tabs and four docs pages with no
signposting toward the three settings their situation actually needs.

Shape: on first login by the bootstrap `super_admin`, before the app proper, show a short flow that
asks *what kind of deployment is this* and writes the corresponding auth config:

1. **Change the bootstrap password** (or confirm the generated one was rotated). Today the hardened
   path logs a generated password at CRITICAL and hopes the operator reads it.
2. **Pick a posture** — three cards, not a form:
   *Just me / small team* → local passwords, registration off, invitations on.
   *My company already has SSO* → OIDC tab prefilled from a provider preset (§5 R6), Test Connection
   inline, and on success offer `local_enabled=false` with the super_admin break-glass explained.
   *Directory (AD/LDAP)* → LDAP tab, Test Connection, `ldap_user_groups` prompted for explicitly,
   because admission control is the setting people skip.
3. **Offer the security defaults that are off** — MFA required, login banner, approval-on-signup
   (§5 R5) — as checkboxes with one-line consequences, not as a wall of toggles.
4. Mark it complete in `SystemSettings` and never show it again; make it re-runnable from
   Settings → Authentication for someone who skipped it.

Deliberately **not** Open WebUI's "first account to sign up is admin". We already seed the bootstrap
super_admin at startup on a single code path, which is why we never had their first-user TOCTOU race
(CVE-2026-45675, CVSS 8.1). This flow configures that account; it does not create it.

---

### Group B — matches them, worth doing when cheap

---

**R6. Onboarding affordances for OIDC.** — **S** (three independent pieces)

- **Provider presets.** A dropdown on the OIDC tab (Keycloak / Authentik / Okta / Entra ID / Google /
  Generic) that fills the discovery URL template, the default scopes, and the right roles/groups claim
  path. This is the 90 % of what their `GOOGLE_CLIENT_ID` / `MICROSOFT_CLIENT_TENANT_ID` shortcuts buy,
  without the separate code paths — and the per-provider knowledge is already written down in
  `oidc-conformance-plan.md` §6.
- **Auto-redirect to SSO.** `oidc_auto_redirect`: an unauthenticated visitor goes straight to the IdP.
  Needs their escape hatch (`/login?form=true`) or a super_admin locks themselves out — and must be
  refused while `local_enabled` and `ldap_enabled` are both false and no super_admin has a fallback,
  in the spirit of the existing cross-field rules.
- **Claim name overrides.** `oidc_email_claim`, `oidc_username_claim`, `oidc_subject_claim`. Cheap and
  occasionally load-bearing (Entra emits `preferred_username`; some deployments put mail in `upn`).

---

**R10. CSV bulk user import.** — **S**
Their Admin Panel → Users → Import. For us it should mint **invitations**, not accounts with
passwords — which is strictly better and reuses `auth/invitations.py` wholesale.

---

### Group C — needs its own decision

---

**R9. OIDC Back-Channel Logout receiver.** — **M**, **DECIDE**
*Build from:* OpenID Connect Back-Channel Logout 1.0.

Open WebUI has it (`ENABLE_OAUTH_BACKCHANNEL_LOGOUT`), but it only works with Redis configured — their
whole revocation story is optional. Ours is not: a logout token would map to a `refresh_token` row and
revoke it immediately and durably, which is the correct implementation and cheaper for us than for
them.

The reason it is a DECIDE rather than an ADOPT: with R4 (SCIM) landed, the enterprise deprovisioning
requirement is already met, and back-channel logout then only improves *latency* (seconds instead of
the next sync). `oidc-conformance-plan.md` §8 explicitly defers it. Worth an issue; not worth
displacing R1–R5.

---

**R11. Granular per-group permissions.** — **XL**, **DECIDE**
Open WebUI exposes 64 `USER_PERMISSIONS_*` toggles. We have role tiers plus viewer/editor/owner on
shared resources. Whether OpenTranscribe needs "this group may not export transcripts" is a product
question, not an auth question, and their **union-only, no-deny** semantics (§6.6) mean their surface
is a warning as much as a model. Decide separately, after R2 has made groups an authorization
principal at all.

---

## 6. What we should NOT copy, and why

These are engineering judgements, independent of licensing. Several are things we have already decided
differently and should keep deciding differently.

**6.1 Stateless JWT as the session, with revocation as an optional Redis add-on.**
Their [hardening guide](https://docs.openwebui.com/getting-started/advanced-topics/hardening/) says it
outright: without Redis, *"signing out does not invalidate a user's token… changing a user's password
does not invalidate their existing sessions… admin-initiated account deactivation does not immediately
block access."* With `JWT_EXPIRES_IN` defaulting to `4w`, a user you fire keeps working access for a
month. And the bolt-on missed a whole surface — CVE-2026-59219: realtime endpoints accepted
Redis-revoked JWTs after signout because they called `decode_token()` and nothing else.
We already decided the opposite in `plans/session-ownership-decision.md`: `RefreshToken` in Postgres is
the single owner, Redis is a cache, and #324 established the fail-closed fallback. Keep it. This is the
largest single quality gap between the two products and it is in our favour.

**6.2 Keeping the JWT in `localStorage`.**
Their SPA does, alongside the httpOnly cookie. Cato's writeup of CVE-2025-64496 names exactly that
combination — long-lived, JS-readable, no revocation — as what escalated an XSS into full account
takeover and then RCE. CVE-2026-70486 is the same pattern again. A
[proposal to move to httpOnly cookies](https://github.com/open-webui/open-webui/discussions/13951) has
sat with zero maintainer replies since May 2025. We are already httpOnly-only with no JS-readable
token. Never regress this, including for R7 API tokens (those are for machines, not browsers).

**6.3 Network isolation as the entire trust model for trusted headers.**
No proxy allowlist, no shared secret, no signature — plus `FORWARDED_ALLOW_IPS=*` by default. And the
"your proxy must strip inbound copies" instruction lives only in the hardening guide, not on the page
an integrator reads. R1 adopts the *capability* and rejects the *trust model*: a required CIDR
allowlist that fails closed, an optional shared secret, and a documented strip requirement on the SSO
page itself.

**6.4 Granting `admin` via an HTTP header, unconditionally.**
`WEBUI_AUTH_TRUSTED_ROLE_HEADER` is on whenever it is configured, and their own warning is *"anyone
[reaching the app directly] could escalate to admin."* R1 keeps it opt-in, allowlisted to
`user`/`admin`, and permanently unable to reach `super_admin` — which is the same rule
`external_sync.py` already enforces for OIDC and is documented in `backend/app/auth/CLAUDE.md`.

**6.5 Strict destructive group synchronisation.**
Verbatim from their SSO docs: a user is *"removed from any Open WebUI groups (including those manually
created or assigned within Open WebUI) if those groups are not present in their OAuth claims for that
login session."* So a typo in `OAUTH_GROUP_CLAIM` yields an empty set, and every login silently strips
every group the user has — presenting as a permissions bug, not a config bug. The docs claim admins are
exempt; researchers could not find that guard in `main`. R2's `UserGroupMember.source` marker means
IdP sync only ever touches what IdP sync created.

**6.6 Union-only permissions with no deny.**
*"True takes precedence over False: if any source grants a permission, the user will have it."* To
revoke a capability you must find and clear it in the global defaults and in every group the user
belongs to. That is unauditable at any real scale — "why can this person do that?" has no bounded
answer. If we build R11, it needs an explicit precedence model.

**6.7 Recommending `MERGE_ACCOUNTS_BY_EMAIL` as the workaround for a missing feature.**
Their hardening guide says do not enable it; their own
[dual-OAuth tutorial](https://docs.openwebui.com/tutorials/auth-sso/dual-oauth-configuration/)
requires it, because it is the only way to get two providers. The documented workaround requires the
setting the documented hardening advice forbids. `oidc-conformance-plan.md` Phase 1c already gives us
an explicit three-valued linking policy; keep the strict end of it reachable and defaulted.

**6.8 "ConfigVar" — env read once at first boot, then silently overridden by the database.**
It generates its own troubleshooting page, and [#20830](https://github.com/open-webui/open-webui/issues/20830)
reports the documented escape hatch not working because values were cached in Redis anyway. Our
precedence is DB > env > coded default, *always*, documented, with `is_set` and `requires_restart`
surfaced per key and a config-change audit trail. Keep it exactly as it is.

**6.9 Hard-deleting a user and cascading through their content.**
Their `DELETE /Users/{id}` is documented as "deactivate" and destroys the user's chats, while leaving
files, knowledge bases and notes orphaned. We disable rather than delete
(`directory_sync_service` rule 3) and have a separate deliberate erasure path. Keep both.

**6.10 No-auth single-user mode.**
`WEBUI_AUTH=False` is a one-way door — once users exist you cannot re-enable auth — and it does not
even fully disable the login path ([#15254](https://github.com/open-webui/open-webui/issues/15254)).
Our dev bootstrap already delivers the same convenience without the door.

**6.11 Delegating rate limiting and brute-force protection entirely to the proxy.**
Reasonable for a product that assumes a private network. Wrong for one that ships a `--with-pki` prod
stack and claims FedRAMP-adjacent controls. Keep `auth/lockout.py` and `auth/rate_limit.py`.

**6.12 Treating an issue tracker closure as a resolution.**
Not a feature, but it matters for how we read their signals: [#11883](https://github.com/open-webui/open-webui/issues/11883)
(password reset) was closed six minutes after opening with zero comments and mirrored to a discussion
still open a year later; a complete TOTP implementation offered in
[#16338](https://github.com/open-webui/open-webui/discussions/16338) was never merged. When judging
"the de-facto standard supports X", check the code and the docs, not the issue state.

---

## 7. Suggested sequence

R1–R5 and R8 are each independently shippable. Suggested order, with the reasoning:

1. **R3** (OIDC admission control) — smallest, closes a live security gap, and lands the claim-reading
   code that R2 needs. Can go in the same release as the OIDC plan's Phase 1.
2. **R1** (trusted-header auth) — highest external demand, and it is mostly *generalising* code that
   already exists and is already fail-closed.
3. **R5** (approval state) — small, and it is the landing state R1 and R2 both want for JIT users.
4. **R2** (group mapping) — the big one, and it needs R3's extraction and R5's approval state.
5. **R4** (SCIM) — largest, and best done once R2 has given groups real meaning, since SCIM's
   `/Groups` endpoint has nothing to write to otherwise.
6. **R8** (first-run flow) — last of the core set, deliberately: it is a *presentation* of R3, R5, R6
   and the existing tabs, so building it first would mean building it twice.
7. **R6/R7/R10** — parallel, independent, any time. R6's provider presets are a prerequisite for the
   SSO card in R8, so do that piece early even if the other two wait.

R1, R2, R4 and R8 each warrant their own GitHub issue. R3 and R5 are small enough to ride along with
existing work.

---

## 8. Relationship to `plans/oidc-conformance-plan.md` — two arguments

I have tried not to restate that plan. Two places where I think it should change, stated explicitly
because the brief asked for disagreement rather than silent divergence.

**8.1 Claim → group mapping is deferred without a priority, and it should be sequenced.**
That plan's §8 puts it out of scope: *"a distinct feature: it needs a data model … and a revocation
story."* I agree entirely with the packaging — it is a separate issue with a separate data model — and
disagree with the implied priority. It is the item an enterprise evaluator asks about first, it is the
one gap where Open WebUI is clearly ahead of us on capability rather than on marketing, and the
"revocation story" the plan correctly identifies as the hard part is exactly what R2's
`UserGroupMember.source` marker resolves in one column. Recommendation: file it now, sequence it
immediately after the conformance plan's Phase 1, and do not let it wait for Phases 2–5.

**8.2 Phase 3g leaves "store the ID token, or at least keep it for the session" undecided. Decide it:
server-side, on the `refresh_token` row — never in a cookie.**
RP-initiated logout needs `id_token_hint`, so the ID token must survive the login. Open WebUI's answer
is `ENABLE_OAUTH_ID_TOKEN_COOKIE`, which defaults to **on** and whose own documentation calls it
*"unsafe, not recommended… recommended to set this to `False`"*. That is a decided question with a
published wrong answer available; we should take the other branch. Storing it alongside
`oidc_refresh_token` on the session row costs one column, keeps it out of the browser entirely, and
makes it die with the session — which is already how every other credential in this codebase behaves.

Everything else in that plan I agree with as written, and this document assumes it lands: in
particular the `oidc_*` rename (R3 and R6 use the new spelling), the Phase 1c linking policy (§6.7
depends on it), and the §4 decision that multi-provider is out of scope — which §2.1 of this document
independently confirms is not a parity gap, since Open WebUI cannot federate two generic OIDC issuers
either.

---

## 9. Open questions and what could not be verified

**About Open WebUI** — flagged by the researchers as docs/code conflicts, not settled facts:

1. **`SCIM_ENABLED` vs `ENABLE_SCIM`.** The env reference and the SCIM page say the former; the
   hardening guide and a source read say the latter. Unresolved. Irrelevant to our design, but it means
   any claim about their SCIM defaults should be treated as soft.
2. **Whether OAuth is genuinely admin-UI-editable in current releases.** v0.10.0/v0.11.0 release notes
   say yes with `ENABLE_OAUTH_PERSISTENT_CONFIG=true`;
   [#24033](https://github.com/open-webui/open-webui/issues/24033) reports only LDAP fields appearing.
   Version-dependent. Our claim to be ahead here is safe either way, since ours is unconditional.
3. **Whether admins are exempt from OAuth group sync.** Documented as exempt; no guard found in `main`.
   If they are not exempt, an admin's groups are wiped on every login — which would strengthen §6.5.
4. **`OAUTH_BLOCKED_GROUPS` semantics.** Docs say "denied access"; a source read suggests "exempt from
   the removal sweep", with a fail-open JSON parse. R3 should implement the *documented* meaning (deny),
   which is the useful one.
5. **PKCE validation.** Docs say a non-`S256` value is rejected at startup; source read suggests it is
   silently ignored. Moot for us — ours is on by default and `S256`-only.
6. **First-run signup behaviour.** Their quick-start and hardening guide disagree on whether signup
   auto-disables after the first account.
7. **Whether their recurring `User not found in the LDAP server` reports are AD referral failures.**
   Plausible — no `auto_referrals` handling exists — but never diagnosed, because a catch-all handler
   collapses every LDAP error into one generic 400. Worth noting as a design anti-pattern regardless.

**About our own position** — things I did not verify:

8. **Nothing was run.** The stack was not started and no test was executed; every OpenTranscribe claim
   is from reading this worktree. In particular the assertion in §2.2 that OIDC has no admission
   control is from `keycloak_auth.py` and the absence of any allowed-groups setting in
   `schemas/auth_config.py` — it should be confirmed against a live Keycloak before R3 is written up as
   a security issue.
9. **Whether `--with-keycloak-test` realms currently restrict who can log in.** If the test realm
   happens to gate membership, R3's severity would look lower in dev than it is in production.
10. **Effort sizes are judgement, not estimates.** R4 (SCIM) in particular could be M or XL depending on
    how much of RFC 7644's `PATCH` path operations we implement; Okta and Entra exercise different
    subsets and only real IdP testing will settle it.
11. **Demand.** Nobody has asked us for SCIM or for trusted-header auth. The case for both is
    "enterprises expect it because Open WebUI has it", which is exactly the reasoning the brief asked
    to be applied critically. R1 has independent justification (it is the standard SSO-proxy pattern
    and we support only a narrow PKI slice of it); R4's justification is weaker until a deployment asks.
