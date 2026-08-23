"""Document notes — the document arm of the comments API (v400, #362 lane C5).

Mirrors ``test_comments.py``'s DB-backed, cross-user-checking shape for the media-file
arm, scoped to the new ``/comments/documents/{document_uuid}/comments`` routes and the
``DocumentShare``-based permission rule. The T-matrix this pins: a sharee sees notes per
policy (visible with a share, absent without one), and there is no cross-user leak.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core.enums import FileStatus
from app.models.document import Document
from app.models.document import DocumentChunk
from app.models.document import DocumentShare
from app.models.media import Comment

COMMENTS_PATH = "/api/comments"


def _make_document(db_session, user, *, status=FileStatus.COMPLETED) -> Document:
    doc_uuid = uuid_pkg.uuid4()
    row = Document(
        uuid=doc_uuid,
        user_id=user.id,
        filename=f"{doc_uuid}.pdf",
        storage_path=f"documents/test/{doc_uuid}.pdf",
        file_size=100,
        content_type="application/pdf",
        status=status,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _make_chunk(db_session, document, *, chunk_index: int = 0) -> DocumentChunk:
    row = DocumentChunk(
        document_id=document.id,
        chunk_index=chunk_index,
        text="hello world",
        char_start=0,
        char_end=11,
        section_path=[],
        block_types=[],
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _share_document(db_session, document, *, owner, with_user, permission="viewer") -> None:
    db_session.add(
        DocumentShare(
            uuid=uuid_pkg.uuid4(),
            document_id=document.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=with_user.id,
            permission=permission,
        )
    )
    db_session.commit()


def _make_note(db_session, document, author, text: str = "a note") -> Comment:
    row = Comment(
        uuid=uuid_pkg.uuid4(),
        document_id=document.id,
        user_id=author.id,
        text=text,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def owned_document(db_session, normal_user) -> Document:
    return _make_document(db_session, normal_user)


@pytest.fixture
def own_note(db_session, owned_document, normal_user) -> Comment:
    return _make_note(db_session, owned_document, normal_user)


class TestCreateNote:
    def test_owner_can_add_a_note(self, client, owned_document, user_token_headers):
        response = client.post(
            f"{COMMENTS_PATH}/documents/{owned_document.uuid}/comments",
            headers=user_token_headers,
            json={"text": "great point"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["text"] == "great point"
        assert body["document_id"] == str(owned_document.uuid)
        assert body["media_file_id"] is None

    def test_owner_can_anchor_a_note_to_a_chunk(
        self, client, db_session, owned_document, user_token_headers
    ):
        chunk = _make_chunk(db_session, owned_document, chunk_index=0)
        response = client.post(
            f"{COMMENTS_PATH}/documents/{owned_document.uuid}/comments",
            headers=user_token_headers,
            json={"text": "anchored", "document_chunk_index": chunk.chunk_index},
        )
        assert response.status_code == 200, response.text
        assert response.json()["document_chunk_index"] == chunk.chunk_index

    def test_an_out_of_range_chunk_index_is_400(self, client, owned_document, user_token_headers):
        response = client.post(
            f"{COMMENTS_PATH}/documents/{owned_document.uuid}/comments",
            headers=user_token_headers,
            json={"text": "bad anchor", "document_chunk_index": 999},
        )
        assert response.status_code == 400

    def test_a_stranger_cannot_add_a_note(self, client, owned_document, other_user_auth_headers):
        response = client.post(
            f"{COMMENTS_PATH}/documents/{owned_document.uuid}/comments",
            headers=other_user_auth_headers,
            json={"text": "intrusion"},
        )
        assert response.status_code == 404

    def test_a_sharee_can_add_a_note(
        self, client, db_session, owned_document, normal_user, other_user, other_user_auth_headers
    ):
        _share_document(db_session, owned_document, owner=normal_user, with_user=other_user)
        response = client.post(
            f"{COMMENTS_PATH}/documents/{owned_document.uuid}/comments",
            headers=other_user_auth_headers,
            json={"text": "collaborator note"},
        )
        assert response.status_code == 200, response.text


class TestListNotes:
    def test_owner_sees_their_notes(self, client, owned_document, own_note, user_token_headers):
        response = client.get(
            f"{COMMENTS_PATH}/documents/{owned_document.uuid}/comments",
            headers=user_token_headers,
        )
        assert response.status_code == 200
        uuids = {c["uuid"] for c in response.json()}
        assert str(own_note.uuid) in uuids

    def test_a_sharee_sees_notes_per_policy(
        self,
        client,
        db_session,
        owned_document,
        own_note,
        normal_user,
        other_user,
        other_user_auth_headers,
    ):
        """The T-matrix's positive half: a viewer share grants read access to notes."""
        _share_document(db_session, owned_document, owner=normal_user, with_user=other_user)
        response = client.get(
            f"{COMMENTS_PATH}/documents/{owned_document.uuid}/comments",
            headers=other_user_auth_headers,
        )
        assert response.status_code == 200
        uuids = {c["uuid"] for c in response.json()}
        assert str(own_note.uuid) in uuids

    def test_an_unshared_document_leaks_no_notes_to_a_stranger(
        self, client, owned_document, own_note, other_user_auth_headers
    ):
        """The T-matrix's negative half: no share, no access — not even a 200 with
        an empty list, which would still confirm the document exists.
        """
        response = client.get(
            f"{COMMENTS_PATH}/documents/{owned_document.uuid}/comments",
            headers=other_user_auth_headers,
        )
        assert response.status_code == 404


class TestDeleteNote:
    def test_author_can_delete_their_own_note(self, client, own_note, user_token_headers):
        response = client.delete(f"{COMMENTS_PATH}/{own_note.uuid}", headers=user_token_headers)
        assert response.status_code == 204

    def test_a_stranger_cannot_delete_a_note(self, client, own_note, other_user_auth_headers):
        """``_assert_comment_file_in_scope`` (not ``_check_document_access``) gates
        this path, so a stranger with no ``DocumentShare`` gets exactly 403 — not
        the 404-for-existence-hiding this module's read paths use.
        """
        response = client.delete(
            f"{COMMENTS_PATH}/{own_note.uuid}", headers=other_user_auth_headers
        )
        assert response.status_code == 403

    def test_a_platform_admin_can_delete_any_note(self, client, own_note, admin_token_headers):
        response = client.delete(f"{COMMENTS_PATH}/{own_note.uuid}", headers=admin_token_headers)
        assert response.status_code == 204


def test_a_media_comment_still_round_trips_unaffected(
    client, db_session, normal_user, user_token_headers
):
    """Non-regression: widening ``comment`` for documents must not disturb the
    existing media-file arm's response shape.
    """
    from app.models.media import MediaFile

    file_uuid = uuid_pkg.uuid4()
    media = MediaFile(
        uuid=file_uuid,
        filename=f"{file_uuid}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        user_id=normal_user.id,
        status="completed",
    )
    db_session.add(media)
    db_session.commit()

    response = client.post(
        f"{COMMENTS_PATH}/files/{media.uuid}/comments",
        headers=user_token_headers,
        json={"text": "still works", "timestamp": 5.0, "media_file_id": str(media.uuid)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["media_file_id"] == str(media.uuid)
    assert body["document_id"] is None
    assert body["document_chunk_index"] is None
