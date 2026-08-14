"""Functional characterization tests for the summarization endpoints.

Covers ``summarization.py`` (mounted at ``/api/files`` + ``/api/files/...``):

- ``POST   /api/files/{uuid}/summarize``          (trigger; unconfigured-LLM 503)
- ``GET    /api/files/{uuid}/summary``            (get; no-summary 404)
- ``DELETE /api/files/{uuid}/summary``            (delete; no-summary 404)
- ``POST   /api/files/{uuid}/identify-speakers``  (LLM speaker-id)
- ``GET    /api/files/analytics``                 (handler removed — still 422, shadowed)

``POST /api/files/search`` used to be covered here. It searched the retired
``transcript_summaries`` OpenSearch index and was unmounted with it (#67); the
404 and the wider retirement are pinned in
``tests/unit/test_transcript_summaries_index_retired.py``.

This stack has NO LLM provider configured, so the live default for the
LLM-gated endpoints is the 503 unconfigured path — that is the primary thing we
pin. ``is_llm_available`` is patched (at the summarization module's import site)
only to make that 503 deterministic and to exercise the configured-path branch
(where the no-opped Celery dispatch is reached). File-ownership 403/404 is the
shared ``get_file_by_uuid_with_permission`` contract. Rows live on the
savepoint-isolated ``db_session``.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock
from unittest.mock import patch

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment


def _make_file(db_session, owner, *, with_transcript: bool = False) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    mf = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="sum_test.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    if with_transcript:
        db_session.add(
            TranscriptSegment(media_file_id=mf.id, start_time=0.0, end_time=1.0, text="hello")
        )
        db_session.commit()
        db_session.refresh(mf)
    return mf


def _llm(available: bool):
    """Patch ``is_llm_available`` at the summarization module import site."""
    return patch(
        "app.api.endpoints.summarization.is_llm_available",
        new=AsyncMock(return_value=available),
    )


# ---------------------------------------------------------------------------
# POST /{uuid}/summarize
# ---------------------------------------------------------------------------


def test_summarize_unauthorized(client):
    response = client.post(f"/api/files/{uuid.uuid4()}/summarize")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_summarize_nonexistent_404(client, user_token_headers):
    response = client.post(f"/api/files/{uuid.uuid4()}/summarize", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_summarize_other_user_403(client, other_user_auth_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user, with_transcript=True)
    response = client.post(f"/api/files/{mf.uuid}/summarize", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_summarize_no_transcript_400(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user, with_transcript=False)
    response = client.post(f"/api/files/{mf.uuid}/summarize", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == (
        "File must have completed transcription before summarization"
    )


def test_summarize_llm_unconfigured_503(client, user_token_headers, normal_user, db_session):
    """The live default on this stack: no LLM provider → 503."""
    mf = _make_file(db_session, normal_user, with_transcript=True)
    with _llm(False):
        response = client.post(f"/api/files/{mf.uuid}/summarize", headers=user_token_headers)
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "AI summarization is currently unavailable" in response.json()["detail"]


def test_summarize_llm_configured_dispatches(client, user_token_headers, normal_user, db_session):
    """Configured-path branch: dispatch is reached (Celery no-opped by conftest)."""
    mf = _make_file(db_session, normal_user, with_transcript=True)
    with _llm(True):
        response = client.post(f"/api/files/{mf.uuid}/summarize", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["message"] == "Summarization task started"
    assert "task_id" in body


# ---------------------------------------------------------------------------
# GET /{uuid}/summary
# ---------------------------------------------------------------------------


def test_get_summary_unauthorized(client):
    response = client.get(f"/api/files/{uuid.uuid4()}/summary")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_get_summary_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/files/{uuid.uuid4()}/summary", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_get_summary_other_user_403(client, other_user_auth_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{mf.uuid}/summary", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_get_summary_none_available_404(client, user_token_headers, normal_user, db_session):
    """A file with no ``summary_data`` is a 404."""
    mf = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{mf.uuid}/summary", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == (
        "No summary available for this file. Please generate one first."
    )


def test_get_summary_reads_the_postgres_column(client, user_token_headers, normal_user, db_session):
    """``media_file.summary_data`` is the source, and since #67 the only one.

    This used to be titled "postgres fallback" and could not distinguish the
    column from the ``transcript_summaries`` copy the handler preferred: whether
    OpenSearch was reachable changed which store answered, and both held the same
    dict, so the assertion passed either way.
    """
    mf = _make_file(db_session, normal_user)
    mf.summary_data = {"bluf": "A short summary."}
    db_session.commit()
    response = client.get(f"/api/files/{mf.uuid}/summary", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["file_id"] == str(mf.uuid)
    assert body["summary_data"]["bluf"] == "A short summary."


# ---------------------------------------------------------------------------
# DELETE /{uuid}/summary
# ---------------------------------------------------------------------------


def test_delete_summary_none_404(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    response = client.delete(f"/api/files/{mf.uuid}/summary", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "No summary found to delete"


def test_delete_summary_postgres(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    mf.summary_data = {"bluf": "delete me"}
    db_session.commit()
    response = client.delete(f"/api/files/{mf.uuid}/summary", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["message"] == "Summary deleted successfully"


def test_delete_summary_other_user_403(client, other_user_auth_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    response = client.delete(f"/api/files/{mf.uuid}/summary", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# POST /{uuid}/identify-speakers
# ---------------------------------------------------------------------------


def test_identify_speakers_no_speakers_400(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    response = client.post(f"/api/files/{mf.uuid}/identify-speakers", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "No speakers found in this file to identify"


def test_identify_speakers_llm_unconfigured_503(
    client, user_token_headers, normal_user, db_session
):
    mf = _make_file(db_session, normal_user)
    db_session.add(Speaker(user_id=normal_user.id, media_file_id=mf.id, name="SPEAKER_00"))
    db_session.commit()
    with _llm(False):
        response = client.post(
            f"/api/files/{mf.uuid}/identify-speakers", headers=user_token_headers
        )
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "AI speaker identification is currently unavailable" in response.json()["detail"]


def test_identify_speakers_llm_configured_dispatches(
    client, user_token_headers, normal_user, db_session
):
    mf = _make_file(db_session, normal_user)
    db_session.add(Speaker(user_id=normal_user.id, media_file_id=mf.id, name="SPEAKER_00"))
    db_session.commit()
    with _llm(True):
        response = client.post(
            f"/api/files/{mf.uuid}/identify-speakers", headers=user_token_headers
        )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["message"] == "Speaker identification task started"
    assert body["speaker_count"] == 1


def test_identify_speakers_other_user_403(client, other_user_auth_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{mf.uuid}/identify-speakers", headers=other_user_auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# GET /analytics  (never-mounted, permanently shadowed)
# ---------------------------------------------------------------------------


def test_analytics_path_shadowed_by_file_detail_route(client, user_token_headers):
    """``GET /api/files/analytics`` resolves to the files router's
    ``GET /api/files/{file_uuid}`` (file_uuid: UUID), which 422s on the non-UUID
    ``analytics`` segment.

    A summarization ``GET /analytics`` handler used to be mounted here but was
    permanently shadowed by that earlier parameterized route (it never ran), had
    no frontend caller, and has been removed. Observable behavior is unchanged
    (still 422); pinned so the path can't silently start routing somewhere else.
    """
    response = client.get("/api/files/analytics", headers=user_token_headers)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
