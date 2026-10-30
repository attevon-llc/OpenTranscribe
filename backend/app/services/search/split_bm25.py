"""Keyword (BM25-only) collapse executed as two requests, highlights for the page only (#1064)."""

import copy
import logging
from typing import Any

from app.core.config import settings
from app.services.search.result_processing import _process_inner_hits
from app.services.search.search_types import SearchHit

logger = logging.getLogger(__name__)


def _execute_split_bm25_collapse(
    client: Any, body: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run a BM25 collapse body as two requests instead of one (issue #1064).

    ``collapse.inner_hits`` is executed by OpenSearch as one sub-search PER
    collapsed group, and every sub-search re-parses the query, rebuilds the
    fuzzy clauses' Levenshtein automata for its rewrite, then rebuilds them
    again to set up the highlighter. That is a fixed cost per group, so it
    multiplies by the relevance over-fetch (200 groups at page 1) and does not
    shrink with the corpus: measured on a 2-vCPU node, a 2,000-chunk index
    answered a minimal ``match`` in ~10 ms and this body in 0.4-2.2 s. A
    ``neural`` query has no terms to rewrite or highlight, which is why the
    semantic path never showed it.

    Instead:

    1. the same body **without** ``inner_hits`` (and without the outer
       ``highlight``, which nothing reads) picks the groups, ranking and the
       ``total_files`` agg exactly as before;
    2. one ``terms`` + ``top_hits`` aggregation over those groups returns
       each file's top segments — same query, same ``sort``/``size``/
       ``_source`` as the inner hits, so the same documents with the same
       scores (``doc_count`` is the inner-hits total) — with the query
       rewritten once, and **without highlights**.

    The segments are grafted back onto the outer hits in the inner-hits
    shape, so ``_process_collapsed_results`` reads the response unchanged.
    Highlights are fetched afterwards for the displayed page only
    (``_hydrate_page_highlights``): highlighting costs ~0.25 ms per segment
    even in one request, and the over-fetched groups never display theirs.
    What those groups fed — keyword counts — needs none, since every hit of
    a BM25 body matched the text query.

    Args:
        client: OpenSearch client.
        body: A body from ``_build_collapsed_bm25_body``. Not mutated.

    Returns:
        (collapsed response with inner hits grafted on, the inner hits'
        highlight config to use for the page).
    """
    body = copy.deepcopy(body)
    inner = body["collapse"].pop("inner_hits")
    body["collapse"].pop("max_concurrent_group_searches", None)
    body.pop("highlight", None)
    response: dict[str, Any] = client.search(index=settings.OPENSEARCH_CHUNKS_INDEX, body=body)

    outer_hits = response.get("hits", {}).get("hits", [])
    uuids = [u for h in outer_hits if (u := h.get("_source", {}).get("file_uuid"))]
    buckets: dict[str, dict[str, Any]] = {}
    if uuids:
        query_bool = body["query"]["bool"]
        segments_body = {
            "size": 0,
            "track_total_hits": False,
            "query": {
                "bool": {
                    "must": query_bool["must"],
                    "filter": [*query_bool["filter"], {"terms": {"file_uuid": uuids}}],
                }
            },
            "aggs": {
                "files": {
                    "terms": {"field": "file_uuid", "size": len(uuids)},
                    "aggs": {
                        "segments": {
                            "top_hits": {
                                "size": inner["size"],
                                "sort": inner["sort"],
                                "_source": inner["_source"],
                            }
                        }
                    },
                }
            },
        }
        seg_resp = client.search(index=settings.OPENSEARCH_CHUNKS_INDEX, body=segments_body)
        for bucket in seg_resp.get("aggregations", {}).get("files", {}).get("buckets", []):
            buckets[bucket["key"]] = bucket

    for hit in outer_hits:
        bucket = buckets.get(hit.get("_source", {}).get("file_uuid"), {})
        hit["inner_hits"] = {
            "segments": {
                "hits": {
                    "total": {"value": bucket.get("doc_count", 0)},
                    "hits": bucket.get("segments", {}).get("hits", {}).get("hits", []),
                }
            }
        }
    return response, inner["highlight"]


def _hydrate_page_highlights(
    client: Any,
    page_hits: list[SearchHit],
    response: dict[str, Any],
    search_body: dict[str, Any],
    highlight: dict[str, Any],
    query: str,
    sources: frozenset[str] | None = None,
) -> None:
    """Highlight the displayed page's segments in ONE request (issue #1064).

    The second half of ``_execute_split_bm25_collapse``. The page's segment
    ids are re-queried with the SAME scoring query and filters (the
    highlighter takes its terms from the query, so anything else would
    highlight differently), and each page hit's occurrences are rebuilt from
    its segments with those highlights attached — the processing the
    single-request body got. Scores, order and counts are untouched.

    Best-effort: a failure leaves the page's snippets as unhighlighted text
    and logs a warning, rather than failing a search that already has results.
    """
    segments_by_file: dict[str, tuple[list[dict[str, Any]], float, str | None]] = {}
    for outer in response.get("hits", {}).get("hits", []):
        src = outer.get("_source", {})
        segs = outer.get("inner_hits", {}).get("segments", {}).get("hits", {}).get("hits", [])
        segments_by_file[src.get("file_uuid", "")] = (
            segs,
            outer.get("_score", 0.0) or 0.0,
            src.get("language"),
        )
    ids = [
        seg["_id"]
        for hit in page_hits
        for seg in segments_by_file.get(hit.file_uuid, ([], 0.0, None))[0]
    ]
    if not ids:
        return
    query_bool = search_body["query"]["bool"]
    try:
        resp = client.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body={
                "size": len(ids),
                "_source": False,
                "track_total_hits": False,
                "query": {
                    "bool": {
                        "must": query_bool["must"],
                        "filter": [*query_bool["filter"], {"ids": {"values": ids}}],
                    }
                },
                "highlight": highlight,
            },
        )
    except Exception as e:  # noqa: BLE001 — display-only enrichment, see docstring
        logger.warning(f"Page highlight request failed for query='{query}': {e}")
        return
    by_id = {h["_id"]: h.get("highlight", {}) for h in resp.get("hits", {}).get("hits", [])}

    for hit in page_hits:
        segs, outer_score, language = segments_by_file.get(hit.file_uuid, ([], 0.0, None))
        if not segs:
            continue
        highlighted = [{**seg, "highlight": by_id.get(seg["_id"], {})} for seg in segs]
        occurrences, title_hl, hl_sources, *_ = _process_inner_hits(
            highlighted,
            outer_score,
            query,
            language,
            sources=sources,
            assume_keyword_match=True,
        )
        occurrences.sort(key=lambda o: -(o.score + (0.001 if o.has_keyword_match else 0)))
        hit.occurrences = occurrences
        hit.title_highlighted = title_hl
        hit.match_sources = hl_sources + [s for s in hit.match_sources if s not in hl_sources]
