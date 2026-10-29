"""Hardened-mode startup checks for service credentials and object-store encryption."""

import logging

from pydantic_settings import BaseSettings

from app.core.config import Settings
from app.core.config import settings

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
