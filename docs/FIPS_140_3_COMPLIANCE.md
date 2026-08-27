# FIPS 140-3 Algorithm-Approved Configuration Guide

This document describes OpenTranscribe's support for **FIPS 140-3 approved cryptographic
algorithms** for federal government and high-security deployments.

## Overview

FIPS 140-3 (Federal Information Processing Standard) is the current cryptographic module
validation standard, mandatory for new federal system deployments since September 2021. It
has two distinct compliance tiers, and this document — like its
[FIPS 140-2 sibling](FIPS_COMPLIANCE.md) — is careful to distinguish them:

| Tier | Description | When required |
|------|-------------|----------------|
| **FIPS-Approved Algorithms** | Uses algorithms on the FIPS 140-3 approved list | FedRAMP Moderate, most government systems |
| **FIPS 140-3 Validated Module** | Runs inside a CMVP-certified cryptographic module | FedRAMP High, DoD IL4+, classified systems |

**OpenTranscribe implements the first tier only.** Every algorithm below is FIPS-approved and
this is boot-enforced (a misconfigured algorithm/secret fails startup rather than degrading
silently — see [Compliance Verification](#compliance-verification) below), which is a real,
tested, meaningful property. It is **not** the same claim as "FIPS 140-3 validated": that
requires the cryptographic module itself (not just the algorithm choice) to hold a CMVP
certificate, which is a separate, formal validation process this project has not undergone. If
your deployment requires a validated module (FedRAMP High, DoD IL4+), see
[FIPS_COMPLIANCE.md](FIPS_COMPLIANCE.md#full-fips-140-2-validated-deployment) for the
host/container-level configuration that gets you there, and consult
[NIST's CMVP validated modules list](https://csrc.nist.gov/projects/cryptographic-module-validation-program/validated-modules)
for a module your platform can validate against.

OpenTranscribe v0.4.0+ supports FIPS 140-3 approved cryptographic algorithms for:

- Password hashing (PBKDF2-SHA256, 600,000 iterations)
- Authentication configuration storage (AES-256-GCM encryption at rest)
- JWT signing (HS512)
- Token hashing (SHA-512)
- MFA backup codes (PBKDF2-SHA256)

## Configuration

### Enabling FIPS 140-3 Mode

Set the following environment variables in your `.env` file:

```bash
# Enable FIPS 140-3 compliance
FIPS_VERSION=140-3

# Password hashing iterations (NIST SP 800-132 2024 recommendation)
PBKDF2_ITERATIONS_V3=600000

# JWT signing algorithm
JWT_ALGORITHM_V3=HS512

# Encryption algorithm
ENCRYPTION_ALGORITHM_V3=AES-256-GCM

# Migration mode (compatible = accept both old and new, strict = new only)
FIPS_MIGRATION_MODE=compatible

# Entropy validation
FIPS_VALIDATE_ENTROPY=true
```

### Algorithm Comparison

| Component | FIPS 140-2 | FIPS 140-3 | Migration |
|-----------|------------|------------|-----------|
| Password Hashing | PBKDF2-SHA256 (210k iter) | PBKDF2-SHA256 (600k iter) | Auto-upgrade on login |
| Symmetric Encryption | Fernet (AES-128-CBC) | AES-256-GCM | Auto-upgrade on access |
| JWT Signing | HS256 | HS512 | Dual verification |
| Token Hashing | SHA-256 | SHA-512 | Auto-upgrade on issuance |
| MFA Backup Codes | bcrypt | PBKDF2-SHA256 (600k iter) | Regenerate required |

## Password Hashing

### PBKDF2-SHA256 with 600,000 Iterations

OpenTranscribe uses PBKDF2-SHA256 with 600,000 iterations for password hashing, meeting the NIST SP 800-132 (2024) recommendations.

**Why 600,000 iterations?**
- OWASP 2023 recommends minimum 600,000 for PBKDF2-SHA256
- Provides ~0.5 second verification time on modern hardware
- Balances security with user experience

**Automatic Upgrade:**
When a user with a legacy hash (bcrypt or lower iteration PBKDF2) logs in:
1. Password is verified against existing hash
2. If valid, password is re-hashed with current algorithm
3. New hash is stored in database
4. User experiences no disruption

### Verification

```python
from app.core.security import verify_password, get_password_hash

# Hash verification
is_valid = verify_password(plain_password, hashed_password)

# New hash creation
new_hash = get_password_hash(password)
# Returns: $pbkdf2-sha256$600000$...
```

## Data Encryption

### AES-256-GCM

Sensitive data is encrypted using AES-256-GCM. In v0.4.0, this covers:
- LDAP bind passwords
- Keycloak client secrets
- TOTP secrets
- API keys for external services
- All authentication configuration values marked as sensitive

Encryption parameters:
- 256-bit key derived via PBKDF2-SHA256
- 96-bit random nonce per encryption
- 128-bit authentication tag

**Data Format:**
```
v3:base64(salt):base64(nonce):base64(ciphertext+tag)
```

**Key Derivation:**
```python
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

kdf = PBKDF2HMAC(
    algorithm=hashes.SHA256(),
    length=32,  # 256 bits
    salt=salt,
    iterations=600000
)
key = kdf.derive(password)
```

**Backward Compatibility:**
Data without the `v3:` prefix is assumed to be legacy Fernet-encrypted and will be:
1. Decrypted using Fernet
2. Re-encrypted using AES-256-GCM
3. Stored with `v3:` prefix

## JWT Signing

### HS512 Algorithm

JWTs are signed using HMAC-SHA512 (HS512) for FIPS 140-3 compliance.

**Token Structure:**
```json
{
  "sub": "user-uuid",
  "exp": 1234567890,
  "iat": 1234567800,
  "jti": "unique-token-id",
  "type": "access",
  "role": "admin"
}
```

**Dual Verification:**
During migration, tokens are verified with algorithm fallback:
1. Try HS512 first (FIPS 140-3)
2. Fall back to HS256 (legacy)

This ensures existing sessions continue to work during upgrade.

## MFA Compliance

### TOTP (Time-Based One-Time Password)

TOTP uses SHA-1 by default per RFC 6238. This is FIPS-allowed because:
- NIST SP 800-131A Rev. 2 permits SHA-1 for HMAC-based applications
- SHA-1's collision weakness doesn't affect HMAC security
- Ensures compatibility with Google Authenticator, Microsoft Authenticator, etc.

**Optional SHA-256/SHA-512:**
For high-security environments with compatible apps:
```bash
TOTP_ALGORITHM=SHA256  # or SHA512
```

### Backup Codes

Backup codes are hashed using PBKDF2-SHA256 with 600,000 iterations:
- 8-character alphanumeric codes (XXXX-XXXX format)
- Cryptographically generated using `secrets` module
- One-time use with secure deletion after verification

## Migration Guide

### Pre-Migration Checklist

1. Backup database
2. Note current FIPS_MODE setting
3. Review number of active users
4. Schedule maintenance window (if strict mode)

### Migration Steps

**Compatible Mode (Recommended):**
```bash
# Step 1: Enable FIPS 140-3 in compatible mode
FIPS_VERSION=140-3
FIPS_MIGRATION_MODE=compatible

# Step 2: Restart services
docker compose restart backend

# Step 3: Monitor migration progress
# Users will be upgraded on next login
```

**Monitoring Progress:**
- Check admin dashboard for migration status
- View percentage of users with upgraded hashes
- Review audit logs for upgrade events

### Rollback

If issues occur:
```bash
# Revert to FIPS 140-2
FIPS_VERSION=140-2
FIPS_MIGRATION_MODE=compatible

# Restart services
docker compose restart backend
```

All data remains accessible; the system simply stops creating new FIPS 140-3 artifacts.

## Compliance Verification

### Verification Script

Run the FIPS 140-3 test suite (gated behind `RUN_FIPS_TESTS`, part of the full
pre-merge gate — see `./scripts/run-integration-tests.sh`):
```bash
cd backend && RUN_FIPS_TESTS=true pytest tests/test_fips_140_3.py -v
```

This checks:
- Password hashing algorithm and iterations
- JWT signing algorithm
- Encryption algorithm
- Token hash algorithm

### Audit Trail

All cryptographic operations are logged:
- Password hash upgrades
- Encryption algorithm changes
- Token re-issuance

Logs are available in:
- `/var/log/opentranscribe/audit.log`
- OpenSearch (if `AUDIT_LOG_TO_OPENSEARCH=true`)

## FedRAMP Compliance Mapping

| FedRAMP Control | Implementation |
|-----------------|----------------|
| IA-5 (Authenticator Management) | PBKDF2-SHA256 (600k iter), configurable password policy, history enforcement — see the note below on current NIST composition/expiry guidance |
| AC-7 (Unsuccessful Login Attempts) | Account lockout (NIST AC-7 compliant), progressive lockout |
| SC-12 (Cryptographic Key Establishment) | PBKDF2 key derivation for AES-256-GCM |
| SC-13 (Cryptographic Protection) | AES-256-GCM for config at rest, HS512 for JWT signing |
| SC-28 (Protection of Information at Rest) | AES-256-GCM encrypted sensitive auth config in database |
| AU-2/AU-3 (Audit Events) | JSON + CEF structured audit logging, OpenSearch integration |

> **Password composition/expiry note (added 2026-08-27):** NIST finalized
> [SP 800-63B Revision 4](https://pages.nist.gov/800-63-4/sp800-63b.html) on 2025-07-31,
> superseding Revision 3. It now directs verifiers to **not** force periodic password changes
> absent evidence of compromise, and **not** impose composition rules (mandatory character-type
> mixes). OpenTranscribe's password policy (`PASSWORD_MAX_AGE_DAYS`,
> `PASSWORD_REQUIRE_{UPPERCASE,LOWERCASE,DIGIT,SPECIAL}`) is fully admin-configurable at runtime
> (Admin → Settings → Authentication) — the shipped defaults currently still reflect the older,
> now-superseded Rev. 3-era posture (60-day expiry, mandatory composition). See the full
> discussion and current guidance in
> [Security Hardening → Password Policy](../docs-site/docs/operations/security-hardening.md#password-policy-and-current-nist-guidance).

## References

- [NIST FIPS 140-3](https://csrc.nist.gov/publications/detail/fips/140/3/final)
- [NIST SP 800-132](https://csrc.nist.gov/publications/detail/sp/800-132/final) - Password-Based Key Derivation
- [NIST SP 800-131A Rev. 2](https://csrc.nist.gov/publications/detail/sp/800-131a/rev-2/final) - Algorithm Transitions
- [NIST SP 800-63B Revision 4](https://pages.nist.gov/800-63-4/sp800-63b.html) - Digital Identity Guidelines: Authentication and Authenticator Management (current password-policy guidance)
- [NIST Cryptographic Module Validation Program (CMVP)](https://csrc.nist.gov/projects/cryptographic-module-validation-program/validated-modules) - validated-module list, for FIPS 140-3 Validated deployments
- [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
- [RFC 6238](https://tools.ietf.org/html/rfc6238) - TOTP
