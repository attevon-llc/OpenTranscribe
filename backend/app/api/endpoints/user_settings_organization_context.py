"""Organization context settings endpoints (LLM prompt injection)."""

from typing import Any

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.core.constants import DEFAULT_ORG_CONTEXT_INCLUDE_CUSTOM_PROMPTS
from app.core.constants import DEFAULT_ORG_CONTEXT_INCLUDE_DEFAULT_PROMPTS
from app.core.constants import DEFAULT_ORG_CONTEXT_TEXT
from app.db.base import get_db
from app.schemas.organization_context import OrganizationContextSettings
from app.schemas.organization_context import OrganizationContextUpdate
from app.schemas.organization_context import SharedOrganizationContext
from app.schemas.organization_context import SharedOrganizationContextList
from app.services.request_context import RequestContext
from app.services.user_setting_queries import delete_settings_holding_value
from app.services.user_setting_queries import delete_user_settings
from app.services.user_setting_queries import find_shared_org_context_flag
from app.services.user_setting_queries import find_user_setting
from app.services.user_setting_queries import list_shared_org_context_flags
from app.services.user_setting_queries import list_user_settings
from app.services.user_setting_queries import list_user_settings_for_users
from app.services.user_setting_queries import upsert_user_setting
from app.services.user_setting_queries import users_by_id

router = APIRouter()


# Database keys for organization context settings
_ORG_CONTEXT_DB_KEYS = [
    "org_context_text",
    "org_context_include_default_prompts",
    "org_context_include_custom_prompts",
    "org_context_is_shared",
    "org_context_use_shared_from",
]

# Defaults for organization context settings
DEFAULT_ORG_CONTEXT_SETTINGS = {
    "context_text": DEFAULT_ORG_CONTEXT_TEXT,
    "include_in_default_prompts": DEFAULT_ORG_CONTEXT_INCLUDE_DEFAULT_PROMPTS,
    "include_in_custom_prompts": DEFAULT_ORG_CONTEXT_INCLUDE_CUSTOM_PROMPTS,
}


def _build_org_context_response(db: Session, user_id: int) -> OrganizationContextSettings:
    """Build organization context settings response for a user."""
    org_settings = list_user_settings(db, user_id, _ORG_CONTEXT_DB_KEYS)

    settings_map: dict[str, str] = {
        str(setting.setting_key): str(setting.setting_value) for setting in org_settings
    }

    return OrganizationContextSettings(
        context_text=settings_map.get("org_context_text", DEFAULT_ORG_CONTEXT_TEXT),
        include_in_default_prompts=settings_map.get(
            "org_context_include_default_prompts", "true"
        ).lower()
        == "true",
        include_in_custom_prompts=settings_map.get(
            "org_context_include_custom_prompts", "false"
        ).lower()
        == "true",
        is_shared=settings_map.get("org_context_is_shared", "false").lower() == "true",
        using_shared_from=settings_map.get("org_context_use_shared_from"),
    )


@router.get("/organization-context", response_model=OrganizationContextSettings)
def get_organization_context(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> OrganizationContextSettings:
    """
    Get user's organization context settings.

    Returns settings with defaults for any values not customized by the user.

    Returns:
        OrganizationContextSettings containing context_text, include_in_default_prompts,
        and include_in_custom_prompts
    """
    return _build_org_context_response(db, current_user.id)


@router.put("/organization-context", response_model=OrganizationContextSettings)
def update_organization_context(
    *,
    db: Session = Depends(get_db),
    settings_data: OrganizationContextUpdate,
    current_user: models.User = Depends(get_current_active_user),
) -> OrganizationContextSettings:
    """
    Update user's organization context settings.

    Validates input and updates only the provided fields.

    Args:
        settings_data: OrganizationContextUpdate with optional fields

    Returns:
        Updated OrganizationContextSettings with all current values
    """
    update_data = settings_data.model_dump(exclude_none=True)

    if not update_data:
        return _build_org_context_response(db, current_user.id)

    # Map frontend keys to database keys
    setting_mappings = {
        "context_text": "org_context_text",
        "include_in_default_prompts": "org_context_include_default_prompts",
        "include_in_custom_prompts": "org_context_include_custom_prompts",
        "is_shared": "org_context_is_shared",
    }

    for frontend_key, value in update_data.items():
        db_key = setting_mappings.get(frontend_key)
        if db_key:
            upsert_user_setting(db, current_user.id, db_key, value)

    # If unsharing, clean up other users who were using this shared context
    if update_data.get("is_shared") is False:
        delete_settings_holding_value(db, "org_context_use_shared_from", str(current_user.id))

    db.commit()

    return _build_org_context_response(db, current_user.id)


@router.delete("/organization-context")
def reset_organization_context(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Reset user's organization context settings to defaults.

    Deletes all user-specific organization context settings from the database.

    Returns:
        Message confirming reset and the default settings
    """
    deleted_count = delete_user_settings(db, current_user.id, _ORG_CONTEXT_DB_KEYS)

    db.commit()

    return {
        "message": f"Organization context reset to defaults. Removed {deleted_count} custom settings.",
        "default_settings": DEFAULT_ORG_CONTEXT_SETTINGS,
    }


@router.get("/organization-context/shared", response_model=SharedOrganizationContextList)
def get_shared_organization_contexts(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> SharedOrganizationContextList:
    """Get organization contexts shared by other users in the caller's active tenant."""
    current_user = ctx.user
    shared_settings = list_shared_org_context_flags(db, current_user.id, ctx.org_id)

    if not shared_settings:
        return SharedOrganizationContextList(shared_contexts=[])

    # Get the context text for each sharing user
    sharer_ids = [int(s.user_id) for s in shared_settings]
    context_texts = list_user_settings_for_users(db, sharer_ids, "org_context_text")
    text_map = {int(s.user_id): str(s.setting_value) for s in context_texts}

    # Batch-fetch owners for attribution
    owners = users_by_id(db, sharer_ids)

    # Check which shared context the current user is using
    using_setting = find_user_setting(db, current_user.id, "org_context_use_shared_from")
    using_from_id = str(using_setting.setting_value) if using_setting else None

    shared_contexts = []
    for uid in sharer_ids:
        owner = owners.get(uid)
        ctx_text = text_map.get(uid, "")
        if not ctx_text or not owner:
            continue
        shared_contexts.append(
            SharedOrganizationContext(
                user_id=str(uid),
                owner_name=owner.full_name or owner.email,
                owner_role=owner.role or "user",
                context_text=ctx_text,
                is_active=using_from_id == str(uid),
            )
        )

    return SharedOrganizationContextList(shared_contexts=shared_contexts)


@router.post("/organization-context/use-shared")
def use_shared_organization_context(
    *,
    db: Session = Depends(get_db),
    body: dict = Body(...),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Start or stop using another user's shared organization context."""
    current_user = ctx.user
    shared_user_id = body.get("user_id")

    if shared_user_id is None:
        # Stop using shared context — revert to own
        delete_user_settings(db, current_user.id, ["org_context_use_shared_from"])
        db.commit()
        return _build_org_context_response(db, current_user.id)

    # Verify the target user's context is shared within the caller's tenant; another
    # tenant's context answers exactly like one that does not exist.
    is_shared = find_shared_org_context_flag(db, int(shared_user_id), ctx.org_id)
    if not is_shared:
        raise HTTPException(status_code=404, detail="Shared organization context not found")

    upsert_user_setting(db, current_user.id, "org_context_use_shared_from", str(shared_user_id))
    db.commit()

    return _build_org_context_response(db, current_user.id)
