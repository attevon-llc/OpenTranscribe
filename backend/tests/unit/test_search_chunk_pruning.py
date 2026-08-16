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

Two properties this module has to keep, both learned the hard way:

* **The gate is realtime, so the stand-in must model realtime.** Since #435 the prune
  finds its orphans with ``mget`` — an id lookup, which reads the translog and sees a
  document the instant it is written — rather than with a ``count``, which is a search
  and sees only what the last refresh made visible. The stand-in has no searcher at
  all, so it cannot reproduce the *bug*; that is what
  ``tests/integration/test_chunk_pruning_opensearch.py`` is for. What it can and must
  check is that the prune asks for the right ids and deletes through the right
  predicate.
* **:data:`FILE_ID` is negative on purpose.** ``index_transcript_chunks`` also writes
  the digest plane, and ``_index_digest_plane`` resolves the file id against Postgres
  in its own session. This module shipped with ``file_id=42``, which is a bet that no
  ``media_file`` has that id — and on the Stage-3 stack row 42 is a real recording, so
  every ``_index()`` here generated a stranger's digest into the fake store and **five
  of these six tests failed** on document sets they never wrote and a count gate they
  never issued. On a machine without that row they all passed while proving less.
  ``media_file.id`` is a positive serial, so a negative id cannot resolve, the digest
  plane is empty by construction, and this module's outcome no longer depends on
  database content it does not own. The sibling integration module carries the
  identical comment for the identical reason; do not "simplify" either back.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.services.search import indexing_service as svc
from app.services.search.fusion import FusionConfig
from app.services.search.fusion import search_pipeline_id

FILE_UUID = "11111111-2222-3333-4444-555555555555"
OTHER_UUID = "99999999-8888-7777-6666-555555555555"

#: Negative so it cannot resolve against ``media_file`` — see the module docstring.
FILE_ID = -42


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
        # Built FROM the config rather than spelled out: since #363 the self-heal
        # compares the whole processor block, so a hand-written stand-in that
        # omitted a field would silently make this a delete-and-recreate probe
        # instead of the no-op the docstring claims.
        cfg = FusionConfig.default()
        return {search_pipeline_id(cfg): cfg.pipeline_body()}


class _FakeIndex:
    """A document store that evaluates the term/range queries this module sends."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.indices = _FakeIndices()
        self.transport = _FakeTransport()
        self.count_bodies: list[dict[str, Any]] = []
        self.delete_bodies: list[dict[str, Any]] = []
        self.mget_id_batches: list[list[str]] = []

    # -- query evaluation --------------------------------------------------

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        """Evaluate the clauses the indexing service actually sends, and only those.

        ``should``/``minimum_should_match``/``must_not``/``exists`` are here because
        index v6's compat arm is exactly that shape — "``doc_type`` is ``chunk``, OR
        the field is absent because this document predates v6". A stand-in that only
        understood ``filter`` raised ``KeyError('filter')`` inside the prune's
        ``except``, so every delete silently became a no-op and this module reported
        a stale tail as a *service* bug. Raising on the unknown clause was the right
        instinct; the arm simply has to be taught rather than caught.
        """
        if "bool" in query:
            clause = query["bool"]
            unknown = set(clause) - {"filter", "should", "must_not", "minimum_should_match"}
            if unknown:
                raise AssertionError(f"fake index cannot evaluate bool keys: {sorted(unknown)}")
            if not all(_FakeIndex._matches(doc, f) for f in clause.get("filter", [])):
                return False
            must_not = clause.get("must_not")
            if must_not is not None:
                negated = [must_not] if isinstance(must_not, dict) else list(must_not)
                if any(_FakeIndex._matches(doc, f) for f in negated):
                    return False
            should = clause.get("should")
            if should is not None:
                matched = sum(1 for f in should if _FakeIndex._matches(doc, f))
                if matched < int(clause.get("minimum_should_match", 1)):
                    return False
            return True
        if "term" in query:
            field, value = next(iter(query["term"].items()))
            return bool(doc.get(field) == value)
        if "range" in query:
            field, spec = next(iter(query["range"].items()))
            if set(spec) != {"gte"}:
                raise AssertionError(f"fake index cannot evaluate range spec: {spec!r}")
            value = doc.get(field)
            return value is not None and int(value) >= int(spec["gte"])
        if "exists" in query:
            return query["exists"]["field"] in doc
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

    def mget(
        self,
        index: str,  # noqa: ARG002
        body: dict[str, Any],
        _source: bool = True,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Realtime lookup by id — the #435 gate.

        A dict lookup is unconditionally realtime, which is precisely why the stand-in
        cannot reproduce the bug and the integration module has to. Recording the id
        batches is the part that earns its keep: it is how a test asserts the prune
        probed the ids it claims to, and how many round trips that took.
        """
        ids = list(body["ids"])
        self.mget_id_batches.append(ids)
        return {"docs": [{"_id": doc_id, "found": doc_id in self.docs} for doc_id in ids]}

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
        file_id=FILE_ID,
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
    """The common case — nothing indexed yet — costs one id probe and zero deletes.

    The gate stopped being a ``count`` in #435, so ``count_bodies`` must now stay
    empty: a count is a search, and a search only sees what the last refresh made
    visible. The probe covers ``keep_count .. keep_count + window``, which is where
    a previous longer chunking's orphans would be.
    """
    service = svc.TranscriptIndexingService()

    result = _index(service, _segments(5, marker="FIRST"))

    assert result["stale_removed"] == 0
    assert fake_index.delete_bodies == []
    assert fake_index.count_bodies == [], "the prune must not issue a searcher-dependent gate"
    assert fake_index.mget_id_batches == [
        [f"{FILE_UUID}_{i}" for i in range(5, 5 + svc._ORPHAN_PROBE_WINDOW)]
    ], "one probe window, starting at the first index the new chunking did not write"


def test_growing_rechunk_issues_no_delete(fake_index):
    """More chunks than last time means no orphans, so no delete_by_query."""
    service = svc.TranscriptIndexingService()
    _index(service, _segments(3, marker="ORIGINAL"))

    result = _index(service, _segments(7, marker="EXPANDED"))

    assert result["stale_removed"] == 0
    assert fake_index.delete_bodies == []
    assert set(fake_index.docs) == {f"{FILE_UUID}_{i}" for i in range(7)}


# ---------------------------------------------------------------------------
# The plane split that index v6 introduced
# ---------------------------------------------------------------------------


def test_the_two_delete_paths_use_the_predicates_their_jobs_require(fake_index):
    """The tail prune is chunk-plane; the per-file delete is every plane.

    This test used to *simulate* the ``doc_type`` discriminator by monkeypatching
    ``chunk_plane_query``, because #383 Phase 3 had not landed. It has: v6 puts
    digest documents in this same index, and the two deletes now deliberately
    disagree. Asserting the simulation instead of the shipped predicates outlived
    its usefulness the moment the real ones existed — and it asserted something that
    is now *false by design*, that the per-file delete carries a chunk-plane filter.

    Getting the split backwards is silent in both directions: a rebuild using the
    chunk-plane predicate strands the digests of a shorter re-sectioning, and a file
    deletion using it leaves a readable summary of a deleted recording.
    """
    from app.services.ingest_artifacts.index_mapping import chunk_plane_clause

    service = svc.TranscriptIndexingService()
    _index(service, _segments(6, marker="ORIGINAL"))

    _index(service, _segments(2, marker="EDITED"))
    service.delete_transcript_chunks(FILE_UUID)

    assert len(fake_index.delete_bodies) == 2
    prune, per_file = (body["query"]["bool"]["filter"] for body in fake_index.delete_bodies)

    assert chunk_plane_clause() in prune, (
        "the tail prune must spare digests, and must do it with the COMPAT-ARMED "
        "clause — a bare doc_type term matches no document written before v6"
    )
    assert {"range": {"chunk_index": {"gte": 2}}} in prune
    assert per_file == [{"term": {"file_uuid": FILE_UUID}}], (
        "the per-file delete must carry no plane predicate at all: a digest that "
        "outlives its file is a readable summary of deleted content"
    )
    assert fake_index.docs == {}


def test_prune_failure_does_not_fail_the_index(fake_index, monkeypatch):
    """The chunks are already written and correct — a failed prune is logged, not raised."""

    def _boom(index, body, _source=True):
        raise RuntimeError("cluster_block_exception")

    service = svc.TranscriptIndexingService()
    # The gate itself, since #435: if the id probe cannot run there is nothing to
    # prune with, and the caller must still see a successful index.
    monkeypatch.setattr(fake_index, "mget", _boom)

    result = _index(service, _segments(4, marker="FIRST"))

    assert result["chunk_count"] == 4
    assert result["stale_removed"] == 0
    assert set(fake_index.docs) == {f"{FILE_UUID}_{i}" for i in range(4)}, (
        "the chunks are written and correct — that is why the failure is swallowed"
    )
