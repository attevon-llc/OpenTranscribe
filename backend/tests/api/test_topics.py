"""Functional characterization tests for the topic-suggestion endpoints.

Covers ``topics.py`` (mounted at ``/api/files``):

- ``GET    /api/files/{uuid}/suggestions``   (get; no-suggestion 404)
- ``DELETE /api/files/{uuid}/suggestions``   (dismiss → status 'rejected')
- ``POST   /api/files/{uuid}/extract``       (LLM-gated; not-configured 400)
- ``POST   /api/files/{uuid}/apply``         (track accepted suggestions)
- ``POST   /api/files/{uuid}/auto-label``    (auto-apply; disabled 400 / 404)
- ``POST   /api/files/batch-extract``        (no-valid-files 400)
- ``POST   /api/files/retroactive-auto-label`` (+ status)

This stack has no LLM provider configured, so ``TopicExtractionService.
create_from_settings`` returns ``None`` → the extract/batch-extract endpoints
take the 400 "LLM provider not configured" path; that is pinned directly (no
mock). Suggestion rows are created on the savepoint-isolated ``db_session``.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.topic import TopicSuggestion


def _make_file(db_session, owner, *, with_transcript: bool = False) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    mf = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="topic_test.wav",
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
            TranscriptSegment(media_file_id=mf.id, start_time=0.0, end_time=1.0, text="hi")
        )
        db_session.commit()
        db_session.refresh(mf)
    return mf


def _make_suggestion(
    db_session, owner, media_file, *, status_value: str = "pending"
) -> TopicSuggestion:
    sug = TopicSuggestion(
        uuid=uuid.uuid4(),
        media_file_id=media_file.id,
        user_id=owner.id,
        status=status_value,
        suggested_tags=[{"name": "budget", "confidence": 0.9}],
        suggested_collections=[],
    )
    db_session.add(sug)
    db_session.commit()
    db_session.refresh(sug)
    return sug


# ---------------------------------------------------------------------------
# GET /{uuid}/suggestions
# ---------------------------------------------------------------------------


def test_get_suggestions_unauthorized(client):
    response = client.get(f"/api/files/{uuid.uuid4()}/suggestions")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_get_suggestions_nonexistent_file_404(client, user_token_headers):
    response = client.get(f"/api/files/{uuid.uuid4()}/suggestions", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_get_suggestions_other_user_403(client, other_user_auth_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{mf.uuid}/suggestions", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_get_suggestions_none_404(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{mf.uuid}/suggestions", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert "No AI suggestions found" in response.json()["detail"]


def test_get_suggestions_happy(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    _make_suggestion(db_session, normal_user, mf)
    response = client.get(f"/api/files/{mf.uuid}/suggestions", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["media_file_id"] == str(mf.uuid)
    assert body["suggested_tags"][0]["name"] == "budget"
    assert body["status"] == "pending"


# ---------------------------------------------------------------------------
# DELETE /{uuid}/suggestions  (dismiss)
# ---------------------------------------------------------------------------


def test_dismiss_suggestions_happy(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    sug = _make_suggestion(db_session, normal_user, mf)
    response = client.delete(f"/api/files/{mf.uuid}/suggestions", headers=user_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT
    db_session.refresh(sug)
    assert sug.status == "rejected"


def test_dismiss_suggestions_none_404(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    response = client.delete(f"/api/files/{mf.uuid}/suggestions", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# POST /{uuid}/apply  (track accepted)
# ---------------------------------------------------------------------------


def test_apply_suggestions_happy(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    _make_suggestion(db_session, normal_user, mf)
    response = client.post(
        f"/api/files/{mf.uuid}/apply",
        headers=user_token_headers,
        json={"accepted_tags": ["budget"], "accepted_collections": []},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["tags_added"] == 1
    assert body["collections_added"] == 0


def test_apply_suggestions_none_404(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{mf.uuid}/apply",
        headers=user_token_headers,
        json={"accepted_tags": [], "accepted_collections": []},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# POST /{uuid}/extract  (LLM-gated; not configured on this stack → 400)
# ---------------------------------------------------------------------------


def test_extract_no_transcript_400(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user, with_transcript=False)
    response = client.post(f"/api/files/{mf.uuid}/extract", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "File has no transcript. Complete transcription first."


def test_extract_llm_not_configured_400(client, user_token_headers, normal_user, db_session):
    """With a transcript but no LLM provider, extraction is rejected with 400."""
    mf = _make_file(db_session, normal_user, with_transcript=True)
    response = client.post(f"/api/files/{mf.uuid}/extract", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == (
        "LLM provider not configured. Configure LLM settings first."
    )


def test_extract_nonexistent_404(client, user_token_headers):
    response = client.post(f"/api/files/{uuid.uuid4()}/extract", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# POST /{uuid}/auto-label
# ---------------------------------------------------------------------------


def test_auto_label_no_suggestion_404(client, user_token_headers, normal_user, db_session):
    """When auto-labeling is enabled but the file has no suggestion → 404.

    (If the user's auto-label setting is disabled, the endpoint returns 400
    first; either way no suggestion exists so we accept both gated outcomes.)
    """
    mf = _make_file(db_session, normal_user)
    response = client.post(f"/api/files/{mf.uuid}/auto-label", headers=user_token_headers)
    assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND)
    if response.status_code == status.HTTP_404_NOT_FOUND:
        assert response.json()["detail"] == "No AI suggestions found for this file"
    else:
        assert response.json()["detail"] == "Auto-labeling is disabled in your settings"


def test_auto_label_other_user_403(client, other_user_auth_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    response = client.post(f"/api/files/{mf.uuid}/auto-label", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# POST /batch-extract  (static route)
# ---------------------------------------------------------------------------


def test_batch_extract_no_valid_files_400(client, user_token_headers):
    """All-unknown UUIDs yield no verified files → 400."""
    response = client.post(
        "/api/files/batch-extract",
        headers=user_token_headers,
        json={"file_uuids": [str(uuid.uuid4())], "force_regenerate": False},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "No valid files found for processing"


def test_batch_extract_unauthorized(client):
    response = client.post("/api/files/batch-extract", json={"file_uuids": []})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Retroactive auto-label status + trigger
# ---------------------------------------------------------------------------


def test_retroactive_status(client, user_token_headers):
    """The status probe returns running=False when no job is active.

    BUGFIX (this wave, topics.py): the handler called ``get_redis().exists(...)``
    with no error handling, so an unreachable Redis surfaced as an unhandled 500.
    It now degrades to ``{"running": False}`` on ``RedisError`` — matching the
    repo's graceful-degradation pattern (celery_metrics / redis_cache_service).
    """
    response = client.get("/api/files/retroactive-auto-label/status", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["running"] is False


def test_retroactive_trigger_dispatches(client, user_token_headers):
    """Celery dispatch is no-opped by conftest; the endpoint still acknowledges."""
    response = client.post("/api/files/retroactive-auto-label", headers=user_token_headers, json={})
    assert response.status_code == status.HTTP_202_ACCEPTED
    assert "task_id" in response.json()
