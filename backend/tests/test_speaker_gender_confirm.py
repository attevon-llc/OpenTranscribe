"""API tests for the speaker gender-confirm endpoint (savepoint-rolled-back).

These cover the MUTATING behavior of POST /speakers/{uuid}/confirm-gender.
The e2e suite deliberately does not mutate gender state on live dev data
(it drifted the speakers-page visual baselines) — mutation coverage lives
here, where the savepoint fixture rolls everything back.
"""

import uuid as uuid_mod
from unittest.mock import patch

from app.models.media import MediaFile
from app.models.media import Speaker


def _make_speaker(db_session, user, gender: str | None = None) -> Speaker:
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename="gender-test.mp4",
        storage_path="test/gender-test.mp4",
        content_type="video/mp4",
        file_size=1000,
    )
    db_session.add(media_file)
    db_session.flush()

    speaker = Speaker(
        uuid=str(uuid_mod.uuid4()),
        media_file_id=media_file.id,
        user_id=user.id,
        name="SPEAKER_00",
        predicted_gender=gender,
    )
    db_session.add(speaker)
    db_session.flush()
    return speaker


class TestConfirmSpeakerGender:
    def test_confirm_sets_gender_and_flag(
        self, client, db_session, normal_user, user_token_headers
    ):
        speaker = _make_speaker(db_session, normal_user, gender="female")
        headers = {"Authorization": user_token_headers["Authorization"]}

        resp = client.post(
            f"/api/speakers/{speaker.uuid}/confirm-gender?gender=male", headers=headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["predicted_gender"] == "male"
        assert body["gender_confirmed_by_user"] is True

        db_session.refresh(speaker)
        assert speaker.predicted_gender == "male"
        assert speaker.gender_confirmed_by_user is True

    def test_confirm_rejects_invalid_gender(
        self, client, db_session, normal_user, user_token_headers
    ):
        speaker = _make_speaker(db_session, normal_user)
        headers = {"Authorization": user_token_headers["Authorization"]}

        resp = client.post(
            f"/api/speakers/{speaker.uuid}/confirm-gender?gender=invalid", headers=headers
        )
        assert resp.status_code == 400

        db_session.refresh(speaker)
        assert speaker.gender_confirmed_by_user is not True

    def test_confirm_dispatches_speaker_updated_ws_event(
        self, client, db_session, normal_user, user_token_headers
    ):
        """Issue #603: confirm-gender used to commit and return with no WS notification,
        so other open tabs on the same file only saw the confirmed gender after a manual
        reload. Assert the notification is actually dispatched, with a payload the
        frontend's `handleSpeakerUpdatedEvent` can key off (`media_file_id` + the
        `speaker_updated` type) — not just that the HTTP call succeeded.
        """
        speaker = _make_speaker(db_session, normal_user, gender="female")
        media_file_uuid = str(speaker.media_file.uuid)
        headers = {"Authorization": user_token_headers["Authorization"]}

        with patch("app.utils.websocket_notify.send_ws_event") as mock_send_ws_event:
            resp = client.post(
                f"/api/speakers/{speaker.uuid}/confirm-gender?gender=male", headers=headers
            )
        assert resp.status_code == 200, resp.text

        mock_send_ws_event.assert_called_once()
        call_args = mock_send_ws_event.call_args
        user_id, notification_type, data = call_args.args
        assert user_id == normal_user.id
        assert notification_type == "speaker_updated"
        assert data["media_file_id"] == media_file_uuid
        assert data["speaker_uuid"] == str(speaker.uuid)

    def test_confirm_requires_editor_permission(
        self, client, db_session, normal_user, admin_user, user_token_headers
    ):
        """A user without ownership or editor share cannot confirm gender."""
        other_speaker = _make_speaker(db_session, admin_user, gender="male")
        headers = {"Authorization": user_token_headers["Authorization"]}

        resp = client.post(
            f"/api/speakers/{other_speaker.uuid}/confirm-gender?gender=female", headers=headers
        )
        assert resp.status_code == 403

        db_session.refresh(other_speaker)
        assert other_speaker.predicted_gender == "male"
