"""Shared helpers for the LLM configuration endpoints (credentials, visibility, activation)."""

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.services.bedrock_settings import connection_from_encrypted
from app.services.bedrock_settings import merge_stored
from app.services.bedrock_settings import pack_stored
from app.services.request_context import RequestContext
from app.services.uuid_helpers import get_llm_config_by_uuid
from app.utils.encryption import encrypt_api_key
from app.utils.encryption import test_encryption
from app.utils.tenant_sharing import shared_visible_in_tenant

_AWS_FIELDS = (
    "aws_region",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
)


def _encrypt_bedrock_fields(
    settings_in: schemas.UserLLMSettingsCreate | schemas.UserLLMSettingsUpdate,
    existing: str | None,
) -> str | None:
    """The encrypted Bedrock blob after applying the request's aws_* fields to ``existing``.

    Blank credential fields keep what is stored; ``clear_aws_credentials`` drops the stored
    key pair so the configuration falls back to the server's credential chain. Nothing here
    is ever logged or returned.
    """
    fields_set = settings_in.model_fields_set
    current = connection_from_encrypted(existing)
    merged = merge_stored(
        current,
        region=settings_in.aws_region,
        region_given="aws_region" in fields_set,
        access_key_id=settings_in.aws_access_key_id,
        secret_access_key=settings_in.aws_secret_access_key,
        session_token=settings_in.aws_session_token,
        clear_credentials=bool(getattr(settings_in, "clear_aws_credentials", False)),
    )
    if existing and merged == current:
        return existing  # nothing changed: keep the stored ciphertext as it is
    plaintext = pack_stored(merged)
    if plaintext is None:
        return None
    if not test_encryption():
        raise HTTPException(status_code=500, detail="Encryption system is not working properly")
    encrypted = encrypt_api_key(plaintext)
    if not encrypted:
        raise HTTPException(status_code=500, detail="Failed to encrypt AWS credentials")
    return encrypted


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
        "has_api_key": config.has_api_key,
        "aws_region": config.aws_region,
        "has_aws_credentials": config.has_aws_credentials,
        "is_shared": config.is_shared,
        "shared_at": config.shared_at,
        "owner_name": owner.full_name if owner else None,
        "owner_role": owner.role if owner else None,
        "is_own": is_own,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


def _visible_to(ctx: RequestContext) -> Any:
    """Configs the caller may see or use: their own, or shared within their active tenant."""
    return shared_visible_in_tenant(
        models.UserLLMSettings.user_id,
        models.UserLLMSettings.is_shared,
        ctx.user.id,
        ctx.org_id,
    )


def _get_accessible_config(db: Session, config_uuid: UUID | str, ctx: RequestContext) -> Any:
    """Load a config the caller owns or that is shared within the caller's tenant.

    Another tenant's shared config answers exactly like another user's private one.
    """
    user_config = get_llm_config_by_uuid(db, config_uuid)
    if user_config.user_id != ctx.user.id and (
        db.query(models.UserLLMSettings.id)
        .filter(models.UserLLMSettings.id == user_config.id, _visible_to(ctx))
        .first()
        is None
    ):
        raise HTTPException(status_code=403, detail="Not authorized to access this configuration")
    return user_config


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
        models.UserLLMSettings.is_active.is_(True),
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


def _apply_credential_update(
    settings_in: schemas.UserLLMSettingsUpdate,
    user_config: models.UserLLMSettings,
    update_data: dict[str, Any],
) -> None:
    """Put the encrypted ``api_key`` column value for an update into ``update_data``.

    Bedrock keeps a credentials blob in the column; every other provider keeps an API key.
    The two never carry across a provider change, and an empty API key removes the stored one.
    """
    effective_provider = str(update_data.get("provider", user_config.provider))
    if effective_provider != str(user_config.provider) and "bedrock" in (
        effective_provider,
        str(user_config.provider),
    ):
        update_data["api_key"] = None

    if effective_provider == "bedrock":
        touched = settings_in.model_fields_set & {*_AWS_FIELDS, "clear_aws_credentials"}
        if touched or "api_key" in update_data:
            update_data["api_key"] = _encrypt_bedrock_fields(
                settings_in,
                existing=None if "api_key" in update_data else user_config.api_key,
            )
        return

    if "api_key" not in settings_in.model_fields_set or settings_in.api_key is None:
        return
    if not settings_in.api_key.strip():  # Empty API key means remove it
        update_data["api_key"] = None
        return
    if not test_encryption():
        raise HTTPException(status_code=500, detail="Encryption system is not working properly")
    encrypted_api_key = encrypt_api_key(settings_in.api_key)
    if not encrypted_api_key:
        raise HTTPException(status_code=500, detail="Failed to encrypt API key")
    update_data["api_key"] = encrypted_api_key
