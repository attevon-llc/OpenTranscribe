"""
API endpoints for user LLM settings management
"""

import contextlib
import logging
import time
import uuid
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.api.endpoints.auth import get_current_active_user
from app.auth.rate_limit import get_llm_outbound_rate_limit
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.db.base import get_db
from app.services import llm_context_window
from app.services import llm_reasoning
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider as ServiceLLMProvider
from app.services.llm_service import LLMService
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key
from app.utils.encryption import test_encryption
from app.utils.url_validation import PinnedTarget
from app.utils.uuid_helpers import get_llm_config_by_uuid
from app.utils.uuid_helpers import require_resource_owner

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
    """Set the active LLM configuration for a user, exclusively.

    ``UserSetting.active_llm_config_id`` is the source of truth (per
    ``UserLLMSettings``'s own docstring), but ``UserLLMSettings.is_active`` is a real,
    queryable column also exposed on the wire (``UserLLMSettingsPublic.is_active``) — so
    this is the ONE place that keeps it in sync. Before issue #607, `is_active` defaulted
    `True` on every row and was never flipped when a different config became active, so
    `GET /api/llm-settings` could report multiple configurations simultaneously as
    `is_active: true` while only one was tracked as real. Every caller that changes the
    active config (creation's first-config auto-activate, this endpoint's own
    `/set-active`, and delete's auto-promote-remaining) funnels through this one function,
    so fixing it here closes every entry point at once.

    Both the bulk deactivate and the target-activate below are scoped to
    ``UserLLMSettings.user_id == user_id`` -- the CALLING user's own rows, never the
    config's actual owner. ``UserSetting.active_llm_config_id`` (the real selector) is
    updated for `user_id` regardless of who owns `config_id`, so activating a config
    someone else shared with you still correctly selects it for your own use -- but the
    owner-scoped queries here find no matching row for it, so their `is_active` column is
    never touched. This is deliberate, not a gap: `is_active` is a per-owner display flag
    ("is this MY config"), and flipping another user's row would misreport which of
    *their* own configs is active in their own UI. Treat `active_llm_config_id` as the
    authority for "what am I using" and `is_active` as "what does the owner see
    highlighted" -- the two questions have different answers for a shared config in use
    by a non-owner (issue #620 item 8d).
    """
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

    # Exclusive toggle: every OTHER config of this user's own is deactivated in the same
    # transaction. `synchronize_session=False` matches this file's existing bulk-update
    # convention (`_clear_shared_active_references`) — cheap here since a user's LLM
    # config count is always small, and `config_id` is excluded so it can never stomp the
    # explicit activation below.
    db.query(models.UserLLMSettings).filter(
        models.UserLLMSettings.user_id == user_id,
        models.UserLLMSettings.id != config_id,
        models.UserLLMSettings.is_active == True,  # noqa: E712
    ).update({"is_active": False}, synchronize_session=False)

    # Set the target row True via the ORM (not the bulk statement above) so an
    # already-loaded instance in THIS session — e.g. the caller's own `user_config` —
    # picks up the change through SQLAlchemy's identity map, rather than returning a
    # stale `is_active` in the response the caller builds right after this call.
    target = (
        db.query(models.UserLLMSettings)
        .filter(
            models.UserLLMSettings.user_id == user_id,
            models.UserLLMSettings.id == config_id,
        )
        .first()
    )
    if target is not None and not target.is_active:
        target.is_active = True
        db.add(target)

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
        schemas.ProviderDefaults(
            provider=schemas.LLMProvider.BEDROCK,
            default_model="anthropic.claude-haiku-4-5-20251001-v1:0",
            default_base_url=None,
            requires_api_key=False,
            supports_custom_url=False,
            max_context_length=None,
            description=(
                "AWS Bedrock — Converse API access to Claude, Nova, Llama and Mistral "
                "models. No API key: credentials resolve via the AWS SDK's standard "
                "chain (IAM role, profile, or environment). The AWS region is set by "
                "your administrator (BEDROCK_REGION/AWS_REGION), not per configuration."
            ),
        ),
    ]


def _assert_safe_llm_endpoint(base_url: str | None, purpose: str) -> None:
    """Refuse a user-supplied LLM endpoint that points at internal infrastructure.

    These endpoints take an arbitrary ``base_url`` from any authenticated user and fetch
    it server-side. With open self-registration that is effectively anonymous reach into
    the deployment's private network and cloud instance metadata (issue #284 A0.1).
    Set ``LLM_ALLOW_PRIVATE_ENDPOINTS=true`` on a single-tenant deployment that genuinely
    runs Ollama/vLLM on a private LAN.

    **This check is validate-only and cannot pin.** It is used where the fetch happens
    somewhere else entirely — ``POST /test-connection`` validates here and connects inside
    ``LLMService.validate_connection``, and a saved ``base_url`` is fetched later by a
    Celery task — so there is no single call frame in which one resolution could serve both
    steps. Where the handler *does* fetch, use :func:`_pin_llm_endpoint` instead; it keeps
    the resolved address and hands it to the client.
    """
    if not base_url:
        return
    from app.core.config import settings
    from app.utils.url_validation import assert_safe_outbound_url

    assert_safe_outbound_url(
        base_url,
        purpose=purpose,
        allow_private=settings.LLM_ALLOW_PRIVATE_ENDPOINTS,
    )


def _pin_llm_endpoint(url: str, purpose: str) -> PinnedTarget:
    """Validate *url* and return the address to dial, for a handler that fetches inline.

    The model-discovery handlers below take ``base_url`` as a query parameter from any
    authenticated user and fetch it in the same function. Validating the hostname and then
    passing the hostname to aiohttp lets it resolve a **second** time, so a host whose DNS
    alternates public/``127.0.0.1`` passes the guard and is connected somewhere else.
    Pinning removes that window; see ``app.utils.url_validation`` for why it does not
    weaken TLS.

    Args:
        url: The user-supplied URL about to be fetched.
        purpose: Short label for the server-side log line.

    Returns:
        The pinned target.

    Raises:
        fastapi.HTTPException: 400, with a generic detail — the rejection reason
            distinguishes "private IP" from "cannot resolve" and would turn the endpoint
            into a network scanner.
    """
    from app.core.config import settings
    from app.utils.url_validation import resolve_pinned_target

    target, reason = resolve_pinned_target(url, allow_private=settings.LLM_ALLOW_PRIVATE_ENDPOINTS)
    if target is None:
        logger.warning("Blocked %s to %r: %s", purpose, url, reason)
        raise HTTPException(
            status_code=400,
            detail=(
                "The provided URL could not be used. It must be a publicly reachable "
                "http(s) address."
            ),
        )
    return target


@router.get("/providers", response_model=schemas.SupportedProvidersResponse)
def get_supported_providers() -> Any:
    """Get list of supported LLM providers with their default configurations.

    This is the canonical provider catalog (the former ``GET /api/llm/providers``
    handler was dead — it called a nonexistent ``LLMService`` method and always
    500'd — and has been removed).

    Deliberate posture: this route declares NO ``current_user`` dependency, so it
    is served WITHOUT authentication. The payload is non-sensitive static metadata
    (provider names, default models, capability flags) with no per-user or secret
    data, so an open catalog is intentional. Pinned by
    ``test_providers_no_auth_dependency`` so the auth posture can't change silently.
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

    if not user_config.is_shared:
        require_resource_owner(
            user_config,
            current_user,
            forbidden_detail="Not authorized to access this configuration",
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

    # Create new configuration. `is_active` (issue #607) is never written from client
    # input here: it is an exclusive, derived flag whose SOLE writer is
    # _set_active_configuration below, so it can never disagree with
    # active_llm_config_id. A newly created row therefore always starts inactive, and
    # becomes active only via the auto-activate-the-first-config path a few lines down —
    # exactly matching prior behavior for a first config, and fixing it for every one
    # after (previously left at the column's `True` default, never flipped).
    settings_data = settings_in.model_dump(exclude={"api_key", "is_active"})
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

    require_resource_owner(
        user_config,
        current_user,
        forbidden_detail="Not authorized to access this configuration",
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


@router.post("/set-active", response_model=schemas.UserLLMSettingsPublic)
def set_active_configuration(
    *,
    db: Session = Depends(get_db),
    request: schemas.SetActiveConfigRequest,
    current_user: models.User = Depends(get_current_active_user),
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
    # Verify the configuration exists and belongs to the user (or is shared) using UUID
    user_config = get_llm_config_by_uuid(db, request.configuration_id)

    if not user_config.is_shared:
        require_resource_owner(
            user_config,
            current_user,
            forbidden_detail="Not authorized to access this configuration",
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
@limiter.limit(get_llm_outbound_rate_limit(), key_func=user_or_ip_key)
def test_llm_connection(
    *,
    request: Request,
    test_request: schemas.ConnectionTestRequest,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Test connection to LLM provider without saving settings.
    If config_id is provided and no api_key, will use the stored API key from that config.

    Rate-limited (issue #676): this handler makes a server-side outbound request to a
    caller-supplied ``base_url`` and sits behind ``get_current_active_user``, not an
    admin gate — see ``get_llm_outbound_rate_limit``.
    """
    _assert_safe_llm_endpoint(test_request.base_url, "LLM test-connection")
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
        logger.exception(f"LLM connection test failed: {e}")

        return schemas.ConnectionTestResponse(
            success=False,
            status=schemas.ConnectionStatus.FAILED,
            message=f"Connection test failed: {str(e)}",
            response_time_ms=response_time,
        )


@router.post("/test-current", response_model=schemas.ConnectionTestResponse)
def test_active_configuration(
    request: Request,
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

    # `db=db` is not optional: `test_llm_connection` declares `db: Session = Depends(get_db)`,
    # so calling it in-process without it binds `db` to the `fastapi.params.Depends` OBJECT.
    # Harmless only while these two callers never set `config_id` on the request they build —
    # the first one that does gets an AttributeError on a Depends instance.
    # `request=request` is required for the same reason (issue #676 made it keyword-only, for
    # the outbound rate limiter's per-user/per-IP key); omitting it is a TypeError at call time,
    # which is what broke both of these endpoints.
    result = test_llm_connection(
        request=request, test_request=test_request, current_user=current_user, db=db
    )

    # Only write back test status if the current user owns the config
    if user_config.user_id == current_user.id:
        user_config.test_status = result.status.value  # type: ignore[assignment]
        user_config.test_message = result.message  # type: ignore[assignment]
        user_config.last_tested = datetime.now(UTC)  # type: ignore[assignment]

        db.add(user_config)
        db.commit()

    return result


@router.post("/test-config/{config_uuid}", response_model=schemas.ConnectionTestResponse)
def test_specific_configuration(
    config_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Test connection for a specific LLM configuration
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)

    if not user_config.is_shared:
        require_resource_owner(
            user_config,
            current_user,
            forbidden_detail="Not authorized to access this configuration",
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

    # `db=db` is not optional: `test_llm_connection` declares `db: Session = Depends(get_db)`,
    # so calling it in-process without it binds `db` to the `fastapi.params.Depends` OBJECT.
    # Harmless only while these two callers never set `config_id` on the request they build —
    # the first one that does gets an AttributeError on a Depends instance.
    # `request=request` is required for the same reason (issue #676 made it keyword-only, for
    # the outbound rate limiter's per-user/per-IP key); omitting it is a TypeError at call time,
    # which is what broke both of these endpoints.
    result = test_llm_connection(
        request=request, test_request=test_request, current_user=current_user, db=db
    )

    # Only write back test status if the current user owns the config
    if user_config.user_id == current_user.id:
        user_config.test_status = result.status.value  # type: ignore[assignment]
        user_config.test_message = result.message  # type: ignore[assignment]
        user_config.last_tested = datetime.now(UTC)  # type: ignore[assignment]

        db.add(user_config)
        db.commit()

    return result


def _config_to_llm_config(db: Session, user_config: models.UserLLMSettings) -> LLMConfig:
    """Build the LLMConfig a stored row describes, decrypting its key."""
    api_key = None
    if user_config.api_key:
        api_key = decrypt_api_key(str(user_config.api_key))
        if not api_key:
            raise HTTPException(status_code=500, detail="Failed to decrypt stored API key")
    return LLMConfig(
        provider=ServiceLLMProvider(user_config.provider),
        model=str(user_config.model_name),
        api_key=api_key,
        base_url=str(user_config.base_url) if user_config.base_url else None,
        max_tokens=int(user_config.max_tokens),
        temperature=float(user_config.temperature),
    )


@router.get("/config/{config_uuid}/reasoning", response_model=schemas.ReasoningCapability)
def get_reasoning_capability(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Read the recorded reasoning off-switch verdict for one configuration.

    Pure read — it never dials the provider. A model that has never been probed
    reports ``unknown``, which is the default everywhere and renders no control.
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)
    if not user_config.is_shared:
        require_resource_owner(
            user_config,
            current_user,
            forbidden_detail="Not authorized to access this configuration",
        )

    provider = str(user_config.provider)
    base_url = str(user_config.base_url) if user_config.base_url else None
    model = str(user_config.model_name)
    return _reasoning_capability_payload(db, provider, base_url, model)


@router.post("/config/{config_uuid}/reasoning-probe", response_model=schemas.ReasoningCapability)
def probe_reasoning_capability(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Measure, and record, whether this model honours a "do not reason" request.

    **Explicitly invoked, never automatic.** Three real generations against the
    configured endpoint is far too much to put in front of a user's first chat
    turn, and a background sweep would dial every configured third-party
    provider unprompted — an egress event, and a bill, that nobody asked for.
    So it sits beside "Test connection", where the cost is visible and the user
    chose to pay it.

    The verdict is keyed to (provider, endpoint, model), so it survives until
    one of those changes and is then simply not found again — there is no stale
    answer to invalidate.
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)
    if not user_config.is_shared:
        require_resource_owner(
            user_config,
            current_user,
            forbidden_detail="Not authorized to access this configuration",
        )

    llm_config = _config_to_llm_config(db, user_config)
    result = llm_reasoning.probe(llm_config)
    llm_reasoning.record(db, llm_config, result)
    return _reasoning_capability_payload(
        db, str(llm_config.provider), llm_config.base_url, llm_config.model
    )


def _reasoning_capability_payload(
    db: Session, provider: str, base_url: str | None, model: str
) -> schemas.ReasoningCapability:
    """Shape a stored verdict (or its absence) into the wire schema."""
    stored = llm_reasoning.read_record(db, provider, base_url, model)
    chars = stored.get("reasoning_chars") or {}
    probed_at = None
    raw_probed_at = stored.get("probed_at")
    if isinstance(raw_probed_at, str):
        with contextlib.suppress(ValueError):
            probed_at = datetime.fromisoformat(raw_probed_at)
    return schemas.ReasoningCapability(
        off_switch=llm_reasoning.verdict_of(stored),
        probeable=ServiceLLMProvider(provider) in llm_reasoning.PROBEABLE_PROVIDERS,
        probed_at=probed_at,
        reasoning_chars_on=int(chars.get("on") or 0),
        reasoning_chars_off=int(chars.get("off") or 0),
        reasoning_chars_omitted=int(chars.get("omitted") or 0),
        detail=str(stored.get("detail") or ""),
    )


@router.get("/config/{config_uuid}/context-window", response_model=schemas.ContextWindowCapability)
def get_context_window(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Read the recorded context-window measurement for one configuration.

    Pure read — it never dials the provider. An unprobed model reports
    ``unknown`` and the user's declared ``max_tokens`` stands.
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)
    if not user_config.is_shared:
        require_resource_owner(
            user_config,
            current_user,
            forbidden_detail="Not authorized to access this configuration",
        )
    return _context_window_payload(db, user_config)


@router.post(
    "/config/{config_uuid}/context-window-probe", response_model=schemas.ContextWindowCapability
)
def probe_context_window(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Discover, and record, the model's maximum context window (issue #533).

    One metadata call (``/v1/models`` or ``/api/show``) — no generation, no user
    content. **Explicitly invoked, never automatic**, for the same reason as the
    reasoning probe beside it: a background sweep would dial every configured
    endpoint unprompted. The verdict is keyed to (provider, endpoint, model);
    editing any of the three orphans it rather than staling it.
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)
    if not user_config.is_shared:
        require_resource_owner(
            user_config,
            current_user,
            forbidden_detail="Not authorized to access this configuration",
        )
    llm_config = _config_to_llm_config(db, user_config)
    result = llm_context_window.probe(llm_config)
    llm_context_window.record(db, llm_config, result)
    return _context_window_payload(db, user_config)


def _context_window_payload(
    db: Session, user_config: models.UserLLMSettings
) -> schemas.ContextWindowCapability:
    """Shape a stored measurement (or its absence) into the wire schema."""
    provider = str(user_config.provider)
    base_url = str(user_config.base_url) if user_config.base_url else None
    model = str(user_config.model_name)
    stored = llm_context_window.read_record(db, provider, base_url, model)
    window = llm_context_window.measured_window(stored)
    configured = int(user_config.max_tokens)
    relation = None
    if window is not None:
        relation = "below" if configured < window else "above" if configured > window else "match"
    probed_at = None
    raw_probed_at = stored.get("probed_at")
    if isinstance(raw_probed_at, str):
        with contextlib.suppress(ValueError):
            probed_at = datetime.fromisoformat(raw_probed_at)
    raw_status = stored.get("status")
    try:
        status = llm_context_window.ContextWindowStatus(str(raw_status))
    except ValueError:
        # Includes a record written by a newer build: an unrecognised status
        # must read as unprobed, not raise the settings page.
        status = llm_context_window.ContextWindowStatus.UNKNOWN
    return schemas.ContextWindowCapability(
        status=status,
        discoverable=(ServiceLLMProvider(provider) in llm_context_window.DISCOVERABLE_PROVIDERS),
        context_window=window,
        configured_max_tokens=configured,
        relation=relation,
        probed_at=probed_at,
        detail=str(stored.get("detail") or ""),
    )


@router.get("/config/{config_uuid}/api-key")
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

    if not user_config.api_key:
        return {"api_key": None}

    # Decrypt API key
    api_key = decrypt_api_key(str(user_config.api_key))
    if not api_key:
        raise HTTPException(status_code=500, detail="Failed to decrypt stored API key")

    return {"api_key": api_key}


@router.get("/ollama/models")
@limiter.limit(get_llm_outbound_rate_limit(), key_func=user_or_ip_key)
async def get_ollama_models(
    request: Request,
    base_url: str,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get available models from an Ollama instance

    Rate-limited (issue #676): fetches a caller-supplied ``base_url`` server-side —
    see ``get_llm_outbound_rate_limit``.
    """
    import aiohttp

    from app.utils.url_validation import pinned_aiohttp_session

    # Clean up base URL
    clean_url = base_url.strip().rstrip("/")
    if clean_url.endswith("/v1"):
        clean_url = clean_url[:-3]  # Remove /v1 suffix

    models_url = f"{clean_url}/api/tags"

    # Validate and PIN the URL actually fetched, not `base_url` — they differ, and a
    # validate-only check would let aiohttp re-resolve at connect time. This stays OUTSIDE
    # the try: the bare `except Exception` below would turn the 400 into a 200 carrying
    # `success: false`, i.e. a refused SSRF reported as a connection problem.
    target = _pin_llm_endpoint(models_url, "Ollama model discovery")

    try:
        async with (
            pinned_aiohttp_session(target, timeout_seconds=10) as session,
            # `original_url`: the resolver does the pinning, so the request keeps its
            # hostname and aiohttp derives SNI + certificate name from it unaided.
            # `allow_redirects=False`: a pin covers one hop.
            session.get(target.original_url, allow_redirects=False) as response,
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
        logger.exception(f"Error fetching Ollama models from {base_url}: {e}")
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
@limiter.limit(get_llm_outbound_rate_limit(), key_func=user_or_ip_key)
async def get_openai_compatible_models(
    request: Request,
    base_url: str,
    api_key: str | None = None,
    config_id: str | None = None,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get available models from an OpenAI-compatible API endpoint.

    Supports: OpenAI, vLLM, OpenRouter, and other OpenAI-compatible providers.
    If config_id is provided and no api_key, will use the stored API key from that config.

    Rate-limited (issue #676): fetches a caller-supplied ``base_url`` server-side —
    see ``get_llm_outbound_rate_limit``.
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

    # Validate and PIN the URL actually fetched (`models_url`, not `base_url`). Outside the
    # try, so the 400 is not swallowed into a `success: false` 200 by the handlers below.
    target = _pin_llm_endpoint(models_url, "OpenAI-compatible model discovery")

    try:
        return await _fetch_and_parse_models(target, headers, base_url)
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


async def _fetch_and_parse_models(
    target: PinnedTarget, headers: dict, base_url: str
) -> dict[str, Any]:
    """Fetch models from a **pinned** target and parse the response.

    Takes a ``PinnedTarget`` rather than a URL string so the address that the SSRF guard
    validated is the address dialled — passing a URL here would let aiohttp resolve the
    hostname a second time, which is the whole DNS-rebinding window.
    """
    from app.utils.url_validation import pinned_aiohttp_session

    models_url = target.original_url
    async with (
        pinned_aiohttp_session(target, timeout_seconds=10) as session,
        # A pin covers one hop; a 302 to an internal address would otherwise be followed
        # with no check at all.
        session.get(models_url, headers=headers, allow_redirects=False) as response,
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
