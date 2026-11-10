"""Construction of :class:`LLMService` from user, system and per-conversation settings.

Split out of ``llm_service`` (v0.7.0 L07) as one concern: every ``create_from_*`` factory,
the user/system configuration resolution and the stored-credential decryption. The mixin
refers to ``LLMService`` and its error types through function-local imports because
``llm_service`` imports this module; going through ``LLMService`` (not this class) also keeps
a patch of any ``LLMService`` attribute effective for these call paths.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.services.bedrock_settings import BedrockConnection
from app.services.bedrock_settings import connection_from_encrypted
from app.services.bedrock_settings import resolve_region
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider
from app.utils.encryption import SecretDecryptionError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.user_llm_settings import UserLLMSettings
    from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResolvedUserLLM:
    """A user's active LLM configuration row, validated but not yet built into a client."""

    settings: "UserLLMSettings"
    provider: LLMProvider
    api_key: str | None
    base_url: str | None
    bedrock: BedrockConnection | None = None


class LLMFactoryMixin:
    """Factories for ``LLMService`` (mixed into it; every method is static)."""

    @staticmethod
    def create_from_settings(
        user_id: int | None = None, *, raise_on_unusable: bool = False
    ) -> "LLMService | None":
        """
        Create LLMService from user-specific settings only

        Args:
            user_id: If provided, attempts to load user-specific settings
            raise_on_unusable: Raise :class:`LLMConfigurationError` for an active but unusable
                configuration instead of returning ``None``, so a caller that talks to the
                person can say "unusable" rather than "none configured".

        Returns:
            LLMService configured with user settings, or None if no user LLM is configured
        """
        from app.services.llm_service import LLMConfigurationError
        from app.services.llm_service import LLMService

        # Try to load user-specific settings
        if user_id:
            try:
                user_service = LLMService.create_from_user_settings(user_id)
                if user_service:
                    return user_service
            except LLMConfigurationError as e:
                logger.warning(f"Unusable LLM configuration for user {user_id}: {e.reason}")
                if raise_on_unusable:
                    raise
                return None

        # No fallback - users must explicitly configure LLM settings
        logger.info(f"No active LLM configuration found for user {user_id}")
        return None

    @staticmethod
    def create_from_config_id(user_id: int, config_id: int) -> "LLMService":
        """Create LLMService from ONE specific LLM configuration the user may use.

        Used by RAG chat, where a conversation can pin a model different from the
        user's active default. Shares the ownership check and key decryption with
        :meth:`create_from_user_settings` so a per-conversation override can never
        reach a configuration the caller isn't entitled to.

        Args:
            user_id: The caller (owner, or a user of a shared configuration).
            config_id: ``user_llm_settings.id`` to load.

        Returns:
            A configured service.

        Raises:
            LLMConfigurationError: the config is missing, not theirs, or unusable.
        """
        from app.db.base import SessionLocal
        from app.models.user_llm_settings import UserLLMSettings
        from app.services.llm_service import LLMConfigFailure
        from app.services.llm_service import LLMConfigurationError
        from app.services.llm_service import LLMService
        from app.utils.tenant_sharing import shared_usable_by

        db = SessionLocal()
        try:
            user_settings = (
                db.query(UserLLMSettings)
                .filter(
                    UserLLMSettings.id == config_id,
                    shared_usable_by(UserLLMSettings.user_id, UserLLMSettings.is_shared, user_id),
                )
                .first()
            )
            if not user_settings:
                logger.warning(f"LLM config {config_id} not available to user {user_id}")
                raise LLMConfigurationError(LLMConfigFailure.NOT_FOUND)

            try:
                api_key, bedrock = LLMService.credentials_from_row(user_settings)
            except ValueError:
                logger.error(f"Failed to decrypt stored credentials for LLM config {config_id}")
                raise LLMConfigurationError(LLMConfigFailure.CREDENTIALS) from None

            from app.services import llm_context_window

            declared_window = int(user_settings.max_tokens)
            try:
                provider = LLMProvider(user_settings.provider)
            except ValueError:
                raise LLMConfigurationError(LLMConfigFailure.UNKNOWN_PROVIDER) from None
            if provider == LLMProvider.BEDROCK and not resolve_region(bedrock):
                raise LLMConfigurationError(LLMConfigFailure.BEDROCK_REGION)
            config = LLMConfig(
                provider=provider,
                model=str(user_settings.model_name),
                api_key=api_key,
                base_url=str(user_settings.base_url) if user_settings.base_url else None,
                max_tokens=llm_context_window.effective_window(
                    db,
                    provider=str(user_settings.provider),
                    base_url=str(user_settings.base_url) if user_settings.base_url else None,
                    model=str(user_settings.model_name),
                    declared=declared_window,
                ),
                temperature=float(user_settings.temperature),
                bedrock=bedrock,
            )
            service = LLMService(config)
            service.budget_user_id = user_id
            return service
        except (ValueError, KeyError, TypeError) as e:
            # A database error is not "unavailable": it propagates as an outage.
            logger.error(f"Could not build LLMService from config {config_id}: {e}")
            raise LLMConfigurationError(LLMConfigFailure.INVALID) from None
        finally:
            db.close()

    @staticmethod
    def _resolve_user_llm_settings(db: "Session", user_id: int) -> _ResolvedUserLLM | None:
        """The user's active LLM configuration, or ``None`` when they have none.

        ``None`` means NO active configuration (system fallback is correct). An active
        configuration that cannot be used raises :class:`LLMConfigurationError` instead.

        DB-only (no network). Shared by :meth:`create_from_user_settings` and
        :meth:`is_configured_for_user` so the two cannot disagree about which row
        counts: a dangling or foreign ``active_llm_config_id``, an undecryptable key
        or an unknown provider string all raise here in both.
        """
        from app import models
        from app.models.user_llm_settings import UserLLMSettings
        from app.services.llm_service import LLMConfigFailure
        from app.services.llm_service import LLMConfigurationError
        from app.services.llm_service import LLMService
        from app.utils.tenant_sharing import shared_usable_by

        active_config_setting = (
            db.query(models.UserSetting)
            .filter(
                models.UserSetting.user_id == user_id,
                models.UserSetting.setting_key == "active_llm_config_id",
            )
            .first()
        )
        if not active_config_setting or not active_config_setting.setting_value:
            logger.info(f"No active LLM configuration for user {user_id}, checking system settings")
            return None

        try:
            active_config_id = int(active_config_setting.setting_value)
        except (TypeError, ValueError):
            logger.error(
                f"Configuration error for user {user_id}: active_llm_config_id "
                f"{active_config_setting.setting_value!r} is not an integer"
            )
            raise LLMConfigurationError(LLMConfigFailure.BAD_POINTER) from None

        # The active configuration may be the user's own or one shared within a tenant
        # both belong to; the stored pointer is re-validated because it outlives
        # membership changes.
        user_settings = (
            db.query(UserLLMSettings)
            .filter(
                UserLLMSettings.id == active_config_id,
                shared_usable_by(UserLLMSettings.user_id, UserLLMSettings.is_shared, user_id),
            )
            .first()
        )
        if not user_settings:
            logger.warning(
                f"Active LLM config {active_config_id} not found for user {user_id}, "
                "refusing system fallback"
            )
            raise LLMConfigurationError(LLMConfigFailure.NOT_FOUND)

        try:
            provider = LLMProvider(user_settings.provider)
        except ValueError as e:
            logger.error(f"Configuration error for user {user_id}: {e}")
            raise LLMConfigurationError(LLMConfigFailure.UNKNOWN_PROVIDER) from None

        try:
            api_key, bedrock = LLMService.credentials_from_row(user_settings)
        except ValueError:
            logger.error(f"Failed to decrypt stored credentials for user {user_id}")
            raise LLMConfigurationError(LLMConfigFailure.CREDENTIALS) from None

        if provider == LLMProvider.BEDROCK and not resolve_region(bedrock):
            logger.warning(
                f"Bedrock configuration for user {user_id} has no region and the server has "
                "none either, refusing system fallback"
            )
            raise LLMConfigurationError(LLMConfigFailure.BEDROCK_REGION)

        # The factory parses these; the DB-only answer must agree with it, or the
        # recovery sweep dispatches tasks that can only skip.
        try:
            float(user_settings.temperature)
            int(user_settings.max_tokens)
        except (TypeError, ValueError):
            logger.error(f"LLM temperature/max_tokens not numeric for user {user_id}")
            raise LLMConfigurationError(LLMConfigFailure.INVALID) from None

        base_url = str(user_settings.base_url) if user_settings.base_url else None
        return _ResolvedUserLLM(user_settings, provider, api_key, base_url, bedrock)

    @staticmethod
    def credentials_from_row(
        user_settings: "UserLLMSettings",
    ) -> tuple[str | None, BedrockConnection | None]:
        """``(api_key, bedrock_connection)`` for a stored configuration row.

        The single decryption point for a row's ``api_key`` column. For ``bedrock`` the column
        holds an encrypted JSON blob of region and AWS credentials, so it is unpacked into a
        :class:`BedrockConnection` and ``api_key`` is ``None``; for every other provider it is
        the key itself. A blob or key that will not decrypt raises ``ValueError``.
        """
        stored = user_settings.api_key
        if str(user_settings.provider) == LLMProvider.BEDROCK.value:
            try:
                connection = (
                    connection_from_encrypted(str(stored)) if stored else BedrockConnection()
                )
            except SecretDecryptionError as exc:
                raise ValueError("stored Bedrock credentials could not be decrypted") from exc
            if stored and not (connection.region or connection.has_credentials):
                raise ValueError("stored Bedrock credentials could not be decrypted")
            return None, connection
        if not stored:
            return None, None
        from app.utils.encryption import decrypt_api_key

        try:
            api_key = decrypt_api_key(str(stored))
        except SecretDecryptionError as exc:
            raise ValueError("stored API key could not be decrypted") from exc
        if not api_key:
            raise ValueError("stored API key could not be decrypted")
        return api_key, None

    @staticmethod
    def is_configured_for_user(db: "Session", user_id: int) -> bool:
        """Would :meth:`create_from_settings` build a service for this user?

        The cheap, DB-only form of that question — it reads the same settings and
        applies the same checks (provider set, credentials or endpoint present) but
        opens no connection, builds no client and measures no context window. For
        callers that decide whether to dispatch LLM work at all, such as the
        post-transcription recovery sweep: a looser check there dispatches tasks
        that can only skip.
        """
        from app.services.llm_service import LLMService

        return LLMService.describe_for_user(db, user_id) is not None

    @staticmethod
    def describe_for_user(db: "Session", user_id: int) -> tuple[str, str, str | None] | None:
        """``(provider, model, base_url)`` of the LLM this user's features would use.

        ``None`` exactly when :meth:`is_configured_for_user` is False; the same DB-only
        resolution (user config, else system settings), no network. A user whose ACTIVE
        configuration is unusable gets ``None`` -- never the system provider's description.
        """
        from app.services.llm_service import LLMConfigurationError
        from app.services.llm_service import LLMService

        try:
            resolved = LLMService._resolve_user_llm_settings(db, user_id)
        except LLMConfigurationError:
            return None
        if resolved is not None:
            if not LLMService._endpoint_resolves(resolved.provider, resolved.base_url):
                return None
            return resolved.provider.value, str(resolved.settings.model_name), resolved.base_url
        system = LLMService._resolve_system_provider()
        if system is None:
            return None
        provider, model, _api_key, base_url = system
        return provider.value, model, base_url

    @staticmethod
    def create_from_user_settings(user_id: int) -> "LLMService | None":
        """Create LLMService from the user's active configuration.

        No active configuration -> the system provider (or ``None``). An active
        configuration that cannot be used raises :class:`LLMConfigurationError`; it never
        falls back to the system provider. Database errors propagate.
        """
        from app.db.base import SessionLocal
        from app.services.llm_service import LLMConfigFailure
        from app.services.llm_service import LLMConfigurationError
        from app.services.llm_service import LLMService

        db = SessionLocal()
        try:
            resolved = LLMService._resolve_user_llm_settings(db, user_id)
            if resolved is None:
                system = LLMService.create_from_system_settings()
                if system is not None:
                    system.budget_user_id = user_id
                return system
            user_settings = resolved.settings

            # Create config from user settings - USER'S DECLARED CONTEXT WINDOW, narrowed
            # only by a MEASURED ceiling read off the live server (#833) -- never inferred.
            from app.services import llm_context_window

            temperature_float = float(user_settings.temperature)
            declared_window = int(user_settings.max_tokens)

            config = LLMConfig(
                provider=resolved.provider,
                model=str(user_settings.model_name),
                api_key=resolved.api_key,
                base_url=resolved.base_url,
                bedrock=resolved.bedrock,
                max_tokens=llm_context_window.effective_window(
                    db,
                    provider=str(user_settings.provider),
                    base_url=resolved.base_url,
                    model=str(user_settings.model_name),
                    declared=declared_window,
                ),
                temperature=temperature_float,
            )

            logger.info(
                f"Created LLMService for user {user_id}: {resolved.provider}/{user_settings.model_name}, user_context_window={user_settings.max_tokens}"
            )
            service = LLMService(config)
            service.budget_user_id = user_id
            return service

        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Configuration error for user {user_id}: {e}")
            raise LLMConfigurationError(LLMConfigFailure.INVALID) from None
        finally:
            db.close()

    @staticmethod
    def _get_provider_config(
        provider: LLMProvider,
    ) -> tuple[str, str | None, str | None] | None:
        """
        Get provider-specific configuration (model, api_key, base_url) with validation.

        Returns:
            Tuple of (model, api_key, base_url) if valid, None if validation fails.
        """
        provider_settings: dict[LLMProvider, dict[str, Any]] = {
            LLMProvider.VLLM: {
                "model": settings.VLLM_MODEL_NAME,
                "api_key": settings.VLLM_API_KEY,
                "base_url": settings.VLLM_BASE_URL,
                "requires_api_key": False,
                "invalid_model_defaults": ["gpt-oss"],
                "invalid_url_defaults": ["http://localhost:8012/v1"],
            },
            LLMProvider.OPENAI: {
                "model": settings.OPENAI_MODEL_NAME,
                "api_key": settings.OPENAI_API_KEY,
                "base_url": settings.OPENAI_BASE_URL,
                "requires_api_key": True,
            },
            LLMProvider.OLLAMA: {
                "model": settings.OLLAMA_MODEL_NAME,
                "api_key": None,
                "base_url": settings.OLLAMA_BASE_URL,
                "requires_api_key": False,
            },
            LLMProvider.CLAUDE: {
                "model": settings.ANTHROPIC_MODEL_NAME,
                "api_key": settings.ANTHROPIC_API_KEY,
                "base_url": settings.ANTHROPIC_BASE_URL,
                "requires_api_key": True,
            },
            LLMProvider.ANTHROPIC: {
                "model": settings.ANTHROPIC_MODEL_NAME,
                "api_key": settings.ANTHROPIC_API_KEY,
                "base_url": settings.ANTHROPIC_BASE_URL,
                "requires_api_key": True,
            },
            LLMProvider.OPENROUTER: {
                "model": settings.OPENROUTER_MODEL_NAME,
                "api_key": settings.OPENROUTER_API_KEY,
                "base_url": settings.OPENROUTER_BASE_URL,
                "requires_api_key": True,
            },
        }

        if provider == LLMProvider.CUSTOM:
            logger.info("Custom provider requires user-specific configuration via UI")
            return None

        if provider == LLMProvider.BEDROCK:
            # Bedrock has no `base_url`/`api_key` shape at all (SDK call, credentials via
            # the IAM chain — see `llm_bedrock.py`'s module docstring), so it does not fit
            # the dict-driven validation below, which assumes an HTTP endpoint and casts
            # `base_url` to `str()` unconditionally. Validated instead against the two
            # things that actually gate a Bedrock call: a model ID, and a region for
            # boto3 to construct the client against (`_client` in `llm_bedrock.py` raises
            # `BedrockNotConfiguredError` without one).
            from app.services.bedrock_settings import env_region
            from app.services.bedrock_settings import resolve_default_model

            model = resolve_default_model()
            if not env_region():
                logger.info("Bedrock provider configured but no AWS region set")
                return None
            return model, None, None

        if provider not in provider_settings:
            logger.warning(f"Unsupported LLM provider: {provider}")
            return None

        cfg = provider_settings[provider]
        model = str(cfg["model"])
        api_key_value = cfg["api_key"]
        api_key = str(api_key_value) if api_key_value else None
        base_url = str(cfg["base_url"])

        # Validate model
        if not model or not model.strip():
            logger.info(f"{provider.value} provider configured but no model name set")
            return None

        # Check for invalid model defaults (e.g., vLLM "gpt-oss")
        invalid_models = cfg.get("invalid_model_defaults", [])
        if model.strip() in invalid_models:
            logger.info(f"{provider.value} provider configured but no valid model name set")
            return None

        # Check for invalid URL defaults
        invalid_urls = cfg.get("invalid_url_defaults", [])
        if base_url in invalid_urls:
            logger.info(
                f"{provider.value} provider configured but using default localhost endpoint (likely not available)"
            )
            return None

        # Validate API key if required
        if cfg.get("requires_api_key") and (not api_key or not api_key.strip()):
            logger.info(f"{provider.value} provider configured but no API key set")
            return None

        return model, api_key, base_url

    @staticmethod
    def _resolve_system_provider() -> tuple[LLMProvider, str, str | None, str | None] | None:
        """``(provider, model, api_key, base_url)`` from the env settings, or ``None``.

        No I/O at all. ``LLM_PROVIDER`` alone is not enough: the provider also needs
        its credentials or endpoint (``_get_provider_config``) and an endpoint the
        client would accept.
        """
        from app.services.llm_service import LLMService

        if not settings.LLM_PROVIDER or settings.LLM_PROVIDER.strip() == "":
            logger.info("No LLM provider configured (LLM_PROVIDER not set)")
            return None

        try:
            provider = LLMProvider(settings.LLM_PROVIDER)
        except ValueError as e:
            logger.warning(f"Invalid LLM provider '{settings.LLM_PROVIDER}': {e}")
            return None

        provider_config = LLMService._get_provider_config(provider)
        if provider_config is None:
            return None

        model, api_key, base_url = provider_config
        if not LLMService._endpoint_resolves(provider, base_url):
            logger.info(f"{provider.value} provider configured but no usable endpoint")
            return None
        return provider, model, api_key, base_url

    @staticmethod
    def create_from_system_settings() -> "LLMService | None":
        """Create LLMService from system settings"""
        from app.services.llm_service import LLMService

        resolved = LLMService._resolve_system_provider()
        if resolved is None:
            return None

        provider, model, api_key, base_url = resolved

        window = 32768  # Conservative system default
        try:
            from app.db.base import SessionLocal
            from app.services import llm_context_window

            db = SessionLocal()
            try:
                window = llm_context_window.effective_window(
                    db, provider=str(provider), base_url=base_url, model=model, declared=window
                )
            finally:
                db.close()
        except SQLAlchemyError as exc:  # a capability read must not break the fallback service
            logger.warning(
                "Context-window measurement unavailable for the system LLM: %s", exc, exc_info=True
            )

        try:
            config = LLMConfig(
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                max_tokens=window,
                temperature=0.3,
            )

            logger.info(
                f"Created LLMService from system settings: {provider}/{model}, context_window={config.max_tokens}"
            )
            return LLMService(config)
        except (ValueError, TypeError, ImportError, OSError) as e:
            logger.exception(f"Failed to create LLMService from system settings: {e}")
            return None
