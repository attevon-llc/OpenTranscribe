import logging
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Tag
from app.models.media import TranscriptSegment

if TYPE_CHECKING:
    from app.api.deps_context import RequestContext

logger = logging.getLogger(__name__)


def apply_tenant_scope(
    query: Query, model: Any, *, user_id: int, organization_id: OrgScope = UNSCOPED
) -> Query:
    """Default-deny tenant filter for a ``Query``, mirroring ``scope_to_context``.

    This is the SQL-plane choke-point primitive used by the resource lookup
    helpers so org awareness is added once, not per-endpoint.

    * Org context (``organization_id`` is an int): rows whose ``organization_id``
      matches.
    * Personal scope (``organization_id is None``): the user's personal rows
      (``user_id`` match AND ``organization_id IS NULL``).
    * ``UNSCOPED`` sentinel (default): legacy caller, plain ``user_id`` filter —
      preserves pre-cloud behavior.

    Community-edition invariance: ``organization_id`` is always None there, so the
    ``model.organization_id IS NULL`` clause is a behavior-preserving no-op on
    top of plain ``user_id`` filtering.

    Args:
        query: The base SQLAlchemy query to constrain.
        model: The mapped class being filtered (must expose ``user_id`` and
            ``organization_id`` columns).
        user_id: The caller's user id (personal scope).
        organization_id: The active org id, None for personal, or UNSCOPED.

    Returns:
        The query with the tenant-scope filter applied.
    """
    if isinstance(organization_id, _Unscoped):
        return query.filter(model.user_id == user_id)
    if organization_id is not None:
        return query.filter(model.organization_id == organization_id)
    return query.filter(model.user_id == user_id, model.organization_id.is_(None))


def _invalidate_tag_cache_for_file(db: Session, file_id: int) -> None:
    """Bust the owning user's tag/file caches after a tag mutation."""
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_tags_for_file(db, file_id)
    except Exception as e:  # pragma: no cover - cache must never break writes
        logger.debug(f"Tag cache invalidation failed (non-critical): {e}")


def get_user_files_query(
    db: Session, user_id: int, *, organization_id: OrgScope = UNSCOPED
) -> Query:
    """
    Standard query for a user's (or org's) files.

    Args:
        db: Database session
        user_id: User ID (personal scope)
        organization_id: Active org id, None for personal, or UNSCOPED (legacy).
            Defaults to UNSCOPED so community callers are unaffected.

    Returns:
        Base query for the in-scope media files
    """
    return apply_tenant_scope(
        db.query(MediaFile), MediaFile, user_id=user_id, organization_id=organization_id
    )


def get_user_files_query_for_context(db: Session, ctx: "RequestContext") -> Query:
    """``get_user_files_query`` using a :class:`RequestContext` for tenant scope."""
    return get_user_files_query(db, ctx.user.id, organization_id=ctx.org_id)


def get_or_create[T](
    db: Session, model: type[T], defaults: dict | None = None, **kwargs
) -> tuple[T, bool]:
    """
    Get an object or create it if it doesn't exist.

    Args:
        db: Database session
        model: SQLAlchemy model class
        defaults: Default values for creation
        **kwargs: Filter criteria

    Returns:
        Tuple of (object, created_flag)
    """
    try:
        instance = db.query(model).filter_by(**kwargs).first()
        if instance:
            return instance, False
        else:
            params = dict((k, v) for k, v in kwargs.items())
            if defaults:
                params.update(defaults)
            instance = model(**params)
            db.add(instance)
            db.commit()
            db.refresh(instance)
            return instance, True
    except SQLAlchemyError as e:
        logger.error(f"Error in get_or_create for {model.__name__}: {e}")
        db.rollback()
        raise


def safe_get_by_id[T](
    db: Session, model: type[T], obj_id: int, user_id: int | None = None
) -> T | None:
    """
    Safely get an object by ID with optional user filtering.

    Args:
        db: Database session
        model: SQLAlchemy model class
        obj_id: Object ID
        user_id: Optional user ID for ownership filtering

    Returns:
        Object if found, None otherwise
    """
    try:
        query = db.query(model).filter(model.id == obj_id)  # type: ignore[attr-defined]

        # Add user filtering if specified and model has user_id field
        if user_id and hasattr(model, "user_id"):
            query = query.filter(model.user_id == user_id)  # type: ignore[attr-defined]

        return query.first()  # type: ignore[no-any-return]
    except SQLAlchemyError as e:
        logger.error(f"Error getting {model.__name__} by ID {obj_id}: {e}")
        db.rollback()
        return None


def bulk_update[T](db: Session, model: type[T], updates: list[dict], id_field: str = "id") -> bool:
    """
    Perform bulk updates efficiently.

    Args:
        db: Database session
        model: SQLAlchemy model class
        updates: List of update dictionaries with ID and field values
        id_field: Name of the ID field

    Returns:
        True if successful, False otherwise
    """
    try:
        for update_data in updates:
            obj_id = update_data.pop(id_field)
            db.query(model).filter(getattr(model, id_field) == obj_id).update(update_data)

        db.commit()
        return True
    except SQLAlchemyError as e:
        logger.error(f"Error in bulk update for {model.__name__}: {e}")
        db.rollback()
        return False


def get_file_with_transcript_count(
    db: Session, file_id: int, user_id: int
) -> tuple[MediaFile | None, int]:
    """
    Get a file with its transcript segment count.

    Args:
        db: Database session
        file_id: File ID
        user_id: User ID

    Returns:
        Tuple of (MediaFile or None, segment_count)
    """
    file_obj = safe_get_by_id(db, MediaFile, file_id, user_id)
    if not file_obj:
        return None, 0

    segment_count = (
        db.query(func.count(TranscriptSegment.id))
        .filter(TranscriptSegment.media_file_id == file_id)
        .scalar()
    )

    return file_obj, segment_count or 0


def get_user_speakers(db: Session, user_id: int) -> list[Speaker]:
    """
    Get all speakers for a user.

    Args:
        db: Database session
        user_id: User ID

    Returns:
        List of Speaker objects
    """
    return db.query(Speaker).filter(Speaker.user_id == user_id).all()  # type: ignore[no-any-return]


def get_unique_speakers_for_file(db: Session, file_id: int) -> list[Speaker]:
    """
    Get unique speakers that appear in a specific file.

    Args:
        db: Database session
        file_id: File ID

    Returns:
        List of unique Speaker objects
    """
    result = db.query(Speaker).filter(Speaker.media_file_id == file_id).all()
    return result  # type: ignore[no-any-return]


def get_file_tags(db: Session, file_id: int) -> list[str]:
    """
    Get tag names for a file.

    Args:
        db: Database session
        file_id: File ID

    Returns:
        List of tag names
    """
    try:
        tags = db.query(Tag.name).join(FileTag).filter(FileTag.media_file_id == file_id).all()
        return [tag[0] for tag in tags]
    except SQLAlchemyError as e:
        logger.error(f"Error getting tags for file {file_id}: {e}")
        db.rollback()
        return []


def get_files_by_status(db: Session, user_id: int, status: str) -> list[MediaFile]:
    """
    Get files by status for a user.

    Args:
        db: Database session
        user_id: User ID
        status: File status

    Returns:
        List of MediaFile objects
    """
    result = (
        db.query(MediaFile)
        .filter(and_(MediaFile.user_id == user_id, MediaFile.status == status))
        .all()
    )
    return result  # type: ignore[no-any-return]


def get_user_file_stats(db: Session, user_id: int) -> dict:
    """
    Get comprehensive file statistics for a user.

    Args:
        db: Database session
        user_id: User ID

    Returns:
        Dictionary with file statistics
    """
    try:
        # Count files by status
        status_counts = (
            db.query(MediaFile.status, func.count(MediaFile.id))
            .filter(MediaFile.user_id == user_id)
            .group_by(MediaFile.status)
            .all()
        )

        # Total file size
        total_size = (
            db.query(func.sum(MediaFile.file_size)).filter(MediaFile.user_id == user_id).scalar()
            or 0
        )

        # Total duration
        total_duration = (
            db.query(func.sum(MediaFile.duration)).filter(MediaFile.user_id == user_id).scalar()
            or 0
        )

        # File type distribution
        type_counts = (
            db.query(MediaFile.content_type, func.count(MediaFile.id))
            .filter(MediaFile.user_id == user_id)
            .group_by(MediaFile.content_type)
            .all()
        )

        return {
            "total_files": sum(count for _, count in status_counts),
            "status_distribution": {status: count for status, count in status_counts},
            "total_size_bytes": total_size,
            "total_duration_seconds": total_duration,
            "type_distribution": {content_type: count for content_type, count in type_counts},
        }
    except SQLAlchemyError as e:
        logger.error(f"Error getting file stats for user {user_id}: {e}")
        db.rollback()
        return {}
