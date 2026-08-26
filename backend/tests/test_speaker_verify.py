"""API tests for POST /speakers/{uuid}/verify (savepoint-rolled-back).

Mirrors ``test_speaker_gender_confirm.py``'s pattern: create a speaker directly via
the ORM (no MinIO/media-upload dependency) and exercise the mutating route with the
`db_session` fixture rolling everything back.

Pinned real behavior of ``verify_speaker_identification`` / ``_reject_speaker_suggestion``
(``app/api/endpoints/speakers.py``):
- ``action="reject"`` sets ``speaker.verified = True`` and ``speaker.confidence = None``,
  and clears ``profile_id`` — it does NOT require a ``profile_uuid``/``profile_name``.
- Authorization is by file permission (``PermissionService.get_file_permission``), not by
  ``speaker.user_id`` directly — a non-owner with no share gets 403, not 404.
- Rejecting an already-rejected/verified speaker is idempotent: the same 200 + same fields.
"""

import uuid as uuid_mod

from app.models.media import MediaFile
from app.models.media import Speaker


def _make_speaker(db_session, user) -> Speaker:
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename="verify-test.mp4",
        storage_path="test/verify-test.mp4",
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
    )
    db_session.add(speaker)
    db_session.flush()
    return speaker


class TestVerifySpeaker:
    def test_owner_rejects_own_speaker_marks_verified(
        self, client, db_session, normal_user, user_token_headers
    ):
        speaker = _make_speaker(db_session, normal_user)
        headers = {"Authorization": user_token_headers["Authorization"]}

        resp = client.post(f"/api/speakers/{speaker.uuid}/verify?action=reject", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "rejected"

        db_session.refresh(speaker)
        assert speaker.verified is True
        assert speaker.confidence is None
        assert speaker.profile_id is None

    def test_verify_on_non_owned_speaker_is_forbidden(
        self, client, db_session, normal_user, admin_user, user_token_headers
    ):
        """Authorization is by file permission, not `speaker.user_id`: a user with no
        share on the file gets 403, not 404 — confirmed from
        `verify_speaker_identification`'s `get_file_permission` check."""
        other_speaker = _make_speaker(db_session, admin_user)
        headers = {"Authorization": user_token_headers["Authorization"]}

        resp = client.post(
            f"/api/speakers/{other_speaker.uuid}/verify?action=reject", headers=headers
        )
        assert resp.status_code == 403

        db_session.refresh(other_speaker)
        assert other_speaker.verified is not True

    def test_verifying_an_already_verified_speaker_is_idempotent(
        self, client, db_session, normal_user, user_token_headers
    ):
        speaker = _make_speaker(db_session, normal_user)
        headers = {"Authorization": user_token_headers["Authorization"]}

        first = client.post(f"/api/speakers/{speaker.uuid}/verify?action=reject", headers=headers)
        second = client.post(f"/api/speakers/{speaker.uuid}/verify?action=reject", headers=headers)

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json() == second.json()

        db_session.refresh(speaker)
        assert speaker.verified is True
        assert speaker.confidence is None
