"""Every route that starts processing refuses with the legal gate's stable code.

The gate is switched on through its real ``SystemSettings`` row (savepoint-isolated) and the
file's owner has accepted nothing, so each route must answer 403 with
``detail == {"code": "legal_acceptance_required", "documents": [...]}`` BEFORE any side effect.
A control per family proves the same request goes through once the owner accepts, so a route
that refused everything for an unrelated reason cannot pass.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.core.constants import LEGAL_PROCESSING_REQUIRED_KEYS_KEY
from app.legal import registry
from app.legal.acceptance import record_acceptance
from app.models.media import MediaFile
from app.models.system_settings import SystemSettings

pytestmark = pytest.mark.xdist_group("legal_processing_gate_settings")

TERMS = "opentranscribe-terms-of-use"
REFUSED = {"code": "legal_acceptance_required", "documents": [TERMS]}


@pytest.fixture
def gate_on(db_session):
    db_session.add(SystemSettings(key=LEGAL_PROCESSING_REQUIRED_KEYS_KEY, value=TERMS))
    db_session.commit()


def _accept(db_session, user) -> None:
    doc = registry.get_document(TERMS)
    assert doc is not None
    record_acceptance(db_session, user.id, doc, None)


def _file(db_session, owner, *, file_status: str = "completed", **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    values = {
        "uuid": file_uuid,
        "filename": "gate_test.wav",
        "title": "gate_test",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": file_status,
        "is_public": False,
        "retry_count": 0,
        "user_id": owner.id,
    }
    values.update(overrides)
    media_file = MediaFile(**values)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def assert_refused(response) -> None:
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert response.json()["detail"] == REFUSED


# --- Upload --------------------------------------------------------------------------------


def test_prepare_upload_is_refused_before_any_row(
    client, user_token_headers, normal_user, db_session, gate_on
):
    before = db_session.query(MediaFile).filter(MediaFile.user_id == normal_user.id).count()
    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json={"filename": "a.wav", "file_size": 1024, "content_type": "audio/wav"},
    )
    assert_refused(response)
    assert db_session.query(MediaFile).filter(MediaFile.user_id == normal_user.id).count() == before


def test_prepare_upload_passes_once_accepted(
    client, user_token_headers, normal_user, db_session, gate_on
):
    _accept(db_session, normal_user)
    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json={"filename": "a.wav", "file_size": 1024, "content_type": "audio/wav"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text


def test_direct_upload_is_refused(client, user_token_headers, gate_on):
    response = client.post(
        "/api/files",
        headers=user_token_headers,
        files={"file": ("a.wav", b"RIFF0000WAVE", "audio/wav")},
    )
    assert_refused(response)


def test_complete_upload_marks_the_row_refused(
    client, user_token_headers, normal_user, db_session, gate_on
):
    media_file = _file(db_session, normal_user, file_status="pending")
    response = client.post(
        "/api/files/complete", headers=user_token_headers, json={"file_id": str(media_file.uuid)}
    )
    assert_refused(response)
    db_session.refresh(media_file)
    assert media_file.status == "error"
    assert media_file.error_category == "legal_acceptance_required"
    assert media_file.last_error_message == f"legal_acceptance_required: {TERMS}"


def test_url_ingest_is_refused_before_any_download(client, user_token_headers, gate_on):
    response = client.post(
        "/api/files/process-url",
        headers=user_token_headers,
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert_refused(response)


# --- Reprocess, retry, recover -------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [None, {"stages": ["rediarize"]}, {"stages": ["summarization"]}, {"stages": ["speaker_llm"]}],
    ids=["full", "rediarize", "summary", "speaker-llm"],
)
def test_reprocess_is_refused_for_every_stage(
    client, user_token_headers, normal_user, db_session, gate_on, body
):
    media_file = _file(db_session, normal_user)
    kwargs = {"json": body} if body is not None else {}
    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess", headers=user_token_headers, **kwargs
    )
    assert_refused(response)
    db_session.refresh(media_file)
    assert media_file.status == "completed"  # nothing was reset or cleared


def test_reprocess_passes_once_accepted(
    client, user_token_headers, normal_user, db_session, gate_on
):
    _accept(db_session, normal_user)
    media_file = _file(db_session, normal_user)
    response = client.post(f"/api/files/{media_file.uuid}/reprocess", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text


def test_an_admin_reprocessing_a_members_file_is_refused_on_the_owner(
    client, admin_token_headers, admin_user, normal_user, db_session, gate_on
):
    """The admin accepted; the member did not. The member's file is still refused."""
    _accept(db_session, admin_user)
    media_file = _file(db_session, normal_user)
    response = client.post(f"/api/files/{media_file.uuid}/reprocess", headers=admin_token_headers)
    assert_refused(response)


@pytest.mark.parametrize(
    "path",
    ["/api/files/{u}/retry", "/api/my-files/{u}/retry", "/api/tasks/retry/{u}"],
    ids=["files-retry", "my-files-retry", "tasks-retry"],
)
def test_retry_routes_are_refused(
    client, user_token_headers, normal_user, db_session, gate_on, path
):
    media_file = _file(db_session, normal_user, file_status="error")
    response = client.post(path.format(u=media_file.uuid), headers=user_token_headers)
    assert_refused(response)


def test_recover_is_refused(client, user_token_headers, normal_user, db_session, gate_on):
    media_file = _file(db_session, normal_user, file_status="processing")
    response = client.post(f"/api/files/{media_file.uuid}/recover", headers=user_token_headers)
    assert_refused(response)


def test_bulk_reports_the_code_per_file_and_continues(
    client, user_token_headers, normal_user, db_session, gate_on
):
    first = _file(db_session, normal_user, file_status="error")
    second = _file(db_session, normal_user, file_status="error")
    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"file_uuids": [str(first.uuid), str(second.uuid)], "action": "retry"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    results = response.json()
    assert [r["file_uuid"] for r in results] == [str(first.uuid), str(second.uuid)]
    assert all(r["success"] is False for r in results)
    assert {r["error"] for r in results} == {"legal_acceptance_required"}


def test_bulk_tag_actions_are_not_processing(
    client, user_token_headers, normal_user, db_session, gate_on
):
    media_file = _file(db_session, normal_user)
    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"file_uuids": [str(media_file.uuid)], "action": "add_tag", "tag_name": "gate-x"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()[0]["error"] != "legal_acceptance_required"


# --- AI processing of an existing transcript -------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/files/{u}/summarize",
        "/api/files/{u}/identify-speakers",
        "/api/files/{u}/extract",
        "/api/files/{u}/retry-summary",
    ],
    ids=["summarize", "identify-speakers", "topics", "retry-summary"],
)
def test_ai_routes_are_refused(client, user_token_headers, normal_user, db_session, gate_on, path):
    media_file = _file(db_session, normal_user)
    response = client.post(path.format(u=media_file.uuid), headers=user_token_headers)
    assert_refused(response)


# --- The admin settings ------------------------------------------------------------------


def test_settings_round_trip_and_audit(client, super_admin_token_headers, db_session):
    response = client.get("/api/admin/legal-processing-gate", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["enabled"] is False
    assert body["required_document_keys"] == []
    assert body["acceptance_scope"] == "either"
    assert TERMS in {d["key"] for d in body["available_documents"]}

    response = client.put(
        "/api/admin/legal-processing-gate",
        headers=super_admin_token_headers,
        json={"required_document_keys": [TERMS], "acceptance_scope": "user"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["enabled"] is True
    assert response.json()["required_document_keys"] == [TERMS]
    assert response.json()["acceptance_scope"] == "user"
    stored = (
        db_session.query(SystemSettings)
        .filter(SystemSettings.key == LEGAL_PROCESSING_REQUIRED_KEYS_KEY)
        .one()
    )
    assert stored.value == TERMS


def test_settings_refuse_an_unserved_key(client, super_admin_token_headers, db_session):
    response = client.put(
        "/api/admin/legal-processing-gate",
        headers=super_admin_token_headers,
        json={"required_document_keys": ["no-such-doc"]},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["detail"] == {
        "code": "legal_document_unavailable",
        "documents": ["no-such-doc"],
    }
    assert (
        db_session.query(SystemSettings)
        .filter(SystemSettings.key == LEGAL_PROCESSING_REQUIRED_KEYS_KEY)
        .count()
        == 0
    )


def test_settings_are_super_admin_only(client, admin_token_headers):
    response = client.get("/api/admin/legal-processing-gate", headers=admin_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
