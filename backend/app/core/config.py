import logging
import os
from pathlib import Path

from pydantic import field_validator
from pydantic import model_validator
from pydantic_settings import BaseSettings

from app.core.legacy_auth_env import oidc_bool_env
from app.core.legacy_auth_env import oidc_env
from app.core.legacy_auth_env import oidc_int_env

_config_logger = logging.getLogger(__name__)


#: Shipped default for :attr:`Settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS`.
#: A module constant rather than a literal in the field so a test can pin the
#: DEFAULT independently of whatever an operator set in the running environment.
DEFAULT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS = 300_000


def _int_env(key: str, default: int) -> int:
    """Read an environment variable and convert to int with validation.

    Args:
        key: Environment variable name.
        default: Default value if the variable is not set or is invalid.

    Returns:
        The integer value, or the default if conversion fails.
    """
    val = os.getenv(key, str(default))
    try:
        return int(val)
    except (ValueError, TypeError):
        _config_logger.warning(f"Invalid integer for {key}='{val}', using default {default}")
        return default


# The ONLY environment names that relax security controls. Anything else — including
# a typo, an empty string, or an unset variable falling back to the default — is
# treated as production and gets the hardened path. Keep this list closed: adding a
# name here disables default-secret refusal, DEBUG enforcement, the Redis-password
# requirement, and the cookie Secure flag for that value (issue #284 A0.3).
RELAXED_ENVIRONMENTS = frozenset({"development", "dev", "testing", "test", "local"})


def is_relaxed_environment(environment: str) -> bool:
    """Whether *environment* names a non-production environment.

    Args:
        environment: The raw ``ENVIRONMENT`` value.

    Returns:
        True only for an explicit member of :data:`RELAXED_ENVIRONMENTS`.
    """
    return environment.strip().lower() in RELAXED_ENVIRONMENTS


def _validate_ldap_settings(settings: "Settings") -> None:
    """Validate LDAP configuration when LDAP authentication is enabled.

    Args:
        settings: The Settings instance to validate.

    Raises:
        ValueError: If LDAP_ENABLED is true but required LDAP fields are missing.
    """
    if not settings.LDAP_ENABLED:
        return

    missing_ldap = []
    if not settings.LDAP_SERVER:
        missing_ldap.append("LDAP_SERVER")
    if not settings.LDAP_BIND_DN:
        missing_ldap.append("LDAP_BIND_DN")
    if not settings.LDAP_BIND_PASSWORD:
        missing_ldap.append("LDAP_BIND_PASSWORD")
    if not settings.LDAP_SEARCH_BASE:
        missing_ldap.append("LDAP_SEARCH_BASE")
    if missing_ldap:
        raise ValueError(
            f"LDAP_ENABLED=true but the following required settings are missing: "
            f"{', '.join(missing_ldap)}"
        )

    if settings.LDAP_USE_SSL and settings.LDAP_USE_TLS:
        _config_logger.warning(
            "LDAP_USE_SSL and LDAP_USE_TLS are mutually exclusive. Preferring TLS (StartTLS)."
        )


def _validate_oidc_settings(settings: "Settings") -> None:
    """Validate OIDC configuration when OIDC authentication is enabled.

    Args:
        settings: The Settings instance to validate.

    Raises:
        ValueError: If OIDC_ENABLED is true but required OIDC fields are missing.
    """
    if not settings.OIDC_ENABLED:
        return

    missing_oidc = []
    if not settings.OIDC_SERVER_URL:
        missing_oidc.append("OIDC_SERVER_URL")
    if not settings.OIDC_CLIENT_ID:
        missing_oidc.append("OIDC_CLIENT_ID")
    if not settings.OIDC_CALLBACK_URL:
        missing_oidc.append("OIDC_CALLBACK_URL")
    if missing_oidc:
        raise ValueError(
            f"OIDC_ENABLED=true but the following required settings are missing: "
            f"{', '.join(missing_oidc)}"
        )


def _validate_pki_settings(settings: "Settings") -> None:
    """Validate PKI configuration when PKI authentication with revocation checking is enabled.

    Args:
        settings: The Settings instance to validate.

    Raises:
        ValueError: If PKI_ENABLED and PKI_VERIFY_REVOCATION are true but PKI_CA_CERT_PATH is missing.
    """
    if settings.PKI_ENABLED and settings.PKI_VERIFY_REVOCATION and not settings.PKI_CA_CERT_PATH:
        raise ValueError(
            "PKI_VERIFY_REVOCATION=true but PKI_CA_CERT_PATH is not set. "
            "CA certificate is required for OCSP revocation checking."
        )


class Settings(BaseSettings):
    # API configuration
    API_PREFIX: str = "/api"
    PROJECT_NAME: str = "Transcription App"

    # Environment configuration.
    #
    # FAIL-CLOSED (issue #284 A0.3): the default is "production", so a deployment
    # that never sets ENVIRONMENT gets the hardened path — default-secret refusal,
    # DEBUG off, Redis password required, Secure cookies. It used to default to
    # "development", and because NOTHING passes ENVIRONMENT into the containers
    # (opentr.sh uses a shell-local variable of the same name and exports BUILD_ENV
    # instead), every deployment — including `./opentr.sh start prod` — silently ran
    # with all of those protections disabled.
    #
    # Dev relaxation is now explicit: docker-compose.override.yml sets
    # ENVIRONMENT=development, and that file is never loaded in prod.
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "production")
    DEBUG: bool = is_relaxed_environment(ENVIRONMENT)

    # Whether the API runs Alembic on startup (issue #284 A1.4). True suits self-host,
    # where one container owns the database. Set false on an orchestrated deploy where a
    # dedicated migrate Job owns migrations — otherwise every API replica races Alembic
    # on rollout. When false, /health/ready asserts the schema is at head and fails 503
    # if not, so a replica pointed at an un-migrated database never takes traffic.
    RUN_MIGRATIONS_ON_STARTUP: bool = (
        os.getenv("RUN_MIGRATIONS_ON_STARTUP", "true").lower() == "true"
    )

    # Global server-side upload ceiling, in bytes (issue #284 A0.12). Matches the 15 GB
    # the UI advertises and the yt-dlp `max_filesize`. Before this there was NO
    # server-side ceiling in community: the only limit lived in the browser, so a client
    # could skip the UI and PUT an arbitrarily large object to the presigned URL.
    # Enforced at prepare (declared size) AND complete (the size MinIO observed).
    # Set 0 to disable — only sensible on a trusted single-user install.
    MAX_UPLOAD_BYTES: int | None = _int_env("MAX_UPLOAD_BYTES", 15 * 1024 * 1024 * 1024) or None

    # Whether anyone can create their own account via POST /api/auth/register.
    # New users are immediately active and GPU-capable, so on a public deployment this
    # is the door in front of every per-user cost (issue #284 A0.11). Set false when an
    # external IdP owns identity, or when accounts should be admin-provisioned.
    ALLOW_OPEN_REGISTRATION: bool = os.getenv("ALLOW_OPEN_REGISTRATION", "true").lower() == "true"

    # Whether a newly provisioned account must be approved by an administrator
    # before it can use anything. Applies to self-registration AND to every
    # just-in-time account an external IdP creates (app/auth/approval.py).
    #
    # Defaults FALSE so an upgrade changes nothing: with it off, accounts are
    # created 'approved' exactly as before. DB-backed override: auth_config
    # `require_account_approval` (Settings -> Authentication -> Local).
    REQUIRE_ACCOUNT_APPROVAL: bool = (
        os.getenv("REQUIRE_ACCOUNT_APPROVAL", "false").lower() == "true"
    )

    # Whether accounts holding a local password may sign in at all. Turn this off
    # when an external IdP (LDAP / OIDC / PKI) is the authoritative identity source
    # and nobody should be able to authenticate against a password stored here.
    #
    # It does NOT hide the username/password form — LDAP authenticates through the
    # same form — and it never applies to an active super_admin, which is the
    # documented break-glass account (docs/AUTH_DEPLOYMENT_GUIDE.md). Both are
    # deliberate: without the first, disabling local auth would break LDAP login;
    # without the second, a misconfiguration would lock every administrator out of
    # the very screen needed to undo it.
    #
    # DB-backed override: auth_config `local_enabled` (Settings -> Authentication).
    LOCAL_AUTH_ENABLED: bool = os.getenv("LOCAL_AUTH_ENABLED", "true").lower() == "true"

    # SSRF egress policy for user-supplied endpoint URLs (issue #284 A0.1/A0.10).
    # Self-hosted Ollama/vLLM on a private LAN is a legitimate setup, so a single-tenant
    # deployment can opt back into private targets. MUST stay false on anything
    # multi-tenant or publicly registerable: with it on, any user can point a
    # "test connection" at internal services and cloud instance metadata.
    LLM_ALLOW_PRIVATE_ENDPOINTS: bool = (
        os.getenv("LLM_ALLOW_PRIVATE_ENDPOINTS", "false").lower() == "true"
    )
    # Same policy for watch-source S3 endpoints and SMB servers. A self-hosted NAS on
    # the LAN is the normal case for single-tenant installs, so this defaults ON for
    # watch sources; turn it OFF on a multi-tenant or publicly-registerable deployment.
    WATCH_ALLOW_PRIVATE_ENDPOINTS: bool = (
        os.getenv("WATCH_ALLOW_PRIVATE_ENDPOINTS", "true").lower() == "true"
    )

    # Bootstrap admin (issue #284 A0.9). In a relaxed environment the seeder creates
    # the well-known admin@example.com / "password" super_admin that the test suite
    # and local workflow depend on. In a hardened environment that credential is NEVER
    # created: the seeder uses these values, generating a strong random password (logged
    # once at startup) when INITIAL_ADMIN_PASSWORD is unset, so a public deploy can never
    # ship with a known super-admin login.
    INITIAL_ADMIN_EMAIL: str = os.getenv("INITIAL_ADMIN_EMAIL", "admin@example.com")
    INITIAL_ADMIN_PASSWORD: str | None = os.getenv("INITIAL_ADMIN_PASSWORD") or None

    # Edition: "community" (self-hosted, default — everything enabled) or
    # "cloud" (commercial managed edition; the private cloud layer overrides
    # the capability resolver to hide platform-managed features from tenants).
    DEPLOYMENT_EDITION: str = os.getenv("DEPLOYMENT_EDITION", "community")

    # JWT Token settings (NIST SP 800-63B compliant)
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "this_should_be_changed_in_production")
    JWT_ALGORITHM: str = "HS256"
    # Access token expiration: 60 minutes (NIST recommended for moderate assurance)
    # Can be reduced to 15-30 minutes for high-security environments
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = _int_env("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", 60)
    # Refresh token expiration: 7 days (for token refresh flow, future implementation)
    JWT_REFRESH_TOKEN_EXPIRE_MINUTES: int = _int_env("JWT_REFRESH_TOKEN_EXPIRE_MINUTES", 10080)
    # Session idle timeout: 15 minutes (NIST moderate assurance, DoD STIG compliant)
    SESSION_IDLE_TIMEOUT_MINUTES: int = _int_env("SESSION_IDLE_TIMEOUT_MINUTES", 15)
    # Session absolute timeout: 8 hours (force re-authentication)
    SESSION_ABSOLUTE_TIMEOUT_MINUTES: int = _int_env("SESSION_ABSOLUTE_TIMEOUT_MINUTES", 480)

    # ===== FIPS 140-2 Password Hashing =====
    # Enable FIPS mode to use only FIPS-approved algorithms (PBKDF2-SHA256)
    FIPS_MODE: bool = os.getenv("FIPS_MODE", "false").lower() == "true"
    # PBKDF2 iterations (OWASP 2023 recommendation: 210,000 for SHA-256)
    PBKDF2_ITERATIONS: int = _int_env("PBKDF2_ITERATIONS", 210000)

    # ===== FIPS 140-3 Configuration (upgraded from FIPS 140-2) =====
    FIPS_VERSION: str = os.getenv("FIPS_VERSION", "140-3")  # "140-2" or "140-3"
    PBKDF2_ITERATIONS_V3: int = _int_env("PBKDF2_ITERATIONS_V3", 600000)  # NIST SP 800-132 2024
    JWT_ALGORITHM_V3: str = os.getenv("JWT_ALGORITHM_V3", "HS512")
    ENCRYPTION_ALGORITHM_V3: str = os.getenv("ENCRYPTION_ALGORITHM_V3", "AES-256-GCM")
    FIPS_MIGRATION_MODE: str = os.getenv(
        "FIPS_MIGRATION_MODE", "compatible"
    )  # "compatible" or "strict"
    FIPS_VALIDATE_ENTROPY: bool = os.getenv("FIPS_VALIDATE_ENTROPY", "true").lower() == "true"
    TOTP_ALGORITHM: str = os.getenv(
        "TOTP_ALGORITHM", "SHA1"
    )  # SHA1, SHA256, SHA512 (SHA1 for app compatibility)

    # ===== Password Policy (FedRAMP IA-5) =====
    # Enable password policy enforcement (disable for testing or non-FedRAMP environments)
    PASSWORD_POLICY_ENABLED: bool = os.getenv("PASSWORD_POLICY_ENABLED", "true").lower() == "true"
    # Minimum password length (NIST SP 800-63B recommends 8+, FedRAMP typically requires 12+)
    PASSWORD_MIN_LENGTH: int = _int_env("PASSWORD_MIN_LENGTH", 12)
    # Require at least one uppercase letter
    PASSWORD_REQUIRE_UPPERCASE: bool = (
        os.getenv("PASSWORD_REQUIRE_UPPERCASE", "true").lower() == "true"
    )
    # Require at least one lowercase letter
    PASSWORD_REQUIRE_LOWERCASE: bool = (
        os.getenv("PASSWORD_REQUIRE_LOWERCASE", "true").lower() == "true"
    )
    # Require at least one digit
    PASSWORD_REQUIRE_DIGIT: bool = os.getenv("PASSWORD_REQUIRE_DIGIT", "true").lower() == "true"
    # Require at least one special character
    PASSWORD_REQUIRE_SPECIAL: bool = os.getenv("PASSWORD_REQUIRE_SPECIAL", "true").lower() == "true"
    # Number of previous passwords to prevent reuse (FedRAMP requires 24)
    PASSWORD_HISTORY_COUNT: int = _int_env("PASSWORD_HISTORY_COUNT", 24)
    # Maximum password age in days before forced reset (FedRAMP requires 60)
    PASSWORD_MAX_AGE_DAYS: int = _int_env("PASSWORD_MAX_AGE_DAYS", 60)

    # ===== Rate Limiting Settings (OWASP recommended) =====
    # Rate limit authentication endpoints per IP address
    RATE_LIMIT_AUTH_PER_MINUTE: int = _int_env("RATE_LIMIT_AUTH_PER_MINUTE", 10)
    # Rate limit for general API endpoints
    RATE_LIMIT_API_PER_MINUTE: int = _int_env("RATE_LIMIT_API_PER_MINUTE", 100)
    # Enable rate limiting (disable for testing)
    RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
    # Trusted proxy IPs for rate limiting (comma-separated)
    # Only trust X-Forwarded-For headers from these IPs
    # Example: "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    RATE_LIMIT_TRUSTED_PROXIES: str = os.getenv("RATE_LIMIT_TRUSTED_PROXIES", "")

    # ===== Token Management (FedRAMP AC-12) =====
    # Refresh token expiration in days (7 days default for refresh token flow)
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = _int_env("JWT_REFRESH_TOKEN_EXPIRE_DAYS", 7)
    # Enable token revocation checking via Redis blacklist
    TOKEN_REVOCATION_ENABLED: bool = os.getenv("TOKEN_REVOCATION_ENABLED", "true").lower() == "true"

    # ===== Account Lockout Settings (NIST AC-7 compliant) =====
    # Number of failed login attempts before lockout
    ACCOUNT_LOCKOUT_THRESHOLD: int = _int_env("ACCOUNT_LOCKOUT_THRESHOLD", 5)
    # Initial lockout duration in minutes (progressive: 15 -> 30 -> 60 -> 1440)
    ACCOUNT_LOCKOUT_DURATION_MINUTES: int = _int_env("ACCOUNT_LOCKOUT_DURATION_MINUTES", 15)
    # Enable progressive lockout (doubles duration for each subsequent lockout)
    ACCOUNT_LOCKOUT_PROGRESSIVE: bool = (
        os.getenv("ACCOUNT_LOCKOUT_PROGRESSIVE", "true").lower() == "true"
    )
    # Maximum lockout duration in minutes (24 hours)
    ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES: int = _int_env(
        "ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES", 1440
    )
    # Enable account lockout (disable for testing)
    ACCOUNT_LOCKOUT_ENABLED: bool = os.getenv("ACCOUNT_LOCKOUT_ENABLED", "true").lower() == "true"

    # ===== Audit Logging (FedRAMP AU-2/AU-3) =====
    AUDIT_LOG_ENABLED: bool = os.getenv("AUDIT_LOG_ENABLED", "true").lower() == "true"
    AUDIT_LOG_FORMAT: str = os.getenv("AUDIT_LOG_FORMAT", "json")  # json or cef
    AUDIT_LOG_TO_OPENSEARCH: bool = os.getenv("AUDIT_LOG_TO_OPENSEARCH", "true").lower() == "true"
    AUDIT_LOG_RETENTION_DAYS: int = _int_env("AUDIT_LOG_RETENTION_DAYS", 365)
    # Fallback to file-based logging when OpenSearch is unavailable (FedRAMP AU-9)
    AUDIT_LOG_FALLBACK_ENABLED: bool = (
        os.getenv("AUDIT_LOG_FALLBACK_ENABLED", "true").lower() == "true"
    )
    AUDIT_LOG_FALLBACK_PATH: str = os.getenv(
        "AUDIT_LOG_FALLBACK_PATH", "/var/log/opentranscribe/audit-fallback.jsonl"
    )

    # ===== Login Banner (FedRAMP AC-8) =====
    LOGIN_BANNER_ENABLED: bool = os.getenv("LOGIN_BANNER_ENABLED", "false").lower() == "true"
    LOGIN_BANNER_TEXT: str = os.getenv("LOGIN_BANNER_TEXT", "")
    LOGIN_BANNER_CLASSIFICATION: str = os.getenv("LOGIN_BANNER_CLASSIFICATION", "UNCLASSIFIED")

    # ===== Account Expiration (FedRAMP AC-2) =====
    ACCOUNT_INACTIVE_DAYS: int = _int_env("ACCOUNT_INACTIVE_DAYS", 90)
    ACCOUNT_EXPIRATION_ENABLED: bool = (
        os.getenv("ACCOUNT_EXPIRATION_ENABLED", "false").lower() == "true"
    )

    # ===== Concurrent Session Limits (FedRAMP AC-10) =====
    MAX_CONCURRENT_SESSIONS: int = _int_env("MAX_CONCURRENT_SESSIONS", 5)  # 0 = unlimited
    CONCURRENT_SESSION_POLICY: str = os.getenv(
        "CONCURRENT_SESSION_POLICY", "terminate_oldest"
    )  # or "reject"

    # ===== SMTP Settings (for password reset emails) =====
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = _int_env("SMTP_PORT", 587)
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "noreply@example.com")
    SMTP_USE_TLS: bool = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
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

    # Database settings
    POSTGRES_USER: str = os.getenv("POSTGRES_USER", "postgres")
    POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "postgres")
    POSTGRES_HOST: str = os.getenv("POSTGRES_HOST", "localhost")
    POSTGRES_PORT: str = os.getenv("POSTGRES_PORT", "5432")
    POSTGRES_DB: str = os.getenv("POSTGRES_DB", "transcribe_app")
    POSTGRES_SSLMODE: str = os.getenv("POSTGRES_SSLMODE", "prefer")
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}",
    )

    # MinIO / S3 settings
    MINIO_ROOT_USER: str = os.getenv("MINIO_ROOT_USER", "minioadmin")
    MINIO_ROOT_PASSWORD: str = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin")
    MINIO_HOST: str = os.getenv("MINIO_HOST", "localhost")
    MINIO_PORT: str = os.getenv("MINIO_PORT", "9000")
    MINIO_SECURE: bool = os.getenv("MINIO_SECURE", "false").lower() == "true"
    MEDIA_BUCKET_NAME: str = os.getenv("MEDIA_BUCKET_NAME", "opentranscribe")

    # ===== Object-storage backend (issue #284 A1.11) =====
    # "minio" (default) keeps the bundled MinIO container exactly as it was: the
    # endpoint is MINIO_HOST:MINIO_PORT, credentials are the static root user/password,
    # and presigned URLs are rewritten onto the /s3 proxy path. "s3" points the same
    # client at real AWS S3 — regional endpoint, SigV4 with the configured region,
    # virtual-host addressing (minio-py switches automatically for AWS hosts), and
    # credentials from the IAM-role chain. Everything below is inert while this is
    # "minio", so a self-hosted install needs none of it.
    STORAGE_BACKEND: str = os.getenv("STORAGE_BACKEND", "minio")  # minio | s3
    # Explicit S3 endpoint. Leave empty on AWS to derive https://s3.<region>.amazonaws.com;
    # set it for another S3-compatible provider (e.g. https://s3.wasabisys.com).
    S3_ENDPOINT_URL: str = os.getenv("S3_ENDPOINT_URL", "")
    # SigV4 signing region. Wrong region = every request 400s with AuthorizationHeaderMalformed.
    # Falls back to AWS_REGION so a container that already sets the standard AWS var works.
    S3_REGION: str = os.getenv("S3_REGION", os.getenv("AWS_REGION", "us-east-1"))
    # True (default) resolves credentials through the AWS provider chain — env vars,
    # EKS/IRSA web-identity token, ECS task role, EC2 instance metadata — so no static
    # keys are needed and rotation is automatic. Set false to sign with the static
    # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY pair instead.
    S3_USE_IAM_ROLE: bool = os.getenv("S3_USE_IAM_ROLE", "true").lower() == "true"
    # Apply a browser-PUT CORS policy to the media bucket at startup. MinIO already
    # answers any origin, so this only matters on S3, where a missing CORS config makes
    # every direct browser upload fail preflight. OFF by default: overwriting a bucket's
    # CORS configuration is destructive and the bucket may be shared.
    S3_CONFIGURE_BUCKET_CORS: bool = (
        os.getenv("S3_CONFIGURE_BUCKET_CORS", "false").lower() == "true"
    )
    # Origins allowed to PUT directly to the bucket. Empty = reuse CORS_ORIGINS.
    S3_CORS_ALLOWED_ORIGINS: str = os.getenv("S3_CORS_ALLOWED_ORIGINS", "")

    # Presigned URL expiration settings.
    # Video/audio URLs default to 6 hours: a single presigned URL must outlive a long
    # viewing/labeling session of a multi-hour file (a 5-minute URL 403s mid-playback when
    # the player issues a byte-range request after expiry — the <video> element keeps the
    # stale URL even though the frontend refresher updates its variable). Override via env.
    MEDIA_URL_EXPIRE_SECONDS: int = _int_env("MEDIA_URL_EXPIRE_SECONDS", 21600)
    # Thumbnail URLs: 15 minutes default - longer since they're static images
    THUMBNAIL_URL_EXPIRE_SECONDS: int = _int_env("THUMBNAIL_URL_EXPIRE_SECONDS", 900)
    # Derived-asset cache retention (subtitle-embedded videos + extracted audio in the
    # processed-videos/derived/ prefix). These are a regenerable cache, not storage —
    # they are duplicates of the originals and re-created on demand in seconds. A MinIO
    # lifecycle rule auto-expires them after this many days to bound disk/cloud usage.
    # Baseline default for headless/cloud deployments; the admin UI (DB) overrides it.
    # 0 disables auto-expiry (keep forever). Tune low on laptops, high on big-disk servers.
    DERIVED_CACHE_RETENTION_DAYS: int = _int_env("DERIVED_CACHE_RETENTION_DAYS", 7)
    # Public URL for presigned URLs (how browsers access MinIO)
    # Dev: http://localhost:5178 | Prod/nginx: https://yourdomain.com/minio or https://minio.yourdomain.com
    MINIO_PUBLIC_URL: str = os.getenv("MINIO_PUBLIC_URL", "")
    # Backend-agnostic alias for MINIO_PUBLIC_URL (issue #284 A1.12). Presigned URLs were
    # pinned to the internal MinIO host and rewritten onto a hardcoded /s3 path; this is
    # the one knob that decides the browser-facing origin whatever the backend is. Empty
    # falls back to MINIO_PUBLIC_URL, then to /s3 on the minio backend (today's default)
    # and to no rewrite at all on s3, where the signed URL already names a reachable host.
    STORAGE_PUBLIC_URL: str = os.getenv("STORAGE_PUBLIC_URL", "")
    # Hard ceiling on every presigned URL this app mints (issue #284 A1.12). Six hours is
    # the practical cap under an IAM role: a presigned URL dies with the credentials that
    # signed it, and STS session credentials (IMDS/IRSA/ECS) top out at 1–12 h with 6 h a
    # safe common denominator — so a 24 h URL would 403 long before it "expires".
    # Requests above the ceiling are clamped and logged, never rejected.
    PRESIGNED_URL_MAX_SECONDS: int = _int_env("PRESIGNED_URL_MAX_SECONDS", 21600)
    # Object size (MB) at or above which the browser uploads via presigned multipart
    # instead of one presigned PUT (issue #327). Above the backend's single-PUT ceiling
    # multipart is mandatory — 5 GiB on native S3 — and this knob cannot raise the
    # threshold past it. Below the ceiling multipart is what makes an interrupted upload
    # resumable, so the default sits far under it. Raise it to keep more uploads on the
    # single-PUT path; it can never disable multipart for objects that need it.
    MULTIPART_THRESHOLD_MB: int = _int_env("MULTIPART_THRESHOLD_MB", 512)

    # Redis settings (for Celery)
    REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT: str = os.getenv("REDIS_PORT", "6379")
    REDIS_PASSWORD: str = os.getenv("REDIS_PASSWORD", "")
    REDIS_USE_TLS: bool = os.getenv("REDIS_USE_TLS", "false").lower() == "true"
    _REDIS_SCHEME: str = (
        "rediss" if os.getenv("REDIS_USE_TLS", "false").lower() == "true" else "redis"
    )
    REDIS_URL: str = os.getenv(
        "REDIS_URL",
        f"{_REDIS_SCHEME}://{':' + REDIS_PASSWORD + '@' if REDIS_PASSWORD else ''}{REDIS_HOST}:{REDIS_PORT}/0",
    )

    # OpenSearch settings
    OPENSEARCH_HOST: str = os.getenv("OPENSEARCH_HOST", "localhost")
    OPENSEARCH_PORT: str = os.getenv("OPENSEARCH_PORT", "9200")
    OPENSEARCH_USER: str = os.getenv("OPENSEARCH_USER", "admin")
    OPENSEARCH_PASSWORD: str = os.getenv("OPENSEARCH_PASSWORD", "admin")
    OPENSEARCH_USE_TLS: bool = os.getenv("OPENSEARCH_USE_TLS", "false").lower() == "true"
    OPENSEARCH_VERIFY_CERTS: bool = os.getenv("OPENSEARCH_VERIFY_CERTS", "false").lower() == "true"
    # How to authenticate to OpenSearch (issue #284 A1.13). "basic" (default) sends the
    # OPENSEARCH_USER/OPENSEARCH_PASSWORD pair the bundled container expects. "sigv4"
    # signs every request with AWS SigV4 from the IAM-role chain, which is the only
    # accepted auth on an Amazon OpenSearch Service domain locked to an IAM policy.
    OPENSEARCH_AUTH: str = os.getenv("OPENSEARCH_AUTH", "basic")  # basic | sigv4
    # Signing region and service for OPENSEARCH_AUTH=sigv4. "es" is a managed domain;
    # "aoss" is OpenSearch Serverless (a different signing service name).
    OPENSEARCH_AWS_REGION: str = os.getenv("OPENSEARCH_AWS_REGION", "")  # empty -> AWS_REGION
    OPENSEARCH_AWS_SERVICE: str = os.getenv("OPENSEARCH_AWS_SERVICE", "es")  # es | aoss
    OPENSEARCH_TRANSCRIPT_INDEX: str = "transcripts"
    OPENSEARCH_SPEAKER_INDEX: str = "speakers"
    OPENSEARCH_SUMMARY_INDEX: str = "transcript_summaries"
    OPENSEARCH_TOPIC_SUGGESTIONS_INDEX: str = "topic_suggestions"
    OPENSEARCH_TOPIC_VECTORS_INDEX: str = "topic_vectors"

    # Search & RAG settings
    OPENSEARCH_CHUNKS_INDEX: str = "transcript_chunks"
    OPENSEARCH_SEARCH_PIPELINE: str = "transcript-hybrid-search"
    SEARCH_CHUNK_TARGET_WORDS: int = _int_env("SEARCH_CHUNK_TARGET_WORDS", 200)
    SEARCH_CHUNK_OVERLAP_WORDS: int = _int_env("SEARCH_CHUNK_OVERLAP_WORDS", 40)
    SEARCH_RRF_RANK_CONSTANT: int = _int_env("SEARCH_RRF_RANK_CONSTANT", 30)
    SEARCH_RRF_WINDOW_SIZE: int = _int_env("SEARCH_RRF_WINDOW_SIZE", 500)
    SEARCH_BULK_BATCH_SIZE: int = max(_int_env("SEARCH_BULK_BATCH_SIZE", 100), 1)
    SEARCH_NEURAL_BATCH_SIZE: int = _int_env("SEARCH_NEURAL_BATCH_SIZE", 5)
    SEARCH_REINDEX_REFRESH_INTERVAL: int = _int_env("SEARCH_REINDEX_REFRESH_INTERVAL", 100)
    REINDEX_PARALLEL_WORKERS: int = _int_env("REINDEX_PARALLEL_WORKERS", 4)
    SEARCH_HYBRID_MIN_SCORE: float = float(os.getenv("SEARCH_HYBRID_MIN_SCORE", "0.005"))
    SEARCH_SEMANTIC_HIGH_CONFIDENCE: float = float(
        os.getenv("SEARCH_SEMANTIC_HIGH_CONFIDENCE", "0.010")
    )
    # Intra-semantic suppression: filter semantic-only results whose score falls
    # below this fraction of the semantic score range. 0.5 = keep top half.
    SEARCH_SEMANTIC_SUPPRESS_RATIO: float = float(
        os.getenv("SEARCH_SEMANTIC_SUPPRESS_RATIO", "0.20")
    )

    # Max concurrent group searches for collapse inner_hits (OpenSearch default: 0 = sequential)
    SEARCH_COLLAPSE_MAX_CONCURRENT: int = _int_env("SEARCH_COLLAPSE_MAX_CONCURRENT", 20)

    # Maximum number of collapsed file groups to over-fetch for client-side sorting.
    # Higher values improve recall for large collections at the cost of memory.
    SEARCH_MAX_OVERFETCH: int = _int_env("SEARCH_MAX_OVERFETCH", 1000)

    # Chunk threshold above which bulk indexing temporarily disables OpenSearch
    # refresh (sets refresh_interval=-1) for the target index to avoid one
    # segment-refresh per bulk batch. The interval is restored afterwards so
    # normal search latency is unaffected. Tuned for 6+ hour transcripts.
    SEARCH_LARGE_TRANSCRIPT_CHUNKS: int = _int_env("SEARCH_LARGE_TRANSCRIPT_CHUNKS", 500)

    # SQLAlchemy connection pool for the FastAPI backend. Celery workers build
    # their own engines, so these sizes mainly control API concurrency.
    DB_POOL_SIZE: int = max(_int_env("DB_POOL_SIZE", 20), 1)
    DB_MAX_OVERFLOW: int = max(_int_env("DB_MAX_OVERFLOW", 40), 0)

    # Server-side backstop for the "transaction held open across slow work"
    # bug class (issue #440). Postgres terminates a backend that has an OPEN
    # transaction and is running NO query for this long. It cannot interrupt a
    # slow *query* — only an idle one — so a legitimately long-running statement
    # is never affected; the only thing it kills is a connection sitting on
    # ACCESS SHARE locks and pinning the VACUUM horizon while the process does
    # something else (HTTP call, model inference, file I/O).
    #
    # This is defence in depth, NOT the fix: the 35 real leaks were fixed in the
    # code and `scripts/audit-session-lifetime.py` keeps the idiom from
    # returning. 5 minutes is ~10x the slowest legitimate transaction here and
    # ~1/10 of the 48-minute leak that motivated it. Set to 0 to disable.
    #
    # Applies to the shared app engine only. `db/migrations.py` builds its own
    # engines, so a long `ALTER TABLE` and the advisory-lock holder are outside
    # this timeout by construction.
    DB_IDLE_IN_TRANSACTION_TIMEOUT_MS: int = max(
        _int_env("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", DEFAULT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS), 0
    )

    # Observability. LOG_FORMAT="json" switches the root logger to structured
    # JSON lines (Loki/CloudWatch-ready); "text" keeps the human-readable format.
    # SLOW_QUERY_MS gates the slow-query WARNING in app.core.db_metrics.
    LOG_FORMAT: str = "text"
    SLOW_QUERY_MS: int = 500
    SETTINGS_CACHE_TTL: int = 30
    READ_CACHE_ENABLED: bool = True

    # OpenSearch Neural Search settings (ML Commons-based)
    # When enabled, embeddings are generated server-side by OpenSearch instead of Python
    OPENSEARCH_NEURAL_SEARCH_ENABLED: bool = (
        os.getenv("OPENSEARCH_NEURAL_SEARCH_ENABLED", "true").lower() == "true"
    )
    # Default model to register/deploy (from OPENSEARCH_EMBEDDING_MODELS in constants.py)
    OPENSEARCH_NEURAL_MODEL: str = os.getenv(
        "OPENSEARCH_NEURAL_MODEL",
        "huggingface/sentence-transformers/all-MiniLM-L6-v2",
    )
    # Neural ingest pipeline name
    OPENSEARCH_NEURAL_PIPELINE: str = os.getenv(
        "OPENSEARCH_NEURAL_PIPELINE", "transcript-neural-ingest"
    )
    # Where the embedding model comes from (issue #284 A1.13).
    #   "local"   (default) — today's behaviour: we mutate ML Commons cluster settings,
    #             download OPENSEARCH_NEURAL_MODEL, and register/deploy it in the cluster.
    #   "managed" — the domain already hosts the model (a remote-model connector, or one
    #             an operator registered by hand). We touch no cluster settings and
    #             download nothing; OPENSEARCH_NEURAL_MODEL_ID names the model to use.
    # A managed OpenSearch domain rejects the "local" path outright: registering a model
    # from a file:// or arbitrary https:// URL needs cluster settings AWS does not expose.
    OPENSEARCH_EMBEDDING_MODE: str = os.getenv("OPENSEARCH_EMBEDDING_MODE", "local")
    # Pre-registered ML Commons model id, used when OPENSEARCH_EMBEDDING_MODE=managed.
    OPENSEARCH_NEURAL_MODEL_ID: str = os.getenv("OPENSEARCH_NEURAL_MODEL_ID", "")

    # Celery settings
    CELERY_BROKER_URL: str = REDIS_URL
    CELERY_RESULT_BACKEND: str = REDIS_URL

    @property
    def is_hardened(self) -> bool:
        """Whether security controls are enforced (the fail-closed default).

        True unless ``ENVIRONMENT`` explicitly names a relaxed environment. Gate every
        security control on THIS, never on ``ENVIRONMENT in ("production", "prod")`` —
        that form fails open for an unset, empty, or misspelled value (issue #284 A0.3).
        """
        return not is_relaxed_environment(self.ENVIRONMENT)

    @property
    def fips_140_3_active(self) -> bool:
        """Whether this deployment runs the FIPS 140-3 cryptographic profile.

        **``FIPS_MODE`` is the operator's switch; ``FIPS_VERSION`` selects which
        profile once it is on.** Read this property — never ``FIPS_VERSION`` alone.

        ``FIPS_VERSION`` defaults to ``"140-3"`` on EVERY deployment (see its
        declaration above), so a gate that reads it by itself is unconditionally
        true. That is not a hypothetical: it is why ordinary non-FIPS installs
        signed their *refresh* tokens with HS512 while their access tokens were
        HS256 — ``token_service.create_refresh_token`` read ``FIPS_VERSION`` alone.
        The same class of defect was already fixed twice, in
        ``token_service.create_token`` and ``auth/mfa.py``'s backup-code context
        ("Non-FIPS deployments issued FIPS-profile credentials", CHANGELOG), each
        time by adding ``FIPS_MODE`` to the condition. This property exists so the
        third fix is the last one.
        """
        return self.FIPS_MODE and self.FIPS_VERSION == "140-3"

    # CORS settings
    # Note: Remove "*" in production and specify exact origins for security
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: str | list[str]) -> list[str] | str:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, (list, str)):
            return v
        raise ValueError(v)

    @model_validator(mode="after")
    def validate_auth_settings(self) -> "Settings":
        """Validate that required fields are set when authentication features are enabled.

        Delegates to helper functions for each authentication type.
        Also validates JWT key security settings.

        Raises:
            ValueError: If a required field is missing when its feature is enabled.
        """
        import warnings

        _validate_ldap_settings(self)
        _validate_oidc_settings(self)
        _validate_pki_settings(self)

        # Warn if using the default JWT_SECRET_KEY
        if self.JWT_SECRET_KEY == "this_should_be_changed_in_production":  # noqa: S105 - checking for default value  # nosec B105
            warnings.warn("SECURITY: Using default JWT_SECRET_KEY!", RuntimeWarning, stacklevel=2)

        # Warn if the key is too short for the algorithm this deployment SIGNS with.
        # Gated on JWT_ALGORITHM, not on FIPS_VERSION: FIPS_VERSION defaults to
        # "140-3" everywhere, so the old form warned on every deployment with a
        # short key regardless of whether HS512 was ever used — noise that trains
        # operators to ignore the one case that matters. Issuance reads
        # JWT_ALGORITHM in every mode (core/security.signing_algorithm), so that is
        # the setting that decides whether a 512-bit key is required.
        if self.JWT_ALGORITHM == "HS512" and len(self.JWT_SECRET_KEY) < 64:
            warnings.warn(
                f"JWT_SECRET_KEY is {len(self.JWT_SECRET_KEY)} bytes but HS512 requires 64+ bytes",
                RuntimeWarning,
                stacklevel=2,
            )

        return self

    # Hardware Detection Settings (auto-detected by default)
    TORCH_DEVICE: str = os.getenv("TORCH_DEVICE", "auto")  # auto, cuda, mps, cpu
    COMPUTE_TYPE: str = os.getenv("COMPUTE_TYPE", "auto")  # auto, float16, float32, int8
    USE_GPU: str = os.getenv("USE_GPU", "auto")  # auto, true, false
    GPU_DEVICE_ID: int = _int_env("GPU_DEVICE_ID", 0)  # Host GPU index (Docker maps to device 0)
    GPU_CLUSTERING_DEVICE: int | None = (
        int(os.environ["GPU_CLUSTERING_DEVICE"])
        if os.environ.get("GPU_CLUSTERING_DEVICE")
        else None
    )  # Dedicated GPU for speaker clustering (falls back to GPU_DEVICE_ID)
    BATCH_SIZE: str = os.getenv("BATCH_SIZE", "auto")  # auto or integer

    # AI Models settings
    # large-v3-turbo: 6x faster, ~6GB VRAM, excellent English, good multilingual
    # large-v3: Best accuracy, ~10GB VRAM, required for translation feature
    # large-v2: Legacy model, ~10GB VRAM, good balance
    # Note: large-v3-turbo cannot translate - use large-v3 if translation is needed
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "large-v3-turbo")
    PYANNOTE_MODEL: str = os.getenv("PYANNOTE_MODEL", "pyannote/speaker-diarization")
    HUGGINGFACE_TOKEN: str | None = os.getenv("HUGGINGFACE_TOKEN", None)

    # Speaker diarization settings
    MIN_SPEAKERS: int = _int_env("MIN_SPEAKERS", 1)
    MAX_SPEAKERS: int = _int_env("MAX_SPEAKERS", 20)
    # NUM_SPEAKERS forces exact speaker count (overrides min/max if set)
    _NUM_SPEAKERS_STR: str | None = os.getenv("NUM_SPEAKERS")
    NUM_SPEAKERS: int | None = int(_NUM_SPEAKERS_STR) if _NUM_SPEAKERS_STR else None

    # Diarization embedding batch size is pinned at 16 in
    # backend/app/transcription/diarizer.py. See
    # docs/diarization-vram-profile/README.md for the Phase A measurements
    # that justify the fixed ceiling (identical throughput from bs=16 to
    # bs=128; 3-7 GB VRAM savings at bs=16). No env var is offered for
    # production because the measured-optimal value is a constant.

    # LLM Configuration - Users configure through web UI, stored in database
    # These are system fallbacks for quick access when no user settings exist
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "")

    # LDAP/Active Directory Configuration
    LDAP_ENABLED: bool = os.getenv("LDAP_ENABLED", "false").lower() == "true"
    LDAP_SERVER: str = os.getenv("LDAP_SERVER", "")
    LDAP_PORT: int = _int_env("LDAP_PORT", 636)
    LDAP_USE_SSL: bool = os.getenv("LDAP_USE_SSL", "true").lower() == "true"
    # LDAP_USE_TLS enables StartTLS on non-SSL connections (port 389)
    # Use LDAP_USE_SSL=true for LDAPS (port 636) - they are mutually exclusive
    LDAP_USE_TLS: bool = os.getenv("LDAP_USE_TLS", "false").lower() == "true"
    LDAP_BIND_DN: str = os.getenv("LDAP_BIND_DN", "")
    LDAP_BIND_PASSWORD: str = os.getenv("LDAP_BIND_PASSWORD", "")
    LDAP_SEARCH_BASE: str = os.getenv("LDAP_SEARCH_BASE", "")
    LDAP_USERNAME_ATTR: str = os.getenv("LDAP_USERNAME_ATTR", "sAMAccountName")
    LDAP_USER_SEARCH_FILTER: str = os.getenv(
        "LDAP_USER_SEARCH_FILTER", "({username_attr}={username})"
    ).replace("{username_attr}", os.getenv("LDAP_USERNAME_ATTR", "sAMAccountName"))
    LDAP_EMAIL_ATTR: str = os.getenv("LDAP_EMAIL_ATTR", "mail")
    LDAP_NAME_ATTR: str = os.getenv("LDAP_NAME_ATTR", "cn")
    LDAP_TIMEOUT: int = _int_env("LDAP_TIMEOUT", 10)
    LDAP_ADMIN_USERS: str = os.getenv("LDAP_ADMIN_USERS", "")
    # LDAP Group-based RBAC (alternative to LDAP_ADMIN_USERS)
    # Comma-separated list of group DNs that grant admin role
    LDAP_ADMIN_GROUPS: str = os.getenv("LDAP_ADMIN_GROUPS", "")
    # Comma-separated list of group DNs required for user access (empty = allow all)
    LDAP_USER_GROUPS: str = os.getenv("LDAP_USER_GROUPS", "")
    # Enable recursive group membership (nested groups via LDAP_MATCHING_RULE_IN_CHAIN)
    LDAP_RECURSIVE_GROUPS: bool = os.getenv("LDAP_RECURSIVE_GROUPS", "false").lower() == "true"
    # Attribute to check for group membership (default: memberOf for AD)
    LDAP_GROUP_ATTR: str = os.getenv("LDAP_GROUP_ATTR", "memberOf")

    # ===== OpenID Connect =====
    # Every one of these also resolves from its historical vendor-prefixed spelling,
    # which takes precedence when both are set — see core/legacy_auth_env.py, the one
    # module that still names it. Deployments never have to edit their .env.
    OIDC_ENABLED: bool = oidc_bool_env("OIDC_ENABLED", False)
    OIDC_SERVER_URL: str = oidc_env("OIDC_SERVER_URL")  # e.g., http://localhost:8180
    # Internal URL for backend-to-provider communication (Docker networking).
    # If not set, falls back to OIDC_SERVER_URL.
    OIDC_INTERNAL_URL: str = oidc_env("OIDC_INTERNAL_URL")
    OIDC_REALM: str = oidc_env("OIDC_REALM", "opentranscribe")
    OIDC_CLIENT_ID: str = oidc_env("OIDC_CLIENT_ID")
    OIDC_CLIENT_SECRET: str = oidc_env("OIDC_CLIENT_SECRET")
    # e.g. http://localhost:5173/login — the SPA route, not a backend path.
    OIDC_CALLBACK_URL: str = oidc_env("OIDC_CALLBACK_URL")
    # Role/group value in the token that grants admin access.
    OIDC_ADMIN_ROLE: str = oidc_env("OIDC_ADMIN_ROLE", "admin")
    OIDC_TIMEOUT: int = oidc_int_env("OIDC_TIMEOUT", 30)
    # OIDC Security: Enable audience (aud) claim validation (OWASP recommended)
    # Default to True for security - validates tokens are intended for this client
    OIDC_VERIFY_AUDIENCE: bool = oidc_bool_env("OIDC_VERIFY_AUDIENCE", True)
    # Expected audience claim value (usually the client ID)
    OIDC_AUDIENCE: str = oidc_env("OIDC_AUDIENCE")
    # Enable PKCE (Proof Key for Code Exchange) for OAuth 2.1 compliance
    OIDC_USE_PKCE: bool = oidc_bool_env("OIDC_USE_PKCE", True)
    # Enable issuer (iss) claim validation (OWASP recommended)
    OIDC_VERIFY_ISSUER: bool = oidc_bool_env("OIDC_VERIFY_ISSUER", True)

    # ===== Generic OIDC discovery (issue #353) =====
    # Without a discovery URL the endpoints are built from OIDC_SERVER_URL +
    # "/realms/<realm>/...", which is a single vendor's URL shape — Authentik and most
    # other providers 404 on it. Set a discovery URL and every endpoint (plus the
    # issuer) is read from the provider's own metadata document instead; the realm
    # form stays as the fallback so existing realm-based deployments are unaffected.
    OIDC_DISCOVERY_URL: str = oidc_env("OIDC_DISCOVERY_URL")
    OIDC_ISSUER: str = oidc_env("OIDC_ISSUER")
    # Dotted path to the claim carrying group/role membership. Realm-shaped
    # providers: realm_access.roles (the default). Authentik/Okta: groups. Entra ID:
    # roles. Getting this wrong is silent — everyone logs in and nobody is an admin.
    OIDC_ROLES_CLAIM: str = oidc_env("OIDC_ROLES_CLAIM", "realm_access.roles")
    OIDC_SCOPES: str = oidc_env("OIDC_SCOPES", "openid email profile")

    # ===== OIDC admission control =====
    # Semicolon-delimited group/role values read from OIDC_ROLES_CLAIM, mirroring
    # LDAP_USER_GROUPS. EMPTY ALLOW-LIST ADMITS EVERYONE — that is today's behaviour
    # and the upgrade-safe default, not an oversight. Without it, pointing this at a
    # corporate tenant gives every identity in the directory an account on first
    # login (auth/oidc/admission.py). Blocked wins over allowed.
    #
    # Plain os.getenv, not oidc_env: these names are new, so there is no retired
    # spelling to honour (core/legacy_auth_env.py owns that translation and its
    # alias map is the closed list of variables that ever had one).
    OIDC_ALLOWED_GROUPS: str = os.getenv("OIDC_ALLOWED_GROUPS", "")
    OIDC_BLOCKED_GROUPS: str = os.getenv("OIDC_BLOCKED_GROUPS", "")

    # ===== SAML 2.0 (#35) =====
    # No retired spelling to honour here (this is the auth type's only name), so
    # plain os.getenv throughout — unlike OIDC_*, none of these route through
    # core/legacy_auth_env.py.
    SAML_ENABLED: bool = os.getenv("SAML_ENABLED", "false").lower() == "true"
    # Entity ID this SP identifies itself as. Also the default audience the IdP's
    # assertion must be addressed to.
    SAML_SP_ENTITY_ID: str = os.getenv("SAML_SP_ENTITY_ID", "")
    # e.g. https://localhost:5173/api/auth/saml/acs — must match exactly what is
    # registered with the IdP, or the assertion's Recipient/Destination check fails.
    SAML_SP_ACS_URL: str = os.getenv("SAML_SP_ACS_URL", "")
    SAML_SP_SLS_URL: str = os.getenv("SAML_SP_SLS_URL", "")
    # SP signing/encryption key pair, PEM. Only required when
    # SAML_SIGN_AUTHN_REQUESTS or SAML_WANT_ASSERTIONS_ENCRYPTED is on. Blank means
    # this SP requests but does not itself sign or decrypt.
    SAML_SP_X509_CERT: str = os.getenv("SAML_SP_X509_CERT", "")
    SAML_SP_PRIVATE_KEY: str = os.getenv("SAML_SP_PRIVATE_KEY", "")
    SAML_IDP_ENTITY_ID: str = os.getenv("SAML_IDP_ENTITY_ID", "")
    SAML_IDP_SSO_URL: str = os.getenv("SAML_IDP_SSO_URL", "")
    SAML_IDP_SLO_URL: str = os.getenv("SAML_IDP_SLO_URL", "")
    # The IdP's signing certificate, PEM (no BEGIN/END lines required — sp.py
    # strips them either way). This is what makes assertion verification real:
    # python3-saml (never hand-rolled parsing) checks the assertion's signature
    # against this, so an unset value must refuse to start SAML, not fall open.
    SAML_IDP_X509_CERT: str = os.getenv("SAML_IDP_X509_CERT", "")
    # OWASP-recommended posture: the IdP must sign what it asserts.
    SAML_WANT_ASSERTIONS_SIGNED: bool = (
        os.getenv("SAML_WANT_ASSERTIONS_SIGNED", "true").lower() == "true"
    )
    SAML_WANT_MESSAGES_SIGNED: bool = (
        os.getenv("SAML_WANT_MESSAGES_SIGNED", "true").lower() == "true"
    )
    SAML_SIGN_AUTHN_REQUESTS: bool = (
        os.getenv("SAML_SIGN_AUTHN_REQUESTS", "false").lower() == "true"
    )
    # Attribute names to read from the assertion. No standard covers this across
    # IdPs — ADFS, Okta, and Azure AD SAML each ship different defaults — so these
    # are configurable rather than hardcoded to one vendor's claim URIs, same
    # reasoning as OIDC_ROLES_CLAIM.
    SAML_EMAIL_ATTRIBUTE: str = os.getenv(
        "SAML_EMAIL_ATTRIBUTE",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    )
    SAML_NAME_ATTRIBUTE: str = os.getenv(
        "SAML_NAME_ATTRIBUTE", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"
    )
    SAML_GROUPS_ATTRIBUTE: str = os.getenv("SAML_GROUPS_ATTRIBUTE", "groups")
    SAML_ADMIN_GROUP: str = os.getenv("SAML_ADMIN_GROUP", "")
    # Same semantics and same upgrade-safe empty-admits-everyone default as
    # OIDC_ALLOWED_GROUPS/OIDC_BLOCKED_GROUPS.
    SAML_ALLOWED_GROUPS: str = os.getenv("SAML_ALLOWED_GROUPS", "")
    SAML_BLOCKED_GROUPS: str = os.getenv("SAML_BLOCKED_GROUPS", "")

    # ===== MFA Settings (FedRAMP IA-2) =====
    # MFA is disabled by default for air-gapped deployments
    MFA_ENABLED: bool = os.getenv("MFA_ENABLED", "false").lower() == "true"
    # When MFA_REQUIRED is true, users must set up MFA on first login
    MFA_REQUIRED: bool = os.getenv("MFA_REQUIRED", "false").lower() == "true"
    # Issuer name shown in authenticator apps
    MFA_ISSUER_NAME: str = os.getenv("MFA_ISSUER_NAME", "OpenTranscribe")
    # Number of backup codes to generate (one-time use)
    MFA_BACKUP_CODE_COUNT: int = _int_env("MFA_BACKUP_CODE_COUNT", 10)
    # MFA token expiry in minutes (short-lived token for MFA verification step)
    MFA_TOKEN_EXPIRE_MINUTES: int = _int_env("MFA_TOKEN_EXPIRE_MINUTES", 5)
    # TOTP verification window (number of time steps before/after to accept)
    # 1 = allow 1 step before/after for clock drift (±30 seconds)
    TOTP_VALID_WINDOW: int = _int_env("TOTP_VALID_WINDOW", 1)
    # Require Redis for MFA replay protection (fail-secure mode).
    # Redis is the only place the "this TOTP code / this MFA half-token was already
    # used" claim lives. With this off, a Redis outage silently downgrades MFA to
    # replayable: _consume_totp_code accepts a replayed code and the half-token
    # blacklist check answers "not used". Defaulting it off therefore made the whole
    # replay defence fail OPEN in production.
    # Default follows the hardened posture (never ENVIRONMENT == "production" —
    # see app/core/CLAUDE.md): fail closed in a real deployment, stay permissive in
    # dev/test where a stack without Redis must still log in. An explicit env value
    # always wins over the default.
    MFA_REQUIRE_REDIS: bool = (
        os.getenv(
            "MFA_REQUIRE_REDIS",
            "false" if is_relaxed_environment(ENVIRONMENT) else "true",
        ).lower()
        == "true"
    )

    # ===== PKI/X.509 Certificate Configuration =====
    PKI_ENABLED: bool = os.getenv("PKI_ENABLED", "false").lower() == "true"
    PKI_CA_CERT_PATH: str = os.getenv(
        "PKI_CA_CERT_PATH", ""
    )  # Path to CA certificate for validation
    PKI_VERIFY_REVOCATION: bool = (
        os.getenv("PKI_VERIFY_REVOCATION", "false").lower() == "true"
    )  # Check CRL/OCSP
    PKI_CERT_HEADER: str = os.getenv(
        "PKI_CERT_HEADER", "X-Client-Cert"
    )  # Header name from reverse proxy
    PKI_CERT_DN_HEADER: str = os.getenv(
        "PKI_CERT_DN_HEADER", "X-Client-Cert-DN"
    )  # Distinguished Name header
    PKI_ADMIN_DNS: str = os.getenv(
        "PKI_ADMIN_DNS", ""
    )  # Comma-separated list of admin certificate DNs
    # OCSP/CRL revocation checking settings
    PKI_OCSP_TIMEOUT_SECONDS: int = _int_env("PKI_OCSP_TIMEOUT_SECONDS", 5)
    PKI_CRL_CACHE_SECONDS: int = _int_env("PKI_CRL_CACHE_SECONDS", 3600)  # Cache CRL for 1 hour
    # Soft-fail allows authentication if revocation check fails (network issues)
    # Defaults to false in production (strict revocation checking)
    PKI_REVOCATION_SOFT_FAIL: bool = (
        os.getenv(
            "PKI_REVOCATION_SOFT_FAIL",
            "true" if is_relaxed_environment(ENVIRONMENT) else "false",
        ).lower()
        == "true"
    )
    # Maximum cache size for OCSP responses (LRU eviction when exceeded)
    PKI_OCSP_CACHE_MAX_SIZE: int = _int_env("PKI_OCSP_CACHE_MAX_SIZE", 1000)
    # Maximum cache size for CRLs (LRU eviction when exceeded)
    PKI_CRL_CACHE_MAX_SIZE: int = _int_env("PKI_CRL_CACHE_MAX_SIZE", 1000)
    # Trusted proxy IPs for PKI certificate headers (comma-separated)
    # Only accept PKI certificate headers from these IPs
    # Example: "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    PKI_TRUSTED_PROXIES: str = os.getenv("PKI_TRUSTED_PROXIES", "")

    # ===== Trusted-header (reverse-proxy) Configuration =====
    # An authenticating reverse proxy asserts the identity in a header. Everything
    # here is also settable in the admin UI (category "proxy"), which wins; these are
    # the .env fallbacks and, for PROXY_ENABLED/PROXY_TRUSTED_PROXIES, what the
    # startup guard in main.py can see before a database session exists.
    PROXY_ENABLED: bool = os.getenv("PROXY_ENABLED", "false").lower() == "true"
    # Comma-separated IPs/CIDRs allowed to assert identity headers. EMPTY MEANS
    # REFUSE EVERY ASSERTION — the fail-closed state the feature is built around.
    PROXY_TRUSTED_PROXIES: str = os.getenv("PROXY_TRUSTED_PROXIES", "")
    PROXY_EMAIL_HEADER: str = os.getenv("PROXY_EMAIL_HEADER", "X-Forwarded-Email")
    PROXY_NAME_HEADER: str = os.getenv("PROXY_NAME_HEADER", "X-Forwarded-User")
    # No default: a groups header drives in-app group membership, so reading one
    # nobody configured would hand a proxy privilege it was never granted.
    PROXY_GROUPS_HEADER: str = os.getenv("PROXY_GROUPS_HEADER", "")
    PROXY_GROUPS_SEPARATOR: str = os.getenv("PROXY_GROUPS_SEPARATOR", ",")
    # Opt-in, and capped at 'admin' by app/auth/proxy/assertion.py. Empty = off.
    PROXY_ROLE_HEADER: str = os.getenv("PROXY_ROLE_HEADER", "")
    # Optional defence in depth: a constant-time-compared value the proxy must send
    # in X-OpenTranscribe-Proxy-Secret, so an allowlisted-but-misconfigured proxy is
    # not by itself sufficient for takeover.
    PROXY_SHARED_SECRET: str = os.getenv("PROXY_SHARED_SECRET", "")
    # Comma-separated email domain allowlist. Empty admits every domain.
    PROXY_ALLOWED_DOMAINS: str = os.getenv("PROXY_ALLOWED_DOMAINS", "")
    PROXY_JIT_PROVISIONING: bool = os.getenv("PROXY_JIT_PROVISIONING", "true").lower() == "true"

    # Quick access defaults for common providers
    VLLM_BASE_URL: str = os.getenv("VLLM_BASE_URL", "http://localhost:8012/v1")
    VLLM_MODEL_NAME: str = os.getenv("VLLM_MODEL_NAME", "gpt-oss")
    VLLM_API_KEY: str = os.getenv("VLLM_API_KEY", "")

    OPENAI_MODEL_NAME: str = os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL_NAME: str = os.getenv("OLLAMA_MODEL_NAME", "llama2:7b-chat")

    # Haiku 4.5 is the documented replacement for the deprecated claude-3-haiku-20240307.
    # Deliberately still the Haiku tier: this default drives the batch enrichment tasks
    # (summarization, topic extraction, speaker ID) where per-transcript cost matters more
    # than frontier reasoning. Operators wanting a stronger model for chat set
    # ANTHROPIC_MODEL_NAME, or pin one per-conversation in the chat UI.
    ANTHROPIC_MODEL_NAME: str = os.getenv("ANTHROPIC_MODEL_NAME", "claude-haiku-4-5")
    ANTHROPIC_BASE_URL: str = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # NOTE the slug convention differs from Anthropic's first-party API: OpenRouter
    # uses a dot ("claude-haiku-4.5"), the first-party ID uses dashes
    # ("claude-haiku-4-5"). Mixing them yields a 404 from whichever side is wrong.
    OPENROUTER_MODEL_NAME: str = os.getenv("OPENROUTER_MODEL_NAME", "anthropic/claude-haiku-4.5")
    OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")

    # ===== Amazon Bedrock =====
    # AWS-native LLM access via the Converse API. There is deliberately NO API-key
    # setting: boto3 resolves credentials through the standard chain (instance role,
    # task role, profile, environment), so a deployment on EC2/ECS/EKS needs no secret
    # provisioned at all — which is most of the operational appeal over a raw API key.
    # Region falls back to the AWS SDK's own variables so an already-configured host
    # needs nothing extra.
    BEDROCK_REGION: str = os.getenv(
        "BEDROCK_REGION", os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", ""))
    )
    # Bare foundation-model ID; a geography prefix is applied at call time to select the
    # cross-region inference profile (see llm_bedrock.resolve_model_id). Set a fully
    # prefixed ID or a profile ARN here to bypass that and pin an exact profile.
    BEDROCK_MODEL_NAME: str = os.getenv(
        "BEDROCK_MODEL_NAME", "anthropic.claude-haiku-4-5-20251001-v1:0"
    )

    # ===== ASR (Speech Recognition) Provider =====
    ASR_PROVIDER: str = os.getenv("ASR_PROVIDER", "local")
    DEEPGRAM_API_KEY: str = os.getenv("DEEPGRAM_API_KEY", "")
    DEEPGRAM_MODEL: str = os.getenv("DEEPGRAM_MODEL", "nova-3")
    ASSEMBLYAI_API_KEY: str = os.getenv("ASSEMBLYAI_API_KEY", "")
    ASSEMBLYAI_MODEL: str = os.getenv("ASSEMBLYAI_MODEL", "universal")
    # OpenAI ASR — reuses OPENAI_API_KEY defined above under LLM settings
    OPENAI_ASR_API_KEY: str = os.getenv("OPENAI_ASR_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    OPENAI_ASR_MODEL: str = os.getenv("OPENAI_ASR_MODEL", "gpt-4o-transcribe")
    # Google Cloud Speech — credentials file path (service account JSON)
    GOOGLE_ASR_API_KEY: str = os.getenv(
        "GOOGLE_ASR_API_KEY", ""
    )  # alias; prefer GOOGLE_CLOUD_CREDENTIALS
    GOOGLE_CLOUD_CREDENTIALS: str = os.getenv("GOOGLE_CLOUD_CREDENTIALS", "")
    GOOGLE_ASR_MODEL: str = os.getenv("GOOGLE_ASR_MODEL", "chirp-3")
    AZURE_SPEECH_KEY: str = os.getenv("AZURE_SPEECH_KEY", "")
    AZURE_SPEECH_REGION: str = os.getenv("AZURE_SPEECH_REGION", "eastus")
    # AZURE_SPEECH_MODEL is the canonical name; AZURE_ASR_MODEL is the alias kept for env compat
    AZURE_SPEECH_MODEL: str = os.getenv(
        "AZURE_SPEECH_MODEL", os.getenv("AZURE_ASR_MODEL", "whisper")
    )
    AZURE_ASR_MODEL: str = os.getenv("AZURE_ASR_MODEL", "whisper")
    # AWS Transcribe — credentials (can also use IAM role / instance profile)
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
    AWS_ASR_MODEL: str = os.getenv("AWS_ASR_MODEL", "standard")
    AWS_TRANSCRIBE_BUCKET: str = os.getenv("AWS_TRANSCRIBE_BUCKET", "")
    SPEECHMATICS_API_KEY: str = os.getenv("SPEECHMATICS_API_KEY", "")
    SPEECHMATICS_MODEL: str = os.getenv("SPEECHMATICS_MODEL", "standard")
    GLADIA_API_KEY: str = os.getenv("GLADIA_API_KEY", "")
    GLADIA_MODEL: str = os.getenv("GLADIA_MODEL", "standard")
    CLOUD_ASR_EXTRACT_EMBEDDINGS: bool = (
        os.getenv("CLOUD_ASR_EXTRACT_EMBEDDINGS", "true").lower() == "true"
    )
    DEPLOYMENT_MODE: str = os.getenv("DEPLOYMENT_MODE", "full")  # full or lite

    # ===== OpenSearch Toggle =====
    OPENSEARCH_ENABLED: bool = os.getenv("OPENSEARCH_ENABLED", "true").lower() == "true"

    # ===== YouTube Anti-Bot Detection Configuration =====
    # Cookie-Based Authentication (allows yt-dlp to use browser cookies)
    YOUTUBE_COOKIE_BROWSER: str | None = os.getenv(
        "YOUTUBE_COOKIE_BROWSER", None
    )  # firefox, chrome, chromium, edge, safari, opera
    YOUTUBE_COOKIE_FILE: str | None = os.getenv(
        "YOUTUBE_COOKIE_FILE", None
    )  # Path to cookies.txt file

    # Playlist Staggering (progressive delays when dispatching videos)
    YOUTUBE_PLAYLIST_STAGGER_ENABLED: bool = (
        os.getenv("YOUTUBE_PLAYLIST_STAGGER_ENABLED", "true").lower() == "true"
    )
    YOUTUBE_PLAYLIST_STAGGER_MIN_SECONDS: int = _int_env("YOUTUBE_PLAYLIST_STAGGER_MIN_SECONDS", 5)
    YOUTUBE_PLAYLIST_STAGGER_MAX_SECONDS: int = _int_env("YOUTUBE_PLAYLIST_STAGGER_MAX_SECONDS", 30)
    YOUTUBE_PLAYLIST_STAGGER_INCREMENT: int = _int_env("YOUTUBE_PLAYLIST_STAGGER_INCREMENT", 5)

    # Pre-Download Jitter (random delay before each download starts)
    YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED: bool = (
        os.getenv("YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED", "true").lower() == "true"
    )
    YOUTUBE_PRE_DOWNLOAD_JITTER_MIN_SECONDS: int = _int_env(
        "YOUTUBE_PRE_DOWNLOAD_JITTER_MIN_SECONDS", 2
    )
    YOUTUBE_PRE_DOWNLOAD_JITTER_MAX_SECONDS: int = _int_env(
        "YOUTUBE_PRE_DOWNLOAD_JITTER_MAX_SECONDS", 15
    )

    # User Rate Limiting (per-user quotas to prevent abuse)
    YOUTUBE_USER_RATE_LIMIT_ENABLED: bool = (
        os.getenv("YOUTUBE_USER_RATE_LIMIT_ENABLED", "true").lower() == "true"
    )
    YOUTUBE_USER_RATE_LIMIT_PER_HOUR: int = _int_env("YOUTUBE_USER_RATE_LIMIT_PER_HOUR", 50)
    YOUTUBE_USER_RATE_LIMIT_PER_DAY: int = _int_env("YOUTUBE_USER_RATE_LIMIT_PER_DAY", 500)

    # Recovery throttle: max YouTube downloads re-queued per health-check cycle
    # (every 10 min).  Keep this well below YOUTUBE_USER_RATE_LIMIT_PER_HOUR / 6
    # to leave headroom for user-initiated downloads.
    YOUTUBE_RECOVERY_BATCH_SIZE: int = _int_env("YOUTUBE_RECOVERY_BATCH_SIZE", 3)

    # Master switch for automatic YouTube download retries.
    # Set to false to stop all automatic re-attempts (both Celery task retries
    # and the recovery task's periodic re-queuing).  Manual downloads via the
    # UI are NOT affected — only automatic/background retry loops are disabled.
    # Re-enable by setting to true and restarting the celery-worker container.
    YOUTUBE_AUTO_RETRY_ENABLED: bool = (
        os.getenv("YOUTUBE_AUTO_RETRY_ENABLED", "true").lower() == "true"
    )

    # Celery task-level rate limit for YouTube downloads.
    # Format: "N/h" (per hour), "N/m" (per minute), "N/s" (per second).
    # This is enforced by the download worker regardless of how many tasks
    # are queued.  Set to "0" or empty to disable.
    YOUTUBE_DOWNLOAD_RATE_LIMIT: str = os.getenv("YOUTUBE_DOWNLOAD_RATE_LIMIT", "30/h")

    # Performance optimization properties
    @property
    def effective_use_gpu(self) -> bool:
        """Determine if GPU should be used based on hardware detection."""
        if self.USE_GPU.lower() == "auto":
            try:
                from app.utils.hardware_detection import detect_hardware

                config = detect_hardware()
                return config.device in ["cuda", "mps"]
            except ImportError:
                return False
        return self.USE_GPU.lower() == "true"

    @property
    def effective_torch_device(self) -> str:
        """Get the effective torch device."""
        if self.TORCH_DEVICE.lower() == "auto":
            try:
                from app.utils.hardware_detection import detect_hardware

                config = detect_hardware()
                return config.device
            except ImportError:
                return "cpu"
        return self.TORCH_DEVICE.lower()

    @property
    def effective_compute_type(self) -> str:
        """Get the effective compute type."""
        if self.COMPUTE_TYPE.lower() == "auto":
            try:
                from app.utils.hardware_detection import detect_hardware

                config = detect_hardware()
                return config.compute_type
            except ImportError:
                return "int8"
        return self.COMPUTE_TYPE.lower()

    @property
    def effective_batch_size(self) -> int:
        """Get the effective batch size."""
        if self.BATCH_SIZE.lower() == "auto":
            try:
                from app.utils.hardware_detection import detect_hardware

                config = detect_hardware()
                return config.batch_size
            except ImportError:
                return 1
        return int(self.BATCH_SIZE)

    # Storage paths (container paths, mounted from host via docker-compose volumes)
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", "/app/data"))
    UPLOAD_DIR: Path = DATA_DIR / "uploads"
    MODEL_BASE_DIR: Path = Path(os.getenv("MODELS_DIR", "/app/models"))
    TEMP_DIR: Path = Path(os.getenv("TEMP_DIR", "/app/temp"))

    # ===== Watch Sources (auto-import from local folder / S3 / SMB) =====
    # Only PHYSICAL paths live in env (they map to a real container volume mount).
    # Every other watch setting — the master toggle, per-source connection
    # details/credentials/schedules, and the global tuning knobs (file stability,
    # max concurrent imports, FS-events) — is DB-backed and managed live from the
    # admin UI with NO restart (see watch_settings_service + the watch_source
    # table). Container path of the mounted local watch folder; empty → the
    # "local" source type is hidden in the UI. Mounted by docker-compose.watch.yml.
    WATCH_FOLDER_PATH: str = os.getenv("WATCH_FOLDER_PATH", "")
    # Temp dir for downloaded/stitched files; falls back to TEMP_DIR when empty.
    WATCH_TEMP_DIR: str = os.getenv("WATCH_TEMP_DIR", "")

    @property
    def watch_temp_dir(self) -> Path:
        """Resolved temp dir for watch-source downloads (falls back to TEMP_DIR)."""
        return Path(self.WATCH_TEMP_DIR) if self.WATCH_TEMP_DIR else self.TEMP_DIR

    # Initialization (CORS and directories)
    def __init__(self, **data):
        super().__init__(**data)
        # Ensure directories exist
        self.UPLOAD_DIR.mkdir(exist_ok=True, parents=True)
        self.TEMP_DIR.mkdir(exist_ok=True, parents=True)

    class Config:
        # `env_file` is resolved against the WORKING DIRECTORY, which made the whole test
        # suite CWD-sensitive: `pytest` from `backend/` found no env file and used the
        # values conftest exports, while the same command from the repo root loaded the
        # operator's real `.env` into a unit-test run. That is not a cosmetic difference —
        # it produced two false failures, one of them a security test that stopped
        # exercising the SSRF guard and started asserting that nothing happened to be
        # listening on a local port, i.e. a control that passed for the wrong reason.
        #
        # Under `TESTING` no env file is loaded at all, so both invocations see exactly the
        # environment the fixtures set. This is also already the de-facto contract: the
        # supported `backend/`-relative run never found a file here, which is why conftest
        # carries its own defaults for the DB connection.
        env_file = None if os.getenv("TESTING", "").lower() in ("1", "true") else ".env"
        case_sensitive = True
        extra = "ignore"  # Ignore env vars not defined in Settings (e.g., from docker-compose)


settings = Settings()
