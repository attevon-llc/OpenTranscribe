"""Document-chunk indexing into the v6 ``transcript_chunks`` document plane (#362 Stage 6c),
executed against a real OpenSearch.

Mirrors ``test_chunk_pruning_opensearch.py``'s shape and its own hard-won lesson: a
throwaway index with the REAL mapping, never a stand-in, because ``file_uuid`` must be a
``keyword`` for the plane query's ``term`` and ``chunk_index`` an ``integer`` for its
``range`` — a dynamically mapped index answers differently.

What is proven here and nowhere else:

* a document chunk really is written with ``doc_type: document_chunk`` and the
  ``{document_uuid}_{chunk_index}`` id scheme, sharing the index with the transcript plane;
* ``document_chunk_plane_query`` really discriminates by ``doc_type`` — a transcript chunk
  and a digest section sharing the same ``file_uuid`` are excluded, not just theoretically
  by the query body but against a real engine's evaluation of it;
* a shrinking re-parse really leaves no stale tail, the same #435 realtime-probe guarantee
  ``_prune_stale_chunks`` gives transcripts, exercised through the document-specific method;
* ``documents.index``'s inner implementation really turns real ``document``/``document_chunk``
  Postgres rows into real, retrievable OpenSearch documents — the full wiring, not each half
  in isolation.

Point the suite at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 \\
        pytest backend/tests/integration/test_document_indexing_opensearch.py -m integration
"""

from __future__ import annotations

import contextlib
import os
import uuid as uuid_pkg

import pytest
from sqlalchemy import text

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and export "
            "OPENSEARCH_PORT — a stand-in index cannot validate delete_by_query semantics."
        ),
    ),
]

USER_ID = 4101
#: Negative, matching test_chunk_pruning_opensearch.py's FILE_ID convention: nothing in
#: index_document_chunks resolves this id against Postgres today (no digest tier for
#: documents), but keeping it un-resolvable is a property worth not losing if that changes.
DOCUMENT_DB_ID = -4101


@pytest.fixture
def chunk_index(monkeypatch):
    """A throwaway chunks index with the REAL mapping, wired in via settings."""
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    name = f"test_document_chunk_{uuid_pkg.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
    svc.reset_neural_pipeline_state()
    try:
        yield client
    finally:
        client.indices.delete(index=name, ignore=[404])
        svc.reset_neural_pipeline_state()


def _chunks(count: int, *, marker: str) -> list[dict]:
    return [
        {
            "chunk_index": i,
            "text": f"{marker} paragraph number {i} about quarterly budget planning.",
            "char_start": i * 100,
            "char_end": i * 100 + 50,
            "page": i + 1,
        }
        for i in range(count)
    ]


def _index(chunks: list[dict], *, document_uuid: str) -> dict:
    from app.services.search.indexing_service import TranscriptIndexingService

    result = TranscriptIndexingService().index_document_chunks(
        document_id=DOCUMENT_DB_ID,
        document_uuid=document_uuid,
        user_id=USER_ID,
        chunks=chunks,
        title="Quarterly Budget Report.pdf",
    )
    assert isinstance(result, dict), f"indexing returned a failure sentinel: {result!r}"
    return result


def _docs(client, document_uuid: str) -> list[dict]:
    from app.core.config import settings

    client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
    response = client.search(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={
            "size": 100,
            "query": {"term": {"file_uuid": document_uuid}},
            "sort": ["chunk_index"],
        },
    )
    return [hit["_source"] for hit in response["hits"]["hits"]]


def test_a_document_chunk_is_written_with_the_document_chunk_doc_type(chunk_index):
    document_uuid = str(uuid_pkg.uuid4())
    result = _index(_chunks(3, marker="ORIGINAL"), document_uuid=document_uuid)
    assert result["chunk_count"] == 3

    docs = _docs(chunk_index, document_uuid)
    assert [int(d["chunk_index"]) for d in docs] == [0, 1, 2]
    assert all(d["doc_type"] == "document_chunk" for d in docs)
    assert all(d["file_id"] == DOCUMENT_DB_ID for d in docs)
    assert "ORIGINAL paragraph number 1" in docs[1]["content"]
    assert docs[1]["embedding_text"].startswith("Quarterly Budget Report.pdf")


def test_document_chunk_plane_query_excludes_sibling_doc_types(chunk_index):
    """The discriminator, proven against a real engine's evaluation of the query body.

    A transcript chunk and a digest section are written under the SAME file_uuid as the
    document chunk — an adversarial setup a `term` on `file_uuid` alone would conflate —
    so a query that returns exactly the document_chunk row is proof the doc_type predicate
    is doing the work, not the file_uuid scoping alone.
    """
    from app.core.config import settings
    from app.services.search.indexing_service import document_chunk_plane_query

    shared_uuid = str(uuid_pkg.uuid4())
    _index(_chunks(1, marker="DOC"), document_uuid=shared_uuid)

    chunk_index.index(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        id=f"{shared_uuid}_transcript_sibling",
        body={"file_uuid": shared_uuid, "chunk_index": 0, "doc_type": "chunk", "content": "x"},
        refresh=True,
    )
    chunk_index.index(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        id=f"{shared_uuid}_digest_sibling",
        body={"file_uuid": shared_uuid, "chunk_index": -1, "doc_type": "digest", "content": "x"},
        refresh=True,
    )

    response = chunk_index.search(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={"size": 100, "query": document_chunk_plane_query(shared_uuid)},
    )
    hits = response["hits"]["hits"]
    assert len(hits) == 1, (
        f"expected exactly the document_chunk doc, got {[h['_id'] for h in hits]}"
    )
    assert hits[0]["_source"]["doc_type"] == "document_chunk"


def test_a_shrinking_reparse_prunes_the_stale_tail(chunk_index):
    """The document-plane sibling of issue #400, exercised end to end."""
    document_uuid = str(uuid_pkg.uuid4())

    first = _index(_chunks(6, marker="ORIGINAL"), document_uuid=document_uuid)
    assert first["chunk_count"] == 6
    assert [int(d["chunk_index"]) for d in _docs(chunk_index, document_uuid)] == list(range(6))

    second = _index(_chunks(2, marker="REPARSED"), document_uuid=document_uuid)
    assert second["chunk_count"] == 2
    assert second["stale_removed"] == 4

    surviving = _docs(chunk_index, document_uuid)
    assert [int(d["chunk_index"]) for d in surviving] == [0, 1]
    assert all("ORIGINAL" not in d["content"] for d in surviving)


def test_prune_touches_only_the_document_being_reindexed(chunk_index):
    sibling_uuid = str(uuid_pkg.uuid4())
    shrinking_uuid = str(uuid_pkg.uuid4())

    _index(_chunks(5, marker="SIBLING"), document_uuid=sibling_uuid)
    _index(_chunks(5, marker="ORIGINAL"), document_uuid=shrinking_uuid)

    result = _index(_chunks(1, marker="EDITED"), document_uuid=shrinking_uuid)
    assert result["stale_removed"] == 4

    assert [int(d["chunk_index"]) for d in _docs(chunk_index, shrinking_uuid)] == [0]
    sibling_docs = _docs(chunk_index, sibling_uuid)
    assert [int(d["chunk_index"]) for d in sibling_docs] == list(range(5))
    assert all("SIBLING" in d["content"] for d in sibling_docs)


# ---------------------------------------------------------------------------
# The full task wiring: real Postgres document_chunk rows -> real OpenSearch docs
# ---------------------------------------------------------------------------


def _new_user(conn) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"docidx_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def test_the_index_document_task_indexes_real_postgres_chunk_rows(
    chunk_index, db_session, monkeypatch
):
    """``documents.index``'s inner implementation, against real ``document`` /
    ``document_chunk`` rows — not hand-built chunk dicts, the actual read path a Celery
    dispatch would exercise.
    """
    from app.models.document import Document
    from app.models.document import DocumentChunk

    @contextlib.contextmanager
    def fake_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr("app.tasks.document_indexing_task.session_scope", fake_scope)

    user_id = _new_user(db_session.connection())
    doc = Document(
        user_id=user_id,
        filename="board_minutes.docx",
        storage_path="tests/documents/board_minutes.docx",
        file_size=4096,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    for i in range(4):
        db_session.add(
            DocumentChunk(
                document_id=doc.id,
                chunk_index=i,
                text=f"Board resolution {i}: approved the annual budget line item.",
                char_start=i * 60,
                char_end=i * 60 + 55,
            )
        )
    db_session.commit()

    from app.tasks.document_indexing_task import _index_document

    result = _index_document(doc.id)
    assert result["status"] == "success", result
    assert result["chunk_count"] == 4

    docs = _docs(chunk_index, str(doc.uuid))
    assert [int(d["chunk_index"]) for d in docs] == [0, 1, 2, 3]
    assert all(d["doc_type"] == "document_chunk" for d in docs)
    assert "Board resolution 2" in docs[2]["content"]
    assert docs[0]["title"] == "board_minutes.docx"


def test_indexing_a_document_with_no_chunks_is_skipped_not_erred(
    chunk_index, db_session, monkeypatch
):
    from app.models.document import Document

    @contextlib.contextmanager
    def fake_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr("app.tasks.document_indexing_task.session_scope", fake_scope)

    user_id = _new_user(db_session.connection())
    doc = Document(
        user_id=user_id,
        filename="empty.txt",
        storage_path="tests/documents/empty.txt",
        file_size=0,
        content_type="text/plain",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    from app.tasks.document_indexing_task import _index_document

    result = _index_document(doc.id)
    assert result == {"status": "skipped", "reason": "no_chunks"}
