"""Tests for derived-cache management: prefixing, retention resolution, admin API.

Derived assets (subtitle-embedded videos + extracted audio) live under the
processed-videos/derived/ prefix so a single MinIO lifecycle rule can auto-expire
them. Retention is DB-over-env so admin UI changes apply with no redeploy.
"""

import uuid

import pytest
from fastapi import status

from app.core.config import settings
from app.models.media import MediaFile
from app.services import cache_management_service
from app.services import system_settings_service
from app.services.minio_service import MinIOService
from app.services.video_processing_service import VideoProcessingService


@pytest.fixture(autouse=True)
def _stub_object_storage(monkeypatch):
    """Neutralize MinIO I/O (unreachable in the SKIP_S3 test env).

    Leaves the real logic under test — cache-key prefixing, DB-backed retention
    resolution, and endpoint auth/schema — fully exercised.
    """
    monkeypatch.setattr(VideoProcessingService, "_ensure_cache_bucket_exists", lambda self: None)
    monkeypatch.setattr(MinIOService, "prefix_stats", lambda self, b, p: (0, 0))
    monkeypatch.setattr(MinIOService, "delete_prefix", lambda self, b, p: 0)
    monkeypatch.setattr(MinIOService, "ensure_prefix_expiry", lambda self, *a, **k: None)
    monkeypatch.setattr(MinIOService, "remove_lifecycle_rule", lambda self, *a, **k: None)


class TestDerivedCachePrefix:
    """All derived cache keys must live under the derived/ lifecycle prefix."""

    def _svc(self):
        return VideoProcessingService(MinIOService())

    def test_video_cache_key_is_prefixed(self):
        svc = self._svc()
        key = svc.generate_cache_key(1, "meeting.mp4", include_speakers=True)
        assert key.startswith("derived/")
        assert key.endswith("_with_speakers.mp4")

    def test_audio_cache_keys_are_prefixed(self):
        svc = self._svc()
        assert svc.audio_cache_key("meeting.mp4", "mp3") == "derived/meeting_audio_mp3.mp3"
        assert svc.audio_cache_key("meeting.mp4", "wav") == "derived/meeting_audio_wav.wav"
        assert svc.audio_cache_key("meeting.mp4", "original") == "derived/meeting_audio_original"


class TestRetentionResolution:
    """DB setting overrides the env baseline; absence falls back to env."""

    def test_defaults_to_env_when_unset(self, db_session):
        # No DB override -> env default.
        assert (
            cache_management_service.resolve_retention_days(db_session)
            == settings.DERIVED_CACHE_RETENTION_DAYS
        )

    def test_db_override_wins(self, db_session):
        system_settings_service.set_setting(
            db_session, cache_management_service.RETENTION_SETTING_KEY, 21
        )
        assert cache_management_service.resolve_retention_days(db_session) == 21


class TestCacheConfigEndpoints:
    def test_get_requires_admin(self, client, auth_headers):
        resp = client.get("/api/admin/settings/cache-config", headers=auth_headers)
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_get_returns_config(self, client, admin_auth_headers):
        resp = client.get("/api/admin/settings/cache-config", headers=admin_auth_headers)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["prefix"] == "derived/"
        assert "retention_days" in data
        assert "object_count" in data
        assert "total_bytes" in data

    def test_put_updates_retention(self, client, admin_auth_headers, db_session):
        resp = client.put(
            "/api/admin/settings/cache-config",
            headers=admin_auth_headers,
            json={"retention_days": 30},
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["retention_days"] == 30
        # Persisted to DB (survives a fresh resolution).
        assert cache_management_service.resolve_retention_days(db_session) == 30

    def test_put_rejects_out_of_range(self, client, admin_auth_headers):
        resp = client.put(
            "/api/admin/settings/cache-config",
            headers=admin_auth_headers,
            json={"retention_days": 99999},
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_clear_returns_count(self, client, admin_auth_headers):
        resp = client.post("/api/admin/settings/cache-config/clear", headers=admin_auth_headers)
        assert resp.status_code == status.HTTP_200_OK
        assert "deleted" in resp.json()
        assert isinstance(resp.json()["deleted"], int)


class TestClearCacheOnDelete:
    """Deleting a file must clear its derived cache (no orphaned duplicates)."""

    def test_clear_cache_for_media_file_targets_all_variants(
        self, db_session, test_user, monkeypatch
    ):
        file = MediaFile(
            uuid=str(uuid.uuid4()),
            filename="talk.mp4",
            storage_path="media/test/talk.mp4",
            content_type="video/mp4",
            file_size=1024,
            user_id=test_user.id,
            status="completed",
            is_public=False,
        )
        db_session.add(file)
        db_session.commit()
        db_session.refresh(file)

        deleted: list[str] = []
        svc = VideoProcessingService(MinIOService())
        monkeypatch.setattr(
            svc.minio_service,
            "delete_object",
            lambda bucket, key: deleted.append(key),
        )
        svc.clear_cache_for_media_file(db_session, int(file.id))

        # Both video variants + all three audio variants, all under derived/.
        assert all(k.startswith("derived/") for k in deleted)
        assert "derived/talk_with_speakers.mp4" in deleted
        assert "derived/talk_no_speakers.mp4" in deleted
        assert "derived/talk_audio_mp3.mp3" in deleted
        assert "derived/talk_audio_wav.wav" in deleted
        assert "derived/talk_audio_original" in deleted
