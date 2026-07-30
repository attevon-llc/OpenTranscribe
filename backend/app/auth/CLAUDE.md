# app/auth — hybrid authentication (local · LDAP · Keycloak · PKI)

## Purpose

Multiple auth methods run **simultaneously**, selected per-user by `User.auth_type`.
`AUTH_TYPE` may be a single value or comma-separated (`local`, `ldap`, `keycloak`, `pki` —
`constants.py:VALID_AUTH_TYPES`). Configure in the Admin UI (Settings → Authentication):
**DB `auth_config` wins over `.env`, which wins over the coded default**
(`services/auth_config_service.py`). Endpoints live in `api/endpoints/auth.py` +
`auth_config.py`, not here.

## Key files

- `roles.py` — the authorization contract (read this first, it's 35 lines).
- `token_service.py` — JWT issue/verify, `rotate_refresh_token`, `revoke_all_user_tokens`,
  Redis JTI revocation list + `refresh_token.revoked_at`.
- `mfa.py` (`MFAService`) · `lockout.py` (progressive, per-identifier) · `rate_limit.py`
  (slowapi, per-IP with trusted-proxy parsing) · `session.py` (`SessionManager`,
  `OIDCStateStore`) · `audit.py` (`AuditLogger`, `AuditEventType`).
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

## Gotchas

- **PKI/Keycloak users bypass MFA only when they used their native method.** If they fall
  back to a local password, MFA still applies (`auth.py`, the `actual_auth_method` check).
  `AUTH_TYPES_SUPPORT_LOCAL_FALLBACK = [pki, keycloak]`; LDAP never has a local password.
- **Negative login tests MUST use a nonexistent account** — never a wrong password for
  `admin@example.com`. Lockout is progressive per-account and poisons the whole suite.
- **Dev relaxes auth limits** (`docker-compose.override.yml`: `RATE_LIMIT_AUTH_PER_MINUTE`
  120, `ACCOUNT_LOCKOUT_THRESHOLD` 100, `..._DURATION_MINUTES` 1; `DEV_*` tunable in `.env`).
  Prod keeps strict values — the override is never loaded there. Env changes need a
  container **recreate**, not `restart-backend`.
- Local IdPs for testing: `--with-ldap-test` (LDAP :3890, UI :17170, `admin`/`admin_password`),
  `--with-keycloak-test` (:8180, `admin`/`admin`), `./opentr.sh start prod --build --with-pki`
  (mTLS at https://localhost:5182 — **prod-only, Vite can't do mTLS**). Client certs:
  `scripts/pki/test-certs/clients/*.p12`.
- Setup docs: `docs/PKI_SETUP.md`, `docs/LDAP_AUTH.md`, `docs/KEYCLOAK_SETUP.md`,
  `docs-site/docs/authentication/{overview,pki,ldap,keycloak}.md`.
