"""
API endpoints for user LLM settings management
"""

import contextlib
import logging
import time
import uuid
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider as ServiceLLMProvider
from app.services.llm_service import LLMService
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key
from app.utils.encryption import test_encryption
from app.utils.uuid_helpers import get_llm_config_by_uuid

router = APIRouter()
logger = logging.getLogger(__name__)


def _enrich_with_owner(config, owner, is_own: bool) -> dict:
    """Build a UserLLMSettingsPublic-compatible dict with owner attribution."""
    return {
        "uuid": config.uuid,
        "user_id": owner.uuid if owner else config.user_id,
        "name": config.name,
        "provider": config.provider,
        "model_name": config.model_name,
        "base_url": config.base_url,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "is_active": config.is_active,
        "last_tested": config.last_tested,
        "test_status": config.test_status,
        "test_message": config.test_message,
        "has_api_key": bool(config.api_key),
        "is_shared": config.is_shared,
        "shared_at": config.shared_at,
        "owner_name": owner.full_name if owner else None,
        "owner_role": owner.role if owner else None,
        "is_own": is_own,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


def _clear_shared_active_references(
    db: Session, config_id: int, setting_key: str, *, exclude_user_id: int | None = None
):
    """Remove UserSetting rows pointing to a deleted/unshared config (for non-owners)."""
    q = db.query(models.UserSetting).filter(
        models.UserSetting.setting_key == setting_key,
        models.UserSetting.setting_value == str(config_id),
    )
    if exclude_user_id is not None:
        q = q.filter(models.UserSetting.user_id != exclude_user_id)
    q.delete(synchronize_session=False)


def _set_active_configuration(db: Session, user_id: int, config_id: int) -> None:
    """Helper function to set active LLM configuration for a user"""
    # Check if setting already exists
    existing_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key == "active_llm_config_id",
        )
        .first()
    )

    if existing_setting:
        existing_setting.setting_value = str(config_id)  # type: ignore[assignment]
        db.add(existing_setting)
    else:
        new_setting = models.UserSetting(
            user_id=user_id,
            setting_key="active_llm_config_id",
            setting_value=str(config_id),
        )
        db.add(new_setting)

    db.commit()


def _get_provider_defaults() -> list[schemas.ProviderDefaults]:
    """Get default configurations for all supported providers"""
    return [
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.OPENAI,
            default_model="gpt-4o-mini",
            default_base_url="https://api.openai.com/v1",
            requires_api_key=True,
            supports_custom_url=True,
            max_context_length=128000,
            description="OpenAI's GPT models - reliable and well-supported",
        ),
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.VLLM,
            default_model="gpt-oss",
            default_base_url="http://localhost:8012/v1",
            requires_api_key=False,
            supports_custom_url=True,
            max_context_length=32768,
            description="vLLM server for local or custom model deployment",
        ),
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.OLLAMA,
            default_model="llama3.2:latest",
            default_base_url="http://localhost:11434",
            requires_api_key=False,
            supports_custom_url=True,
            max_context_length=128000,
            description="Ollama for local model deployment - uses native /api/chat endpoint",
        ),
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.ANTHROPIC,
            default_model="claude-opus-4-5-20251101",
            default_base_url="https://api.anthropic.com/v1",
            requires_api_key=True,
            supports_custom_url=False,
            max_context_length=200000,
            description="Anthropic's Claude models - excellent for analysis",
        ),
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.OPENROUTER,
            default_model="anthropic/claude-3.5-haiku",
            default_base_url="https://openrouter.ai/api/v1",
            requires_api_key=True,
            supports_custom_url=False,
            max_context_length=200000,
            description="OpenRouter provides access to many model providers",
        ),
    ]


@router.get("/providers", response_model=schemas.SupportedProvidersResponse)
def get_supported_providers() -> Any:
    """
    Get list of supported LLM providers with their default configurations
    """
    providers = _get_provider_defaults()
    return schemas.SupportedProvidersResponse(providers=providers)


@router.get("", response_model=schemas.UserLLMConfigurationsList)
def get_user_configurations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get all user's LLM configurations
    """
    # Get all user configurations
    configurations = (
        db.query(models.UserLLMSettings)
        .filter(models.UserLLMSettings.user_id == current_user.id)
        .order_by(models.UserLLMSettings.created_at.desc())
        .all()
    )

    # Get active configuration ID
    active_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key == "active_llm_config_id",
        )
        .first()
    )

    active_config_uuid: UUID | None = None
    if active_setting and active_setting.setting_value:
        with contextlib.suppress(ValueError):
            active_config_id = int(active_setting.setting_value)
            # Convert integer ID to UUID by finding the config
            active_config = (
                db.query(models.UserLLMSettings)
                .filter(models.UserLLMSettings.id == active_config_id)
                .first()
            )
            if active_config:
                active_config_uuid = UUID(str(active_config.uuid))

    # Convert to public schemas
    public_configs: list[schemas.UserLLMSettingsPublic] = []
    for config in configurations:
        # Let FastAPI handle the conversion automatically
        public_configs.append(config)  # type: ignore[arg-type]

    # Fetch shared configs from OTHER users
    shared_configs = (
        db.query(models.UserLLMSettings)
        .filter(
            models.UserLLMSettings.is_shared == True,  # noqa: E712
            models.UserLLMSettings.user_id != current_user.id,
        )
        .order_by(models.UserLLMSettings.shared_at.desc())
        .all()
    )

    # Batch-fetch owners for attribution
    owner_ids = {c.user_id for c in shared_configs}
    owners = (
        {u.id: u for u in db.query(models.User).filter(models.User.id.in_(owner_ids)).all()}
        if owner_ids
        else {}
    )
    public_shared = [
        _enrich_with_owner(c, owners.get(c.user_id), is_own=False) for c in shared_configs
    ]

    return schemas.UserLLMConfigurationsList(
        configurations=public_configs,
        shared_configurations=public_shared,  # type: ignore[arg-type]
        active_configuration_id=active_config_uuid,
        total=len(public_configs),
    )


@router.get("/status", response_model=schemas.LLMSettingsStatus)
def get_llm_settings_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get status information about user's LLM settings
    """
    # Get all own configurations
    total_configs = (
        db.query(models.UserLLMSettings)
        .filter(models.UserLLMSettings.user_id == current_user.id)
        .count()
    )

    # Get active configuration (may be own or shared)
    active_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key == "active_llm_config_id",
        )
        .first()
    )

    active_config = None
    if active_setting and active_setting.setting_value:
        try:
            active_config_id = int(active_setting.setting_value)
            active_config = (
                db.query(models.UserLLMSettings)
                .filter(
                    models.UserLLMSettings.id == active_config_id,
                    or_(
                        models.UserLLMSettings.user_id == current_user.id,
                        models.UserLLMSettings.is_shared == True,  # noqa: E712
                    ),
                )
                .first()
            )
        except ValueError:
            pass

    active_public = (
        schemas.UserLLMSettingsPublic.model_validate(active_config) if active_config else None
    )

    return schemas.LLMSettingsStatus(
        has_settings=total_configs > 0 or active_config is not None,
        active_configuration=active_public,
        total_configurations=total_configs,
        using_system_default=not bool(active_config),
    )


@router.get("/config/{config_uuid}", response_model=schemas.UserLLMSettingsPublic)
def get_user_configuration(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get a specific user's LLM configuration
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)

    if user_config.user_id != current_user.id and not user_config.is_shared:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to access this configuration",
        )

    # Convert to public schema (excludes API key)
    return user_config


@router.post("", response_model=schemas.UserLLMSettingsPublic)
def create_user_llm_configuration(
    *,
    db: Session = Depends(get_db),
    settings_in: schemas.UserLLMSettingsCreate,
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Create a new LLM configuration for the current user
    """
    # Check if user already has a configuration with this name
    existing_config = (
        db.query(models.UserLLMSettings)
        .filter(
            models.UserLLMSettings.user_id == current_user.id,
            models.UserLLMSettings.name == settings_in.name,
        )
        .first()
    )

    if existing_config:
        raise HTTPException(
            status_code=400,
            detail=f"Configuration with name '{settings_in.name}' already exists.",
        )

    # Test encryption before proceeding
    if not test_encryption():
        raise HTTPException(status_code=500, detail="Encryption system is not working properly")

    # Encrypt API key if provided
    encrypted_api_key = None
    if settings_in.api_key:
        encrypted_api_key = encrypt_api_key(settings_in.api_key)
        if not encrypted_api_key:
            raise HTTPException(status_code=500, detail="Failed to encrypt API key")

    # Create new configuration
    settings_data = settings_in.model_dump(exclude={"api_key"})
    settings_data.update({"user_id": current_user.id, "api_key": encrypted_api_key})

    # Set shared_at timestamp if shared on creation
    if settings_data.get("is_shared"):
        settings_data["shared_at"] = datetime.now(timezone.utc)

    user_config = models.UserLLMSettings(**settings_data)
    db.add(user_config)
    db.commit()
    db.refresh(user_config)

    # If this is the user's first configuration, make it active
    existing_count = (
        db.query(models.UserLLMSettings)
        .filter(models.UserLLMSettings.user_id == current_user.id)
        .count()
    )

    if existing_count == 1:  # This is the first config
        _set_active_configuration(db, current_user.id, user_config.id)

    return user_config


@router.put("/config/{config_uuid}", response_model=schemas.UserLLMSettingsPublic)
def update_user_llm_configuration(
    config_uuid: str,
    *,
    db: Session = Depends(get_db),
    settings_in: schemas.UserLLMSettingsUpdate,
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Update a specific LLM configuration
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)

    if user_config.user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to access this configuration",
        )

    config_id = user_config.id

    # Check for name conflicts if name is being updated
    if settings_in.name and settings_in.name != user_config.name:
        existing_config = (
            db.query(models.UserLLMSettings)
            .filter(
                models.UserLLMSettings.user_id == current_user.id,
                models.UserLLMSettings.name == settings_in.name,
                models.UserLLMSettings.id != config_id,
            )
            .first()
        )

        if existing_config:
            raise HTTPException(
                status_code=400,
                detail=f"Configuration with name '{settings_in.name}' already exists.",
            )

    # Handle API key encryption
    update_data = settings_in.model_dump(exclude_unset=True, exclude={"api_key"})

    if "api_key" in settings_in.model_fields_set and settings_in.api_key is not None:
        if settings_in.api_key.strip():  # Non-empty API key
            if not test_encryption():
                raise HTTPException(
                    status_code=500, detail="Encryption system is not working properly"
                )

            encrypted_api_key = encrypt_api_key(settings_in.api_key)
            if not encrypted_api_key:
                raise HTTPException(status_code=500, detail="Failed to encrypt API key")
            update_data["api_key"] = encrypted_api_key
        else:  # Empty API key means remove it
            update_data["api_key"] = None

    # Handle is_shared toggle with shared_at timestamp
    if settings_in.is_shared is not None:
        if settings_in.is_shared and not user_config.is_shared:
            update_data["shared_at"] = datetime.now(timezone.utc)
        elif not settings_in.is_shared and user_config.is_shared:
            update_data["shared_at"] = None
            _clear_shared_active_references(
                db,
                user_config.id,
                "active_llm_config_id",
                exclude_user_id=current_user.id,
            )

    # Reset test status when settings change (but not for share-only updates)
    non_share_keys = {k for k in update_data if k not in ("is_shared", "shared_at")}
    if non_share_keys:
        update_data["test_status"] = "untested"
        update_data["test_message"] = None
        update_data["last_tested"] = None

    # Apply updates
    for field, value in update_data.items():
        setattr(user_config, field, value)

    db.add(user_config)
    db.commit()
    db.refresh(user_config)

    return user_config


@router.post("/set-active", response_model=schemas.UserLLMSettingsPublic)
def set_active_configuration(
    *,
    db: Session = Depends(get_db),
    request: schemas.SetActiveConfigRequest,
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Set the active LLM configuration for the user
    """
    # Verify the configuration exists and belongs to the user (or is shared) using UUID
    user_config = get_llm_config_by_uuid(db, request.configuration_id)

    if user_config.user_id != current_user.id and not user_config.is_shared:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to access this configuration",
        )

    # Set as active using the integer ID (internal)
    _set_active_configuration(db, current_user.id, user_config.id)

    return user_config


@router.delete("/config/{config_uuid}")
def delete_user_llm_configuration(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Delete a specific LLM configuration
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)

    if user_config.user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to access this configuration",
        )

    config_id = user_config.id

    # Clean up other users who had this shared config active (preserve owner's for auto-promote)
    if user_config.is_shared:
        _clear_shared_active_references(
            db,
            int(config_id),
            "active_llm_config_id",
            exclude_user_id=current_user.id,
        )

    # Check if this is the active configuration
    active_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key == "active_llm_config_id",
        )
        .first()
    )

    is_active = False
    if active_setting and active_setting.setting_value == str(config_id):
        is_active = True

    # Delete the configuration
    db.delete(user_config)

    # If this was the active configuration, clear the active setting
    # or set another configuration as active if available
    if is_active:
        # Find another configuration to set as active
        remaining_config = (
            db.query(models.UserLLMSettings)
            .filter(
                models.UserLLMSettings.user_id == current_user.id,
                models.UserLLMSettings.id != config_id,
            )
            .first()
        )

        if remaining_config:
            # Set the first remaining config as active
            _set_active_configuration(db, current_user.id, remaining_config.id)
        else:
            # No configurations left, remove the active setting
            if active_setting:
                db.delete(active_setting)

    db.commit()

    return {"detail": "Configuration deleted successfully."}


@router.delete("/all")
def delete_all_user_configurations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Delete all user's LLM configurations (revert to system defaults)
    """
    # Delete all configurations
    deleted_count = (
        db.query(models.UserLLMSettings)
        .filter(models.UserLLMSettings.user_id == current_user.id)
        .delete()
    )

    # Delete active setting
    active_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key == "active_llm_config_id",
        )
        .first()
    )

    if active_setting:
        db.delete(active_setting)

    db.commit()

    return {
        "detail": f"All {deleted_count} configurations deleted successfully. Using system defaults."
    }


@router.post("/test", response_model=schemas.ConnectionTestResponse)
async def test_llm_connection(
    *,
    test_request: schemas.ConnectionTestRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Test connection to LLM provider without saving settings.
    If config_id is provided and no api_key, will use the stored API key from that config.
    """
    start_time = time.time()

    try:
        # If no api_key provided but config_id is, look up the stored key
        effective_api_key = test_request.api_key
        if not effective_api_key and test_request.config_id:
            try:
                config = (
                    db.query(models.UserLLMSettings)
                    .filter(
                        models.UserLLMSettings.uuid == test_request.config_id,
                        or_(
                            models.UserLLMSettings.user_id == current_user.id,
                            models.UserLLMSettings.is_shared == True,  # noqa: E712
                        ),
                    )
                    .first()
                )
                if config and config.api_key:
                    # Decrypt the stored API key
                    effective_api_key = decrypt_api_key(str(config.api_key))
            except Exception as e:
                logger.warning(
                    f"Could not retrieve stored API key for config {test_request.config_id}: {e}"
                )

        # Map schema enum to service enum
        service_provider = ServiceLLMProvider(test_request.provider.value)

        # Create LLM config for testing
        llm_config = LLMConfig(
            provider=service_provider,
            model=test_request.model_name,
            api_key=effective_api_key,
            base_url=test_request.base_url,
        )

        # Create and test LLM service
        llm_service = LLMService(llm_config)
        try:
            # Test the connection
            actual_url = llm_service.endpoints[service_provider]
            logger.debug(
                f"Testing LLM connection to: {actual_url} (Provider: {service_provider}, Model: {test_request.model_name})"
            )

            success, message = llm_service.validate_connection()
            response_time = int((time.time() - start_time) * 1000)

            return schemas.ConnectionTestResponse(
                success=success,
                status=schemas.ConnectionStatus.SUCCESS
                if success
                else schemas.ConnectionStatus.FAILED,
                message=f"{message} (URL: {actual_url})",
                response_time_ms=response_time,
            )
        finally:
            llm_service.close()

    except Exception as e:
        response_time = int((time.time() - start_time) * 1000)
        logger.error(f"LLM connection test failed: {e}")

        return schemas.ConnectionTestResponse(
            success=False,
            status=schemas.ConnectionStatus.FAILED,
            message=f"Connection test failed: {str(e)}",
            response_time_ms=response_time,
        )


@router.post("/test-current", response_model=schemas.ConnectionTestResponse)
async def test_active_configuration(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Test connection using current user's active LLM configuration
    """
    # Get active configuration
    active_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key == "active_llm_config_id",
        )
        .first()
    )

    if not active_setting or not active_setting.setting_value:
        raise HTTPException(
            status_code=404,
            detail="No active LLM configuration found. Please set an active configuration first.",
        )

    try:
        active_config_id = int(active_setting.setting_value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid active configuration ID") from e

    user_config = (
        db.query(models.UserLLMSettings)
        .filter(
            models.UserLLMSettings.id == active_config_id,
            or_(
                models.UserLLMSettings.user_id == current_user.id,
                models.UserLLMSettings.is_shared == True,  # noqa: E712
            ),
        )
        .first()
    )

    if not user_config:
        raise HTTPException(
            status_code=404,
            detail="Active LLM configuration not found.",
        )

    # Decrypt API key
    api_key = None
    if user_config.api_key:
        api_key = decrypt_api_key(str(user_config.api_key))
        if not api_key and user_config.api_key:  # Decryption failed
            raise HTTPException(status_code=500, detail="Failed to decrypt stored API key")

    # Test connection
    test_request = schemas.ConnectionTestRequest(
        provider=schemas.LLMProvider(user_config.provider),
        model_name=str(user_config.model_name),
        api_key=api_key,
        base_url=str(user_config.base_url) if user_config.base_url else None,
    )

    result = await test_llm_connection(test_request=test_request, current_user=current_user)

    # Only write back test status if the current user owns the config
    if user_config.user_id == current_user.id:
        user_config.test_status = result.status.value  # type: ignore[assignment]
        user_config.test_message = result.message  # type: ignore[assignment]
        user_config.last_tested = datetime.now(timezone.utc)  # type: ignore[assignment]

        db.add(user_config)
        db.commit()

    return result


@router.post("/test-config/{config_uuid}", response_model=schemas.ConnectionTestResponse)
async def test_specific_configuration(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Test connection for a specific LLM configuration
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)

    if user_config.user_id != current_user.id and not user_config.is_shared:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to access this configuration",
        )

    # Decrypt API key
    api_key = None
    if user_config.api_key:
        api_key = decrypt_api_key(str(user_config.api_key))
        if not api_key and user_config.api_key:  # Decryption failed
            raise HTTPException(status_code=500, detail="Failed to decrypt stored API key")

    # Test connection
    test_request = schemas.ConnectionTestRequest(
        provider=schemas.LLMProvider(user_config.provider),
        model_name=str(user_config.model_name),
        api_key=api_key,
        base_url=str(user_config.base_url) if user_config.base_url else None,
    )

    result = await test_llm_connection(test_request=test_request, current_user=current_user)

    # Only write back test status if the current user owns the config
    if user_config.user_id == current_user.id:
        user_config.test_status = result.status.value  # type: ignore[assignment]
        user_config.test_message = result.message  # type: ignore[assignment]
        user_config.last_tested = datetime.now(timezone.utc)  # type: ignore[assignment]

        db.add(user_config)
        db.commit()

    return result


@router.get("/config/{config_uuid}/api-key")
async def get_config_api_key(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get the decrypted API key for a specific configuration.
    Only the owner can access their own API keys.
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)

    if user_config.user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to access this configuration",
        )

    if not user_config.api_key:
        return {"api_key": None}

    # Decrypt API key
    api_key = decrypt_api_key(str(user_config.api_key))
    if not api_key:
        raise HTTPException(status_code=500, detail="Failed to decrypt stored API key")

    return {"api_key": api_key}


@router.get("/ollama/models")
async def get_ollama_models(
    base_url: str,
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get available models from an Ollama instance
    """
    import aiohttp

    try:
        # Clean up base URL
        clean_url = base_url.strip().rstrip("/")
        if clean_url.endswith("/v1"):
            clean_url = clean_url[:-3]  # Remove /v1 suffix

        models_url = f"{clean_url}/api/tags"

        timeout = aiohttp.ClientTimeout(total=10)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(models_url) as response,
        ):
            if response.status == 200:
                data = await response.json()
                models = []

                if "models" in data:
                    for model in data["models"]:
                        models.append(
                            {
                                "name": model.get("name", ""),
                                "size": model.get("size", 0),
                                "modified_at": model.get("modified_at", ""),
                                "digest": model.get("digest", ""),
                                "details": model.get("details", {}),
                                "display_name": model.get("name", "").split(":")[
                                    0
                                ],  # Remove tag for display
                            }
                        )

                return {
                    "success": True,
                    "models": models,
                    "total": len(models),
                    "message": f"Found {len(models)} models on Ollama server",
                }
            else:
                error_text = await response.text()
                return {
                    "success": False,
                    "models": [],
                    "total": 0,
                    "message": f"Failed to fetch models: HTTP {response.status} - {error_text}",
                }
    except aiohttp.ClientError as e:
        return {
            "success": False,
            "models": [],
            "total": 0,
            "message": f"Connection error: {str(e)}",
        }
    except Exception as e:
        logger.error(f"Error fetching Ollama models from {base_url}: {e}")
        return {
            "success": False,
            "models": [],
            "total": 0,
            "message": f"Unexpected error: {str(e)}",
        }


# --- Model Discovery Helper Functions ---


def _model_discovery_response(
    success: bool, model_list: list[Any] | None = None, message: str = ""
) -> dict[str, Any]:
    """Create a standardized model discovery response."""
    return {
        "success": success,
        "models": model_list or [],
        "total": len(model_list) if model_list else 0,
        "message": message,
    }


def _get_stored_api_key(db: Session, config_id: str, user_id: int) -> str | None:
    """Retrieve and decrypt stored API key for a config (own or shared)."""
    try:
        config_uuid = uuid.UUID(config_id)
        config = (
            db.query(models.UserLLMSettings)
            .filter(
                models.UserLLMSettings.uuid == config_uuid,
                or_(
                    models.UserLLMSettings.user_id == user_id,
                    models.UserLLMSettings.is_shared == True,  # noqa: E712
                ),
            )
            .first()
        )
        if config and config.api_key:
            return decrypt_api_key(str(config.api_key))
    except (ValueError, Exception) as e:
        logger.warning(f"Could not retrieve stored API key for config {config_id}: {e}")
    return None


def _extract_raw_models(data: Any) -> tuple[list | None, str | None]:
    """
    Extract raw models list from various OpenAI-compatible response formats.

    Returns (raw_models, error_message). If error_message is set, raw_models is None.
    """
    # Format 1: OpenAI standard { "data": [...] }
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        logger.debug("Model discovery: Using OpenAI standard format (data array)")
        return data["data"], None

    # Format 2: Direct array response [...]
    if isinstance(data, list):
        logger.debug("Model discovery: Using direct array format")
        return data, None

    # Format 3: { "models": [...] } (some providers use this)
    if isinstance(data, dict) and "models" in data and isinstance(data["models"], list):
        logger.debug("Model discovery: Using 'models' key format")
        return data["models"], None

    # Format 4: { "object": "list", "data": [...] } (explicit OpenAI format)
    if isinstance(data, dict) and data.get("object") == "list" and "data" in data:
        logger.debug("Model discovery: Using OpenAI list object format")
        return data["data"], None

    # Unexpected format
    keys_info = list(data.keys()) if isinstance(data, dict) else type(data).__name__
    error_msg = (
        f"Unexpected response format. Expected 'data' or 'models' array. Got keys: {keys_info}"
    )
    logger.warning(f"Model discovery: {error_msg}")
    return None, error_msg


def _parse_model_entry(model: Any) -> dict | None:
    """Parse a single model entry into standardized format."""
    if isinstance(model, dict):
        model_id = model.get("id") or model.get("name") or model.get("model") or ""
        return {
            "name": model.get("name", model_id),
            "id": model_id,
            "owned_by": model.get("owned_by", model.get("owner", "")),
            "created": model.get("created", model.get("created_at", 0)),
        }
    if isinstance(model, str):
        return {"name": model, "id": model, "owned_by": "", "created": 0}
    logger.warning(f"Model discovery: Skipping unexpected model format: {type(model)}")
    return None


def _get_http_error_message(status_code: int, models_url: str, error_text: str = "") -> str:
    """Get user-friendly error message for HTTP error codes."""
    error_messages = {
        401: "Authentication failed: Invalid or missing API key",
        403: "Access forbidden: Check API key permissions",
        404: f"Models endpoint not found at {models_url}. Check base URL configuration.",
    }
    if status_code in error_messages:
        return error_messages[status_code]
    return f"Failed to fetch models: HTTP {status_code} - {error_text[:200]}"


@router.get("/openai-compatible/models")
async def get_openai_compatible_models(
    base_url: str,
    api_key: str | None = None,
    config_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get available models from an OpenAI-compatible API endpoint.

    Supports: OpenAI, vLLM, OpenRouter, and other OpenAI-compatible providers.
    If config_id is provided and no api_key, will use the stored API key from that config.
    """
    import aiohttp

    # Resolve effective API key
    effective_api_key = api_key
    if not effective_api_key and config_id:
        effective_api_key = _get_stored_api_key(db, config_id, current_user.id)

    # Build models URL
    clean_url = base_url.strip().rstrip("/")
    if not clean_url.endswith("/v1"):
        clean_url = f"{clean_url}/v1"
    models_url = f"{clean_url}/models"

    # Prepare headers
    headers = {"Authorization": f"Bearer {effective_api_key}"} if effective_api_key else {}

    try:
        return await _fetch_and_parse_models(models_url, headers, base_url)
    except aiohttp.ClientConnectorError:
        logger.warning(f"Model discovery: Connection failed to {base_url}")
        return _model_discovery_response(
            False,
            message=f"Connection failed: Could not reach {base_url}. Check the URL and ensure the server is running.",
        )
    except aiohttp.ClientError as e:
        logger.warning(f"Model discovery: Client error for {base_url}: {e}")
        return _model_discovery_response(False, message=f"Connection error: {str(e)}")
    except TimeoutError:
        logger.warning(f"Model discovery: Timeout connecting to {base_url}")
        return _model_discovery_response(
            False,
            message=f"Connection timeout: Server at {base_url} did not respond within 10 seconds.",
        )
    except Exception as e:
        logger.error(f"Error fetching OpenAI-compatible models from {base_url}: {e}", exc_info=True)
        return _model_discovery_response(False, message=f"Unexpected error: {str(e)}")


async def _fetch_and_parse_models(models_url: str, headers: dict, base_url: str) -> dict[str, Any]:
    """Fetch models from URL and parse the response."""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=10)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.get(models_url, headers=headers) as response,
    ):
        if response.status != 200:
            error_text = await response.text() if response.status not in (401, 403, 404) else ""
            if response.status not in (401, 403, 404):
                logger.warning(
                    f"Model discovery: HTTP {response.status} from {models_url}: {error_text[:200]}"
                )
            return _model_discovery_response(
                False, message=_get_http_error_message(response.status, models_url, error_text)
            )

        # Parse JSON response
        try:
            data = await response.json()
        except Exception as json_err:
            logger.warning(f"Model discovery: Invalid JSON response from {models_url}: {json_err}")
            return _model_discovery_response(
                False, message=f"Invalid JSON response from provider: {str(json_err)}"
            )

        # Extract raw models from various formats
        raw_models, error_msg = _extract_raw_models(data)
        if error_msg:
            return _model_discovery_response(False, message=error_msg)

        # Parse each model entry
        model_list = [parsed for m in (raw_models or []) if (parsed := _parse_model_entry(m))]

        if not model_list and raw_models:
            logger.warning(
                f"Model discovery: Found {len(raw_models)} raw models but none could be parsed"
            )
            return _model_discovery_response(
                False,
                message=f"Found {len(raw_models)} models but could not parse them. Check provider compatibility.",
            )

        logger.info(
            f"Model discovery: Successfully found {len(model_list)} models from {models_url}"
        )
        return _model_discovery_response(True, model_list, f"Found {len(model_list)} models")


def _parse_anthropic_model(model: dict) -> dict:
    """Parse a single Anthropic model entry into standardized format."""
    return {
        "id": model.get("id", ""),
        "display_name": model.get("display_name", model.get("id", "")),
        "created_at": model.get("created_at", ""),
        "type": model.get("type", "model"),
    }


async def _fetch_anthropic_models(headers: dict) -> dict[str, Any]:
    """Fetch and parse models from Anthropic API."""
    import aiohttp

    models_url = "https://api.anthropic.com/v1/models"
    timeout = aiohttp.ClientTimeout(total=10)

    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.get(models_url, headers=headers) as response,
    ):
        if response.status != 200:
            error_text = await response.text() if response.status not in (401, 403) else ""
            return _model_discovery_response(
                False, message=_get_http_error_message(response.status, models_url, error_text)
            )

        data = await response.json()
        # Anthropic returns { "data": [...], "has_more": bool, "first_id": str, "last_id": str }
        model_list = [_parse_anthropic_model(m) for m in data.get("data", [])]

        logger.info(f"Anthropic model discovery: Found {len(model_list)} models")
        return _model_discovery_response(True, model_list, f"Found {len(model_list)} models")


@router.get("/anthropic/models")
async def get_anthropic_models(
    api_key: str | None = None,
    config_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get available models from Anthropic API.
    If config_id is provided and no api_key, will use the stored API key from that config.
    """
    import aiohttp

    # Resolve effective API key
    effective_api_key = api_key
    if not effective_api_key and config_id:
        effective_api_key = _get_stored_api_key(db, config_id, current_user.id)

    if not effective_api_key:
        return _model_discovery_response(
            False, message="API key is required to fetch Anthropic models"
        )

    headers = {
        "x-api-key": effective_api_key,
        "anthropic-version": "2023-06-01",
    }

    try:
        return await _fetch_anthropic_models(headers)
    except aiohttp.ClientConnectorError:
        return _model_discovery_response(
            False, message="Connection failed: Could not reach Anthropic API"
        )
    except aiohttp.ClientError as e:
        return _model_discovery_response(False, message=f"Connection error: {str(e)}")
    except TimeoutError:
        return _model_discovery_response(
            False, message="Connection timeout: Anthropic API did not respond within 10 seconds"
        )
    except Exception as e:
        logger.error(f"Error fetching Anthropic models: {e}", exc_info=True)
        return _model_discovery_response(False, message=f"Unexpected error: {str(e)}")


@router.get("/encryption-test")
def test_encryption_endpoint(
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Test the encryption system (for debugging)
    """
    if not test_encryption():
        raise HTTPException(status_code=500, detail="Encryption system is not working properly")

    return {"status": "success", "message": "Encryption system is working correctly"}
