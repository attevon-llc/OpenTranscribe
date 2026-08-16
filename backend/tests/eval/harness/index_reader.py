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
import time
from typing import Any

from tests.eval.harness.qrels import ChunkDoc

logger = logging.getLogger(__name__)

_PAGE = 1000

#: Force-merging tens of thousands of chunks routinely outruns the client's
#: 10 s default, and a timeout here is not a failure — the merge continues
#: server-side and the next run measures a half-merged index without saying so.
_FORCEMERGE_TIMEOUT_S = 900

#: Seconds between settle polls. Long enough that two polls cannot both land
#: inside one file's delete-then-reindex window.
_SETTLE_INTERVAL_S = 5.0


class IndexNotSettledError(RuntimeError):
    """The corpus never reached its expected size, or never stopped moving."""


def await_settled(
    client: Any,
    index: str,
    file_uuids: list[str],
    *,
    expected_files: int,
    since: str | None = None,
    timeout_s: float = 1800.0,
    interval_s: float = _SETTLE_INTERVAL_S,
) -> dict[str, Any]:
    """Block until the corpus is fully indexed and has stopped changing.

    Three conditions, and each one closes a hole the others leave open:

    * **every expected file is represented.** A reindex deletes a file's chunks
      before writing the new ones, so a poll during the run sees a corpus that
      is merely *smaller* — not obviously mid-flight.
    * **the (files, chunks) pair is identical on two consecutive polls.**
      Polling the total chunk count alone is what produced this harness's
      phantom deltas of 223 / 357 / 591 chunks between runs over an unchanged
      corpus: a plateau in a rising count is indistinguishable from the end of
      it.
    * **nothing predates *since*, when given.** The first two conditions are
      both satisfied by a corpus whose reindex has been *dispatched but has not
      started* — Celery queue latency is easily longer than two poll intervals,
      and the check would then certify the OLD index as the new one. Passing the
      dispatch timestamp makes "every chunk was written by this run" part of
      the condition rather than an assumption about scheduling.

    Args:
        client: OpenSearch client.
        index: Chunks index name.
        file_uuids: The corpus's file uuids.
        expected_files: How many of them must carry at least one chunk.
        since: ISO-8601 instant every chunk's ``indexed_at`` must be at or
            after. ``None`` (the plain measurement case) checks only shape.
        timeout_s: Give up after this long.
        interval_s: Delay between polls.

    Returns:
        The settled counters, plus the poll history that got there.

    Raises:
        IndexNotSettledError: On timeout. The message names which condition
            was still unmet, because "it timed out" is not diagnosable.
    """
    deadline = time.monotonic() + timeout_s
    history: list[dict[str, int]] = []
    previous: tuple[int, int] | None = None

    while True:
        client.indices.refresh(index=index)
        observation = _observe(client, index, file_uuids, since=since)
        history.append(observation)
        current = (observation["files"], observation["chunks"])
        complete = observation["files"] == expected_files and not observation["stale"]

        if complete and current == previous:
            logger.info("Corpus settled: %s after %d poll(s)", observation, len(history))
            return {
                "files": observation["files"],
                "chunks": observation["chunks"],
                "expected_files": expected_files,
                "polls": len(history),
                "history": history,
            }

        previous = current
        if time.monotonic() >= deadline:
            raise IndexNotSettledError(
                f"Corpus never settled within {timeout_s:.0f}s: "
                f"{_unmet(observation, expected_files, history)}. Measuring here would "
                f"compare a half-indexed corpus against a whole one."
            )
        logger.info("Waiting for corpus to settle: %s", observation)
        time.sleep(interval_s)


def _unmet(observation: dict[str, int], expected_files: int, history: list[dict[str, int]]) -> str:
    if observation["files"] != expected_files:
        return f"only {observation['files']} of {expected_files} files carry chunks"
    if observation["stale"]:
        return f"{observation['stale']} chunk(s) predate this run"
    return f"chunk count still moving ({history[-2:]})"


def _observe(
    client: Any, index: str, file_uuids: list[str], *, since: str | None = None
) -> dict[str, int]:
    """One (distinct files, chunks, chunks predating *since*) reading.

    A plain ``terms`` query, never a hybrid body — the OpenSearch 3.4 crash in
    ``score-ranker-processor`` applies to measurement code too. ``cardinality``
    is exact well below its 40,000 threshold, and this corpus is in the hundreds.
    A document with no ``indexed_at`` at all counts as stale: it certainly was
    not written by a run that stamps one.
    """
    aggs: dict[str, Any] = {
        "files": {"cardinality": {"field": "file_uuid", "precision_threshold": 40000}}
    }
    if since is not None:
        aggs["stale"] = {
            "filter": {
                "bool": {
                    "should": [
                        {"range": {"indexed_at": {"lt": since}}},
                        {"bool": {"must_not": {"exists": {"field": "indexed_at"}}}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        }
    body = {
        "size": 0,
        "query": {"bool": {"filter": [{"terms": {"file_uuid": file_uuids}}]}},
        "track_total_hits": True,
        "aggs": aggs,
    }
    response = client.search(index=index, body=body)
    aggregations = response["aggregations"]
    return {
        "files": int(aggregations["files"]["value"]),
        "chunks": int(response["hits"]["total"]["value"]),
        "stale": int(aggregations["stale"]["doc_count"]) if since is not None else 0,
    }


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
