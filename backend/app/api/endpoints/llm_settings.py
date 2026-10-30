"""API endpoints for user LLM settings management"""

import contextlib
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.api.deps_context import get_current_context
from app.api.endpoints import llm_connection_tests
from app.api.endpoints import llm_local_endpoints
from app.api.endpoints import llm_model_discovery
from app.api.endpoints import llm_provider_catalog
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.llm_config_helpers import _AWS_FIELDS
from app.api.endpoints.llm_config_helpers import _apply_credential_update
from app.api.endpoints.llm_config_helpers import _clear_shared_active_references
from app.api.endpoints.llm_config_helpers import _encrypt_bedrock_fields
from app.api.endpoints.llm_config_helpers import _enrich_with_owner
from app.api.endpoints.llm_config_helpers import _get_accessible_config
from app.api.endpoints.llm_config_helpers import _set_active_configuration
from app.api.endpoints.llm_config_helpers import _visible_to
from app.api.endpoints.llm_endpoint_safety import _assert_safe_llm_endpoint
from app.db.base import get_db
from app.services import llm_reasoning
from app.services.request_context import RequestContext
from app.services.uuid_helpers import get_llm_config_by_uuid
from app.services.uuid_helpers import require_resource_owner
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key
from app.utils.encryption import test_encryption
from app.utils.tenant_sharing import owner_in_tenant

_config_router = APIRouter()
_api_key_router = APIRouter()
_encryption_router = APIRouter()


@_config_router.get("", response_model=schemas.UserLLMConfigurationsList)
def get_user_configurations(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """
    Get all user's LLM configurations, plus those shared within the caller's tenant
    """
    current_user = ctx.user
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
                .filter(models.UserLLMSettings.id == active_config_id, _visible_to(ctx))
                .first()
            )
            if active_config:
                active_config_uuid = UUID(str(active_config.uuid))

    # Convert to public schemas
    public_configs: list[schemas.UserLLMSettingsPublic] = []
    for config in configurations:
        # Let FastAPI handle the conversion automatically
        public_configs.append(config)  # type: ignore[arg-type]

    # Fetch shared configs from OTHER users in the caller's active tenant
    shared_configs = (
        db.query(models.UserLLMSettings)
        .filter(
            models.UserLLMSettings.is_shared.is_(True),
            models.UserLLMSettings.user_id != current_user.id,
            owner_in_tenant(models.UserLLMSettings.user_id, ctx.org_id),
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

    # Measured reasoning off-switch per config, so the chat UI can decide whether
    # to offer the control without a request per configuration (issue #64).
    # Only recorded verdicts appear; an absent uuid means "never probed", which
    # the client must treat as "no control".
    reasoning_verdicts: dict[str, llm_reasoning.ReasoningOffSwitch] = {
        str(c.uuid): verdict
        for c in [*configurations, *shared_configs]
        if (
            verdict := llm_reasoning.read(
                db,
                str(c.provider),
                str(c.base_url) if c.base_url else None,
                str(c.model_name),
            )
        )
        is not llm_reasoning.ReasoningOffSwitch.UNKNOWN
    }

    return schemas.UserLLMConfigurationsList(
        configurations=public_configs,
        shared_configurations=public_shared,  # type: ignore[arg-type]
        active_configuration_id=active_config_uuid,
        reasoning_off_switch=reasoning_verdicts,
        total=len(public_configs),
    )


@_config_router.get("/status", response_model=schemas.LLMSettingsStatus)
def get_llm_settings_status(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """
    Get status information about user's LLM settings
    """
    current_user = ctx.user
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
                .filter(models.UserLLMSettings.id == active_config_id, _visible_to(ctx))
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


@_config_router.get("/config/{config_uuid}", response_model=schemas.UserLLMSettingsPublic)
def get_user_configuration(
    config_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """
    Get a specific user's LLM configuration
    """
    user_config = _get_accessible_config(db, config_uuid, ctx)

    # Convert to public schema (excludes API key)
    return user_config


@_config_router.post("", response_model=schemas.UserLLMSettingsPublic)
def create_user_llm_configuration(
    *,
    db: Session = Depends(get_db),
    settings_in: schemas.UserLLMSettingsCreate,
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Create a new LLM configuration for the current user
    """
    # Refuse an unsafe base_url at the point of persistence, not only on the
    # "Test connection" button (issue #820) — otherwise a config saved without
    # ever being tested, or one written directly against this API, bypasses the
    # SSRF guard entirely.
    _assert_safe_llm_endpoint(settings_in.base_url, "LLM configuration create")

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
    if settings_in.provider == schemas.LLMProvider.BEDROCK:
        # Bedrock keeps its region and AWS credentials in one encrypted blob in this column.
        encrypted_api_key = _encrypt_bedrock_fields(settings_in, existing=None)
    elif settings_in.api_key:
        encrypted_api_key = encrypt_api_key(settings_in.api_key)
        if not encrypted_api_key:
            raise HTTPException(status_code=500, detail="Failed to encrypt API key")

    # Create new configuration. `is_active` (issue #607) is never written from client
    # input here: it is an exclusive, derived flag whose SOLE writer is
    # _set_active_configuration below, so it can never disagree with
    # active_llm_config_id. A newly created row therefore always starts inactive, and
    # becomes active only via the auto-activate-the-first-config path a few lines down —
    # exactly matching prior behavior for a first config, and fixing it for every one
    # after (previously left at the column's `True` default, never flipped).
    settings_data = settings_in.model_dump(exclude={"api_key", "is_active", *_AWS_FIELDS})
    settings_data.update(
        {"user_id": current_user.id, "api_key": encrypted_api_key, "is_active": False}
    )

    # Set shared_at timestamp if shared on creation
    if settings_data.get("is_shared"):
        settings_data["shared_at"] = datetime.now(UTC)

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


@_config_router.put("/config/{config_uuid}", response_model=schemas.UserLLMSettingsPublic)
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

    require_resource_owner(
        user_config,
        current_user,
        forbidden_detail="Not authorized to access this configuration",
    )

    config_id = user_config.id

    # Refuse an unsafe base_url at the point of persistence, not only on the
    # "Test connection" button (issue #820). Only when this request actually
    # SETS base_url — `UserLLMSettingsUpdate` is all-Optional and a PUT that
    # doesn't touch base_url (e.g. renaming the config) must not re-validate an
    # already-accepted, unchanged value.
    if "base_url" in settings_in.model_fields_set:
        _assert_safe_llm_endpoint(settings_in.base_url, "LLM configuration update")

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
    update_data = settings_in.model_dump(
        exclude_unset=True, exclude={"api_key", "clear_aws_credentials", *_AWS_FIELDS}
    )

    _apply_credential_update(settings_in, user_config, update_data)

    # Handle is_shared toggle with shared_at timestamp
    if settings_in.is_shared is not None:
        if settings_in.is_shared and not user_config.is_shared:
            update_data["shared_at"] = datetime.now(UTC)
        elif not settings_in.is_shared and user_config.is_shared:
            update_data["shared_at"] = None
            _clear_shared_active_references(
                db,
                user_config.id,
                "active_llm_config_id",
                exclude_user_id=current_user.id,
            )

    # is_active (issue #607) is an exclusive, derived flag — the SOLE writer is
    # _set_active_configuration, never a raw column assignment here, or a PUT could
    # reintroduce the exact bug #607 fixed (two rows both reading `is_active: true`).
    # `True` routes through that helper below; `False` is dropped rather than applied
    # directly — there is no supported "deactivate to nothing" via this endpoint, only
    # activating a DIFFERENT config (via this same field, DELETE's auto-promote, or
    # POST /set-active) ever changes what is active.
    activate_requested = update_data.pop("is_active", None) is True

    # Reset test status when settings change (but not for share-only/activation-only updates)
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

    if activate_requested:
        _set_active_configuration(db, current_user.id, config_id)
        db.refresh(user_config)

    return user_config


@_config_router.post("/set-active", response_model=schemas.UserLLMSettingsPublic)
def set_active_configuration(
    *,
    db: Session = Depends(get_db),
    request: schemas.SetActiveConfigRequest,
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """
    Set the active LLM configuration for the user.

    ``configuration_id`` may name a config owned by someone else, so long as it is
    shared (the `is_shared` check below) -- selecting it here only changes what THIS
    user's turns use (`UserSetting.active_llm_config_id`, see `_set_active_configuration`'s
    own docstring). The shared config's `is_active` column, which the owner's own UI
    reads, is deliberately left untouched: it belongs to the owner's row, not the
    activating user's, and this endpoint's `_set_active_configuration` call is scoped to
    the CALLING user's own configs only (issue #620 item 8d).
    """
    current_user = ctx.user
    # Verify the configuration exists and belongs to the user (or is shared) using UUID
    user_config = _get_accessible_config(db, request.configuration_id, ctx)

    # Set as active using the integer ID (internal)
    _set_active_configuration(db, current_user.id, user_config.id)

    return user_config


@_config_router.delete("/config/{config_uuid}")
def delete_user_llm_configuration(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Delete a specific LLM configuration
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)

    require_resource_owner(
        user_config,
        current_user,
        forbidden_detail="Not authorized to access this configuration",
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


@_config_router.delete("/all")
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


@_api_key_router.get("/config/{config_uuid}/api-key")
def get_config_api_key(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get the decrypted API key for a specific configuration.
    Only the owner can access their own API keys.
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)

    require_resource_owner(
        user_config,
        current_user,
        forbidden_detail="Not authorized to access this configuration",
    )

    # A Bedrock row's column holds its credentials blob, which no endpoint ever reveals.
    if not user_config.api_key or str(user_config.provider) == "bedrock":
        return {"api_key": None}

    # Decrypt API key
    api_key = decrypt_api_key(str(user_config.api_key))
    if not api_key:
        raise HTTPException(status_code=500, detail="Failed to decrypt stored API key")

    return {"api_key": api_key}


@_encryption_router.get("/encryption-test")
def test_encryption_endpoint(
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Test the encryption system (for debugging)
    """
    if not test_encryption():
        raise HTTPException(status_code=500, detail="Encryption system is not working properly")

    return {"status": "success", "message": "Encryption system is working correctly"}


# The composed router. Registration order is the pre-split route table's order exactly
# (route resolution and the OpenAPI document are order-sensitive), which is why the
# api-key and encryption-test routes sit in their own fragments between the sibling
# modules' routers. Routes are spliced rather than ``include_router``-ed because FastAPI
# refuses to nest a router whose path is empty (``POST ""``) under an empty prefix.
router = APIRouter()
for _fragment in (
    llm_local_endpoints.router,
    llm_provider_catalog.router,
    _config_router,
    llm_connection_tests.router,
    _api_key_router,
    llm_model_discovery.router,
    _encryption_router,
):
    router.routes.extend(_fragment.routes)
