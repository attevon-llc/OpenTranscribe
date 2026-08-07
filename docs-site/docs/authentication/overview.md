---
sidebar_position: 1
title: Authentication Overview
---

# Authentication Overview

OpenTranscribe has four identity sources — **local passwords**, **LDAP/Active Directory**,
**OpenID Connect**, and **PKI/X.509 certificates**. They run *simultaneously*; each account
records which one owns it in `user.auth_type`, so a deployment can have directory users,
certificate users and a handful of local accounts at the same time.

Everything on this page is configured in **Settings → Authentication**, which is
**super_admin-only**. Configuration is stored in the `auth_config` table, secrets encrypted
with AES-256-GCM, and takes effect without a restart.

## Where a setting comes from

Every auth setting resolves in the same order:

> **database (`auth_config`) → environment variable → coded default**

Once you save a panel, the database value wins. The environment variable is a bootstrap seed
and a fallback, not an override. Two exceptions are called out where they apply:
`jwt_access_token_expire_minutes` and `jwt_refresh_token_expire_days` are marked
*requires restart* (cookie max-age is computed from them at import time).

Secrets are never returned by the API. A sensitive key comes back with `config_value: null`
and an `is_set` boolean; the panel renders "a secret is configured — leave blank to keep it".

## Identity sources

| Source | `auth_type` | Guide |
|---|---|---|
| Local password | `local` | this page |
| LDAP / Active Directory | `ldap` | [LDAP setup](./ldap) |
| OpenID Connect (Keycloak, Authentik, Okta, Entra ID, Authelia, Auth0, Zitadel…) | `oidc` | [OIDC setup](./oidc) |
| PKI / X.509 client certificates | `pki` | [PKI setup](./pki) |

Directory groups can be mapped onto in-app groups and an in-app role — see
[IdP group mapping](./groups).

## The identity-source model

The question this answers is: *"our IdP owns identity — why can people still sign in with a
password, and why can they still self-register?"*

| Setting | Where | Meaning |
|---|---|---|
| `local_enabled` | Authentication → Local | May accounts holding a local password authenticate at all |
| `allow_registration` | Authentication → Local | May anyone create their own account |
| `user.auth_type` | Settings → Users | Which source owns *this* account |
| `user.allow_local_fallback` | Settings → Users (super_admin only) | May *this* external account also use a local password |
| `pki_allow_password_fallback` | Authentication → PKI | Deployment **ceiling** over the per-user flag, for `pki` accounts |

Rules the code enforces:

- **`allow_registration` cannot be true while `local_enabled` is false.** Self-registration
  creates `auth_type='local'` accounts with a local password; with local login off, every
  account it minted could never sign in. The API rejects the combination with a 400 that names
  which switch to change first — and it re-checks the *resulting* state, so you cannot assemble
  the rejected combination one save at a time.
- **`local_enabled` does not hide the username/password form.** LDAP authenticates through the
  same form, so the login page renders it whenever `local_enabled` **or** `ldap_enabled` is on.
- **Local password fallback is one rule, in one place.** `local` always may; `ldap` never may
  (no local password is stored for a directory account, and the per-user flag does not override
  that); `pki` and `oidc` may only with the per-user opt-in, and for `pki` only if the
  deployment ceiling also allows it. An unrecognised `auth_type` is refused.
- **An external identity may not take over an existing account by email coincidence.** See
  [Account linking](#account-linking) below.

### The super_admin exemption

**An active `super_admin` that has a local password path is never blocked by the
deployment's own identity-source policy** — neither `local_enabled` nor
`pki_allow_password_fallback` applies to it. ("Has a local password path" means
`auth_type='local'`, or an external `auth_type` with `allow_local_fallback` set; a `pki`
super_admin with the per-user flag off still has no password to fall back to.)

A `super_admin` with `allow_local_fallback` set is additionally **exempt from account
lockout** — an emergency-access account that an attacker can lock out is the outage the
exemption exists to prevent (NIST AC-7 permits this). Attempts are still recorded for audit.

This is deliberate and load-bearing, not a convenience: authentication configuration is itself
super_admin-gated. Without the exemption, a deployment that turned off local login while its
IdP was misconfigured would have no way back into the screen that undoes it.

## Privilege tiers

`user.role` is the **sole** authorization truth — `is_superuser` is a derived mirror of
`role == super_admin`, enforced by a database CHECK constraint. The dividing rule:

> **Anything that changes how the deployment runs, or that stores infrastructure credentials,
> is `super_admin`. Anything that manages users and their content is `admin`.**

| Tier | Covers |
|---|---|
| `user` | Own content, own settings, own MFA |
| `admin` | User accounts, tasks, search and speaker maintenance, data integrity, retention |
| `super_admin` | Authentication config, role changes, audit log, ASR provider, engine settings, backups, media mirror, watch sources, redaction policy |

:::warning Changed in v0.5.0
Six panels moved from `admin` to `super_admin`: **ASR provider**, **Engine configuration**,
**Backups**, **Media Mirror**, **Watch sources**, and the **Redaction policy** floor. If a
plain `admin` administers any of those today, promote them before upgrading.
:::

- **Creating more super_admins is a UI action.** Settings → Users → Role, visible only to a
  super_admin, backed by an audited `PUT /api/admin/users/{uuid}/role`.
- **The last super_admin cannot be demoted or deleted.**
- **External identity providers grant at most `admin`.** `super_admin` is local-only, by
  design — it is the break-glass account for exactly the IdP that is failing. The cap is
  enforced in the service, in the Pydantic schema, *and* by a database CHECK constraint on
  the group-mapping table.

## Admission control

Authentication answers *"are you who you say you are"*. **Admission** answers *"does this
deployment want you"*. They used to be the same question for OIDC, which is how just-in-time
provisioning ended up minting an account for every identity in a corporate tenant on first
login.

| Control | Applies to | Empty / off means |
|---|---|---|
| `ldap_user_groups` | LDAP | No group requirement |
| `oidc_allowed_groups` / `oidc_blocked_groups` | OIDC | Admit everyone |
| `require_account_approval` (Authentication → Local) | Self-registration **and** every external JIT path | Accounts are usable immediately |

- **An empty allow-list admits everyone.** Reading it the other way would lock out every
  existing deployment on upgrade; only a non-empty list restricts.
- **Blocked means denied**, not "exempt from the allow-list", and is evaluated first.
- The OIDC group lists are **semicolon-delimited** — a directory group value is a DN and
  contains commas. Matching is case-insensitive exact, against the claim named by
  `oidc_roles_claim`.
- The check re-runs on **every** login, so removing someone from the group locks them out
  rather than only affecting new users.
- Refusals return the same generic 401 an unusable token gets — a distinct message would be an
  account-existence oracle. The reason goes to the audit log.

### Approval queue

`require_account_approval` gives a newly provisioned account `approval_status = pending` instead
of making it usable. This is **not** the same as `is_active`: deactivation revokes an account
that was once usable, approval gates one that never has been.

Enforcement is the same lifecycle gate as the rest of this section — **403** with
`detail.code` of `account_pending_approval` or `account_rejected`, not a second mechanism.
Administrators clear the queue at `GET`/`POST /api/admin/user-approvals` (**admin** tier —
managing users; the switch that *creates* the queue is auth config, hence super_admin).

Turning the setting off releases pending accounts. **Rejected stays rejected**, and rejection
never deletes the row. The bootstrap super_admin is never created pending, so a first-boot
deployment cannot lock itself out of its own queue.

## Account lifecycle

### Invitations

Settings → Users → **Invite**. An admin names an address plus the target `role` and
`auth_type`; the recipient gets an emailed link, proves control of the address, and chooses
their own credential — or is handed straight to the IdP when `auth_type` is external.

This is the supported way to onboard when self-registration is off. Invitation tokens are
SHA-256 hashed at rest, single-use, and expiring; every rejection (unknown, expired, revoked,
already used, address already registered) returns exactly the same message.

### Email verification

`require_email_verification` (Authentication → Local, category `local`) gates **local password
login only**. An account whose identity lives in LDAP/OIDC/PKI has its address asserted by the
provider, so blocking those logins here would second-guess the IdP. Verification tokens expire
after 24 hours and are rate-limited to 3 issues per hour.

:::note
There is currently no toggle for `require_email_verification` in the admin panel. Set it with
`PUT /api/admin/auth-config/local` (`{"require_email_verification": true}`).
:::

### Forced password change

`user.must_change_password` confines the account to `PUT /users/me` (the self-service password
change) and the logout routes. Everything else returns **403** with a machine-readable
`detail.code == "password_change_required"`. The flag is set by an admin password reset, and
automatically when a local password exceeds `password_max_age_days`.

An account whose `password_changed_at` is NULL is **not** forced through a change — the column
was never stamped on older accounts, and forcing all of them at once would be a self-inflicted
outage. The backend logs a warning naming the account instead.

### Account expiry

`user.account_expires_at` time-boxes an account (a contractor, an auditor). Past that instant
every request returns **403** with `detail.code == "account_expired"`. There is no exempt
route: unlike a forced password change there is no self-service remedy, so the caller is not
routed to a screen that cannot help them. The denial is audited.

### Login banner acknowledgment (FedRAMP AC-8)

With `login_banner_enabled` on, a user who has not acknowledged the banner is refused on every
non-exempt route with **403** `detail.code == "banner_acknowledgment_required"`. The exempt set
is the banner itself, the acknowledgment endpoint, and both logout routes.

**An acknowledgment expires when the banner text changes.** The comparison is against the
`login_banner_text` config row's `updated_at`, so editing the wording re-asks everyone. A user
who accepted "UNCLASSIFIED — monitoring in effect" has not accepted a later
"SECRET — no personal use".

### Sessions

**A session *is* a `refresh_token` row.** Concurrent-session limits, rotation, revocation and
the idle/absolute timeouts all key off those rows; there is no second session store.

| Setting | Default | Bounds |
|---|---|---|
| `jwt_access_token_expire_minutes` *(requires restart)* | 60 | 1–1440 |
| `jwt_refresh_token_expire_days` *(requires restart)* | 7 | 1–365 |
| `session_idle_timeout_minutes` | 15 | 1–1440 |
| `session_absolute_timeout_minutes` | 480 | 1–10080 |
| `max_concurrent_sessions` | 5 | 0–1000 (0 = unlimited) |
| `concurrent_session_policy` | `terminate_oldest` | `terminate_oldest` \| `reject` |

- **Idle timeout is checked at refresh, not per request.** Polling endpoints (progress,
  notifications, task status) and WebSocket keepalives would reset a per-request activity clock
  continuously, so the control would read as satisfied and never fire. The granularity is
  therefore one access-token lifetime.
- **The absolute timeout is carried forward, never recomputed.** It is the only thing that caps
  a client that refreshes forever. Both timeout columns are nullable and un-backfilled — NULL
  means "no cap recorded", is treated as valid, and is stamped on the row's first rotation, so
  upgrading does not sign everyone out a second time.
- **Hitting the concurrent cap is audited**, whether the policy evicted the oldest session or
  rejected the new one (`reject` returns 429).
- Users see and revoke their own sessions in **Settings → Profile → Active sessions**; an admin
  sees and revokes another user's via `GET`/`DELETE /api/admin/users/{uuid}/sessions`.

### Directory-sync deprovisioning (LDAP)

A periodic sweep asks the directory whether each `auth_type='ldap'` account still exists and
is still enabled, disables the ones that are gone, **and revokes their sessions**. See
[LDAP → Directory sync](./ldap#directory-sync-and-deprovisioning). Disabling without revoking
would leave a refresh token rotating indefinitely, so the revocation is the half that actually
closes the hole.

## Account linking

Every external source resolves a user by *its own identifier* first (`ldap_uid`,
`oidc_subject`, `pki_subject_dn`) and only then falls back to matching on email address. That
fallback is an account-takeover vector, because the address is an attribute of the external
source: whoever can write it — a directory administrator, a self-service directory, anyone who
can get a certificate issued — could point it at an existing account and inherit it.

One rule, used by LDAP, OIDC and PKI alike:

1. Link on an email match **only when the source asserts the address is verified**.
2. **Never** link a `super_admin` account by email, verified or not.

A refusal **fails the login** — it does not fall through to creating a second account, because
that would either collide on the unique email index or leave two accounts for one person. It is
audited as an `AUTH_LOGIN_FAILURE` with `error_code ACCOUNT_LINK_REFUSED`, and it surfaces to
the caller as the *same* generic failure that path returns for a bad credential, so it cannot be
used to probe which addresses exist.

**Operator remedy**: link the account deliberately instead of by coincidence — set the
account's provider identifier from the admin UI, or change one of the two addresses.

:::warning Behaviour change in v0.5.0
**Authentik hardcodes `email_verified` to `false` and Entra ID omits the claim entirely.** On
those providers an OIDC login will no longer take over a pre-existing local account with the
same address. Use one of the two remedies above.
:::

## Multi-factor authentication

TOTP per RFC 6238 (Google Authenticator, Authy, Microsoft Authenticator), with one-time backup
codes.

| Setting | Default |
|---|---|
| `mfa_enabled` | `false` |
| `mfa_required` | `false` |
| `mfa_issuer_name` | `OpenTranscribe` |
| `mfa_backup_code_count` | 10 |
| `mfa_token_expire_minutes` | 5 |

- **`mfa_required` is enforced at the server**, not just in the SPA. A user who has not enrolled
  receives a short-lived, enrolment-scoped half-token that authorizes only `/mfa/setup` and
  `/mfa/verify-setup`; completing enrolment issues the session. An API client that ignores the
  hint gets nothing.
- **Every token carries a purpose claim and every consumer checks it.** Access, refresh and MFA
  tokens are signed with the same key, so the claim is the only thing separating them.
- **PKI and OIDC users bypass local MFA only when they used their native method.** If they fall
  back to a local password, MFA applies.
- TOTP codes are single-use; the MFA half-token's JTI is blacklisted after verification.

## Password policy, lockout, rate limiting

| Setting | Default | Bounds |
|---|---|---|
| `password_policy_enabled` | `true` | |
| `password_min_length` | 12 | 8–128 |
| `password_require_uppercase` / `_lowercase` / `_digit` / `_special` | `true` | |
| `password_history_count` | 24 | 0–100 (0 disables) |
| `password_max_age_days` | 60 | 0–3650 (0 = never expires) |
| `account_lockout_enabled` | `true` | |
| `account_lockout_threshold` | 5 | 1–1000 |
| `account_lockout_duration_minutes` | 15 | 1–10080 |
| `account_lockout_progressive` | `true` | |
| `account_lockout_max_duration_minutes` | 1440 | 1–525600 |
| `rate_limit_enabled` | `true` | |
| `rate_limit_auth_per_minute` | 10 | 1–10000 |

- Lockout is keyed on a **canonical identifier per account**, so an account reachable by both
  an email address and an LDAP uid gets one budget, not two.
- Rate limiting is per-IP, resolved through the trusted-proxy chain.
- The dev stack relaxes both (`docker-compose.override.yml`: 120 requests/min, lockout threshold
  100). Production never loads that overlay.

## Transactional auth email

Password resets, invitations and verification links need a working mail transport. Which one is
an explicit choice: **Settings → Watch Sources → Email configurations** holds the provider rows,
and one of them is *designated* to carry authentication mail
(`PUT /api/admin/auth-config/email/designation`, super_admin).

- Clearing the designation falls back to the `SMTP_*` environment transport.
- A designation naming a row that does not exist, or a disabled row, is **rejected at write
  time** rather than failing silently later.
- Deleting or disabling the designated row is refused while it holds the designation.

## Audit

Authentication and administrative events go to an **OpenSearch-backed** audit log, viewable at
Settings → Audit Logs (super_admin). Separately, **Settings → Authentication → Audit** shows
changes to the auth configuration itself, read from the `auth_config_audit` table in Postgres —
including *who* made each change.

## Compliance

| Requirement | Implementation |
|---|---|
| FedRAMP IA-2 | MFA (server-enforced), PKI authentication |
| FedRAMP IA-5(1) | Password policy, history, expiry → forced change |
| FedRAMP AC-2 | Account expiry, invitations, directory-sync deprovisioning |
| FedRAMP AC-2(3) | `last_login_at` stamped on every successful authentication |
| FedRAMP AC-7 / NIST 800-53 AC-7 | Progressive account lockout |
| FedRAMP AC-8 | Login banner, **enforced** acknowledgment |
| FedRAMP AC-10 | Concurrent-session limit, audited |
| FedRAMP AC-12 | Refresh-token rotation, revocation, idle + absolute timeouts |
| FedRAMP AU-2 / AU-3 | Audit logging of authentication and admin events |
| FedRAMP SC-28 | Auth-config secrets encrypted at rest (AES-256-GCM) |

## Next steps

- [LDAP / Active Directory](./ldap)
- [OpenID Connect](./oidc)
- [PKI / X.509](./pki)
- [IdP group mapping](./groups)
- [Environment variables](../configuration/environment-variables.md)
