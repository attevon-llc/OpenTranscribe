# Auth provider compatibility — the enterprise matrix

What OpenTranscribe can authenticate against, what it cannot, and what it would take. Written to
answer one procurement question: *"does it work with our identity provider?"*

The goal is **not** to support every provider by name. It is to support the four protocols
enterprises actually deploy, well enough that any conformant provider works without us shipping
code for it — plus presets so an operator does not have to know that Keycloak nests roles under
`realm_access` and Authentik does not.

## Where we are

| Method | State | Notes |
|---|---|---|
| Local password | shipped | policy, history, expiry, lockout, TOTP MFA, invitations, email verification |
| LDAP / AD | shipped | incl. AD nested groups via `LDAP_MATCHING_RULE_IN_CHAIN` (OID 1.2.840.113556.1.4.1941), admission via `ldap_user_groups`, deprovisioning sweep |
| OIDC | shipped | discovery, PKCE S256, ID-token-only validation, asymmetric-alg allow-list, configurable claims, userinfo fallback, admission control |
| PKI / X.509 | shipped | CAC + PIV CN parsing, OCSP/CRL from certificate extensions, fail-closed proxy trust |
| Trusted-header proxy | in flight | fail-closed CIDR allowlist, optional shared secret, role header capped at admin |
| SCIM 2.0 | in flight | Users + Groups, hashed bearer tokens |
| SAML 2.0 | approved, not built | library only — never hand-roll |

**Untested against a real instance: everything except Keycloak and LLDAP.** That is the honest
headline. We have Keycloak and LLDAP containers; every other provider claim is protocol reasoning,
not observation.

## The four protocols, and who needs which

- **OIDC** — the modern default. Covers Entra ID, Okta, Auth0, Google Workspace, Keycloak,
  Authentik, Zitadel, PingFederate, OneLogin, JumpCloud, AWS Cognito, GitLab.
- **SAML 2.0** — the one that still wins deals. ADFS, Shibboleth (universities), older Entra
  configurations, and a long tail of enterprises whose IdP team will not stand up an OIDC client.
  Usually the second procurement question after "do you do SSO".
- **LDAP / AD** — on-premise, air-gapped, and anywhere the directory *is* the system of record.
  Our reporter's deployment is exactly this.
- **Trusted-header proxy** — the escape hatch. If an enterprise fronts everything with Authelia,
  oauth2-proxy, Cloudflare Access, Pomerium or Teleport, this makes us compatible with *whatever
  that proxy speaks*, including protocols we will never implement. Highest compatibility-per-line
  in the whole matrix.

Those four cover essentially the entire enterprise market. Everything below is about making them
work without per-provider code.

## Provider matrix

### OIDC

| Provider | Expected | Known trap |
|---|---|---|
| Keycloak | works (tested) | roles nested under `realm_access.roles` — our current default |
| Authentik | works, **untested** | emits `groups`; **hardcodes `email_verified: false`** |
| Entra ID | works, untested | groups are **object GUIDs, not names**; **groups-overage** above ~150–200 groups replaces the claim with `_claim_names`/`_claim_sources` and requires a Graph call |
| Okta | works, untested | groups claim must be explicitly added to the token in the app config |
| Google Workspace | works, untested | **no groups in the token at all** — needs Directory API |
| Auth0 | works, untested | groups/roles only via a custom action + namespaced claim |
| PingFederate / OneLogin / JumpCloud / Zitadel / Cognito | expected to work | standard discovery |
| GitLab | works, untested | standard OIDC |
| **GitHub** | **does not work** | OAuth2 only — **no `id_token`**, no discovery document |

Two structural gaps fall out of that table:

1. **Claim location differs per provider and fails silently.** Wrong claim name → login succeeds,
   groups and roles are empty, nobody notices until permissions are wrong. Fixed by presets +
   showing which claims the token actually carried.
2. **Some providers cannot put groups in a token at all** (Google Workspace always; Entra above the
   overage threshold). A token-only design cannot serve them. Either document the limitation
   honestly or add an optional directory-lookup step.

### SAML 2.0 (approved, unbuilt)

ADFS, Shibboleth, Okta, Entra, PingFederate, OneLogin, Google Workspace. One implementation covers
all of them — SAML's interop story is better than OIDC's for group claims, because attribute
statements are part of the assertion rather than provider-specific extensions.

**Non-negotiable: use `python3-saml` or `pysaml2`.** XML signature wrapping has produced critical
authentication bypasses in Shibboleth and many commercial SPs. Signature validation and XML
canonicalisation are the most dangerous code in this entire domain to write yourself.

### LDAP

| Directory | State |
|---|---|
| Active Directory | supported, incl. nested groups |
| OpenLDAP / LLDAP | supported (LLDAP tested) |
| FreeIPA / 389DS / Oracle DSEE | expected — standard LDAPv3 |
| JumpCloud LDAP | expected |

Nested-group support is AD-specific (`LDAP_MATCHING_RULE_IN_CHAIN`). Other directories need
`memberOf` overlay or a recursive walk; today `ldap_recursive_groups` silently does nothing
non-AD. Worth stating in docs rather than fixing — AD is the overwhelming majority.

### Proxy

oauth2-proxy, Authelia, Cloudflare Access, Pomerium, Teleport, AWS ALB OIDC, Istio/Envoy ext_authz.
All assert identity in headers; our contract is header names + a fail-closed CIDR allowlist +
optional shared secret. One implementation, unlimited providers.

### SCIM 2.0

Okta, Entra, OneLogin, JumpCloud all speak SCIM for provisioning. This is what turns "SSO works"
into "IT never has to touch it" — and it is what enterprise procurement checklists ask for by name.

## Plan, in priority order

Priority is by **deals unblocked per unit of work**, not by interest.

**P1 — make the providers we already claim actually work**
1. Provider presets (Keycloak / Authentik / Entra / Okta / Google / Generic) filling claim names,
   scopes and discovery URL shape. Removes the single biggest silent-failure class.
2. Show the claims the IdP actually returned, in Test Connection and on first login. An operator
   should *see* that `groups` exists and `realm_access.roles` does not.
3. The Authentik `email_verified` remedy — currently documented and unimplementable.
4. A real Authentik test container alongside Keycloak and LLDAP.

**P2 — close the protocol gap**
5. SAML 2.0 (approved). Reuses the whole policy layer; the marginal cost is protocol binding, SP
   metadata, ACS, SLO and certificate rotation.
6. Trusted-header proxy (in flight) — finish and document per-proxy recipes.

**P3 — enterprise operations**
7. SCIM 2.0 (in flight) — verify against Okta *and* Entra, which exercise different `PATCH` subsets.
8. Entra groups-overage handling, or an explicit documented limit.
9. Optional directory-lookup for group membership (Google Workspace Directory API, Entra Graph) for
   the providers that cannot put groups in a token.

**P4 — long tail**
10. Generic OAuth2 (non-OIDC) for GitHub and similar: no `id_token`, so identity comes from a
    provider-specific userinfo call. Deliberately last — it is a different trust model and mostly
    matters for developer-facing products, not enterprise.

## What we should promise

Only what we have watched work. Suggested language once P1 lands:

- **Verified**: Keycloak, Authentik, LLDAP/OpenLDAP, Active Directory.
- **Standards-supported**: any OIDC provider with a discovery document; any SAML 2.0 IdP; any
  LDAPv3 directory; any authenticating reverse proxy.
- **Known unsupported**: GitHub OAuth2; group claims on Google Workspace and on Entra tenants above
  the overage threshold.

An enterprise buyer will forgive a documented limitation. They will not forgive discovering it in
production — and the silent-empty-groups failure is exactly the kind that surfaces after rollout.
