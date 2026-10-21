"""Build the speaker detector a pipeline plan names."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

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
    """Turn a plan's speaker-detection engine into a provider.

    ``services/pipeline_plan.py`` decides WHICH detector runs; nothing here re-derives that
    from a stored source. Building reads the database (a stored key) and returns a plain
    provider object, so the caller can close its session before the provider does any slow
    or network work.
    """

    @staticmethod
    def create_for_engine(
        engine: str,
        user_id: int,
        db: Session,
        *,
        config_uuid: str | None = None,
    ) -> DiarizationProvider:
        """The detector for a post-stage ``engine`` (``pipeline_plan.post_stage_engine``).

        Args:
            engine: ``local`` (this server's engine, chosen by the admin as
                ``engine.diarizer_backend``), ``pyannote_ai`` (the user's pyannote.ai key)
                or ``asr_config``.
            user_id: Whose credential to use.
            db: An open session (reads only).
            config_uuid: The ASR configuration named as the detector (``asr_config``).

        Raises:
            DiarizationNotConfiguredError: ``pyannote_ai`` and the user has no usable key
                (none stored, or one that no longer decrypts; issue #1204).
            DiarizerConfigUnusableError: ``asr_config`` whose configuration is gone, no
                longer shared, local, text-only, untimed or undecryptable, or an engine no
                plan produces.
        """
        if engine == "local":
            from .local_provider import LocalDiarizationProvider

            return LocalDiarizationProvider()

        if engine == "pyannote_ai":
            from app.utils.encryption import decrypt_api_key

            config = get_pyannote_credential(user_id, db)
            api_key = decrypt_api_key(str(config.api_key)) if config and config.api_key else None
            if not api_key:
                logger.warning(
                    "User %d is planned for pyannote.ai speaker detection but has no usable key",
                    user_id,
                )
                raise DiarizationNotConfiguredError()

            from .pyannote_provider import PyAnnoteCloudDiarizationProvider
            from .pyannote_provider import current_pyannote_model

            return PyAnnoteCloudDiarizationProvider(
                api_key=api_key,
                model_name=current_pyannote_model(config.model_name if config else None),
            )

        if engine == "asr_config":
            # Another saved provider's speaker detection (#1226). The plan already resolved
            # "same provider and model as the transcription" to that provider's own speakers,
            # so reaching here means a separate, billed call is intended.
            cfg = get_diarizer_asr_config(db, user_id, config_uuid)
            if cfg is None:
                logger.warning(
                    "User %d is planned for another provider's speakers but config %s is "
                    "unavailable",
                    user_id,
                    config_uuid,
                )
            return _adapter_for_config(cfg)

        logger.warning(
            "No speaker detector can be built for engine %r (user %d, config %s)",
            engine,
            user_id,
            config_uuid,
        )
        raise DiarizerConfigUnusableError()
