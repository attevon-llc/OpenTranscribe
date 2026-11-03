"""A failed read of the caller's LLM settings is a 503 with a fixed sentence.

A database error while resolving the provider is an outage: the request may succeed in a
moment. It must not be answered with a 500, nor read as "no LLM is configured" (which tells
the user to redo setup that is fine), nor with a silently assumed context window. The
sentence is fixed so SQL and connection details never reach the client.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from app.models.media import MediaFile
from app.models.media import TranscriptSegment

UNREADABLE = "AI provider settings could not be read. Try again in a moment."
RESOLVE = "app.services.llm_service.LLMService._resolve_user_llm_settings"
LEAK_MARKER = "SELECT secret_column FROM user_llm_settings"


@pytest.fixture
def settings_unreadable() -> Iterator[None]:
    error = OperationalError(LEAK_MARKER, {}, Exception("connection reset"))
    with patch(RESOLVE, side_effect=error):
        yield


@pytest.fixture
def stream_slot() -> Iterator[MagicMock]:
    """The chat concurrency slot, stubbed; yields the release mock to assert it was freed."""
    with (
        patch("app.services.chat.limits.acquire_stream_slot", return_value="slot-test"),
        patch("app.services.chat.limits.release_stream_slot") as release,
    ):
        yield release


#: (status, detail, Retry-After, exception text leaked) of a correct outage response.
OUTAGE = (503, UNREADABLE, "5", False)


def _outage_fields(response) -> tuple:
    detail = response.json().get("detail") if response.status_code != 500 else None
    return (
        response.status_code,
        detail,
        response.headers.get("Retry-After"),
        LEAK_MARKER in response.text,
    )


def test_chat_send_is_a_503_when_llm_settings_cannot_be_read(
    client, auth_headers, settings_unreadable, stream_slot
):
    created = client.post("/api/chat/conversations", json={}, headers=auth_headers)
    assert created.status_code == 201, created.text

    response = client.post(
        f"/api/chat/conversations/{created.json()['uuid']}/messages",
        json={"content": "hi"},
        headers=auth_headers,
    )

    assert _outage_fields(response) == OUTAGE
    assert stream_slot.call_count == 1
    assert stream_slot.call_args.args[1] == "slot-test"


def test_answer_budget_is_a_503_when_llm_settings_cannot_be_read(
    client, auth_headers, settings_unreadable
):
    response = client.get("/api/chat/answer-budget", headers=auth_headers)
    assert _outage_fields(response) == OUTAGE


def test_context_estimate_is_a_503_not_an_assumed_window(client, auth_headers, settings_unreadable):
    response = client.post(
        "/api/chat/context/estimate",
        json={"file_uuids": [], "collection_uuids": [], "tag_names": [], "speakers": []},
        headers=auth_headers,
    )
    assert _outage_fields(response) == OUTAGE


def _file_with_transcript(db_session, owner) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="settings-outage.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
    )
    db_session.add(media)
    db_session.commit()
    db_session.add(
        TranscriptSegment(media_file_id=media.id, start_time=0.0, end_time=1.0, text="hi")
    )
    db_session.commit()
    db_session.refresh(media)
    return media


def test_topic_extract_is_a_503_when_llm_settings_cannot_be_read(
    client, user_token_headers, normal_user, db_session, settings_unreadable
):
    media = _file_with_transcript(db_session, normal_user)
    response = client.post(f"/api/files/{media.uuid}/extract", headers=user_token_headers)
    assert _outage_fields(response) == OUTAGE


def test_batch_topic_extract_is_a_503_when_llm_settings_cannot_be_read(
    client, user_token_headers, normal_user, db_session, settings_unreadable
):
    media = _file_with_transcript(db_session, normal_user)
    response = client.post(
        "/api/files/batch-extract",
        json={"file_uuids": [str(media.uuid)]},
        headers=user_token_headers,
    )
    assert _outage_fields(response) == OUTAGE
