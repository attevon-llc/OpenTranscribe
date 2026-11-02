"""Shared helpers for the search query endpoints (response shaping, source counts,
quarantine post-filter, summary-plane payload)."""

import copy
import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.schemas.search import RESULT_TYPE_TO_SOURCES
from app.schemas.search import SEARCH_RESULT_TYPES
from app.schemas.search import SEARCH_SOURCES
from app.services.collection_visibility import collections_on_files
from app.services.request_context import RequestContext

logger = logging.getLogger(__name__)


def _search_response_to_schema(response) -> dict[str, Any]:
    """Convert HybridSearchService response to serializable dict.

    Carries an ``embedding_warning`` when the index is a PROVEN mix of two
    embedding models (#437). Until this, the mixed verdict had three readers —
    the status endpoint, the model-switch response and a beat-task log — and none
    of them is the person reading the results, so a mixed index went on ranking
    two incomparable vector populations against each other in silence. The
    advisory is deployment-level and TTL-cached (see
    ``embedding_provenance.search_provenance_advisory``), so an ordinary search
    pays nothing and the key is absent in the healthy case.
    """
    from app.services.search.embedding_provenance import search_provenance_advisory

    advisory = search_provenance_advisory()
    return {
        **({"embedding_warning": advisory} if advisory else {}),
        "query": response.query,
        "results": [
            {
                "file_uuid": hit.file_uuid,
                "file_id": hit.file_id,
                "title": hit.title,
                "speakers": hit.speakers,
                "tags": hit.tags,
                "upload_time": hit.upload_time,
                "language": hit.language,
                "content_type": hit.content_type,
                "relevance_score": hit.relevance_score,
                "occurrences": [
                    {
                        "snippet": occ.snippet,
                        "speaker": occ.speaker,
                        "speaker_highlighted": occ.speaker_highlighted,
                        "start_time": occ.start_time,
                        "end_time": occ.end_time,
                        "chunk_index": occ.chunk_index,
                        "score": occ.score,
                        "match_type": occ.match_type,
                        "has_keyword_match": occ.has_keyword_match,
                        "highlight_type": occ.highlight_type,
                    }
                    for occ in hit.occurrences
                ],
                "total_occurrences": hit.total_occurrences,
                "title_highlighted": hit.title_highlighted,
                "keyword_occurrences": hit.keyword_occurrences,
                "semantic_only": hit.semantic_only,
                "semantic_confidence": hit.semantic_confidence,
                "match_sources": hit.match_sources,
                "relevance_percent": hit.relevance_percent,
                "duration": hit.duration,
                "file_size": hit.file_size,
                "semantic_occurrences": hit.semantic_occurrences,
                "has_both_match_types": hit.has_both_match_types,
            }
            for hit in response.results
        ],
        "total_results": response.total_results,
        "total_files": response.total_files,
        "page": response.page,
        "page_size": response.page_size,
        "total_pages": response.total_pages,
        "search_time_ms": response.search_time_ms,
        "filters_applied": response.filters_applied,
        "search_mode": getattr(response, "search_mode", "hybrid"),
    }


def _attach_visible_collections(
    db: Session, ctx: RequestContext, hits: list[dict[str, Any]]
) -> None:
    """Add ``collections`` (``uuid`` + ``name``) to each hit, for the whole page in one batch.

    Per caller, after the (possibly shared, cached) search response was copied into
    ``hits``, so one user's collections never reach another user's cached page.
    """
    by_file = collections_on_files(
        db,
        [hit["file_id"] for hit in hits if hit.get("file_id")],
        ctx.user.id,
        organization_id=ctx.org_id,
    )
    for hit in hits:
        hit["collections"] = [
            {"uuid": str(collection.uuid), "name": collection.name}
            for collection in by_file.get(hit.get("file_id") or 0, [])
        ]


def _require_iso_date_bounds(**bounds: str | None) -> None:
    """400 on an unparseable date bound, for both search legs.

    OpenSearch would otherwise fail the query and the leg would degrade to an empty page,
    which reads as "nothing matched" rather than "your filter was invalid".
    """
    for name, bound in bounds.items():
        if bound is None:
            continue
        try:
            datetime.fromisoformat(bound)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"{name} must be an ISO 8601 date or datetime"
            ) from None


def _source_counts(
    db: Session,
    ctx: RequestContext,
    q: str,
    **filter_kwargs: Any,
) -> dict[str, int | None]:
    """Corpus-wide per-source match counts for the toggle pills (issue #760).

    Always computes all four, independent of which sources are selected — a
    disabled pill still shows what it would return. A failed count is
    ``None``, never ``0``: "0 matches" and "the count failed" must never be
    indistinguishable (the same #817 rule `search_match_count` follows).

    The summary count runs through the raw, UNMASKED `search_summary_plane`
    call (page_size=1, hits discarded) rather than through
    `_summary_search_payload`/`search_summaries` — counting must never pay
    (or depend on) a Presidio pass, or a masking outage would also blank the
    Summary pill's count, which is exactly the failure §6 exists to avoid.
    """
    from app.services.search.hybrid_search_service import HybridSearchService
    from app.services.search.hybrid_search_service import _quarantined_file_uuids
    from app.services.search.quarantine_exclusion import SearchCountUnavailableError

    service = HybridSearchService()
    counts: dict[str, int | None] = {
        "content": None,
        "title": None,
        "speaker": None,
        "summary": None,
    }

    for source, fields in (
        ("content", ["content", "content.exact"]),
        ("title", ["title"]),
        ("speaker", ["speaker"]),
    ):
        try:
            counts[source] = service.count_matches(
                q,
                user_id=ctx.user.id,
                organization_id=ctx.org_id,
                is_admin=ctx.bypass.user_is_admin,
                fields=fields,
                count_files=True,
                **filter_kwargs,
            )
        except SearchCountUnavailableError:
            logger.warning("source_counts: %s count unavailable", source)

    try:
        quarantined = [] if ctx.bypass.user_is_admin else _quarantined_file_uuids()
        _hits, total = service.search_summary_plane(
            q,
            ctx.user.id,
            organization_id=ctx.org_id,
            page=1,
            page_size=1,
            quarantined_file_uuids=quarantined,
            **filter_kwargs,
        )
        counts["summary"] = total
    except Exception:  # noqa: BLE001 — best-effort count, never a hard failure
        logger.warning("source_counts: summary count unavailable", exc_info=True)

    return counts


def _drop_quarantined_search_hits(db: Session, response: Any) -> Any:
    """Remove quarantined files from a search response (non-admin).

    The hidden files 404 on detail/stream anyway (the per-resource gate), so this
    just keeps them out of the result list/snippets too — the search-snippet
    redaction surface for takedowns.

    Returns a shallow COPY of ``response`` with the quarantined hits dropped and
    totals decremented — it must never mutate the object passed in. ``search()``
    can hand back a process-local cache hit BY REFERENCE
    (``hybrid_search_service._get_cached_response``), so mutating that object in
    place would mutate the shared cached entry itself: every later request
    against the same cache key — including an admin's, who must see the
    untrimmed page — would then see whatever this pass last left behind, and a
    file released after being cached would stay hidden until the cache entry's
    TTL naturally expires rather than reappearing on the next request. Callers
    must use the returned value; the input is left exactly as ``search()``
    produced it.
    """
    hits = getattr(response, "results", None) or []
    if not hits:
        return response
    from app.models.media import MediaFile

    uuids = [h.file_uuid for h in hits if getattr(h, "file_uuid", None)]
    if not uuids:
        return response
    quarantined = {
        str(row[0])
        for row in db.query(MediaFile.uuid)
        .filter(MediaFile.uuid.in_(uuids), MediaFile.is_quarantined.is_(True))
        .all()
    }
    if not quarantined:
        return response
    kept = [h for h in hits if str(h.file_uuid) not in quarantined]
    removed = len(hits) - len(kept)
    response = copy.copy(response)
    response.results = kept
    # Keep the reported totals consistent with the trimmed page.
    if hasattr(response, "total_results"):
        response.total_results = max(0, int(getattr(response, "total_results", 0)) - removed)
    if hasattr(response, "total_files"):
        response.total_files = max(0, int(getattr(response, "total_files", 0)) - removed)
    return response


def _summary_search_payload(
    db: Session,
    ctx: RequestContext,
    q: str,
    page: int,
    page_size: int,
    *,
    speakers: list[str] | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    file_type: list[str] | None = None,
    collection_id: int | None = None,
    min_duration: float | None = None,
    max_duration: float | None = None,
    min_file_size: int | None = None,
    max_file_size: int | None = None,
    language: str | None = None,
    title_filter: str | None = None,
    file_uuid: str | None = None,
) -> dict[str, Any]:
    r"""Build the ``summary_results``/``summary_total`` pair for issue #462, #963.

    Since #963 this queries the OpenSearch summary plane
    (``services/search/summary_search.search_summaries`` ->
    ``HybridSearchService.search_summary_plane``), not Postgres full-text
    search — see that module's docstring for the design. The filter kwargs
    below are forwarded verbatim into the SAME filter builder the transcript
    leg uses (`HybridSearchService._build_filters`), closing #831's whole
    defect class (one builder, not two).

    ⚠️ Deliberately UNCACHED — do not "finish the job" by adding a response
    cache here the way the transcript leg has one (issue #822's plan named this
    as a possible follow-up and it was rejected). A cached page is a cached
    verdict: this payload's shape already depends on the caller's redaction
    policy AND on quarantine state, and re-serving a page for
    `SEARCH_CACHE_TTL_SECONDS` after a file is quarantined would reopen #818 —
    a taken-down summary still readable, from cache, for the whole TTL. The
    transcript-search cache can afford this because that leg re-checks
    quarantine per request too (`_drop_quarantined_search_hits`); a summary
    cache would need the identical re-check on every read, which is exactly the
    round trip a cache exists to avoid. Not worth it for a corpus this small
    (one JSONB blob per file, per `search_summaries`' own docstring).

    **The same argument covers ACCESS, not only takedown.** Both authorities on
    this surface — `get_accessible_file_ids_subquery` and `exclude_quarantined`
    — are PRE-filters inside the query (#818 moved quarantine there so `total`
    and the page offsets stay honest). A pre-filter's verdict is therefore baked
    into the page, with no post-filter left downstream to re-apply it: unlike
    the transcript leg, there is nothing here that a cached page would still be
    re-checked by. Both authorities are time-varying by design, and neither is a
    property of the query text a key would be built from.

    ⚠️ **And the obvious key does not close it.** The transcript leg folds
    `hybrid_search_service._search_corpus_version()` into `_make_cache_key`, so
    reusing that counter here is the natural move — and it genuinely does cover
    the takedown case (`takedown_service.quarantine_file`/`release_file` both
    call `bump_corpus_version`). It is now bumped by exactly FOUR things, and
    those are all of them: chunk-plane indexing writes
    (`chat.retrieval_cache.bump_corpus_version`, from
    `index_transcript_chunks` and rename propagation), quarantine, release, and
    — as of the share-revocation cache-invalidation fix — an ACL rewrite via
    `tasks.search_file_field_tasks.update_file_access_index` (the single call site
    every collection-share grant/revocation and group-membership change
    dispatches through). A **summary regeneration** (`tasks/summarization.py`,
    which rewrites the very `media_file.summary_data` this leg reads) still
    bumps nothing. So a corpus-version-keyed cache would still keep serving the
    pre-regeneration summary for the rest of `SEARCH_CACHE_TTL_SECONDS` — while
    *reading* as though it were invalidated — even though the share-revocation
    half of this concern is now closed. Verify that call-site list before
    reconsidering (`rg 'bump_corpus_version\(\)' backend/app`); a cache here
    still needs a summary-regeneration invalidation signal that does not yet
    exist.

    Access control (since #963) is the OpenSearch-side ``accessible_user_ids``
    denormalized copy — the SAME authority and the SAME query
    (``HybridSearchService._build_filters``) the transcript leg uses — applied
    inside ``search_summaries`` -> ``search_summary_plane`` itself; this
    function does not re-derive visibility.

    Masking uses the REQUESTING user's policy (the read-surface rule from #85,
    the same subject the summary-detail endpoint already resolves) and fails
    CLOSED: a detector outage feeding one of the caller's enabled categories
    withholds these results with a 503 rather than serving an unmasked
    summary. Masking runs per-leaf, on each leaf actually RETURNED, before any
    snippet is extracted — see ``services/search/summary_search.py`` and
    ``redaction/summary_redaction.py`` for why batching leaks repeated names,
    and for why only the returned leaves are examined (issue #822).

    Quarantine is resolved from Postgres (fail-closed:
    ``hybrid_search_service._quarantined_file_uuids``) and applied INSIDE
    ``search_summary_plane`` as a ``must_not`` PRE-filter, so
    ``summary_total`` and the page offsets are consistent with what is
    returned. This function used to post-filter the hit list here; that left
    the count disclosing a taken-down file whose hit fell outside the
    requested page (#818).

    The date/tag/collection/… filter kwargs go the same way and for the same
    reason — into ``search_summary_plane``'s own query, never a pass over the
    returned hits. Applying them here instead would leave ``summary_total``
    counting files the page had just removed, which is the #818 shape
    exactly.
    """
    from app.services.redaction.config import resolve_effective_config
    from app.services.redaction.summary_redaction import SummaryMaskingUnavailableError
    from app.services.search.quarantine_exclusion import QuarantineExclusionUnavailableError
    from app.services.search.summary_search import search_summaries

    cfg = resolve_effective_config(db, ctx.user.id, organization_id=ctx.org_id)
    try:
        result = search_summaries(
            db,
            q,
            ctx.user.id,
            organization_id=ctx.org_id,
            page=page,
            page_size=page_size,
            redaction_cfg=cfg,
            include_quarantined=ctx.bypass.user_is_admin,
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
    except SummaryMaskingUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except QuarantineExclusionUnavailableError as e:
        # Literal 503, matching this module's other quarantine-unavailable
        # arms — silently searching without the exclusion is a takedown
        # bypass, not a degraded result.
        raise HTTPException(
            status_code=503, detail="The summary search exclusion list is unavailable."
        ) from e

    return {
        "summary_results": [
            {
                "file_uuid": hit.file_uuid,
                "file_id": hit.file_id,
                "title": hit.title,
                "matches": [
                    {"key_path": m.key_path, "snippet": m.snippet, "score": m.score}
                    for m in hit.matches
                ],
            }
            for hit in result.results
        ],
        "summary_total": result.total,
    }


def _require_valid_sort_and_mode(sort_by: str, sort_order: str, search_mode: str) -> None:
    """400 on an unsupported ``sort_by``, ``sort_order`` or ``search_mode``."""
    valid_sort_fields = (
        "relevance",
        "upload_time",
        "completed_at",
        "filename",
        "duration",
        "file_size",
    )
    if sort_by not in valid_sort_fields:
        raise HTTPException(
            status_code=400,
            detail=f"sort_by must be one of: {', '.join(valid_sort_fields)}",
        )

    if sort_order not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail="sort_order must be: asc or desc")

    if search_mode not in ("hybrid", "keyword"):
        raise HTTPException(status_code=400, detail="search_mode must be: hybrid or keyword")


def _resolve_source_set(sources: list[str] | None, result_type: str) -> tuple[bool, frozenset[str]]:
    """Resolve the requested result sources; ``(explicit_sources, source_set)``.

    Issue #760: ``sources`` wins outright over ``result_type`` when both are
    supplied (never merged). Omitting ``sources`` preserves the legacy
    ``result_type`` behavior, including its default.
    """
    explicit_sources = sources is not None
    if explicit_sources:
        if not sources or any(s not in SEARCH_SOURCES for s in sources):
            raise HTTPException(
                status_code=400,
                detail=f"sources must be one or more of: {', '.join(SEARCH_SOURCES)}",
            )
        return True, frozenset(sources)
    if result_type not in SEARCH_RESULT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"result_type must be one of: {', '.join(SEARCH_RESULT_TYPES)}",
        )
    return False, RESULT_TYPE_TO_SOURCES[result_type]
