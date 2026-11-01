"""Cache-bucket copies, ordering between racing actions, and late objects.

* A takedown deletes every cache copy (derived renders, bulk exports) and reports it; a copy
  it cannot list or delete makes the revocation incomplete.
* ``settle_file`` leaves storage matching the LAST committed state when a quarantine and a
  release interleave.
* A playback rendition finishing during a takedown is tagged (and an ERROR names a refusal).
* The org-admin shape carries no infrastructure detail.
* The startup backfill resolves enforcement once and logs one summary.

Storage is ``fixtures.fake_tagging_store`` (real ``S3Error``).
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fixtures.fake_tagging_store import FakeTaggingStore
from fixtures.fake_tagging_store import install

from app.core.constants import STORAGE_QUARANTINE_TAG_KEY
from app.models.media import MediaFile
from app.services import media_revocation
from app.services import media_revocation_cache as cache

PUT_TAGS = "s3:PutObjectTagging"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(media_revocation, "UNTAG_BACKOFF_SECONDS", 0)


def _file(file_id: int = 42) -> MediaFile:
    return MediaFile(
        id=file_id,
        uuid=uuid_pkg.uuid4(),
        storage_path=f"user_1/file_{file_id}/original.mp4",
        thumbnail_path=None,
        playback_path=None,
    )


def _store(monkeypatch, file: MediaFile, *, extra=(), deny=()) -> FakeTaggingStore:
    return install(
        monkeypatch,
        FakeTaggingStore(objects=[file.storage_path, *extra], deny=deny),
        native_s3=False,
    )


class TestCacheCopies:
    def test_derived_renders_and_bulk_exports_are_deleted_and_reported(self, monkeypatch):
        file = _file(42)
        other_files_render = "derived/420_meeting_with_speakers.mp4"
        store = _store(
            monkeypatch,
            file,
            extra=[
                "derived/42_meeting_with_speakers.mp4",
                "derived/42_meeting_audio_mp3.mp3",
                other_files_render,
                "bulk/job1.zip",
                "bulk/by-file/42/job1.zip",
                "bulk/job2.pdf",
            ],
        )

        report = media_revocation.revoke_media(file)

        roles = [(o.role, o.write.outcome) for o in report.objects]
        assert roles == [
            ("original", "applied"),
            ("derived", "removed"),
            ("derived", "removed"),
            ("bulk_export", "removed"),
        ]
        assert report.complete is True
        assert "derived/42_meeting_with_speakers.mp4" not in store.objects
        assert "bulk/job1.zip" not in store.objects
        assert "bulk/by-file/42/job1.zip" not in store.objects
        # Another file's render (id 420) and an export of another file survive.
        assert other_files_render in store.objects
        assert "bulk/job2.pdf" in store.objects

    def test_a_removed_export_also_drops_its_cached_result(self, monkeypatch):
        file = _file(42)
        _store(monkeypatch, file, extra=["bulk/job9.zip", "bulk/by-file/42/job9.zip"])
        dropped: list[str] = []
        monkeypatch.setattr(cache, "drop_bulk_result", dropped.append)

        media_revocation.revoke_media(file)

        assert dropped == ["bulk/job9.zip"]
        assert cache.bulk_job_of("bulk/job9.zip") == "job9"

    def test_a_cache_copy_that_cannot_be_listed_makes_the_revocation_incomplete(self, monkeypatch):
        file = _file()
        _store(monkeypatch, file, deny=["s3:ListBucket"])

        report = media_revocation.revoke_media(file)

        assert report.complete is False
        assert report.reason == media_revocation.REASON_COPY_REMOVAL_FAILED
        assert report.missing_permissions == ["s3:ListBucket"]
        assert set(report.failed_roles) == {"derived", "bulk_export"}

    def test_a_cache_copy_that_cannot_be_deleted_makes_it_incomplete(self, monkeypatch):
        file = _file()
        store = _store(monkeypatch, file, extra=["derived/42_meeting_audio_wav.wav"])
        store.deny_on["derived/42_meeting_audio_wav.wav"] = {"s3:DeleteObject"}

        report = media_revocation.revoke_media(file)

        assert report.complete is False
        assert report.failed_roles == ["derived"]
        assert report.missing_permissions == ["s3:DeleteObject"]

    def test_bulk_markers_are_written_per_file(self, monkeypatch):
        written: list[tuple[str, str]] = []

        class _Storage:
            def upload_bytes(self, bucket, key, data, content_type):
                written.append((bucket, key))

        cache.record_bulk_export(_Storage(), "cache", [3, 1, 3], "bulk/abc.zip")

        assert written == [("cache", "bulk/by-file/1/abc.zip"), ("cache", "bulk/by-file/3/abc.zip")]

    def test_an_export_built_during_a_takedown_is_deleted_after_upload(self, monkeypatch):
        store = install(monkeypatch, FakeTaggingStore(objects=["bulk/late.zip"]), native_s3=False)
        monkeypatch.setattr(cache, "any_quarantined", lambda ids: True)

        assert cache.remove_if_quarantined([1], "bulk/late.zip") is True
        assert "bulk/late.zip" not in store.objects

    def test_an_export_of_unquarantined_files_is_kept(self, monkeypatch):
        store = install(monkeypatch, FakeTaggingStore(objects=["bulk/ok.zip"]), native_s3=False)
        monkeypatch.setattr(cache, "any_quarantined", lambda ids: False)

        assert cache.remove_if_quarantined([1], "bulk/ok.zip") is False
        assert "bulk/ok.zip" in store.objects


class TestOrdering:
    def test_a_release_committing_mid_quarantine_leaves_the_media_untagged(self, monkeypatch):
        """The quarantine's tag write is in flight when a release commits."""
        file = _file()
        store = _store(monkeypatch, file)
        state = {"quarantined": True}
        real_set = store.set_object_tags

        def _release_commits_during_the_write(bucket_name, object_name, tags, version_id=None):
            real_set(bucket_name, object_name, tags)
            state["quarantined"] = False

        monkeypatch.setattr(store, "set_object_tags", _release_commits_during_the_write)

        report = media_revocation.settle_file(file, lambda: state["quarantined"])

        assert store.quarantined(file.storage_path) is False
        assert report.action == "restore"

    def test_a_quarantine_committing_mid_release_leaves_the_media_tagged(self, monkeypatch):
        file = _file()
        store = _store(monkeypatch, file)
        store.tags[file.storage_path] = {STORAGE_QUARANTINE_TAG_KEY: "true"}
        state = {"quarantined": False}
        writes: list[str] = []
        real_set = store.set_object_tags

        def _quarantine_commits_during_the_untag(bucket_name, object_name, tags, version_id=None):
            real_set(bucket_name, object_name, tags)
            writes.append(object_name)
            if len(writes) == 1:
                state["quarantined"] = True

        monkeypatch.setattr(store, "set_object_tags", _quarantine_commits_during_the_untag)

        report = media_revocation.settle_file(file, lambda: state["quarantined"])

        assert store.quarantined(file.storage_path) is True
        assert (report.action, report.complete) == ("revoke", True)

    def test_running_out_of_rounds_never_reports_the_opposite_state_complete(self, monkeypatch):
        file = _file()
        _store(monkeypatch, file)
        reads = iter([True, False, True, False, True])

        report = media_revocation.settle_file(file, lambda: next(reads))

        # Four states applied, the database changed a fifth time: storage may match neither.
        assert report.action == "restore"
        assert (report.complete, report.reason) == (False, media_revocation.REASON_STATE_CHANGED)
        assert report.to_tenant_wire()["reason"] == "restore_incomplete"

    def test_the_deadline_stops_new_rounds_and_reports_incomplete(self, monkeypatch):
        file = _file()
        store = _store(monkeypatch, file)
        monkeypatch.setattr(media_revocation, "TAKEDOWN_SETTLE_DEADLINE_SECONDS", 0)
        reads = iter([True, False])

        report = media_revocation.settle_file(file, lambda: next(reads))

        assert (report.action, report.complete) == ("revoke", False)
        assert report.reason == media_revocation.REASON_STATE_CHANGED
        assert [a for a, _ in store.calls].count("s3:PutObjectTagging") == 1

    def test_storage_calls_use_the_bounded_takedown_client(self, monkeypatch):
        from app.services import minio_service

        file = _file()
        bounded = _store(monkeypatch, file)
        monkeypatch.setattr(minio_service, "minio_client", FakeTaggingStore(deny=[PUT_TAGS]))
        monkeypatch.setattr(minio_service, "get_takedown_client", lambda: bounded)

        report = media_revocation.revoke_media(file)

        assert report.complete is True
        assert bounded.quarantined(file.storage_path) is True

    def test_a_concurrent_writer_that_undoes_the_tag_is_caught_by_the_read_back(self, monkeypatch):
        from app.services.minio_service import write_object_quarantine_tag

        store = install(monkeypatch, FakeTaggingStore(objects=["k"]), native_s3=False)
        real_set = store.set_object_tags

        def _someone_else_wins(bucket_name, object_name, tags, version_id=None):
            real_set(bucket_name, object_name, tags)
            store.tags[object_name] = {}

        monkeypatch.setattr(store, "set_object_tags", _someone_else_wins)

        write = write_object_quarantine_tag("k", True)

        assert (write.outcome, write.error_code) == ("failed", "ConcurrentTagWrite")


class TestLateObject:
    def test_a_rendition_finished_during_a_takedown_is_tagged(self, monkeypatch):
        store = install(monkeypatch, FakeTaggingStore(objects=["r.m4a"]), native_s3=False)

        result = media_revocation.settle_object_tag(
            "playback", "r.m4a", lambda: True, file_label="file 1"
        )

        assert result is not None and result.outcome == "applied"
        assert store.quarantined("r.m4a") is True

    def test_a_refused_tag_is_an_error_naming_the_permission(self, monkeypatch, caplog):
        install(monkeypatch, FakeTaggingStore(objects=["r.m4a"], deny=[PUT_TAGS]), native_s3=False)

        with caplog.at_level("ERROR", logger="app.services.media_revocation"):
            media_revocation.settle_object_tag(
                "playback", "r.m4a", lambda: True, file_label="file 1"
            )

        errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1 and PUT_TAGS in errors[0]

    def test_an_unquarantined_file_writes_nothing(self, monkeypatch):
        store = install(monkeypatch, FakeTaggingStore(objects=["r.m4a"]), native_s3=False)

        assert (
            media_revocation.settle_object_tag(
                "playback", "r.m4a", lambda: False, file_label="file 1"
            )
            is None
        )
        assert store.calls == []


def test_the_tenant_shape_carries_no_infrastructure_detail(monkeypatch):
    file = _file()
    _store(monkeypatch, file, deny=[PUT_TAGS])

    wire = media_revocation.revoke_media(file).to_tenant_wire()

    assert wire == {
        "action": "revoke",
        "complete": False,
        "reason": "revocation_incomplete",
        "exposure_seconds": wire["exposure_seconds"],
    }
    assert isinstance(wire["exposure_seconds"], int)


def test_the_backfill_resolves_enforcement_once_and_logs_one_summary(monkeypatch, caplog):
    from app import main

    calls = {"n": 0}

    def _resolve():
        calls["n"] += 1
        return ("none", False)

    rows = [_file(1), _file(2), _file(3)]
    store = install(
        monkeypatch, FakeTaggingStore(objects=[r.storage_path for r in rows]), native_s3=False
    )
    monkeypatch.setattr(media_revocation, "resolve_enforcement", _resolve)

    class _Query:
        def filter(self, *_a):
            return self

        def all(self):
            return rows

    class _Session:
        def query(self, *_a):
            return _Query()

        def close(self):
            return None

    monkeypatch.setattr("app.db.base.SessionLocal", _Session)

    with caplog.at_level("INFO"):
        main._backfill_quarantine_tags()

    assert calls["n"] == 1
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "3/3" in errors[0] and "enforcement_missing" in errors[0]
    assert all(store.quarantined(r.storage_path) for r in rows)
