# auth/proxy — trusted-header auth (`auth_type='proxy'`)

Moved intact from `backend/app/auth/CLAUDE.md`.

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
