"""Reading the chunk index, and putting it in a measurable state first.

Addendum §4: the chunks index is single-shard, so BM25 is reproducible — but
only after **seed -> refresh -> force-merge**. Deleted documents leave tombstones
whose term statistics still count toward IDF, so two runs over the same corpus
can disagree purely on how much re-indexing happened in between.

No aggregation is ever issued against a hybrid body here either: OpenSearch 3.4
throws inside ``score-ranker-processor`` when cardinality aggs meet
hybrid + collapse + RRF. Per-file coverage is derived from hits.
"""

from __future__ import annotations

import logging
from typing import Any

from tests.eval.harness.qrels import ChunkDoc

logger = logging.getLogger(__name__)

_PAGE = 1000

#: Force-merging tens of thousands of chunks routinely outruns the client's
#: 10 s default, and a timeout here is not a failure — the merge continues
#: server-side and the next run measures a half-merged index without saying so.
_FORCEMERGE_TIMEOUT_S = 900


def prepare_index(client: Any, index: str) -> dict[str, Any]:
    """Refresh, force-merge to one segment, refresh again.

    Returns:
        Counters worth recording with the result: the document count actually
        measured against and how many deleted docs were merged away.
    """
    client.indices.refresh(index=index)
    client.indices.forcemerge(
        index=index, max_num_segments=1, request_timeout=_FORCEMERGE_TIMEOUT_S
    )
    client.indices.refresh(index=index)
    stats = client.indices.stats(index=index)
    docs = stats.get("_all", {}).get("primaries", {}).get("docs", {})
    prepared = {
        "docs_count": int(docs.get("count") or 0),
        "docs_deleted": int(docs.get("deleted") or 0),
    }
    logger.info("Index prepared: %s", prepared)
    return prepared


def fetch_chunks(client: Any, index: str, file_uuids: list[str]) -> dict[str, list[ChunkDoc]]:
    """Every indexed chunk for ``file_uuids``, keyed by file.

    Paged with ``search_after`` on the index's own sort keys rather than a
    scroll: no server-side context to leak if the harness dies mid-page.
    """
    by_file: dict[str, list[ChunkDoc]] = {}
    after: list[Any] | None = None
    while True:
        body: dict[str, Any] = {
            "size": _PAGE,
            "query": {"bool": {"filter": [{"terms": {"file_uuid": file_uuids}}]}},
            "sort": [{"file_uuid": "asc"}, {"chunk_index": "asc"}],
            "_source": ["file_uuid", "chunk_index", "speaker", "start_time", "end_time"],
            "track_total_hits": False,
        }
        if after is not None:
            body["search_after"] = after
        hits = client.search(index=index, body=body).get("hits", {}).get("hits", [])
        if not hits:
            break
        for hit in hits:
            source = hit.get("_source") or {}
            file_uuid = str(source.get("file_uuid") or "")
            if not file_uuid:
                continue
            by_file.setdefault(file_uuid, []).append(
                ChunkDoc(
                    file_uuid=file_uuid,
                    chunk_index=int(source.get("chunk_index") or 0),
                    speaker=str(source.get("speaker") or ""),
                    start_time=float(source.get("start_time") or 0.0),
                    end_time=float(source.get("end_time") or source.get("start_time") or 0.0),
                )
            )
        after = hits[-1].get("sort")
        if after is None:
            break
    return by_file
