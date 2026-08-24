"""
Prompt management utilities for AI summarization
"""

import logging

from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.base import SessionLocal
from app.models import SummaryPrompt
from app.models import UserSetting

# Database-only prompt management - no fallbacks needed

logger = logging.getLogger(__name__)


def get_user_active_prompt(
    user_id: int | None = None,
    db: Session | None = None,
    prompt_uuid: str | None = None,
) -> str:
    """
    Get the active summary prompt for a user, falling back to system default

    Args:
        user_id: User ID to get prompt for (None for system default)
        db: Optional database session (creates new one if not provided)

    Returns:
        The prompt text to use for summarization
    """
    return get_user_active_prompt_info(user_id, db, prompt_uuid=prompt_uuid)[0]


def get_user_active_prompt_info(
    user_id: int | None = None,
    db: Session | None = None,
    prompt_uuid: str | None = None,
) -> tuple[str, bool]:
    """
    Get the active summary prompt and whether it is a system default.

    Args:
        user_id: User ID to get prompt for (None for system default)
        db: Optional database session (creates new one if not provided)

    Returns:
        Tuple of (prompt_text, is_system_default)
    """
    should_close_db = db is None
    if db is None:
        db = SessionLocal()

    try:
        record = _resolve_active_prompt_record(user_id, db, prompt_uuid)
        if record is None:
            raise ValueError("No active system default prompt found in database")
        return str(record.prompt_text), bool(record.is_system_default)

    except Exception as e:
        logger.error(f"Error getting user active prompt for user {user_id}: {e}")
        raise

    finally:
        if should_close_db:
            db.close()


def _resolve_active_prompt_record(
    user_id: int | None,
    db: Session,
    prompt_uuid: str | None = None,
) -> SummaryPrompt | None:
    """Resolve the SummaryPrompt row that would be used for summarization.

    Mirrors the resolution order used by ``get_user_active_prompt_info`` but
    returns the row (so callers can read text, flags, or bump usage_count)
    instead of just the text. Returns None only if no system default exists.

    Args:
        user_id: User ID (None for system default)
        db: Database session
        prompt_uuid: Optional explicit prompt UUID (collection default / manual)

    Returns:
        The resolved SummaryPrompt row, or None if no system default is available.
    """
    # If a specific prompt UUID was provided, use it when valid.
    if prompt_uuid:
        prompt_by_uuid: SummaryPrompt | None = (
            db.query(SummaryPrompt)
            .filter(and_(SummaryPrompt.uuid == prompt_uuid, SummaryPrompt.is_active))
            .first()
        )
        if prompt_by_uuid:
            logger.info(f"Using prompt by UUID: {prompt_by_uuid.name} ({prompt_uuid})")
            return prompt_by_uuid
        logger.warning(f"Prompt UUID {prompt_uuid} not found or inactive, falling back")

    # If no user specified, use the system default.
    if user_id is None:
        return get_system_default_prompt_record(db)

    # Resolve the user's active prompt setting.
    active_setting = (
        db.query(UserSetting)
        .filter(
            and_(
                UserSetting.user_id == user_id,
                UserSetting.setting_key == "active_summary_prompt_id",
            )
        )
        .first()
    )

    active_prompt: SummaryPrompt | None = None
    if active_setting and active_setting.setting_value:
        try:
            prompt_id = int(active_setting.setting_value)
            active_prompt = (
                db.query(SummaryPrompt)
                .filter(and_(SummaryPrompt.id == prompt_id, SummaryPrompt.is_active))
                .first()
            )
        except (ValueError, TypeError):
            logger.warning(f"Invalid prompt ID in user setting: {active_setting.setting_value}")

    if not active_prompt:
        return get_system_default_prompt_record(db)

    # Verify the user has access (own, system, or shared); else fall back.
    if (
        not active_prompt.is_system_default
        and active_prompt.user_id != user_id
        and not active_prompt.is_shared
    ):
        logger.warning(f"User {user_id} attempted to use inaccessible prompt {active_prompt.id}")
        return get_system_default_prompt_record(db)

    return active_prompt


def resolve_active_prompt_record(
    user_id: int | None = None,
    db: Session | None = None,
    prompt_uuid: str | None = None,
) -> SummaryPrompt | None:
    """Public wrapper around :func:`_resolve_active_prompt_record` with db lifecycle."""
    should_close_db = db is None
    if db is None:
        db = SessionLocal()
    try:
        return _resolve_active_prompt_record(user_id, db, prompt_uuid)
    finally:
        if should_close_db:
            db.close()


def increment_prompt_usage(db: Session, prompt_id_or_uuid) -> None:
    """Increment ``usage_count`` by one for the given prompt (best-effort).

    Called once per successful summarization for the prompt actually used, so
    the shared library's ``usage_count DESC`` ordering and the "most used
    prompts" metric reflect real usage. Never raises — a counter bump must not
    fail the summarization task.

    Args:
        db: Database session
        prompt_id_or_uuid: Internal integer id or UUID/str of the prompt
    """
    try:
        query = db.query(SummaryPrompt)
        if isinstance(prompt_id_or_uuid, int):
            query = query.filter(SummaryPrompt.id == prompt_id_or_uuid)
        else:
            query = query.filter(SummaryPrompt.uuid == str(prompt_id_or_uuid))
        prompt = query.first()
        if prompt is None:
            return
        prompt.usage_count = (prompt.usage_count or 0) + 1
        db.commit()
    except Exception as e:
        logger.warning(f"Failed to increment usage_count for prompt {prompt_id_or_uuid}: {e}")
        db.rollback()


def get_system_default_prompt_record(db: Session) -> SummaryPrompt | None:
    """
    Get the system default prompt row from database with intelligent fallback.

    Args:
        db: Database session

    Returns:
        The best-matching system default SummaryPrompt row, or None if no
        active system default prompt exists at all.
    """
    try:
        # First try to find a universal/general prompt
        logger.info("Querying for universal/general system prompt")
        default_prompt: SummaryPrompt | None = (
            db.query(SummaryPrompt)
            .filter(
                and_(
                    SummaryPrompt.is_system_default,
                    SummaryPrompt.content_type == "general",
                    SummaryPrompt.is_active,
                    or_(
                        SummaryPrompt.name.ilike("%universal%"),
                        SummaryPrompt.name.ilike("%general%"),
                    ),
                )
            )
            .first()
        )

        if default_prompt:
            logger.info(f"Found universal system prompt: {default_prompt.name}")
            return default_prompt

        # If no universal prompt found, fallback to any general system prompt
        logger.info("No universal prompt found, trying any general system prompt")
        default_prompt = (
            db.query(SummaryPrompt)
            .filter(
                and_(
                    SummaryPrompt.is_system_default,
                    SummaryPrompt.content_type == "general",
                    SummaryPrompt.is_active,
                )
            )
            .first()
        )

        if default_prompt:
            logger.info(f"Found general system prompt: {default_prompt.name}")
            return default_prompt

        # Final fallback: any active system prompt
        logger.warning("No general system prompt found, using any available system prompt")
        any_system_prompt: SummaryPrompt | None = (
            db.query(SummaryPrompt)
            .filter(and_(SummaryPrompt.is_system_default, SummaryPrompt.is_active))
            .first()
        )

        if any_system_prompt:
            logger.warning(
                f"Using fallback system prompt: {any_system_prompt.name} (type: {any_system_prompt.content_type})"
            )
            return any_system_prompt

        logger.error("No active system default prompts found in database at all!")
        return None

    except Exception as e:
        logger.error(f"Error getting system default prompt: {e}")
        raise


def get_system_default_prompt(db: Session) -> str:
    """
    Get the system default prompt text from database with intelligent fallback.

    Args:
        db: Database session

    Returns:
        System default prompt text

    Raises:
        ValueError: If no active system default prompt exists.
    """
    record = get_system_default_prompt_record(db)
    if record is None:
        raise ValueError("No active system default prompt found in database")
    return str(record.prompt_text)
