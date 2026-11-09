"""Database reads behind the search reindex admin endpoints."""

from sqlalchemy import exists
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenancy import OrgScope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.services.permission_service import org_scope_pred
from app.utils.db_helpers import owned_in_tenant


def titles_for_uuids(
    db: Session,
    file_uuids: list[str],
    *,
    organization_id: int | None,
    owner_user_id: int | None,
    tenant_scoped: bool,
) -> dict[str, str]:
    """``{uuid: filename}`` for the given files that the caller may see.

    ``tenant_scoped`` False (single tenant) applies no filter beyond the uuid list. When True
    the rows are restricted to ``organization_id``, and further to ``owner_user_id`` when set.
    """
    query = db.query(MediaFile.uuid, MediaFile.filename).filter(MediaFile.uuid.in_(file_uuids))
    if tenant_scoped:
        query = query.filter(org_scope_pred(MediaFile.organization_id, organization_id))
        if owner_user_id is not None:
            query = query.filter(MediaFile.user_id == owner_user_id)
    return {str(row[0]): row[1] for row in query.all()}


def count_indexable_files(db: Session, *, user_id: int, organization_id: OrgScope) -> int:
    """The caller's COMPLETED files in the tenant that have transcript segments."""
    has_segments = exists(
        select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == MediaFile.id)
    )
    return (
        db.query(MediaFile)
        .filter(
            owned_in_tenant(MediaFile, user_id=user_id, organization_id=organization_id),
            MediaFile.status == FileStatus.COMPLETED,
            has_segments,
        )
        .count()
    )
