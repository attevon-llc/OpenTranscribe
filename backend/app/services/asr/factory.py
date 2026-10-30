"""Factory for creating ASR provider instances."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.exceptions import ASRConfigurationError

from .base import ASRProvider
from .catalog import ASR_PROVIDER_CATALOG
from .catalog import _capabilities
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
#: Each entry reads its ``Settings`` field by a literal name, which is what lets the env-coverage
#: test see that ``.env.example``'s ``*_MODEL`` keys are consumed.
_ENV_MODEL: dict[str, Callable[[], str]] = {
    "deepgram": lambda: settings.DEEPGRAM_MODEL,
    "assemblyai": lambda: settings.ASSEMBLYAI_MODEL,
    "openai": lambda: settings.OPENAI_ASR_MODEL,
    "google": lambda: settings.GOOGLE_ASR_MODEL,
    "azure": lambda: settings.AZURE_ASR_MODEL,
    "aws": lambda: settings.AWS_ASR_MODEL,
    "speechmatics": lambda: settings.SPEECHMATICS_MODEL,
    "gladia": lambda: settings.GLADIA_MODEL,
    "pyannote": lambda: settings.PYANNOTE_MODEL,
}


def _env_model(provider: str) -> str:
    return _ENV_MODEL[provider]()


def _env_key(provider: str) -> str:
    """The stripped ``Settings`` API key for ``provider`` (``_KEY_REQUIRED`` names the field)."""
    return str(getattr(settings, _KEY_REQUIRED[provider]) or "").strip()


def _env_provider_usable(provider: str) -> bool:
    """Whether ``ASR_PROVIDER=<provider>`` builds that provider (``_from_env``'s fallback rule)."""
    if provider not in _ENV_MODEL:
        return False
    return provider not in _KEY_REQUIRED or bool(_env_key(provider))


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

        ``None`` covers no pointer, a dangling or no-longer-shared pointer, a row that
        cannot be read, and a row its owner marked inactive (``is_active``); each falls back
        to env/local in every caller.
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
            return None
        if not cfg.is_active:
            # The owner switched this config off (``UserASRSettings.is_active``). The pointer
            # is kept, so switching it back on resumes it; until then it must not run.
            logger.warning(
                "ASR config id=%s for user %d is inactive — falling back to env/local",
                setting.setting_value,
                user_id,
            )
            return None
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
        env_provider = settings.ASR_PROVIDER.lower()
        if env_provider != "local" and _env_provider_usable(env_provider):
            return env_provider, _env_model(env_provider), None
        return "local", settings.WHISPER_MODEL, None

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

        env_provider = settings.ASR_PROVIDER.lower()
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
            api_key = _env_key(provider)
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

            return DeepgramProvider(settings.DEEPGRAM_API_KEY, _env_model(provider))
        if provider == "assemblyai":
            from .assemblyai_provider import AssemblyAIProvider

            return AssemblyAIProvider(settings.ASSEMBLYAI_API_KEY, _env_model(provider))
        if provider == "openai":
            from .openai_provider import OpenAIASRProvider

            return OpenAIASRProvider(settings.OPENAI_API_KEY, _env_model(provider))
        if provider == "google":
            from .google_provider import GoogleASRProvider

            return GoogleASRProvider(
                model_name=_env_model(provider),
                credentials_file=settings.GOOGLE_CLOUD_CREDENTIALS or None,
            )
        if provider == "azure":
            from .azure_provider import AzureASRProvider

            return AzureASRProvider(
                settings.AZURE_SPEECH_KEY,
                settings.AZURE_SPEECH_REGION,
                _env_model(provider),
            )
        if provider == "aws":
            from .aws_provider import AWSTranscribeProvider

            return AWSTranscribeProvider(
                region=settings.AWS_REGION,
                model_name=_env_model(provider),
                access_key_id=settings.AWS_ACCESS_KEY_ID or None,
                secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
            )
        if provider == "speechmatics":
            from .speechmatics_provider import SpeechmaticsProvider

            return SpeechmaticsProvider(settings.SPEECHMATICS_API_KEY, _env_model(provider))
        if provider == "gladia":
            from .gladia_provider import GladiaProvider

            return GladiaProvider(settings.GLADIA_API_KEY, _env_model(provider))
        if provider == "pyannote":
            from .pyannote_provider import PyAnnoteProvider

            return PyAnnoteProvider(settings.PYANNOTE_API_KEY, _env_model(provider))
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
            from .aws_provider import AWSTranscribeProvider

            # UI creds take precedence; otherwise fall back to boto3's default chain
            # (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY env vars, or an IAM role on AWS).
            return AWSTranscribeProvider(
                region=region or settings.AWS_REGION,
                model_name=model or "standard",
                access_key_id=access_key_id or settings.AWS_ACCESS_KEY_ID or None,
                secret_access_key=api_key or settings.AWS_SECRET_ACCESS_KEY or None,
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
