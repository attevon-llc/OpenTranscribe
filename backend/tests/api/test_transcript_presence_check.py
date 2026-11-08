""" "Has this file been transcribed?" is an EXISTS, not a load of every segment (audit L7).

``if not media_file.transcript_segments`` lazy-loads the whole relationship, every
column of every segment, to test emptiness. For a multi-hour recording that is thousands
of rows read on the request path before the summary or topic request is even queued.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import status

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from tests.query_budget import count_statements

SEGMENT_ROW_LOAD = r"transcript_segment\.text"


def _file(db_session, owner, segments: int) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="presence.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.add_all(
        TranscriptSegment(
            media_file_id=media_file.id, start_time=float(i), end_time=i + 1.0, text=f"s{i}"
        )
        for i in range(segments)
    )
    db_session.commit()
    db_session.expire_all()
    return media_file


@pytest.fixture
def llm_available():
    with (
        patch("app.api.endpoints.summarization.is_llm_available", new=AsyncMock(return_value=True)),
        patch(
            "app.api.endpoints.topics.TopicExtractionService.create_from_settings",
            return_value=MagicMock(),
        ),
    ):
        yield


@pytest.mark.parametrize("path", ["summarize", "extract"])
def test_the_transcript_check_reads_no_segment_rows(
    client, db_session, normal_user, user_token_headers, llm_available, path
):
    media_file = _file(db_session, normal_user, segments=50)

    with count_statements(db_session.connection()) as log:
        response = client.post(f"/api/files/{media_file.uuid}/{path}", headers=user_token_headers)

    assert response.status_code < 400, response.text  # the transcript check passed
    assert log.matching(SEGMENT_ROW_LOAD) == [], log.describe()


@pytest.mark.parametrize("path", ["summarize", "extract"])
def test_a_file_with_no_transcript_is_still_refused(
    client, db_session, normal_user, user_token_headers, llm_available, path
):
    media_file = _file(db_session, normal_user, segments=0)

    response = client.post(f"/api/files/{media_file.uuid}/{path}", headers=user_token_headers)

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "transcri" in response.json()["detail"].lower()


def test_the_bulk_redact_check_reads_no_segment_rows(db_session, normal_user, monkeypatch):
    from app.api.endpoints.files import bulk_action_handlers as handlers
    from app.api.endpoints.files import redaction_gate

    media_file = _file(db_session, normal_user, segments=50)
    monkeypatch.setattr(redaction_gate, "claim_and_dispatch_redaction_scan", lambda *a, **k: None)

    with count_statements(db_session.connection()) as log:
        result = handlers._handle_redact_action(db_session, str(media_file.uuid), media_file.id)

    assert result.error != "NO_TRANSCRIPT", result
    assert log.matching(SEGMENT_ROW_LOAD) == [], log.describe()
