"""Deployment-locked controls: capability keys enforced on the server (issue #1109).

A capability resolver can lock settings the deployment owns. Each key below is
checked where the value is actually USED, not only where the UI renders it:

- ``url_ingest``: ``POST /files/process-url`` and ``GET /files/youtube/quota`` 404.
- ``transcription.model_choice``: a client ``whisper_model`` on prepare, complete
  and reprocess is ignored, so a lightweight model cannot route a file to CPU.
- ``transcription.diarization_source`` / ``transcription.advanced``: writes are
  ignored, reads report the default, and the transcription task ignores values a
  user stored before the lock.
- ``speaker_attributes.migration``, ``media_sources``, ``audio_extraction``: their
  routes 404; per-user media sources are not used for downloads.
- ``admin.flower``: the nginx ``auth_request`` probe denies (401).

None of these locks has a platform-admin bypass: they describe the deployment,
not a subscription tier, so they apply to every account.

Community defaults are all True; each "on" case below pins that nothing changes
for a self-hosted install.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi import status

from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.models.media import MediaFile


@pytest.fixture
def caps_off():
    """Install a resolver that turns the given capability keys off."""

    def _install(*keys: str) -> None:
        overrides = dict.fromkeys(keys, False)
        set_capability_resolver(lambda _request: {**COMMUNITY_CAPABILITIES, **overrides})

    yield _install
    reset_capability_resolver()


@pytest.fixture
def complete_without_storage(monkeypatch):
    """Let ``/files/complete`` run with no object store: the object "exists", no header reads."""

    def _no_header(*args, **kwargs):
        raise RuntimeError("no s3")

    monkeypatch.setattr("app.services.minio_service.object_exists_and_size", lambda path: 4096)
    monkeypatch.setattr("app.services.minio_service.range_read", _no_header)
    monkeypatch.setattr(
        "app.api.endpoints.files.complete_upload._fingerprint_object", lambda *a, **k: None
    )


def _seed_prepared_file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    values = {
        "uuid": file_uuid,
        "filename": "locked.wav",
        "title": "locked",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "pending",
        "is_public": False,
        "user_id": owner.id,
    }
    values.update(overrides)
    media_file = MediaFile(**values)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


# ---------------------------------------------------------------------------
# url_ingest
# ---------------------------------------------------------------------------


class TestUrlIngest:
    def test_process_url_404_when_off(self, client, user_token_headers, caps_off):
        caps_off("url_ingest")
        response = client.post(
            "/api/files/process-url",
            headers=user_token_headers,
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_youtube_quota_404_when_off(self, client, user_token_headers, caps_off):
        caps_off("url_ingest")
        response = client.get("/api/files/youtube/quota", headers=user_token_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_off_applies_to_platform_admins_too(self, client, super_admin_token_headers, caps_off):
        caps_off("url_ingest")
        response = client.get("/api/files/youtube/quota", headers=super_admin_token_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_youtube_quota_reachable_by_default(self, client, user_token_headers):
        with patch(
            "app.services.youtube_rate_limiter.youtube_rate_limiter.get_remaining_quota",
            return_value={"hourly_remaining": -1},
        ):
            response = client.get("/api/files/youtube/quota", headers=user_token_headers)
        assert response.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# transcription.model_choice
# ---------------------------------------------------------------------------


class TestModelChoice:
    def test_prepare_ignores_requested_model_when_off(
        self, client, user_token_headers, db_session, caps_off
    ):
        caps_off("transcription.model_choice")
        response = client.post(
            "/api/files/prepare",
            headers=user_token_headers,
            json={
                "filename": "m.wav",
                "file_size": 4096,
                "content_type": "audio/wav",
                "whisper_model": "base",
            },
        )
        assert response.status_code == status.HTTP_200_OK
        row = db_session.query(MediaFile).filter(MediaFile.uuid == response.json()["file_id"]).one()
        assert row.requested_whisper_model is None

    def test_prepare_keeps_requested_model_by_default(self, client, user_token_headers, db_session):
        response = client.post(
            "/api/files/prepare",
            headers=user_token_headers,
            json={
                "filename": "m.wav",
                "file_size": 4096,
                "content_type": "audio/wav",
                "whisper_model": "base",
            },
        )
        assert response.status_code == status.HTTP_200_OK
        row = db_session.query(MediaFile).filter(MediaFile.uuid == response.json()["file_id"]).one()
        assert row.requested_whisper_model == "base"

    @pytest.mark.parametrize(
        ("locked", "expected"),
        [(True, None), (False, "base")],
    )
    def test_complete_dispatches_deployment_default_when_off(
        self, client, user_token_headers, normal_user, db_session, caps_off, locked, expected
    ):
        """Both the request field and a model stored at /prepare before the lock are dropped."""
        if locked:
            caps_off("transcription.model_choice")
        media_file = _seed_prepared_file(db_session, normal_user, requested_whisper_model="base")
        dispatch = MagicMock()
        with (
            patch("app.services.minio_service.object_exists_and_size", return_value=4096),
            patch("app.services.minio_service.range_read", side_effect=RuntimeError("no s3")),
            patch("app.api.endpoints.files.complete_upload._fingerprint_object", return_value=None),
            patch(
                "app.api.endpoints.files.upload.dispatch_upload_pipeline_or_mark_error", dispatch
            ),
        ):
            response = client.post(
                "/api/files/complete",
                headers=user_token_headers,
                json={"file_id": str(media_file.uuid), "whisper_model": "base"},
            )
        assert response.status_code == status.HTTP_200_OK, response.text
        dispatch.assert_called_once()
        assert dispatch.call_args.kwargs["whisper_model"] == expected

    @pytest.mark.parametrize(
        ("locked", "expected"),
        [(False, "base"), (True, None)],
        ids=["unlocked", "locked-after-prepare"],
    )
    def test_complete_without_a_model_uses_the_one_recorded_at_prepare(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        caps_off,
        pipeline_stages,
        complete_without_storage,
        locked,
        expected,
    ):
        """The upload wizard sends its model choice on /prepare only (#1121).

        /complete then carries no ``whisper_model``, and the transcription must still run
        with the model the user picked -- read back from the row /prepare wrote, unless the
        deployment has locked model choice since. The read-back lives in
        ``dispatch_transcription_pipeline`` (``reuse_requested_options``), so this drives the
        real dispatch and asserts on what the first pipeline stage is built with.
        """
        if locked:
            caps_off("transcription.model_choice")
        media_file = _seed_prepared_file(db_session, normal_user, requested_whisper_model="base")

        response = client.post(
            "/api/files/complete",
            headers=user_token_headers,
            json={"file_id": str(media_file.uuid)},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert [stage["whisper_model"] for stage in pipeline_stages] == [expected]

    @pytest.mark.parametrize(
        ("locked", "expected"),
        [(True, None), (False, "base")],
    )
    def test_reprocess_ignores_requested_model_when_off(
        self, client, user_token_headers, normal_user, db_session, caps_off, locked, expected
    ):
        if locked:
            caps_off("transcription.model_choice")
        media_file = _seed_prepared_file(db_session, normal_user, status="completed")
        # Stop at the hand-off: only the model the endpoint passes on matters here.
        reprocess = MagicMock(side_effect=HTTPException(status_code=409, detail="stub"))
        with patch("app.api.endpoints.files.process_file_reprocess", reprocess):
            response = client.post(
                f"/api/files/{media_file.uuid}/reprocess",
                headers=user_token_headers,
                json={"whisper_model": "base"},
            )
        assert response.status_code == status.HTTP_409_CONFLICT
        reprocess.assert_called_once()
        assert reprocess.call_args.kwargs["whisper_model"] == expected


# ---------------------------------------------------------------------------
# transcription.diarization_source / transcription.advanced (user settings)
# ---------------------------------------------------------------------------


def _store_user_settings(db_session, user, values: dict[str, str]) -> None:
    from app.models import UserSetting

    for key, value in values.items():
        db_session.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.commit()


_ADVANCED_WRITE = {
    "vad_threshold": 0.9,
    "vad_min_silence_ms": 5000,
    "vad_min_speech_ms": 900,
    "vad_speech_pad_ms": 1000,
    "repetition_penalty": 1.8,
    "hallucination_silence_threshold": 4.0,
}


class TestTranscriptionSettingLocks:
    def test_diarization_source_write_ignored_when_off(
        self, client, user_token_headers, normal_user, db_session, caps_off
    ):
        from app.models import UserSetting

        caps_off("transcription.diarization_source")
        response = client.put(
            "/api/user-settings/transcription",
            headers=user_token_headers,
            json={"diarization_source": "off", "source_language": "de"},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["diarization_source"] == "provider"
        # The unlocked field in the same request is still saved.
        assert body["source_language"] == "de"
        stored = (
            db_session.query(UserSetting)
            .filter(
                UserSetting.user_id == normal_user.id,
                UserSetting.setting_key == "transcription_diarization_source",
            )
            .first()
        )
        assert stored is None

    def test_diarization_source_read_reports_default_when_off(
        self, client, user_token_headers, normal_user, db_session, caps_off
    ):
        _store_user_settings(db_session, normal_user, {"transcription_diarization_source": "off"})
        caps_off("transcription.diarization_source")
        response = client.get("/api/user-settings/transcription", headers=user_token_headers)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["diarization_source"] == "provider"

    def test_diarization_source_write_kept_by_default(self, client, user_token_headers):
        response = client.put(
            "/api/user-settings/transcription",
            headers=user_token_headers,
            json={"diarization_source": "off"},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["diarization_source"] == "off"

    def test_advanced_writes_ignored_when_off(self, client, user_token_headers, caps_off):
        from app.core.constants import DEFAULT_REPETITION_PENALTY
        from app.core.constants import DEFAULT_VAD_THRESHOLD

        caps_off("transcription.advanced")
        response = client.put(
            "/api/user-settings/transcription",
            headers=user_token_headers,
            json=_ADVANCED_WRITE,
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["vad_threshold"] == DEFAULT_VAD_THRESHOLD
        assert body["repetition_penalty"] == DEFAULT_REPETITION_PENALTY
        assert body["hallucination_silence_threshold"] is None

    def test_advanced_writes_kept_by_default(self, client, user_token_headers):
        response = client.put(
            "/api/user-settings/transcription",
            headers=user_token_headers,
            json=_ADVANCED_WRITE,
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["vad_threshold"] == 0.9
        assert response.json()["repetition_penalty"] == 1.8


class TestTaskTimeSettingLocks:
    """The worker reads stored values directly; a value saved before the lock must not apply."""

    _STORED = {
        "transcription_diarization_source": "off",
        "transcription_vad_threshold": "0.9",
        "transcription_repetition_penalty": "1.8",
        "transcription_hallucination_silence_threshold": "4.0",
        "transcription_min_speakers": "2",
    }

    def test_locked_values_fall_back_to_defaults(self, db_session, normal_user, caps_off):
        from app.core.constants import DEFAULT_REPETITION_PENALTY
        from app.core.constants import DEFAULT_VAD_THRESHOLD
        from app.tasks.transcription.user_settings import _get_user_transcription_settings

        _store_user_settings(db_session, normal_user, self._STORED)
        caps_off("transcription.diarization_source", "transcription.advanced")

        resolved = _get_user_transcription_settings(db_session, normal_user.id)

        assert resolved["diarization_source"] == "provider"
        assert resolved["disable_diarization"] is False
        assert resolved["vad_threshold"] == DEFAULT_VAD_THRESHOLD
        assert resolved["repetition_penalty"] == DEFAULT_REPETITION_PENALTY
        # Not a locked field: still the user's own value.
        assert resolved["min_speakers"] == 2

    def test_stored_values_apply_by_default(self, db_session, normal_user):
        from app.tasks.transcription.user_settings import _get_user_transcription_settings

        _store_user_settings(db_session, normal_user, self._STORED)

        resolved = _get_user_transcription_settings(db_session, normal_user.id)

        assert resolved["diarization_source"] == "off"
        assert resolved["disable_diarization"] is True
        assert resolved["vad_threshold"] == 0.9
        assert resolved["repetition_penalty"] == 1.8
        assert resolved["hallucination_silence_threshold"] == 4.0


# ---------------------------------------------------------------------------
# Whole-surface gates: speaker-attribute migration, media sources, audio extraction
# ---------------------------------------------------------------------------


class TestSurfaceGates:
    def test_speaker_attribute_migration_404_when_off(
        self, client, super_admin_token_headers, caps_off
    ):
        caps_off("speaker_attributes.migration")
        response = client.get(
            "/api/speaker-attributes/migration/status", headers=super_admin_token_headers
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_speaker_attribute_migration_reachable_by_default(
        self, client, super_admin_token_headers
    ):
        with patch(
            "app.api.endpoints.speaker_attribute_migration.attribute_migration_progress"
        ) as progress:
            progress.get_status.return_value = {"running": False}
            response = client.get(
                "/api/speaker-attributes/migration/status", headers=super_admin_token_headers
            )
        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.parametrize(
        ("key", "path"),
        [
            ("media_sources", "/api/user-settings/media-sources"),
            ("audio_extraction", "/api/user-settings/audio-extraction"),
        ],
    )
    def test_user_settings_surface_404_when_off(
        self, client, user_token_headers, caps_off, key, path
    ):
        caps_off(key)
        assert client.get(path, headers=user_token_headers).status_code == 404

    @pytest.mark.parametrize(
        "path", ["/api/user-settings/media-sources", "/api/user-settings/audio-extraction"]
    )
    def test_user_settings_surface_reachable_by_default(self, client, user_token_headers, path):
        assert client.get(path, headers=user_token_headers).status_code == 200

    def test_media_source_write_404_when_off(self, client, user_token_headers, caps_off):
        caps_off("media_sources")
        response = client.post(
            "/api/user-settings/media-sources",
            headers=user_token_headers,
            json={"hostname": "media.example.com", "provider_type": "mediacms"},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_per_user_media_sources_unused_for_downloads_when_off(
        self, db_session, normal_user, caps_off
    ):
        from app.models.user_media_source import UserMediaSource
        from app.services.protected_media_plugins.mediacms import MediacmsProvider

        db_session.add(
            UserMediaSource(
                user_id=normal_user.id,
                hostname="media.example.com",
                provider_type="mediacms",
                is_active=True,
            )
        )
        db_session.commit()

        assert len(MediacmsProvider._query_user_media_sources(db_session, normal_user.id)) == 1
        caps_off("media_sources")
        assert MediacmsProvider._query_user_media_sources(db_session, normal_user.id) == []


# ---------------------------------------------------------------------------
# admin.flower
# ---------------------------------------------------------------------------


class TestFlower:
    def test_flower_authz_denied_when_off(self, client, admin_token_headers, caps_off):
        caps_off("admin.flower")
        response = client.get("/api/auth/flower-authz", headers=admin_token_headers)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_flower_authz_allowed_by_default(self, client, admin_token_headers):
        response = client.get("/api/auth/flower-authz", headers=admin_token_headers)
        assert response.status_code == status.HTTP_200_OK
