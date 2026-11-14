import json
import logging
import os
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Literal
from urllib.parse import quote

from pydantic import ValidationInfo
from pydantic import field_validator
from pydantic import model_validator
from pydantic_settings import NoDecode
from pydantic_settings import SettingsConfigDict

from app.core.auth_settings_checks import validate_ldap_settings
from app.core.auth_settings_checks import validate_oidc_settings
from app.core.auth_settings_checks import validate_pki_settings
from app.core.broker_connection import assemble_redis_url
from app.core.constants import AUDIT_RETENTION_FLOOR_DAYS
from app.core.env import env_bool
from app.core.settings_capacity import CapacitySettings
from app.core.settings_concurrency import ConcurrencySettings
from app.core.settings_deployment import DeploymentSettings
from app.core.settings_identity import IdentitySettings
from app.core.settings_providers import ProviderSettings
from app.core.settings_proxy_auth import ProxyAuthSettings
from app.core.settings_request_limits import RequestLimitSettings
from app.core.settings_search import SearchSettings
from app.core.settings_security import SecuritySettings
from app.core.settings_storage import StorageSettings

_config_logger = logging.getLogger(__name__)


# The ONLY environment names that relax security controls. Anything else — including
# a typo, an empty string, or an unset variable falling back to the default — is
# treated as production and gets the hardened path. Keep this list closed: adding a
# name here disables default-secret refusal, DEBUG enforcement, the Redis-password
# requirement, and the cookie Secure flag for that value (issue #284 A0.3).
RELAXED_ENVIRONMENTS = frozenset({"development", "dev", "testing", "test", "local"})

# The Vite dev server's origins — CORS_ORIGINS' default in a relaxed environment only.
DEV_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def is_relaxed_environment(environment: str) -> bool:
    """Whether *environment* names a non-production environment.

    Args:
        environment: The raw ``ENVIRONMENT`` value.

    Returns:
        True only for an explicit member of :data:`RELAXED_ENVIRONMENTS`.
    """
    return environment.strip().lower() in RELAXED_ENVIRONMENTS


# The symmetric encryption algorithms ``app/utils/encryption.py`` actually IMPLEMENTS.
#
# ⚠️ This is keyed on the code, NOT on what FIPS approves. AES-256-CCM is equally
# FIPS-approved and adding its name here without writing the code would be a data-loss
# bug, not a compliance win: the envelopes (``v3:``/``v4:salt:nonce:ciphertext``) record
# no algorithm field, so decrypt has to use exactly the algorithm encrypt used. Every
# ciphertext already in the database — LLM/ASR provider keys, MFA TOTP secrets, SMB/SMTP
# credentials, backup secrets — would become undecryptable.
#
# ``ENCRYPTION_ALGORITHM_V3`` is therefore validated against this set at boot
# (``app.core.startup_validation._validate_production_secrets``) rather than dispatched on at runtime: a
# FIPS deployment configured for an algorithm this build does not implement refuses to
# start instead of silently encrypting with a different one than its compliance
# documentation claims.
IMPLEMENTED_ENCRYPTION_ALGORITHMS = frozenset({"AES-256-GCM"})


def resolve_ldap_search_filter(search_filter: str, username_attr: str) -> str:
    """Substitute the ``{username_attr}`` placeholder in an LDAP search filter.

    Shared by the ``.env``-backed :class:`Settings` validator (below) and
    ``LdapConfig.from_db`` (``app/auth/ldap_auth.py``) so this bug class — a search
    filter loaded with the literal placeholder still in it, matching no real LDAP
    attribute — cannot recur in only one of the two config-loading paths again. See
    ``LDAP_USER_SEARCH_FILTER``'s declaration for why the placeholder exists at all.

    Args:
        search_filter: The raw filter, e.g. ``"({username_attr}={username})"``.
        username_attr: The resolved LDAP username attribute, e.g. ``"uid"``.

    Returns:
        The filter with ``{username_attr}`` substituted. ``{username}`` is left
        untouched — it is filled in per-login with the escaped username.
    """
    return search_filter.replace("{username_attr}", username_attr)


class Settings(
    SecuritySettings,
    StorageSettings,
    SearchSettings,
    IdentitySettings,
    ProviderSettings,
    ProxyAuthSettings,
    DeploymentSettings,
    RequestLimitSettings,
    CapacitySettings,
    ConcurrencySettings,
):
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
    DEBUG: bool | None = None  # unset: on in a relaxed environment (see validate_auth_settings)

    # Whether the API runs Alembic on startup (issue #284 A1.4). True suits self-host,
    # where one container owns the database. Set false on an orchestrated deploy where a
    # dedicated migrate Job owns migrations — otherwise every API replica races Alembic
    # on rollout. When false, /health/ready asserts the schema is at head and fails 503
    # if not, so a replica pointed at an un-migrated database never takes traffic.
    RUN_MIGRATIONS_ON_STARTUP: bool = env_bool("RUN_MIGRATIONS_ON_STARTUP", True)

    # Whether anyone can create their own account via POST /api/auth/register.
    # New users are immediately active and GPU-capable, so on a public deployment this
    # is the door in front of every per-user cost (issue #284 A0.11). Set false when an
    # external IdP owns identity, or when accounts should be admin-provisioned.
    ALLOW_OPEN_REGISTRATION: bool = env_bool("ALLOW_OPEN_REGISTRATION", True)

    # Whether a newly provisioned account must be approved by an administrator
    # before it can use anything. Applies to self-registration AND to every
    # just-in-time account an external IdP creates (app/auth/approval.py).
    #
    # Defaults FALSE so an upgrade changes nothing: with it off, accounts are
    # created 'approved' exactly as before. DB-backed override: auth_config
    # `require_account_approval` (Settings -> Authentication -> Local).
    REQUIRE_ACCOUNT_APPROVAL: bool = env_bool("REQUIRE_ACCOUNT_APPROVAL", False)

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
    LOCAL_AUTH_ENABLED: bool = env_bool("LOCAL_AUTH_ENABLED", True)

    # SSRF egress policy for user-supplied endpoint URLs (issue #284 A0.1/A0.10).
    # Self-hosted Ollama/vLLM on a private LAN is a legitimate setup, so a single-tenant
    # deployment can opt back into private targets. MUST stay false on anything
    # multi-tenant or publicly registerable: with it on, any user can point a
    # "test connection" at internal services and cloud instance metadata.
    LLM_ALLOW_PRIVATE_ENDPOINTS: bool = env_bool("LLM_ALLOW_PRIVATE_ENDPOINTS", False)
    # Same policy for watch-source S3 endpoints and SMB servers. A self-hosted NAS on
    # the LAN is the normal case for single-tenant installs, so this defaults ON for
    # watch sources; turn it OFF on a multi-tenant or publicly-registerable deployment.
    WATCH_ALLOW_PRIVATE_ENDPOINTS: bool = env_bool("WATCH_ALLOW_PRIVATE_ENDPOINTS", True)
    # Same policy again for cloud ASR provider base URLs (UserASRSettings.base_url /
    # GLADIA_API_BASE_URL — issue #594). A SEPARATE flag from LLM_ALLOW_PRIVATE_ENDPOINTS
    # on purpose: an operator may want a self-hosted LLM but not a self-hosted ASR
    # endpoint, or vice versa, and coupling the two would silently loosen one policy
    # while an admin thought they were only touching the other. MUST stay false on
    # anything multi-tenant or publicly registerable, same rationale as LLM's.
    ASR_ALLOW_PRIVATE_ENDPOINTS: bool = env_bool("ASR_ALLOW_PRIVATE_ENDPOINTS", False)

    # Cookie Secure-flag override for plain-HTTP LAN deployments (issue #284 A0.3
    # follow-up). `is_hardened` forces every auth cookie's `Secure` attribute on
    # whenever `ENVIRONMENT` is not explicitly relaxed -- which is correct when
    # hardened mode means "behind real TLS", but a homelab or small-business
    # deployment on a plain-HTTP LAN IP is ALSO the normal, legitimate case for a
    # single-tenant self-hosted install. A `Secure` cookie set over plain HTTP is
    # silently dropped by the browser on any origin other than localhost/127.0.0.1,
    # so a LAN IP with no TLS-terminating reverse proxy in front gets a login that
    # appears to succeed and then can never hold a session -- indistinguishable
    # from a wrong password. This does NOT relax `is_hardened` itself: secret
    # enforcement, rate limits, lockout thresholds, and DEBUG-off all stay exactly
    # as strict. It relaxes only the one attribute that requires TLS to function,
    # and it is off by default so nothing changes unless an operator opts in.
    ALLOW_INSECURE_COOKIES: bool = env_bool("ALLOW_INSECURE_COOKIES", False)

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

    # Tenancy mode: "auto" (default), "single" or "multi". Read by
    # app.services.platform_access.tenancy_mode, which fails closed (unknown -> multi).
    # Deliberately an env var and not a SystemSettings row: it constrains platform admins, so
    # an admin must not be able to switch it off from the UI.
    TENANCY_MODE: str = os.getenv("TENANCY_MODE", "auto")

    # JWT Token settings (NIST SP 800-63B compliant)
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "this_should_be_changed_in_production")
    JWT_ALGORITHM: str = "HS256"
    # Access token expiration: 60 minutes (NIST recommended for moderate assurance)
    # Can be reduced to 15-30 minutes for high-security environments
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    # Session idle timeout: 15 minutes (NIST moderate assurance, DoD STIG compliant)
    SESSION_IDLE_TIMEOUT_MINUTES: int = 15
    # Session absolute timeout: 8 hours (force re-authentication)
    SESSION_ABSOLUTE_TIMEOUT_MINUTES: int = 480
    # Apply SESSION_ABSOLUTE_TIMEOUT_MINUTES to tokens from a registered external
    # identity provider, keyed on the token's auth_time (issue #1106). A verifier
    # that supplies no auth_time is not enforced (logged), so this cannot lock
    # everyone out. No effect when no external verifier is registered.
    EXTERNAL_SESSION_ABSOLUTE_TIMEOUT_ENFORCED: bool = True

    @field_validator(
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "API_THREADPOOL_SIZE",
        "SEARCH_BULK_BATCH_SIZE",
        "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS",
    )
    @classmethod
    def _clamp_pool_and_batch_floors(cls, v: int, info: ValidationInfo) -> int:
        """Floor each field again, AFTER pydantic-settings has resolved it.

        Same bug class as ``_clamp_chunks_index_topology`` above: the ``max(_int_env(...),
        N)`` written on each field default only runs once, at class BODY execution time, to
        compute the field's *default* — but ``Settings`` is a ``BaseSettings`` subclass, so
        pydantic-settings separately re-reads the matching env var at every ``Settings()``
        construction and casts it straight to ``int``, bypassing that default (and its
        clamp) entirely whenever the var is actually set. A ``DB_POOL_SIZE`` env var reached
        ``settings.DB_POOL_SIZE`` unclamped until this validator was added — a measured +3
        discrepancy versus the intended floor.
        """
        floors = {
            "DB_POOL_SIZE": 1,
            "DB_MAX_OVERFLOW": 0,
            "API_THREADPOOL_SIZE": 0,
            "SEARCH_BULK_BATCH_SIZE": 1,
            "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS": 0,
        }
        assert info.field_name is not None  # always set for a field_validator
        return max(v, floors[info.field_name])

    # Observability. LOG_FORMAT="json" switches the root logger to structured
    # JSON lines (Loki/CloudWatch-ready); "text" keeps the human-readable format.
    # SLOW_QUERY_MS gates the slow-query WARNING in app.core.db_metrics.
    LOG_FORMAT: str = "text"
    SLOW_QUERY_MS: int = 500
    SETTINGS_CACHE_TTL: int = 30
    READ_CACHE_ENABLED: bool = True

    # Celery settings. Plain "" defaults (not `= REDIS_URL`, which at class-body
    # execution time would bind to REDIS_URL's own pre-validator "" placeholder,
    # not its resolved value) resolved in `validate_auth_settings` below.
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    @property
    def db_role_split_requested(self) -> bool:
        """Whether the operator asked for the owner/app database role split."""
        return bool(self.POSTGRES_APP_USER) or self.MIGRATION_DATABASE_URL != self.DATABASE_URL

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

    # CORS settings. The Vite dev origins are the default only in a relaxed environment:
    # a hardened deployment that leaves CORS_ORIGINS unset resolves to [] in
    # validate_auth_settings. The SPA is served same-origin and needs no CORS entry, while
    # the dev default let any page on a user's localhost:5173 make credentialed requests
    # and still never listed the app's own origin (issue #1029).
    #
    # NoDecode: pydantic-settings JSON-decodes a list field's env value before any
    # validator runs, so the documented comma-separated form
    # (`CORS_ORIGINS=https://a,https://b`) raised SettingsError at startup. The
    # validator below parses both forms itself.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = list(DEV_CORS_ORIGINS)

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            if v.lstrip().startswith("["):
                parsed = json.loads(v)
                if not isinstance(parsed, list):
                    raise ValueError(v)
                return [str(i).strip() for i in parsed]
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list):
            return v
        raise ValueError(v)

    def _resolve_database_urls(self) -> None:
        """Resolve DATABASE_URL (runtime) and MIGRATION_DATABASE_URL (owner) and the retention."""
        owner_url = (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )
        if self.POSTGRES_APP_USER and self.DATABASE_URL and not self.MIGRATION_DATABASE_URL:
            raise ValueError(
                "POSTGRES_APP_USER is set and DATABASE_URL is explicit: set "
                "MIGRATION_DATABASE_URL (owner) and point DATABASE_URL at the app role, "
                "or unset POSTGRES_APP_USER"
            )
        if not self.DATABASE_URL:
            if self.POSTGRES_APP_USER:
                self.DATABASE_URL = (
                    f"postgresql://{quote(self.POSTGRES_APP_USER, safe='')}:"
                    f"{quote(self.POSTGRES_APP_PASSWORD, safe='')}"
                    f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
                )
            else:
                self.DATABASE_URL = owner_url
        if not self.MIGRATION_DATABASE_URL:
            self.MIGRATION_DATABASE_URL = owner_url if self.POSTGRES_APP_USER else self.DATABASE_URL
        if 0 < self.AUDIT_LOG_RETENTION_DAYS < AUDIT_RETENTION_FLOOR_DAYS:
            raise ValueError(
                f"AUDIT_LOG_RETENTION_DAYS={self.AUDIT_LOG_RETENTION_DAYS} is below the "
                f"{AUDIT_RETENTION_FLOOR_DAYS}-day floor; use 0 (keep forever) or "
                f">= {AUDIT_RETENTION_FLOOR_DAYS}"
            )

    @model_validator(mode="after")
    def validate_auth_settings(self) -> "Settings":
        """Validate that required fields are set when authentication features are enabled.

        Delegates to helper functions for each authentication type.
        Also validates JWT key security settings.

        Raises:
            ValueError: If a required field is missing when its feature is enabled.
        """
        import warnings

        # Resolve the environment-conditional defaults that can't live in a plain
        # class-body `os.getenv(key, default)` expression — see MFA_REQUIRE_REDIS's
        # declaration above for why. Both fields are `bool | None`, so pydantic-settings
        # itself already distinguishes "genuinely unset" (`None`) from "explicitly set"
        # (a real `True`/`False`, including an explicit `false`/`0` from a .env FILE —
        # `env_ignore_empty` only skips a truly empty string, not a literal false value).
        # Checking `self.<field> is None` here (instead of re-reading raw `os.environ`)
        # is what makes this resolve correctly regardless of whether the value came from
        # process env, a `.env` file, or Docker Compose's `env_file:` — all three are
        # already merged onto `self` by pydantic-settings before this validator runs.
        if self.DEBUG is None:
            self.DEBUG = is_relaxed_environment(self.ENVIRONMENT)
        if self.MFA_REQUIRE_REDIS is None:
            self.MFA_REQUIRE_REDIS = not is_relaxed_environment(self.ENVIRONMENT)
        if self.PKI_REVOCATION_SOFT_FAIL is None:
            self.PKI_REVOCATION_SOFT_FAIL = is_relaxed_environment(self.ENVIRONMENT)
        # CORS_ORIGINS keeps a list type (every consumer iterates it), so "unset" is read
        # from model_fields_set, which pydantic-settings fills from env, .env and init
        # kwargs alike. An explicit value — including `[]` — is always honoured.
        if "CORS_ORIGINS" not in self.model_fields_set and not is_relaxed_environment(
            self.ENVIRONMENT
        ):
            self.CORS_ORIGINS = []

        # Same bug class, for str fields whose default is assembled from other
        # already-resolved fields instead of a bool computed from ENVIRONMENT.
        # `self.POSTGRES_*`/`self.REDIS_*`/etc. are themselves plain `str`
        # fields with LITERAL `os.getenv` defaults, so they are already
        # correctly resolved on `self` by this point regardless of whether
        # this validator runs before or after them.
        self._resolve_database_urls()
        if not self.REDIS_URL:
            self.REDIS_URL = assemble_redis_url(
                use_tls=self.REDIS_USE_TLS,
                host=self.REDIS_HOST,
                port=self.REDIS_PORT,
                password=self.REDIS_PASSWORD,
            )
        if not self.CELERY_BROKER_URL:
            self.CELERY_BROKER_URL = self.REDIS_URL
        if not self.CELERY_RESULT_BACKEND:
            self.CELERY_RESULT_BACKEND = self.REDIS_URL
        if not self.S3_REGION:
            self.S3_REGION = self.AWS_REGION or "us-east-1"
        if not self.BEDROCK_REGION:
            self.BEDROCK_REGION = self.AWS_REGION or os.environ.get("AWS_DEFAULT_REGION") or ""
        if not self.OPENSEARCH_AWS_REGION:
            self.OPENSEARCH_AWS_REGION = self.AWS_REGION

        # Resolve the LDAP search filter's `{username_attr}` placeholder from the
        # resolved LDAP_USERNAME_ATTR field — same bug class as MFA_REQUIRE_REDIS
        # above, a plain-field default instead of a class-body expression computed
        # from another env var. See LDAP_USER_SEARCH_FILTER's declaration.
        self.LDAP_USER_SEARCH_FILTER = resolve_ldap_search_filter(
            self.LDAP_USER_SEARCH_FILTER, self.LDAP_USERNAME_ATTR
        )

        validate_ldap_settings(self)
        validate_oidc_settings(self)
        validate_pki_settings(self)

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

    # Alertmanager webhook bearer token (issue #791); empty = the endpoint refuses every request.
    ALERTMANAGER_WEBHOOK_TOKEN: str = ""

    # Storage paths (container paths, mounted from host via docker-compose volumes)
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", "/app/data"))

    # Where the GDPR erasure journal (services/erasure_ledger_service) is written — the
    # out-of-database copy of every erasure request that lets a restore of an older dump
    # be detected and the erasure re-run. "file" (default): DATA_DIR/gdpr/ on the data
    # volume, unchanged. "object_storage": one object per entry under
    # ERASURE_JOURNAL_OBJECT_PREFIX in the media bucket, for deployments whose containers
    # have no durable writable volume (read-only root filesystem, ephemeral pods) — a
    # journal on an ephemeral path vanishes with the pod, which is the same as no journal.
    # A typo must fail at startup, not silently fall back to a path that cannot be written.
    ERASURE_JOURNAL_BACKEND: Literal["file", "object_storage"] = "file"
    ERASURE_JOURNAL_OBJECT_PREFIX: str = "gdpr/erasure-journal/"
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
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.TEMP_DIR.mkdir(exist_ok=True, parents=True)

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
    #
    # `env_ignore_empty=True`: `.env.example` ships dozens of deliberate `VAR=` blanks
    # meaning "use the coded default" (a convention used throughout that file). Without
    # this, pydantic-settings treats an empty string as an explicit value and its bool/int
    # parsers reject "" outright — this is what crashed a real fresh install on
    # `MFA_REQUIRE_REDIS=` (empty). With it on, an empty env var is treated as unset and
    # the field's plain Python default applies; a malformed NON-empty value (e.g. a stray
    # inline `#` comment pydantic-settings does not strip) still raises loudly, which is
    # the correct, deliberate behavior — see NUM_SPEAKERS below.
    model_config = SettingsConfigDict(
        env_file=None if os.getenv("TESTING", "").lower() in ("1", "true") else ".env",
        case_sensitive=True,
        extra="ignore",  # Ignore env vars not defined in Settings (e.g., from docker-compose)
        env_ignore_empty=True,
    )


settings = Settings()
