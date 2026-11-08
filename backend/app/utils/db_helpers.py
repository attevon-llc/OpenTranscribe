import logging
from typing import Any

from sqlalchemy import and_
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker

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


def org_stamp_is(org_col: Any, organization_id: int | None) -> ColumnElement[bool]:
    """NULL-safe predicate: a row's ``organization_id`` stamp names the active tenant.

    An org id matches that org's rows only; ``None`` (personal scope) matches org-less
    rows only. Community-edition invariance: every row is org-less and every caller is
    in personal scope, so this is always true there.
    """
    if organization_id is None:
        return org_col.is_(None)  # type: ignore[no-any-return]
    return org_col == organization_id  # type: ignore[no-any-return]


def owned_in_tenant(
    model: Any, *, user_id: int, organization_id: OrgScope = UNSCOPED
) -> ColumnElement[bool]:
    """Owner-listing predicate: the caller's OWN rows, restricted to the active tenant.

    Unlike ``apply_tenant_scope`` this never widens an org-context listing to other
    members' rows — it keeps ``user_id == caller`` and only adds the tenant stamp.
    ``UNSCOPED`` (no context threaded) is plain ``user_id`` filtering.
    """
    owner: ColumnElement[bool] = model.user_id == user_id
    if isinstance(organization_id, _Unscoped):
        return owner
    return and_(owner, org_stamp_is(model.organization_id, organization_id))


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

        return query.first()
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


def file_has_transcript(db: Session, file_id: int) -> bool:
    """Whether the file has at least one transcript segment, as one ``EXISTS``.

    Not ``bool(media_file.transcript_segments)``: that lazy-loads every column of every
    segment to answer a yes/no question (concurrency audit L7).
    """
    return bool(db.query(exists().where(TranscriptSegment.media_file_id == file_id)).scalar())


def get_user_speakers(db: Session, user_id: int) -> list[Speaker]:
    """
    Get all speakers for a user.

    Args:
        db: Database session
        user_id: User ID

    Returns:
        List of Speaker objects
    """
    return db.query(Speaker).filter(Speaker.user_id == user_id).all()


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
    return result
