---
sidebar_label: Trusted-header (reverse proxy)
sidebar_position: 6
---

# Trusted-header (reverse proxy) authentication

For deployments where an authenticating reverse proxy — oauth2-proxy, Authelia, Cloudflare
Access, an enterprise SSO gateway — already terminates authentication in front of
OpenTranscribe and asserts the resulting identity in a request header.

Configure it at **Settings → Authentication → Proxy** (super_admin); values are stored in
`auth_config` and take effect without a restart, with the `PROXY_*` environment variables as a
bootstrap seed and — for `proxy_enabled` / `proxy_trusted_proxies` specifically — what the
startup guard can see before a database session exists.

`pki_mode='header'` (see [PKI](./pki)) is a specialisation of the same trust mechanism: a proxy
vouching for a certificate subject DN instead of an email address. There is exactly one
trusted-peer implementation, shared by both.

## Trust configuration — read this first

:::danger[`proxy_trusted_proxies` is required whenever proxy auth is enabled]
An **empty** allowlist **refuses every header-sourced assertion outright** — it does not mean
"trust everyone". A hardened deployment additionally refuses to *start* with `PROXY_ENABLED` and
no allowlist configured.

Set it to the address the **backend** sees the reverse proxy arrive from (not the client's
address), e.g. `127.0.0.1,10.0.0.0/8`. Single addresses and CIDR ranges are both accepted, and
only the **immediate peer** is checked — never `X-Forwarded-For`, which the client can set
itself.
:::

## Configuration reference

| Field | Config key | Default |
|---|---|---|
| Enabled | `proxy_enabled` | `false` |
| Trusted proxies | `proxy_trusted_proxies` | — (**required when enabled**) |
| Shared secret *(sensitive)* | `proxy_shared_secret` | — |
| Email header | `proxy_email_header` | `X-Forwarded-Email` |
| Name header | `proxy_name_header` | `X-Forwarded-User` |
| Groups header | `proxy_groups_header` | — (empty = proxy does not manage groups) |
| Groups separator | `proxy_groups_separator` | `,` |
| Role header | `proxy_role_header` | — (empty = header-driven privilege is off) |
| Allowed domains | `proxy_allowed_domains` | — (empty admits every domain) |
| JIT provisioning | `proxy_jit_provisioning` | `true` |

The email header is the identity assertion; everything else is optional. `POST
/api/auth/proxy/authenticate` mints a normal session (a `refresh_token` row) from it, so idle/
absolute timeout, the concurrent-session cap, revocation and the sessions UI all apply exactly
as they do to any other login.

### Shared secret (defence in depth)

`proxy_shared_secret`, when set, must be sent by the proxy in `X-OpenTranscribe-Proxy-Secret`
and is compared in constant time. With it configured, an allow-listed proxy that has been
misconfigured to pass client-supplied headers straight through is not, by itself, sufficient
for account takeover.

### Groups and role headers

- **`proxy_groups_header` absent vs. empty are different instructions.** An absent header means
  "I do not manage your groups" and skips membership reconciliation entirely; an empty header
  reconciles membership to empty. Use `;` as `proxy_groups_separator` when the values are LDAP
  DNs, which contain commas. See [IdP group mapping](./groups) — proxy-sourced mappings
  (`source: "proxy"`) are created via `/api/admin/group-mappings`; the mapping admin panel only
  offers `ldap`/`oidc` as the source today.
- **`proxy_role_header` is opt-in and capped at `admin`.** Empty means header-driven privilege is
  off (the default); `super_admin` is unreachable through it under any configuration. A
  deployment that never opts in cannot have an existing `admin` silently demoted by a login,
  because `apply_role=False` when neither a role header nor a group assertion is present.

### Allowed domains

`proxy_allowed_domains` is a comma-separated allowlist of email domains. Empty admits every
domain the proxy asserts.

## Per-request consistency

Every request from a session created via `auth_type='proxy'` is re-checked against the current
header assertion. If a trusted peer now asserts a **different** address than the session's user,
the session is **revoked** (not just refused) — narrowed to only `proxy`-type accounts, only a
header actually from a trusted peer, and treating **absence** of the header as not an assertion
(so a proxy that only asserts identity at login, not on every request, does not revoke its own
sessions).

## Account linking

A header-asserted email that matches no existing account, but matches one linked to a different
source, is only linked when the assertion is treated as verified — proxy assertions are treated
as verified by definition, since the address *is* the assertion here and there is no second
identifier to bind on — and **never** to a `super_admin`. See
[Account linking](./overview#account-linking).

## MFA

A proxy-authenticated user bypasses local MFA, since the reverse proxy is expected to own
authentication (including any second factor) itself.

## Related

- [Authentication overview](./overview)
- [PKI / X.509](./pki) — `pki_mode='header'` reuses this trust mechanism for certificate DNs
- [IdP group mapping](./groups)
