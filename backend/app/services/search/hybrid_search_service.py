"""Hybrid BM25 + vector search service using OpenSearch 3.4 native features.

``HybridSearchService`` is the entry point; the query planes it drives live in sibling
modules (``collapsed_search``, ``split_bm25``, ``two_phase_search``,
``summary_plane_search``, ``suggestions``) and the request pieces in ``filters``,
``query_building``, ``collapse_bodies``, ``result_processing`` and ``highlighting``, with
``search_cache``, ``search_infra``, ``quarantine_exclusion`` and ``search_types`` beside
them. This module keeps the orchestration, the neural-availability probe and the
corpus-wide match count.
"""

import logging
import threading
import time
from typing import TYPE_CHECKING
from typing import Any

from app.core.config import settings
from app.core.constants import SEARCH_DEFAULT_PAGE_SIZE
from app.core.constants import SEARCH_MAX_PAGE_SIZE
from app.services.opensearch_service import get_opensearch_client
from app.services.search import collapse_bodies
from app.services.search import collapsed_search
from app.services.search import filters as search_filters
from app.services.search import highlighting
from app.services.search import query_building
from app.services.search import result_processing
from app.services.search import snippet_redaction
from app.services.search import split_bm25
from app.services.search import suggestions
from app.services.search import summary_plane_search
from app.services.search import two_phase_search
from app.services.search.filters import _collect_filters_applied
from app.services.search.fusion import FusionConfig
from app.services.search.fusion import search_pipeline_id
from app.services.search.quarantine_exclusion import SearchCountUnavailableError
from app.services.search.quarantine_exclusion import _quarantined_file_uuids
from app.services.search.query_building import _parse_query_operators
from app.services.search.search_cache import _get_cached_response
from app.services.search.search_cache import _make_cache_key
from app.services.search.search_cache import _redaction_policy_fingerprint
from app.services.search.search_cache import _resolve_redaction_config_for_cache
from app.services.search.search_cache import _search_corpus_version
from app.services.search.search_cache import _set_cached_response
from app.services.search.search_infra import _ensure_infrastructure
from app.services.search.search_types import SearchResponse

if TYPE_CHECKING:  # pragma: no cover - import cost paid only by type checkers
    from opensearchpy import OpenSearch

logger = logging.getLogger(__name__)


_neural_search_available: bool | None = None
_neural_search_check_time: float = 0.0
_NEURAL_SEARCH_CACHE_TTL: float = 120.0  # Re-check every 2 minutes (success)
_NEURAL_SEARCH_FAILURE_TTL: float = 30.0  # Re-check every 30 seconds (failure)

# Lock for module-level state mutations
_state_lock = threading.Lock()


def reset_neural_search_state() -> None:
    """Reset the neural search availability state.

    Call this when switching models or after configuration changes.
    """
    global _neural_search_available
    global _neural_search_check_time
    _neural_search_available = None
    _neural_search_check_time = 0.0
    logger.info("Neural search state reset")


class HybridSearchService:
    """Executes hybrid BM25 + vector search with RRF via OpenSearch 3.4 native pipeline.

    The query planes and request builders live in sibling modules and are bound here, so
    every caller keeps one method surface. A function that needs the service back (a
    sibling method, or :meth:`_client`) takes it as its first argument and binds as a
    method; the rest are static.
    """

    _redact_snippets = staticmethod(snippet_redaction._redact_snippets)
    _sort_and_paginate = staticmethod(result_processing._sort_and_paginate)
    get_suggestions = staticmethod(suggestions.get_suggestions)
    get_available_filters = staticmethod(suggestions.get_available_filters)
    _build_filters = staticmethod(search_filters._build_filters)
    search_summary_plane = summary_plane_search.search_summary_plane
    _build_highlight_fields = staticmethod(query_building._build_highlight_fields)
    _build_text_query = staticmethod(query_building._build_text_query)
    _get_search_fields = staticmethod(query_building._get_search_fields)
    _apply_sort_clause = staticmethod(collapse_bodies._apply_sort_clause)
    _build_collapsed_search_body = collapse_bodies._build_collapsed_search_body
    _bm25_leg_is_starved = collapse_bodies._bm25_leg_is_starved
    _build_collapsed_bm25_body = staticmethod(collapse_bodies._build_collapsed_bm25_body)
    _execute_split_bm25_collapse = staticmethod(split_bm25._execute_split_bm25_collapse)
    _hydrate_page_highlights = staticmethod(split_bm25._hydrate_page_highlights)
    _detect_keyword_match_fallback = staticmethod(highlighting._detect_keyword_match_fallback)
    _process_inner_hits = staticmethod(result_processing._process_inner_hits)
    _normalize_relevance_percent = staticmethod(result_processing._normalize_relevance_percent)
    _apply_semantic_demotion = staticmethod(result_processing._apply_semantic_demotion)
    _backfill_starved_groups = staticmethod(collapsed_search._backfill_starved_groups)
    _process_collapsed_results = staticmethod(result_processing._process_collapsed_results)
    _phase2_lookup = staticmethod(two_phase_search._phase2_lookup)
    _sort_buckets = staticmethod(two_phase_search._sort_buckets)
    _build_search_hit_from_bucket = staticmethod(two_phase_search._build_search_hit_from_bucket)
    _apply_semantic_highlights = staticmethod(highlighting._apply_semantic_highlights)
    _search_with_two_phase = two_phase_search._search_with_two_phase
    _search_with_collapse = collapsed_search._search_with_collapse

    @staticmethod
    def _client() -> "OpenSearch | None":
        """The OpenSearch client for every query plane, looked up in this module per call.

        The plane modules take the service and ask it, so this module's
        ``get_opensearch_client`` is the one lookup (and the one stand-in point) for
        all the requests a single search makes.
        """
        return get_opensearch_client()

    @staticmethod
    def _infrastructure(fusion: FusionConfig | None = None) -> str:
        """This module's ``_ensure_infrastructure``, looked up per call (see :meth:`_client`)."""
        return _ensure_infrastructure(fusion)

    def search(
        self,
        query: str,
        user_id: int,
        page: int = 1,
        page_size: int = SEARCH_DEFAULT_PAGE_SIZE,
        speakers: list[str] | None = None,
        tags: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort_by: str = "relevance",
        sort_order: str = "desc",
        search_mode: str = "hybrid",
        file_type: list[str] | None = None,
        collection_id: int | None = None,
        min_duration: float | None = None,
        max_duration: float | None = None,
        min_file_size: int | None = None,
        max_file_size: int | None = None,
        language: str | None = None,
        title_filter: str | None = None,
        organization_id: int | None = None,
        file_uuid: str | None = None,
        fusion: FusionConfig | None = None,
        sources: frozenset[str] | None = None,
    ) -> SearchResponse:
        """Execute hybrid search and return grouped results.

        Args:
            query: Search query text.
            user_id: Current user ID for filtering.
            fusion: Hybrid fusion strategy for **this request** (#363). None uses
                the configured default. The resolved pipeline id is part of the
                response cache key, so two strategies cannot serve each other's
                cached page.
            organization_id: Active org id (None = personal). Adds the tenant gate
                so cross-org transcript content never surfaces.
            page: Page number (1-indexed).
            page_size: Results per page.
            speakers: Optional speaker filter list.
            tags: Optional tag filter list.
            date_from: Optional start date filter (ISO format).
            date_to: Optional end date filter (ISO format).
            sort_by: Sort field - relevance, upload_time, completed_at, filename, duration, file_size.
            sort_order: Sort direction - asc or desc.
            title_filter: Optional filename/title substring filter.

        Returns:
            SearchResponse with grouped results.
        """
        client = get_opensearch_client()
        if not client:
            logger.warning("OpenSearch client not initialized")
            return self._empty_response(query, page, page_size)

        start_time = time.time()
        page_size = min(page_size, SEARCH_MAX_PAGE_SIZE)

        # Redaction policy resolved BEFORE the cache lookup, not after (#86-
        # adjacent fix): the response this method is about to cache gets masked
        # under this policy, so the cache key must name it. Resolving it only at
        # `_redact_snippets` time — the pre-fix behaviour — meant a policy change
        # (a user flips masking, or the admin floor changes) took up to
        # `SEARCH_CACHE_TTL_SECONDS` to take effect on a repeated query, and two
        # policies could collide on one key if `user_id` were ever reused for a
        # tenant-shared cache in the future.
        redaction_cfg = _resolve_redaction_config_for_cache(
            user_id, organization_id=organization_id
        )
        policy_fingerprint = _redaction_policy_fingerprint(redaction_cfg)

        # Check cache
        cache_key = _make_cache_key(
            query=query,
            user_id=user_id,
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
            organization_id=organization_id,
            file_uuid=file_uuid,
            fusion_pipeline=search_pipeline_id(fusion),
            redaction_policy=policy_fingerprint,
            corpus_version=_search_corpus_version(),
            # Issue #760: a cached page must name the sources it was built
            # for, or toggling a pill re-serves the previous page for
            # SEARCH_CACHE_TTL_SECONDS.
            sources=sorted(sources) if sources is not None else None,
        )
        cached = _get_cached_response(cache_key)
        if cached:
            return cached

        pipeline_id = _ensure_infrastructure(fusion)

        # Parse inline query operators (e.g., speaker:"Joe Rogan" china)
        clean_query, operators = _parse_query_operators(query)
        if "speaker" in operators:
            speakers = list(speakers or []) + [operators["speaker"]]
        search_query = clean_query.strip() if clean_query else ""

        # Debug logging
        logger.info(
            f"SEARCH: original='{query}', clean='{clean_query}', search_query='{search_query}', speakers={speakers}"
        )

        # Build filters
        filters = self._build_filters(
            user_id,
            speakers,
            tags,
            date_from,
            date_to,
            file_type=file_type,
            collection_id=collection_id,
            min_duration=min_duration,
            max_duration=max_duration,
            min_file_size=min_file_size,
            max_file_size=max_file_size,
            language=language,
            title_filter=title_filter,
            organization_id=organization_id,
            file_uuid=file_uuid,
        )
        filters_applied = _collect_filters_applied(
            speakers=speakers,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            file_type=file_type,
            collection_id=collection_id,
            language=language,
            title_filter=title_filter,
        )

        # Determine search capabilities
        query_embedding, use_hybrid, use_neural = self._generate_query_embedding(
            search_query, search_mode
        )
        has_speaker_filter = bool(speakers)

        # Issue #760, §5: a semantic (kNN) hit cannot be attributed to a
        # field — the embedding covers title/speaker roster/content all at
        # once — so it is unattributable to Title-only or Speaker-only. When
        # `content` was not selected, force keyword-only: no kNN leg, no
        # fusion pipeline, so nothing unsupportable is ever returned.
        if sources is not None and "content" not in sources:
            use_neural = False

        result = self._search_with_collapse(
            query=query,
            search_query=search_query,
            filters=filters,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            search_mode=search_mode,
            filters_applied=filters_applied,
            start_time=start_time,
            has_speaker_filter=has_speaker_filter,
            use_neural=use_neural,
            search_pipeline=pipeline_id,
            sources=sources,
        )

        # Read-time content redaction of snippets, for whichever of PII / profanity /
        # custom words this user's policy masks. Applied before caching so the
        # cache (keyed on user_id AND the policy fingerprint above) holds the
        # masked version. `redaction_cfg` is the SAME resolution used for the
        # cache key above — passed through rather than re-resolved, so the
        # config that decided the key is provably the config that did the
        # masking (one DB round trip, not two, and no window for the two to
        # silently disagree).
        self._redact_snippets(result, user_id, redaction_cfg)

        # Cache the response — but NOT if it fell back to BM25-only due to
        # a transient error, so the next request retries hybrid properly.
        if not result._fell_back_to_bm25:
            _set_cached_response(cache_key, result)
        else:
            logger.info("Skipping cache for BM25-fallback response (query='%s')", query)

        return result

    def _check_neural_search_available(self) -> bool:
        """Check if neural search is available in OpenSearch.

        Caches the result to avoid repeated checks. Success is cached for
        _NEURAL_SEARCH_CACHE_TTL (120s); failure is cached for the shorter
        _NEURAL_SEARCH_FAILURE_TTL (30s) so the system recovers quickly
        from transient errors.

        Returns:
            True if neural search is available and a model is deployed.
        """
        global _neural_search_available
        global _neural_search_check_time

        if _neural_search_available is not None:
            ttl = (
                _NEURAL_SEARCH_CACHE_TTL if _neural_search_available else _NEURAL_SEARCH_FAILURE_TTL
            )
            if time.time() - _neural_search_check_time < ttl:
                return _neural_search_available
            # TTL expired — reset to force re-check
            _neural_search_available = None

        with _state_lock:
            # Double-check after acquiring lock
            if _neural_search_available is not None:
                ttl = (
                    _NEURAL_SEARCH_CACHE_TTL
                    if _neural_search_available
                    else _NEURAL_SEARCH_FAILURE_TTL
                )
                if time.time() - _neural_search_check_time < ttl:
                    return _neural_search_available
                _neural_search_available = None

            if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
                _neural_search_available = False
                return False

            try:
                from .ml_model_service import get_ml_model_service

                ml_service = get_ml_model_service()
                model_id = ml_service.get_active_model_id()
                _neural_search_available = model_id is not None
                _neural_search_check_time = time.time()
                if _neural_search_available:
                    logger.info(f"Neural search available with model: {model_id}")
                else:
                    logger.info("Neural search not available - no deployed model")
                return _neural_search_available
            except Exception as e:
                logger.warning(f"Could not check neural search availability: {e}")
                _neural_search_available = False
                _neural_search_check_time = time.time()
                return False

    def _get_neural_model_id(self) -> str | None:
        """Get the active neural model ID.

        Returns:
            Model ID string or None if not available.
        """
        try:
            from .ml_model_service import get_ml_model_service

            ml_service = get_ml_model_service()
            return ml_service.get_active_model_id()
        except Exception as e:
            logger.warning(f"Could not get neural model ID: {e}")
            return None

    def _generate_query_embedding(
        self,
        query: str,
        search_mode: str,
    ) -> tuple[None, bool, bool]:
        """Check if hybrid/neural search should be used.

        Neural search generates embeddings server-side in OpenSearch,
        so no client-side embedding is needed.

        Args:
            query: Search query text.
            search_mode: Search mode - "keyword" skips semantic search.

        Returns:
            Tuple of (None, whether hybrid mode is active, whether to use neural query).
        """
        if search_mode == "keyword":
            return None, False, False

        # Check if neural search is available
        if self._check_neural_search_available():
            # Neural mode: OpenSearch generates embeddings server-side
            return None, True, True

        # Neural search not available, fall back to BM25-only
        logger.warning("Neural search not available, using BM25-only mode")
        return None, False, False

    def count_matches(
        self,
        query: str,
        user_id: int,
        file_uuid: str | None = None,
        organization_id: int | None = None,
        is_admin: bool = False,
        fields: list[str] | None = None,
        count_files: bool = False,
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
    ) -> int:
        """Count transcript chunks matching ``query`` (optionally within one file).

        A deliberately lightweight path for the in-page transcript find bar. It runs a
        single ``size=0`` OpenSearch query — no query embedding, no RRF pipeline, no
        snippet extraction, no highlighting — so it stays cheap even when many users
        search at once (FastAPI runs this sync call in its threadpool and OpenSearch
        serves the searches concurrently). Returns the exact matching-chunk count
        (``track_total_hits``), used purely as a "matches exist beyond the loaded
        window" signal for progressive loading.

        ``client is None`` and an empty/whitespace ``query`` both still return ``0``
        rather than raising — the first is "lite mode" (no OpenSearch client at all),
        the second is "nothing to search for". Neither is a quarantine-exclusion
        failure and neither has content to withhold, so ``0`` is the honest answer.

        Args:
            query: The search text.
            user_id: Current user id — scopes to ``accessible_user_ids``.
            file_uuid: Optional single-file scope. Callers (``search.py``) resolve
                this through ``get_file_by_uuid_with_permission`` BEFORE calling
                here, which already 404s a quarantined file — so this method does
                not additionally apply the quarantine exclusion when scoped to one
                file; there is nothing left for it to hide.
            organization_id: Active org id (None = personal) — tenant gate.
            is_admin: When True, skip the quarantine exclusion — the admin review
                bypass every other quarantine-aware read surface applies.
            fields: OpenSearch fields to query (defaults to content/content.exact,
                the find-bar's original behaviour). Issue #760 reuses this method
                for the per-source pill counts by passing ``["title"]`` /
                ``["speaker"]`` — never masked, since it counts, never renders.
            count_files: When True, count DISTINCT matching files (a `cardinality`
                agg on `file_uuid`, HyperLogLog++ — exact at/below 10,000 distinct
                files) instead of matching chunks. The pill counts (#760) must
                count files, matching what `total_files` already means elsewhere
                on this page; the find bar's original chunk count is unchanged
                (`count_files=False` default).
            speakers, tags, date_from, date_to, file_type, collection_id,
                min_duration, max_duration, min_file_size, max_file_size,
                language, title_filter: The SAME filter set `search()` accepts —
                so a per-source pill count reflects the filters currently active
                on the page it labels, not the whole unfiltered corpus. All
                default to None (unfiltered), matching the find bar's original
                behaviour when the caller passes none of them.

        Returns:
            The exact matching-chunk count, or (``count_files=True``) the
            approximate-above-10,000 distinct-file count.

        Raises:
            SearchCountUnavailableError: The quarantine exclusion set could not be
                resolved, or the OpenSearch query itself failed. Either way this
                count cannot be trusted (issue #817) — reporting it as ``0`` would
                make this endpoint a content oracle for a file the caller cannot
                see, since "0 matches" and "the search itself failed" would be
                indistinguishable to the find bar.
        """
        clean = (query or "").strip()
        if not clean:
            return 0
        client = get_opensearch_client()
        if not client:
            return 0

        _ensure_infrastructure()
        filters = self._build_filters(
            user_id,
            speakers,
            tags,
            date_from,
            date_to,
            file_type=file_type,
            collection_id=collection_id,
            min_duration=min_duration,
            max_duration=max_duration,
            min_file_size=min_file_size,
            max_file_size=max_file_size,
            language=language,
            title_filter=title_filter,
            file_uuid=file_uuid,
            organization_id=organization_id,
        )
        # Same posture as `get_available_filters`: quarantine is Postgres-only,
        # so an unscoped (whole-corpus) count has to exclude taken-down files
        # itself. A file-scoped count needs no exclusion of its own — the
        # caller's permission check already refused the request if that one
        # file is quarantined.
        quarantined_uuids = [] if (is_admin or file_uuid) else _quarantined_file_uuids()
        query_must_not = [{"terms": {"file_uuid": quarantined_uuids}}] if quarantined_uuids else []

        text_query = self._build_text_query(clean, fields or ["content", "content.exact"])
        body: dict[str, Any] = {
            "size": 0,
            "track_total_hits": not count_files,
            "query": {
                "bool": {
                    "filter": filters,
                    "must": [text_query],
                    **({"must_not": query_must_not} if query_must_not else {}),
                }
            },
        }
        if count_files:
            # Same shape as `_build_collapsed_bm25_body`'s `total_files` agg —
            # exact at/below 10,000 distinct files, approximate above it.
            body["aggs"] = {
                "total_files": {"cardinality": {"field": "file_uuid", "precision_threshold": 10000}}
            }
        try:
            resp = client.search(index=settings.OPENSEARCH_CHUNKS_INDEX, body=body)
        except Exception as exc:
            logger.warning(f"count_matches failed: {exc}")
            raise SearchCountUnavailableError(
                "The transcript match count could not be computed."
            ) from exc

        if count_files:
            return int(resp.get("aggregations", {}).get("total_files", {}).get("value", 0))
        total = resp.get("hits", {}).get("total", {})
        if isinstance(total, dict):
            return int(total.get("value", 0))
        return int(total or 0)

    def _empty_response(self, query: str, page: int, page_size: int) -> SearchResponse:
        """Return an empty search response."""
        return SearchResponse(
            query=query,
            results=[],
            total_results=0,
            total_files=0,
            page=page,
            page_size=page_size,
            total_pages=0,
            search_time_ms=0.0,
        )
