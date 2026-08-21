"""``chunk_retrieval``'s document-plane widening against a REAL OpenSearch index
(issue #463, permission matrix row T10).

T10's two halves — the same-integer-id ACL leak, and that the document-plane
ACL rewrite path actually exists — are proven exhaustively at the OpenSearch
query level by ``tests/integration/test_document_plane_acl.py`` (lane C0).
This module does not repeat that proof; it proves the RETRIEVAL layer built
here (``retrieve_chunks``'s ``_widen_to_document_plane``,
``search_document_chunks``) participates correctly with it: a sharee can
retrieve a shared document's chunks through ``retrieve_chunks`` itself (not
just a raw query against the index), a speaker-scoped turn never sees the
document plane even for the owner, and the search-UI document leg finds an
owned document.

Runs against the LIVE/dev OpenSearch cluster the same way every other
service-backed unit test in this suite does (``SKIP_OPENSEARCH`` auto-detected
by TCP probe) — **never a throwaway index**. Every document this module
indexes is deleted via the normal per-file delete path
(``TranscriptIndexingService.delete_transcript_chunks``) in a fixture
``finally``, the same discipline e2e upload tests use for their own data; the
query text for every assertion is a per-test random token so a match can only
be the document this test itself indexed, never real corpus content.
"""

from __future__ import annotations

import contextlib
import os
import uuid as uuid_pkg

import pytest

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason="No OpenSearch reachable (SKIP_OPENSEARCH) — needs the real chunks index.",
    ),
]


@pytest.fixture
def patched_session_scope(monkeypatch, db_session):
    """``update_document_access_index`` opens its own ``session_scope()`` — a
    second, real connection that cannot see this test's uncommitted setup rows
    under READ COMMITTED. Same fix ``test_document_plane_acl.py`` documents:
    monkeypatch the SOURCE module's name, since the task does a local
    ``from app.db.session_utils import session_scope`` inside its body.
    """

    @contextlib.contextmanager
    def fake_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr("app.db.session_utils.session_scope", fake_scope)


def _new_user(db_session):
    from app.core.security import get_password_hash
    from app.models.user import User

    user = User(
        email=f"docretrieve-{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password=get_password_hash("x"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _new_document(db_session, owner_id: int):
    from app.models.document import Document

    doc_uuid = uuid_pkg.uuid4()
    doc = Document(
        uuid=doc_uuid,
        user_id=owner_id,
        filename=f"docretrieve-{doc_uuid.hex[:8]}.pdf",
        storage_path=f"docretrieve-test/{doc_uuid.hex}.pdf",
        file_size=10,
        content_type="application/pdf",
    )
    db_session.add(doc)
    db_session.flush()
    return doc


@pytest.fixture
def indexed_document(db_session):
    """A real ``Document`` row with one real chunk indexed into the LIVE
    chunks index. Cleaned up unconditionally via the normal per-file delete —
    never an index create/delete, only a scoped delete of what this fixture
    wrote.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search.indexing_service import TranscriptIndexingService

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    owner = _new_user(db_session)
    doc = _new_document(db_session, owner.id)
    unique_word = f"unobtainium{uuid_pkg.uuid4().hex[:10]}"

    # NOTE: ``index_document_chunks`` does not yet write ``page``/``section_path``/
    # ``char_start``/``char_end`` into the index document at all (a gap in
    # ``services/search/indexing_service.py``, outside this lane's file set —
    # see the PR/report). Passing them here is harmless (extra dict keys the
    # indexer ignores) and documents the intended shape once that write-side
    # gap closes; this fixture's assertions only check what indexing actually
    # populates today.
    result = TranscriptIndexingService().index_document_chunks(
        document_id=doc.id,
        document_uuid=str(doc.uuid),
        user_id=owner.id,
        chunks=[
            {
                "chunk_index": 0,
                "text": f"the {unique_word} clause covers termination",
                "char_start": 0,
                "char_end": 40,
                "page": 4,
                "section_path": ["Section 5", "Termination"],
            }
        ],
        title="docretrieve.pdf",
    )
    assert isinstance(result, dict), f"document indexing returned a failure sentinel: {result!r}"
    client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)

    try:
        yield owner, doc, unique_word
    finally:
        TranscriptIndexingService().delete_transcript_chunks(str(doc.uuid))
        client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)


class TestRetrieveChunksSeesTheDocumentPlane:
    def test_owner_can_retrieve_their_own_document_chunk(self, indexed_document):
        from app.services.search.chunk_retrieval import retrieve_chunks

        owner, doc, unique_word = indexed_document
        chunks = retrieve_chunks(unique_word, user_id=owner.id, search_mode="keyword")
        assert any(c.file_uuid == str(doc.uuid) and c.is_document for c in chunks), (
            f"owner could not retrieve their own document chunk via retrieve_chunks; got {chunks}"
        )

    def test_a_stranger_cannot_retrieve_it_before_any_share(self, indexed_document):
        from app.services.search.chunk_retrieval import retrieve_chunks

        _owner, doc, unique_word = indexed_document
        stranger = _new_user_id_never_granted_access()
        chunks = retrieve_chunks(unique_word, user_id=stranger, search_mode="keyword")
        assert not any(c.file_uuid == str(doc.uuid) for c in chunks)

    def test_speaker_filtered_retrieval_excludes_the_document_even_for_the_owner(
        self, indexed_document
    ):
        from app.services.search.chunk_retrieval import retrieve_chunks

        owner, doc, unique_word = indexed_document
        chunks = retrieve_chunks(
            unique_word, user_id=owner.id, speakers=["Somebody"], search_mode="keyword"
        )
        assert not any(c.file_uuid == str(doc.uuid) for c in chunks), (
            "a speaker-scoped turn must never surface a document chunk, even to its owner"
        )


def _new_user_id_never_granted_access() -> int:
    """A large, essentially-unused integer id — no share, no ownership, no row
    at all. Retrieval must find nothing for it regardless."""
    return -abs(uuid_pkg.uuid4().int % 1_000_000) - 1


class TestSharedVisibilityThroughRetrieveChunks:
    """T10's shared-visibility half, proven through ``retrieve_chunks`` itself
    — not just a raw OpenSearch query (that half is already proven by
    ``test_document_plane_acl.py``). A vacuous test would pass with the
    negative control removed; it is asserted explicitly first.
    """

    def test_shared_visibility_a_real_share_makes_it_retrievable(
        self, indexed_document, patched_session_scope, monkeypatch, db_session
    ):
        from app.core.config import settings
        from app.services.opensearch_service import get_opensearch_client
        from app.services.search.chunk_retrieval import retrieve_chunks
        from app.tasks import search_indexing_task
        from app.tasks.search_indexing_task import update_document_access_index

        owner, doc, unique_word = indexed_document
        sharee = _new_user(db_session)

        # Negative control FIRST — proves the positive assertion below is not
        # vacuous (i.e. the sharee doesn't just see everything regardless).
        before = retrieve_chunks(unique_word, user_id=sharee.id, search_mode="keyword")
        assert not any(c.file_uuid == str(doc.uuid) for c in before), (
            "test guard failed: the sharee must NOT see the document before any share exists"
        )

        # The seam a real document-sharing lane will drive — monkeypatched
        # here to simulate "a share now grants `sharee` access", exactly as
        # test_document_plane_acl.py does, but the assertion below is at the
        # RETRIEVAL layer instead of a raw OpenSearch query.
        monkeypatch.setattr(
            search_indexing_task,
            "_document_accessible_user_ids",
            lambda db, document_id, owner_id: [owner_id, sharee.id],
        )
        result = update_document_access_index([doc.id])
        assert result["status"] == "success"
        assert result["updated"] == 1

        client = get_opensearch_client()
        assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
        client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)

        after = retrieve_chunks(unique_word, user_id=sharee.id, search_mode="keyword")
        assert any(c.file_uuid == str(doc.uuid) and c.is_document for c in after), (
            f"the shared document must be retrievable by the sharee through "
            f"retrieve_chunks; got {after}"
        )


class TestSearchDocumentChunksAgainstTheLiveIndex:
    def test_search_document_chunks_finds_the_owners_document(self, indexed_document):
        from app.services.search.chunk_retrieval import search_document_chunks

        owner, doc, unique_word = indexed_document
        result = search_document_chunks(unique_word, user_id=owner.id, search_mode="keyword")
        assert any(hit.file_uuid == str(doc.uuid) for hit in result.results), (
            f"search_document_chunks did not find the owner's document; got {result.results}"
        )
        # page/section_path are asserted against a MOCKED source hit in
        # tests/unit/test_document_retrieval.py, not here — indexing does not
        # yet write those fields into the real index (see the fixture note
        # above), so a real hit's page/section_path are legitimately None/[]
        # today.
        hit = next(h for h in result.results if h.file_uuid == str(doc.uuid))
        assert hit.matches[0].snippet

    def test_a_stranger_gets_nothing_from_search_document_chunks(self, indexed_document):
        from app.services.search.chunk_retrieval import search_document_chunks

        _owner, doc, unique_word = indexed_document
        stranger = _new_user_id_never_granted_access()
        result = search_document_chunks(unique_word, user_id=stranger, search_mode="keyword")
        assert not any(hit.file_uuid == str(doc.uuid) for hit in result.results)
