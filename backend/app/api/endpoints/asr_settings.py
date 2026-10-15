"""
API endpoints for user ASR (Automatic Speech Recognition) settings management.

Follows the same pattern as llm_settings.py for consistency.
"""

import contextlib
import logging
import os
import re
import time
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context

# Deployment configuration is the super_admin tier: this router
# pins the deployment's ASR engine/model.
from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.pipeline_settings import plan_issues_out
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.rate_limit import get_api_rate_limit
from app.auth.rate_limit import limiter
from app.core import local_whisper_catalog
from app.core.constants import WHISPER_WORKER_PREFIXES
from app.db.base import get_db
from app.schemas.asr_settings import ASRModelCapabilities
from app.schemas.pipeline import ASRSwitchResult
from app.services.asr_switch_plan import SwitchOutcome
from app.services.asr_switch_plan import assess_asr_switch
from app.services.local_models import cache as local_model_cache
from app.services.local_models import worker_report
from app.services.pipeline_plan_inputs import plan_for_user
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key
from app.utils.encryption import test_encryption
from app.utils.lite_mode import refuse_on_lite
from app.utils.tenant_sharing import owner_in_tenant
from app.utils.tenant_sharing import shared_visible_in_tenant

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sensitive-data sanitization for error messages
# ---------------------------------------------------------------------------

# Matches Bearer tokens and high-entropy strings with common API-key prefixes,
# as well as generic base64-ish blobs of 32+ chars that could be a raw key.
_KEY_PATTERN = re.compile(
    r"(Bearer\s+)\S+|(sk-|dg_|aai_)\S+",
    re.IGNORECASE,
)


def _sanitize_message(message: str, *secrets: str | None) -> str:
    """Remove known secrets and API-key-shaped tokens from an error message.

    Performs two passes:
    1. Exact replacement of each provided *secret* value.
    2. Regex-based scrubbing of Bearer tokens and common API-key prefixes.
    """
    if not message:
        return message
    result = message
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    result = _KEY_PATTERN.sub(
        lambda m: m.group(1) + "***" if m.group(1) else m.group(2) + "***", result
    )
    return result


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

# Valid ASR providers as accepted by the factory.
_VALID_PROVIDERS = frozenset(
    {
        "local",
        "deepgram",
        "assemblyai",
        "openai",
        "google",
        "azure",
        "aws",
        "speechmatics",
        "gladia",
        "pyannote",
    }
)

# Cloud-based providers (excludes "local").
_CLOUD_PROVIDERS = {
    "deepgram",
    "assemblyai",
    "openai",
    "google",
    "azure",
    "aws",
    "speechmatics",
    "gladia",
    "pyannote",
}

# Azure and AWS region allowlists — imported from schemas (single source of truth).
_VALID_AZURE_REGIONS = schemas.asr_settings.VALID_AZURE_REGIONS
_VALID_AWS_REGIONS = schemas.asr_settings.VALID_AWS_REGIONS


def _validate_region(provider: str | None, region: str | None) -> None:
    """Raise HTTP 400 if *region* is not on the allowlist for providers that require one."""
    if not region:
        return
    if provider == "azure" and region not in _VALID_AZURE_REGIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown Azure region '{region}'",
        )
    if provider == "aws" and region not in _VALID_AWS_REGIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown AWS region '{region}'",
        )


# ---------------------------------------------------------------------------
# Lazy imports to avoid circular dependencies at module load time
# ---------------------------------------------------------------------------


def _get_provider_catalog() -> dict:
    """Return the ASR provider catalog, or empty dict if not yet available."""
    try:
        from app.services.asr.factory import ASR_PROVIDER_CATALOG  # type: ignore[import]

        return ASR_PROVIDER_CATALOG
    except ImportError:
        logger.debug("ASR provider catalog not yet available (services/asr not yet created)")
        return {}


def _get_asr_settings_model():
    """Lazily import the UserASRSettings model."""
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    return UserASRSettings


def _create_asr_provider(
    provider: str,
    api_key: str | None,
    model: str | None,
    base_url: str | None,
    region: str | None,
    access_key_id: str | None = None,
):
    """Create an ASR provider instance via the factory."""
    from app.services.asr.factory import ASRProviderFactory  # type: ignore[import]

    return ASRProviderFactory.create_from_config(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        region=region,
        access_key_id=access_key_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _set_active_asr_configuration(db: Session, user_id: int, config_id: int) -> None:
    """Persist the active ASR configuration ID in UserSetting."""
    existing = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key == "active_asr_config_id",
        )
        .first()
    )
    if existing:
        existing.setting_value = str(config_id)  # type: ignore[assignment]
        db.add(existing)
    else:
        db.add(
            models.UserSetting(
                user_id=user_id,
                setting_key="active_asr_config_id",
                setting_value=str(config_id),
            )
        )
    db.commit()


def _refuse_if_introduced(outcome: SwitchOutcome) -> None:
    """409 ``{code, message}`` for a speaker-detection error the switch would introduce.

    Same shape and rule as ``PUT /user-settings/transcription``: the stable code and its
    fixed sentence, never exception text (``services/asr_switch_plan``).
    """
    if outcome.refusal is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": outcome.refusal.code, "message": outcome.refusal.message},
        )


def _get_active_asr_config_id(db: Session, user_id: int) -> int | None:
    """Return the stored active ASR config integer ID, or None."""
    setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key == "active_asr_config_id",
        )
        .first()
    )
    if setting and setting.setting_value:
        with contextlib.suppress(ValueError, TypeError):
            return int(setting.setting_value)
    return None


def _clear_active_asr_setting(db: Session, user_id: int) -> None:
    """Delete the active_asr_config_id UserSetting row if it exists."""
    setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key == "active_asr_config_id",
        )
        .first()
    )
    if setting:
        db.delete(setting)
        db.commit()


def _config_to_dict(config: Any, *, owner=None, is_own: bool = True) -> dict:
    """Serialize a UserASRSettings record to a safe public dict (no raw API key)."""
    return {
        "id": config.id,
        "uuid": str(config.uuid),
        "user_id": config.user_id,
        "name": config.name,
        "provider": config.provider,
        "model_name": config.model_name,
        "base_url": config.base_url,
        "region": config.region,
        "is_active": config.is_active,
        "has_api_key": bool(config.api_key),
        "has_access_key_id": bool(getattr(config, "access_key_id", None)),
        "last_tested": config.last_tested.isoformat() if config.last_tested else None,
        "test_status": config.test_status,
        "test_message": config.test_message,
        "is_shared": config.is_shared,
        "shared_at": config.shared_at.isoformat() if config.shared_at else None,
        "owner_name": owner.full_name if owner else None,
        "owner_role": owner.role if owner else None,
        "is_own": is_own,
        "created_at": config.created_at.isoformat() if config.created_at else None,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


def _get_config_or_404(
    db: Session, config_uuid: UUID, user_id: int, *, shared_in: RequestContext | None = None
) -> Any:
    """Fetch UserASRSettings by UUID+user or raise 404.

    With ``shared_in``, a config shared within that request's active tenant also
    qualifies; another tenant's shared config answers like a missing one.
    """
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    filters = [UserASRSettings.uuid == config_uuid]
    if shared_in is not None:
        filters.append(
            shared_visible_in_tenant(
                UserASRSettings.user_id, UserASRSettings.is_shared, user_id, shared_in.org_id
            )
        )
    else:
        filters.append(UserASRSettings.user_id == user_id)
    config = db.query(UserASRSettings).filter(*filters).first()
    if not config:
        raise HTTPException(status_code=404, detail="ASR configuration not found")
    return config


def _clear_asr_shared_active_references(
    db: Session, config_id: int, *, exclude_user_id: int | None = None
):
    """Remove UserSetting rows pointing to a deleted/unshared ASR config."""
    q = db.query(models.UserSetting).filter(
        models.UserSetting.setting_key == "active_asr_config_id",
        models.UserSetting.setting_value == str(config_id),
    )
    if exclude_user_id is not None:
        q = q.filter(models.UserSetting.user_id != exclude_user_id)
    q.delete(synchronize_session=False)


def _reset_diarizer_references(db: Session, config_uuids: list[str], user_id: int) -> bool:
    """Reset every ``asr_config`` speaker source that names a deleted configuration (E5).

    The pointer is removed for every user who held it (the owner and anyone it was shared
    with, like the active-config references above), and a source still set to
    ``asr_config`` reverts to the default, which is the transcription provider's own speaker
    detection. Without this a deleted diarizer leaves a blocking ``diarizer_config_missing``
    in each holder's plan. Returns whether the CALLER's source was reverted.
    """
    from app.services.pipeline_plan_inputs import DIARIZER_CONFIG_KEY
    from app.services.pipeline_plan_inputs import SOURCE_KEY

    if not config_uuids:
        return False
    pointers = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.setting_key == DIARIZER_CONFIG_KEY,
            models.UserSetting.setting_value.in_(config_uuids),
        )
        .all()
    )
    holders = {int(row.user_id) for row in pointers}
    for row in pointers:
        db.delete(row)
    if not holders:
        return False
    sources = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id.in_(holders),
            models.UserSetting.setting_key == SOURCE_KEY,
            models.UserSetting.setting_value == "asr_config",
        )
        .all()
    )
    reverted = {int(row.user_id) for row in sources}
    for row in sources:
        db.delete(row)
    if reverted:
        logger.info(
            "Deleted ASR config was a speaker detector: reset %d user(s) to the default",
            len(reverted),
        )
    return user_id in reverted


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/providers")
def get_providers(
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Return the full ASR provider catalog with models, pricing, and capabilities.

    For the local provider, each model entry includes a ``downloaded`` boolean
    indicating whether the model weights are already present on disk.
    """
    catalog = _get_provider_catalog()
    providers = list(catalog.values())

    # Annotate local models with download availability
    try:
        from app.services.asr.model_discovery import get_downloaded_model_names

        downloaded = get_downloaded_model_names()
        for p in providers:
            if p.get("id") == "local":
                for m in p.get("models", []):
                    m["downloaded"] = m["id"] in downloaded
                break
    except Exception:
        logger.debug("Model discovery unavailable, skipping download annotations")

    return {"providers": providers}


@router.get("/local-models")
def get_local_models(
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Return locally downloaded Whisper models discovered from the cache directory."""
    from app.services.asr.model_discovery import discover_local_models

    return {"models": discover_local_models()}


@router.get("/status")
def get_asr_status(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Get ASR configuration status summary for the current user."""
    current_user = ctx.user
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    # Single query: fetch all configs + active_config_id setting in 2 queries total.
    # Resolve active_config from the already-fetched list (no 3rd DB round-trip).
    active_config_id = _get_active_asr_config_id(db, current_user.id)

    configs = db.query(UserASRSettings).filter(UserASRSettings.user_id == current_user.id).all()
    has_settings = len(configs) > 0

    active_config = None
    if active_config_id:
        # Try own configs first, then shared
        active_config = next((c for c in configs if c.id == active_config_id), None)
        if not active_config:
            active_config = (
                db.query(UserASRSettings)
                .filter(
                    UserASRSettings.id == active_config_id,
                    UserASRSettings.is_shared == True,  # noqa: E712
                    owner_in_tenant(UserASRSettings.user_id, ctx.org_id),
                )
                .first()
            )
            if active_config:
                has_settings = True

    deployment_mode = os.getenv("DEPLOYMENT_MODE", "full").lower()
    asr_provider = os.getenv("ASR_PROVIDER", "local").lower()
    using_local_default = active_config is None and asr_provider == "local"

    active_provider: str | None = None
    active_model: str | None = None
    active_config_uuid_str: str | None = None
    is_cloud_provider = False

    if active_config:
        active_provider = str(getattr(active_config, "provider", "") or "")
        active_model = str(getattr(active_config, "model_name", "") or "")
        active_config_uuid_str = str(getattr(active_config, "uuid", "") or "")
        is_cloud_provider = active_provider.lower() in _CLOUD_PROVIDERS
    elif not using_local_default:
        # Env-level provider (not local)
        active_provider = asr_provider
        is_cloud_provider = asr_provider in _CLOUD_PROVIDERS

    from app.services.asr.factory import ASRProviderFactory

    # Inline capability lookup using already-resolved active_config
    # (avoids 2 duplicate DB queries that get_active_model_capabilities would make)
    if active_config:
        provider_id = str(active_config.provider or "local")
        model_id = str(active_config.model_name or "")
        caps = ASRProviderFactory.get_model_capabilities(provider_id, model_id)
        model_capabilities = {"provider": provider_id, "model_id": model_id, **caps}
    elif asr_provider != "local":
        caps = ASRProviderFactory.get_model_capabilities(asr_provider, "")
        model_capabilities = {"provider": asr_provider, "model_id": "", **caps}
    else:
        whisper_model = os.getenv("WHISPER_MODEL", "large-v3-turbo")
        caps = ASRProviderFactory.get_model_capabilities("local", whisper_model)
        model_capabilities = {"provider": "local", "model_id": whisper_model, **caps}

    return {
        "has_settings": has_settings,
        "active_config": _config_to_dict(active_config) if active_config else None,
        "using_local_default": using_local_default,
        "deployment_mode": deployment_mode,
        "asr_configured": bool(active_config) or not using_local_default,
        "active_provider": active_provider,
        "active_model": active_model,
        "active_config_uuid": active_config_uuid_str,
        "is_cloud_provider": is_cloud_provider,
        # Validated through the typed schema so a catalog entry missing a capability
        # field fails here rather than reaching the SPA as an absent key.
        "active_model_capabilities": ASRModelCapabilities.model_validate(
            model_capabilities
        ).model_dump(),
    }


@router.get("")
def list_asr_settings(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """List the current user's ASR configurations and those shared within their tenant."""
    current_user = ctx.user
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    configs = (
        db.query(UserASRSettings)
        .filter(UserASRSettings.user_id == current_user.id)
        .order_by(UserASRSettings.created_at)
        .all()
    )
    active_config_id = _get_active_asr_config_id(db, current_user.id)

    # Resolve active UUID from the already-fetched list — no 3rd DB query needed.
    active_uuid = None
    if active_config_id:
        active = next((c for c in configs if c.id == active_config_id), None)
        if active:
            active_uuid = str(active.uuid)

    # Fetch shared configs from other users in the caller's active tenant
    shared_configs = (
        db.query(UserASRSettings)
        .filter(
            UserASRSettings.is_shared == True,  # noqa: E712
            UserASRSettings.user_id != current_user.id,
            owner_in_tenant(UserASRSettings.user_id, ctx.org_id),
        )
        .order_by(UserASRSettings.created_at)
        .all()
    )
    owner_ids = {c.user_id for c in shared_configs}
    owners = (
        {u.id: u for u in db.query(models.User).filter(models.User.id.in_(owner_ids)).all()}
        if owner_ids
        else {}
    )

    return {
        "configs": [_config_to_dict(c) for c in configs],
        "shared_configs": [
            _config_to_dict(c, owner=owners.get(c.user_id), is_own=False) for c in shared_configs
        ],
        "active_config_id": active_config_id,
        "active_config_uuid": active_uuid,
    }


@router.get("/config/{config_uuid}")
def get_asr_config(
    config_uuid: UUID,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Get a specific ASR configuration by UUID (API key is never returned)."""
    current_user = ctx.user
    config = _get_config_or_404(db, config_uuid, current_user.id, shared_in=ctx)
    return _config_to_dict(config)


@router.get("/config/{config_uuid}/api-key")
def get_asr_config_api_key(
    config_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Return the decrypted API key for the specified configuration. Owner-only."""
    config = _get_config_or_404(db, config_uuid, current_user.id)

    if not config.api_key:
        return {"api_key": None}

    decrypted = decrypt_api_key(str(config.api_key))
    if decrypted is None:
        logger.error(f"Failed to decrypt API key for ASR config {config_uuid}")
        raise HTTPException(status_code=500, detail="Failed to decrypt API key")

    return {"api_key": decrypted}


@router.post("", status_code=201)
def create_asr_config(
    request: Request,
    settings_in: schemas.UserASRSettingsCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Create a new ASR configuration.

    The user's first config is auto-activated unless that would make their stored speaker
    detection choice unrunnable (issue #1225); ``auto_activated`` says which, and
    ``pipeline_issues`` describes the pipeline in effect afterwards. On lite a ``local``
    config is refused (409 ``local_asr_unavailable``, #1228): it could never transcribe.
    """
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    if settings_in.provider.value == "local":
        refuse_on_lite("local_asr_unavailable")

    # Duplicate name check
    existing = (
        db.query(UserASRSettings)
        .filter(
            UserASRSettings.user_id == current_user.id,
            UserASRSettings.name == settings_in.name,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Configuration named '{settings_in.name}' already exists",
        )

    encrypted_key = None
    if settings_in.api_key:
        if not test_encryption():
            raise HTTPException(status_code=500, detail="Encryption system is not working properly")
        encrypted_key = encrypt_api_key(settings_in.api_key)
        if not encrypted_key:
            raise HTTPException(status_code=500, detail="Failed to encrypt API key")

    encrypted_access_key_id = None
    if settings_in.access_key_id:
        if not test_encryption():
            raise HTTPException(status_code=500, detail="Encryption system is not working properly")
        encrypted_access_key_id = encrypt_api_key(settings_in.access_key_id)
        if not encrypted_access_key_id:
            raise HTTPException(status_code=500, detail="Failed to encrypt access key ID")

    # Check before insert whether the user already has any config (determines auto-activate).
    # This avoids a second count query after the INSERT.
    has_existing = (
        db.query(UserASRSettings.id)
        .filter(UserASRSettings.user_id == current_user.id)
        .limit(1)
        .scalar()
        is not None
    )

    is_shared = bool(settings_in.is_shared)
    config = UserASRSettings(
        user_id=current_user.id,
        name=settings_in.name,
        provider=settings_in.provider.value,
        model_name=settings_in.model_name,
        api_key=encrypted_key,
        access_key_id=encrypted_access_key_id,
        base_url=settings_in.base_url,
        region=settings_in.region,
        is_active=settings_in.is_active,
        is_shared=is_shared,
        shared_at=datetime.now(UTC) if is_shared else None,
    )
    db.add(config)
    db.commit()
    db.refresh(config)

    # Auto-activate the user's first config, unless it would break their speaker detection
    # choice: a silent switch into an error combination is what #1225 rules out.
    auto_activated = False
    if not has_existing:
        outcome = assess_asr_switch(db, current_user.id, config, request=request)
        if outcome.refusal is None:
            _set_active_asr_configuration(db, current_user.id, config.id)
            auto_activated = True
        else:
            logger.info(
                "Not auto-activating ASR config %s for user %d: %s",
                config.uuid,
                current_user.id,
                outcome.refusal.code,
            )

    result = _config_to_dict(config)
    result["auto_activated"] = auto_activated
    result["pipeline_issues"] = plan_issues_out(plan_for_user(db, current_user.id, request=request))
    return result


@router.put("/config/{config_uuid}")
def update_asr_config(  # noqa: C901
    config_uuid: UUID,
    settings_in: schemas.UserASRSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Update an existing ASR configuration (on lite, never into a ``local`` one, #1228)."""
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    if settings_in.provider is not None and settings_in.provider.value == "local":
        refuse_on_lite("local_asr_unavailable")

    config = _get_config_or_404(db, config_uuid, current_user.id)

    # Cross-field region validation needs the effective provider (may be from existing config)
    if "region" in settings_in.model_fields_set:
        incoming_provider = settings_in.provider.value if settings_in.provider else config.provider
        _validate_region(incoming_provider, settings_in.region)

    # Name update with duplicate check
    if "name" in settings_in.model_fields_set:
        new_name = settings_in.name
        if new_name != config.name:
            dup = (
                db.query(UserASRSettings)
                .filter(
                    UserASRSettings.user_id == current_user.id,
                    UserASRSettings.name == new_name,
                    UserASRSettings.id != config.id,
                )
                .first()
            )
            if dup:
                raise HTTPException(
                    status_code=409,
                    detail=f"Configuration named '{new_name}' already exists",
                )
        config.name = new_name  # type: ignore[assignment]

    if "provider" in settings_in.model_fields_set:
        new_provider = settings_in.provider.value  # type: ignore[union-attr]
        if new_provider != config.provider:
            # Provider change invalidates the existing API key (it belongs to the old
            # provider's account).  Clear the stored key so the user is prompted to
            # supply a new one rather than inadvertently inheriting a stale credential.
            config.api_key = None  # type: ignore[assignment]
        config.provider = new_provider  # type: ignore[assignment]
        # Reset test status when provider changes
        config.test_status = None  # type: ignore[assignment]
        config.test_message = None  # type: ignore[assignment]
        config.last_tested = None  # type: ignore[assignment]

    if "model_name" in settings_in.model_fields_set:
        config.model_name = settings_in.model_name  # type: ignore[assignment]

    if "api_key" in settings_in.model_fields_set:
        new_key = settings_in.api_key
        if new_key is None or (isinstance(new_key, str) and not new_key.strip()):
            # Empty string or null from the client means "keep the existing key".
            # The frontend sends "" when the user edits other fields without touching
            # the API key field.  Only a deliberate non-empty value replaces the key.
            pass  # Existing key is preserved
        else:
            if not test_encryption():
                raise HTTPException(
                    status_code=500, detail="Encryption system is not working properly"
                )
            encrypted = encrypt_api_key(new_key)
            if not encrypted:
                raise HTTPException(status_code=500, detail="Failed to encrypt API key")
            config.api_key = encrypted  # type: ignore[assignment]
            # Reset test status only when key actually changes
            config.test_status = None  # type: ignore[assignment]

    if "access_key_id" in settings_in.model_fields_set:
        new_akid = settings_in.access_key_id
        if new_akid is None or (isinstance(new_akid, str) and not new_akid.strip()):
            pass  # Empty/null means "keep the existing access key ID" (mirrors api_key)
        else:
            if not test_encryption():
                raise HTTPException(
                    status_code=500, detail="Encryption system is not working properly"
                )
            encrypted_akid = encrypt_api_key(new_akid)
            if not encrypted_akid:
                raise HTTPException(status_code=500, detail="Failed to encrypt access key ID")
            config.access_key_id = encrypted_akid  # type: ignore[assignment]
            config.test_status = None  # type: ignore[assignment]

    if "base_url" in settings_in.model_fields_set:
        config.base_url = settings_in.base_url  # type: ignore[assignment]

    if "region" in settings_in.model_fields_set:
        config.region = settings_in.region  # type: ignore[assignment]

    if "is_active" in settings_in.model_fields_set:
        config.is_active = settings_in.is_active  # type: ignore[assignment]

    # Handle is_shared toggle with shared_at timestamp
    if "is_shared" in settings_in.model_fields_set:
        new_shared = settings_in.is_shared
        if new_shared and not config.is_shared:
            config.is_shared = True  # type: ignore[assignment]
            config.shared_at = datetime.now(UTC)  # type: ignore[assignment]
        elif not new_shared and config.is_shared:
            config.is_shared = False  # type: ignore[assignment]
            config.shared_at = None  # type: ignore[assignment]
            _clear_asr_shared_active_references(db, config.id, exclude_user_id=current_user.id)

    db.add(config)
    db.commit()
    db.refresh(config)

    return _config_to_dict(config)


@router.post("/set-active", response_model=ASRSwitchResult)
def set_active_asr_config(
    request: Request,
    body: schemas.SetActiveASRConfigRequest,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> ASRSwitchResult:
    """Set the active ASR configuration for the current user.

    409 ``{code, message}`` when the switch would make the stored speaker detection choice
    unrunnable (issue #1225); otherwise ``pipeline_issues`` describes the new pipeline.
    """
    current_user = ctx.user
    config_uuid = body.resolved_uuid()
    if not config_uuid:
        raise HTTPException(status_code=400, detail="config_uuid (or uuid) is required")

    config = _get_config_or_404(db, config_uuid, current_user.id, shared_in=ctx)
    outcome = assess_asr_switch(db, current_user.id, config, request=request)
    _refuse_if_introduced(outcome)
    _set_active_asr_configuration(db, current_user.id, config.id)

    return ASRSwitchResult(
        message="Active ASR configuration updated",
        config_uuid=str(config.uuid),
        pipeline_issues=plan_issues_out(outcome.plan),
    )


@router.delete("/config/{config_uuid}", response_model=schemas.ASRConfigDeleted)
def delete_asr_config(
    request: Request,
    config_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> schemas.ASRConfigDeleted:
    """Delete an ASR configuration and report the pipeline it leaves (issue #1236).

    A speaker-detection choice naming this configuration (source ``asr_config``) is reset
    to the default for every user who held it (#1226); ``diarizer_source_reverted`` says
    whether the caller's was.

    If it was active, ``choose_after_active_delete`` picks what becomes active: the first
    remaining config that keeps the stored speaker detection choice runnable (#1225), else
    the cleared pointer. On lite, where clearing means local Whisper, a remaining config
    that can transcribe is kept instead, and with none the delete is refused (409
    ``local_asr_unavailable``, #1228). Otherwise deletion is never refused: a stored choice
    the remaining state cannot run comes back in ``pipeline_issues`` (as for
    ``set-active``), with ``config_uuid`` naming the config now active (``null``: none).
    """
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]
    from app.services.asr_switch_plan import active_delete_refusal
    from app.services.asr_switch_plan import choose_after_active_delete

    config = _get_config_or_404(db, config_uuid, current_user.id)
    active_config_id = _get_active_asr_config_id(db, current_user.id)
    was_active = active_config_id == config.id
    config_id = config.id
    candidates = (
        db.query(UserASRSettings)
        .filter(UserASRSettings.user_id == current_user.id, UserASRSettings.id != config_id)
        .order_by(UserASRSettings.id)
        .all()
        if was_active
        else []
    )
    if was_active:
        refusal = active_delete_refusal(db, current_user.id, candidates, request=request)
        if refusal is not None:
            raise HTTPException(
                status_code=409, detail={"code": refusal.code, "message": refusal.message}
            )

    # Clean up other users who had this shared config active (preserve owner's for auto-promote)
    if config.is_shared:
        _clear_asr_shared_active_references(db, config.id, exclude_user_id=current_user.id)
    reverted = _reset_diarizer_references(db, [str(config.uuid)], current_user.id)

    db.delete(config)
    db.commit()

    promoted = None
    if was_active:
        promoted = choose_after_active_delete(db, current_user.id, candidates, request=request)
        if promoted is not None:
            _set_active_asr_configuration(db, current_user.id, promoted.id)
        else:
            _clear_active_asr_setting(db, current_user.id)
    active_uuid = None
    if promoted is not None:
        active_uuid = str(promoted.uuid)
    elif not was_active and active_config_id is not None:
        still_active = db.query(UserASRSettings.uuid).filter_by(id=active_config_id).scalar()
        active_uuid = str(still_active) if still_active is not None else None
    return schemas.ASRConfigDeleted(
        message="ASR configuration deleted",
        config_uuid=active_uuid,
        diarizer_source_reverted=reverted,
        pipeline_issues=plan_issues_out(plan_for_user(db, current_user.id, request=request)),
    )


@router.post("/clear-active", response_model=ASRSwitchResult)
def clear_active_asr_config(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> ASRSwitchResult:
    """Clear the active ASR configuration, reverting to the deployment default.

    409 ``{code, message}`` when that would make the stored speaker detection choice
    unrunnable (issue #1225); otherwise ``pipeline_issues`` describes the new pipeline.
    """
    outcome = assess_asr_switch(db, current_user.id, None, request=request)
    _refuse_if_introduced(outcome)
    _clear_active_asr_setting(db, current_user.id)
    return ASRSwitchResult(
        message="Reverted to local ASR default",
        pipeline_issues=plan_issues_out(outcome.plan),
    )


@router.delete("/all", response_model=schemas.ASRConfigDeleted)
def delete_all_asr_configs(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> schemas.ASRConfigDeleted:
    """Delete all ASR configurations for the current user (reverts to system default).

    Speaker-detection choices naming any of them are reset like a single delete. On lite,
    where the default is local Whisper, deleting a user's configs is refused (409
    ``local_asr_unavailable``, #1228).
    """
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    owned = db.query(UserASRSettings).filter(UserASRSettings.user_id == current_user.id)
    uuids = [str(row.uuid) for row in owned.with_entities(UserASRSettings.uuid).all()]
    if uuids:
        # Clearing every config clears the active one, and without a config lite would fall
        # back to local Whisper, which it does not ship (#1228). With nothing configured
        # there is nothing to delete and no fallback to introduce.
        refuse_on_lite("local_asr_unavailable")
    reverted = _reset_diarizer_references(db, uuids, current_user.id)
    owned.delete()
    _clear_active_asr_setting(db, current_user.id)
    db.commit()
    return schemas.ASRConfigDeleted(
        message="All ASR configurations deleted",
        config_uuid=None,
        diarizer_source_reverted=reverted,
        pipeline_issues=plan_issues_out(plan_for_user(db, current_user.id, request=request)),
    )


@router.post("/test")
@limiter.limit(get_api_rate_limit())
def test_asr_connection(
    request: Request,
    settings_in: schemas.ASRConnectionTestRequest,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Test an ad-hoc ASR connection without saving configuration."""
    provider_name = settings_in.provider.value
    api_key = settings_in.api_key
    base_url = settings_in.base_url
    region = settings_in.region
    model_name = settings_in.model_name
    access_key_id = getattr(settings_in, "access_key_id", None)

    start_time = time.time()
    try:
        provider = _create_asr_provider(
            provider=provider_name,
            api_key=api_key,
            model=model_name,
            base_url=base_url,
            region=region,
            access_key_id=access_key_id,
        )
        success, message, response_time_ms = provider.validate_connection()
    except Exception as exc:
        response_time_ms = (time.time() - start_time) * 1000
        sanitized = _sanitize_message(str(exc), api_key)
        logger.warning(
            "ASR ad-hoc test failed for provider '%s': %s",
            provider_name,
            sanitized,
        )
        return {
            "success": False,
            "message": sanitized,
            "response_time_ms": int(response_time_ms),
        }

    return {
        "success": success,
        "message": message,
        "response_time_ms": int(response_time_ms),
    }


@router.post("/test-config/{config_uuid}")
@limiter.limit(get_api_rate_limit())
def test_saved_asr_config(
    request: Request,
    config_uuid: UUID,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Test a saved ASR configuration and persist the result (test_status, test_message, last_tested)."""
    current_user = ctx.user
    config = _get_config_or_404(db, config_uuid, current_user.id, shared_in=ctx)

    # Decrypt the stored key only for error sanitization below — provider construction
    # (and its own decryption) goes through the shared factory helper so this path and
    # the transcription-job path cannot drift apart (issue #300).
    api_key = None
    if config.api_key:
        api_key = decrypt_api_key(str(config.api_key))
        if api_key is None:
            logger.error("Failed to decrypt API key for ASR config %s", config_uuid)
            raise HTTPException(status_code=500, detail="Failed to decrypt stored API key")

    start_time = time.time()
    try:
        from app.services.asr.factory import ASRProviderFactory

        provider = ASRProviderFactory.create_from_db_config(config)
        success, message, response_time_ms = provider.validate_connection()
    except Exception as exc:
        success = False
        # Sanitize before storing or returning — provider exceptions may echo back the key
        message = _sanitize_message(str(exc), api_key)
        response_time_ms = (time.time() - start_time) * 1000
        logger.warning("ASR saved-config test failed for config %s: %s", config_uuid, message)

    # Persist test result only if the current user owns the config
    if config.user_id == current_user.id:
        config.test_status = "success" if success else "failed"  # type: ignore[assignment]
        config.test_message = _sanitize_message(message, api_key)  # type: ignore[assignment]
        config.last_tested = datetime.now(UTC)  # type: ignore[assignment]
        db.add(config)
        db.commit()

    return {
        "success": success,
        "message": message,
        "response_time_ms": int(response_time_ms),
        "config_uuid": str(config.uuid),
    }


# ---------------------------------------------------------------------------
# Admin: local ASR model control
# ---------------------------------------------------------------------------


@router.get("/local-model/active")
def get_active_local_model(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Return the active local Whisper model name and available downloaded models."""
    from app.models.system_settings import SystemSettings
    from app.services.asr.model_discovery import discover_local_models

    setting = db.query(SystemSettings).filter(SystemSettings.key == "asr.local_model").first()
    active_model = (
        setting.value if setting and setting.value else os.getenv("WHISPER_MODEL", "large-v3-turbo")
    )

    # Look up catalog info for the active model
    catalog = _get_provider_catalog()
    local_models = catalog.get("local", {}).get("models", [])
    model_info: dict[str, Any] = {}
    for m in local_models:
        if m["id"] == active_model:
            model_info = {
                "display_name": m.get("display_name", active_model),
                "description": m.get("description", ""),
                "supports_translation": m.get("supports_translation", True),
                "supports_diarization": m.get("supports_diarization", True),
                "language_support": m.get("language_support", "multilingual"),
            }
            break
    # English-only ".en" variants (tiny.en, base.en, …) are not in the catalog but are
    # English-only. Resolve them before the substring loop so they are not mis-matched to
    # the multilingual base entry (e.g. "tiny.en" -> "tiny").
    if not model_info and active_model.endswith(".en"):
        model_info = {
            "display_name": active_model,
            "description": "English-only Whisper model",
            "supports_translation": False,
            "supports_diarization": True,
            "language_support": "english_only",
        }
    # Substring match for models like "large-v3-turbo" in custom repo names
    if not model_info:
        sorted_models = sorted(local_models, key=lambda x: len(x["id"]), reverse=True)
        for m in sorted_models:
            if m["id"] in active_model:
                model_info = {
                    "display_name": m.get("display_name", active_model),
                    "description": m.get("description", ""),
                    "supports_translation": m.get("supports_translation", True),
                    "supports_diarization": m.get("supports_diarization", True),
                    "language_support": m.get("language_support", "multilingual"),
                }
                break

    return {
        "active_model": active_model,
        "source": "database" if (setting and setting.value) else "environment",
        "available_models": discover_local_models(),
        "model_info": model_info,
        # What each Whisper worker has actually loaded (empty when Redis is unreachable).
        "loaded_models": worker_report.read_loaded_models(),
    }


class _SetLocalModelRequest(BaseModel):
    model_name: str


@router.post("/local-model/set")
@limiter.limit(get_api_rate_limit())
def set_active_local_model(
    request: Request,
    body: _SetLocalModelRequest,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> Any:
    """Set the active local Whisper model (admin only).

    Stores the model name in SystemSettings so the GPU worker uses it on
    next startup.  Does NOT restart the worker — call ``/local-model/restart``
    separately to apply with graceful drain. Lite has no local Whisper: 409
    ``local_asr_unavailable`` (#1228).
    """
    refuse_on_lite("local_asr_unavailable")
    requested = body.model_name.strip()
    if not requested:
        raise HTTPException(status_code=400, detail="model_name is required")

    # Only catalog models can be selected (#1246). Aliases such as `crisperwhisper` and the
    # PyTorch checkpoint `nyrahealth/CrisperWhisper` (no CT2 model.bin, so faster-whisper cannot
    # load it) resolve to their loadable catalog entry; any other name, including an arbitrary
    # `org/repo`, is refused.
    model_id = local_whisper_catalog.canonical_id(requested)
    entry = local_whisper_catalog.get(model_id) if model_id else None
    if entry is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unknown_local_model",
                "message": "This model is not in the list of supported local models.",
            },
        )
    # The worker loads the model from the cache with no network on restart; a model that is not
    # fully on disk would make it download inside preload or start unloaded.
    if not local_model_cache.is_complete(entry.repo_id):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "local_model_not_downloaded",
                "message": "Download this model before activating it.",
            },
        )
    model_name = entry.id

    from app.models.system_settings import SystemSettings

    setting = db.query(SystemSettings).filter(SystemSettings.key == "asr.local_model").first()
    if setting:
        setting.value = model_name  # type: ignore[assignment]
    else:
        setting = SystemSettings(
            key="asr.local_model",
            value=model_name,
            description="Active local Whisper model set by admin",
        )
        db.add(setting)
    db.commit()

    logger.info("Admin %s set local ASR model to '%s'", current_user.email, model_name)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=request.client.host if request.client else None,
        details={"setting": "local_whisper_model", "action": "set", "model": model_name},
    )

    return {
        "model_name": model_name,
        "message": f"Local model set to '{model_name}'. Restart GPU worker to apply.",
    }


@router.post("/local-model/restart")
@limiter.limit("5/minute")
def restart_gpu_worker(
    request: Request,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> Any:
    """Gracefully restart the Whisper worker(s) to apply a new model (super_admin only).

    Sends a warm shutdown, by name, to the workers in ``WHISPER_WORKER_PREFIXES``.  Each
    finishes the jobs it is running before exiting (the remote control does not arm the
    cooperative abort), Docker ``restart: always`` brings the container back, and the
    ``worker_ready`` hook preloads the stored model and records it
    (``services/local_models/worker_report``).  Clients confirm the switch by waiting for the
    returned ``workers`` to report the new model after ``restart_requested_at`` via
    ``GET /local-model/active``.  Lite runs no GPU worker: 409 ``local_asr_unavailable``
    (#1228).  No worker answering is 409 ``gpu_worker_not_found`` and nothing is signalled;
    it used to broadcast ``shutdown`` to every worker in the deployment.
    """
    refuse_on_lite("local_asr_unavailable")
    from app.core.celery import celery_app
    from app.models.system_settings import SystemSettings

    # Read the pending model so we can report it
    setting = db.query(SystemSettings).filter(SystemSettings.key == "asr.local_model").first()
    pending_model = (
        setting.value if setting and setting.value else os.getenv("WHISPER_MODEL", "large-v3-turbo")
    )

    restart_requested_at = datetime.now(UTC).isoformat()

    # Discover GPU workers
    try:
        inspector = celery_app.control.inspect(timeout=3.0)
        ping_result = inspector.ping() or {}
    except Exception as exc:
        logger.warning("Failed to inspect Celery workers: %s", exc)
        ping_result = {}

    # Only the workers that load the pinned Whisper model. Restarting anything else (the
    # diarization worker, media downloads - which include a model download in flight - yt-dlp,
    # redaction, embeddings) buys nothing and interrupts unrelated work.
    gpu_workers = [name for name in ping_result if name.startswith(WHISPER_WORKER_PREFIXES)]

    if not gpu_workers:
        # No broadcast: with nobody to name, "shutdown" would reach every worker.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "gpu_worker_not_found",
                "message": "No transcription worker answered. It may already be restarting.",
            },
        )

    # Check for active GPU tasks. Best-effort: this only produces an advisory
    # count in the response, so a broker hiccup must not block the restart.
    try:
        active_result = inspector.active() or {}
    except Exception:
        logger.exception("Failed to inspect active GPU tasks; reporting 0")
        active_result = {}

    active_gpu_tasks = 0
    for worker_name in gpu_workers:
        tasks = active_result.get(worker_name, [])
        active_gpu_tasks += len(tasks)

    # Send warm shutdown to GPU workers only
    try:
        celery_app.control.broadcast("shutdown", destination=gpu_workers)
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as exc:
        logger.exception("Failed to send shutdown to GPU workers")
        raise HTTPException(
            status_code=500,
            detail="Failed to signal worker restart.",
        ) from exc

    logger.info(
        "Admin %s triggered GPU worker restart (model=%s, workers=%s, active_tasks=%d)",
        current_user.email,
        pending_model,
        gpu_workers,
        active_gpu_tasks,
    )

    return {
        "status": "restart_signaled",
        "model": pending_model,
        "workers": gpu_workers,
        "active_tasks": active_gpu_tasks,
        "restart_requested_at": restart_requested_at,
        "message": (
            f"GPU worker restart signaled. "
            f"{'Worker will finish ' + str(active_gpu_tasks) + ' active task(s) before restarting.' if active_gpu_tasks > 0 else 'Worker will restart immediately.'} "
            f"New model: {pending_model}"
        ),
    }
