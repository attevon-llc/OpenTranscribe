"""``update_file_access_index`` / ``update_document_access_index`` must COUNT an
unresolvable file/document as an error, never silently ``continue`` past it (lane C0
task 5).

``PermissionService.get_users_with_file_access`` always seeds its result with the
file's own owner unless the ``MediaFile`` row itself cannot be found — so an empty
result is never "this file legitimately has zero accessible users," it is "this
file_id could not be resolved." The old ``if not accessible_ids: continue`` could not
tell that apart from a successful update of zero users; nothing in the returned dict
said which ids were skipped. Same shape for ``update_document_access_index``'s
``Document`` lookup.

No live OpenSearch needed — a stub client records what it was asked to update, which
is enough to prove the missing id short-circuits BEFORE any ``update_by_query`` is
issued for it, while a resolvable id in the same batch still gets rewritten.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_pkg

import pytest
from sqlalchemy import text


@pytest.fixture(autouse=True)
def _patched_session_scope(monkeypatch, db_session):
    @contextlib.contextmanager
    def fake_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr("app.db.session_utils.session_scope", fake_scope)


class _StubOpenSearchClient:
    """Records every ``update_by_query`` call; never touches a real cluster."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def update_by_query(self, *, index, body, **kwargs):
        self.calls.append({"index": index, "body": body, **kwargs})
        return {"updated": 1}


def _new_user(db_session):
    from app.core.security import get_password_hash
    from app.models.user import User

    user = User(
        email=f"errcount-{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password=get_password_hash("x"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _new_media_file(db_session, owner_id: int):
    from app.models.media import MediaFile

    file_uuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner_id,
        filename=f"errcount-{file_uuid.hex[:8]}.wav",
        storage_path=f"errcount-test/{file_uuid.hex}.wav",
        content_type="audio/wav",
        file_size=1024,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _new_document(db_session, owner_id: int):
    from app.models.document import Document

    doc_uuid = uuid_pkg.uuid4()
    doc = Document(
        uuid=doc_uuid,
        user_id=owner_id,
        filename=f"errcount-{doc_uuid.hex[:8]}.pdf",
        storage_path=f"errcount-test/{doc_uuid.hex}.pdf",
        file_size=10,
        content_type="application/pdf",
    )
    db_session.add(doc)
    db_session.flush()
    return doc


class TestUpdateFileAccessIndexCountsUnresolvableFiles:
    def test_a_nonexistent_media_file_id_is_counted_as_an_error_not_skipped(
        self, db_session, monkeypatch
    ):
        from app.tasks import search_indexing_task

        owner = _new_user(db_session)
        real_file = _new_media_file(db_session, owner.id)
        # Guaranteed not to resolve: higher than anything the sequence has issued.
        missing_id = int(
            db_session.execute(text("SELECT nextval('media_file_id_seq') + 1000000")).scalar()
        )

        stub = _StubOpenSearchClient()
        monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: stub)

        result = search_indexing_task.update_file_access_index([missing_id, real_file.id])

        assert result["status"] == "success"
        assert result["errors"] == 1
        assert result["missing_file_ids"] == [missing_id]
        # Exactly one update_by_query call — the resolvable file only. The missing id
        # must never reach the OpenSearch client at all.
        assert len(stub.calls) == 1
        assert stub.calls[0]["body"]["query"]["bool"]["filter"][0] == {
            "term": {"file_id": real_file.id}
        }

    def test_missing_file_ids_is_absent_when_everything_resolves(self, db_session, monkeypatch):
        """The contract from the docstring: the key is only present when non-empty —
        a caller checking ``"missing_file_ids" in result`` must see it absent on a
        clean run, not an empty list."""
        from app.tasks import search_indexing_task

        owner = _new_user(db_session)
        real_file = _new_media_file(db_session, owner.id)

        stub = _StubOpenSearchClient()
        monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: stub)

        result = search_indexing_task.update_file_access_index([real_file.id])

        assert result["errors"] == 0
        assert "missing_file_ids" not in result


class TestUpdateDocumentAccessIndexCountsUnresolvableDocuments:
    def test_a_nonexistent_document_id_is_counted_as_an_error_not_skipped(
        self, db_session, monkeypatch
    ):
        from app.tasks import search_indexing_task

        owner = _new_user(db_session)
        real_document = _new_document(db_session, owner.id)
        missing_id = int(
            db_session.execute(text("SELECT nextval('document_id_seq') + 1000000")).scalar()
        )

        stub = _StubOpenSearchClient()
        monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: stub)

        result = search_indexing_task.update_document_access_index([missing_id, real_document.id])

        assert result["status"] == "success"
        assert result["errors"] == 1
        assert result["missing_document_ids"] == [missing_id]
        assert len(stub.calls) == 1
        assert stub.calls[0]["body"]["query"]["bool"]["filter"][0] == {
            "term": {"file_id": real_document.id}
        }

    def test_missing_document_ids_is_absent_when_everything_resolves(self, db_session, monkeypatch):
        from app.tasks import search_indexing_task

        owner = _new_user(db_session)
        real_document = _new_document(db_session, owner.id)

        stub = _StubOpenSearchClient()
        monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: stub)

        result = search_indexing_task.update_document_access_index([real_document.id])

        assert result["errors"] == 0
        assert "missing_document_ids" not in result
