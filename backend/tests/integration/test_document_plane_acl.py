"""``MediaFile.id`` / ``Document.id`` are independent integer sequences, so a same-
integer-id collision between a media file and a document is a real, reachable state —
this is permission-matrix row T10, executed against a real OpenSearch (lane C0 tasks
2 and 3).

Two things are proven here and nowhere else:

* **The leak is closed.** ``update_file_access_index`` / ``update_file_tags_index``
  used to key an ``update_by_query`` on a bare ``{"term": {"file_id": file_id}}``, with
  no plane predicate. Sharing (or tagging) a media file whose id happens to equal some
  document's id silently overwrote that document's chunk ACL — with the MEDIA file's
  grant list, which can belong to an entirely different set of users. This module
  seeds exactly that collision and proves the document's chunks are untouched.
* **The document-plane ACL rewrite path exists and actually makes a chunk visible to
  a new grantee.** Before this lane, nothing could ever widen a document chunk's
  ``accessible_user_ids`` past ``[owner_id]`` — ``update_document_access_index`` is
  the missing rewrite a future document-sharing lane needs, and this asserts it
  produces a real, sharee-matching field in a real index, not just a plausible-looking
  ``update_by_query`` body.

Point the suite at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 \\
        pytest backend/tests/integration/test_document_plane_acl.py -m integration
"""

from __future__ import annotations

import contextlib
import os
import uuid as uuid_pkg

import pytest
from sqlalchemy import text

from app.core.config import settings

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and export "
            "OPENSEARCH_PORT — a stand-in index cannot validate update_by_query semantics."
        ),
    ),
]


@pytest.fixture
def chunk_index(monkeypatch):
    """A throwaway chunks index with the REAL mapping — same fixture shape as
    ``test_document_indexing_opensearch.py`` and ``test_chunk_pruning_opensearch.py``.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    name = f"test_t10_acl_{uuid_pkg.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
    svc.reset_neural_pipeline_state()
    try:
        yield client
    finally:
        client.indices.delete(index=name, ignore=[404])
        svc.reset_neural_pipeline_state()


@pytest.fixture
def patched_session_scope(monkeypatch, db_session):
    """``update_file_access_index`` / ``update_document_access_index`` each open their
    own ``session_scope()`` — a second, real connection that cannot see this test's
    uncommitted setup rows under READ COMMITTED. Same fix ``test_document_parse_task.py``
    and ``test_chat_endpoints.py`` document: monkeypatch the SOURCE module's name (both
    tasks do ``from app.db.session_utils import session_scope`` locally, inside the
    function body, so patching the attribute on ``app.db.session_utils`` itself is what
    the lazy import re-reads at call time).
    """

    @contextlib.contextmanager
    def fake_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr("app.db.session_utils.session_scope", fake_scope)


def _shared_id(db_session) -> int:
    """A single integer value guaranteed unused by EITHER the ``media_file`` or the
    ``document`` sequence, so setting it explicitly as the primary key of one row in
    each table is a genuine, reproducible T10 collision — not a hope that two
    independent ``SERIAL`` counters happened to agree.
    """
    return int(
        db_session.execute(
            text("SELECT GREATEST(nextval('media_file_id_seq'), nextval('document_id_seq')) + 1000")
        ).scalar()
    )


def _new_user(db_session):
    from app.core.security import get_password_hash
    from app.models.user import User

    user = User(
        email=f"t10-{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password=get_password_hash("x"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _new_media_file(db_session, owner_id: int, explicit_id: int):
    from app.models.media import MediaFile

    file_uuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        id=explicit_id,
        uuid=file_uuid,
        user_id=owner_id,
        filename=f"t10-{file_uuid.hex[:8]}.wav",
        storage_path=f"t10-test/{file_uuid.hex}.wav",
        content_type="audio/wav",
        file_size=1024,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _new_document(db_session, owner_id: int, explicit_id: int):
    from app.models.document import Document

    doc_uuid = uuid_pkg.uuid4()
    doc = Document(
        id=explicit_id,
        uuid=doc_uuid,
        user_id=owner_id,
        filename=f"t10-{doc_uuid.hex[:8]}.pdf",
        storage_path=f"t10-test/{doc_uuid.hex}.pdf",
        file_size=10,
        content_type="application/pdf",
    )
    db_session.add(doc)
    db_session.flush()
    return doc


def _index_media_chunks(*, file_id: int, file_uuid: str, owner_id: int):
    from app.services.search.indexing_service import TranscriptIndexingService

    segments = [{"start": 0.0, "end": 2.0, "text": "the quarterly budget review", "speaker": "A"}]
    result = TranscriptIndexingService().index_transcript_chunks(
        file_id=file_id,
        file_uuid=file_uuid,
        user_id=owner_id,
        segments=segments,
        title="t10 media",
        speakers=["A"],
        tags=[],
        language="en",
        content_type="audio/wav",
        accessible_user_ids=[owner_id],
    )
    assert isinstance(result, dict), f"media indexing returned a failure sentinel: {result!r}"
    return result


def _index_document_chunks(*, document_id: int, document_uuid: str, owner_id: int):
    from app.services.search.indexing_service import TranscriptIndexingService

    chunks = [{"chunk_index": 0, "text": "t10 document body", "char_start": 0, "char_end": 18}]
    result = TranscriptIndexingService().index_document_chunks(
        document_id=document_id,
        document_uuid=document_uuid,
        user_id=owner_id,
        chunks=chunks,
        title="t10 document.pdf",
    )
    assert isinstance(result, dict), f"document indexing returned a failure sentinel: {result!r}"
    return result


def _accessible_ids_for(client, index_name: str, *, file_uuid: str, doc_type: str) -> list[int]:
    client.indices.refresh(index=index_name)
    response = client.search(
        index=index_name,
        body={
            "size": 10,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"file_uuid": file_uuid}},
                        {"term": {"doc_type": doc_type}},
                    ]
                }
            },
        },
    )
    hits = response["hits"]["hits"]
    assert hits, f"expected at least one {doc_type} document for {file_uuid}"
    return sorted(hits[0]["_source"]["accessible_user_ids"])


class TestTheFileIdCollisionDoesNotLeakAcrossPlanes:
    """T10, leak half: sharing the media file must not touch the document's ACL."""

    def test_the_collision_leak_is_closed(
        self, chunk_index, db_session, patched_session_scope, monkeypatch
    ):
        from app.services.permission_service import PermissionService
        from app.tasks.search_indexing_task import update_file_access_index

        shared_id = _shared_id(db_session)
        media_owner = _new_user(db_session)
        doc_owner = _new_user(db_session)
        sharee = _new_user(db_session)

        media_file = _new_media_file(db_session, media_owner.id, shared_id)
        document = _new_document(db_session, doc_owner.id, shared_id)

        _index_media_chunks(
            file_id=shared_id, file_uuid=str(media_file.uuid), owner_id=media_owner.id
        )
        _index_document_chunks(
            document_id=shared_id, document_uuid=str(document.uuid), owner_id=doc_owner.id
        )
        # The bulk load indexes with refresh=False (issue #435) — update_by_query's own
        # search phase must not race it, or it finds nothing to rewrite.
        chunk_index.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)

        monkeypatch.setattr(
            PermissionService,
            "get_users_with_file_access",
            staticmethod(lambda db, file_id: [media_owner.id, sharee.id]),
        )

        result = update_file_access_index([shared_id])
        assert result["status"] == "success"
        assert result["updated"] >= 1, "the media chunk itself must have been rewritten"

        media_acl = _accessible_ids_for(
            chunk_index,
            settings.OPENSEARCH_CHUNKS_INDEX,
            file_uuid=str(media_file.uuid),
            doc_type="chunk",
        )
        assert media_acl == sorted([media_owner.id, sharee.id]), (
            "the media file's own chunk must pick up the new grant"
        )

        document_acl = _accessible_ids_for(
            chunk_index,
            settings.OPENSEARCH_CHUNKS_INDEX,
            file_uuid=str(document.uuid),
            doc_type="document_chunk",
        )
        assert document_acl == [doc_owner.id], (
            "the LEAK this test guards: a same-integer-id document must keep its own "
            f"ACL, unaffected by the media file's share. Got {document_acl}"
        )


class TestTheDocumentPlaneAclRewritePath:
    """T10, shared-visibility half: the missing rewrite now exists and works."""

    def test_update_document_access_index_widens_the_grant_a_sharee_can_be_found_by(
        self, chunk_index, db_session, patched_session_scope, monkeypatch
    ):
        from app.tasks import search_indexing_task
        from app.tasks.search_indexing_task import update_document_access_index

        owner = _new_user(db_session)
        sharee = _new_user(db_session)
        # A document id that does NOT collide with anything — this test is about the
        # rewrite mechanism itself, not the collision (covered above).
        document_id = _shared_id(db_session) + 1
        document = _new_document(db_session, owner.id, document_id)

        _index_document_chunks(
            document_id=document_id, document_uuid=str(document.uuid), owner_id=owner.id
        )
        chunk_index.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)

        before = _accessible_ids_for(
            chunk_index,
            settings.OPENSEARCH_CHUNKS_INDEX,
            file_uuid=str(document.uuid),
            doc_type="document_chunk",
        )
        assert before == [owner.id], "a freshly indexed document is owner-only, by design"

        # The seam a real document-sharing lane will replace: monkeypatched here to
        # simulate "a share now grants `sharee` access", driving the REAL
        # update_document_access_index end to end — nothing about the OpenSearch call
        # itself is mocked.
        monkeypatch.setattr(
            search_indexing_task,
            "_document_accessible_user_ids",
            lambda db, document_id, owner_id: [owner_id, sharee.id],
        )

        result = update_document_access_index([document_id])
        assert result["status"] == "success"
        assert result["updated"] == 1

        after = _accessible_ids_for(
            chunk_index,
            settings.OPENSEARCH_CHUNKS_INDEX,
            file_uuid=str(document.uuid),
            doc_type="document_chunk",
        )
        assert after == sorted([owner.id, sharee.id])

        # The retrieval gate itself: a sharee-scoped `terms` filter — the shape
        # `hybrid_search_service` applies on every query — must now match this chunk.
        # This is the outcome T10's shared-visibility half is actually about: not that
        # a field CONTAINS the sharee's id, but that the id is queryable the same way
        # production filters on it.
        chunk_index.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
        response = chunk_index.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body={
                "size": 10,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"file_uuid": str(document.uuid)}},
                            {"terms": {"accessible_user_ids": [sharee.id]}},
                        ]
                    }
                },
            },
        )
        assert response["hits"]["hits"], (
            "the shared document must be retrievable by the sharee's accessible_user_ids"
        )

    def test_update_document_access_index_does_not_touch_a_colliding_media_file(
        self, chunk_index, db_session, patched_session_scope
    ):
        """The mirror of the leak test above, run from the document-plane side."""
        from app.tasks.search_indexing_task import update_document_access_index

        shared_id = _shared_id(db_session)
        media_owner = _new_user(db_session)
        doc_owner = _new_user(db_session)

        media_file = _new_media_file(db_session, media_owner.id, shared_id)
        document = _new_document(db_session, doc_owner.id, shared_id)

        _index_media_chunks(
            file_id=shared_id, file_uuid=str(media_file.uuid), owner_id=media_owner.id
        )
        _index_document_chunks(
            document_id=shared_id, document_uuid=str(document.uuid), owner_id=doc_owner.id
        )
        chunk_index.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)

        result = update_document_access_index([shared_id])
        assert result["status"] == "success"
        assert result["updated"] == 1

        media_acl = _accessible_ids_for(
            chunk_index,
            settings.OPENSEARCH_CHUNKS_INDEX,
            file_uuid=str(media_file.uuid),
            doc_type="chunk",
        )
        assert media_acl == [media_owner.id], (
            "the document-plane rewrite must not touch the colliding media file's chunk"
        )
