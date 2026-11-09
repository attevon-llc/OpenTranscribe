"""Queries and writes behind the summary-prompt endpoints.

The tenant predicates (``_usable_by``, ``_shared_in_tenant``, ``_in_tenant``) are built by the
endpoint layer from the request context and passed in as SQL criteria: this module never sees
a request. Functions take a ``Session`` and plain arguments and never commit; the caller owns
the transaction.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnExpressionArgument
from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app.models.collection import Collection
from app.utils.pagination import paginate

ACTIVE_PROMPT_SETTING_KEY = "active_summary_prompt_id"


def prompt_matches(db: Session, prompt_id: int, criterion: ColumnExpressionArgument[bool]) -> bool:
    """True when prompt ``prompt_id`` also satisfies ``criterion`` (a tenant predicate)."""
    return (
        db.query(models.SummaryPrompt.id)
        .filter(models.SummaryPrompt.id == prompt_id, criterion)
        .first()
        is not None
    )


def active_prompt_matches_uuid(
    db: Session, prompt_uuid: str, criterion: ColumnExpressionArgument[bool]
) -> bool:
    """True when ``prompt_uuid`` names an active prompt that satisfies ``criterion``."""
    return (
        db.query(models.SummaryPrompt.id)
        .filter(
            models.SummaryPrompt.uuid == prompt_uuid,
            models.SummaryPrompt.is_active.is_(True),
            criterion,
        )
        .first()
        is not None
    )


def count_active_user_prompts(db: Session, user_id: int) -> int:
    """Active, user-owned prompts (clones included): the custom-prompt cap counts these."""
    return (
        db.query(models.SummaryPrompt)
        .filter(
            and_(
                models.SummaryPrompt.user_id == user_id,
                models.SummaryPrompt.is_active,
            )
        )
        .count()
    )


def get_active_prompt_setting(db: Session, user_id: int) -> models.UserSetting | None:
    """The user's stored active-prompt choice (an internal prompt id as text), if any."""
    return (
        db.query(models.UserSetting)
        .filter(
            and_(
                models.UserSetting.user_id == user_id,
                models.UserSetting.setting_key == ACTIVE_PROMPT_SETTING_KEY,
            )
        )
        .first()
    )


def set_active_prompt_setting(db: Session, user_id: int, prompt_id: int) -> None:
    """Store ``prompt_id`` as the user's active prompt (update or insert); does not commit."""
    setting = get_active_prompt_setting(db, user_id)
    if setting:
        setting.setting_value = str(prompt_id)
    else:
        db.add(
            models.UserSetting(
                user_id=user_id,
                setting_key=ACTIVE_PROMPT_SETTING_KEY,
                setting_value=str(prompt_id),
            )
        )


def clear_active_prompt_settings(
    db: Session, prompt_id: int, *, except_user_id: int | None = None
) -> None:
    """Drop every user's active-prompt choice that points at ``prompt_id``; does not commit."""
    query = db.query(models.UserSetting).filter(
        models.UserSetting.setting_key == ACTIVE_PROMPT_SETTING_KEY,
        models.UserSetting.setting_value == str(prompt_id),
    )
    if except_user_id is not None:
        query = query.filter(models.UserSetting.user_id != except_user_id)
    query.delete(synchronize_session=False)


def prompt_by_id_matching(
    db: Session, prompt_id: int, criterion: ColumnExpressionArgument[bool]
) -> models.SummaryPrompt | None:
    """The prompt with ``prompt_id`` when it satisfies ``criterion``, else None."""
    return (
        db.query(models.SummaryPrompt)
        .filter(models.SummaryPrompt.id == prompt_id, criterion)
        .first()
    )


def prompt_by_uuid_or_none(db: Session, prompt_uuid: str) -> models.SummaryPrompt | None:
    """The prompt with this UUID, or None."""
    return db.query(models.SummaryPrompt).filter(models.SummaryPrompt.uuid == prompt_uuid).first()


def default_system_prompt(db: Session) -> models.SummaryPrompt | None:
    """The system prompt used when a user has no usable choice.

    Prefers an active general prompt named universal/general, then any active general system
    prompt, then any active system prompt.
    """
    system_default = models.SummaryPrompt.is_system_default
    tiers = (
        and_(
            system_default,
            models.SummaryPrompt.content_type == "general",
            models.SummaryPrompt.is_active,
            or_(
                models.SummaryPrompt.name.ilike("%universal%"),
                models.SummaryPrompt.name.ilike("%general%"),
            ),
        ),
        and_(
            system_default,
            models.SummaryPrompt.content_type == "general",
            models.SummaryPrompt.is_active,
        ),
        and_(system_default, models.SummaryPrompt.is_active),
    )
    for tier in tiers:
        found = db.query(models.SummaryPrompt).filter(tier).first()
        if found:
            return found
    return None


def list_prompts_page(
    db: Session, conditions: Sequence[ColumnExpressionArgument[bool]], skip: int, limit: int
) -> tuple[list[models.SummaryPrompt], int]:
    """One page of prompts matching all ``conditions``: system prompts first, then by name."""
    query = db.query(models.SummaryPrompt)
    if conditions:
        query = query.filter(and_(*conditions))
    return paginate(
        query.order_by(models.SummaryPrompt.is_system_default.desc(), models.SummaryPrompt.name),
        skip,
        limit,
    )


def prompts_for_content_type(
    db: Session, content_type: str, criterion: ColumnExpressionArgument[bool]
) -> list[models.SummaryPrompt]:
    """Active prompts of ``content_type`` satisfying ``criterion``, ordered by name."""
    return (
        db.query(models.SummaryPrompt)
        .filter(
            and_(
                models.SummaryPrompt.content_type == content_type,
                criterion,
                models.SummaryPrompt.is_active,
            )
        )
        .order_by(models.SummaryPrompt.name)
        .all()
    )


def linked_collection_rows(
    db: Session, prompt_ids: list[int], user_id: int
) -> list[tuple[int | None, Any, str]]:
    """``(prompt_id, collection_uuid, collection_name)`` for the user's collections by prompt."""
    rows = (
        db.query(Collection.default_summary_prompt_id, Collection.uuid, Collection.name)
        .filter(
            Collection.default_summary_prompt_id.in_(prompt_ids),
            Collection.user_id == user_id,
        )
        .order_by(Collection.name)
        .all()
    )
    return [(row[0], row[1], row[2]) for row in rows]


def users_by_id(db: Session, user_ids: set[int]) -> dict[int, models.User]:
    """The users with these ids, keyed by id (attribution lookups)."""
    if not user_ids:
        return {}
    return {u.id: u for u in db.query(models.User).filter(models.User.id.in_(user_ids)).all()}


def shared_library_page(
    db: Session,
    visible: ColumnExpressionArgument[bool],
    *,
    content_type: str | None,
    tags: str | None,
    search: str | None,
    sort_by: str,
    skip: int,
    limit: int,
) -> tuple[list[models.SummaryPrompt], int]:
    """One filtered, sorted page of the shared-prompt library restricted to ``visible``."""
    query = db.query(models.SummaryPrompt).filter(visible, models.SummaryPrompt.is_active.is_(True))
    if content_type:
        query = query.filter(models.SummaryPrompt.content_type == content_type)
    if tags:
        for tag in tags.split(","):
            tag = tag.strip().lower()
            if tag:
                query = query.filter(models.SummaryPrompt.tags.op("@>")(f'["{tag}"]'))
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                models.SummaryPrompt.name.ilike(pattern),
                models.SummaryPrompt.description.ilike(pattern),
            )
        )
    if sort_by == "popular":
        query = query.order_by(models.SummaryPrompt.usage_count.desc())
    elif sort_by == "name":
        query = query.order_by(models.SummaryPrompt.name)
    else:
        query = query.order_by(models.SummaryPrompt.shared_at.desc())
    return paginate(query, skip, limit)


def shared_library_tags(db: Session, visible: ColumnExpressionArgument[bool]) -> list[str]:
    """Distinct tags across the active prompts in ``visible``, sorted."""
    tag_col = func.jsonb_array_elements_text(models.SummaryPrompt.tags).label("tag")
    rows = (
        db.query(tag_col)
        .filter(visible, models.SummaryPrompt.is_active.is_(True))
        .distinct()
        .order_by("tag")
        .all()
    )
    return [row[0] for row in rows]
