---
sidebar_position: 7
title: Security Hardening
description: Security best practices and compliance checklist
---

# Security Hardening

This guide covers security best practices for production OpenTranscribe deployments, including authentication hardening, network security, data protection, and compliance considerations.

## Security Architecture

```mermaid
graph TB
    subgraph Network["Network Layer"]
        FW[Firewall / Reverse Proxy]
        TLS[TLS 1.2+ Termination]
        CORS[CORS Policy]
    end

    subgraph Container["Container Layer"]
        NR[Non-Root User - appuser UID 1000]
        ISO[Docker Network Isolation]
        IMG[Minimal Base Images]
        HC[Health Checks]
    end

    subgraph Application["Application Layer"]
        AUTH[Authentication - Local/LDAP/OIDC/SAML/PKI/Trusted header]
        MFA[Multi-Factor Authentication]
        RBAC[Role-Based Access Control]
        RL[Rate Limiting]
        AUDIT[Audit Logging]
    end

    subgraph Data["Data Layer"]
        EAR[Encryption at Rest - AES-256-GCM]
        EIT[Encryption in Transit - TLS]
        HASH[Password Hashing - PBKDF2-SHA256]
        BACKUP[Encrypted Backups]
    end

    FW --> TLS --> CORS --> NR --> ISO --> IMG
    IMG --> AUTH --> MFA --> RBAC --> RL --> AUDIT
    AUDIT --> EAR --> EIT --> HASH --> BACKUP
```

## Security Checklist

Use this checklist before deploying to production.

### Pre-Deployment

- [ ] Change all default passwords (database, MinIO, Redis, admin account)
- [ ] Generate a strong `JWT_SECRET_KEY` (minimum 64 characters): `openssl rand -hex 64`
- [ ] Generate a strong `ENCRYPTION_KEY`: `openssl rand -hex 32`

:::tip
The installer (`setup-opentranscribe.sh`) generates all of these automatically on fresh
install, including the MinIO encryption key. The items above only need manual attention if you
created `.env` by hand from `.env.example` — the backend refuses to start in production mode
if placeholder keys are detected.
:::
- [ ] Leave `ENVIRONMENT` unset (it defaults to `production`, which turns debug off and enforces the secret and Redis-password checks); only the dev override sets it to `development`
- [ ] Configure TLS certificates for HTTPS
- [ ] Leave `CORS_ORIGINS` unset unless a frontend is served from a different origin (hardened default: none)
- [ ] Configure trusted proxies / host validation for your domain(s) (nginx `server_name`; see `configuration/nginx-setup.md`) — there is no `ALLOWED_HOSTS` env var
- [ ] Leave `ENABLE_API_DOCS` unset: in a hardened deployment the Swagger UI, ReDoc and OpenAPI schema (`/api/docs`, `/api/redoc`, `/api/openapi.json`) are not published unless it is set to `true`. (The `/docs/` path on NGINX is the embedded documentation site, not the API docs.)

### Authentication

- [ ] Enable MFA enforcement for admin accounts (at minimum)
- [ ] Configure password policy (minimum 12 characters, complexity requirements)
- [ ] Set account lockout threshold (recommended: 5 failed attempts)
- [ ] Configure session timeout (recommended: 30 minutes for sensitive environments)
- [ ] Enable audit logging

### Infrastructure

- [ ] Restrict exposed ports to only what is necessary (typically 443 only)
- [ ] Configure firewall rules for Docker networks
- [ ] Enable container health checks
- [ ] Set up log aggregation and monitoring
- [ ] Schedule regular backups with encryption
- [ ] Plan credential rotation schedule

## Authentication Hardening

### MFA Enforcement

OpenTranscribe supports TOTP-based multi-factor authentication compatible with standard authenticator apps (Google Authenticator, Microsoft Authenticator, Authy).

**Configuration options:**

| Setting | Description | Recommended |
|---------|-------------|-------------|
| MFA required for all users | Every user must enroll in MFA | High-security environments |
| MFA required for admins only | Only admin-role users must enroll | Standard deployments |
| MFA optional | Users can opt in | Development only |

Configure in **Settings > Authentication > Local Auth > Multi-Factor Authentication (MFA)**: *Enable TOTP-based MFA*, *Require MFA for all users*, and *Require MFA for administrators*.

Backup codes are generated during enrollment (8 characters each; the number is the *Backup Codes* setting, default 10). They are stored hashed (bcrypt, or PBKDF2-SHA256 when `FIPS_MODE=true`) and are single-use.

### Password Policies

Password rules are set by a **level** (`basic`, `standard`, `hardened`, `custom`) chosen in **Settings > Authentication > Local Auth** or with `PASSWORD_POLICY_PROFILE`. The individual values below apply under `hardened` (read-only) and `custom` (editable); `basic` and `standard` fix their own rules. A fresh install from `.env.example` gets `standard`; an existing install without the variable keeps the code default `hardened`. See [Password policy levels](../configuration/password-policy.md) for the full table.

```bash
# .env configuration (hardened / custom levels)
PASSWORD_MIN_LENGTH=12              # Minimum password length
PASSWORD_REQUIRE_UPPERCASE=true     # Require uppercase letter
PASSWORD_REQUIRE_LOWERCASE=true     # Require lowercase letter
PASSWORD_REQUIRE_DIGIT=true         # Require number
PASSWORD_REQUIRE_SPECIAL=true       # Require special character
PASSWORD_HISTORY_COUNT=24           # Prevent reuse of last N passwords (coded default 24)
PASSWORD_MAX_AGE_DAYS=60            # Force password change (0 = disabled; 60 is the coded default)
```

Passwords are hashed using bcrypt with SHA-256 pre-hash (overcomes bcrypt's 72-byte limit). For FIPS environments, PBKDF2-SHA256 with 600,000 iterations is used instead.

### Account Lockout

Protects against brute-force attacks:

```bash
ACCOUNT_LOCKOUT_THRESHOLD=5         # Lock after N failed attempts
ACCOUNT_LOCKOUT_DURATION_MINUTES=15 # Lockout duration in minutes
ACCOUNT_LOCKOUT_PROGRESSIVE=true    # Escalate duration on repeated lockouts
ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES=1440  # Progressive lockout ceiling (24h)
```

Lockout events are recorded in the audit log. Administrators can manually unlock accounts through the admin panel.

### Session Management

- **Access tokens**: Short-lived JWT tokens (default 60 minutes; shorten with *Token Expiry* in **Settings > Authentication > Sessions** for high-security environments)
- **Idle and absolute timeouts**: sessions end after 15 minutes idle (`SESSION_IDLE_TIMEOUT_MINUTES`) or 8 hours total (`SESSION_ABSOLUTE_TIMEOUT_MINUTES`)
- **Refresh tokens**: Rotated on each use, preventing token replay (default lifetime 7 days)
- **Session invalidation**: All sessions terminated on password change
- **Concurrent sessions**: Optionally limit active sessions per user

## Network Security

### Port Exposure

In production, expose only the minimum required ports. The compose files publish some host ports by default, so this needs checking rather than assuming:

| Port | Service | Default binding |
|------|---------|-----------------|
| 443 | NGINX (HTTPS) | All interfaces (public) |
| 80 | NGINX (HTTP redirect) | All interfaces (redirect to 443 only) |
| 5174 (`BACKEND_PORT`) | Backend API | **All interfaces**; set `BACKEND_BIND_HOST=127.0.0.1` behind NGINX |
| 5173 (`FRONTEND_PORT`), 5183 (`DOCS_PORT`) | Frontend and docs containers | **All interfaces**; block at the host firewall |
| 5176 / 5177 | PostgreSQL / Redis | `127.0.0.1` only |
| 5178 / 5179 | MinIO API / console | `127.0.0.1` only |
| 5180 / 5181 | OpenSearch API / admin | `127.0.0.1` only |
| 5175 | Flower | `127.0.0.1` only |

Unsetting a `*_PORT` variable does not remove its mapping (each is written `${VAR:-default}`). See [Production Deployment: Restricting Exposed Ports](./production-deployment.md#restricting-exposed-ports).

### Docker Network Isolation

OpenTranscribe uses a single Docker bridge network. The datastores are reached over that network (`postgres:5432`, `redis:6379`, `minio:9000`, `opensearch:9200`); their loopback-only host mappings exist for local tooling. To drop one entirely, remove it with your own compose override.

### LLM Firewall (Self-Hosted Models)

When running vLLM or Ollama on the host machine, Docker containers need firewall rules to reach the LLM server.

**Restrictive approach -- allow only Docker containers:**

```bash
# Allow vLLM only from Docker network
sudo ufw allow from 172.17.0.0/16 to any port 8000 proto tcp comment 'vLLM from Docker'

# Allow Ollama only from Docker network
sudo ufw allow from 172.17.0.0/16 to any port 11434 proto tcp comment 'Ollama from Docker'
```

**Alternative -- use Docker bridge IP in configuration:**

Instead of `localhost`, configure the LLM endpoint as:
- `http://172.17.0.1:8000/v1` (Docker bridge gateway on Linux)
- `http://host.docker.internal:8000/v1` (Docker Desktop on macOS/Windows)

Or add `extra_hosts` to relevant services in `docker-compose.yml`:

```yaml
services:
  backend:
    extra_hosts:
      - host.docker.internal:host-gateway
  celery-nlp-worker:
    extra_hosts:
      - host.docker.internal:host-gateway
```

:::tip
Both the `backend` and `celery-nlp-worker` containers need access to the LLM server. The Settings UI test runs from `backend`, but actual summarization runs from `celery-nlp-worker`.
:::

### TLS Configuration

For production deployments with NGINX:

```bash
# Set NGINX_SERVER_NAME and the certificate paths in .env, then start production;
# the NGINX overlay is added automatically (see Production Deployment).
./opentr.sh start prod --build
```

Ensure TLS certificates are:
- From a trusted CA (not self-signed) for public deployments
- Renewed before expiration (automate with certbot or similar)
- Using TLS 1.2 or higher (TLS 1.0 and 1.1 should be disabled)

## Data Protection

### Where Your Data Lives

Transcripts and personal data exist in **several places**, not just the database. An at-rest
encryption strategy must cover all of them:

| Store | Sensitive content | At-rest encryption |
|---|---|---|
| **PostgreSQL** | Transcript text, summaries, speaker names, user emails | No native encryption — use volume/disk encryption (below) |
| **OpenSearch** | Full transcript text (search index) + embeddings | No native encryption — use volume/disk encryption (below) |
| **MinIO** | Original media files, exports, thumbnails | **AES-256-GCM server-side encryption, enabled by default** |
| **Redis** | In-flight task payloads, notifications | Volume/disk encryption (if persistence enabled) |
| **Backups** | Complete database dump (all transcripts) | `./opentranscribe.sh backup --encrypt` (GPG AES-256) |

### Encryption at Rest

**MinIO Storage** (media files): Server-side AES-256-GCM encryption is **enabled automatically
by the installer**, which generates `MINIO_KMS_SECRET_KEY` and sets
`MINIO_KMS_AUTO_ENCRYPTION=on`. Manual configuration:

```bash
# .env configuration
MINIO_KMS_SECRET_KEY=opentranscribe-key:$(openssl rand -base64 32)
MINIO_KMS_AUTO_ENCRYPTION=on
```

**Database fields**: Sensitive fields (API keys, TOTP secrets, LLM provider credentials,
watch-source passwords) are encrypted at the application layer using AES-256-GCM with:
- 256-bit key derived via PBKDF2-SHA256 (600k iterations)
- 96-bit random nonce per encryption
- 128-bit authentication tag

Data format: `v3:base64(salt):base64(nonce):base64(ciphertext+tag)`

**PostgreSQL and OpenSearch data directories**: Vanilla PostgreSQL and OpenSearch have **no
built-in at-rest encryption**. The standard approach is encrypting the storage layer beneath
them:

- **Full-disk encryption**: LUKS/dm-crypt on the host (or FileVault on macOS, BitLocker on
  Windows). Protects against stolen or improperly decommissioned disks; transparent to Docker.
- **Encrypted volumes**: Place the Docker volume directories (`POSTGRES_DATA_PATH`,
  `OPENSEARCH_DATA_PATH`, etc.) on an encrypted filesystem or encrypted block device.
- **Cloud block storage**: If running on cloud VMs, enable the provider's volume encryption
  (e.g., encrypted EBS).

Note: transcript *text* in PostgreSQL/OpenSearch is intentionally **not** encrypted at the
application layer — full-text search, semantic search, and LLM features require the backend to
read it. Access is protected by authentication, per-user authorization on every query, and
audit logging. For masking PII/profanity at display and export time, see
[Content Redaction](../features/content-redaction.md).

### Encryption in Transit

All inter-service communication should use TLS in production:

- **Client to NGINX**: TLS 1.2+ (configured in NGINX)
- **NGINX to backend**: Internal Docker network (trusted)
- **Backend to PostgreSQL**: Enable `sslmode=require` in database connection string
- **Backend to MinIO**: Configure MinIO with TLS certificates
- **Backend to OpenSearch**: Enable HTTPS for OpenSearch

### Backup Encryption

Database backups contain **every user's transcripts in plaintext SQL**. Encrypt any backup
that leaves the host:

```bash
# Encrypted backup - pg_dump is piped directly into GPG (AES-256);
# the plaintext dump never touches disk. Prompts for a passphrase.
./opentranscribe.sh backup --encrypt

# Restore - .gpg files are detected and decrypted automatically
./opentranscribe.sh restore backups/opentranscribe_backup_YYYYMMDD_HHMMSS.sql.gpg
```

Store the passphrase in a password manager — an encrypted backup without its passphrase is
unrecoverable.

## API Security

### Rate Limiting

Authentication endpoints are rate-limited to prevent brute-force attacks:

| Endpoint | Limit | Scope |
|----------|-------|-------|
| Login | Configurable | Per-IP and per-user |
| Registration | Configurable | Per-IP |
| Password Reset | Configurable | Per-IP |
| API endpoints | Configurable | Per-user token |

### CORS Configuration

The SPA and API are served from the same origin, which needs no CORS entry. With
`CORS_ORIGINS` unset, a hardened deployment allows **no** cross-origin origins (development
defaults to the Vite dev server's origins). Set it only when a frontend is served from a
different origin, and list exactly that origin:

```bash
# .env - only when the frontend lives on a different origin
CORS_ORIGINS=https://transcribe.yourdomain.com
```

Never use `*` (wildcard) in production; the backend refuses to start with it. The resolved list
is logged at startup (`CORS allowed origins: ...`). See
[CORS Origins](../configuration/environment-variables.md#cors-origins).

### Content Security Policy

The shipped NGINX configuration (`nginx/site.conf.template`) already sends a CSP and the other security headers (`X-Frame-Options: SAMEORIGIN`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, HSTS). The exact values are listed in [Production Deployment: CSP and Security Headers](./production-deployment.md#10-csp-and-security-headers). If you terminate TLS on a different proxy, replicate them.

### Input Validation

OpenTranscribe validates all inputs through Pydantic schemas on the backend. File uploads are validated for:
- File type (allowed MIME types only)
- File size (configurable maximum, default 15GB)
- Filename sanitization

## Container Security

### Non-Root Execution

All backend containers run as `appuser` (UID 1000, GID 999 — the Dockerfile uses `groupadd -r`), not root:

- Reduces impact of container escape vulnerabilities
- Compliant with security scanning tools (Trivy, Snyk)
- User groups: `appuser`, `video` (for GPU access)

If you encounter permission issues with model cache directories:

```bash
./scripts/fix-model-permissions.sh
```

### Image Security

- **Multi-stage builds**: Production images use multi-stage Dockerfiles to minimize attack surface
- **Minimal base images**: Only necessary runtime dependencies are included
- **No secrets in images**: All secrets are passed via environment variables or mounted files
- **Regular updates**: Base images should be rebuilt periodically to pick up security patches

### Image Scanning

Scan images before deployment:

```bash
# Using Trivy
trivy image davidamacey/opentranscribe-backend:latest
trivy image davidamacey/opentranscribe-frontend:latest

# Using Docker Scout
docker scout cves davidamacey/opentranscribe-backend:latest
```

## Audit and Compliance

### Audit Logging

All authentication events are logged for security monitoring:

- Login attempts (success and failure) with IP address
- Password changes and resets
- MFA enrollment and removal
- Account lockouts and unlocks
- Session creation and termination
- Administrative actions (user management, settings changes)

Events are viewable and exportable (CSV or JSON) in **Settings > Audit Logs**; an organization admin can export their own organization's events. Log format supports integration with SIEM systems (Splunk, ELK, etc.).

### Log Retention

Configure log retention based on your compliance requirements:

| Compliance Framework | Minimum Retention |
|---------------------|-------------------|
| SOC 2 | 1 year |
| HIPAA | 6 years |
| FedRAMP | 3 years |
| GDPR | As needed for purpose |

### FedRAMP/NIST Controls Mapping

OpenTranscribe includes features that map to NIST 800-53 controls:

| Control | Implementation |
|---------|---------------|
| **AC-2** (Account Management) | User management, role assignment, account disable |
| **AC-7** (Unsuccessful Login Attempts) | Account lockout after configurable threshold |
| **AC-8** (System Use Notification) | Login banner with a free-text classification label (UNCLASSIFIED by default) that every user must acknowledge |
| **AC-12** (Session Termination) | Configurable idle (15 min) and absolute (8 h) session timeouts |
| **AU-2/AU-3** (Audit Events) | Comprehensive audit logging with timestamps and source IPs |
| **IA-2** (Identification and Authentication) | Multi-factor authentication, PKI/CAC support |
| **IA-5** (Authenticator Management) | Configurable password policy (complexity, history, expiration) — see [Password Policy and Current NIST Guidance](#password-policy-and-current-nist-guidance) below for how the shipped defaults relate to current guidance |
| **SC-12** (Cryptographic Key Establishment) | PBKDF2 key derivation |
| **SC-13** (Cryptographic Protection) | AES-256-GCM at rest, HMAC-SHA-256 (HS256) JWT signing -- both FIPS-approved; see [Algorithm Requirements](#algorithm-requirements) |
| **SC-28** (Protection of Information at Rest) | Encrypted sensitive data fields |

The login banner and its classification label are configured in **Settings > Authentication > Local Auth > Login Banner**.

### Password Policy and Current NIST Guidance

OpenTranscribe's password policy is fully admin-configurable at runtime
(**Settings > Authentication > Local Auth**, backed by `SystemSettings`, resolved DB > `.env` >
coded default, no restart needed). It is organised as four **levels**: `basic`, `standard`
(NIST SP 800-63B Revision 4: long passphrases, no composition rules, no forced rotation,
breached-password screening), `hardened` (DoD STIG / FedRAMP style: composition rules, 60-day
expiry, history of 24) and `custom`. The full rule table and the precedence rules are in
[Password policy levels](../configuration/password-policy.md).

**Which level you get depends on how the install was created.** A fresh install from
`.env.example` gets `standard`. An existing install with no `PASSWORD_POLICY_PROFILE` line keeps
the code default, `hardened`, so an upgrade does not change anyone's rules. The values the
`hardened` level applies:

| Setting | `hardened` value | Purpose |
|---|---|---|
| `password_min_length` | 12 | Minimum password length |
| `password_require_uppercase` / `_lowercase` / `_digit` / `_special` | all `true` | Composition (character-class) requirements |
| `password_max_age_days` | 60 | Forced expiration; **0 disables expiry entirely** |
| `password_history_count` | 24 | Prevents reuse of recent passwords |

NIST finalized [SP 800-63B Revision 4](https://pages.nist.gov/800-63-4/sp800-63b.html) on
2025-07-31, and it reverses the older composition and expiry guidance:

> "Verifiers and CSPs **SHALL NOT** require subscribers to change passwords periodically.
> However, verifiers **SHALL** force a change if there is evidence that the authenticator has
> been compromised."
>
> "Verifiers and CSPs **SHALL NOT** impose other composition rules (e.g., requiring mixtures of
> different character types) for passwords."

The `standard` level follows that guidance. Choose `hardened` (set explicitly, so the choice is
recorded) when your own compliance regime still mandates rotation and composition rules. Changing
the level does not affect stored passwords; the policy is enforced when a password is set or
changed.

### GDPR Compliance

Erasure (Art. 17) previously destroyed data but left no durable record that a request had ever
been made or fulfilled — which made Art. 30(1)'s demonstrability requirement and Art. 12(3)'s
one-month deadline both impossible to prove against. An **erasure-ledger** now records that an
erasure was requested, one row per request, with the SLA clock on it.

- **No free-text column, by design.** Every column is a short, `CHECK`-constrained enum
  (subject type, status, actor kind, deferred reason) — there is nowhere to put an email or any
  other personal data even by accident, so the ledger itself can never become a copy of the PII
  it is documenting. The subject is identified only by surrogate database keys, which are
  meaningless once the row they point at is destroyed.
- **Legal-hold re-erasure.** A file under a legal hold (Art. 17(3)(e)) is deferred rather than
  erased immediately; once the hold is lifted, the deferred entry is re-erased rather than
  silently forgotten. A hold is either the operator's or an organization's (placed by an
  organization administrator where the organization legal-hold capability is on); the
  file-destroy path checks both and refuses a held file.
- **Restore reconciliation.** Restoring a database backup taken before an erasure can resurrect
  the erased subject. A reconciliation task re-checks every completed ledger entry against the
  live schema after a restore and re-erases any subject it finds has come back.

## Secrets Management

### Protecting the .env File

The `.env` file contains all sensitive configuration. Protect it:

```bash
# Restrict file permissions
chmod 600 .env
chown root:root .env

# Never commit to version control
# (.env is in .gitignore by default)
```

### Credential Rotation

Establish a rotation schedule for all credentials:

| Credential | Rotation Frequency | How to Rotate |
|-----------|-------------------|---------------|
| `JWT_SECRET_KEY` | 90 days | Update in `.env`, restart backend (invalidates all sessions) |
| `ENCRYPTION_KEY` | Do not rotate by editing `.env` | It decrypts every secret stored in the database (API keys, MFA secrets, backup and watch-source credentials). Changing it makes them permanently undecryptable; there is no automatic re-encryption. Back it up with your database backups |
| Database password | 90 days | Update in PostgreSQL and `.env`, restart all services |
| MinIO credentials | 90 days | Update in MinIO and `.env`, restart all services |
| LLM API keys | Per provider policy | Update in **Settings → LLM Provider Configuration** |

### Avoiding Secrets in Logs

OpenTranscribe redacts sensitive values from log output. When adding custom integrations, never log:
- API keys or tokens
- Passwords or password hashes
- Encryption keys
- User session tokens
- TOTP secrets or backup codes

## FIPS 140-3 Compliance

For government and high-security deployments, OpenTranscribe supports FIPS 140-3 compliant cryptographic operations.

### Enabling FIPS Mode

```bash
# .env configuration
FIPS_MODE=true                      # THE master switch -- nothing below takes effect without it
FIPS_VERSION=140-3
PBKDF2_ITERATIONS_V3=600000        # NIST SP 800-132 2024 recommendation
ENCRYPTION_ALGORITHM_V3=AES-256-GCM # Authenticated encryption
FIPS_MIGRATION_MODE=compatible      # Accept both old and new formats during transition
FIPS_VALIDATE_ENTROPY=true          # Validate entropy sources
```

:::caution[`FIPS_MODE` is the switch, not `FIPS_VERSION`]
`FIPS_VERSION` defaults to `140-3` on **every** deployment, so a condition that reads it
alone can never be false. Every gate in the codebase therefore goes through one property,
`settings.fips_140_3_active` (= `FIPS_MODE and FIPS_VERSION == "140-3"`), and
`tests/unit/test_jwt_algorithm_single_owner.py` fails the build if a module reads
`FIPS_VERSION` directly. Set `FIPS_MODE=true`.
:::

:::caution[`ENCRYPTION_ALGORITHM_V3` is validated, not dispatched on]
`AES-256-GCM` is the only value this build implements, and it is not selectable. The v3
ciphertext envelope (`v3:salt:nonce:ciphertext`) records **no algorithm field**, so decrypt
has to use exactly the algorithm encrypt used — switching would make every stored provider
key, TOTP secret and credential undecryptable. Naming any other algorithm therefore makes a
FIPS deployment **refuse to start**, rather than silently encrypt with something other than
what you configured.

`FIPS_VALIDATE_ENTROPY=true` (the default) makes that same startup check verify the OS
CSPRNG is usable and that `ENCRYPTION_KEY` and `JWT_SECRET_KEY` are plausibly random —
minimum length, distinct bytes, no repeated block or run, and a Shannon-entropy floor. A
failure names the offending variable and refuses the boot. Both checks are inert unless
`FIPS_MODE=true`. Generate keys with the installer or
`generate_encryption_key()`; a hand-typed passphrase will be rejected.
:::

### Algorithm Requirements

| Component | Non-FIPS default | FIPS 140-3 (`FIPS_MODE=true`) | Migration |
|-----------|------------------|-------------------------------|-----------|
| Password Hashing | bcrypt-SHA256 (cost 12) | PBKDF2-SHA256 (600k iter) | Auto-upgrade on login |
| Symmetric Encryption | AES-256-GCM | AES-256-GCM | Auto-upgrade on access |
| JWT Signing (**all** token types) | `JWT_ALGORITHM` (HS256) | `JWT_ALGORITHM` (HS256) | None needed -- see below |
| Token Hashing | SHA-512 | SHA-512 | n/a |
| MFA Backup Codes | bcrypt (cost 12) | PBKDF2-SHA256 (600k iter) | Existing bcrypt codes keep working |

#### JWT signing is HS256, and that is FIPS-approved

Access, refresh and MFA tokens are all signed with **`JWT_ALGORITHM` -- HS256 by default,
in every mode, FIPS included.** This is a compliant configuration, not a gap:

- HMAC is approved by **FIPS 198-1**, and SHA-256 by **FIPS 180-4**.
- **NIST SP 800-57 Part 1 Rev. 5** rates HMAC-SHA-256 at 128 bits of security --
  comfortably above the 112-bit minimum **SP 800-131A Rev. 2** requires through 2030 and
  beyond.

:::info[Refresh tokens were HS512 until this release, on every deployment]
An earlier revision of this page said FIPS mode switched JWT signing to HS512, and that
was corrected to "HS256 always" -- but **both statements were wrong about refresh
tokens**. `token_service.create_refresh_token` selected its algorithm with
`JWT_ALGORITHM_V3 if FIPS_VERSION == "140-3" else "HS256"`, and since `FIPS_VERSION`
defaults to `140-3`, *every* install -- FIPS or not -- signed refresh tokens with HS512
while its access tokens were HS256. Nothing failed visibly, because the refresh verifier
tried both algorithms.

Refresh tokens now follow `JWT_ALGORITHM` like everything else. **No action is required
and no session is signed out**: while the migration window is open (the default),
verification accepts HS256 and HS512, so refresh tokens issued before the upgrade keep
working until they expire or rotate. One behaviour improves as a side effect --
`POST /auth/logout` presented with a *refresh* token now actually revokes it, where
before the HS512 signature failed that handler's HS256-only decode and the token stayed
valid.
:::

Two functions in `app/core/security.py` own these decisions, and every issuer and
verifier delegates to them:

- **`signing_algorithm(token_type)`** -- what gets signed. Returns `settings.JWT_ALGORITHM`
  for every token type, in every FIPS mode.
- **`accepted_algorithms(token_type)`** -- what gets accepted. The signing algorithm first,
  then the other configured algorithm and the historical HS256 default while the migration
  window is open.

`accepted_algorithms` is what the request-path verifiers (`get_current_user` /
`get_optional_current_user`), the WebSocket and SAML verifier (`verify_token`) and the
refresh verifier all call, so acceptance can no longer be wider in one and narrower in
another. It previously could: under `FIPS_MIGRATION_MODE=strict` the WebSocket verifier
accepted only `JWT_ALGORITHM_V3`, which **no issuer in the codebase mints**, so a strict
deployment authenticated HTTP requests normally and refused every WebSocket handshake.

If your authorising official requires HS512 specifically, set it explicitly:

```bash
JWT_ALGORITHM=HS512
JWT_SECRET_KEY=<at least 64 bytes>   # HS512 needs a 512-bit key; startup warns if shorter
```

That one setting moves issuance and verification together, for all three token types.
While `FIPS_MIGRATION_MODE=compatible`, HS256 tokens issued before the change stay
verifiable, so a rolling restart does not sign anyone out; `strict` closes that window.

### Migration Process

1. **Set `FIPS_MIGRATION_MODE=compatible`** -- accepts both legacy and FIPS 140-3 formats
2. **Restart services** -- `./opentr.sh restart-backend`
3. **Monitor migration** -- users are upgraded automatically on next login. Token-algorithm
   fallbacks are audited as `legacy_algorithm_fallback` (with the algorithm actually used
   and the one now expected), which is how you know the window can be closed
4. **Switch to strict mode** when all users have been upgraded: `FIPS_MIGRATION_MODE=strict`

:::warning[What `strict` means, and what it does not]
`strict` narrows acceptance to **exactly the algorithm this deployment signs with** --
i.e. it closes the migration window. It does **not** mean "HS512 only": a deployment left
at the default `JWT_ALGORITHM=HS256` signs HS256, so strict accepts HS256 and refuses
HS512. Refusing what you issue is an outage, not a hardening.

So `strict` only refuses something once you have *also* set `JWT_ALGORITHM=HS512`. Turning
it on invalidates every token signed with the other algorithm immediately -- including
refresh tokens, which means active sessions must re-authenticate rather than refresh. Do
it in a maintenance window, after step 3 shows no more fallbacks.
:::

### TOTP Compatibility

TOTP uses SHA-1 by default per RFC 6238. This is FIPS-allowed because NIST SP 800-131A Rev. 2 permits SHA-1 for HMAC-based applications (SHA-1's collision weakness does not affect HMAC security). This ensures compatibility with standard authenticator apps.

For environments requiring SHA-256/SHA-512 TOTP (with compatible authenticator apps):

```bash
TOTP_ALGORITHM=SHA256  # or SHA512
```

### Verification

Run the FIPS 140-3 test suite (it runs by default with the backend suite and is part of the
full pre-merge gate; see `./scripts/run-integration-tests.sh`):

```bash
cd backend && pytest tests/test_fips_140_3.py -v
```

This checks password hashing algorithm and iterations, JWT signing algorithm, encryption algorithm, and token hash algorithm.

## Vulnerability Reporting

If you discover a security vulnerability in OpenTranscribe, **do not** open a public GitHub issue. Use GitHub's private vulnerability reporting (the "Report a vulnerability" link in the repository's `SECURITY.md`) and include a description, steps to reproduce, the potential impact, and a suggested fix if you have one.

The maintainers aim to acknowledge a report within 5 working days and agree a disclosure timeline once the impact is understood. Security fixes ship in a patch release of the latest released minor version only; older minors are not patched, so upgrading is the supported path (see [Upgrading](./upgrading.md)). Reporters are credited in the advisory if they ask.
