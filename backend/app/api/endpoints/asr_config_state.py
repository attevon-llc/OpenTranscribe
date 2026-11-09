"""Shared state helpers for the ASR settings endpoints (no router).

Active-configuration pointers, config lookup and serialization, region allowlists and the
cleanup of references to deleted or unshared configurations. Used by ``asr_settings``
(config CRUD), ``asr_provider_catalog`` (status) and ``asr_connection_tests``.
"""

import contextlib
import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.services.asr_config_queries import find_asr_config_by_uuid
from app.services.request_context import RequestContext
from app.services.user_setting_queries import delete_settings_holding_value
from app.services.user_setting_queries import find_user_setting
from app.services.user_setting_queries import list_settings_holding_value
from app.services.user_setting_queries import list_user_settings_with_value
from app.services.user_setting_queries import upsert_user_setting

logger = logging.getLogger(__name__)

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


def _set_active_asr_configuration(db: Session, user_id: int, config_id: int) -> None:
    """Persist the active ASR configuration ID in UserSetting."""
    upsert_user_setting(db, user_id, "active_asr_config_id", config_id)
    db.commit()


def _get_active_asr_config_id(db: Session, user_id: int) -> int | None:
    """Return the stored active ASR config integer ID, or None."""
    setting = find_user_setting(db, user_id, "active_asr_config_id")
    if setting and setting.setting_value:
        with contextlib.suppress(ValueError, TypeError):
            return int(setting.setting_value)
    return None


def _clear_active_asr_setting(db: Session, user_id: int) -> None:
    """Delete the active_asr_config_id UserSetting row if it exists."""
    setting = find_user_setting(db, user_id, "active_asr_config_id")
    if setting:
        db.delete(setting)
        db.commit()


def _config_to_dict(config: Any, *, owner: models.User | None = None, is_own: bool = True) -> dict:
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
    config = find_asr_config_by_uuid(
        db,
        config_uuid,
        user_id,
        shared_org_id=shared_in.org_id if shared_in is not None else None,
        include_shared=shared_in is not None,
    )
    if not config:
        raise HTTPException(status_code=404, detail="ASR configuration not found")
    return config


def _clear_asr_shared_active_references(
    db: Session, config_id: int, *, exclude_user_id: int | None = None
) -> None:
    """Remove UserSetting rows pointing to a deleted/unshared ASR config."""
    delete_settings_holding_value(
        db, "active_asr_config_id", str(config_id), exclude_user_id=exclude_user_id
    )


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
    pointers = list_settings_holding_value(db, DIARIZER_CONFIG_KEY, config_uuids)
    holders = {int(row.user_id) for row in pointers}
    for row in pointers:
        db.delete(row)
    if not holders:
        return False
    sources = list_user_settings_with_value(db, holders, SOURCE_KEY, "asr_config")
    reverted = {int(row.user_id) for row in sources}
    for row in sources:
        db.delete(row)
    if reverted:
        logger.info(
            "Deleted ASR config was a speaker detector: reset %d user(s) to the default",
            len(reverted),
        )
    return user_id in reverted
