"""Active summary-prompt selection: read the current choice, set it.

Static ``/active`` paths, included by ``prompts.py`` before its ``/{prompt_uuid}`` routes.
"""

from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.api.deps_context import get_current_context
from app.api.endpoints.prompts_access import _is_usable
from app.api.endpoints.prompts_access import _usable_by
from app.db.base import get_db
from app.services.request_context import RequestContext

router = APIRouter()


@router.get("/active/current", response_model=schemas.ActivePromptResponse)
def get_active_prompt(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """
    Get the user's currently active summary prompt
    """
    current_user = ctx.user
    # Get user's active prompt setting
    active_setting = (
        db.query(models.UserSetting)
        .filter(
            and_(
                models.UserSetting.user_id == current_user.id,
                models.UserSetting.setting_key == "active_summary_prompt_id",
            )
        )
        .first()
    )

    active_prompt = None
    active_prompt_uuid: str | None = None

    if active_setting and active_setting.setting_value:
        try:
            active_prompt_id = int(active_setting.setting_value)
            active_prompt = (
                db.query(models.SummaryPrompt)
                .filter(models.SummaryPrompt.id == active_prompt_id, _usable_by(ctx))
                .first()
            )
            if active_prompt:
                active_prompt_uuid = str(active_prompt.uuid)
        except (ValueError, TypeError):
            pass

    # If no active prompt or prompt not found, get default system prompt
    if not active_prompt:
        # First try to find a universal/general prompt
        active_prompt = (
            db.query(models.SummaryPrompt)
            .filter(
                and_(
                    models.SummaryPrompt.is_system_default,
                    models.SummaryPrompt.content_type == "general",
                    models.SummaryPrompt.is_active,
                    or_(
                        models.SummaryPrompt.name.ilike("%universal%"),
                        models.SummaryPrompt.name.ilike("%general%"),
                    ),
                )
            )
            .first()
        )

        # If no universal prompt found, fallback to any general system prompt
        if not active_prompt:
            active_prompt = (
                db.query(models.SummaryPrompt)
                .filter(
                    and_(
                        models.SummaryPrompt.is_system_default,
                        models.SummaryPrompt.content_type == "general",
                        models.SummaryPrompt.is_active,
                    )
                )
                .first()
            )

        # Final fallback: any active system prompt
        if not active_prompt:
            active_prompt = (
                db.query(models.SummaryPrompt)
                .filter(
                    and_(
                        models.SummaryPrompt.is_system_default,
                        models.SummaryPrompt.is_active,
                    )
                )
                .first()
            )

        # Get UUID if we found a fallback prompt
        active_prompt_uuid = str(active_prompt.uuid) if active_prompt else None

    return schemas.ActivePromptResponse(
        active_prompt_id=active_prompt_uuid,  # type: ignore[arg-type]
        active_prompt=active_prompt,  # type: ignore[arg-type]  # ORM coerced via from_attributes
    )


@router.post("/active/set", response_model=schemas.ActivePromptResponse)
def set_active_prompt(
    *,
    db: Session = Depends(get_db),
    prompt_selection: schemas.ActivePromptSelection,
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """
    Set the user's active summary prompt
    """
    current_user = ctx.user
    # Convert UUID to string for query
    prompt_uuid_str = str(prompt_selection.prompt_id)

    # Verify prompt exists and user has access - query by UUID not ID
    prompt = (
        db.query(models.SummaryPrompt).filter(models.SummaryPrompt.uuid == prompt_uuid_str).first()
    )
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    # Check access: system prompts are public, prompts shared within the caller's tenant
    # are usable, others (incl. another tenant's shared prompts) are private
    if not _is_usable(db, prompt, ctx):
        raise HTTPException(status_code=403, detail="Cannot use other users' private prompts")

    if not prompt.is_active:
        raise HTTPException(status_code=400, detail="Cannot use inactive prompt")

    # Update or create user setting - store internal ID for efficiency
    setting = (
        db.query(models.UserSetting)
        .filter(
            and_(
                models.UserSetting.user_id == current_user.id,
                models.UserSetting.setting_key == "active_summary_prompt_id",
            )
        )
        .first()
    )

    if setting:
        setting.setting_value = str(prompt.id)  # type: ignore[assignment]
    else:
        setting = models.UserSetting(
            user_id=current_user.id,
            setting_key="active_summary_prompt_id",
            setting_value=str(prompt.id),  # Store internal ID
        )
        db.add(setting)

    db.commit()
    db.refresh(prompt)

    return schemas.ActivePromptResponse(active_prompt_id=str(prompt.uuid), active_prompt=prompt)  # type: ignore[arg-type]
