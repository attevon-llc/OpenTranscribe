"""Per-file voiceprint consent over HTTP and its service rules (issue #1356).

/prepare records the uploader's stated state; /complete may only RESTRICT it; the owner changes
it later with PATCH, and a move to ``declined`` queues the file-scoped opt-out purge.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import status

from app.models.media import MediaFile
from app.models.media import Task
from app.services.privacy import file_consent
from app.services.privacy.policy import clear_write_backstop_cache
from app.tasks.privacy_purge_task import privacy_purge_task
from tests.fixtures.speaker_cleanup_seams import make_file


def _prepare(client, headers, **extra: Any) -> dict:
    payload = {"filename": "c.wav", "file_size": 4096, "content_type": "audio/wav", **extra}
    response = client.post("/api/files/prepare", headers=headers, json=payload)
    assert response.status_code == status.HTTP_200_OK, response.text
    body: dict[str, Any] = response.json()
    return body


def _row(db_session, file_uuid: str) -> MediaFile:
    db_session.expire_all()
    row: MediaFile = (
        db_session.query(MediaFile).filter(MediaFile.uuid == uuid.UUID(file_uuid)).one()
    )
    return row


@pytest.fixture
def sent(monkeypatch) -> list[dict[str, Any]]:
    queued: list[dict[str, Any]] = []

    def record(args=None, kwargs=None, **options):
        queued.append({"kwargs": dict(kwargs or {}), **options})

    monkeypatch.setattr(privacy_purge_task, "apply_async", record)
    clear_write_backstop_cache()
    return queued


# --- /prepare -------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["granted", "declined"])
def test_prepare_stores_the_stated_state_and_who_stated_it(
    client, user_token_headers, db_session, normal_user, state
):
    body = _prepare(client, user_token_headers, voiceprint_consent=state)

    row = _row(db_session, body["file_id"])
    assert row.voiceprint_consent == state
    assert row.voiceprint_consent_by_user_id == normal_user.id
    assert row.voiceprint_consent_at is not None


def test_prepare_without_the_field_leaves_it_unspecified(client, user_token_headers, db_session):
    row = _row(db_session, _prepare(client, user_token_headers)["file_id"])

    assert row.voiceprint_consent == "unspecified"
    assert row.voiceprint_consent_at is None


@pytest.mark.parametrize("bad", ["unspecified", "yes", "", 1])
def test_prepare_rejects_an_invalid_state(client, user_token_headers, bad):
    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json={
            "filename": "c.wav",
            "file_size": 4096,
            "content_type": "audio/wav",
            "voiceprint_consent": bad,
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# --- /complete: restrict-only -----------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "requested", "expected", "changed"),
    [
        ("unspecified", "declined", "declined", True),
        ("granted", "declined", "declined", True),
        ("declined", "declined", "declined", False),
        ("declined", "granted", "declined", False),  # never widens
        ("unspecified", "granted", "unspecified", False),  # never widens
        ("granted", None, "granted", False),
    ],
)
def test_complete_may_only_restrict(db_session, normal_user, stored, requested, expected, changed):
    row = make_file(db_session, normal_user)
    row.voiceprint_consent = stored

    did_change = file_consent.restrict_at_complete(row, requested, normal_user.id)

    assert (row.voiceprint_consent, did_change) == (expected, changed)


def test_the_complete_request_accepts_the_field_and_rejects_garbage():
    from pydantic import ValidationError

    from app.api.endpoints.files.complete_upload import CompleteUploadRequest

    assert CompleteUploadRequest(file_id="x", voiceprint_consent="declined").voiceprint_consent
    with pytest.raises(ValidationError):
        CompleteUploadRequest.model_validate({"file_id": "x", "voiceprint_consent": "maybe"})


# --- PATCH /files/{uuid}/voiceprint-consent --------------------------------------------


def _patch(client, headers, file_uuid, state):
    return client.patch(
        f"/api/files/{file_uuid}/voiceprint-consent", headers=headers, json={"state": state}
    )


def test_owner_opt_out_commits_the_state_then_queues_a_files_scope_opt_out_purge(
    client, user_token_headers, db_session, normal_user, sent
):
    file_uuid = _prepare(client, user_token_headers, voiceprint_consent="granted")["file_id"]
    file_id = _row(db_session, file_uuid).id

    response = _patch(client, user_token_headers, file_uuid, "declined")

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert (body["state"], body["previous_state"], body["changed"]) == (
        "declined",
        "granted",
        True,
    )
    assert _row(db_session, file_uuid).voiceprint_consent == "declined"
    assert len(sent) == 1
    kwargs = sent[0]["kwargs"]
    assert kwargs["scope"]["kind"] == "files"
    assert kwargs["scope"]["file_ids"] == [file_id]
    assert kwargs["targets"] == ["voiceprints"]
    assert kwargs["purge_reason"] == "opt_out"
    assert kwargs["actor_user_id"] == normal_user.id
    assert body["purge_task_id"] == sent[0]["task_id"]
    task = db_session.get(Task, body["purge_task_id"])
    assert task is not None and task.user_id == normal_user.id


def test_repeating_a_decline_requeues_the_purge(client, user_token_headers, sent):
    file_uuid = _prepare(client, user_token_headers)["file_id"]

    first = _patch(client, user_token_headers, file_uuid, "declined").json()
    again = _patch(client, user_token_headers, file_uuid, "declined").json()

    assert (first["changed"], again["changed"]) == (True, False)
    assert len(sent) == 2


def test_granting_after_a_decline_queues_nothing_and_says_to_reprocess(
    client, user_token_headers, db_session, sent
):
    file_uuid = _prepare(client, user_token_headers, voiceprint_consent="declined")["file_id"]

    body = _patch(client, user_token_headers, file_uuid, "granted").json()

    assert body["state"] == "granted"
    assert body["reprocess_to_create_voiceprints"] is True
    assert body["purge_task_id"] is None
    assert sent == []
    assert _row(db_session, file_uuid).voiceprint_consent == "granted"


def test_a_non_owner_gets_404_and_nothing_changes(
    client, user_token_headers, other_user_auth_headers, db_session, sent
):
    file_uuid = _prepare(client, user_token_headers, voiceprint_consent="granted")["file_id"]

    response = _patch(client, other_user_auth_headers, file_uuid, "declined")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert _row(db_session, file_uuid).voiceprint_consent == "granted"
    assert sent == []


def test_an_unknown_file_is_404(client, user_token_headers):
    assert (
        _patch(client, user_token_headers, uuid.uuid4(), "declined").status_code
        == status.HTTP_404_NOT_FOUND
    )


@pytest.mark.parametrize(
    "bad", [{"state": "unspecified"}, {"state": "x"}, {}, {"state": "granted", "x": 1}]
)
def test_invalid_bodies_are_422(client, user_token_headers, bad):
    file_uuid = _prepare(client, user_token_headers)["file_id"]

    response = client.patch(
        f"/api/files/{file_uuid}/voiceprint-consent", headers=user_token_headers, json=bad
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_effective_policy_for_the_file_reports_locked_by_file(
    client, user_token_headers, db_session
):
    file_uuid = _prepare(client, user_token_headers, voiceprint_consent="declined")["file_id"]

    response = client.get(
        f"/api/privacy/effective?file_uuid={file_uuid}", headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["voiceprint_matching"] == {"enabled": False, "locked_by": "file"}


def test_a_failed_queue_still_saved_the_state_and_says_how_to_retry(
    client, user_token_headers, db_session, monkeypatch
):
    def refuse(*a, **kw):
        raise RuntimeError("broker down")

    monkeypatch.setattr(privacy_purge_task, "apply_async", refuse)
    file_uuid = _prepare(client, user_token_headers)["file_id"]

    response = _patch(client, user_token_headers, file_uuid, "declined")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert _row(db_session, file_uuid).voiceprint_consent == "declined"
