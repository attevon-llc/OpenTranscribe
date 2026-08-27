# Security Policy

## Supported Versions

We actively maintain and provide security updates for the following versions:

| Version | Supported          |
| ------- | ------------------ |
| Latest  | :white_check_mark: |

## Reporting a Vulnerability

We take security vulnerabilities seriously. If you discover a security vulnerability in OpenTranscribe, please follow these guidelines:

### Reporting Process

1. **DO NOT** create a public GitHub issue for security vulnerabilities
2. Email security concerns to: [your-email@domain.com] (replace with your email)
3. Include as much detail as possible:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fixes (if any)

### What to Expect

- **Acknowledgment**: We'll acknowledge receipt of your report within 48 hours
- **Investigation**: We'll investigate and assess the vulnerability
- **Updates**: We'll provide regular updates on our progress
- **Resolution**: We'll work to resolve the issue as quickly as possible

### Response Timeline

- **Critical vulnerabilities**: 24-48 hours for initial response, 7 days for fix
- **High severity**: 72 hours for initial response, 14 days for fix
- **Medium/Low severity**: 1 week for initial response, 30 days for fix

## Security Best Practices for Users

### Docker Security
- Keep Docker and Docker Compose updated
- Use non-root users in containers when possible
- Regularly update base images
- Scan images for vulnerabilities

### API Security
- Use strong JWT secrets in production
- Enable HTTPS/TLS for all communications
- Regularly rotate API keys and secrets
- Implement rate limiting

### Data Security
- Encrypt sensitive data at rest
- Use secure file upload validation
- Regularly backup your data
- Monitor access logs

### Infrastructure Security
- Keep all dependencies updated
- Use firewalls to restrict access
- Monitor system logs
- Implement proper access controls

## Known Security Considerations

### File Uploads
- OpenTranscribe processes user-uploaded audio/video files
- Files are validated for type and size
- Consider running in isolated environments for maximum security

### AI Model Security
- WhisperX models are downloaded from Hugging Face
- Verify model checksums when possible
- Keep models updated

### Database Security
- Use strong database passwords
- Limit database access to necessary services only
- Regularly backup database with encryption

## Security Features (v0.4.0)

### Core Security
- JWT-based authentication with secure refresh token rotation
- Role-based access control (RBAC): `user`, `admin`, `super_admin`
- AES-256-GCM encrypted authentication configuration stored in database
- File type validation and input sanitization
- CORS protection
- Content Security Policy (CSP) headers
- Private MinIO buckets (no public object access)
- Non-root container user (`appuser`, UID 1000) following principle of least privilege
- Per-IP and per-user rate limiting on all auth and API endpoints

### Enterprise Authentication (4-Method Hybrid)

All four methods can be active simultaneously. Users choose their preferred login method on the login screen.

| Method | Description | Use Case |
|--------|-------------|----------|
| **Local** | bcrypt_sha256 password hashing | Default for standalone deployments |
| **LDAP/AD** | LDAPS with service account binding, username or email login | Enterprise Active Directory integration |
| **OIDC/Keycloak** | OAuth 2.0 with PKCE, OIDC discovery, federated logout | Single Sign-On, federated identity |
| **PKI/X.509** | mTLS client certificates, OCSP/CRL revocation | Government systems (CAC/PIV) |

Auth configuration is stored encrypted (AES-256-GCM) in the database and managed via the Super Admin UI. See detailed setup guides:
- [LDAP Authentication](LDAP_AUTH.md)
- [OpenID Connect Setup](OIDC_SETUP.md)
- [PKI/Certificate Authentication](PKI_SETUP.md)
- [Auth Deployment Guide](AUTH_DEPLOYMENT_GUIDE.md)

### Multi-Factor Authentication (MFA)

- **TOTP**: RFC 6238 compliant time-based one-time passwords (HMAC-SHA1, 30-second window, 6 digits)
- **Authenticator app compatible**: Google Authenticator, Microsoft Authenticator, Authy, FreeOTP, 1Password, etc.
- **Backup Codes**: Emergency recovery codes (stored hashed with PBKDF2-SHA256)
- **Configurable Enforcement**: Required for admins only, or for all users
- **External IdP bypass**: PKI and Keycloak users bypass local MFA (their IdP handles it)

### Password Security (configurable, maps to FedRAMP IA-5)

**Password Policies** (all admin-configurable at runtime — Admin → Settings → Authentication —
not hardcoded):
- Minimum length (default 12 characters)
- Complexity requirements (uppercase, lowercase, numbers, symbols) — default on
- Password history enforcement (prevents reuse of last N passwords, default 24)
- Expiration policies (default 60 days; 0 disables expiry)
- Common password and pattern blacklist

⚠️ **The shipped defaults above (mandatory composition, 60-day expiry) reflect NIST SP 800-63B
Revision 3, which [Revision 4](https://pages.nist.gov/800-63-4/sp800-63b.html) (final,
2025-07-31) superseded — the current guidance directs verifiers to *not* force periodic changes
or impose composition rules. See
[Security Hardening → Password Policy and Current NIST Guidance](https://github.com/attevon-llc/OpenTranscribe/blob/master/docs-site/docs/operations/security-hardening.md#password-policy-and-current-nist-guidance)
for the full discussion and how to align an admin-configured deployment with current guidance.

**Implementation:**
- bcrypt_sha256 hashing (overcomes bcrypt's 72-byte limit)
- PBKDF2-SHA256 available for FIPS mode (210,000 iterations FIPS 140-2; 600,000 iterations FIPS 140-3)
- Automatic hash algorithm upgrade on login
- Secure password reset with time-limited tokens

### Account Lockout (NIST AC-7 Compliant)

- Configurable failed attempt threshold (default: 5 attempts)
- Progressive lockout duration
- Automatic unlock after timeout
- Admin override capability (lock/unlock via Admin UI)
- Lockout events logged for security monitoring

### Certificate Revocation (PKI)

- **OCSP**: Real-time revocation checking — denied access within seconds of revocation
- **CRL**: Periodic revocation list — configurable refresh interval (default 24 hours)
- Both methods can be active simultaneously

### Session Management

- Short-lived JWT access tokens (configurable expiration, default 60 minutes)
- Refresh token rotation on every use (stolen tokens become invalid after single use)
- Concurrent session limits per user (configurable)
- Session invalidation on password change and admin-forced logout
- Idle timeout and absolute session timeout options

### Rate Limiting

Authentication endpoints are protected with rate limiting:
- Login attempts: Configurable per-IP and per-user limits
- Registration: Prevents mass account creation
- Password reset: Prevents enumeration attacks
- API endpoints: Configurable limits per endpoint

### Audit Logging

All authentication events are logged in structured **JSON** and **CEF (Common Event Format)** for SIEM integration:
- Login attempts (success/failure with method, IP, user agent)
- Password changes and MFA enrollment/removal
- Account lockouts and unlocks
- Session creation and termination
- Administrative actions (config changes, role promotions)
- Certificate validation events (PKI)

Logs are available in container stdout and optionally indexed in OpenSearch (set `AUDIT_LOG_TO_OPENSEARCH=true`).

## FedRAMP Compliance Features

OpenTranscribe includes features to support FedRAMP compliance requirements:

### AC-7: Unsuccessful Login Attempts (NIST AC-7)
- Configurable lockout threshold (default: 5 failed attempts)
- Progressive lockout duration
- Automatic unlock after configurable timeout
- Lockout events captured in audit log

### AC-8: System Use Notification
- Classification banners (configurable levels: UNCLASSIFIED, CUI, SECRET, TOP SECRET)
- System use notifications displayed before login
- Customizable banner text and colors

### AC-12: Session Termination
- Configurable session timeouts (idle and absolute)
- Automatic logout on inactivity
- Admin-initiated session termination
- Refresh token rotation to detect and invalidate stolen tokens

### IA-2: Identification and Authentication
- Multi-factor authentication (TOTP, RFC 6238)
- PKI/CAC support for government systems
- Strong authentication for privileged users

### IA-5: Authenticator Management (configurable, maps to FedRAMP IA-5)
- Password complexity policies (uppercase, lowercase, number, symbol required by default —
  see the [Password Security](#password-security-configurable-maps-to-fedramp-ia-5) note above
  on current NIST guidance)
- Password history enforcement (configurable, default 24 previous passwords)
- Authenticator feedback protection (no password hints)
- Password expiration policies with advance warning (default 60 days; configurable to 0/disabled)
- bcrypt_sha256 default; PBKDF2-SHA256 in FIPS mode

### SC-13: Cryptographic Protection
- AES-256-GCM for sensitive configuration data at rest
- TLS 1.2+ for all service-to-service communication
- PBKDF2-SHA256 for password-based key derivation (FIPS mode)

### AU-2/AU-3: Audit Events
- Comprehensive audit logging in JSON and CEF formats
- Timestamp and user identification on every event
- Event type and outcome (success/failure) recording
- Source IP address and user agent logging
- OpenSearch integration for audit log search and analysis

For testing compliance features, see [TESTING_CHECKLIST.md](TESTING_CHECKLIST.md).
For FIPS cryptographic compliance, see [FIPS_140_3_COMPLIANCE.md](FIPS_140_3_COMPLIANCE.md).

## Responsible Disclosure

We appreciate the security research community's efforts to improve OpenTranscribe's security. We're committed to working with researchers and will acknowledge their contributions (with their permission) in our security advisories.

Thank you for helping keep OpenTranscribe secure!
