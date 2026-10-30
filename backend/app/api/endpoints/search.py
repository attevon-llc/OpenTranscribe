"""Search API endpoints with hybrid BM25 + vector search."""

import logging
import math
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.search_helpers import _attach_visible_collections
from app.api.endpoints.search_helpers import _drop_quarantined_search_hits
from app.api.endpoints.search_helpers import _require_iso_date_bounds
from app.api.endpoints.search_helpers import _require_valid_sort_and_mode
from app.api.endpoints.search_helpers import _resolve_source_set
from app.api.endpoints.search_helpers import _search_response_to_schema
from app.api.endpoints.search_helpers import _source_counts
from app.api.endpoints.search_helpers import _summary_search_payload
from app.auth.rate_limit import get_search_rate_limit
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.core.constants import SEARCH_DEFAULT_PAGE_SIZE
from app.core.constants import SEARCH_MAX_PAGE_SIZE
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.user import User
from app.services.collection_visibility import resolve_collection_filter
from app.services.request_context import RequestContext

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
@limiter.limit(get_search_rate_limit(), key_func=user_or_ip_key)
def search_transcripts(
    *,
    request: Request,
    q: str = Query(..., min_length=1, description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(
        SEARCH_DEFAULT_PAGE_SIZE, ge=1, le=SEARCH_MAX_PAGE_SIZE, description="Results per page"
    ),
    speakers: list[str] = Query(None, description="Filter by speaker names"),
    tags: list[str] = Query(None, description="Filter by tags"),
    date_from: str | None = Query(None, description="Filter from date (ISO format)"),
    date_to: str | None = Query(None, description="Filter to date (ISO format)"),
    sort_by: str = Query(
        "relevance",
        description=(
            "Sort by: relevance, upload_time, completed_at, filename, duration, file_size. "
            "Note: completed_at uses upload_time for search results (completion time not indexed)."
        ),
    ),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    search_mode: str = Query("hybrid", description="Search mode: hybrid or keyword"),
    file_type: list[str] | None = Query(None, description="Filter by file type: audio, video"),
    collection_id: int | None = Query(
        None,
        description=(
            "Filter by collection integer ID (API callers). A collection the caller "
            "cannot see matches nothing. Mutually exclusive with `collection_uuid`."
        ),
    ),
    collection_uuid: UUID | None = Query(
        None,
        description=(
            "Filter by collection UUID (what the SPA sends). A collection the caller "
            "cannot see matches nothing. Mutually exclusive with `collection_id`."
        ),
    ),
    min_duration: float | None = Query(None, description="Minimum duration in seconds"),
    max_duration: float | None = Query(None, description="Maximum duration in seconds"),
    min_file_size: int | None = Query(None, description="Minimum file size in bytes"),
    max_file_size: int | None = Query(None, description="Maximum file size in bytes"),
    language: str | None = Query(None, description="Filter by language code"),
    title_filter: str | None = Query(
        None, description="Filter by filename/title (substring match)"
    ),
    file_uuid: str | None = Query(
        None,
        description=(
            "Scope results to a single file (its UUID). Used by the in-page transcript "
            "find bar to list every match across the whole paginated transcript."
        ),
    ),
    result_type: str = Query(
        "transcripts",
        description=(
            "DEPRECATED — superseded by `sources` (issue #760). Which result "
            "group(s) to return: transcripts, summaries, or all. Defaults to "
            "transcripts for byte-identical behavior against existing callers. "
            "Ignored when `sources` is supplied."
        ),
    ),
    sources: list[str] | None = Query(
        None,
        description=(
            "Issue #760 — which result source(s) to search: content, title, "
            "speaker, summary (repeat the param for more than one, e.g. "
            "`sources=content&sources=title`). Wins over `result_type` when "
            "both are supplied. Omitting it entirely preserves `result_type`'s "
            "(or its own default's) byte-identical legacy behavior. An "
            "explicitly empty or unknown value is a 400."
        ),
    ),
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
) -> dict[str, Any]:
    """
    Google-style hybrid search across all transcripts.

    Returns results grouped by file with timestamped occurrences.
    Uses BM25 + vector search with Reciprocal Rank Fusion (RRF).

    Args:
        q: Search query text.
        page: Page number (1-indexed).
        page_size: Number of results per page.
        speakers: Optional speaker name filters.
        tags: Optional tag filters.
        date_from: Optional start date filter.
        date_to: Optional end date filter.
        sort_by: Sort field - relevance, upload_time, completed_at, filename, duration, file_size.
        sort_order: Sort direction - asc or desc.

    Returns:
        Search results grouped by file with highlighted snippets.

    Notes:
        - Results are sorted in unified order by the requested field. RRF scores
          already combine both keyword and semantic signals.
        - Sorting by 'completed_at' uses upload_time as a fallback since the completion
          timestamp is not indexed in the search layer.
        - **Rate-limited** (issue #904), keyed per-user: ``RATE_LIMIT_SEARCH_PER_MINUTE``
          (default 30/minute) — this route can burn ~2s of Presidio snippet masking
          per call. ``/count`` and ``/suggestions`` are deliberately NOT limited: they
          are per-keystroke polls by design (``TranscriptSearch.svelte``,
          ``SearchAutocomplete.svelte``), built to be cheap
          (see ``search_match_count``'s docstring), already quarantine-hardened by
          issue #817. A 30/min cap on either would break the find bar.
    """
    _require_valid_sort_and_mode(sort_by, sort_order, search_mode)

    _require_iso_date_bounds(date_from=date_from, date_to=date_to)

    if collection_uuid is not None and collection_id is not None:
        raise HTTPException(
            status_code=400, detail="Pass collection_uuid or collection_id, not both"
        )
    # Every leg below filters by the RESOLVED id: one visibility rule, and a collection the
    # caller cannot see narrows to nothing instead of answering differently for it.
    collection_id = resolve_collection_filter(
        db,
        user_id=ctx.user.id,
        organization_id=ctx.org_id,
        collection_uuid=collection_uuid,
        collection_id=collection_id,
    )

    # Issue #760: `sources` wins outright over `result_type` when both are
    # supplied (never merged — two inputs that combine are one undebuggable
    # input). Omitting `sources` entirely preserves the legacy `result_type`
    # behavior byte-for-byte, including its default.
    explicit_sources, source_set = _resolve_source_set(sources, result_type)

    # OR-union (issue #760, J-760-10): a file matches if it matches ANY
    # selected source. The transcript leg and the summary leg are two
    # independent OpenSearch queries (§PC5 — content/title/speaker are FIELD
    # selectors on the transcript leg's one query, `summary` is a whole
    # separate leg); their result sets are returned side by side, never
    # AND-intersected against each other.
    want_transcripts = bool(source_set & {"content", "title", "speaker"})
    want_summaries = "summary" in source_set
    transcript_sources = source_set - {"summary"}

    if want_transcripts:
        from app.services.search.hybrid_search_service import HybridSearchService

        search_service = HybridSearchService()
        search_response = search_service.search(
            query=q,
            user_id=ctx.user.id,
            page=page,
            page_size=page_size,
            speakers=speakers,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            sort_order=sort_order,
            search_mode=search_mode,
            file_type=file_type,
            collection_id=collection_id,
            min_duration=min_duration,
            max_duration=max_duration,
            min_file_size=min_file_size,
            max_file_size=max_file_size,
            language=language,
            title_filter=title_filter,
            organization_id=ctx.org_id,
            file_uuid=file_uuid,
            sources=transcript_sources if explicit_sources else None,
        )

        # Abuse/DMCA: the OpenSearch transcript index has no quarantine field, so
        # drop any taken-down files from the result page against the DB (page-sized,
        # one IN query). Admins keep visibility for review.
        if not ctx.bypass.user_is_admin:
            search_response = _drop_quarantined_search_hits(db, search_response)

        payload = _search_response_to_schema(search_response)
        _attach_visible_collections(db, ctx, payload["results"])
    else:
        # Same shape a transcript-search response carries, with nothing found —
        # so a `summaries`-only caller still gets a well-formed SearchResponseSchema
        # and doesn't have to special-case an absent `results` key.
        payload = {
            "query": q,
            "results": [],
            "total_results": 0,
            "total_files": 0,
            "page": page,
            "page_size": page_size,
            "total_pages": 0,
            "search_time_ms": 0.0,
            "filters_applied": {},
            "search_mode": search_mode,
        }

    if want_summaries:
        # The SPA sends every filter on every tab, so the summary leg has to
        # honour the same ones the transcript leg above just did — otherwise one
        # request's two legs disagree about which files the caller asked for
        # (issue #831). Since #963 both legs route through the SAME filter
        # builder (`HybridSearchService._build_filters`), so there is no
        # longer a second SQL predicate implementation to keep in sync.
        # `date_from`/`date_to` were validated as ISO 8601 above, then reach
        # OpenSearch as raw strings.
        try:
            payload.update(
                _summary_search_payload(
                    db,
                    ctx,
                    q,
                    page,
                    page_size,
                    speakers=speakers,
                    tags=tags,
                    date_from=date_from,
                    date_to=date_to,
                    file_type=file_type,
                    collection_id=collection_id,
                    min_duration=min_duration,
                    max_duration=max_duration,
                    min_file_size=min_file_size,
                    max_file_size=max_file_size,
                    language=language,
                    title_filter=title_filter,
                    file_uuid=file_uuid,
                )
            )
        except (HTTPException, DomainError) as e:
            # Issue #760, §6: a masking outage must not blank the whole page
            # when more than one source was requested — only the summary leg
            # degrades. `_summary_search_payload` still raises a literal 503
            # for `SummaryMaskingUnavailableError`/`QuarantineExclusionUnavailableError`;
            # re-raise unchanged for a summary-ONLY request (nothing else to
            # show), degrade otherwise. Never "fail open" — this withholds
            # the summary results, it never serves them unmasked.
            if e.status_code == 503 and want_transcripts:
                payload["summary_results"] = []
                payload["summary_total"] = 0
                payload["summary_unavailable"] = "masking_unavailable"
            else:
                raise

    if want_summaries:
        # `total_pages` is the only paging signal the response carries, so it has
        # to reach the END OF EVERY LEG the request asked for.
        #
        # `summaries`-only: the transcript leg is what normally fills it and
        # never ran, so the placeholder built above left it hardcoded at 0 no
        # matter how many summary hits were found, and real pagination never
        # reached the client.
        #
        # `all` (issue #831 item 3): it was filled by the TRANSCRIPT leg alone,
        # from that leg's own `total_files`. Every summary page past the
        # transcript leg's last one was therefore unreachable — a client walking
        # 1..total_pages never asked for them, and those hits were silently
        # dropped. With zero transcript hits the transcript leg still reports
        # `max(1, …) == 1`, so a three-page summary result lost two thirds of
        # itself. The reverse direction was already correct and stays that way:
        # the summary leg's offset simply runs past its end and returns `[]`.
        #
        # ⚠️ There is deliberately NO single combined count, and this is not an
        # omission. `total_results`/`total_files` describe the transcript leg and
        # `summary_total` the summary leg; the two count different things
        # (transcript occurrences grouped by file, versus whole summaries), so
        # one merged number would have to misreport at least one of them. The
        # invariant that matters — and that the tests pin — is that each leg's
        # own total matches what that leg's pages actually walk, and that the
        # page count reaches the end of both. Nothing is interleaved into a
        # single ranked sequence either: an RRF fusion score and a `ts_rank` are
        # not on a common scale, so ordering them against each other would be a
        # fabricated ranking, not a combined one.
        summary_pages = math.ceil(payload.get("summary_total", 0) / page_size)
        if want_transcripts:
            payload["total_pages"] = max(int(payload.get("total_pages") or 0), summary_pages)
        else:
            payload["total_pages"] = summary_pages

    # Issue #760: the resolved source set and the per-source pill counts are
    # attached ONLY when the caller explicitly asked via `sources` — a legacy
    # `result_type` request stays byte-identical (no new keys, no extra cost).
    if explicit_sources:
        payload["sources"] = sorted(source_set)
        payload["source_counts"] = _source_counts(
            db,
            ctx,
            q,
            speakers=speakers,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            file_type=file_type,
            collection_id=collection_id,
            min_duration=min_duration,
            max_duration=max_duration,
            min_file_size=min_file_size,
            max_file_size=max_file_size,
            language=language,
            title_filter=title_filter,
            file_uuid=file_uuid,
        )

    return payload


@router.get("/count")
def search_match_count(
    q: str = Query(..., min_length=1, description="Search query"),
    file_uuid: str | None = Query(None, description="Scope the count to a single file (its UUID)."),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
) -> dict[str, int]:
    """Lightweight transcript match-count for the in-page find bar.

    Returns just ``{"total": N}`` — the number of transcript chunks matching ``q``
    (optionally within a single file). This is intentionally far cheaper than the full
    hybrid ``/search`` (no query embedding, RRF pipeline, snippets, or highlighting),
    so it stays fast under concurrent use: the find bar polls it as the user types to
    learn whether matches exist beyond the segments currently loaded in the browser.

    A ``file_uuid`` scope is resolved through the permission chokepoint BEFORE
    OpenSearch is ever touched (issue #817): without this, the endpoint answered
    a real chunk count for a file the caller could not otherwise see at all — a
    quarantined file, or one outside the caller's tenant — making the count a
    content oracle for exactly the recordings a takedown or the tenant gate
    exists to hide. The whole-corpus (no ``file_uuid``) case has no single
    resource to check permission against, so ``count_matches`` excludes
    quarantined files from the count itself instead.

    Raises:
        HTTPException: 404/403 from the permission chokepoint when ``file_uuid``
            names a file the caller cannot see; 503 when the quarantine
            exclusion — or the count query itself — could not be resolved.
    """
    from app.services.search.hybrid_search_service import HybridSearchService
    from app.services.search.hybrid_search_service import QuarantineExclusionUnavailableError
    from app.services.uuid_helpers import get_file_by_uuid_with_permission

    if file_uuid:
        # Raises 404/403 on its own — the same chokepoint every other
        # single-file read surface uses, and it must run before any
        # OpenSearch call this endpoint makes.
        get_file_by_uuid_with_permission(
            db,
            file_uuid,
            ctx.user.id,
            bypass=ctx.bypass,
            organization_id=ctx.org_id,
        )

    service = HybridSearchService()
    try:
        total = service.count_matches(
            q,
            user_id=ctx.user.id,
            file_uuid=file_uuid,
            organization_id=ctx.org_id,
            is_admin=ctx.bypass.user_is_admin,
        )
    except QuarantineExclusionUnavailableError as e:
        # Literal 503 to match get_available_filters' raise below; this module
        # does not import `fastapi.status`.
        raise HTTPException(
            status_code=503,
            detail="Search is temporarily unavailable.",
        ) from e
    return {"total": total}


@router.get("/suggestions")
def search_suggestions(
    q: str = Query(..., min_length=2, description="Search prefix"),
    limit: int = Query(8, ge=1, le=20, description="Max suggestions"),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
) -> list[dict[str, Any]]:
    """
    Auto-complete suggestions as user types.

    Returns ranked suggestions from title prefix matches,
    speaker name matches, and frequent content terms.

    Quarantined (DMCA/abuse takedown) files are excluded on BOTH the title and
    speaker legs inside ``HybridSearchService.get_suggestions`` itself (issue
    #817). This replaces a DB post-filter that used to live here and covered
    titles only — the speaker-name leg carries no file linkage to post-filter
    against, so a quarantined file's speaker names leaked into autocomplete
    with no way for this endpoint to catch it after the fact. See that
    method's docstring for the exclusion; this endpoint only shapes a
    resolution failure into a 503.

    Args:
        q: Search prefix text.
        limit: Maximum number of suggestions.

    Returns:
        List of suggestion items with type and text.

    Raises:
        HTTPException: 503 when the quarantine exclusion could not be applied.
    """
    from app.services.search.hybrid_search_service import HybridSearchService
    from app.services.search.hybrid_search_service import QuarantineExclusionUnavailableError

    search_service = HybridSearchService()
    try:
        return search_service.get_suggestions(
            prefix=q,
            user_id=ctx.user.id,
            limit=limit,
            organization_id=ctx.org_id,
            is_admin=ctx.bypass.user_is_admin,
        )
    except QuarantineExclusionUnavailableError as e:
        raise HTTPException(
            status_code=503,
            detail="Search suggestions are temporarily unavailable.",
        ) from e


@router.get("/filters")
def get_available_filters(
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
) -> dict[str, Any]:
    """
    Return available filter options (speakers, tags, date range).

    Quarantined (DMCA/abuse takedown) files are excluded from every facet for
    non-admins, including the file's own owner — same admin bypass as the
    results page's ``_drop_quarantined_search_hits``.

    That exclusion **fails closed** (issue #876): if the quarantined-file set
    cannot be resolved completely — the DB is unreachable, or there are more
    quarantined files than one ``terms`` clause can carry — this answers **503**
    rather than serving buckets that may have been built from taken-down
    content. Same posture as ``_summary_search_payload``'s masking outage above.

    Returns:
        Dict with speakers, tags, and date_range filter options.

    Raises:
        HTTPException: 503 when the quarantine exclusion could not be applied.
    """
    from app.services.search.hybrid_search_service import HybridSearchService
    from app.services.search.hybrid_search_service import QuarantineExclusionUnavailableError

    search_service = HybridSearchService()
    try:
        return search_service.get_available_filters(
            user_id=ctx.user.id, organization_id=ctx.org_id, is_admin=ctx.bypass.user_is_admin
        )
    except QuarantineExclusionUnavailableError as e:
        # Literal 503 to match _summary_search_payload's masking-outage raise
        # above; this module does not import `fastapi.status`.
        raise HTTPException(
            status_code=503,
            detail="Search filters are temporarily unavailable.",
        ) from e
