"""Quarantine read rule on the chat, tag-listing and background-export paths.

Multi-tenant mode, platform admin, no support-access grant: the admin role alone does not
reach a quarantined row. Background tasks hold no request and therefore no grant, so there
the review exemption is "single-tenant mode and an admin" only. Single-mode controls keep
the review path honest.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime

import pytest
from fastapi import status

from app.core.config import settings
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.models.media import TranscriptSegment
from app.schemas.transcript_export import TranscriptDocumentExportRequest
from app.services.takedown_service import is_review_admin
from app.services.transcript_document.build import DocumentExportRefusedError
from app.services.transcript_document.build import prepare_document

SNIPPET = "quarantine-rule-snippet-9f3a"


@pytest.fixture
def multi_mode(monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "multi")


@pytest.fixture
def single_mode(monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")


@pytest.fixture
def quarantined_file(db_session, admin_user) -> MediaFile:
    file_uuid = str(uuid_pkg.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="read_rule_tasks.wav",
        title="read_rule_tasks",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        is_public=False,
        is_quarantined=True,
        user_id=admin_user.id,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.add(
        TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media_file.id,
            start_time=0.0,
            end_time=1.0,
            text="hello there",
        )
    )
    db_session.commit()
    return media_file


@pytest.fixture
def conversation(db_session, admin_user, quarantined_file) -> ChatConversation:
    conv = ChatConversation(
        uuid=uuid_pkg.uuid4(), user_id=admin_user.id, title="read-rule", context={}
    )
    db_session.add(conv)
    db_session.commit()
    db_session.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conv.id,
            role="assistant",
            content="answer",
            citations=[
                {
                    "id": 1,
                    "kind": "chunk",
                    "file_uuid": str(quarantined_file.uuid),
                    "title": "t",
                    "chunk_index": 0,
                    "start_time": 0.0,
                    "end_time": 1.0,
                    "speaker": "A",
                    "snippet": SNIPPET,
                    "expanded": False,
                }
            ],
        )
    )
    db_session.commit()
    return conv


class TestChatCitations:
    def test_messages_drop_the_citation_for_an_unreached_admin(
        self, client, admin_token_headers, conversation, multi_mode
    ):
        r = client.get(
            f"/api/chat/conversations/{conversation.uuid}/messages", headers=admin_token_headers
        )
        assert r.status_code == status.HTTP_200_OK, r.text
        assert SNIPPET not in r.text

    def test_messages_keep_the_citation_for_a_single_mode_admin(
        self, client, admin_token_headers, conversation, single_mode
    ):
        r = client.get(
            f"/api/chat/conversations/{conversation.uuid}/messages", headers=admin_token_headers
        )
        assert r.status_code == status.HTTP_200_OK, r.text
        assert SNIPPET in r.text

    def test_export_drops_the_citation_for_an_unreached_admin(
        self, client, admin_token_headers, conversation, multi_mode
    ):
        r = client.get(
            f"/api/chat/conversations/{conversation.uuid}/export",
            headers=admin_token_headers,
            params={"format": "json"},
        )
        assert r.status_code == status.HTTP_200_OK, r.text
        assert SNIPPET not in r.text
        assert json.loads(r.text)  # a real document, not an empty body

    def test_export_keeps_the_citation_for_a_single_mode_admin(
        self, client, admin_token_headers, conversation, single_mode
    ):
        r = client.get(
            f"/api/chat/conversations/{conversation.uuid}/export",
            headers=admin_token_headers,
            params={"format": "json"},
        )
        assert r.status_code == status.HTTP_200_OK, r.text
        assert SNIPPET in r.text


class TestTagFileListing:
    @pytest.fixture
    def tag(self, db_session, admin_user, quarantined_file) -> Tag:
        tag = Tag(
            uuid=uuid_pkg.uuid4(), name=f"rr-{uuid_pkg.uuid4().hex[:8]}", user_id=admin_user.id
        )
        db_session.add(tag)
        db_session.commit()
        db_session.add(FileTag(media_file_id=quarantined_file.id, tag_id=tag.id, source="manual"))
        db_session.commit()
        return tag

    def test_unreached_admin_does_not_see_the_file(
        self, client, admin_token_headers, tag, multi_mode
    ):
        r = client.get(f"/api/tags/{tag.uuid}/files", headers=admin_token_headers)
        assert r.status_code == status.HTTP_200_OK, r.text
        assert r.json()["files"] == []

    def test_single_mode_admin_sees_the_file(self, client, admin_token_headers, tag, single_mode):
        r = client.get(f"/api/tags/{tag.uuid}/files", headers=admin_token_headers)
        assert r.status_code == status.HTTP_200_OK, r.text
        assert len(r.json()["files"]) == 1


class TestBackgroundReviewAdmin:
    """Celery tasks hold a user id and no grant: the async plane fails closed in multi mode."""

    def test_review_admin_is_false_in_multi_mode(self, db_session, admin_user, multi_mode):
        assert is_review_admin(db_session, admin_user.id) is False

    def test_review_admin_is_true_for_a_single_mode_admin(
        self, db_session, admin_user, single_mode
    ):
        assert is_review_admin(db_session, admin_user.id) is True

    def test_review_admin_is_false_for_a_plain_user(self, db_session, normal_user, single_mode):
        assert is_review_admin(db_session, normal_user.id) is False

    def _prepare(self, db_session, admin_user, media_file):
        return prepare_document(
            db_session,
            file_id=media_file.id,
            user_id=admin_user.id,
            organization_id=None,
            request=TranscriptDocumentExportRequest(format="docx"),
            now=datetime.now(UTC),
        )

    def test_document_export_refuses_a_quarantined_file_in_multi_mode(
        self, db_session, admin_user, quarantined_file, multi_mode
    ):
        with pytest.raises(DocumentExportRefusedError) as refused:
            self._prepare(db_session, admin_user, quarantined_file)
        assert refused.value.reason == "failed"

    def test_document_export_does_not_refuse_the_file_for_a_single_mode_admin(
        self, db_session, admin_user, quarantined_file, single_mode
    ):
        reason = None
        try:
            self._prepare(db_session, admin_user, quarantined_file)
        except DocumentExportRefusedError as e:
            reason = e.reason
        assert reason != "failed"
