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
    monkeypatch.setattr(MinIOService, "delete_object", lambda self, b, k: None)
    # Module-level delete_file is imported lazily inside the cleanup helper.
    import app.services.minio_service as minio_mod

    monkeypatch.setattr(minio_mod, "delete_file", lambda path: None)


class TestDerivedCachePrefix:
    """All derived cache keys must live under the derived/ lifecycle prefix."""

    def _svc(self):
        return VideoProcessingService(MinIOService())

    def test_video_cache_key_is_prefixed(self):
        svc = self._svc()
        key = svc.generate_cache_key(
            1, "meeting.mp4", include_speakers=True, redaction_fingerprint=""
        )
        assert key.startswith("derived/")
        assert key.endswith("_with_speakers.mp4")

    def test_a_masking_policy_gets_its_own_cache_key(self):
        """Burned-in text cannot be masked afterwards, so the key names the policy.

        Sharing one key across policies is what let an already-cached unmasked render
        keep being served after an admin turned ``force_export_redacted`` on (#85).
        """
        svc = self._svc()
        unmasked = svc.generate_cache_key(
            1, "meeting.mp4", include_speakers=True, redaction_fingerprint=""
        )
        masked = svc.generate_cache_key(
            1, "meeting.mp4", include_speakers=True, redaction_fingerprint="abc123def456"
        )
        other_policy = svc.generate_cache_key(
            1, "meeting.mp4", include_speakers=True, redaction_fingerprint="0123456789ab"
        )
        assert unmasked == "derived/meeting_with_speakers.mp4", (
            "a deployment that masks nothing must keep its existing keys valid"
        )
        assert masked == "derived/meeting_with_speakers_rabc123def456.mp4"
        assert len({unmasked, masked, other_policy}) == 3

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

    def test_clear_derived_cache_targets_all_variants(self, db_session, sample_user, monkeypatch):
        file = MediaFile(
            uuid=str(uuid.uuid4()),
            filename="talk.mp4",
            storage_path="media/test/talk.mp4",
            content_type="video/mp4",
            file_size=1024,
            user_id=sample_user.id,
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

        # Policy-specific renders (#85) cannot be named by a pure function -- one file
        # owns one video per redaction policy that ever rendered it -- so the sweep
        # lists them. `talk_2_...` is a DIFFERENT file whose name shares the prefix.
        class _Obj:
            def __init__(self, name):
                self.object_name = name

        monkeypatch.setattr(
            svc.minio_service,
            "list_objects",
            lambda bucket, prefix=None, recursive=False: [
                _Obj("derived/talk_with_speakers_rdeadbeef1234.mp4"),
                _Obj("derived/talk_no_speakers_r0123456789ab.mp4"),
                _Obj("derived/talk_2_with_speakers_rfeedface5678.mp4"),
            ],
        )
        # `clear_cache_for_media_file` took a caller-owned Session and issued its five
        # MinIO deletes through it. It is gone; `clear_derived_cache` takes no session,
        # so the caller resolves the filename in a short read that closes first.
        svc.clear_derived_cache(int(file.id), str(file.filename))

        # Both video variants + all three audio variants, all under derived/.
        assert all(k.startswith("derived/") for k in deleted)
        assert "derived/talk_with_speakers.mp4" in deleted
        assert "derived/talk_no_speakers.mp4" in deleted
        assert "derived/talk_audio_mp3.mp3" in deleted
        # ...plus every masked render, or a deleted file leaves a video of its
        # transcript behind in object storage.
        assert "derived/talk_with_speakers_rdeadbeef1234.mp4" in deleted
        assert "derived/talk_no_speakers_r0123456789ab.mp4" in deleted
        assert "derived/talk_2_with_speakers_rfeedface5678.mp4" not in deleted, (
            "the sweep must not delete a neighbouring file whose name shares the prefix"
        )
        assert "derived/talk_audio_wav.wav" in deleted
        assert "derived/talk_audio_original" in deleted


class TestCanonicalDestroy:
    """All delete paths funnel through one purge_media_file implementation."""

    def _make_completed_file(self, db_session, sample_user, name="purge.mp4"):
        f = MediaFile(
            uuid=str(uuid.uuid4()),
            filename=name,
            storage_path=f"media/test/{name}",
            content_type="video/mp4",
            file_size=1024,
            user_id=sample_user.id,
            status="completed",
            is_public=False,
        )
        db_session.add(f)
        db_session.commit()
        db_session.refresh(f)
        return f

    def test_purge_deletes_db_row(self, db_session, sample_user):
        from app.services import file_cleanup_service

        f = self._make_completed_file(db_session, sample_user)
        fid = int(f.id)
        result = file_cleanup_service.purge_media_file(db_session, f)
        assert result["deleted"] is True
        assert db_session.query(MediaFile).filter(MediaFile.id == fid).first() is None

    def test_auto_delete_alias_destroys_via_canonical_path(self, db_session, sample_user):
        """The retention/orphan alias produces the same destroy as purge_media_file."""
        from app.services import file_cleanup_service

        f = self._make_completed_file(db_session, sample_user, name="retention.mp4")
        fid = int(f.id)
        result = file_cleanup_service.auto_delete_media_file(db_session, f)
        assert result["deleted"] is True
        assert db_session.query(MediaFile).filter(MediaFile.id == fid).first() is None

    def test_interactive_delete_routes_through_purge(self, db_session, sample_user, monkeypatch):
        """crud.delete_media_file (single/bulk/force) destroys via the canonical path."""
        from app.api.endpoints.files import crud
        from app.services import file_cleanup_service

        f = self._make_completed_file(db_session, sample_user, name="interactive.mp4")
        fid = int(f.id)

        called = {}
        real_purge = file_cleanup_service.purge_media_file

        def spy(db, file):
            called["hit"] = True
            return real_purge(db, file)

        monkeypatch.setattr(crud, "purge_media_file", spy, raising=False)
        # crud imports purge_media_file lazily inside the function; patch the source too.
        monkeypatch.setattr(file_cleanup_service, "purge_media_file", spy)

        crud.delete_media_file(db_session, str(f.uuid), sample_user, force=True)
        assert called.get("hit") is True
        assert db_session.query(MediaFile).filter(MediaFile.id == fid).first() is None


class TestLegacyReclaim:
    """Upgrade reclaim removes pre-prefix root-level derived objects only."""

    def test_reclaim_deletes_only_root_level_objects(self, monkeypatch):
        import app.services.minio_service as minio_mod

        class _Obj:
            def __init__(self, name):
                self.object_name = name

        listed = [
            _Obj("video_with_speakers.mp4"),  # legacy root → delete
            _Obj("clip_audio_mp3.mp3"),  # legacy root → delete
            _Obj("derived/new_with_speakers.mp4"),  # managed → keep
            _Obj("bulk/job123.zip"),  # managed → keep
        ]
        removed = []

        def fake_list(bucket, recursive=True):
            return iter(listed)

        def fake_remove(bucket, delete_objects):
            for d in delete_objects:
                removed.append(d.name)
            return iter([])  # no errors

        monkeypatch.setattr(minio_mod.minio_client, "list_objects", fake_list)
        monkeypatch.setattr(minio_mod.minio_client, "remove_objects", fake_remove)

        count = cache_management_service.reclaim_legacy_derived_cache()
        assert count == 2
        assert set(removed) == {"video_with_speakers.mp4", "clip_audio_mp3.mp3"}
