"""
API endpoints for AI summarization prompt management
"""

import contextlib
import logging
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import cast

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from sqlalchemy import and_
from sqlalchemy import not_
from sqlalchemy import or_
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.api.endpoints.auth import get_current_active_user
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.audit import request_org_id
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.utils.pagination import paginate
from app.utils.uuid_helpers import get_prompt_by_uuid

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_USER_PROMPTS = 50


def _assert_under_prompt_limit(db: Session, user_id: int) -> None:
    """Raise HTTP 400 if the user is at or above the custom-prompt cap.

    Counts active, user-owned prompts. Clones count toward this limit too.
    """
    user_prompt_count = (
        db.query(models.SummaryPrompt)
        .filter(
            and_(
                models.SummaryPrompt.user_id == user_id,
                models.SummaryPrompt.is_active,
            )
        )
        .count()
    )
    if user_prompt_count >= MAX_USER_PROMPTS:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum number of custom prompts reached ({MAX_USER_PROMPTS})",
        )


@router.get("", response_model=schemas.SummaryPromptList)
def get_prompts(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    content_type: str | None = Query(None, description="Filter by content type"),
    include_system: bool = Query(True, description="Include system prompts"),
    include_user: bool = Query(True, description="Include user's custom prompts"),
    include_shared: bool = Query(True, description="Include shared prompts from other users"),
) -> Any:
    """
    Retrieve summary prompts with filtering and pagination.

    This endpoint allows users to fetch both system-provided prompts and their own
    custom prompts for AI summarization. Results can be filtered by content type
    and paginated for efficient retrieval.

    Args:
        db: Database session dependency
        current_user: Current authenticated user
        skip: Number of prompts to skip for pagination (default: 0)
        limit: Maximum number of prompts to return (default: 100, max: 1000)
        content_type: Optional filter by content type ('meeting', 'interview',
                     'podcast', 'documentary', 'general')
        include_system: Whether to include system-provided default prompts
        include_user: Whether to include user's custom prompts

    Returns:
        SummaryPromptList: Paginated list of prompts with metadata

    Raises:
        HTTPException: If database query fails
    """
    # Build filter conditions
    conditions = []

    if content_type:
        conditions.append(models.SummaryPrompt.content_type == content_type)

    # Filter by ownership
    ownership_conditions = []
    if include_system:
        ownership_conditions.append(models.SummaryPrompt.is_system_default)  # type: ignore[arg-type]
    if include_user:
        ownership_conditions.append(
            and_(  # type: ignore[arg-type]
                models.SummaryPrompt.user_id == current_user.id,
                not_(models.SummaryPrompt.is_system_default),
            )
        )
    if include_shared:
        ownership_conditions.append(
            and_(  # type: ignore[arg-type]
                models.SummaryPrompt.is_shared == True,  # noqa: E712
                models.SummaryPrompt.user_id != current_user.id,
                not_(models.SummaryPrompt.is_system_default),
            )
        )

    if ownership_conditions:
        conditions.append(or_(*ownership_conditions))

    # Only active prompts
    conditions.append(models.SummaryPrompt.is_active.is_(True))

    # Get prompts
    query = db.query(models.SummaryPrompt)
    if conditions:
        query = query.filter(and_(*conditions))

    prompts, total = paginate(
        query.order_by(
            models.SummaryPrompt.is_system_default.desc(),  # System prompts first
            models.SummaryPrompt.name,
        ),
        skip,
        limit,
    )

    # Get collections that use each prompt as default (for current user only)
    from app.models.media import Collection

    prompt_ids = [p.id for p in prompts]
    collection_map: dict[int, list[schemas.LinkedCollection]] = {}
    if prompt_ids:
        collection_rows = (
            db.query(
                Collection.default_summary_prompt_id,
                Collection.uuid,
                Collection.name,
            )
            .filter(
                Collection.default_summary_prompt_id.in_(prompt_ids),
                Collection.user_id == current_user.id,
            )
            .order_by(Collection.name)
            .all()
        )
        for row in collection_rows:
            pid = row[0]
            if pid not in collection_map:
                collection_map[pid] = []
            collection_map[pid].append(schemas.LinkedCollection(uuid=row[1], name=row[2]))

    # Batch-fetch users referenced for attribution: creators + sharers
    referenced_ids = {p.user_id for p in prompts if p.user_id and p.user_id != current_user.id}
    referenced_ids |= {p.shared_by for p in prompts if p.shared_by}
    users = (
        {u.id: u for u in db.query(models.User).filter(models.User.id.in_(referenced_ids)).all()}
        if referenced_ids
        else {}
    )

    # Build response with linked collections and author info
    prompt_list = []
    for p in prompts:
        pwc = schemas.SummaryPromptWithCollections.model_validate(p)
        pwc.linked_collections = collection_map.get(p.id, [])
        pwc.is_owner = p.user_id == current_user.id
        if p.user_id and p.user_id != current_user.id:
            owner = users.get(p.user_id)
            if owner:
                pwc.author_name = owner.full_name
                pwc.author_role = owner.role
        if p.shared_by:
            sharer = users.get(p.shared_by)
            if sharer:
                pwc.shared_by_name = sharer.full_name
        prompt_list.append(pwc)

    return schemas.SummaryPromptList(
        prompts=prompt_list,
        total=total,
        page=skip // limit + 1 if limit > 0 else 1,
        size=len(prompts),
        has_next=(skip + limit) < total,
        has_prev=skip > 0,
    )


@router.get("/by-content-type/{content_type}", response_model=schemas.ContentTypePromptsResponse)
def get_prompts_by_content_type(
    content_type: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get prompts organized by content type with active prompt indication.

    This endpoint provides a structured view of prompts for a specific content type,
    separating system prompts from user-created prompts, and indicating which
    prompt is currently active for the user.

    Args:
        content_type: The content type to filter by ('meeting', 'interview', etc.)
        db: Database session dependency
        current_user: Current authenticated user

    Returns:
        ContentTypePromptsResponse: Organized prompts with active prompt ID

    Raises:
        HTTPException: If content_type is invalid or database query fails
    """
    # Validate content type
    # "qa_panel" is the community Q&A-panel extractor seeded in initial_data (#136).
    # "speaker_identification" is deliberately ABSENT: it is a system prompt for an
    # internal pipeline step, not a summarization style a user picks per file.
    valid_types = {"meeting", "interview", "podcast", "documentary", "general", "qa_panel"}
    if content_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid content type. Must be one of: {valid_types}",
        )

    # Get system prompts for this content type
    system_prompts = (
        db.query(models.SummaryPrompt)
        .filter(
            and_(
                models.SummaryPrompt.content_type == content_type,
                models.SummaryPrompt.is_system_default,
                models.SummaryPrompt.is_active,
            )
        )
        .order_by(models.SummaryPrompt.name)
        .all()
    )

    # Get user's custom prompts for this content type
    user_prompts = (
        db.query(models.SummaryPrompt)
        .filter(
            and_(
                models.SummaryPrompt.content_type == content_type,
                models.SummaryPrompt.user_id == current_user.id,
                not_(models.SummaryPrompt.is_system_default),
                models.SummaryPrompt.is_active,
            )
        )
        .order_by(models.SummaryPrompt.name)
        .all()
    )

    # Get active prompt ID for this user
    active_prompt_setting = (
        db.query(models.UserSetting)
        .filter(
            and_(
                models.UserSetting.user_id == current_user.id,
                models.UserSetting.setting_key == "active_summary_prompt_id",
            )
        )
        .first()
    )

    active_prompt_uuid = None
    if active_prompt_setting and active_prompt_setting.setting_value:
        with contextlib.suppress(ValueError, TypeError):
            # Get the prompt by internal ID and extract its UUID
            active_prompt_id = int(active_prompt_setting.setting_value)
            active_prompt = (
                db.query(models.SummaryPrompt)
                .filter(models.SummaryPrompt.id == active_prompt_id)
                .first()
            )
            if active_prompt:
                active_prompt_uuid = str(active_prompt.uuid)

    # Get shared prompts for this content type (from other users)
    shared_prompts = (
        db.query(models.SummaryPrompt)
        .filter(
            and_(
                models.SummaryPrompt.content_type == content_type,
                models.SummaryPrompt.is_shared == True,  # noqa: E712
                models.SummaryPrompt.user_id != current_user.id,
                not_(models.SummaryPrompt.is_system_default),
                models.SummaryPrompt.is_active,
            )
        )
        .order_by(models.SummaryPrompt.name)
        .all()
    )

    return schemas.ContentTypePromptsResponse(
        content_type=content_type,
        system_prompts=cast(list[schemas.SummaryPrompt], system_prompts),  # type: ignore[arg-type]
        user_prompts=cast(list[schemas.SummaryPrompt], user_prompts),  # type: ignore[arg-type]
        shared_prompts=cast(list[schemas.SummaryPrompt], shared_prompts),  # type: ignore[arg-type]
        active_prompt_id=active_prompt_uuid,  # type: ignore[arg-type]
    )


@router.post("", response_model=schemas.SummaryPrompt)
def create_prompt(
    *,
    db: Session = Depends(get_db),
    prompt_in: schemas.SummaryPromptCreate,
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Create a new custom summary prompt for the current user.

    Allows users to create personalized prompts for AI summarization. Each user
    is limited to a maximum of 50 custom prompts to prevent abuse.

    Args:
        db: Database session dependency
        prompt_in: The prompt data to create (name, description, prompt_text, etc.)
        current_user: Current authenticated user

    Returns:
        SummaryPrompt: The created prompt object

    Raises:
        HTTPException: If user has reached the prompt limit (50) or creation fails
    """
    # Check if user already has too many prompts
    _assert_under_prompt_limit(db, current_user.id)

    prompt_data = prompt_in.model_dump()
    prompt_data.update({"user_id": current_user.id, "is_system_default": False})

    # Set shared_at timestamp if shared on creation
    if prompt_data.get("is_shared"):
        prompt_data["shared_at"] = datetime.now(UTC)

    prompt = models.SummaryPrompt(**prompt_data)
    db.add(prompt)
    db.commit()
    db.refresh(prompt)
    return prompt


# =============================================================================
# STATIC ROUTES - Must come before parameterized routes
# =============================================================================


@router.get("/active/current", response_model=schemas.ActivePromptResponse)
def get_active_prompt(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get the user's currently active summary prompt
    """
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
                .filter(models.SummaryPrompt.id == active_prompt_id)
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
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Set the user's active summary prompt
    """
    # Convert UUID to string for query
    prompt_uuid_str = str(prompt_selection.prompt_id)

    # Verify prompt exists and user has access - query by UUID not ID
    prompt = (
        db.query(models.SummaryPrompt).filter(models.SummaryPrompt.uuid == prompt_uuid_str).first()
    )
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    # Check access: system prompts are public, shared prompts are usable, others are private
    if not prompt.is_system_default and prompt.user_id != current_user.id and not prompt.is_shared:
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


# =============================================================================
# SHARED PROMPT ROUTES - Must come before parameterized /{prompt_uuid} routes
# =============================================================================


@router.get("/shared/library", response_model=schemas.SharedPromptLibrary)
def get_shared_prompt_library(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
    content_type: str | None = Query(None),
    tags: str | None = Query(None, description="Comma-separated tags"),
    search: str | None = Query(None, description="Search name/description"),
    sort_by: str = Query("newest", description="popular, newest, or name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> Any:
    """Browse shared prompts with filtering and pagination."""
    query = db.query(models.SummaryPrompt).filter(
        models.SummaryPrompt.is_shared == True,  # noqa: E712
        models.SummaryPrompt.is_active == True,  # noqa: E712
        not_(models.SummaryPrompt.is_system_default),
    )

    if content_type:
        query = query.filter(models.SummaryPrompt.content_type == content_type)

    if tags:
        for tag in tags.split(","):
            tag = tag.strip().lower()
            if tag:
                query = query.filter(models.SummaryPrompt.tags.op("@>")(f'["{tag}"]'))

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(
                models.SummaryPrompt.name.ilike(search_pattern),
                models.SummaryPrompt.description.ilike(search_pattern),
            )
        )

    if sort_by == "popular":
        query = query.order_by(models.SummaryPrompt.usage_count.desc())
    elif sort_by == "name":
        query = query.order_by(models.SummaryPrompt.name)
    else:
        query = query.order_by(models.SummaryPrompt.shared_at.desc())

    prompts, total = paginate(query, skip, limit)

    # Batch-fetch users referenced for attribution: creators + sharers
    referenced_ids = {p.user_id for p in prompts if p.user_id}
    referenced_ids |= {p.shared_by for p in prompts if p.shared_by}
    users = (
        {u.id: u for u in db.query(models.User).filter(models.User.id.in_(referenced_ids)).all()}
        if referenced_ids
        else {}
    )

    prompt_list = []
    for p in prompts:
        sp = schemas.SummaryPrompt.model_validate(p)
        sp.is_owner = p.user_id == current_user.id
        owner = users.get(p.user_id) if p.user_id else None
        if owner:
            sp.author_name = owner.full_name
            sp.author_role = owner.role
        if p.shared_by:
            sharer = users.get(p.shared_by)
            if sharer:
                sp.shared_by_name = sharer.full_name
        prompt_list.append(sp)

    # Get available tags
    available_tags: list[str] = []
    try:
        tag_rows = db.execute(
            text(
                "SELECT DISTINCT jsonb_array_elements_text(tags) AS tag "
                "FROM summary_prompt WHERE is_shared = TRUE AND is_active = TRUE "
                "ORDER BY tag"
            )
        ).fetchall()
        available_tags = [row[0] for row in tag_rows]
    except Exception:
        logger.debug("Could not fetch available tags", exc_info=True)

    return schemas.SharedPromptLibrary(
        prompts=prompt_list,
        total=total,
        page=skip // limit + 1 if limit > 0 else 1,
        size=len(prompt_list),
        has_next=(skip + limit) < total,
        has_prev=skip > 0,
        available_tags=available_tags,
    )


@router.post("/shared/{prompt_uuid}/toggle", response_model=schemas.SummaryPrompt)
def share_prompt(
    prompt_uuid: str,
    share_data: schemas.SummaryPromptShare,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Toggle sharing on a prompt. Owner or admin can toggle."""
    prompt = get_prompt_by_uuid(db, prompt_uuid)

    if prompt.user_id != current_user.id and current_user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not authorized to share this prompt")
    if prompt.is_system_default:
        raise HTTPException(status_code=400, detail="Cannot share system prompts")

    prompt.is_shared = share_data.is_shared  # type: ignore[assignment]
    if share_data.is_shared:
        if not prompt.shared_at:
            prompt.shared_at = datetime.now(UTC)  # type: ignore[assignment]
        # Record who flipped sharing on (may differ from the creator)
        prompt.shared_by = current_user.id  # type: ignore[assignment]
    else:
        prompt.shared_at = None  # type: ignore[assignment]
        prompt.shared_by = None  # type: ignore[assignment]
        # Clean up other users who had this prompt set as active
        db.query(models.UserSetting).filter(
            models.UserSetting.setting_key == "active_summary_prompt_id",
            models.UserSetting.setting_value == str(prompt.id),
            models.UserSetting.user_id != current_user.id,
        ).delete(synchronize_session=False)

    db.commit()
    db.refresh(prompt)

    ctx = get_request_context(request)
    audit_logger.log(
        event_type=AuditEventType.PROMPT_SHARE
        if share_data.is_shared
        else AuditEventType.PROMPT_UNSHARE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=ctx["source_ip"],
        user_agent=ctx["user_agent"],
        # Org attribution (issue #262a): resolved by resolve_org_context when
        # the request carried org context; None (personal/community) otherwise.
        organization_id=request_org_id(request),
        details={
            "prompt_uuid": str(prompt.uuid),
            "prompt_name": str(prompt.name),
            "owner_id": int(prompt.user_id) if prompt.user_id else None,
        },
    )
    return prompt


# =============================================================================
# PARAMETERIZED ROUTES - Must come after static routes
# =============================================================================


@router.get("/{prompt_uuid}", response_model=schemas.SummaryPrompt)
def get_prompt(
    prompt_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get a specific prompt by UUID
    """
    prompt = get_prompt_by_uuid(db, prompt_uuid)

    # Check access: system prompts are public, shared are accessible, others are private
    if not prompt.is_system_default and prompt.user_id != current_user.id and not prompt.is_shared:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    return prompt


@router.put("/{prompt_uuid}", response_model=schemas.SummaryPrompt)
def update_prompt(
    *,
    db: Session = Depends(get_db),
    prompt_uuid: str,
    prompt_in: schemas.SummaryPromptUpdate,
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Update a custom summary prompt (user's own prompts only)
    """
    prompt = get_prompt_by_uuid(db, prompt_uuid)

    # Only allow users to update their own custom prompts
    if prompt.is_system_default or prompt.user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Cannot modify system prompts or other users' prompts",
        )

    update_data = prompt_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(prompt, field, value)

    db.add(prompt)
    db.commit()
    db.refresh(prompt)
    return prompt


@router.delete("/{prompt_uuid}")
def delete_prompt(
    prompt_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Delete a custom summary prompt (user's own prompts only)
    """
    prompt = get_prompt_by_uuid(db, prompt_uuid)
    prompt_id = prompt.id

    # Only allow users to delete their own custom prompts
    if prompt.is_system_default or prompt.user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Cannot delete system prompts or other users' prompts",
        )

    # Clean up ALL users who had this prompt set as active (owner + non-owners for shared)
    db.query(models.UserSetting).filter(
        models.UserSetting.setting_key == "active_summary_prompt_id",
        models.UserSetting.setting_value == str(prompt_id),
    ).delete(synchronize_session=False)

    db.delete(prompt)
    db.commit()
    return {"detail": "Prompt deleted successfully"}


@router.post("/{prompt_uuid}/clone", response_model=schemas.SummaryPrompt)
def clone_prompt(
    prompt_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Clone an accessible prompt into the current user's editable library.

    A user may clone a system prompt, a shared prompt, or one of their own
    prompts. The clone is private (not shared), owned by the current user, and
    counts toward the per-user prompt cap.
    """
    source = get_prompt_by_uuid(db, prompt_uuid)

    # Access check mirrors the single-prompt GET: system, shared, or own only.
    if not source.is_system_default and not source.is_shared and source.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Cannot clone other users' private prompts")

    # Clones count toward the per-user cap.
    _assert_under_prompt_limit(db, current_user.id)

    clone = models.SummaryPrompt(
        user_id=current_user.id,
        name=f"{source.name} (copy)",
        description=source.description,
        prompt_text=source.prompt_text,
        content_type=source.content_type,
        tags=list(source.tags or []),
        is_system_default=False,
        is_active=True,
        is_shared=False,
        shared_at=None,
        shared_by=None,
        usage_count=0,
    )
    db.add(clone)
    db.commit()
    db.refresh(clone)

    ctx = get_request_context(request)
    audit_logger.log(
        event_type=AuditEventType.PROMPT_CLONE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=ctx["source_ip"],
        user_agent=ctx["user_agent"],
        organization_id=request_org_id(request),
        details={
            "prompt_uuid": str(clone.uuid),
            "prompt_name": str(clone.name),
            "source_prompt_uuid": str(source.uuid),
            "owner_id": int(source.user_id) if source.user_id else None,
        },
    )

    # The clone is always owned by the caller; reflect that in the response.
    response = schemas.SummaryPrompt.model_validate(clone)
    response.is_owner = True
    return response
