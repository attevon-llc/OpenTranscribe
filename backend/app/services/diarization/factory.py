"""Factory for creating diarization provider instances."""

from __future__ import annotations

import logging

from .base import DiarizationProvider

logger = logging.getLogger(__name__)

VALID_DIARIZATION_SOURCES = ("provider", "local", "pyannote", "off")
DEFAULT_DIARIZATION_SOURCE = "provider"


class DiarizationProviderFactory:
    """Create diarization provider instances based on user settings."""

    @staticmethod
    def create_for_source(
        source: str,
        api_key: str | None = None,
        model_name: str | None = None,
    ) -> DiarizationProvider | None:
        """Create a diarization provider for the given source.

        Args:
            source: One of ``VALID_DIARIZATION_SOURCES``.
            api_key: API key for cloud providers (pyannote).
            model_name: Model identifier for cloud providers.

        Returns:
            A ``DiarizationProvider`` instance, or ``None`` when the source is
            ``"provider"`` (use ASR provider's built-in diarization), ``"off"``
            (diarization disabled), or ``"local"`` (run on our own GPU — resolved by
            ``rediarize_task`` calling ``ModelManager.get_diarizer()`` directly, never
            through this factory; see the class-level note on ``VALID_DIARIZATION_SOURCES``
            usage in ``app/services/diarization/CLAUDE.md``).

        Raises:
            ValueError: If *source* is unknown or a required API key is missing.
        """
        if source in ("provider", "off", "local"):
            return None

        if source == "pyannote":
            from .pyannote_provider import PyAnnoteCloudDiarizationProvider

            if not api_key:
                raise ValueError("pyannote.ai diarization requires an API key")
            provider: DiarizationProvider = PyAnnoteCloudDiarizationProvider(
                api_key=api_key,
                model_name=model_name or "precision-2",
            )
            return provider

        raise ValueError(f"Unknown diarization source: {source}")

    @staticmethod
    def create_for_user(user_id: int, db) -> DiarizationProvider | None:  # noqa: ANN001
        """Create diarization provider from user's persisted settings.

        Args:
            user_id: The user whose settings to load.
            db: An active SQLAlchemy ``Session``.

        Returns:
            A ``DiarizationProvider`` instance, or ``None`` when diarization_source
            is ``"provider"``, ``"off"``, or ``"local"`` (the ``local`` source runs on our
            own GPU via ``rediarize_task`` -> ``ModelManager.get_diarizer()`` directly,
            never through this factory — issue #672).
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

        if source in ("provider", "off", "local"):
            return None

        if source == "pyannote":
            # Read pyannote.ai diarization config from user_diarization_settings.
            from app.models.user_diarization_settings import UserDiarizationSettings
            from app.utils.encryption import decrypt_value

            config = (
                db.query(UserDiarizationSettings)
                .filter(
                    UserDiarizationSettings.user_id == user_id,
                    UserDiarizationSettings.provider == "pyannote",
                    UserDiarizationSettings.is_active.is_(True),
                )
                .first()
            )
            if not config or not config.api_key:
                logger.warning(
                    "User %d has diarization_source=pyannote but no active pyannote config",
                    user_id,
                )
                return None

            from .pyannote_provider import PyAnnoteCloudDiarizationProvider

            provider: DiarizationProvider = PyAnnoteCloudDiarizationProvider(
                api_key=decrypt_value(config.api_key) or "",
                model_name=config.model_name or "precision-2",
            )
            return provider

        logger.warning("Unknown diarization source '%s' for user %d", source, user_id)
        return None
