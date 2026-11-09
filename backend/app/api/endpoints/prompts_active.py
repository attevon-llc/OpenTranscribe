"""Active summary-prompt selection: read the current choice, set it.

Static ``/active`` paths, included by ``prompts.py`` before its ``/{prompt_uuid}`` routes.
"""

from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import schemas
from app.api.deps_context import get_current_context
from app.api.endpoints.prompts_access import _is_usable
from app.api.endpoints.prompts_access import _usable_by
from app.db.base import get_db
from app.services.request_context import RequestContext
from app.services.summary_prompt_queries import default_system_prompt
from app.services.summary_prompt_queries import get_active_prompt_setting
from app.services.summary_prompt_queries import prompt_by_id_matching
from app.services.summary_prompt_queries import prompt_by_uuid_or_none
from app.services.summary_prompt_queries import set_active_prompt_setting

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
    active_setting = get_active_prompt_setting(db, current_user.id)

    active_prompt = None
    active_prompt_uuid: str | None = None

    if active_setting and active_setting.setting_value:
        try:
            active_prompt_id = int(active_setting.setting_value)
            active_prompt = prompt_by_id_matching(db, active_prompt_id, _usable_by(ctx))
            if active_prompt:
                active_prompt_uuid = str(active_prompt.uuid)
        except (ValueError, TypeError):
            pass

    # No usable choice: fall back to a system default prompt
    if not active_prompt:
        active_prompt = default_system_prompt(db)
        active_prompt_uuid = str(active_prompt.uuid) if active_prompt else None

    return schemas.ActivePromptResponse(
        active_prompt_id=active_prompt_uuid,
        active_prompt=active_prompt,
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
    prompt = prompt_by_uuid_or_none(db, prompt_uuid_str)
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    # Check access: system prompts are public, prompts shared within the caller's tenant
    # are usable, others (incl. another tenant's shared prompts) are private
    if not _is_usable(db, prompt, ctx):
        raise HTTPException(status_code=403, detail="Cannot use other users' private prompts")

    if not prompt.is_active:
        raise HTTPException(status_code=400, detail="Cannot use inactive prompt")

    set_active_prompt_setting(db, current_user.id, prompt.id)

    db.commit()
    db.refresh(prompt)

    return schemas.ActivePromptResponse(active_prompt_id=str(prompt.uuid), active_prompt=prompt)
