"""Factory for creating ASR provider instances."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

from app.core.exceptions import ASRConfigurationError

from .base import ASRProvider
from .local_provider import LocalASRProvider

if TYPE_CHECKING:
    from app.models.user_asr_settings import UserASRSettings

logger = logging.getLogger(__name__)


def _guard_local_allowed() -> None:
    """Raise if resolving to ``LocalASRProvider`` is not viable under this deployment.

    Lite deployments (``DEPLOYMENT_MODE=lite``, set by ``docker-compose.lite.yml``) ship
    from ``requirements-lite.txt``, which has no whisperx/faster-whisper — that image is
    cloud-ASR-only by design. Without this guard, resolution silently returns
    ``LocalASRProvider()`` and the failure only surfaces much later, deep in a Celery GPU
    task, as a raw ``ModuleNotFoundError`` instead of a clear configuration error.
    """
    from app.core.config import settings

    if settings.DEPLOYMENT_MODE.lower() == "lite":
        raise ASRConfigurationError(
            "Lite deployments require a cloud ASR provider to be configured — local "
            "WhisperX transcription is not available in this image (DEPLOYMENT_MODE=lite). "
            "Configure a cloud ASR provider in Settings, or set ASR_PROVIDER to a cloud "
            "provider."
        )


# Providers that require a non-empty API key from env vars.
_KEY_REQUIRED: dict[str, str] = {
    "deepgram": "DEEPGRAM_API_KEY",
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "azure": "AZURE_SPEECH_KEY",
    "speechmatics": "SPEECHMATICS_API_KEY",
    "gladia": "GLADIA_API_KEY",
    "pyannote": "PYANNOTE_API_KEY",
}

#: ``ASR_PROVIDER`` env fallback: provider -> a reader of its model env var with the default.
#: The one table both ``_from_env`` (which builds the provider) and ``resolve_active_config``
#: (which only names it) read, so the model a plan reports is the model the job would use.
#: Each entry reads its variable by a literal name, which is what lets the env-coverage test
#: see that ``.env.example``'s ``*_MODEL`` keys are consumed.
_ENV_MODEL: dict[str, Callable[[], str]] = {
    "deepgram": lambda: os.getenv("DEEPGRAM_MODEL", "nova-3"),
    "assemblyai": lambda: os.getenv("ASSEMBLYAI_MODEL", "universal"),
    "openai": lambda: os.getenv("OPENAI_ASR_MODEL", "gpt-4o-transcribe"),
    "google": lambda: os.getenv("GOOGLE_ASR_MODEL", "chirp-3"),
    "azure": lambda: os.getenv("AZURE_ASR_MODEL", "whisper"),
    "aws": lambda: os.getenv("AWS_ASR_MODEL", "standard"),
    "speechmatics": lambda: os.getenv("SPEECHMATICS_MODEL", "standard"),
    "gladia": lambda: os.getenv("GLADIA_MODEL", "standard"),
    "pyannote": lambda: os.getenv("PYANNOTE_MODEL", "parakeet"),
}


def _env_model(provider: str) -> str:
    return _ENV_MODEL[provider]()


def _env_provider_usable(provider: str) -> bool:
    """Whether ``ASR_PROVIDER=<provider>`` builds that provider (``_from_env``'s fallback rule)."""
    if provider not in _ENV_MODEL:
        return False
    env_var = _KEY_REQUIRED.get(provider)
    return env_var is None or bool(os.getenv(env_var, "").strip())


# Two capability fields every provider entry carries (a model entry overrides where it
# differs), both filled from what the provider MODULE does, never from vendor docs:
#
# - ``speaker_count_hints``: which of min/max/num speakers the module actually sends.
#   ``tests/unit/test_asr_catalog_speaker_hints.py`` derives the set from each module's
#   source and fails on a mismatch.
# - ``timestamp_granularity``: ``word`` (per-word times), ``segment`` (segment times only)
#   or ``none`` (text with no usable times, so speakers cannot be assigned to it).
#   ``tests/unit/test_asr_catalog_timestamp_granularity.py`` feeds each parser a fixture
#   response and fails when the parsed result cannot honour the declared value.
#
# ``services/pipeline_plan.py`` reads both to decide how speaker turns reach the text.
ASR_PROVIDER_CATALOG: dict = {
    "local": {
        "id": "local",
        "display_name": "Local (GPU)",
        "requires_api_key": False,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        # Honoured by the in-process PyAnnote engine only; the diar-native engine ignores
        # them. pipeline_plan resolves the real set from engine.diarizer_backend.
        "speaker_count_hints": ["min", "max", "num"],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": True,
        "description": "Local GPU transcription via faster-whisper + PyAnnote",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Excellent — PyAnnote provides best-in-class speaker diarization",
        "models": [
            {
                "id": "large-v3-turbo",
                "display_name": "Large V3 Turbo",
                "description": "Default — 6× faster, ~6 GB VRAM",
                "price_per_min_batch": 0,
                "is_default": True,
                "supports_diarization": True,
                "supports_translation": False,
                "language_support": "english_optimized",
            },
            {
                "id": "large-v3",
                "display_name": "Large V3",
                "description": "Best accuracy, ~10 GB VRAM, supports translation",
                "price_per_min_batch": 0,
                "supports_diarization": True,
                "supports_translation": True,
                "language_support": "multilingual",
            },
            {
                "id": "large-v2",
                "display_name": "Large V2",
                "description": "Legacy, ~10 GB VRAM",
                "price_per_min_batch": 0,
                "supports_diarization": True,
                "supports_translation": True,
                "language_support": "multilingual",
            },
            {
                "id": "medium",
                "display_name": "Medium",
                "description": "~5 GB VRAM",
                "price_per_min_batch": 0,
                "supports_diarization": True,
                "supports_translation": True,
                "language_support": "multilingual",
            },
            {
                "id": "small",
                "display_name": "Small",
                "description": "~2 GB VRAM",
                "price_per_min_batch": 0,
                "supports_diarization": True,
                "supports_translation": True,
                "language_support": "multilingual",
            },
            {
                "id": "base",
                "display_name": "Base",
                "description": "~1 GB VRAM",
                "price_per_min_batch": 0,
                "supports_diarization": True,
                "supports_translation": True,
                "language_support": "multilingual",
            },
            {
                "id": "tiny",
                "display_name": "Tiny",
                "description": "Fastest, lowest accuracy",
                "price_per_min_batch": 0,
                "supports_diarization": True,
                "supports_translation": True,
                "language_support": "multilingual",
            },
            {
                # CTranslate2 build (config.json + model.bin) — the only CrisperWhisper
                # variant that loads in faster-whisper's WhisperModel. The PyTorch
                # checkpoint nyrahealth/CrisperWhisper is intentionally NOT in this
                # catalog: it ships model.safetensors (no CT2 model.bin), so CTranslate2
                # cannot load it and the whisperx transformers backend is still a stub.
                "id": "nyrahealth/faster_CrisperWhisper",
                "display_name": "CrisperWhisper (English)",
                "description": "Precise verbatim word-level timestamps, English only, ~3 GB VRAM",
                "price_per_min_batch": 0,
                "supports_diarization": True,
                "supports_translation": False,
                "language_support": "english_only",
            },
        ],
    },
    "deepgram": {
        "id": "deepgram",
        "display_name": "Deepgram",
        "requires_api_key": True,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": [],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "Nova-3 flagship model with medical terminology support",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Fair — may detect fewer speakers than local PyAnnote. Consider using local diarization for multi-speaker content.",
        "models": [
            {
                "id": "nova-3",
                "display_name": "Nova-3",
                "description": "Flagship, best accuracy, 36+ languages",
                "price_per_min_batch": 0.0043,
                "price_per_min_stream": 0.0077,
                "languages": 36,
                "is_default": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
            {
                "id": "nova-3-medical",
                "display_name": "Nova-3 Medical",
                "description": "Medical terminology optimized, English only",
                "price_per_min_batch": 0.0043,
                "price_per_min_stream": 0.0077,
                "languages": 1,
                "is_medical": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
            {
                "id": "nova-2",
                "display_name": "Nova-2",
                "description": "Legacy, stable, 36 languages",
                "price_per_min_batch": 0.0043,
                "price_per_min_stream": 0.0077,
                "languages": 36,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
        ],
    },
    "assemblyai": {
        "id": "assemblyai",
        "display_name": "AssemblyAI",
        "requires_api_key": True,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": ["min", "max", "num"],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "Best-in-class diarization with the Universal speech models",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Good — AssemblyAI offers strong diarization with speaker count support",
        # Only universal-3-pro and universal-2 are accepted by the current speech_models API;
        # slam-1 / nano were rejected live ("must be one of universal-3-pro, universal-2").
        "models": [
            {
                "id": "universal",
                "display_name": "Universal-3 Pro",
                "description": "Highest accuracy — English, Spanish, Portuguese, French, German, Italian",
                "price_per_min_batch": 0.0045,
                "languages": 6,
                "is_default": True,
                "supports_diarization": True,
            },
            {
                "id": "universal-multilingual",
                "display_name": "Universal-2 (99 languages)",
                "description": "Multilingual with automatic language detection",
                "price_per_min_batch": 0.0045,
                "languages": 99,
                "supports_diarization": True,
            },
        ],
    },
    "openai": {
        "id": "openai",
        "display_name": "OpenAI",
        "requires_api_key": True,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": False,
        "speaker_count_hints": [],
        # gpt-4o-transcribe / gpt-4o-mini-transcribe (and any model not listed) are asked
        # for "json", which carries no times; the parser makes one 0.0/0.0 segment.
        "timestamp_granularity": "none",
        "supports_vocabulary": False,
        "supports_translation": True,
        "description": "Whisper-1 and GPT-4o Transcribe (25 MB file limit)",
        "status": "experimental",
        "status_note": "This provider has not been fully tested with real API keys. Results may vary.",
        "diarization_quality": "",
        "models": [
            {
                "id": "gpt-4o-transcribe",
                "display_name": "GPT-4o Transcribe",
                "description": "Higher accuracy than Whisper-1",
                "price_per_min_batch": 0.006,
                "is_default": True,
                "max_file_size_mb": 25,
                "supports_confidence": True,
                "supports_translation": False,
            },
            {
                "id": "gpt-4o-transcribe-diarize",
                "display_name": "GPT-4o Transcribe + Diarize",
                "description": "Built-in speaker labels",
                "price_per_min_batch": 0.006,
                "max_file_size_mb": 25,
                "supports_diarization": True,
                "supports_translation": False,
                "timestamp_granularity": "segment",
            },
            {
                "id": "whisper-1",
                "display_name": "Whisper-1",
                "description": "99 languages, supports translation to English",
                "price_per_min_batch": 0.006,
                "languages": 99,
                "max_file_size_mb": 25,
                "supports_translation": True,
                # verbose_json without timestamp_granularities: segment times only.
                "timestamp_granularity": "segment",
            },
        ],
    },
    "google": {
        "id": "google",
        "display_name": "Google Cloud Speech",
        "requires_api_key": False,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": ["min", "max"],
        "timestamp_granularity": "word",
        "supports_vocabulary": False,
        "supports_translation": False,
        "description": "Chirp 3 model with 100+ language support (uses service account credentials)",
        "status": "experimental",
        "status_note": "This provider has not been fully tested with real API keys. Results may vary.",
        "diarization_quality": "Unknown — supports min/max speaker count parameters",
        "models": [
            {
                "id": "chirp-3",
                "display_name": "Chirp 3",
                "description": "Best multilingual, 100+ languages",
                "price_per_min_batch": 0.024,
                "languages": 100,
                "is_default": True,
                "supports_diarization": True,
            },
            {
                "id": "long",
                "display_name": "Long Audio",
                "description": "Optimised for long recordings",
                "price_per_min_batch": 0.024,
                "languages": 100,
                "supports_diarization": True,
            },
            {
                "id": "short",
                "display_name": "Short Audio",
                "description": "Optimised for short clips",
                "price_per_min_batch": 0.024,
                "languages": 100,
                "supports_diarization": True,
            },
        ],
    },
    "azure": {
        "id": "azure",
        "display_name": "Azure Speech",
        "requires_api_key": True,
        "requires_region": True,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": [],
        # Word times are parsed only when the SDK fills NBest[0].Words, which is never
        # explicitly requested; treated as segment until a live response proves otherwise.
        "timestamp_granularity": "segment",
        "supports_vocabulary": False,
        "supports_translation": False,
        "description": "Azure Speech Services — Whisper and Standard models",
        "status": "experimental",
        "status_note": "This provider has not been fully tested with real API keys. Results may vary.",
        "diarization_quality": "Unknown — uses ConversationTranscriber for diarization",
        "models": [
            {
                "id": "whisper",
                "display_name": "Azure Whisper",
                "description": "Whisper via Azure, 99 languages",
                "price_per_min_batch": 0.017,
                "languages": 99,
                "is_default": True,
                "supports_diarization": True,
            },
            {
                "id": "standard",
                "display_name": "Standard",
                "description": "Azure's own model, 100+ languages",
                "price_per_min_batch": 0.017,
                "languages": 100,
                "supports_diarization": True,
            },
        ],
    },
    "aws": {
        "id": "aws",
        "display_name": "Amazon Transcribe",
        "requires_api_key": True,  # the AWS Secret Access Key (stored in api_key, encrypted)
        "requires_access_key_id": True,  # the AWS Access Key ID (encrypted, AWS-only)
        "requires_region": True,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": ["max"],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "Standard and Medical transcription (HIPAA-eligible)",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Supports up to 30 speakers",
        "models": [
            {
                "id": "standard",
                "display_name": "Standard (single language)",
                "description": "One language per file — uses your source language, or "
                "auto-detects the dominant language when set to Auto",
                "price_per_min_batch": 0.024,
                "languages": 100,
                "is_default": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
            {
                "id": "multilingual",
                "display_name": "Multilingual (code-switching)",
                "description": "Multiple languages within one file — detects and transcribes "
                "each language (e.g. a bilingual speaker switching mid-conversation)",
                "price_per_min_batch": 0.024,
                "languages": 100,
                "supports_diarization": True,
                "supports_vocabulary": False,
            },
            {
                "id": "medical",
                "display_name": "Medical",
                "description": "HIPAA-eligible, medical vocabulary",
                "price_per_min_batch": 0.075,
                "is_medical": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
        ],
    },
    "speechmatics": {
        "id": "speechmatics",
        "display_name": "Speechmatics",
        "requires_api_key": True,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": [],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "Enterprise-grade with 55+ languages and speaker diarization",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Strong — enterprise speaker diarization",
        "models": [
            {
                "id": "standard",
                "display_name": "Standard",
                "description": "55+ languages, includes diarization",
                "price_per_min_batch": 0.004,
                "languages": 55,
                "is_default": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
        ],
    },
    "gladia": {
        "id": "gladia",
        "display_name": "Gladia",
        "requires_api_key": True,
        "requires_region": False,
        # True since issue #594 wired UserASRSettings.base_url into the real request
        # path (GladiaProvider._resolve_base_url, guarded by
        # ASR_ALLOW_PRIVATE_ENDPOINTS). Every other provider below still drops a
        # configured base_url silently, so their entries stay False — flipping one
        # without wiring the field would resurface #594 under a different vendor.
        "supports_custom_url": True,
        "supports_diarization": True,
        "speaker_count_hints": [],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "All features bundled, 100+ languages",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Good — diarization included in all plans",
        "models": [
            {
                "id": "standard",
                "display_name": "Standard",
                "description": "All features included, 100+ languages",
                "price_per_min_batch": 0.010,
                "languages": 100,
                "is_default": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
        ],
    },
    "pyannote": {
        "id": "pyannote",
        "display_name": "pyannote.ai",
        "requires_api_key": True,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": ["min", "max", "num"],
        "timestamp_granularity": "word",
        "supports_vocabulary": False,
        "supports_translation": False,
        "description": "STT Orchestration — premium diarization + transcription in one API call",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Excellent — premium cloud diarization from pyannote.ai",
        "models": [
            {
                "id": "parakeet",
                "display_name": "Parakeet (NVIDIA)",
                "description": "Fast, accurate, 100 languages, bundled with premium diarization",
                "price_per_min_batch": 0.027,
                "languages": 100,
                "is_default": True,
                "supports_diarization": True,
            },
            {
                "id": "whisper-large-v3-turbo",
                "display_name": "Whisper Large V3 Turbo",
                "description": "OpenAI Whisper via pyannote, bundled with premium diarization",
                "price_per_min_batch": 0.027,
                "languages": 100,
                "supports_diarization": True,
            },
        ],
    },
}


def _capabilities(provider_entry: dict, model_entry: dict | None) -> dict:
    """One catalog entry's capabilities; a model entry's value wins over its provider's."""
    model = model_entry or {}

    def pick(key: str, default):
        return model.get(key, provider_entry.get(key, default))

    return {
        "supports_translation": pick("supports_translation", True),
        "language_support": model.get("language_support", "multilingual"),
        "languages": model.get("languages"),
        "supports_diarization": bool(pick("supports_diarization", False)),
        "speaker_count_hints": list(pick("speaker_count_hints", [])),
        "timestamp_granularity": pick("timestamp_granularity", "none"),
    }


class ASRProviderFactory:
    @staticmethod
    def create_from_db_config(cfg) -> ASRProvider:
        """Build a provider from a stored ``UserASRSettings`` row, decrypting credentials.

        The single place that turns a saved config into a live provider. Both the
        transcription job path (:meth:`create_for_user`) and the saved-config
        "Test connection" endpoint go through here so the two cannot drift — an
        earlier split let the endpoint forward ``access_key_id`` while the job path
        silently dropped it, so AWS jobs ran under whatever ambient credentials
        boto3 resolved (issue #300).

        Args:
            cfg: A ``UserASRSettings`` row.

        Returns:
            The configured ASR provider.

        Raises:
            ValueError: If a stored credential is present but fails to decrypt.
        """
        from app.utils.encryption import decrypt_api_key

        if cfg.provider == "local":
            # Explicit "local" in DB config — honour it without trying to decrypt
            # a (nonexistent) API key.
            _guard_local_allowed()
            return LocalASRProvider()

        api_key: str | None = None
        if cfg.api_key:
            api_key = decrypt_api_key(str(cfg.api_key))
            if not api_key:
                raise ValueError(
                    f"Decryption of stored API key returned empty for ASR config id={cfg.id} "
                    f"(provider={cfg.provider}); the stored key is corrupt or ENCRYPTION_KEY changed"
                )

        access_key_id: str | None = None
        if getattr(cfg, "access_key_id", None):
            access_key_id = decrypt_api_key(str(cfg.access_key_id))
            if not access_key_id:
                raise ValueError(
                    f"Decryption of stored access key ID returned empty for ASR config "
                    f"id={cfg.id} (provider={cfg.provider})"
                )

        return ASRProviderFactory.create_from_config(
            provider=cfg.provider,
            api_key=api_key,
            model=cfg.model_name,
            base_url=cfg.base_url,
            region=cfg.region,
            access_key_id=access_key_id,
        )

    @staticmethod
    def _active_config_row(user_id: int, db) -> UserASRSettings | None:
        """The user's active ``UserASRSettings`` row (own or shared), or ``None``.

        ``None`` covers no pointer, a dangling or no-longer-shared pointer, and a row that
        cannot be read; each falls back to env/local in every caller.
        """
        from app import models

        setting = (
            db.query(models.UserSetting)
            .filter(
                models.UserSetting.user_id == user_id,
                models.UserSetting.setting_key == "active_asr_config_id",
            )
            .first()
        )
        if not (setting and setting.setting_value):
            return None
        try:
            from app.models.user_asr_settings import UserASRSettings
            from app.utils.tenant_sharing import shared_usable_by

            cfg: UserASRSettings | None = (
                db.query(UserASRSettings)
                .filter(
                    UserASRSettings.id == int(setting.setting_value),
                    # Own, or shared within a tenant both belong to: the stored
                    # pointer outlives membership changes, so it is re-checked here.
                    shared_usable_by(UserASRSettings.user_id, UserASRSettings.is_shared, user_id),
                )
                .first()
            )
        except Exception as exc:
            logger.warning(
                "Failed to load ASR config for user %d: %s — falling back to env/local",
                user_id,
                exc,
            )
            return None
        if cfg is None:
            logger.warning(
                "ASR config id=%s for user %d not found or not accessible — "
                "falling back to env/local",
                setting.setting_value,
                user_id,
            )
        return cfg

    @staticmethod
    def resolve_active_config(user_id: int, db) -> tuple[str, str, UserASRSettings | None]:
        """``(provider, model, cfg)`` the user's transcription would run with.

        The "which config row" half of :meth:`create_for_user`, without building a
        provider or decrypting anything: the active DB config (own or shared), else a
        usable ``ASR_PROVIDER`` env provider, else local with ``WHISPER_MODEL``. ``cfg``
        is the ``UserASRSettings`` row, or ``None`` for the env and local fallbacks.
        """
        return ASRProviderFactory.resolve_config(ASRProviderFactory._active_config_row(user_id, db))

    @staticmethod
    def resolve_config(
        cfg: UserASRSettings | None,
    ) -> tuple[str, str, UserASRSettings | None]:
        """``(provider, model, cfg)`` for ``cfg`` as the active row (``None``: no row).

        Lets a caller plan a switch before making it (``services/asr_switch_plan``) with
        the same env / local fallback a job uses.
        """
        if cfg is not None:
            return str(cfg.provider or "local"), str(cfg.model_name or ""), cfg
        env_provider = os.getenv("ASR_PROVIDER", "local").lower()
        if env_provider != "local" and _env_provider_usable(env_provider):
            return env_provider, _env_model(env_provider), None
        return "local", os.getenv("WHISPER_MODEL", "large-v3-turbo"), None

    @staticmethod
    def create_for_user(user_id: int, db) -> ASRProvider:
        """Load active ASR config from DB → env vars → default local.

        Selection priority (highest to lowest):
        1. User's active DB config (UserASRSettings referenced by active_asr_config_id setting)
        2. ASR_PROVIDER env var (if set to a non-local provider)
        3. LocalASRProvider (default fallback)
        """
        cfg = ASRProviderFactory._active_config_row(user_id, db)
        if cfg is not None:
            logger.info(
                "ASR provider for user %d: %s (DB config id=%s model=%s%s)",
                user_id,
                cfg.provider,
                cfg.id,
                cfg.model_name,
                "" if cfg.api_key else ", no API key",
            )
            try:
                return ASRProviderFactory.create_from_db_config(cfg)
            except ASRConfigurationError:
                # Not a config-loading failure — a deliberate refusal to silently fall
                # back to a local provider this deployment cannot run. Let it propagate.
                raise
            except Exception as exc:
                logger.warning(
                    "Failed to load ASR config for user %d: %s — falling back to env/local",
                    user_id,
                    exc,
                )

        env_provider = os.getenv("ASR_PROVIDER", "local").lower()
        if env_provider and env_provider != "local":
            logger.info(
                "ASR provider for user %d: %s (env var ASR_PROVIDER)", user_id, env_provider
            )
            return ASRProviderFactory._from_env(env_provider)

        logger.debug("ASR provider for user %d: local (default)", user_id)
        _guard_local_allowed()
        return LocalASRProvider()

    @staticmethod
    def _from_env(provider: str) -> ASRProvider:  # noqa: C901
        """Instantiate a provider from environment variables.

        For providers that require an API key, falls back to LocalASRProvider with a
        warning when the corresponding env var is empty.  This prevents silent failures
        where a mis-configured ASR_PROVIDER results in API calls with a blank key.
        """
        if provider in _KEY_REQUIRED:
            env_var = _KEY_REQUIRED[provider]
            api_key = os.getenv(env_var, "").strip()
            if not api_key:
                logger.warning(
                    "ASR_PROVIDER=%s but %s is empty — falling back to local provider. "
                    "Set %s or configure a DB ASR config to use %s.",
                    provider,
                    env_var,
                    env_var,
                    provider,
                )
                _guard_local_allowed()
                return LocalASRProvider()

        if provider == "deepgram":
            from .deepgram_provider import DeepgramProvider

            return DeepgramProvider(os.getenv("DEEPGRAM_API_KEY", ""), _env_model(provider))
        if provider == "assemblyai":
            from .assemblyai_provider import AssemblyAIProvider

            return AssemblyAIProvider(os.getenv("ASSEMBLYAI_API_KEY", ""), _env_model(provider))
        if provider == "openai":
            from .openai_provider import OpenAIASRProvider

            return OpenAIASRProvider(os.getenv("OPENAI_API_KEY", ""), _env_model(provider))
        if provider == "google":
            from .google_provider import GoogleASRProvider

            return GoogleASRProvider(
                model_name=_env_model(provider),
                credentials_file=os.getenv("GOOGLE_CLOUD_CREDENTIALS"),
            )
        if provider == "azure":
            from .azure_provider import AzureASRProvider

            return AzureASRProvider(
                os.getenv("AZURE_SPEECH_KEY", ""),
                os.getenv("AZURE_SPEECH_REGION", "eastus"),
                _env_model(provider),
            )
        if provider == "aws":
            from .aws_provider import AWSTranscribeProvider

            return AWSTranscribeProvider(
                region=os.getenv("AWS_REGION", "us-east-1"),
                model_name=_env_model(provider),
                access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            )
        if provider == "speechmatics":
            from .speechmatics_provider import SpeechmaticsProvider

            return SpeechmaticsProvider(os.getenv("SPEECHMATICS_API_KEY", ""), _env_model(provider))
        if provider == "gladia":
            from .gladia_provider import GladiaProvider

            return GladiaProvider(os.getenv("GLADIA_API_KEY", ""), _env_model(provider))
        if provider == "pyannote":
            from .pyannote_provider import PyAnnoteProvider

            return PyAnnoteProvider(os.getenv("PYANNOTE_API_KEY", ""), _env_model(provider))
        logger.warning("Unknown ASR provider '%s', falling back to local", provider)
        _guard_local_allowed()
        return LocalASRProvider()

    @staticmethod
    def create_from_config(
        provider: str,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        region: str | None = None,
        access_key_id: str | None = None,
    ) -> ASRProvider:
        """Create provider for connection testing (no DB lookup).

        ``access_key_id`` is AWS-only (the Secret Access Key arrives via ``api_key``).
        """
        if provider == "local":
            _guard_local_allowed()
            return LocalASRProvider()
        if provider == "deepgram":
            from .deepgram_provider import DeepgramProvider

            return DeepgramProvider(api_key or "", model or "nova-3")
        if provider == "assemblyai":
            from .assemblyai_provider import AssemblyAIProvider

            return AssemblyAIProvider(api_key or "", model or "universal")
        if provider == "openai":
            from .openai_provider import OpenAIASRProvider

            return OpenAIASRProvider(api_key or "", model or "gpt-4o-transcribe")
        if provider == "google":
            from .google_provider import GoogleASRProvider

            return GoogleASRProvider(api_key=api_key, model_name=model or "chirp-3")
        if provider == "azure":
            from .azure_provider import AzureASRProvider

            return AzureASRProvider(api_key or "", region or "eastus", model or "whisper")
        if provider == "aws":
            import os

            from .aws_provider import AWSTranscribeProvider

            # UI creds take precedence; otherwise fall back to boto3's default chain
            # (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY env vars, or an IAM role on AWS).
            return AWSTranscribeProvider(
                region=region or os.getenv("AWS_REGION") or "us-east-1",
                model_name=model or "standard",
                access_key_id=access_key_id or os.getenv("AWS_ACCESS_KEY_ID"),
                secret_access_key=api_key or os.getenv("AWS_SECRET_ACCESS_KEY"),
            )
        if provider == "speechmatics":
            from .speechmatics_provider import SpeechmaticsProvider

            return SpeechmaticsProvider(api_key or "", model or "standard")
        if provider == "gladia":
            from .gladia_provider import GladiaProvider

            # base_url (issue #594): GladiaProvider._resolve_base_url applies the
            # ASR_ALLOW_PRIVATE_ENDPOINTS SSRF guard, so passing it through here is safe
            # even though it is user-supplied — the same guard the LLM path applies.
            return GladiaProvider(api_key or "", model or "standard", base_url=base_url)
        if provider == "pyannote":
            from .pyannote_provider import PyAnnoteProvider

            return PyAnnoteProvider(api_key or "", model or "parakeet")
        logger.warning("Unknown provider '%s', falling back to local", provider)
        _guard_local_allowed()
        return LocalASRProvider()

    @staticmethod
    def get_model_capabilities(provider_id: str, model_id: str) -> dict:
        """Look up capabilities for a specific provider+model from the catalog.

        Model-level values win over provider-level ones (OpenAI: the provider does not
        diarize, ``gpt-4o-transcribe-diarize`` does). For unknown local models: substring
        match against known IDs (most-specific first: "large-v3-turbo" before "large-v3").
        An unknown provider gets a permissive language default and, fail closed, no
        speakers and no timing.
        """
        provider_entry = ASR_PROVIDER_CATALOG.get(provider_id)
        if not provider_entry:
            return {
                "supports_translation": True,
                "language_support": "multilingual",
                "languages": None,
                "supports_diarization": False,
                "speaker_count_hints": [],
                "timestamp_granularity": "none",
            }

        models = provider_entry.get("models", [])
        for m in models:
            if m["id"] == model_id:
                return _capabilities(provider_entry, m)

        # For local provider: substring match (most-specific first)
        if provider_id == "local":
            # English-only Whisper ".en" variants (tiny.en, base.en, small.en, medium.en,
            # distil-*.en) are not enumerated in the catalog but are English-only and cannot
            # translate. Checked BEFORE the substring loop so "tiny.en" is not mis-matched to
            # the multilingual "tiny" entry. Classified consistently with the catalog's
            # english_only entries so the same UI guards and transcriber safety net apply.
            if model_id.endswith(".en"):
                return {
                    **_capabilities(provider_entry, None),
                    "supports_translation": False,
                    "language_support": "english_only",
                    "languages": 1,
                }

            sorted_models = sorted(models, key=lambda m: len(m["id"]), reverse=True)
            for m in sorted_models:
                if m["id"] in model_id:
                    return _capabilities(provider_entry, m)

        return _capabilities(provider_entry, None)

    @staticmethod
    def get_active_model_capabilities(user_id: int, db) -> dict:
        """Get capabilities for user's active ASR model.

        Resolution is :meth:`resolve_active_config`: user DB ASR config -> a usable
        ASR_PROVIDER env provider -> local with WHISPER_MODEL.
        """
        provider_id, model_id, _cfg = ASRProviderFactory.resolve_active_config(user_id, db)
        return {
            "provider": provider_id,
            "model_id": model_id,
            **ASRProviderFactory.get_model_capabilities(provider_id, model_id),
        }

    @staticmethod
    def get_provider_catalog() -> dict:
        return ASR_PROVIDER_CATALOG
