"""Security-control settings: FIPS, password policy, rate limits, tokens, lockout,
audit, banner, account expiry, session limits, SMTP and abuse intake.

See ``settings_proxy_auth`` for why these are mixins of ``Settings``.
"""

import os

from pydantic_settings import BaseSettings

from app.core.env import env_bool


class SecuritySettings(BaseSettings):
    # ===== FIPS 140-2 Password Hashing =====
    # Enable FIPS mode to use only FIPS-approved algorithms (PBKDF2-SHA256)
    FIPS_MODE: bool = env_bool("FIPS_MODE", False)
    # PBKDF2 iterations (OWASP 2023 recommendation: 210,000 for SHA-256)
    PBKDF2_ITERATIONS: int = 210000
    # ===== FIPS 140-3 Configuration (upgraded from FIPS 140-2) =====
    FIPS_VERSION: str = os.getenv("FIPS_VERSION", "140-3")  # "140-2" or "140-3"
    # Governs PASSWORD HASHING ONLY (core/security.py) -- NOT the utils/encryption.py data
    # encryption envelope, which hardcodes its own module constant of the same value and
    # default (C8). That envelope records no iteration count, so decrypt must reproduce
    # exactly what encrypt used; if this setting ever governed it too, raising it in .env
    # would silently orphan every ciphertext encrypted under the old count.
    PBKDF2_ITERATIONS_V3: int = 600000  # NIST SP 800-132 2024
    JWT_ALGORITHM_V3: str = os.getenv("JWT_ALGORITHM_V3", "HS512")
    ENCRYPTION_ALGORITHM_V3: str = os.getenv("ENCRYPTION_ALGORITHM_V3", "AES-256-GCM")
    FIPS_MIGRATION_MODE: str = os.getenv(
        "FIPS_MIGRATION_MODE", "compatible"
    )  # "compatible" or "strict"
    FIPS_VALIDATE_ENTROPY: bool = env_bool("FIPS_VALIDATE_ENTROPY", True)
    TOTP_ALGORITHM: str = os.getenv(
        "TOTP_ALGORITHM", "SHA1"
    )  # SHA1, SHA256, SHA512 (SHA1 for app compatibility)

    # ===== Password Policy (FedRAMP IA-5) =====
    # Enable password policy enforcement (disable for testing or non-FedRAMP environments)
    PASSWORD_POLICY_ENABLED: bool = env_bool("PASSWORD_POLICY_ENABLED", True)
    # Which tier applies: "basic" (min 8, no composition, no expiry/history; light, opt-in),
    # "standard" (NIST SP 800-63B-4: min 15 / 8 with MFA, no composition rules, no periodic
    # expiry, blocklist on), "hardened" (DoD STIG style: the individual values below, forced
    # rotation, history 24) or "custom" (the individual PASSWORD_* values exactly as set).
    # "nist" and "stig" are accepted as aliases of "standard" and "hardened". Code default is
    # "hardened" so an install that never set it keeps its behaviour on upgrade; .env.example
    # ships "standard" for fresh installs. See docs-site/docs/configuration/password-policy.md.
    PASSWORD_POLICY_PROFILE: str = "hardened"  # noqa: S105 - a policy profile name, not a credential
    # Upper length cap. 0 = profile default (basic/standard: 128, never below 64; others: none).
    PASSWORD_MAX_LENGTH: int = 0
    # Breached/common-password blocklist. Empty = profile default (basic/standard on, hardened/custom off);
    # "true"/"false" overrides the profile.
    PASSWORD_BLOCKLIST_ENABLED: str = ""
    # Operator-supplied blocklist file (SHA-1 hashes or plaintext, one per line). Empty = the
    # list installed by `./opentranscribe.sh download-models password-blocklist`; if none is
    # installed the check is skipped with a startup warning.
    PASSWORD_BLOCKLIST_PATH: str = ""
    # Extra context-specific words a password may not contain (comma-separated); the product
    # name is always included.
    PASSWORD_CONTEXT_WORDS: str = ""
    # Optional online k-anonymity range lookup (HIBP-style). OFF by default: air-gapped
    # installs must never make an outbound call. Fails OPEN (logged) on timeout/error.
    PASSWORD_HIBP_ENABLED: bool = False
    PASSWORD_HIBP_URL: str = "https://api.pwnedpasswords.com/range"  # noqa: S105 - a public API URL, not a credential
    PASSWORD_HIBP_TIMEOUT_SECONDS: float = 3.0
    # Minimum password length (NIST SP 800-63B recommends 8+, FedRAMP typically requires 12+)
    PASSWORD_MIN_LENGTH: int = 12
    # Hours a password must be kept before it can be changed again (0 disables). Applies to
    # hardened/custom only; basic/standard never enforce a minimum age.
    PASSWORD_MIN_AGE_HOURS: int = 24
    # Require at least one uppercase letter
    PASSWORD_REQUIRE_UPPERCASE: bool = env_bool("PASSWORD_REQUIRE_UPPERCASE", True)
    # Require at least one lowercase letter
    PASSWORD_REQUIRE_LOWERCASE: bool = env_bool("PASSWORD_REQUIRE_LOWERCASE", True)
    # Require at least one digit
    PASSWORD_REQUIRE_DIGIT: bool = env_bool("PASSWORD_REQUIRE_DIGIT", True)
    # Require at least one special character
    PASSWORD_REQUIRE_SPECIAL: bool = env_bool("PASSWORD_REQUIRE_SPECIAL", True)
    # Number of previous passwords to prevent reuse (FedRAMP requires 24)
    PASSWORD_HISTORY_COUNT: int = 24
    # Maximum password age in days before forced reset (FedRAMP requires 60)
    PASSWORD_MAX_AGE_DAYS: int = 60
    # ===== Rate Limiting Settings (OWASP recommended) =====
    # Rate limit authentication endpoints per IP address
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10
    # Rate limit for general API endpoints
    RATE_LIMIT_API_PER_MINUTE: int = 100
    # Rate limit for GET /auth/methods (issue #1131). Public and read-only, but every SPA page
    # load calls it (the root layout and the login page each do), so the credential-endpoint
    # limit above (10/min) is exhausted by a handful of reloads. 60/min is one per second:
    # ample for reloads and tabs behind a shared NAT, still a hard ceiling for scraping.
    RATE_LIMIT_AUTH_METHODS_PER_MINUTE: int = 60
    # Rate limit for handlers that make a server-side outbound request to a caller-supplied
    # base_url (LLM connection-test + model-discovery, issue #676). Deliberately tighter than
    # the general API limit: a connection test is a human-scale action, and this router has
    # no admin gate, so an uncapped limit here is an amplification/resource-exhaustion surface
    # even though the target itself is DNS-pinned against private IPs (see
    # `_assert_safe_llm_endpoint`/`_pin_llm_endpoint` in `app/api/endpoints/llm_endpoint_safety.py`).
    RATE_LIMIT_LLM_OUTBOUND_PER_MINUTE: int = 10
    # Rate limit for GET /users/search (the sharing/group-member-add autocomplete). The
    # binding legitimate flow is adding members to a group: the picker clears its query
    # after every add, so adding 10 members is roughly 30-40 requests/minute of genuine
    # traffic. 60 is 1.5x that.
    RATE_LIMIT_DIRECTORY_PER_MINUTE: int = 60
    # Rate limit for GET /search (issue #904). Not `get_api_rate_limit()` (100/min, meant
    # for cheap CRUD reads) — this route can burn ~2s of Presidio masking per call, the same
    # argument RATE_LIMIT_LLM_OUTBOUND_PER_MINUTE already makes. The heaviest realistic
    # legitimate minute, once the dead client-side prefetch is removed (issue #904 Unit 6),
    # is ~20/min of steady result-page paging; 30 is 1.5x that.
    RATE_LIMIT_SEARCH_PER_MINUTE: int = 30
    # Enable rate limiting (disable for testing)
    RATE_LIMIT_ENABLED: bool = env_bool("RATE_LIMIT_ENABLED", True)
    # Trusted proxy IPs for rate limiting (comma-separated)
    # Only trust X-Forwarded-For headers from these IPs
    # Example: "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    RATE_LIMIT_TRUSTED_PROXIES: str = os.getenv("RATE_LIMIT_TRUSTED_PROXIES", "")

    # ===== Token Management (FedRAMP AC-12) =====
    # Refresh token expiration in days (7 days default for refresh token flow)
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # Enable token revocation checking via Redis blacklist
    TOKEN_REVOCATION_ENABLED: bool = env_bool("TOKEN_REVOCATION_ENABLED", True)

    # ===== Account Lockout Settings (NIST AC-7 compliant) =====
    # Number of failed login attempts before lockout
    ACCOUNT_LOCKOUT_THRESHOLD: int = 5
    # Initial lockout duration in minutes (progressive: 15 -> 30 -> 60 -> 1440)
    ACCOUNT_LOCKOUT_DURATION_MINUTES: int = 15
    # Enable progressive lockout (doubles duration for each subsequent lockout)
    ACCOUNT_LOCKOUT_PROGRESSIVE: bool = env_bool("ACCOUNT_LOCKOUT_PROGRESSIVE", True)
    # Maximum lockout duration in minutes (24 hours)
    ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES: int = 1440
    # Enable account lockout (disable for testing)
    ACCOUNT_LOCKOUT_ENABLED: bool = env_bool("ACCOUNT_LOCKOUT_ENABLED", True)

    # ===== Audit Logging (FedRAMP AU-2/AU-3) =====
    AUDIT_LOG_ENABLED: bool = env_bool("AUDIT_LOG_ENABLED", True)
    AUDIT_LOG_FORMAT: str = os.getenv("AUDIT_LOG_FORMAT", "json")  # json or cef
    AUDIT_LOG_TO_OPENSEARCH: bool = env_bool("AUDIT_LOG_TO_OPENSEARCH", True)
    # 0 = keep forever (the default). Anything else is the age, in days, after which the daily
    # sweep may delete rows from the prunable audit tables; values below
    # AUDIT_RETENTION_FLOOR_DAYS are rejected at startup (see validate_auth_settings).
    AUDIT_LOG_RETENTION_DAYS: int = 0
    # Fallback to file-based logging when OpenSearch is unavailable (FedRAMP AU-9)
    AUDIT_LOG_FALLBACK_ENABLED: bool = env_bool("AUDIT_LOG_FALLBACK_ENABLED", True)
    AUDIT_LOG_FALLBACK_PATH: str = "/var/log/opentranscribe/audit-fallback.jsonl"

    # ===== Login Banner (FedRAMP AC-8) =====
    LOGIN_BANNER_ENABLED: bool = env_bool("LOGIN_BANNER_ENABLED", False)
    LOGIN_BANNER_TEXT: str = os.getenv("LOGIN_BANNER_TEXT", "")
    LOGIN_BANNER_CLASSIFICATION: str = os.getenv("LOGIN_BANNER_CLASSIFICATION", "UNCLASSIFIED")

    # ===== Account Expiration (FedRAMP AC-2) =====
    ACCOUNT_INACTIVE_DAYS: int = 90
    ACCOUNT_EXPIRATION_ENABLED: bool = env_bool("ACCOUNT_EXPIRATION_ENABLED", False)

    # ===== Concurrent Session Limits (FedRAMP AC-10) =====
    MAX_CONCURRENT_SESSIONS: int = 5  # 0 = unlimited
    CONCURRENT_SESSION_POLICY: str = os.getenv(
        "CONCURRENT_SESSION_POLICY", "terminate_oldest"
    )  # or "reject"

    # ===== SMTP Settings (for password reset emails) =====
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = 587
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "noreply@example.com")
    SMTP_USE_TLS: bool = env_bool("SMTP_USE_TLS", True)
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:5173")

    # ===== Abuse / DMCA / safe-harbor intake =====
    # Contact address surfaced in the UI/API for abuse reports and DMCA takedown
    # notices. Empty = not configured (self-host operators set their own). The
    # enforcement mechanism is the admin quarantine/takedown on MediaFile; this
    # is purely the published intake address. See docs/abuse-and-takedown.md.
    ABUSE_CONTACT_EMAIL: str = os.getenv("ABUSE_CONTACT_EMAIL", "")

    # Encryption settings for sensitive data (API keys, etc.)
    ENCRYPTION_KEY: str = os.getenv(
        "ENCRYPTION_KEY", "this_should_be_changed_in_production_for_api_key_encryption"
    )
