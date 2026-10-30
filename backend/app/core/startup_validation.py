"""Hardened-mode startup checks: boot secrets, FIPS configuration, service credentials, object-store encryption."""

import logging
import os

from pydantic_settings import BaseSettings

from app.core.config import IMPLEMENTED_ENCRYPTION_ALGORITHMS
from app.core.config import Settings
from app.core.config import settings
from app.core.entropy import assert_csprng_available
from app.core.entropy import validate_secret_entropy
from app.core.legacy_auth_env import deprecated_oidc_env_names

logger = logging.getLogger(__name__)


class ServiceCredentialSettings(BaseSettings):
    """Environment inputs read only by ``validate_service_credentials``.

    Shares ``Settings.model_config`` so ``.env`` handling (including the ``TESTING``
    isolation and empty-means-unset) cannot drift from the main settings class.
    """

    model_config = Settings.model_config

    # Basic-auth password for the Flower dashboard.
    FLOWER_PASSWORD: str = ""
    # Bundled-MinIO server-side encryption switch ("on" encrypts every new object).
    MINIO_KMS_AUTO_ENCRYPTION: str = ""
    # Explicit opt-out for a deployment that deliberately stores objects unencrypted
    # (e.g. the volume is encrypted at the host level).
    ALLOW_UNENCRYPTED_OBJECT_STORE: bool = False


service_credential_settings = ServiceCredentialSettings()


def validate_service_credentials(is_production: bool) -> None:
    """Hardened-mode checks for the Redis and Flower passwords and object-store encryption.

    Raises:
        ValueError: If the Redis password is unset in production, the Flower password is unset or a known default, or bundled MinIO
            encryption is off without ``ALLOW_UNENCRYPTED_OBJECT_STORE``.
    """
    if is_production and not settings.REDIS_PASSWORD:
        logger.critical("REDIS_PASSWORD must be set in production!")
        raise ValueError("REDIS_PASSWORD is required in production environment")

    # Flower dashboard password: the compose files fall back to "flower" when unset and
    # .env.example ships a placeholder, so reject both here like the keys above.
    flower_password = service_credential_settings.FLOWER_PASSWORD.strip().lower()
    if is_production and (
        not flower_password
        or flower_password in ("flower", "changeme")
        or "change_me" in flower_password
    ):
        logger.critical(
            "FLOWER_PASSWORD is unset or a known default in production! "
            "Set a strong, unique value via the FLOWER_PASSWORD environment variable."
        )
        raise ValueError("Insecure FLOWER_PASSWORD in production environment")

    # Bundled MinIO only encrypts objects at rest when MINIO_KMS_AUTO_ENCRYPTION=on
    # (compose defaults to off so key-less development installs still start).
    if (
        is_production
        and settings.STORAGE_BACKEND == "minio"
        and service_credential_settings.MINIO_KMS_AUTO_ENCRYPTION.strip().lower()
        not in ("on", "true", "1")
        and not service_credential_settings.ALLOW_UNENCRYPTED_OBJECT_STORE
    ):
        logger.critical(
            "MINIO_KMS_AUTO_ENCRYPTION is not 'on' in production. Enable it, or set "
            "ALLOW_UNENCRYPTED_OBJECT_STORE=true to store objects unencrypted deliberately."
        )
        raise ValueError("MINIO_KMS_AUTO_ENCRYPTION must be on in production environment")


def _validate_fips_cryptographic_configuration() -> None:
    """Enforce the FIPS 140-3 settings this build has to be able to honour.

    A private step of :func:`_validate_production_secrets`, extracted only because that
    function is at its complexity ceiling — **not** a second boot gate. It has exactly one
    caller and must keep exactly one.

    Gated on ``settings.fips_140_3_active`` rather than ``is_hardened``: ``FIPS_MODE``
    defaults to false, so an ordinary dev/CI boot returns immediately, but a deployment
    that has explicitly claimed the FIPS profile must satisfy it whatever its ENVIRONMENT
    says. Both settings below were documented as compliance controls in
    ``docs/FIPS_140_3_COMPLIANCE.md``, ``docs/ENV_VARIABLES_FIPS_140_3.md`` and the
    operations security-hardening guide while being read by nothing outside a test that
    asserted a constant equals its own default. This is what makes that documentation true.

    Raises:
        ValueError: If ``ENCRYPTION_ALGORITHM_V3`` names an algorithm this build does not
            implement, or if ``FIPS_VALIDATE_ENTROPY`` is on and the CSPRNG or the
            configured key material fails validation.
    """
    if not settings.fips_140_3_active:
        return

    # Normalised before comparison: refusing to boot over `aes-256-gcm` vs `AES-256-GCM`
    # would be a false refusal, and a fail-closed control that rejects a correct
    # configuration is a bug rather than extra safety.
    configured_algorithm = settings.ENCRYPTION_ALGORITHM_V3.strip().upper()
    if configured_algorithm not in IMPLEMENTED_ENCRYPTION_ALGORITHMS:
        # Refuse rather than fall back. Silently encrypting with AES-256-GCM while the
        # operator configured something else would make the compliance documentation a
        # lie; switching algorithm to obey the setting would orphan every existing
        # ciphertext, because the v3 envelope records no algorithm field (see
        # IMPLEMENTED_ENCRYPTION_ALGORITHMS in core/config.py).
        implemented = ", ".join(sorted(IMPLEMENTED_ENCRYPTION_ALGORITHMS))
        logger.critical(
            "ENCRYPTION_ALGORITHM_V3=%r is not implemented by this build. "
            "Implemented: %s. Refusing to start.",
            settings.ENCRYPTION_ALGORITHM_V3,
            implemented,
        )
        raise ValueError(
            f"ENCRYPTION_ALGORITHM_V3={settings.ENCRYPTION_ALGORITHM_V3!r} is not "
            f"implemented by this build (implemented: {implemented}). FIPS 140-3 mode "
            "refuses to start rather than encrypt with an algorithm other than the one "
            "configured."
        )

    if not settings.FIPS_VALIDATE_ENTROPY:
        return

    # REDIS_PASSWORD is deliberately not in this list: it is a service credential checked
    # for presence by the caller, not cryptographic key material anything derives from.
    try:
        assert_csprng_available()
        for secret_name in ("ENCRYPTION_KEY", "JWT_SECRET_KEY"):
            validate_secret_entropy(secret_name, getattr(settings, secret_name))
    except ValueError as exc:
        logger.critical("%s Refusing to start.", exc)
        raise


def _validate_production_secrets():
    """Validate that production secrets are properly configured.

    Gated on ``settings.is_hardened`` (fail-closed): every check below applies unless
    ENVIRONMENT explicitly names a relaxed environment. The previous
    ``ENVIRONMENT in ("production", "prod")`` test was never true in practice — nothing
    passes ENVIRONMENT into the containers — so none of these ran anywhere (#284 A0.3).

    The one exception is :func:`_validate_fips_cryptographic_configuration`, which gates on
    ``settings.fips_140_3_active`` instead — see its docstring.
    """
    is_production = settings.is_hardened

    # Check JWT secret key
    insecure_jwt_secrets = (
        "this_should_be_changed_in_production",
        "changeme",
        "secret",
        "your-secret-key",
    )
    # "change_me" catches the .env.example placeholder (CHANGE_ME_auto_generated_on_install)
    # when .env was hand-copied instead of generated by the installer.
    jwt_secret = settings.JWT_SECRET_KEY.lower()
    if is_production and (jwt_secret in insecure_jwt_secrets or "change_me" in jwt_secret):
        logger.error(
            "SECURITY ERROR: JWT_SECRET_KEY is using an insecure default value in production! "
            "Set a strong, unique secret key via the JWT_SECRET_KEY environment variable."
        )
        raise ValueError("Insecure JWT_SECRET_KEY in production environment")

    # Check encryption key
    encryption_key = settings.ENCRYPTION_KEY.lower()
    if is_production and (
        "this_should_be_changed" in encryption_key or "change_me" in encryption_key
    ):
        logger.error(
            "SECURITY ERROR: ENCRYPTION_KEY is using an insecure default value in production! "
            "Set a strong, unique encryption key via the ENCRYPTION_KEY environment variable."
        )
        raise ValueError("Insecure ENCRYPTION_KEY in production environment")

    _validate_fips_cryptographic_configuration()

    # Warn about OIDC audience validation disabled in production
    if is_production and settings.OIDC_ENABLED and not settings.OIDC_VERIFY_AUDIENCE:
        logger.warning(
            "SECURITY WARNING: OIDC_VERIFY_AUDIENCE is disabled in production! "
            "This allows tokens intended for other clients to be accepted. "
            "Set OIDC_VERIFY_AUDIENCE=true and configure OIDC_AUDIENCE for proper token validation."
        )

    # One line, once, naming the retired environment-variable spellings still in use.
    # They keep working permanently (core/legacy_auth_env.py); this only tells the
    # operator that a provider-neutral canonical name now exists.
    deprecated_env = deprecated_oidc_env_names()
    if deprecated_env:
        logger.warning(
            "DEPRECATED: %s still use the pre-rename environment-variable names. "
            "They continue to work and take precedence, but the canonical spelling is "
            "OIDC_* (see docs/OIDC_SETUP.md).",
            ", ".join(deprecated_env),
        )

    # Enforce PKI trusted proxies in production, warn in development
    if settings.PKI_ENABLED and not settings.PKI_TRUSTED_PROXIES:
        if is_production:
            logger.critical(
                "PKI_ENABLED=true but PKI_TRUSTED_PROXIES is empty! "
                "Any client can inject fake certificate headers. Refusing to start."
            )
            raise ValueError("PKI_TRUSTED_PROXIES must be set when PKI_ENABLED=true in production")
        else:
            logger.warning(
                "SECURITY WARNING: PKI_ENABLED=true but PKI_TRUSTED_PROXIES is empty! "
                "This allows any client to inject PKI certificate headers. "
                "Configure PKI_TRUSTED_PROXIES with the address of the proxy that "
                "terminates mTLS — the narrowest range that covers it, e.g. its own "
                "'/32', or the container network it runs on. Do NOT paste a whole "
                "private range: '10.0.0.0/8' and '192.168.0.0/16' are what ordinary "
                "LAN routers hand out, so trusting one lets any device on that LAN "
                "assert a certificate DN and sign in as it (issue #620)."
            )

    # Same rule for trusted-header authentication, and for the same reason: a header
    # nobody vouched for is an attacker-supplied string, and PROXY_ROLE_HEADER can
    # turn one into an admin session. The runtime path fails closed as well
    # (auth/header_trust.py refuses every assertion with an empty allowlist) — this
    # guard is what makes the misconfiguration visible at boot rather than as a
    # silent, total authentication outage.
    #
    # It reads .env only. A deployment that enables proxy auth *solely* in the admin
    # UI is not visible here, because Settings is built before any database session
    # exists; the runtime refusal is what covers that case, and the admin UI's own
    # cross-field validation is where the warning belongs.
    if settings.PROXY_ENABLED and not settings.PROXY_TRUSTED_PROXIES:
        if is_production:
            logger.critical(
                "PROXY_ENABLED=true but PROXY_TRUSTED_PROXIES is empty! "
                "Any client could inject identity headers. Refusing to start."
            )
            raise ValueError(
                "PROXY_TRUSTED_PROXIES must be set when PROXY_ENABLED=true in production"
            )
        logger.warning(
            "SECURITY WARNING: PROXY_ENABLED=true but PROXY_TRUSTED_PROXIES is empty. "
            "Every header-sourced assertion will be REFUSED until you configure the "
            "reverse proxy's addresses (e.g. '10.0.0.0/8,172.16.0.0/12')."
        )

    validate_service_credentials(is_production)

    # Check debug mode in production
    if is_production and settings.DEBUG:
        logger.critical("DEBUG=true in production environment!")
        raise ValueError("DEBUG must be false in production environment")

    # ALLOW_INSECURE_COOKIES strips the auth cookies' Secure flag on plain-HTTP
    # requests to this deployment (see auth/cookies.py:_secure_for_request) — the
    # documented, narrow opt-out for a homelab/small-business LAN install with no
    # TLS-terminating reverse proxy. Logged here, not at cookies.py's import time:
    # this function runs after configure_logging(), so under LOG_FORMAT=json this
    # warning is actually JSON like every other boot line, instead of being the
    # one line a structured log collector could drop or mangle.
    if is_production and settings.ALLOW_INSECURE_COOKIES:
        logger.warning(
            "ALLOW_INSECURE_COOKIES is enabled: auth cookies are set WITHOUT the "
            "Secure flag on plain-HTTP requests to this hardened deployment. This "
            "is intended for a plain-HTTP LAN deployment with no TLS-terminating "
            "reverse proxy. If this deployment is reachable from an untrusted "
            "network, put a TLS-terminating reverse proxy in front of it and "
            "disable this setting instead."
        )

    # Warn about insecure presigned URLs in production
    public_storage_url = settings.STORAGE_PUBLIC_URL or settings.MINIO_PUBLIC_URL
    if is_production and public_storage_url and not public_storage_url.startswith("https://"):
        logger.warning(
            "SECURITY WARNING: STORAGE_PUBLIC_URL/MINIO_PUBLIC_URL uses HTTP instead of "
            "HTTPS. Presigned URLs will be served over an insecure connection in production."
        )

    # TESTING=true enables auth shortcuts (a fabricated user, a password-free login
    # path). Enforcement lives at the USE sites in endpoints/auth.py, which additionally
    # require `not settings.is_hardened` — so the flag is inert in a real deployment even
    # if it leaks into the environment. Warn here rather than refusing to boot: the
    # secrets-guard test suite legitimately simulates production while TESTING is set
    # process-wide by conftest (issue #284 A0.8).
    if is_production and os.environ.get("TESTING", "False").lower() == "true":
        logger.warning(
            "TESTING=true in a hardened environment. Auth shortcuts are disabled by "
            "is_hardened, but this variable should not be set in production."
        )

    # A wildcard origin combined with allow_credentials=True lets any site read
    # authenticated responses. Browsers reject that pairing, but the misconfiguration
    # should surface at boot rather than as confusing CORS failures (issue #284 A0.8).
    if is_production and "*" in settings.CORS_ORIGINS:
        logger.critical(
            "CORS_ORIGINS contains '*' while credentials are allowed. "
            "Set explicit origins. Refusing to start."
        )
        raise ValueError("Wildcard CORS_ORIGINS is not permitted with credentialed requests")

    # The default depends on ENVIRONMENT (issue #1029), so state what was resolved: an
    # operator whose separate-origin frontend is refused can see why from the boot log.
    logger.info(
        "CORS allowed origins: %s (same-origin requests need no entry)",
        settings.CORS_ORIGINS or "none",
    )

    if is_production:
        logger.info("Production security validation passed")
