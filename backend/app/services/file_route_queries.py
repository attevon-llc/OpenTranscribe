"""Media-file row lookups and writes behind the ``/api/files`` routes.

Plain ``Session`` helpers taking plain arguments. None of them commits: the route
that called them keeps its own transaction boundary exactly where it was.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import urllib3
from minio.error import S3Error
from sqlalchemy import func
from sqlalchemy import text
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import selectinload

from app.core import redaction_defaults
from app.core.tenancy import OrgScope
from app.models.media import Analytics
from app.models.media import Comment
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.tag import FileTag
from app.models.tag import Tag
from app.models.topic import TopicSuggestion
from app.services.file_availability import available_pred
from app.services.permission_service import PermissionService
from app.services.tag_scope import tenant_bound_or_system
from app.utils.db_helpers import org_stamp_is
from app.utils.db_helpers import owned_in_tenant

_logger = logging.getLogger(__name__)


def get_file_by_id(db: Session, file_id: int) -> MediaFile | None:
    """One media file by primary key, or None. No tenant or ownership gate."""
    return db.query(MediaFile).filter(MediaFile.id == file_id).first()


def get_file_by_public_uuid(db: Session, file_uuid: Any) -> MediaFile | None:
    """One media file by its public UUID, or None. No tenant or ownership gate."""
    return db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()


def get_owned_file_by_uuid(db: Session, file_uuid: Any, user_id: int) -> MediaFile | None:
    """The file with this public UUID when ``user_id`` owns it, else None."""
    return (
        db.query(MediaFile)
        .filter(MediaFile.uuid == file_uuid, MediaFile.user_id == user_id)
        .first()
    )


def get_owned_file_in_tenant(
    db: Session, file_id: int, *, user_id: int, organization_id: OrgScope
) -> MediaFile | None:
    """The file with this primary key when ``user_id`` owns it inside the tenant, else None."""
    return (
        db.query(MediaFile)
        .filter(MediaFile.id == file_id)
        .filter(owned_in_tenant(MediaFile, user_id=user_id, organization_id=organization_id))
        .first()
    )


def get_pending_upload(
    db: Session, file_uuid: Any, *, user_id: int, organization_id: int | None
) -> MediaFile | None:
    """The caller's still-PENDING upload in the tenant (org scope, or the org-less personal scope)."""
    org_pred = (
        MediaFile.organization_id == organization_id
        if organization_id is not None
        else MediaFile.organization_id.is_(None)
    )
    return (
        db.query(MediaFile)
        .filter(
            MediaFile.uuid == file_uuid,
            MediaFile.user_id == user_id,
            MediaFile.status == FileStatus.PENDING,
            org_pred,
        )
        .first()
    )


def is_unfinished_upload(db: Session, db_file: MediaFile) -> bool:
    """True only when no finished upload or processing exists for this PENDING row.

    A prepared row (or an in-progress multipart upload) has no storage object yet;
    ``/complete`` and the legacy upload route both run only after the
    bytes landed. Any doubt (storage error, history present) reads as finished, so
    the file is trashed rather than destroyed.
    """
    from app.services.minio_service import object_exists_and_size

    if db_file.thumbnail_path or db_file.playback_path or db_file.completed_at:
        return False
    has_segments = (
        db.query(TranscriptSegment.id).filter(TranscriptSegment.media_file_id == db_file.id).first()
    )
    if has_segments is not None:
        return False
    if not db_file.storage_path:
        return True
    try:
        return object_exists_and_size(str(db_file.storage_path)) is None
    except (S3Error, urllib3.exceptions.HTTPError, OSError):
        _logger.warning("Could not confirm upload state for %s; trashing", db_file.uuid)
        return False


def withdraw_pending_redaction(db: Session, file_id: int, previous_status: str | None) -> int:
    """Restore a file's redaction status when it is still ``pending``; returns rows changed.

    One conditional UPDATE, so a scan that already moved the status on is left alone.
    The caller commits.
    """
    return (
        db.query(MediaFile)
        .filter(
            MediaFile.id == file_id,
            MediaFile.redaction_status == redaction_defaults.REDACTION_STATUS_PENDING,
        )
        .update({MediaFile.redaction_status: previous_status}, synchronize_session=False)
    )


def get_segment_with_speaker(
    db: Session, segment_uuid: Any, media_file_id: int
) -> TranscriptSegment | None:
    """One transcript segment of a file by public UUID, with its speaker eagerly loaded."""
    return (
        db.query(TranscriptSegment)
        .options(joinedload(TranscriptSegment.speaker))
        .filter(
            TranscriptSegment.uuid == segment_uuid,
            TranscriptSegment.media_file_id == media_file_id,
        )
        .first()
    )


def waveform_candidates(db: Session, *, force_regenerate: bool) -> list[MediaFile]:
    """Completed audio/video files that need a waveform (every one when ``force_regenerate``)."""
    query = (
        db.query(MediaFile)
        .filter(MediaFile.status == FileStatus.COMPLETED)
        .filter(MediaFile.content_type.like("audio/%") | MediaFile.content_type.like("video/%"))
    )
    if not force_regenerate:
        query = query.filter(MediaFile.waveform_data.is_(None))
    return query.all()


def waveform_counts(db: Session) -> tuple[int, int, int]:
    """``(total, completed, completed_with_waveform)`` over audio/video files, in one query."""
    is_media = MediaFile.content_type.like("audio/%") | MediaFile.content_type.like("video/%")
    counts = (
        db.query(
            func.count().label("total"),
            func.count().filter(MediaFile.status == FileStatus.COMPLETED).label("completed"),
            func.count()
            .filter(
                MediaFile.status == FileStatus.COMPLETED,
                MediaFile.waveform_data.isnot(None),
            )
            .label("with_waveforms"),
        )
        .filter(is_media)
        .one()
    )
    return counts.total, counts.completed, counts.with_waveforms


def find_imported_duplicate(
    db: Session,
    *,
    user_id: int,
    organization_id: int | None,
    normalized_url: str,
    video_id: str,
) -> MediaFile | None:
    """The caller's live copy of an already-imported video in this tenant, or None.

    Matches on ``source_url`` first, then the legacy ``metadata_raw`` ``video_id`` and
    ``youtube_id`` keys. A trashed copy is not a duplicate (issue #1101), a quarantined
    one is.
    """
    base = db.query(MediaFile).filter(
        MediaFile.user_id == user_id,
        org_stamp_is(MediaFile.organization_id, organization_id),
        available_pred(include_quarantined=True),
    )
    existing = base.filter(MediaFile.source_url == normalized_url).first()
    if not existing:
        existing = (
            base.filter(text("metadata_raw->>'video_id' = :video_id"))
            .params(video_id=video_id)
            .first()
        )
    if not existing:
        existing = (
            base.filter(text("metadata_raw->>'youtube_id' = :youtube_id"))
            .params(youtube_id=video_id)
            .first()
        )
    return existing


def file_has_segments(db: Session, file_id: int) -> bool:
    """Whether a file has at least one transcript segment."""
    return (
        db.query(TranscriptSegment.id).filter(TranscriptSegment.media_file_id == file_id).first()
        is not None
    )


def list_ordered_segments(db: Session, file_id: int) -> list[TranscriptSegment]:
    """Every transcript segment of a file in reading order."""
    return (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(
            TranscriptSegment.start_time,
            TranscriptSegment.end_time,
            TranscriptSegment.id,
        )
        .all()
    )


def list_file_speakers(db: Session, file_id: int) -> list[Speaker]:
    """Every speaker row of a file."""
    return db.query(Speaker).filter(Speaker.media_file_id == file_id).all()


def list_comments_by_time(db: Session, file_id: int) -> list[Comment]:
    """Every comment on a file, ordered by timestamp."""
    return (
        db.query(Comment).filter(Comment.media_file_id == file_id).order_by(Comment.timestamp).all()
    )


def segment_stats(db: Session, file_id: int) -> tuple[int, float]:
    """``(segment count, end time of the last segment)`` for a file; ``(0, 0.0)`` if empty."""
    stats = (
        db.query(func.count(TranscriptSegment.id), func.max(TranscriptSegment.end_time))
        .filter(TranscriptSegment.media_file_id == file_id)
        .first()
    )
    if not stats:
        return 0, 0.0
    return stats[0], float(stats[1]) if stats[1] is not None else 0.0


def page_transcript_segments(
    db: Session, file_id: int, segment_limit: int | None, segment_offset: int
) -> tuple[list[TranscriptSegment], int]:
    """One page of a file's segments in reading order plus the file's total segment count.

    Each segment's speaker AND that speaker's linked profile are loaded with
    ``selectinload`` so the caller's ``add_computed_status`` never lazy-SELECTs per
    speaker. Not ``joinedload``: a joined speaker repeats its wide row (three JSONB
    detail columns) for every segment, while ``selectinload`` fetches each distinct
    speaker once (SQLAlchemy 2.0, "Relationship Loading Techniques", #1337).
    """
    total_segments = (
        db.query(func.count(TranscriptSegment.id))
        .filter(TranscriptSegment.media_file_id == file_id)
        .scalar()
    )
    segment_query = (
        db.query(TranscriptSegment)
        .options(selectinload(TranscriptSegment.speaker).selectinload(Speaker.profile))
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(
            TranscriptSegment.start_time,
            TranscriptSegment.end_time,
            TranscriptSegment.id,
        )
    )
    if segment_offset > 0:
        segment_query = segment_query.offset(segment_offset)
    if segment_limit is not None:
        segment_query = segment_query.limit(segment_limit)
    return segment_query.all(), total_segments


def count_speaker_turns(db: Session, file_id: int) -> int:
    """Number of speaker turns: adjacent segments by the same speaker count once.

    A window function (LAG over start_time) finds the points where the speaker changes.
    """
    result = db.execute(
        text("""
            SELECT COUNT(*) FROM (
                SELECT speaker_id,
                       LAG(speaker_id) OVER (ORDER BY start_time) AS prev_speaker_id
                FROM transcript_segment
                WHERE media_file_id = :file_id
            ) sub
            WHERE speaker_id IS DISTINCT FROM prev_speaker_id
               OR prev_speaker_id IS NULL
        """),
        {"file_id": file_id},
    ).scalar()
    return int(result or 0)


def list_file_speaker_ids(db: Session, file_id: int) -> list[int]:
    """Primary keys of every speaker row of a file."""
    return [int(row[0]) for row in db.query(Speaker.id).filter(Speaker.media_file_id == file_id)]


def delete_file_segments(db: Session, file_id: int) -> None:
    """Bulk-delete a file's transcript segments (no session sync); the caller commits.

    Bulk on purpose: with autoflush off, per-row ORM deletes were still pending when the
    speakers went and the segments' foreign key refused the speaker delete.
    """
    db.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == file_id).delete(
        synchronize_session=False
    )


def unlink_segment_speakers(db: Session, file_id: int) -> None:
    """Null ``speaker_id`` on every segment of a file so its speakers can be deleted."""
    db.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == file_id).update(
        {TranscriptSegment.speaker_id: None}, synchronize_session=False
    )


def delete_file_speakers(db: Session, file_id: int) -> None:
    """Bulk-delete a file's speaker rows (no session sync); the caller commits."""
    db.query(Speaker).filter(Speaker.media_file_id == file_id).delete(synchronize_session=False)


def delete_file_analytics(db: Session, file_id: int) -> None:
    """Mark a file's analytics row for deletion when there is one; the caller commits."""
    existing = db.query(Analytics).filter(Analytics.media_file_id == file_id).first()
    if existing:
        db.delete(existing)


def delete_file_topic_suggestions(db: Session, file_id: int) -> None:
    """Mark every AI topic suggestion of a file for deletion; the caller commits."""
    for suggestion in (
        db.query(TopicSuggestion).filter(TopicSuggestion.media_file_id == file_id).all()
    ):
        db.delete(suggestion)


def gallery_base_query(
    db: Session,
    load_options: Sequence[Any],
    *,
    user_id: int,
    org_scope: int | None,
    ownership: str,
    sees_all: bool,
) -> tuple[Query[MediaFile], int | None]:
    """The gallery's starting query and the owner id the later filters should apply.

    ``sees_all`` (the platform role) lists every file of the tenant; otherwise ``ownership``
    picks ``mine`` (default, owned files in the tenant), ``shared`` (reachable through a
    collection, not owned) or ``all`` (owned plus shared). The tenant gate on the owned
    branches is org rows in an org context and org-less rows in personal scope. The second
    value is None whenever the caller's own id must not be filtered on again.
    """
    if org_scope is not None:
        org_pred = MediaFile.organization_id == org_scope
    else:
        org_pred = MediaFile.organization_id.is_(None)

    if sees_all:
        return db.query(MediaFile).options(*load_options).filter(org_pred), None
    if ownership == "mine":
        query = (
            db.query(MediaFile)
            .options(*load_options)
            .filter(MediaFile.user_id == user_id, org_pred)
        )
        return query, user_id

    accessible = PermissionService.get_accessible_file_ids_subquery(
        db, user_id, organization_id=org_scope
    )
    if ownership == "shared":
        query = (
            db.query(MediaFile)
            .options(*load_options)
            .filter(
                MediaFile.id.in_(db.query(accessible.c.id)),
                MediaFile.user_id != user_id,  # Exclude owned files
            )
        )
        return query, None
    query = (
        db.query(MediaFile)
        .options(*load_options)
        .filter(MediaFile.id.in_(db.query(accessible.c.id)))
    )
    return query, None


def list_file_tags(db: Session, file_id: int, organization_id: int | None) -> list[Tag]:
    """Tags on a file that belong to the tenant or are system tags."""
    return (
        db.query(Tag)
        .join(FileTag, FileTag.tag_id == Tag.id)
        .filter(FileTag.media_file_id == file_id, tenant_bound_or_system(organization_id))
        .all()
    )


def get_file_analytics(db: Session, file_id: int) -> Analytics | None:
    """The stored analytics row of a file, or None."""
    return db.query(Analytics).filter(Analytics.media_file_id == file_id).first()


def list_file_speakers_with_profile(db: Session, file_id: int) -> list[Speaker]:
    """A file's speakers with their linked profiles eager-loaded.

    Loading the profile up front stops ``add_computed_status`` (which reads
    ``speaker.profile``) firing one lazy SELECT per speaker.
    """
    return (
        db.query(Speaker)
        .options(selectinload(Speaker.profile))
        .filter(Speaker.media_file_id == file_id)
        .all()
    )
