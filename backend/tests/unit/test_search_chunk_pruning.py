"""Re-indexing a transcript must not leave the previous chunking's tail behind (issue #400).

Chunk doc ids are deterministic — ``{file_uuid}_{chunk_index}`` — so a re-index
*overwrites* chunks ``0..N-1`` and silently orphans everything above ``N`` when the new
chunking is shorter. Those orphans keep their old text, old speaker labels and old
timestamps, and keep surfacing in both search results and RAG chat retrieval.

These tests drive the real ``TranscriptIndexingService`` and the real chunker against an
**in-memory stand-in for OpenSearch** that stores documents by id and evaluates the
queries the service actually sends. It is not a mock of the service under test: the
assertion is made against the resulting document store, so a service that skipped the
delete would leave the tail there and fail. ``_FakeIndex._matches`` raises on any query
clause it does not understand, so a change in the delete query's shape surfaces as an
error instead of silently matching nothing.

Not run against a real cluster: the only reachable OpenSearch here is the shared dev
stack, whose live index these tests must never write to.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.services.search import indexing_service as svc

FILE_UUID = "11111111-2222-3333-4444-555555555555"
OTHER_UUID = "99999999-8888-7777-6666-555555555555"


# ---------------------------------------------------------------------------
# In-memory OpenSearch stand-in
# ---------------------------------------------------------------------------


class _FakeIndices:
    """The ``client.indices`` namespace the indexing path touches."""

    def __init__(self) -> None:
        self.refreshed = 0

    def exists(self, index: str) -> bool:  # noqa: ARG002
        return True

    def get_mapping(self, index: str) -> dict[str, Any]:
        return {index: {"mappings": {"_meta": {"version": svc._INDEX_VERSION}}}}

    def get_settings(self, index: str) -> dict[str, Any]:
        return {index: {"settings": {"index": {"refresh_interval": "1s"}}}}

    def put_settings(self, index: str, body: dict[str, Any]) -> None:
        pass

    def refresh(self, index: str) -> None:  # noqa: ARG002
        self.refreshed += 1

    def exists_alias(self, name: str) -> bool:  # noqa: ARG002
        return True


class _FakeTransport:
    """Answers the search-pipeline probe so ``ensure_search_pipeline_exists`` is a no-op."""

    def perform_request(self, method: str, path: str, body: Any = None) -> Any:  # noqa: ARG002
        pipeline_id = settings.OPENSEARCH_SEARCH_PIPELINE
        return {
            pipeline_id: {
                "phase_results_processors": [
                    {
                        "score-ranker-processor": {
                            "combination": {
                                "rank_constant": settings.SEARCH_RRF_RANK_CONSTANT,
                            }
                        }
                    }
                ]
            }
        }


class _FakeIndex:
    """A document store that evaluates the term/range queries this module sends."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.indices = _FakeIndices()
        self.transport = _FakeTransport()
        self.count_bodies: list[dict[str, Any]] = []
        self.delete_bodies: list[dict[str, Any]] = []

    # -- query evaluation --------------------------------------------------

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        if "bool" in query:
            return all(_FakeIndex._matches(doc, f) for f in query["bool"]["filter"])
        if "term" in query:
            field, value = next(iter(query["term"].items()))
            return bool(doc.get(field) == value)
        if "range" in query:
            field, spec = next(iter(query["range"].items()))
            if set(spec) != {"gte"}:
                raise AssertionError(f"fake index cannot evaluate range spec: {spec!r}")
            value = doc.get(field)
            return value is not None and int(value) >= int(spec["gte"])
        raise AssertionError(f"fake index cannot evaluate query clause: {query!r}")

    def _hits(self, query: dict[str, Any]) -> list[str]:
        return [doc_id for doc_id, doc in self.docs.items() if self._matches(doc, query)]

    # -- client API --------------------------------------------------------

    def bulk(self, body: list[Any], refresh: bool = False) -> dict[str, Any]:  # noqa: ARG002
        for action, doc in zip(body[::2], body[1::2], strict=True):
            # ``doc_type`` is what #383 Phase 3 will stamp on chunk documents to tell
            # them apart from per-file digests. Nothing reads it today, so it is inert
            # for every other test here — but it lets
            # ``test_every_delete_goes_through_chunk_plane_query`` simulate Phase 3.
            self.docs[action["index"]["_id"]] = {"doc_type": "chunk", **doc}
        return {"errors": False, "items": []}

    def count(self, index: str, body: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.count_bodies.append(body)
        return {"count": len(self._hits(body["query"]))}

    def delete_by_query(
        self,
        index: str,  # noqa: ARG002
        body: dict[str, Any],
        refresh: bool = False,  # noqa: ARG002
        conflicts: str | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        self.delete_bodies.append(body)
        doomed = self._hits(body["query"])
        for doc_id in doomed:
            del self.docs[doc_id]
        return {"deleted": len(doomed)}


@pytest.fixture
def fake_index(monkeypatch) -> _FakeIndex:
    """Point the indexing service at the in-memory index, text-only (no neural pipeline)."""
    client = _FakeIndex()
    monkeypatch.setattr(svc, "opensearch_client", client)
    monkeypatch.setattr(svc, "get_opensearch_client", lambda: client)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
    svc.reset_neural_pipeline_state()
    return client


def _segments(count: int, *, marker: str) -> list[dict[str, Any]]:
    """``count`` segments with alternating speakers → exactly ``count`` chunks.

    Alternating the speaker prevents turn merging, and each segment is well under
    ``SEARCH_CHUNK_TARGET_WORDS``, so the chunker emits one chunk per segment.
    """
    return [
        {
            "start": float(i * 10),
            "end": float(i * 10 + 10),
            "text": f"{marker} statement number {i} about quarterly planning and staffing.",
            "speaker": f"Speaker {i % 2}",
        }
        for i in range(count)
    ]


def _index(service: svc.TranscriptIndexingService, segments, *, file_uuid=FILE_UUID):
    return service.index_transcript_chunks(
        file_id=42,
        file_uuid=file_uuid,
        user_id=7,
        segments=segments,
        title="Quarterly planning",
        speakers=["Speaker 0", "Speaker 1"],
        tags=[],
    )


# ---------------------------------------------------------------------------
# The decisive case: a shrinking re-chunk
# ---------------------------------------------------------------------------


def test_shrinking_rechunk_leaves_no_stale_tail(fake_index):
    """8 chunks re-indexed as 3 must leave exactly 3 documents, all with the new text."""
    service = svc.TranscriptIndexingService()

    first = _index(service, _segments(8, marker="ORIGINAL"))
    assert first["chunk_count"] == 8
    assert set(fake_index.docs) == {f"{FILE_UUID}_{i}" for i in range(8)}

    second = _index(service, _segments(3, marker="EDITED"))

    assert second["chunk_count"] == 3
    assert set(fake_index.docs) == {f"{FILE_UUID}_{i}" for i in range(3)}
    assert second["stale_removed"] == 5
    for doc in fake_index.docs.values():
        assert "ORIGINAL" not in doc["content"]


def test_prune_is_scoped_to_the_file_that_was_reindexed(fake_index):
    """A shrinking re-chunk must not touch another recording's chunks."""
    service = svc.TranscriptIndexingService()
    _index(service, _segments(6, marker="OTHER"), file_uuid=OTHER_UUID)
    _index(service, _segments(6, marker="ORIGINAL"))

    _index(service, _segments(2, marker="EDITED"))

    assert set(fake_index.docs) == {f"{OTHER_UUID}_{i}" for i in range(6)} | {
        f"{FILE_UUID}_{i}" for i in range(2)
    }


# ---------------------------------------------------------------------------
# The hot path must not pay for a delete it does not need
# ---------------------------------------------------------------------------


def test_first_index_after_transcription_issues_no_delete(fake_index):
    """The common case — nothing indexed yet — costs one count and zero deletes."""
    service = svc.TranscriptIndexingService()

    result = _index(service, _segments(5, marker="FIRST"))

    assert result["stale_removed"] == 0
    assert fake_index.delete_bodies == []
    assert len(fake_index.count_bodies) == 1


def test_growing_rechunk_issues_no_delete(fake_index):
    """More chunks than last time means no orphans, so no delete_by_query."""
    service = svc.TranscriptIndexingService()
    _index(service, _segments(3, marker="ORIGINAL"))

    result = _index(service, _segments(7, marker="EXPANDED"))

    assert result["stale_removed"] == 0
    assert fake_index.delete_bodies == []
    assert set(fake_index.docs) == {f"{FILE_UUID}_{i}" for i in range(7)}


# ---------------------------------------------------------------------------
# The single-predicate guarantee that #383 Phase 3 depends on
# ---------------------------------------------------------------------------


def test_every_delete_goes_through_chunk_plane_query(fake_index, monkeypatch):
    """Adding a ``doc_type`` predicate in one function must reach every delete path.

    Phase 3 of #383 puts non-chunk (digest) documents in this same index. This
    simulates that one-line change and asserts both delete paths — the stale-tail
    prune and the full ``delete_transcript_chunks`` — carry the new predicate.
    """
    real_query = svc.chunk_plane_query

    def _with_doc_type(file_uuid: str, *, from_chunk_index: int | None = None):
        query = real_query(file_uuid, from_chunk_index=from_chunk_index)
        query["bool"]["filter"].append({"term": {"doc_type": "chunk"}})
        return query

    service = svc.TranscriptIndexingService()
    _index(service, _segments(6, marker="ORIGINAL"))

    monkeypatch.setattr(svc, "chunk_plane_query", _with_doc_type)
    _index(service, _segments(2, marker="EDITED"))
    service.delete_transcript_chunks(FILE_UUID)

    assert len(fake_index.delete_bodies) == 2
    for body in fake_index.delete_bodies:
        assert {"term": {"doc_type": "chunk"}} in body["query"]["bool"]["filter"]
    assert fake_index.docs == {}


def test_prune_failure_does_not_fail_the_index(fake_index, monkeypatch):
    """The chunks are already written and correct — a failed prune is logged, not raised."""

    def _boom(index, body):
        raise RuntimeError("cluster_block_exception")

    service = svc.TranscriptIndexingService()
    monkeypatch.setattr(fake_index, "count", _boom)

    result = _index(service, _segments(4, marker="FIRST"))

    assert result["chunk_count"] == 4
    assert result["stale_removed"] == 0
