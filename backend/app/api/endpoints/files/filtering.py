import logging
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import Float
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session

from app.core.constants import FILE_TYPE_MIME_PREFIXES
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Tag
from app.models.user import User

logger = logging.getLogger(__name__)

# Query-cost bound on the owner-facet endpoint (issue #966). NOT a security
# control — the security boundary is the join predicate in
# `get_accessible_owners` (the same `get_accessible_file_ids_subquery`/org-scope
# predicate `GET /files` itself uses). Raising this number only changes how many
# rows a single request can cost; it does not widen who can be seen.
OWNER_FACET_LIMIT = 100


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
        # Subquery: find media_file IDs that have every requested tag.
        # COUNT over Tag.name, not Tag.id: since v374 the same name can exist as
        # several rows (one per owner), and a file carrying two of them would
        # otherwise satisfy a two-tag filter with a single distinct name.
        matching_ids = (
            select(FileTag.media_file_id)
            .join(Tag, Tag.id == FileTag.tag_id)
            .where(Tag.name.in_(tag))
            .group_by(FileTag.media_file_id)
            .having(func.count(func.distinct(Tag.name)) == len(tag))
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

    An unrecognized value narrows to nothing rather than being dropped (issue #871):
    dropping it returned the caller's WHOLE accessible file set while they believed they had
    filtered, and ``?file_type=audio&file_type=bogus`` silently narrowed to audio-only. This
    is not an authorization bug — the base query is already scoped to the caller's own
    accessible/non-quarantined files before this filter runs — it just returned too many of
    the caller's OWN files. Mirrors the search plane's ``_file_type_filter_clause``
    (``FILE_TYPE_MIME_PREFIXES``, shared with this function since #871), including its literal
    MIME fallback: a caller passing a full ``audio/mpeg`` still gets an exact match.
    """
    if file_type:
        type_conditions: list[sa.ColumnElement[bool]] = []
        for ft in file_type:
            prefix = FILE_TYPE_MIME_PREFIXES.get(ft.lower())
            if prefix:
                type_conditions.append(MediaFile.content_type.like(f"{prefix}%"))
            else:
                type_conditions.append(MediaFile.content_type == ft)
        # type_conditions is never empty here (every branch above appends exactly one
        # condition per requested value) — no "if type_conditions:" guard. That guard was
        # the bug: it let an all-unrecognized file_type list fall through unfiltered.
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
        logger.exception(f"OpenSearch transcript search failed: {e}")
        # Degrade gracefully — skip filter rather than error the whole request

    return query


def resolve_owner_user_ids(db: Session, owner: list[UUID] | None) -> list[int] | None:
    """Translate the ``owner`` query param's UUIDs into internal user ids.

    No authorization check happens here — the caller applies the resulting ids
    as a predicate *inside* an already-authorized file set (the
    ``get_accessible_file_ids_subquery`` union for a normal user, or the
    tenant-org predicate for an admin — see ``list_media_files`` and
    ``get_accessible_owners``), so a UUID that does not name an owner the
    caller can actually see just matches zero rows; it cannot widen the set.

    Args:
        db: Database session.
        owner: Requested owner UUIDs, or ``None``/empty for "no owner filter".

    Returns:
        ``None`` when no filter was requested. Otherwise a list of internal
        user ids — ``[-1]`` (an impossible id) when none of the requested
        UUIDs resolved to a real user, so an unmatched owner narrows to zero
        results rather than silently falling through to "unfiltered"
        (mirrors the ``file_type`` fix for issue #871).
    """
    if not owner:
        return None
    ids = [row[0] for row in db.query(User.id).filter(User.uuid.in_(owner)).all()]
    return ids or [-1]


def apply_owner_filter(query: Query, owner_user_ids: list[int] | None) -> Query:
    """Restrict to specific owner(s) (issue #966's ownership multi-select).

    Applied INSIDE the caller's already-authorized accessible/admin file set —
    this is a plain narrowing predicate, not a second authorization check.
    See :func:`resolve_owner_user_ids`.

    Args:
        query: Base query, already scoped to the caller's accessible files.
        owner_user_ids: Internal user ids to restrict to, or ``None`` for no filter.

    Returns:
        Filtered query.
    """
    if owner_user_ids is not None:
        query = query.filter(MediaFile.user_id.in_(owner_user_ids))
    return query


def get_accessible_owners(
    db: Session,
    user_id: int,
    *,
    organization_id: OrgScope = UNSCOPED,
    is_admin: bool = False,
) -> list[User]:
    """Distinct owners of the files THIS caller can already see (issue #966).

    SECURITY (constraint 1 of the #966 review): joins ``User`` against the
    IDENTICAL file-visibility predicate ``GET /files`` itself uses —
    ``PermissionService.get_accessible_file_ids_subquery`` for a normal
    caller (owned files + files reachable via a shared collection, grant or
    group), or the same tenant-org predicate ``list_media_files``'s own
    admin branch uses for an admin. This answers "whose files can *this*
    caller already see", never "who exists" — it deliberately does NOT reuse
    ``GET /users/search``'s tenant-wide directory scope, which would let any
    authenticated user enumerate every account in the organization.

    This is a CLOSED ENUMERATION endpoint: there is no free-text parameter
    here on purpose (constraint 2) — the caller receives the full (capped)
    list of owners they can already see and filters it client-side, the same
    shape ``SearchableMultiSelect`` already uses for tags/collections/speakers.
    A free-text parameter would turn an authorized list into an
    account-probing oracle.

    Args:
        db: Database session.
        user_id: The caller's internal id.
        organization_id: Active org id, ``None`` for personal, or ``UNSCOPED``
            (legacy) — tenant-gates the query exactly like ``list_media_files``.
        is_admin: When True, mirrors ``list_media_files``'s admin branch (every
            org-scoped file, not just the caller's accessible subset).

    Returns:
        Up to :data:`OWNER_FACET_LIMIT` distinct ``User`` rows (constraint 4 —
        a QUERY COST bound only; see that constant's docstring for why it must
        never be mistaken for the security boundary, which is the join
        predicate above).
    """
    if is_admin:
        if not isinstance(organization_id, _Unscoped):
            org_pred = (
                MediaFile.organization_id == organization_id
                if organization_id is not None
                else MediaFile.organization_id.is_(None)
            )
            file_pred: sa.ColumnElement[bool] = org_pred
        else:
            file_pred = sa.true()
    else:
        from app.services.permission_service import PermissionService

        accessible_sq = PermissionService.get_accessible_file_ids_subquery(
            db, user_id, organization_id=organization_id
        )
        file_pred = MediaFile.id.in_(select(accessible_sq))

    return (
        db.query(User)
        .join(MediaFile, MediaFile.user_id == User.id)
        .filter(file_pred)
        .distinct()
        .order_by(User.full_name, User.email)
        .limit(OWNER_FACET_LIMIT)
        .all()
    )


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
    query = apply_owner_filter(query, filters.get("owner_user_ids"))
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
    is_admin: bool = False,
    owner_user_ids: list[int] | None = None,
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
        is_admin: When False (default), quarantined (DMCA/legal-hold) files are
            excluded — matching every other read surface (A2). Facet VALUES
            (formats, codecs, languages, date/size ranges) drawn only from a
            quarantined file must not leak even though the file itself 404s.
        owner_user_ids: Optional owner-filter ids (issue #966), applied INSIDE
            the ownership-scoped predicate below via ``resolve_owner_user_ids``
            — so the facet counts this returns match what ``GET /files``
            itself would return for the same ``owner`` selection.

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

    if not is_admin:
        # Mirrors `services/takedown_service.exclude_quarantined` — that helper
        # takes a Query, and `file_filter` here is a bare predicate combined
        # into two different queries below, so the same condition is applied
        # directly rather than reshaping this function around a Query object.
        file_filter = and_(file_filter, MediaFile.is_quarantined.is_(False))

    if owner_user_ids is not None:
        file_filter = and_(file_filter, MediaFile.user_id.in_(owner_user_ids))

    format_text = cast(MediaFile.metadata_important["format"], String)
    codec_text = cast(MediaFile.metadata_important["codec"], String)
    width_text = cast(MediaFile.metadata_important["width"], String)
    height_text = cast(MediaFile.metadata_important["height"], String)

    # --- Query 1: Distinct values (formats, codecs, languages) via array_agg ---
    #
    # `languages` rides this existing query rather than taking its own round trip
    # (#453). Transcription has always been multilingual — WhisperX detects 100+
    # languages and `MediaFile.language` records the code — but nothing ever offered
    # it as a filter, so a user with a mixed-language library had no way to narrow to
    # one. The chunk index has carried `language` as a filterable keyword all along.
    distinct_row = (
        db.query(
            func.array_agg(func.distinct(format_text)).filter(
                format_text.isnot(None), format_text != "null"
            ),
            func.array_agg(func.distinct(codec_text)).filter(
                codec_text.isnot(None), codec_text != "null"
            ),
            func.array_agg(func.distinct(MediaFile.language)).filter(
                MediaFile.language.isnot(None), MediaFile.language != ""
            ),
        )
        .filter(file_filter)
        .first()
    )

    formats = [v for v in (distinct_row[0] or []) if v] if distinct_row else []
    codecs = [v for v in (distinct_row[1] or []) if v] if distinct_row else []
    # Sorted so the filter list is stable between requests; array_agg order is not.
    languages = sorted(v for v in (distinct_row[2] or []) if v) if distinct_row else []

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
        "languages": languages,
        "duration": {"min": min_duration, "max": max_duration},
        "file_size": {"min": min_file_size, "max": max_file_size},
        "resolution": {
            "width": {"min": min_width, "max": max_width},
            "height": {"min": min_height, "max": max_height},
        },
    }
