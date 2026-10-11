"""Factory for creating diarization provider instances."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from app.core.constants import DEFAULT_DIARIZATION_SOURCE
from app.core.exceptions import ASRConfigurationError
from app.services.error_categorization_service import DIARIZATION_NOT_CONFIGURED_MESSAGE

from .base import DiarizationProvider
from .errors import DiarizerConfigUnusableError

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.orm import Session

    from app.models.user_asr_settings import UserASRSettings
    from app.models.user_diarization_settings import UserDiarizationSettings

logger = logging.getLogger(__name__)

#: The ``user_diarization_settings`` identity of a user's pyannote.ai credential: one row
#: per user (issue #1204), written only by ``api/endpoints/diarization_settings.py``.
PYANNOTE_PROVIDER = "pyannote"
PYANNOTE_CREDENTIAL_NAME = "pyannote.ai"


class DiarizationNotConfiguredError(RuntimeError):
    """The user chose pyannote.ai speaker detection and has no usable API key (#1204).

    ``str()`` is the fixed user-facing sentence registered in
    ``ErrorCategorizationService``, so the transcription failure handler classifies it
    exactly (reason ``diarization_not_configured``, never retried automatically) and stores
    nothing but that sentence.
    """

    MESSAGE = DIARIZATION_NOT_CONFIGURED_MESSAGE

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


def get_pyannote_credential(user_id: int, db: Session) -> UserDiarizationSettings | None:
    """The user's own active pyannote.ai credential row, or ``None``."""
    from app.models.user_diarization_settings import UserDiarizationSettings

    return (
        db.query(UserDiarizationSettings)
        .filter(
            UserDiarizationSettings.user_id == user_id,
            UserDiarizationSettings.provider == PYANNOTE_PROVIDER,
            UserDiarizationSettings.is_active.is_(True),
        )
        .first()
    )


def has_pyannote_credential(user_id: int, db: Session) -> bool:
    """Whether the user has a stored pyannote.ai key (not checked against the vendor)."""
    row = get_pyannote_credential(user_id, db)
    return bool(row is not None and row.api_key)


def get_diarizer_asr_config(
    db: Session, user_id: int, config_uuid: str | None, request: Request | None = None
) -> UserASRSettings | None:
    """The saved ASR configuration named as the speaker detector, if the user may use it.

    Own, or shared within a tenant both belong to (re-checked on every read, like the active
    ASR config). Using another provider for speakers needs the per-user cloud-provider
    capability (``asr.user_providers``); without it the config is treated as unavailable.
    ``None`` for no pointer, a malformed or dangling one, or a capability that is off.
    """
    if not config_uuid:
        return None
    try:
        parsed = uuid.UUID(str(config_uuid))
    except ValueError:
        logger.warning("Malformed diarizer config pointer for user %d", user_id)
        return None
    from app.core.capabilities import capability_enabled

    if not capability_enabled("asr.user_providers", request):
        return None
    from app.models.user_asr_settings import UserASRSettings
    from app.utils.tenant_sharing import shared_usable_by

    cfg: UserASRSettings | None = (
        db.query(UserASRSettings)
        .filter(
            UserASRSettings.uuid == parsed,
            shared_usable_by(UserASRSettings.user_id, UserASRSettings.is_shared, user_id),
        )
        .first()
    )
    return cfg


def _adapter_for_config(cfg: UserASRSettings | None) -> DiarizationProvider:
    """An :class:`ASRDiarizationAdapter` over a saved ASR configuration.

    Builds the ASR provider through ``ASRProviderFactory.create_from_db_config`` (the one
    decryption path). Makes no network call; the caller runs ``diarize`` with no session open.

    Raises:
        DiarizerConfigUnusableError: No config, a local one, a model that returns no speakers
            or no timing, or credentials that no longer decrypt.
    """
    from app.services.asr.factory import ASRProviderFactory

    from .asr_adapter import ASRDiarizationAdapter

    if cfg is None or (cfg.provider or "local") == "local":
        raise DiarizerConfigUnusableError()
    model = str(cfg.model_name or "")
    caps = ASRProviderFactory.get_model_capabilities(str(cfg.provider), model)
    if not caps["supports_diarization"] or caps["timestamp_granularity"] == "none":
        raise DiarizerConfigUnusableError()
    try:
        asr_provider = ASRProviderFactory.create_from_db_config(cfg)
    except ASRConfigurationError:
        raise
    except Exception as exc:
        # Decryption failure (a corrupt key, or ENCRYPTION_KEY changed). The text names the
        # config id, never a secret; it stays in the log.
        logger.warning(
            "Speaker-detection config %s cannot be built (%s)", cfg.uuid, type(exc).__name__
        )
        raise DiarizerConfigUnusableError() from exc
    return ASRDiarizationAdapter(
        asr_provider, model_name=model, speaker_hints=caps["speaker_count_hints"]
    )


class DiarizationProviderFactory:
    """Create diarization provider instances based on user settings."""

    @staticmethod
    def create_for_source(
        source: str,
        api_key: str | None = None,
        model_name: str | None = None,
        *,
        asr_config: UserASRSettings | None = None,
    ) -> DiarizationProvider | None:
        """Create a diarization provider for the given source.

        Args:
            source: One of ``core.constants.VALID_DIARIZATION_SOURCES``.
            api_key: API key for cloud providers (pyannote).
            model_name: Model identifier for cloud providers.
            asr_config: For ``asr_config``: the saved ASR configuration whose speaker
                detection is used (resolve it with :func:`get_diarizer_asr_config`).

        Returns:
            A ``DiarizationProvider`` instance, or ``None`` when the source is
            ``"provider"`` (use ASR provider's built-in diarization), ``"off"``
            (diarization disabled), or ``"local"`` (run on our own GPU — resolved by
            ``rediarize_task`` calling ``ModelManager.get_diarizer()`` directly, never
            through this factory; see the class-level note on ``VALID_DIARIZATION_SOURCES``
            usage in ``app/services/diarization/CLAUDE.md``).

        Raises:
            ValueError: If *source* is unknown or a required API key is missing.
            DiarizerConfigUnusableError: ``asr_config`` with no usable configuration.
        """
        if source in ("provider", "off", "local"):
            return None

        if source == "asr_config":
            return _adapter_for_config(asr_config)

        if source == "pyannote":
            from app.core.constants import PYANNOTE_DEFAULT_DIARIZATION_MODEL

            from .pyannote_provider import PyAnnoteCloudDiarizationProvider

            if not api_key:
                raise ValueError("pyannote.ai diarization requires an API key")
            provider: DiarizationProvider = PyAnnoteCloudDiarizationProvider(
                api_key=api_key,
                model_name=model_name or PYANNOTE_DEFAULT_DIARIZATION_MODEL,
            )
            return provider

        raise ValueError(f"Unknown diarization source: {source}")

    @staticmethod
    def create_for_user(user_id: int, db: Session) -> DiarizationProvider | None:
        """Create diarization provider from user's persisted settings.

        Args:
            user_id: The user whose settings to load.
            db: An active SQLAlchemy ``Session``.

        Returns:
            A ``DiarizationProvider`` instance, or ``None`` when diarization_source
            is ``"provider"``, ``"off"``, or ``"local"`` (the ``local`` source runs on our
            own GPU via ``rediarize_task`` -> ``ModelManager.get_diarizer()`` directly,
            never through this factory — issue #672).

        Raises:
            DiarizationNotConfiguredError: The source is ``"pyannote"`` and the user has no
                stored key, or the stored key no longer decrypts (issue #1204). This used to
                return ``None``, which the cloud pipeline turned into a transcript with no
                speakers and a log line nobody saw.
            DiarizerConfigUnusableError: The source is ``"asr_config"`` and the named
                configuration is gone, unshared, unusable, or returns no speakers (#1226).
        """
        from app.models import UserSetting

        # Read diarization source setting
        source_setting = (
            db.query(UserSetting)
            .filter(
                UserSetting.user_id == user_id,
                UserSetting.setting_key == "transcription_diarization_source",
            )
            .first()
        )
        source = source_setting.setting_value if source_setting else DEFAULT_DIARIZATION_SOURCE
        from app.core.locked_settings import locked_transcription_fields

        if "diarization_source" in locked_transcription_fields():
            # The deployment owns the diarization source (issue #1109).
            source = DEFAULT_DIARIZATION_SOURCE

        if source in ("provider", "off", "local"):
            return None

        if source == "asr_config":
            return _asr_config_for_user(user_id, db)

        if source == "pyannote":
            from app.utils.encryption import decrypt_api_key

            config = get_pyannote_credential(user_id, db)
            api_key = decrypt_api_key(str(config.api_key)) if config and config.api_key else None
            if not api_key:
                logger.warning(
                    "User %d chose pyannote.ai diarization but has no usable stored key",
                    user_id,
                )
                raise DiarizationNotConfiguredError()

            from .pyannote_provider import PyAnnoteCloudDiarizationProvider
            from .pyannote_provider import current_pyannote_model

            provider: DiarizationProvider = PyAnnoteCloudDiarizationProvider(
                api_key=api_key,
                model_name=current_pyannote_model(config.model_name if config else None),
            )
            return provider

        logger.warning("Unknown diarization source '%s' for user %d", source, user_id)
        return None


def _asr_config_for_user(user_id: int, db: Session) -> DiarizationProvider | None:
    """The user's ``asr_config`` speaker detector, or ``None`` when it is their transcriber.

    A config with the same provider and model as the user's active transcription is that
    provider's own speaker detection (the plan's ``diarizer_same_as_transcription``): no
    second, double-billed call is made; ``None`` means "use the ASR provider's speakers".
    """
    from app.models import UserSetting
    from app.services.asr.factory import ASRProviderFactory
    from app.services.pipeline_plan_inputs import DIARIZER_CONFIG_KEY

    pointer = (
        db.query(UserSetting.setting_value)
        .filter(UserSetting.user_id == user_id, UserSetting.setting_key == DIARIZER_CONFIG_KEY)
        .scalar()
    )
    cfg = get_diarizer_asr_config(db, user_id, pointer)
    if cfg is None:
        logger.warning("User %d chose another provider for speakers but it is unavailable", user_id)
        raise DiarizerConfigUnusableError()
    provider, model, _active = ASRProviderFactory.resolve_active_config(user_id, db)
    if (str(cfg.provider), str(cfg.model_name or "")) == (provider, model):
        return None
    return _adapter_for_config(cfg)
