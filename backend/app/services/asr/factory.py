"""Factory for creating ASR provider instances."""

from __future__ import annotations

import logging
import os

from .base import ASRProvider
from .local_provider import LocalASRProvider

logger = logging.getLogger(__name__)

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

ASR_PROVIDER_CATALOG: dict = {
    "local": {
        "id": "local",
        "display_name": "Local (GPU)",
        "requires_api_key": False,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
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
            },
            {
                "id": "whisper-1",
                "display_name": "Whisper-1",
                "description": "99 languages, supports translation to English",
                "price_per_min_batch": 0.006,
                "languages": 99,
                "max_file_size_mb": 25,
                "supports_translation": True,
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
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "Standard and Medical transcription (HIPAA-eligible)",
        "status": "experimental",
        "status_note": "Configure an IAM user's Access Key ID + Secret Access Key with "
        "Amazon Transcribe + S3 permissions, plus a region.",
        "diarization_quality": "Up to 10 speakers (MaxSpeakerLabels)",
        "models": [
            {
                "id": "standard",
                "display_name": "Standard",
                "description": "General purpose",
                "price_per_min_batch": 0.024,
                "is_default": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
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
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "Enterprise-grade with 55+ languages and 3 diarization modes",
        "status": "experimental",
        "status_note": "This provider has not been fully tested with real API keys. Results may vary.",
        "diarization_quality": "Unknown — offers 3 diarization modes",
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
        "supports_custom_url": False,
        "supports_diarization": True,
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


class ASRProviderFactory:
    @staticmethod
    def create_for_user(user_id: int, db) -> ASRProvider:
        """Load active ASR config from DB → env vars → default local.

        Selection priority (highest to lowest):
        1. User's active DB config (UserASRSettings referenced by active_asr_config_id setting)
        2. ASR_PROVIDER env var (if set to a non-local provider)
        3. LocalASRProvider (default fallback)
        """
        from app import models
        from app.utils.encryption import decrypt_api_key

        setting = (
            db.query(models.UserSetting)
            .filter(
                models.UserSetting.user_id == user_id,
                models.UserSetting.setting_key == "active_asr_config_id",
            )
            .first()
        )
        if setting and setting.setting_value:
            try:
                from sqlalchemy import or_

                from app.models.user_asr_settings import UserASRSettings

                cfg = (
                    db.query(UserASRSettings)
                    .filter(
                        UserASRSettings.id == int(setting.setting_value),
                        or_(
                            UserASRSettings.user_id == user_id,
                            UserASRSettings.is_shared == True,  # noqa: E712
                        ),
                    )
                    .first()
                )
                if cfg:
                    # Explicit "local" provider in DB config — honour it without trying
                    # to decrypt a (nonexistent) API key.
                    if cfg.provider == "local":
                        logger.info(
                            "ASR provider for user %d: local (explicit DB config id=%s)",
                            user_id,
                            cfg.id,
                        )
                        return LocalASRProvider()

                    api_key: str | None = None
                    if cfg.api_key:
                        api_key = decrypt_api_key(cfg.api_key)
                        if not api_key:
                            # Decryption returned empty — the stored key is corrupt or the
                            # encryption secret changed.  Log and fall through to env/local.
                            logger.error(
                                "Decryption of stored API key returned empty for ASR config id=%s "
                                "(user %d, provider=%s). Falling back to env/local provider.",
                                cfg.id,
                                user_id,
                                cfg.provider,
                            )
                            # Fall through intentionally
                        else:
                            logger.info(
                                "ASR provider for user %d: %s (DB config id=%s model=%s)",
                                user_id,
                                cfg.provider,
                                cfg.id,
                                cfg.model_name,
                            )
                            return ASRProviderFactory.create_from_config(
                                provider=cfg.provider,
                                api_key=api_key,
                                model=cfg.model_name,
                                base_url=cfg.base_url,
                                region=cfg.region,
                            )
                    else:
                        # No API key stored — valid for providers like Google (service account)
                        # or AWS (IAM role).
                        logger.info(
                            "ASR provider for user %d: %s (DB config id=%s, no API key)",
                            user_id,
                            cfg.provider,
                            cfg.id,
                        )
                        return ASRProviderFactory.create_from_config(
                            provider=cfg.provider,
                            api_key=None,
                            model=cfg.model_name,
                            base_url=cfg.base_url,
                            region=cfg.region,
                        )
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
                return LocalASRProvider()

        if provider == "deepgram":
            from .deepgram_provider import DeepgramProvider

            return DeepgramProvider(
                os.getenv("DEEPGRAM_API_KEY", ""), os.getenv("DEEPGRAM_MODEL", "nova-3")
            )
        if provider == "assemblyai":
            from .assemblyai_provider import AssemblyAIProvider

            return AssemblyAIProvider(
                os.getenv("ASSEMBLYAI_API_KEY", ""), os.getenv("ASSEMBLYAI_MODEL", "universal")
            )
        if provider == "openai":
            from .openai_provider import OpenAIASRProvider

            return OpenAIASRProvider(
                os.getenv("OPENAI_API_KEY", ""), os.getenv("OPENAI_ASR_MODEL", "gpt-4o-transcribe")
            )
        if provider == "google":
            from .google_provider import GoogleASRProvider

            return GoogleASRProvider(
                model_name=os.getenv("GOOGLE_ASR_MODEL", "chirp-3"),
                credentials_file=os.getenv("GOOGLE_CLOUD_CREDENTIALS"),
            )
        if provider == "azure":
            from .azure_provider import AzureASRProvider

            return AzureASRProvider(
                os.getenv("AZURE_SPEECH_KEY", ""),
                os.getenv("AZURE_SPEECH_REGION", "eastus"),
                os.getenv("AZURE_ASR_MODEL", "whisper"),
            )
        if provider == "aws":
            from .aws_provider import AWSTranscribeProvider

            return AWSTranscribeProvider(
                region=os.getenv("AWS_REGION", "us-east-1"),
                model_name=os.getenv("AWS_ASR_MODEL", "standard"),
                access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            )
        if provider == "speechmatics":
            from .speechmatics_provider import SpeechmaticsProvider

            return SpeechmaticsProvider(
                os.getenv("SPEECHMATICS_API_KEY", ""), os.getenv("SPEECHMATICS_MODEL", "standard")
            )
        if provider == "gladia":
            from .gladia_provider import GladiaProvider

            return GladiaProvider(
                os.getenv("GLADIA_API_KEY", ""), os.getenv("GLADIA_MODEL", "standard")
            )
        if provider == "pyannote":
            from .pyannote_provider import PyAnnoteProvider

            return PyAnnoteProvider(
                os.getenv("PYANNOTE_API_KEY", ""), os.getenv("PYANNOTE_MODEL", "parakeet")
            )
        logger.warning("Unknown ASR provider '%s', falling back to local", provider)
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

            return GladiaProvider(api_key or "", model or "standard")
        if provider == "pyannote":
            from .pyannote_provider import PyAnnoteProvider

            return PyAnnoteProvider(api_key or "", model or "parakeet")
        logger.warning("Unknown provider '%s', falling back to local", provider)
        return LocalASRProvider()

    @staticmethod
    def get_model_capabilities(provider_id: str, model_id: str) -> dict:
        """Look up capabilities for a specific provider+model from the catalog.

        For unknown local models: substring match against known IDs
        (most-specific first: "large-v3-turbo" before "large-v3").
        No match -> permissive default (supports_translation=True, multilingual).
        """
        provider_entry = ASR_PROVIDER_CATALOG.get(provider_id)
        if not provider_entry:
            return {
                "supports_translation": True,
                "language_support": "multilingual",
                "languages": None,
            }

        # Check model-level capabilities first
        models = provider_entry.get("models", [])
        for m in models:
            if m["id"] == model_id:
                return {
                    "supports_translation": m.get(
                        "supports_translation",
                        provider_entry.get("supports_translation", True),
                    ),
                    "language_support": m.get("language_support", "multilingual"),
                    "languages": m.get("languages"),
                }

        # For local provider: substring match (most-specific first)
        if provider_id == "local":
            # English-only Whisper ".en" variants (tiny.en, base.en, small.en, medium.en,
            # distil-*.en) are not enumerated in the catalog but are English-only and cannot
            # translate. Checked BEFORE the substring loop so "tiny.en" is not mis-matched to
            # the multilingual "tiny" entry. Classified consistently with the catalog's
            # english_only entries so the same UI guards and transcriber safety net apply.
            if model_id.endswith(".en"):
                return {
                    "supports_translation": False,
                    "language_support": "english_only",
                    "languages": 1,
                }

            sorted_models = sorted(models, key=lambda m: len(m["id"]), reverse=True)
            for m in sorted_models:
                if m["id"] in model_id:
                    return {
                        "supports_translation": m.get(
                            "supports_translation",
                            provider_entry.get("supports_translation", True),
                        ),
                        "language_support": m.get("language_support", "multilingual"),
                        "languages": m.get("languages"),
                    }

        # Fallback to provider-level capabilities
        return {
            "supports_translation": provider_entry.get("supports_translation", True),
            "language_support": "multilingual",
            "languages": None,
        }

    @staticmethod
    def get_active_model_capabilities(user_id: int, db) -> dict:
        """Get capabilities for user's active ASR model.

        Resolution: user DB ASR config -> ASR_PROVIDER env -> WHISPER_MODEL env -> local default.
        """
        from app import models as app_models

        # 1. Check user's active DB config
        setting = (
            db.query(app_models.UserSetting)
            .filter(
                app_models.UserSetting.user_id == user_id,
                app_models.UserSetting.setting_key == "active_asr_config_id",
            )
            .first()
        )
        if setting and setting.setting_value:
            try:
                from sqlalchemy import or_

                from app.models.user_asr_settings import UserASRSettings

                cfg = (
                    db.query(UserASRSettings)
                    .filter(
                        UserASRSettings.id == int(setting.setting_value),
                        or_(
                            UserASRSettings.user_id == user_id,
                            UserASRSettings.is_shared == True,  # noqa: E712
                        ),
                    )
                    .first()
                )
                if cfg:
                    provider_id = cfg.provider or "local"
                    model_id = cfg.model_name or ""
                    caps = ASRProviderFactory.get_model_capabilities(provider_id, model_id)
                    return {
                        "provider": provider_id,
                        "model_id": model_id,
                        **caps,
                    }
            except Exception:
                logger.debug("Failed to resolve active ASR config for user %d", user_id)

        # 2. ASR_PROVIDER env var
        env_provider = os.getenv("ASR_PROVIDER", "local").lower()
        if env_provider != "local":
            return {
                "provider": env_provider,
                "model_id": "",
                **ASRProviderFactory.get_model_capabilities(env_provider, ""),
            }

        # 3. Local default with WHISPER_MODEL
        whisper_model = os.getenv("WHISPER_MODEL", "large-v3-turbo")
        caps = ASRProviderFactory.get_model_capabilities("local", whisper_model)
        return {
            "provider": "local",
            "model_id": whisper_model,
            **caps,
        }

    @staticmethod
    def get_provider_catalog() -> dict:
        return ASR_PROVIDER_CATALOG
