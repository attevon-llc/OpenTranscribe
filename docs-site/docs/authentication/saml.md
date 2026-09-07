---
sidebar_label: SAML 2.0
sidebar_position: 3.5
---

# SAML 2.0 authentication

For identity providers that only speak SAML (ADFS, Shibboleth, Okta-classic and similar
enterprise deployments), OpenTranscribe acts as a SAML 2.0 Service Provider.

Configure it at **Settings → Authentication → SAML** (super_admin); values are stored in
`auth_config` and take effect without a restart, with the `SAML_*` environment variables as a
bootstrap seed and fallback.

## How it works

Assertion parsing and XML signature verification are handled entirely by `python3-saml` — never
hand-rolled — so the class of XML-signature-wrapping bugs that has affected Shibboleth and
commercial SPs in the past is out of scope here by construction.

Unlike OIDC's callback, the IdP's own endpoints (ACS, SLS) are browser POST/redirect targets: on
success they finish with an HTTP redirect to the SPA plus httpOnly session cookies already set,
not JSON for a `fetch()` caller.

| Endpoint | Purpose |
|---|---|
| `GET /api/auth/saml/metadata` | This SP's metadata document (public, no secret in it) — give this URL, or its content, to your IdP |
| `GET /api/auth/saml/login` | SP-initiated login: redirects the browser to the IdP |
| `POST /api/auth/saml/acs` | Assertion Consumer Service — the IdP POSTs the assertion here |
| `GET`/`POST /api/auth/saml/sls` | Single Logout Service — the IdP's logout callback |

## Configuration reference

| Field | Config key | Default |
|---|---|---|
| Enabled | `saml_enabled` | `false` |
| SP Entity ID | `saml_sp_entity_id` | — |
| SP ACS URL | `saml_sp_acs_url` | `http://localhost:5173/api/auth/saml/acs` |
| SP SLS URL | `saml_sp_sls_url` | `http://localhost:5173/api/auth/saml/sls` |
| SP signing certificate | `saml_sp_x509_cert` | — (required only when `saml_sign_authn_requests=true`) |
| SP signing private key *(sensitive)* | `saml_sp_private_key` | — |
| IdP Entity ID | `saml_idp_entity_id` | — |
| IdP SSO URL | `saml_idp_sso_url` | — |
| IdP SLO URL | `saml_idp_slo_url` | — |
| IdP signing certificate | `saml_idp_x509_cert` | — (required to enable SAML) |
| Want assertions signed | `saml_want_assertions_signed` | `true` |
| Want messages signed | `saml_want_messages_signed` | `true` |
| Sign AuthnRequests | `saml_sign_authn_requests` | `false` |
| Email attribute | `saml_email_attribute` | `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress` |
| Name attribute | `saml_name_attribute` | `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name` |
| Groups attribute | `saml_groups_attribute` | `groups` |
| Admin group | `saml_admin_group` | — |
| Allowed groups | `saml_allowed_groups` | — (empty admits everyone) |
| Blocked groups | `saml_blocked_groups` | — (evaluated first) |

**`saml_idp_x509_cert` is what makes assertion signature verification real.** Leaving it blank
refuses to enable SAML rather than accepting an unverifiable assertion. `saml_sp_acs_url` and
`saml_sp_sls_url` must be reachable from the user's *browser* (the IdP redirects/POSTs there),
and must match byte-for-byte what is registered at the IdP.

## Admission control

`saml_allowed_groups` / `saml_blocked_groups` reuse the same allow/block evaluation as OIDC
(`oidc.admission.check_group_admission` — the group-list syntax is protocol-agnostic): both
lists are **semicolon-delimited** (a directory group value is often a DN and contains commas),
matching is case-insensitive exact, an **empty allow-list admits everyone**, and a block is
evaluated **first** — "blocked" means refused, not "exempt from the allow-list".

## Provisioning, roles and account linking

A user who authenticates via SAML is created on first login (`auth_type='saml'`). `saml_admin_group`
is the legacy single-role signal, capped at `admin` — `super_admin` is local-only, the same rule
as every other external source.

**SAML always asserts `email_verified=False`** — SAML has no standard "this address is verified"
claim, so an email-match account takeover is refused unconditionally here rather than being an
admin-togglable setting someone could open by mistake. Link a SAML identity to an existing
account deliberately (set the account's SAML identifier from the admin UI) instead.

:::note[Narrower than OIDC's provisioning]
SAML does not (yet) extend the `group_mapping` table that LDAP/OIDC/proxy use (its `source`
column is CHECK-constrained to a closed set — widening it is a separate, independently
reviewable schema change), and does not track `(NameID, SessionIndex)` per session — so
SP-initiated logout ends only the local OpenTranscribe session rather than also notifying the
IdP. Both are documented follow-up scope, not silent gaps.
:::

## MFA

A SAML user bypasses local MFA only when they authenticated through the IdP. If the account has
`allow_local_fallback` and signs in with a local password instead, local MFA still applies.

## Local testing

There is no `--with-saml-test` local IdP container yet (unlike `--with-ldap-test`,
`--with-keycloak-test`, `--with-authentik-test`) — verifying against a real SAML IdP (e.g.
SimpleSAMLphp or a Keycloak SAML client) is deferred scope for now.

## Related

- [Authentication overview](./overview)
- [IdP group mapping](./groups) (LDAP/OIDC/proxy only — SAML is not yet a group-mapping source)
