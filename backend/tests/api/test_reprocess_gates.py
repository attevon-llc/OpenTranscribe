"""The refusals ``POST /api/files/{uuid}/reprocess`` owes BEFORE it deletes anything.

``process_file_reprocess`` clears a file's transcript data and then dispatches, so every
refusal below is only worth anything if the data is still there afterwards. Each test pairs
the refusal with a control that the same call succeeds without the refusing condition.
The retry ceiling, the hold refusal's ordering against cancellation, and the reset-failure
500 had no test of their own on this route; the media, GPU-access and permission gates are
pinned in ``test_reprocess_media_guard_872`` / ``test_files_reprocess`` /
``test_file_mutation_permissions``.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from fastapi import status

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.services import system_settings_service


def _file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    row = MediaFile(
        uuid=file_uuid,
        filename="gate.wav",
        title="gate",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status=overrides.pop("status", FileStatus.COMPLETED),
        is_public=False,
        retry_count=overrides.pop("retry_count", 0),
        user_id=owner.id,
        **overrides,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _with_speaker(db_session, owner, media: MediaFile) -> int:
    speaker = Speaker(
        uuid=str(uuid.uuid4()), user_id=owner.id, media_file_id=media.id, name="SPEAKER_00"
    )
    db_session.add(speaker)
    db_session.commit()
    return int(media.id)


def _speakers(db_session, media_id: int) -> int:
    db_session.expire_all()
    return int(db_session.query(Speaker).filter(Speaker.media_file_id == media_id).count())


def _rediarize(client, headers, media):
    return client.post(
        f"/api/files/{media.uuid}/reprocess", headers=headers, json={"stages": ["rediarize"]}
    )


def test_non_admin_at_the_retry_ceiling_is_refused_with_the_speakers_intact(
    client, user_token_headers, normal_user, db_session
):
    system_settings_service.update_retry_config(db_session, max_retries=1)
    media = _file(db_session, normal_user, retry_count=2)
    media_id = _with_speaker(db_session, normal_user, media)

    response = _rediarize(client, user_token_headers, media)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "maximum retry attempts (1)" in response.json()["detail"]
    assert _speakers(db_session, media_id) == 1


def test_below_the_retry_ceiling_the_same_call_clears_the_speakers(
    client, user_token_headers, normal_user, db_session
):
    system_settings_service.update_retry_config(db_session, max_retries=5)
    media = _file(db_session, normal_user, retry_count=2)
    media_id = _with_speaker(db_session, normal_user, media)

    response = _rediarize(client, user_token_headers, media)

    assert response.status_code == status.HTTP_200_OK
    assert _speakers(db_session, media_id) == 0


def test_an_admin_is_not_bound_by_the_retry_ceiling(
    client, admin_token_headers, normal_user, db_session
):
    system_settings_service.update_retry_config(db_session, max_retries=1)
    media = _file(db_session, normal_user, retry_count=2)

    response = _rediarize(client, admin_token_headers, media)

    assert response.status_code == status.HTTP_200_OK


def test_a_held_file_is_refused_before_its_task_is_cancelled_or_its_data_cleared(
    client, user_token_headers, normal_user, db_session
):
    media = _file(
        db_session,
        normal_user,
        status=FileStatus.PROCESSING,
        active_task_id=str(uuid.uuid4()),
        legal_hold=True,
    )
    media_id = _with_speaker(db_session, normal_user, media)

    with patch("app.utils.task_cancel.cancel_active_task") as cancel:
        response = _rediarize(client, user_token_headers, media)

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"]["error"] == "FILE_UNDER_LEGAL_HOLD"
    cancel.assert_not_called()
    assert _speakers(db_session, media_id) == 1


def test_control_the_same_processing_file_unheld_is_cancelled_and_reprocessed(
    client, user_token_headers, normal_user, db_session
):
    media = _file(
        db_session,
        normal_user,
        status=FileStatus.PROCESSING,
        active_task_id=str(uuid.uuid4()),
    )
    media_id = _with_speaker(db_session, normal_user, media)

    with patch("app.utils.task_cancel.cancel_active_task") as cancel:
        response = _rediarize(client, user_token_headers, media)

    assert response.status_code == status.HTTP_200_OK
    cancel.assert_called_once()
    assert _speakers(db_session, media_id) == 0


def test_a_failed_reset_is_a_500_and_clears_nothing(
    client, user_token_headers, normal_user, db_session
):
    media = _file(db_session, normal_user)
    media_id = _with_speaker(db_session, normal_user, media)

    with (
        patch("app.utils.file_retry.reset_file_for_retry", return_value=False),
        patch("app.api.endpoints.files.reprocess.clear_selective_data") as clear,
    ):
        selective = client.post(
            f"/api/files/{media.uuid}/reprocess",
            headers=user_token_headers,
            json={"stages": ["transcription"]},
        )
        full = client.post(f"/api/files/{media.uuid}/reprocess", headers=user_token_headers)

    for response in (selective, full):
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert response.json()["detail"] == "Failed to reset file for reprocessing"
    clear.assert_not_called()
    assert _speakers(db_session, media_id) == 1


def test_transcription_stage_resets_the_file_before_it_clears_data(
    client, user_token_headers, normal_user, db_session
):
    media = _file(db_session, normal_user)
    order: list[str] = []

    def _reset(*_a, **_k):
        order.append("reset")
        return True

    with (
        patch("app.utils.file_retry.reset_file_for_retry", side_effect=_reset),
        patch(
            "app.api.endpoints.files.reprocess.clear_selective_data",
            side_effect=lambda *a, **k: order.append("clear"),
        ),
    ):
        response = client.post(
            f"/api/files/{media.uuid}/reprocess",
            headers=user_token_headers,
            json={"stages": ["transcription"]},
        )

    assert response.status_code == status.HTTP_200_OK
    assert order == ["reset", "clear"]
