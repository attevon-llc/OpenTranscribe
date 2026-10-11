"""Build the speaker detector a pipeline plan names."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.services.error_categorization_service import DIARIZATION_NOT_CONFIGURED_MESSAGE
from app.services.error_categorization_service import DIARIZER_CONFIG_UNUSABLE_MESSAGE

from .base import DiarizationProvider

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

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


class DiarizerConfigUnusableError(RuntimeError):
    """The speaker detector a run was planned with cannot be built.

    Raised for ``asr_config`` (another saved ASR configuration's speaker detection), whose
    adapter is not part of this change, and for an engine name no plan produces. ``str()``
    is the fixed sentence registered in ``ErrorCategorizationService`` (reason
    ``pipeline_not_runnable``, permanent retry category ``CONFIGURATION_REQUIRED``).
    """

    MESSAGE = DIARIZER_CONFIG_UNUSABLE_MESSAGE

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
            DiarizerConfigUnusableError: ``asr_config`` (its adapter is not built yet), or
                an engine no plan produces.
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

        logger.warning(
            "No speaker detector can be built for engine %r (user %d, config %s)",
            engine,
            user_id,
            config_uuid,
        )
        raise DiarizerConfigUnusableError()
