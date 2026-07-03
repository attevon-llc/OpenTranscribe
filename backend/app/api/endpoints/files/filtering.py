import logging
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Float
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Tag

logger = logging.getLogger(__name__)


def apply_search_filter(query: Query, search: str | None) -> Query:
    """
    Apply search filter for filename and title.

    Args:
        query: Base query
        search: Search term

    Returns:
        Filtered query
    """
    if search:
        # Escape LIKE metacharacters so user input like "%" or "_" is treated literally
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions = [MediaFile.filename.ilike(f"%{escaped}%", escape="\\")]
        if MediaFile.title is not None:
            conditions.append(MediaFile.title.ilike(f"%{escaped}%", escape="\\"))
        query = query.filter(sa.or_(*conditions))
    return query


def apply_tag_filter(query: Query, tag: list[str] | None) -> Query:
    """
    Apply tag filter — finds files that have ALL specified tags (AND logic).

    Uses a single subquery with HAVING COUNT instead of chaining one join per
    tag, which avoids Cartesian-product blowup when multiple tags are selected.

    Args:
        query: Base query
        tag: List of tag names

    Returns:
        Filtered query
    """
    if tag and len(tag) > 0:
        # Subquery: find media_file IDs that have every requested tag
        matching_ids = (
            select(FileTag.media_file_id)
            .join(Tag, Tag.id == FileTag.tag_id)
            .where(Tag.name.in_(tag))
            .group_by(FileTag.media_file_id)
            .having(func.count(func.distinct(Tag.id)) == len(tag))
        )
        query = query.filter(MediaFile.id.in_(matching_ids))
    return query


def apply_speaker_filter(query: Query, speaker: list[str] | None) -> Query:
    """
    Apply speaker filter using display name or original name.

    Uses an EXISTS subquery instead of joining through transcript_segment
    then calling DISTINCT.  This eliminates the Cartesian product that occurs
    when a file has many segments — the planner can stop at the first
    matching row per file.

    Args:
        query: Base query
        speaker: List of speaker names

    Returns:
        Filtered query
    """
    if speaker and len(speaker) > 0:
        speaker_or_conditions = []
        for s in speaker:
            speaker_or_conditions.append(sa.or_(Speaker.display_name == s, Speaker.name == s))

        if speaker_or_conditions:
            exists_subq = (
                select(sa.literal(1))
                .select_from(Speaker)
                .where(
                    Speaker.media_file_id == MediaFile.id,
                    sa.or_(*speaker_or_conditions),
                )
                .exists()
            )
            query = query.filter(exists_subq)
    return query


def apply_date_filters(query: Query, from_date: datetime | None, to_date: datetime | None) -> Query:
    """
    Apply date range filters.

    Args:
        query: Base query
        from_date: Start date
        to_date: End date

    Returns:
        Filtered query
    """
    if from_date:
        query = query.filter(MediaFile.upload_time >= from_date)

    if to_date:
        query = query.filter(MediaFile.upload_time <= to_date)

    return query


def apply_duration_filters(
    query: Query, min_duration: float | None, max_duration: float | None
) -> Query:
    """
    Apply duration range filters.

    Args:
        query: Base query
        min_duration: Minimum duration in seconds
        max_duration: Maximum duration in seconds

    Returns:
        Filtered query
    """
    if min_duration is not None:
        query = query.filter(cast(MediaFile.duration, Float) >= min_duration)

    if max_duration is not None:
        query = query.filter(cast(MediaFile.duration, Float) <= max_duration)

    return query


def apply_file_size_filters(
    query: Query, min_file_size: int | None, max_file_size: int | None
) -> Query:
    """
    Apply file size range filters (MB to bytes conversion).

    Args:
        query: Base query
        min_file_size: Minimum file size in MB
        max_file_size: Maximum file size in MB

    Returns:
        Filtered query
    """
    if min_file_size is not None:
        query = query.filter(MediaFile.file_size >= min_file_size * 1024 * 1024)

    if max_file_size is not None:
        query = query.filter(MediaFile.file_size <= max_file_size * 1024 * 1024)

    return query


def apply_file_type_filter(query: Query, file_type: list[str] | None) -> Query:
    """
    Apply file type filter (audio/video).

    Args:
        query: Base query
        file_type: List of file types ('audio', 'video')

    Returns:
        Filtered query
    """
    if file_type:
        type_conditions = []
        for ft in file_type:
            if ft == "audio":
                type_conditions.append(MediaFile.content_type.like("audio/%"))
            elif ft == "video":
                type_conditions.append(MediaFile.content_type.like("video/%"))
        if type_conditions:
            query = query.filter(sa.or_(*type_conditions))

    return query


def apply_status_filter(query: Query, status: list[str] | None) -> Query:
    """
    Apply status filter.

    Args:
        query: Base query
        status: List of status values

    Returns:
        Filtered query
    """
    if status:
        status_conditions = []
        for s in status:
            status_conditions.append(MediaFile.status == s)
        if status_conditions:
            query = query.filter(sa.or_(*status_conditions))

    return query


def apply_transcript_search_filter(
    query: Query,
    transcript_search: str | None,
    user_id: int | None = None,
) -> Query:
    """
    Apply transcript content search filter using OpenSearch.

    Delegates to the OpenSearch transcript index instead of scanning the
    transcript_segment table with ILIKE.  OpenSearch provides BM25 keyword
    matching with highlighting — orders of magnitude faster than a
    PostgreSQL full-table scan on a TEXT column.

    Falls back gracefully if OpenSearch is unavailable (filter is skipped).

    Args:
        query: Base query
        transcript_search: Search term for transcript content
        user_id: Restrict search to this user's files (None = all)

    Returns:
        Filtered query with only matching file IDs
    """
    if not transcript_search:
        return query

    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        logger.warning("OpenSearch unavailable — skipping transcript search filter")
        return query

    try:
        # Build OpenSearch query with both phrase and keyword matching
        must_clauses: list[dict] = []
        filter_clauses: list[dict] = []

        # Keyword search on transcript content
        must_clauses.append(
            {
                "multi_match": {
                    "query": transcript_search,
                    "fields": ["content"],
                    "type": "best_fields",
                    "operator": "and",
                }
            }
        )

        # Scope to user's files when specified
        if user_id is not None:
            filter_clauses.append({"term": {"user_id": user_id}})

        search_body: dict = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    **({"filter": filter_clauses} if filter_clauses else {}),
                }
            },
            "_source": ["file_uuid"],
            "size": 10000,  # upper bound; gallery pagination trims further
        }

        response = client.search(
            index=settings.OPENSEARCH_TRANSCRIPT_INDEX,
            body=search_body,
        )

        file_uuids = []
        for hit in response["hits"]["hits"]:
            file_uuid = hit["_source"].get("file_uuid")
            if file_uuid:
                file_uuids.append(file_uuid)

        if not file_uuids:
            # No matches — return impossible filter so query yields zero rows
            query = query.filter(sa.literal(False))
        else:
            query = query.filter(MediaFile.uuid.in_(file_uuids))

    except Exception as e:
        logger.error(f"OpenSearch transcript search failed: {e}")
        # Degrade gracefully — skip filter rather than error the whole request

    return query


def apply_all_filters(query: Query, filters: dict) -> Query:
    """
    Apply all filters to the query.

    Filter order is optimized: cheap, high-selectivity filters (status,
    date range) are applied first to reduce the working set before more
    expensive filters (speaker EXISTS, tag subquery, OpenSearch).

    Args:
        query: Base query
        filters: Dictionary of filter parameters

    Returns:
        Filtered query
    """
    # High-selectivity / cheap filters first
    query = apply_status_filter(query, filters.get("status"))
    query = apply_date_filters(query, filters.get("from_date"), filters.get("to_date"))
    query = apply_file_type_filter(query, filters.get("file_type"))
    query = apply_duration_filters(query, filters.get("min_duration"), filters.get("max_duration"))
    query = apply_file_size_filters(
        query, filters.get("min_file_size"), filters.get("max_file_size")
    )
    query = apply_search_filter(query, filters.get("search"))

    # More expensive filters — subqueries / external service
    query = apply_tag_filter(query, filters.get("tag"))
    query = apply_speaker_filter(query, filters.get("speaker"))
    query = apply_transcript_search_filter(
        query,
        filters.get("transcript_search"),
        user_id=filters.get("user_id"),
    )

    return query


def get_metadata_filters(
    db: Session,
    user_id: int,
    ownership: str = "all",
    *,
    organization_id: OrgScope = UNSCOPED,
) -> dict:
    """
    Get available metadata filters for the user's accessible files.

    Consolidated into 2 queries (was 6) — one for distinct values, one for
    all min/max ranges — so PostgreSQL scans the table at most twice.

    Args:
        db: Database session
        user_id: User ID
        ownership: 'mine', 'shared', or 'all' (default: 'all')
        organization_id: Active org id, None for personal, or UNSCOPED (legacy) —
            tenant-gates every ownership branch (default-deny across scopes).

    Returns:
        Dictionary of available filter options
    """
    from sqlalchemy import and_

    from app.services.permission_service import PermissionService

    # Build the file filter based on ownership (tenant-gated)
    if ownership == "mine":
        file_filter = MediaFile.user_id == user_id
        if not isinstance(organization_id, _Unscoped):
            org_pred = (
                MediaFile.organization_id == organization_id
                if organization_id is not None
                else MediaFile.organization_id.is_(None)
            )
            file_filter = and_(file_filter, org_pred)
    elif ownership == "shared":
        accessible_sq = PermissionService.get_accessible_file_ids_subquery(
            db, user_id, organization_id=organization_id
        )
        file_filter = and_(MediaFile.id.in_(select(accessible_sq)), MediaFile.user_id != user_id)
    else:  # "all"
        accessible_sq = PermissionService.get_accessible_file_ids_subquery(
            db, user_id, organization_id=organization_id
        )
        file_filter = MediaFile.id.in_(select(accessible_sq))

    format_text = cast(MediaFile.metadata_important["format"], String)
    codec_text = cast(MediaFile.metadata_important["codec"], String)
    width_text = cast(MediaFile.metadata_important["width"], String)
    height_text = cast(MediaFile.metadata_important["height"], String)

    # --- Query 1: Distinct values (formats, codecs) via array_agg ---
    distinct_row = (
        db.query(
            func.array_agg(func.distinct(format_text)).filter(
                format_text.isnot(None), format_text != "null"
            ),
            func.array_agg(func.distinct(codec_text)).filter(
                codec_text.isnot(None), codec_text != "null"
            ),
        )
        .filter(file_filter)
        .first()
    )

    formats = [v for v in (distinct_row[0] or []) if v] if distinct_row else []
    codecs = [v for v in (distinct_row[1] or []) if v] if distinct_row else []

    # --- Query 2: All min/max ranges in a single table scan ---
    ranges = (
        db.query(
            func.min(cast(MediaFile.duration, Float)),
            func.max(cast(MediaFile.duration, Float)),
            func.min(cast(width_text, Integer)),
            func.max(cast(width_text, Integer)),
            func.min(cast(height_text, Integer)),
            func.max(cast(height_text, Integer)),
            func.min(MediaFile.file_size),
            func.max(MediaFile.file_size),
        )
        .filter(file_filter)
        .first()
    )

    if ranges is not None:
        min_duration = float(ranges[0]) if ranges[0] is not None else 0.0
        max_duration = float(ranges[1]) if ranges[1] is not None else 0.0
        min_width = ranges[2] if ranges[2] is not None else 0
        max_width = ranges[3] if ranges[3] is not None else 0
        min_height = ranges[4] if ranges[4] is not None else 0
        max_height = ranges[5] if ranges[5] is not None else 0
        min_file_size = ranges[6] if ranges[6] is not None else 0
        max_file_size = ranges[7] if ranges[7] is not None else 0
    else:
        min_duration = max_duration = 0.0
        min_width = max_width = min_height = max_height = 0
        min_file_size = max_file_size = 0

    return {
        "formats": formats,
        "codecs": codecs,
        "duration": {"min": min_duration, "max": max_duration},
        "file_size": {"min": min_file_size, "max": max_file_size},
        "resolution": {
            "width": {"min": min_width, "max": max_width},
            "height": {"min": min_height, "max": max_height},
        },
    }
