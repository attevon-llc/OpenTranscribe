"""Speaker list, picker, occurrence and segment-count queries.

The endpoints decide *who may see what* (``file_ids_in_scope``, ``org_scope_pred`` and the
owner/bypass choice) and hand the result in as SQL criteria; this module runs the queries.
Functions take a ``Session`` and plain arguments and never commit.
"""

from collections.abc import Collection
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy import ColumnExpressionArgument
from sqlalchemy import Integer
from sqlalchemy import case
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import load_only
from sqlalchemy.orm import selectinload

from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.services.file_availability import exclude_unavailable
from app.services.permission_service import PermissionService

FIRST_SEGMENTS_PER_SPEAKER = 4


@dataclass(frozen=True)
class PickerGroup:
    """One entry of the speaker picker: a label shared by one or more speakers."""

    label: str
    is_unnamed: bool
    media_count: int
    representative_uuid: str
    representative_name: str
    profile_uuid: str | None


def speaker_number_order() -> list[Any]:
    """SQL ordering: ``SPEAKER_nn`` first by number, others after; ``id`` makes it total."""
    number = case(
        (Speaker.name.op("~")(r"^SPEAKER_\d+$"), cast(func.substr(Speaker.name, 9), Integer)),
        else_=999,
    )
    return [number, Speaker.id]


def list_speakers_page(
    db: Session,
    criteria: Sequence[ColumnExpressionArgument[bool]],
    *,
    verified_only: bool,
    file_id: int | None,
    skip: int,
    page_size: int,
) -> list[Speaker]:
    """Speakers matching ``criteria``, with profile and file loaded.

    One file's speakers come back whole; otherwise the page is cut in the database
    (``skip``/``page_size`` over ``speaker_number_order``).
    """
    query = (
        db.query(Speaker)
        .options(joinedload(Speaker.profile), joinedload(Speaker.media_file))
        .filter(*criteria)
    )
    if verified_only:
        query = query.filter(Speaker.verified.is_(True))
    if file_id is not None:
        query = query.filter(Speaker.media_file_id == file_id)
    else:
        query = query.order_by(*speaker_number_order()).offset(skip).limit(page_size)
    return query.all()


def profile_id_for_uuid(
    db: Session,
    profile_uuid: str,
    *,
    owner_user_id: int | None,
    tenant_pred: ColumnExpressionArgument[bool] | None,
) -> int | None:
    """Internal id of the profile with this UUID, optionally limited to an owner and a tenant."""
    query = db.query(SpeakerProfile.id).filter(SpeakerProfile.uuid == profile_uuid)
    if owner_user_id is not None:
        query = query.filter(SpeakerProfile.user_id == owner_user_id)
    if tenant_pred is not None:
        query = query.filter(tenant_pred)
    found = query.scalar()
    return int(found) if found is not None else None


def speaker_picker_groups(
    db: Session,
    filters: Sequence[ColumnExpressionArgument[bool]],
    *,
    label: ColumnElement[Any],
    is_unnamed: ColumnElement[Any],
    visible_profile: ColumnElement[Any],
    limit: int,
) -> list[PickerGroup]:
    """The top ``limit`` picker entries: speakers matching ``filters`` grouped by ``label``.

    Ordered named-first, then by how many files the label spans. One GROUP BY query ranks the
    groups; a second fetches each winner's representative speaker and a third the profile
    UUIDs.
    """
    media_count = func.count(func.distinct(Speaker.media_file_id))
    grouped = (
        db.query(
            label.label("label"),
            is_unnamed.label("is_unnamed"),
            media_count.label("media_count"),
            func.min(Speaker.id).label("rep_id"),
            func.min(visible_profile).label("profile_id"),
        )
        .filter(*filters)
        .group_by(label)
        .order_by(is_unnamed.asc(), media_count.desc(), label)
        .limit(limit)
        .all()
    )
    if not grouped:
        return []

    rep_ids = [row.rep_id for row in grouped]
    reps_by_id = {
        row.id: row
        for row in db.query(Speaker.id, Speaker.uuid, Speaker.name).filter(Speaker.id.in_(rep_ids))
    }
    profile_ids = {row.profile_id for row in grouped if row.profile_id is not None}
    profile_uuids: dict[int, str] = {}
    if profile_ids:
        profile_uuids = {
            pid: str(puuid)
            for pid, puuid in db.query(SpeakerProfile.id, SpeakerProfile.uuid).filter(
                SpeakerProfile.id.in_(profile_ids)
            )
        }

    groups: list[PickerGroup] = []
    for row in grouped:
        rep = reps_by_id.get(row.rep_id)
        if rep is None:
            continue
        groups.append(
            PickerGroup(
                label=row.label,
                is_unnamed=bool(row.is_unnamed),
                media_count=row.media_count,
                representative_uuid=str(rep.uuid),
                representative_name=rep.name,
                profile_uuid=profile_uuids.get(row.profile_id) if row.profile_id else None,
            )
        )
    return groups


def segment_counts_by_speaker(db: Session, speaker_ids: Collection[int]) -> dict[int, int]:
    """Transcript segment count per speaker, in one query."""
    if not speaker_ids:
        return {}
    rows = (
        db.query(TranscriptSegment.speaker_id, func.count(TranscriptSegment.id))
        .filter(TranscriptSegment.speaker_id.in_(speaker_ids))
        .group_by(TranscriptSegment.speaker_id)
        .all()
    )
    return {speaker_id: count for speaker_id, count in rows if speaker_id is not None}


def first_segment_timestamps_by_speaker(
    db: Session, speaker_ids: Sequence[int], file_id: int
) -> dict[int, list[dict[str, Any]]]:
    """The first ``FIRST_SEGMENTS_PER_SPEAKER`` segments per speaker in a file.

    Window functions rank each speaker's segments and number every segment of the file, so
    ``segment_index`` is the 0-based position within the whole file.
    """
    if not speaker_ids:
        return {}
    ranked = text("""
        WITH ranked AS (
            SELECT
                ts.speaker_id,
                ts.uuid,
                ts.start_time,
                ROW_NUMBER() OVER (
                    PARTITION BY ts.speaker_id ORDER BY ts.start_time
                ) AS speaker_rank,
                ROW_NUMBER() OVER (
                    ORDER BY ts.start_time
                ) - 1 AS segment_index
            FROM transcript_segment ts
            WHERE ts.media_file_id = :file_id
              AND ts.speaker_id = ANY(:speaker_ids)
        )
        SELECT speaker_id, uuid, start_time, segment_index
        FROM ranked
        WHERE speaker_rank <= :per_speaker
        ORDER BY speaker_id, start_time
    """)
    rows = db.execute(
        ranked,
        {
            "file_id": file_id,
            "speaker_ids": list(speaker_ids),
            "per_speaker": FIRST_SEGMENTS_PER_SPEAKER,
        },
    ).fetchall()

    timestamps: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        timestamps.setdefault(row[0], []).append(
            {"uuid": str(row[1]), "start_time": float(row[2]), "segment_index": int(row[3])}
        )
    return timestamps


def _scoped_speakers(
    query: Any,
    *,
    owner_user_id: int | None,
    tenant_pred: ColumnExpressionArgument[bool] | None,
    include_quarantined: bool | None,
) -> list[Speaker]:
    if owner_user_id is not None:
        query = query.filter(Speaker.user_id == owner_user_id)
    if include_quarantined is not None:
        query = exclude_unavailable(query, include_quarantined=include_quarantined)
    if tenant_pred is not None:
        query = query.filter(tenant_pred)
    # Every caller reads each speaker's file (occurrence rows, tenant checks); one IN query
    # replaces a lazy load per speaker.
    rows: list[Speaker] = query.options(selectinload(Speaker.media_file)).all()
    return rows


def speakers_linked_to_profile(
    db: Session,
    profile_id: int,
    *,
    owner_user_id: int | None,
    tenant_pred: ColumnExpressionArgument[bool] | None,
    include_quarantined: bool | None,
) -> list[Speaker]:
    """Speakers linked to a profile, on files the caller may see.

    ``owner_user_id`` limits to one owner (``None`` = every owner in scope). ``tenant_pred``
    is the file-tenant criterion. ``include_quarantined`` is the list exemption passed to
    ``exclude_unavailable``; ``None`` applies no availability filter at all.
    """
    query = db.query(Speaker).join(MediaFile).filter(Speaker.profile_id == profile_id)
    return _scoped_speakers(
        query,
        owner_user_id=owner_user_id,
        tenant_pred=tenant_pred,
        include_quarantined=include_quarantined,
    )


def speakers_named(
    db: Session,
    display_name: str | None,
    *,
    excluding_speaker_id: int | None,
    owner_user_id: int | None,
    tenant_pred: ColumnExpressionArgument[bool] | None,
    include_quarantined: bool | None,
) -> list[Speaker]:
    """Speakers with this display name on files the caller may see (see ``speakers_linked_to_profile``)."""
    query = db.query(Speaker).join(MediaFile).filter(Speaker.display_name == display_name)
    if excluding_speaker_id is not None:
        query = query.filter(Speaker.id != excluding_speaker_id)
    return _scoped_speakers(
        query,
        owner_user_id=owner_user_id,
        tenant_pred=tenant_pred,
        include_quarantined=include_quarantined,
    )


def user_media_file_summaries(db: Session, user_id: int) -> list[Any]:
    """``(id, filename, title, status)`` rows of every file the user owns (diagnostics)."""
    return (
        db.query(MediaFile.id, MediaFile.filename, MediaFile.title, MediaFile.status)
        .filter(MediaFile.user_id == user_id)
        .all()
    )


def user_speakers_for_diagnostics(db: Session, user_id: int) -> list[Speaker]:
    """The user's speakers with only the diagnostic columns loaded, in file then id order."""
    return (
        db.query(Speaker)
        .options(
            load_only(
                Speaker.id,
                Speaker.name,
                Speaker.display_name,
                Speaker.profile_id,
                Speaker.media_file_id,
                Speaker.verified,
                Speaker.confidence,
                Speaker.identity_source,
            )
        )
        .filter(Speaker.user_id == user_id)
        .order_by(Speaker.media_file_id, Speaker.id)
        .all()
    )


def user_profiles(db: Session, user_id: int) -> list[SpeakerProfile]:
    return db.query(SpeakerProfile).filter(SpeakerProfile.user_id == user_id).all()


def speakers_with_display_name(
    db: Session,
    display_name: str,
    *,
    owner_user_id: int | None,
    tenant_pred: ColumnExpressionArgument[bool],
) -> list[Speaker]:
    """Speakers with this display name that satisfy ``tenant_pred`` (no file join)."""
    query = db.query(Speaker).filter(Speaker.display_name == display_name)
    if owner_user_id is not None:
        query = query.filter(Speaker.user_id == owner_user_id)
    return query.filter(tenant_pred).all()


def accessible_file_scope(db: Session, user_id: int, organization_id: OrgScope) -> Any:
    """Subquery of the file ids a user may read in a tenant (owned and shared)."""
    return select(
        PermissionService.get_accessible_file_ids_subquery(
            db, user_id, organization_id=organization_id
        )
    )
