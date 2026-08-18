"""A bulk load that lost documents must not report success (issue #495).

``_retry_failed_docs`` gives up after 2 attempts, logs
``"N documents failed after 2 retries"`` and returns however many landed. Nothing
compared that number to how many were built, so ``index_transcript_chunks``
returned ``status: success`` with ``chunk_count`` quietly short and the file was
left **permanently half-searchable** — the missing chunks unreachable by search
and by RAG chat, the only trace one ERROR line in a worker log.

A task that reports success is never retried by anything, which is what makes this
worse than a crash. Document ids are deterministic (``{file_uuid}_{chunk_index}``),
so failing is safe: a re-run overwrites rather than duplicates.

The digest plane had the same defect one step further along — it discarded
``_bulk_index_documents``'s return value entirely and reported ``len(documents)``,
the number of sections *generated*, identically whether all of them were written or
none were.

⚠️ The two planes are treated DIFFERENTLY on purpose and both directions are pinned
below: chunks **raise**, digests **report the true count without raising**. The
digest tier is derived enrichment (its caller already declines to fail an index over
it) while the chunks are the transcript itself. Do not "make them consistent"
without re-reading that argument in ``_index_digest_plane``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def seams(monkeypatch):
    """Substitute every collaborator except the count bookkeeping under test."""
    from app.services.search import indexing_service

    for name, value in (
        ("is_neural_pipeline_available", lambda: True),
        ("ensure_chunks_index_exists", lambda: True),
        ("ensure_search_pipeline_exists", lambda: True),
        ("get_opensearch_client", lambda: object()),
    ):
        monkeypatch.setattr(indexing_service, name, value)

    service = indexing_service.TranscriptIndexingService
    monkeypatch.setattr(service, "_prune_stale_chunks", lambda self, file_uuid, keep_count: 0)
    monkeypatch.setattr(service, "_index_digest_plane", lambda self, **kwargs: 0)
    return monkeypatch


def _index(segments: int = 3) -> dict[str, Any]:
    from app.services.search.indexing_service import TranscriptIndexingService

    result = TranscriptIndexingService().index_transcript_chunks(
        file_id=1,
        file_uuid="22222222-2222-4222-8222-222222222222",
        user_id=1,
        segments=[
            {
                "start": float(i * 4),
                "end": float(i * 4 + 4),
                "text": f"segment {i} says something worth indexing",
                "speaker": f"SPEAKER_0{i}",
            }
            for i in range(segments)
        ],
        title="A recording",
        speakers=[f"SPEAKER_0{i}" for i in range(segments)],
        tags=[],
    )
    assert isinstance(result, dict), f"expected the dict shape, got {type(result).__name__}"
    return result


def test_a_short_bulk_load_raises_instead_of_reporting_success(seams, monkeypatch):
    """The defect itself: fewer documents landed than were built."""
    from app.services.search.indexing_service import TranscriptIndexingService

    built: list[int] = []

    def _lost_one(self, chunks, use_neural_pipeline):
        built.append(len(chunks))
        return len(chunks) - 1  # exactly what exhausted retries produce

    monkeypatch.setattr(TranscriptIndexingService, "_bulk_index_chunks", _lost_one)

    with pytest.raises(RuntimeError) as excinfo:
        _index()

    assert built and built[0] >= 2, "the fixture produced too few chunks to lose one"
    message = str(excinfo.value)
    # Both numbers must be in the message: "some failed" without the counts leaves
    # an operator unable to tell one lost chunk from a wholly empty index.
    assert f"{built[0] - 1} of {built[0]}" in message, message
    assert "22222222-2222-4222-8222-222222222222" in message


def test_a_complete_bulk_load_still_succeeds(seams, monkeypatch):
    """The control. Without it, "raises on partial" would pass if it always raised."""
    from app.services.search.indexing_service import TranscriptIndexingService

    monkeypatch.setattr(
        TranscriptIndexingService,
        "_bulk_index_chunks",
        lambda self, chunks, use_neural_pipeline: len(chunks),
    )

    result = _index()

    # No `reason` key: this is a real index, not one of the "nothing to index"
    # outcomes that also report chunk_count 0.
    assert result["chunk_count"] >= 2
    assert "reason" not in result


def test_an_empty_bulk_load_raises_too(seams, monkeypatch):
    """Zero landed is the same defect at its worst, not a separate "nothing to do"."""
    from app.services.search.indexing_service import TranscriptIndexingService

    monkeypatch.setattr(
        TranscriptIndexingService,
        "_bulk_index_chunks",
        lambda self, chunks, use_neural_pipeline: 0,
    )

    with pytest.raises(RuntimeError, match="Partial chunk index"):
        _index()


def _digest_plane(monkeypatch, *, sections: int, written: int) -> int:
    """Drive ``_index_digest_plane`` with a controllable bulk outcome."""
    from app.services.ingest_artifacts import index_mapping as digest_mapping
    from app.services.search import indexing_service
    from app.services.search.indexing_service import TranscriptIndexingService

    documents = [{"content": f"section {n}"} for n in range(sections)]
    ids = [f"22222222_digest_{n}" for n in range(sections)]

    monkeypatch.setattr(digest_mapping, "build_digest_documents", lambda **kw: documents)
    monkeypatch.setattr(digest_mapping, "digest_document_ids", lambda *a, **kw: ids)
    monkeypatch.setattr(indexing_service, "get_opensearch_client", lambda: object())
    monkeypatch.setattr(
        TranscriptIndexingService,
        "_bulk_index_documents",
        lambda self, docs, use_neural: written,
    )
    monkeypatch.setattr(
        TranscriptIndexingService, "_prune_stale_digests", lambda self, file_uuid, keep_count: 0
    )

    # `_index_digest_plane` imports these INSIDE the function, so patch them at their
    # source module — patching them on `indexing_service` binds nothing.
    import app.db.session_utils as session_utils
    from app.services import ingest_artifacts

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(session_utils, "session_scope", lambda: _Session())
    monkeypatch.setattr(
        ingest_artifacts,
        "generate_file_artifacts",
        lambda db, file_id: SimpleNamespace(digest={"sections": []}, facts={}),
    )

    return TranscriptIndexingService()._index_digest_plane(
        file_id=1,
        file_uuid="22222222-2222-4222-8222-222222222222",
        base_metadata={"user_id": 1, "title": "A recording"},
        use_neural=True,
    )


def test_the_digest_plane_reports_what_landed_not_what_was_built(monkeypatch, caplog):
    """It returned ``len(documents)`` regardless of how many were written."""
    with caplog.at_level(logging.ERROR):
        returned = _digest_plane(monkeypatch, sections=4, written=1)

    assert returned == 1, (
        "the digest count must be what OpenSearch accepted; returning the number of "
        "sections generated reports an identical figure whether all or none landed"
    )
    assert any("1 of 4 sections landed" in record.message for record in caplog.records), [
        r.message for r in caplog.records
    ]


def test_the_digest_plane_does_not_raise_on_a_short_write(monkeypatch):
    """The deliberate asymmetry with the chunk plane, pinned so it is a decision.

    ``_index_digest_plane``'s caller declines to fail an index over a digest
    problem — a missing digest degrades summarization, missing chunks make part of
    a recording unfindable. If someone later "makes the planes consistent" by
    raising here, this test says the asymmetry was chosen.
    """
    assert _digest_plane(monkeypatch, sections=4, written=0) == 0


def test_a_complete_digest_write_reports_every_section(monkeypatch, caplog):
    """The control: the short-write path must not be the only one that works."""
    with caplog.at_level(logging.ERROR):
        returned = _digest_plane(monkeypatch, sections=4, written=4)

    assert returned == 4
    assert not [r for r in caplog.records if "sections landed" in r.message]
