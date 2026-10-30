"""Realtime orphan-id walk shared by the chunk, digest and summary stale-tail prunes."""

import logging
from collections.abc import Callable
from typing import Any

from app.services.opensearch_service import opensearch_client

logger = logging.getLogger(__name__)


#: How many consecutive document ids one realtime orphan probe covers (#435).
#:
#: A bulk load writes ``0..n-1``, so the tail a shorter re-chunk orphans is normally
#: contiguous and a single id would answer the question. The probe is a *window*
#: because that assumption is not guaranteed and is already violated on purpose in
#: the suite: ``_extract_failed_docs`` drops documents whose bulk error is permanent,
#: leaving a hole, and ``test_a_shorter_resection_leaves_no_orphan_digest`` plants its
#: orphan at ``sections + 3``. 64 ids cost one round trip.
#:
#: ⚠️ **An empty window does not end the walk.** It used to, and a hole of 64 or more
#: therefore stranded every document above it — permanently, which is the exact
#: failure #435 exists to prevent, reintroduced by the stop condition itself. The
#: walk now consults ``_probe_ceiling`` before giving up; see
#: :meth:`TranscriptIndexingService._orphaned_document_ids`.
_ORPHAN_PROBE_WINDOW = 64


class OrphanProbeMixin:
    """Finds the ids of documents a shorter re-generation left behind (issue #435)."""

    def _orphaned_document_ids(
        self,
        *,
        index_name: str,
        document_id: Callable[[int], str],
        first_orphan: int,
        plane_query: dict[str, Any],
        counter_field: str,
    ) -> list[str]:
        """Ids of this file's documents from ``first_orphan`` upward, read REALTIME.

        ``mget`` addresses documents **by id**, and an id lookup reads the translog:
        it sees a document the instant the bulk load returns, with no refresh. A
        ``count``/``search`` sees only what the last refresh made visible. That
        difference is the whole of the #435 fix — see :meth:`_prune_stale_chunks`.

        Measured on this stack: 5 documents bulk-loaded with ``refresh=False`` were
        all returned by ``mget`` while a ``count`` over the same predicate returned 0.

        Args:
            index_name: Index to probe.
            document_id: Builds the document id for an index — ``{uuid}_{n}`` for a
                chunk, ``{uuid}_digest_{n}`` for a digest section.
            first_orphan: First index that the new generation did not write.

        Returns:
            The ids that exist, in probe order. Empty means nothing to prune.
        """
        if not opensearch_client:
            return []

        found: list[str] = []
        ceiling: int | None = None
        start = first_orphan
        while True:
            probe = [document_id(n) for n in range(start, start + _ORPHAN_PROBE_WINDOW)]
            response = opensearch_client.mget(index=index_name, body={"ids": probe}, _source=False)
            window = [doc["_id"] for doc in response.get("docs", []) if doc.get("found")]
            if window:
                found.extend(window)
                start += _ORPHAN_PROBE_WINDOW
                continue

            # An empty window does NOT mean the tail ended (#400 follow-up). A
            # partially failed bulk load leaves a HOLE — `_extract_failed_docs`
            # drops permanently-failed documents — and returning here stranded
            # everything above a hole of `_ORPHAN_PROBE_WINDOW` or more,
            # permanently. That is exactly the failure #435 exists to prevent,
            # reintroduced by the walk's own stop condition.
            #
            # The searcher answers "is there anything higher?" for documents old
            # enough to be refreshed, which is precisely the case a hole implies
            # (the survivors above it are from the previous generation). The
            # unrefreshed case #435 cares about is contiguous from `first_orphan`,
            # so the FIRST window already found it and we never reach here.
            if ceiling is None:
                ceiling = self._probe_ceiling(
                    index_name, plane_query=plane_query, counter_field=counter_field
                )
            if ceiling is None or start > ceiling:
                return found
            start += _ORPHAN_PROBE_WINDOW

    def _probe_ceiling(
        self, index_name: str, *, plane_query: dict[str, Any], counter_field: str
    ) -> int | None:
        """Highest value of *counter_field* the SEARCHER can see for this plane.

        Deliberately a search, and deliberately only consulted after an empty
        probe window: a searcher cannot see the unrefreshed writes the id probe
        exists for, but it is the only thing that can say how far above a hole to
        keep looking. Paying for it lazily keeps it off the common path — a
        shrinking re-chunk finds its tail in the first window and never gets here,
        and a growing or unchanged one pays one small aggregation.

        *plane_query* is the caller's own plane predicate — ``chunk_plane_query``
        or ``digest_plane_query`` for the whole file — so the ceiling is measured
        over exactly the documents the delete will target, compat arm included.
        *counter_field* is the field the document id counts up with:
        ``chunk_index`` for the chunk plane, ``digest_section`` for the digest
        plane (whose ``chunk_index`` is a NEGATIVE sentinel and counts the wrong
        way).

        Returns:
            The maximum value, or ``None`` when the searcher knows nothing (a
            fresh or wholly-unrefreshed generation) or the query failed. ``None``
            means "stop walking" — the behaviour the walk already had, so this can
            only ever find more, never less.
        """
        try:
            response = opensearch_client.search(
                index=index_name,
                body={
                    "size": 0,
                    "query": plane_query,
                    "aggs": {"ceiling": {"max": {"field": counter_field}}},
                },
            )
        except Exception as exc:  # noqa: BLE001 — the walk degrades, it must not fail
            logger.debug(f"Could not read the orphan ceiling: {exc}")
            return None

        value = (response.get("aggregations") or {}).get("ceiling", {}).get("value")
        return None if value is None else int(value)
