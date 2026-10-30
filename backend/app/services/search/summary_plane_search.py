"""The raw summary-plane query of ``transcript_chunks`` (issue #963); masking is the caller's."""

import logging
from typing import TYPE_CHECKING
from typing import Any

from app.core.config import settings
from app.core.constants import SEARCH_MAX_SNIPPETS_PER_FILE
from app.services.ingest_artifacts.index_mapping import summary_plane_clause

if TYPE_CHECKING:  # pragma: no cover - import cost paid only by type checkers
    from app.services.search.hybrid_search_service import HybridSearchService

logger = logging.getLogger(__name__)


#: Field boosts for the summary plane (issue #963) — a sibling of
#: :meth:`HybridSearchService._get_search_fields`. The summary plane has no
#: single-valued ``speaker`` field by construction (a summary leaf is not
#: attributable to one speaker), so ``speakers`` (plural) stands in.
_SUMMARY_SEARCH_FIELDS = ["content^3", "content.exact^2", "title^2", "speakers^1"]


def search_summary_plane(
    service: "HybridSearchService",
    query: str,
    user_id: int,
    *,
    organization_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
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
    quarantined_file_uuids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Search the summary plane of ``transcript_chunks`` (issue #963).

    A second, independent hybrid (BM25 + kNN, RRF-fused) query against the
    **same index**, scoped to :func:`summary_plane_clause` instead of the
    chunk plane. Reuses :meth:`_build_filters` (ACL/tenant/metadata
    filters — the single source #831 closed), :meth:`_build_text_query`
    (BM25), and the same fusion-pipeline machinery
    :func:`ensure_fusion_pipeline` / :meth:`_get_neural_model_id` the
    transcript leg uses. **Never merged into the transcript leg's ranked
    sequence** — an RRF score and this leg's RRF score ARE now on a common
    scale, but the deliberate decision to keep two separate result lists
    (``api/endpoints/search.py``) stands independently, for RRF-competition
    reasons (#462 rejection 4).

    ``quarantined_file_uuids`` is resolved by the caller from Postgres —
    the chunks index carries no ``is_quarantined`` field — and applied
    here as a ``must_not`` PRE-filter, so ``total`` and the page are
    consistent with each other (#818: a post-filter cannot correct either).

    Returns:
        ``(hits, total)`` where each hit is a raw dict:
        ``{"file_uuid", "file_id", "title", "matches": [{"key_path",
        "content", "score"}, ...]}``. Masking and snippet truncation are
        the caller's job (``summary_search.search_summaries``) — this
        method returns the RAW indexed text, never masked (§5 of the
        design: the summary plane stores raw text and masking happens at
        read time, exactly like the chunk plane).
    """
    client = service._client()
    if not client:
        return [], 0

    filters = service._build_filters(
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
        plane_clause=summary_plane_clause(),
        speaker_field="speakers",
    )
    if quarantined_file_uuids:
        filters.append({"bool": {"must_not": {"terms": {"file_uuid": quarantined_file_uuids}}}})

    clean_query = query.strip() if query else ""
    text_query_clause = service._build_text_query(clean_query, _SUMMARY_SEARCH_FIELDS)

    pipeline_id = service._infrastructure()
    # Same gate as the transcript leg (``_generate_query_embedding``): a deployment with
    # neural search disabled must get a plain BM25 query, not a hybrid one built from
    # whichever model id the cluster happens to still hold.
    use_neural = bool(clean_query) and service._check_neural_search_available()
    model_id = service._get_neural_model_id() if use_neural else None

    collapse_config: dict[str, Any] = {
        "field": "file_uuid",
        "inner_hits": {
            "name": "leaves",
            "size": SEARCH_MAX_SNIPPETS_PER_FILE,
            "sort": [{"_score": {"order": "desc"}}],
            "_source": {"includes": ["summary_key_path", "content"]},
        },
    }

    needs_pipeline = False
    if model_id and clean_query:
        neural_clause = {
            "bool": {
                "must": [
                    {
                        "neural": {
                            "embedding": {
                                "query_text": clean_query,
                                "model_id": model_id,
                                "k": settings.SEARCH_RRF_WINDOW_SIZE,
                            }
                        }
                    }
                ],
                "filter": filters,
            }
        }
        body: dict[str, Any] = {
            "size": min(page * page_size + page_size * 5, settings.SEARCH_MAX_OVERFETCH),
            "query": {
                "hybrid": {
                    "queries": [
                        {"bool": {"must": [text_query_clause], "filter": filters}},
                        neural_clause,
                    ]
                }
            },
            "collapse": collapse_config,
            "_source": {"excludes": ["embedding"]},
            "track_total_hits": False,
        }
        needs_pipeline = True
    else:
        body = {
            "size": min(page * page_size + page_size * 5, settings.SEARCH_MAX_OVERFETCH),
            "query": {"bool": {"must": [text_query_clause], "filter": filters}},
            "collapse": collapse_config,
            "_source": {"excludes": ["embedding"]},
            "track_total_hits": False,
        }

    try:
        response = client.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body=body,
            params={"search_pipeline": pipeline_id} if needs_pipeline else {},
        )
    except Exception as exc:
        logger.warning(f"Summary plane search failed: {exc}")
        return [], 0

    top_hits = response.get("hits", {}).get("hits", [])
    ranked: list[tuple[float, str, str, dict[str, Any]]] = []
    for hit in top_hits:
        source = hit.get("_source", {})
        inner = hit.get("inner_hits", {}).get("leaves", {}).get("hits", {}).get("hits", [])
        matches = [
            {
                "key_path": leaf.get("_source", {}).get("summary_key_path", ""),
                "content": leaf.get("_source", {}).get("content", ""),
                "score": float(leaf.get("_score") or 0.0),
            }
            for leaf in inner
        ]
        ranked.append(
            (
                float(hit.get("_score") or 0.0),
                str(source.get("upload_time") or ""),
                str(source.get("file_uuid") or ""),
                {
                    "file_uuid": source.get("file_uuid"),
                    "file_id": source.get("file_id"),
                    "title": source.get("title") or "",
                    "matches": matches,
                },
            )
        )

    # Ties are the common case on short summaries, and OpenSearch breaks them by internal
    # doc order -- stable for one index state but neither newest-first nor a total order
    # the caller can rely on. ``sort`` criteria beyond ``_score`` are not allowed under the
    # RRF pipeline, so break ties here: best score, then newest upload, then file_uuid.
    # Three STABLE sorts, least significant key first.
    ranked.sort(key=lambda r: r[2])
    ranked.sort(key=lambda r: r[1], reverse=True)
    ranked.sort(key=lambda r: r[0], reverse=True)
    results: list[dict[str, Any]] = [r[3] for r in ranked]

    total = len(results)
    start = max(0, (page - 1) * page_size)
    page_results = results[start : start + page_size]
    return page_results, total
