# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (a fake RequestContext, plain dicts) to
# signatures that declare richer types. Declared once here rather than as a cast at
# every call site — same convention as tests/unit/test_task_session_lifetime.py.
"""The files/storage plane must not hold a DB transaction across storage work.

Sibling of ``test_task_session_lifetime.py``, which covers the Celery half and
documents the measured incident (a CPU worker ``idle in transaction`` for 48 min,
an NLP worker for 1 h 26 m, both found only because DDL migration tests started
failing with ``LockNotAvailable``). This module covers the eleven findings
``scripts/audit-session-lifetime.py`` catalogued in the files/storage plane:

===================================================  ==================================
site                                                 slow work formerly inside the txn
===================================================  ==================================
``imohash_recompute.recompute_all``                  a MinIO ranged read per file, whole batch
``thumbnail_migration.migrate_thumbnails_to_webp``   presign + WebP render + upload, per file
``cache_management.apply_retention``                 bucket construction + lifecycle rule
``cache_management.set_retention_days``              same, on the settings-write path
``file_cleanup.delete_file_storage_artifacts``       up to 7 MinIO deletes
``file_cleanup.purge_media_file``                    those deletes + 6 OpenSearch round trips
``VideoProcessingService.clear_derived_cache``       5 MinIO deletes
``files.clear_video_cache``                          the same 5, on a REQUEST session
``files.complete_upload``                            multipart assembly + header read + imohash
``MediaDownloadService.process_media_url_sync``      a yt-dlp download + a multi-GB upload

Two observables, because the plane has two shapes:

* **Task shape** — the module owns its ``session_scope``, so a depth-tracking
  stand-in reports the open-scope depth at the moment each slow stub runs, and
  every test asserts ``>= 2`` scopes were opened so a task that stopped touching
  the DB entirely cannot pass. This is the harness from
  ``test_task_session_lifetime.py``.
* **Parameter shape** — an endpoint or service is *handed* a session it does not
  own (``Depends(get_db)``, or a caller's ``session_scope``). There is no ``with``
  to measure, and under this suite's savepoint harness ``in_transaction()`` is
  always true, so the observable is instead **how many commits the caller's
  session had taken at the moment the slow call ran**. A commit is what actually
  ends the read transaction; combined with the assertion that the slow phase
  received *plain data only* (no ``Session``, no ORM instance, so it cannot
  reopen one by lazy load), it pins the same property. Reverting the split — the
  slow call before the commit — takes the count to 0 and fails.
"""

import uuid as uuid_mod
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

import app.api.endpoints.files as files_api
from app.api.endpoints.files import complete_upload as cu
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.services import cache_management_service as cms
from app.services import file_cleanup_service as fcs
from app.services import media_download_service as mds
from app.services import video_processing_service as vps
from app.tasks import imohash_recompute as imo
from app.tasks import thumbnail_migration as thumb

_ORM_TYPES = (Session, MediaFile, Speaker)


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def _observed(tracker, label: str, value):
    """Record *label* on the tracker and return *value*.

    Replaces the `(tracker.observe(...), value)[1]` tuple trick, which used a
    None-returning call as an expression — legal, but it reads as if `observe` returned
    something and mypy flags it as such. A named helper says what is happening.
    """
    tracker.observe(label)
    return value


class _ScopeTracker:
    """Stands in for ``session_scope``, recording how many scopes are open."""

    def __init__(self, session):
        self._session = session
        self.depth = 0
        self.max_depth = 0
        self.opened = 0
        self.observations: list[tuple[str, int]] = []

    @contextmanager
    def scope(self):
        self.depth += 1
        self.opened += 1
        self.max_depth = max(self.max_depth, self.depth)
        try:
            yield self._session
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        finally:
            self.depth -= 1

    def observe(self, label: str) -> None:
        self.observations.append((label, self.depth))

    @property
    def seen(self) -> dict[str, int]:
        return dict(self.observations)


class _CommitTracker:
    """Counts commits on a session the code under test does NOT own.

    ``observe`` records the commit count at the moment a slow call runs. A count
    of 0 means the read transaction that located the row was still open.
    """

    def __init__(self, session):
        self.session = session
        self.commits = 0
        self.observations: list[tuple[str, int]] = []

    def install(self, monkeypatch) -> None:
        real_commit = self.session.commit

        def _counting_commit(*args, **kwargs):
            self.commits += 1
            return real_commit(*args, **kwargs)

        monkeypatch.setattr(self.session, "commit", _counting_commit)

    def observe(self, label: str) -> None:
        self.observations.append((label, self.commits))

    def commits_at(self, label: str) -> list[int]:
        return [count for name, count in self.observations if name == label]

    @property
    def seen(self) -> set[str]:
        return {name for name, _ in self.observations}


def _held(tracker, label: str) -> str:
    return (
        f"the caller's read transaction was still open when {label!r} ran "
        f"(commits so far: {tracker.observations})"
    )


def _leak(tracker: _ScopeTracker, label: str) -> str:
    return f"a DB transaction was held across the slow phase ({label}): {tracker.observations}"


def _assert_plain(value, what: str) -> None:
    """Fail if a Session or ORM instance escaped into the slow phase.

    Recurses one level into lists/tuples/dicts: ``plan["speaker_uuids"]`` holding
    live ``Speaker`` rows is a *list*, so a shallow isinstance check would call
    the leak plain.
    """
    assert not isinstance(value, _ORM_TYPES), (
        f"{what} received {type(value).__name__} — an ORM instance in the slow "
        "phase lazy-loads and silently reopens the transaction"
    )
    members = value.values() if isinstance(value, dict) else value
    if isinstance(members, (list, tuple, set)) or isinstance(value, dict):
        for member in members:
            assert not isinstance(member, _ORM_TYPES), (
                f"{what} received a collection containing {type(member).__name__} — "
                "an ORM instance in the slow phase lazy-loads and silently reopens "
                "the transaction"
            )


def _make_media_file(db_session, user, *, filename="meeting.mp4", **overrides) -> MediaFile:
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename=filename,
        storage_path=f"media/{user.id}/{uuid_mod.uuid4()}.mp4",
        thumbnail_path=f"thumbnails/{uuid_mod.uuid4()}.jpg",
        content_type="video/mp4",
        file_size=1024,
        status=FileStatus.COMPLETED,
        **overrides,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


# --------------------------------------------------------------------------- #
# 1. imohash_recompute.recompute_all — a MinIO ranged read per file
# --------------------------------------------------------------------------- #
def test_imohash_recompute_reads_minio_outside_the_session(db_session, normal_user, monkeypatch):
    tracker = _ScopeTracker(db_session)
    monkeypatch.setattr(imo, "session_scope", tracker.scope)
    monkeypatch.setattr(imo.recompute_progress, "start_migration", lambda **kw: None)
    monkeypatch.setattr(imo.recompute_progress, "increment_processed", lambda **kw: None)
    monkeypatch.setattr(imo, "_finalize_recompute", lambda: None)

    files = [_make_media_file(db_session, normal_user) for _ in range(2)]
    db_session.commit()
    # The dev database this suite runs against holds the whole library; the id
    # cursor the task already supports narrows the batch to the rows made here.
    after_id = min(int(f.id) for f in files) - 1

    fingerprinted: list[str] = []

    def _compute(storage_path, size=None):
        tracker.observe("compute_from_minio")
        _assert_plain(storage_path, "compute_from_minio")
        fingerprinted.append(storage_path)
        return f"fp-{len(fingerprinted)}"

    monkeypatch.setattr(imo, "compute_from_minio", _compute)

    summary = imo.recompute_all(batch_size=2, after_id=after_id)

    assert summary["files_found"] == 2, summary
    assert summary["files_recomputed"] == 2, summary

    depths = [d for label, d in tracker.observations if label == "compute_from_minio"]
    assert depths == [0, 0], _leak(tracker, "compute_from_minio")
    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"

    # The read phase really produced the paths the fingerprinter was given, so
    # "no scope open" cannot be satisfied by fingerprinting nothing...
    assert sorted(fingerprinted) == sorted(str(f.storage_path) for f in files)
    # ...and the write phase really landed.
    db_session.expire_all()
    stored = {
        str(row.storage_path): row.imohash
        for row in db_session.query(MediaFile).filter(MediaFile.id.in_([f.id for f in files]))
    }
    assert all(value and value.startswith("fp-") for value in stored.values()), stored


def test_imohash_read_phase_returns_plain_data(db_session, normal_user, monkeypatch):
    tracker = _ScopeTracker(db_session)
    monkeypatch.setattr(imo, "session_scope", tracker.scope)
    media_file = _make_media_file(db_session, normal_user)
    db_session.commit()

    batch, has_more = imo._load_recompute_batch(int(media_file.id) - 1, 1)

    assert tracker.depth == 0
    assert has_more is False
    assert len(batch) == 1
    assert batch[0]["id"] == int(media_file.id)
    assert batch[0]["storage_path"] == str(media_file.storage_path)
    for value in batch[0].values():
        _assert_plain(value, "_load_recompute_batch")


# --------------------------------------------------------------------------- #
# 2. thumbnail_migration — presign + WebP render + upload, per file
# --------------------------------------------------------------------------- #
@pytest.fixture
def thumbnail_env(db_session, normal_user, monkeypatch):
    """Two JPEG-thumbnail rows plus observable presign/render/upload/delete seams."""
    tracker = _ScopeTracker(db_session)
    monkeypatch.setattr(thumb, "session_scope", tracker.scope)

    files = [_make_media_file(db_session, normal_user, filename=f"talk{i}.mp4") for i in range(2)]
    db_session.commit()
    ours = {int(f.id) for f in files}

    # The real loader still runs, inside the real read scope; only its RESULT is
    # narrowed to the rows this test created (the dev library holds others).
    real_loader = thumb._load_migration_batch
    monkeypatch.setattr(
        thumb,
        "_load_migration_batch",
        lambda batch_size: ([r for r in real_loader(batch_size)[0] if r["id"] in ours], False),
    )

    monkeypatch.setattr(
        thumb,
        "get_file_url",
        lambda path, expires=300: _observed(tracker, "presign", f"https://minio.invalid/{path}"),
    )
    monkeypatch.setattr(
        thumb,
        "generate_thumbnail_from_url",
        lambda url: _observed(tracker, "render", b"webp-bytes"),
    )
    monkeypatch.setattr(thumb, "upload_file", lambda **kwargs: tracker.observe("upload_thumbnail"))
    monkeypatch.setattr(thumb, "delete_file", lambda path: tracker.observe("delete_old"))

    tracker.ours = ours  # type: ignore[attr-defined]
    return tracker


def test_thumbnail_migration_renders_outside_the_session(db_session, thumbnail_env):
    tracker = thumbnail_env
    ours = tracker.ours

    summary = thumb.migrate_thumbnails_to_webp(batch_size=20)

    assert summary["files_found"] == 2, summary
    assert summary["files_migrated"] == 2, summary

    for label in ("presign", "render", "upload_thumbnail", "delete_old"):
        depths = [d for name, d in tracker.observations if name == label]
        assert depths == [0, 0], _leak(tracker, label)
    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"

    # The write phase really repointed the rows at the WebP objects.
    db_session.expire_all()
    paths = [
        str(row.thumbnail_path)
        for row in db_session.query(MediaFile).filter(MediaFile.id.in_(ours))
    ]
    assert paths and all(p.endswith(".webp") for p in paths), paths


# --------------------------------------------------------------------------- #
# 3/4. cache_management — the lifecycle rule is object storage, not Postgres
# --------------------------------------------------------------------------- #
def test_apply_retention_pushes_the_rule_after_ending_the_read(db_session, monkeypatch):
    tracker = _CommitTracker(db_session)
    tracker.install(monkeypatch)
    pushed: list[int] = []

    def _push(days):
        tracker.observe("lifecycle_rule")
        pushed.append(days)

    monkeypatch.setattr(cms, "_push_lifecycle_rule", _push)
    monkeypatch.setattr(cms, "resolve_retention_days", lambda db: 17)

    days = cms.apply_retention(db_session)

    assert days == 17
    assert pushed == [17], "the resolved value never reached object storage"
    assert tracker.commits_at("lifecycle_rule") == [1], _held(tracker, "lifecycle_rule")


def test_set_retention_days_pushes_the_rule_after_ending_the_write(db_session, monkeypatch):
    tracker = _CommitTracker(db_session)
    tracker.install(monkeypatch)
    pushed: list[int] = []

    def _push(days):
        tracker.observe("lifecycle_rule")
        pushed.append(days)

    monkeypatch.setattr(cms, "_push_lifecycle_rule", _push)

    days = cms.set_retention_days(db_session, 9)

    assert days == 9
    assert pushed == [9]
    # ``set_setting`` commits and then refreshes (reopening a transaction), so the
    # count here must be at least two: its own, plus the one that ends the refresh.
    counts = tracker.commits_at("lifecycle_rule")
    assert counts and counts[0] >= 2, _held(tracker, "lifecycle_rule")
    assert cms.resolve_retention_days(db_session) == 9


# --------------------------------------------------------------------------- #
# 5. purge_media_file — the canonical destroy
# --------------------------------------------------------------------------- #
@pytest.fixture
def purge_env(db_session, monkeypatch):
    """Observe the two external-destroy seams without touching MinIO/OpenSearch."""
    tracker = _CommitTracker(db_session)
    tracker.install(monkeypatch)
    captured: dict = {}

    def _storage(file_id, artifacts):
        tracker.observe("storage_delete")
        captured["storage"] = (file_id, artifacts)
        return True

    def _opensearch(target, file_uuid):
        tracker.observe("opensearch_delete")
        captured["opensearch"] = (target, file_uuid)
        return []

    monkeypatch.setattr(fcs, "delete_file_storage_artifacts", _storage)
    monkeypatch.setattr(fcs, "_cleanup_opensearch_for_file", _opensearch)
    monkeypatch.setattr(fcs, "_cleanup_empty_clusters", lambda db, owner_id: None)
    tracker.captured = captured  # type: ignore[attr-defined]
    return tracker


def test_purge_media_file_destroys_external_copies_outside_the_transaction(
    db_session, normal_user, purge_env
):
    tracker = purge_env
    media_file = _make_media_file(db_session, normal_user)
    speaker = Speaker(
        uuid=str(uuid_mod.uuid4()),
        media_file_id=media_file.id,
        user_id=normal_user.id,
        name="SPEAKER_00",
    )
    db_session.add(speaker)
    db_session.flush()
    file_id, file_uuid = int(media_file.id), str(media_file.uuid)
    storage_path, speaker_uuid = str(media_file.storage_path), str(speaker.uuid)

    result = fcs.purge_media_file(db_session, media_file)

    assert result["deleted"] is True, result
    assert result["residual_errors"] == []

    for label in ("storage_delete", "opensearch_delete"):
        counts = tracker.commits_at(label)
        assert counts and counts[0] >= 1, _held(tracker, label)

    # Plain data only: an ORM instance reaching either phase would lazy-load
    # ``file.speakers`` mid-destroy and reopen the transaction by the back door.
    got_file_id, artifacts = tracker.captured["storage"]
    assert got_file_id == file_id
    assert artifacts["storage_path"] == storage_path
    assert artifacts["filename"] == "meeting.mp4"
    for value in artifacts.values():
        _assert_plain(value, "delete_file_storage_artifacts")

    target, got_uuid = tracker.captured["opensearch"]
    assert got_uuid == file_uuid
    _assert_plain(target, "_cleanup_opensearch_for_file")
    assert target["file_id"] == file_id
    assert target["speaker_uuids"] == [speaker_uuid], (
        "the speaker UUIDs must be enumerated in the read phase — reading "
        "file.speakers from the OpenSearch phase is the lazy-load leak"
    )

    # ...and the row really went.
    db_session.expire_all()
    assert db_session.query(MediaFile).filter(MediaFile.id == file_id).first() is None


def test_purge_plan_returns_plain_data(db_session, normal_user):
    media_file = _make_media_file(db_session, normal_user)
    speaker = Speaker(
        uuid=str(uuid_mod.uuid4()),
        media_file_id=media_file.id,
        user_id=normal_user.id,
        name="SPEAKER_00",
    )
    db_session.add(speaker)
    db_session.flush()

    plan = fcs._load_purge_plan(db_session, media_file)

    for value in plan.values():
        _assert_plain(value, "_load_purge_plan")
    assert plan["file_id"] == int(media_file.id)
    assert plan["owner_id"] == int(normal_user.id)
    assert plan["speaker_uuids"] == [str(speaker.uuid)]
    assert plan["speaker_read_error"] is None
    for value in plan.values():
        _assert_plain(value, "_load_purge_plan")


def test_purge_reports_an_unreadable_speaker_list_as_a_residual(monkeypatch):
    """A failed enumeration is still a miss: the embeddings are unaccounted for."""
    monkeypatch.setattr(fcs, "_erase_speaker_docs", lambda uuids, fail: None)
    monkeypatch.setattr(fcs, "_erase_transcript_doc", lambda uuid, fail: None)
    monkeypatch.setattr(fcs, "_erase_transcript_chunks", lambda uuid, fail: None)
    monkeypatch.setattr(fcs, "_erase_summary_docs", lambda file_id, fail: None)

    residual = fcs._cleanup_opensearch_for_file(
        {"file_id": 1, "speaker_uuids": [], "speaker_read_error": "DetachedInstanceError"},
        "some-uuid",
    )
    assert any(entry["stage"] == "speakers" for entry in residual), residual


# --------------------------------------------------------------------------- #
# 6/7. the storage helpers themselves need no session at all
# --------------------------------------------------------------------------- #
def test_delete_file_storage_artifacts_takes_no_session(monkeypatch):
    deleted: list[str] = []
    cleared: list[tuple] = []
    monkeypatch.setattr("app.services.minio_service.delete_file", deleted.append)
    monkeypatch.setattr(
        vps.VideoProcessingService, "_ensure_cache_bucket_exists", lambda self: None
    )
    monkeypatch.setattr(
        vps.VideoProcessingService,
        "clear_derived_cache",
        lambda self, file_id, filename: cleared.append((file_id, filename)),
    )

    ok = fcs.delete_file_storage_artifacts(
        7,
        {
            "filename": "talk.mp4",
            "storage_path": "media/1/talk.mp4",
            "thumbnail_path": "thumbnails/talk.webp",
        },
    )

    assert ok is True
    assert deleted == ["media/1/talk.mp4", "thumbnails/talk.webp"]
    assert cleared == [(7, "talk.mp4")]


def test_clear_derived_cache_opens_no_session_and_targets_all_variants(
    db_session, normal_user, monkeypatch
):
    # A REAL row, so that reintroducing a filename lookup inside this method
    # succeeds and is caught by the scope assertion rather than by a stray
    # "media file not found".
    media_file = _make_media_file(db_session, normal_user, filename="talk.mp4")
    db_session.commit()

    tracker = _ScopeTracker(db_session)
    monkeypatch.setattr(vps, "session_scope", tracker.scope)
    monkeypatch.setattr(
        vps.VideoProcessingService, "_ensure_cache_bucket_exists", lambda self: None
    )

    deleted: list[str] = []
    service = vps.VideoProcessingService(
        SimpleNamespace(delete_object=lambda b, k: deleted.append(k))
    )
    service.clear_derived_cache(int(media_file.id), "talk.mp4")

    assert tracker.opened == 0, (
        "clear_derived_cache opened a session — the filename must come from the "
        "caller's read phase, not a lookup wedged between the deletes"
    )
    assert all(key.startswith("derived/") for key in deleted), deleted
    assert "derived/talk_with_speakers.mp4" in deleted
    assert "derived/talk_no_speakers.mp4" in deleted
    assert "derived/talk_audio_mp3.mp3" in deleted
    assert "derived/talk_audio_wav.wav" in deleted
    assert "derived/talk_audio_original" in deleted


# --------------------------------------------------------------------------- #
# 8. DELETE /files/{uuid}/cache — a REQUEST session across five MinIO deletes
# --------------------------------------------------------------------------- #
def test_clear_video_cache_endpoint_purges_after_ending_the_read(
    db_session, normal_user, monkeypatch
):
    media_file = _make_media_file(db_session, normal_user, filename="talk.mp4")
    db_session.commit()

    # Installed AFTER the fixture's own commit, or that commit would be counted
    # and every assertion below would pass for the wrong reason.
    tracker = _CommitTracker(db_session)
    tracker.install(monkeypatch)
    cleared: list[tuple] = []

    def _clear(file_id, filename):
        tracker.observe("cache_clear")
        cleared.append((file_id, filename))

    monkeypatch.setattr(files_api, "_clear_derived_cache", _clear)

    result = files_api.clear_video_cache(
        str(media_file.uuid),
        db_session,
        normal_user,
        SimpleNamespace(org_id=None),
    )

    assert result is None  # 204, unchanged response contract
    assert cleared == [(int(media_file.id), "talk.mp4")]
    counts = tracker.commits_at("cache_clear")
    assert counts and counts[0] >= 1, _held(tracker, "cache_clear")


# --------------------------------------------------------------------------- #
# 9. POST /files/complete — multipart assembly, header read and imohash
# --------------------------------------------------------------------------- #
@pytest.fixture
def complete_upload_env(monkeypatch):
    """Neutralize the parts of /files/complete that are not under test.

    Size gating, the client-hash update, the pipeline dispatch and magic-byte
    validation each have their own suites; patched once here so the test body
    holds only the three storage seams whose transaction state is the subject.
    """
    monkeypatch.setattr(
        "app.api.endpoints.files.upload.validate_file_size_for_tenant",
        lambda size, org_id: None,
    )
    monkeypatch.setattr("app.api.endpoints.files.upload._update_file_hash", lambda f, h, name: None)
    monkeypatch.setattr(
        "app.api.endpoints.files.upload.dispatch_upload_pipeline", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "app.utils.file_validation.validate_uploaded_file", lambda b, ct, name: (True, "")
    )


def test_complete_upload_touches_storage_after_ending_the_read(
    db_session, normal_user, monkeypatch, complete_upload_env
):
    media_file = _make_media_file(db_session, normal_user, filename="talk.mp4")
    media_file.status = FileStatus.PENDING
    db_session.commit()
    storage_path = str(media_file.storage_path)

    # Installed AFTER the fixture's own commit — see the note in the sibling test.
    tracker = _CommitTracker(db_session)
    tracker.install(monkeypatch)

    def _exists(path):
        tracker.observe("object_exists")
        assert path == storage_path
        return 2048

    def _range_read(path, start, length):
        tracker.observe("header_read")
        return b"\x00\x00\x00\x18ftypmp42"

    def _fingerprint(task_id, path, size):
        tracker.observe("imohash")
        _assert_plain(path, "_fingerprint_object")
        return "fp-complete"

    monkeypatch.setattr("app.services.minio_service.object_exists_and_size", _exists)
    monkeypatch.setattr("app.services.minio_service.range_read", _range_read)
    monkeypatch.setattr(cu, "_fingerprint_object", _fingerprint)

    response = cu.complete_upload(
        cu.CompleteUploadRequest(file_id=str(media_file.uuid), task_id=None),
        db_session,
        normal_user,
    )

    assert response["file_size"] == 2048
    assert response["imohash"] == "fp-complete"

    for label in ("object_exists", "header_read", "imohash"):
        counts = tracker.commits_at(label)
        assert counts and counts[0] >= 1, _held(tracker, label)

    db_session.expire_all()
    refreshed = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).first()
    assert refreshed.imohash == "fp-complete"
    assert refreshed.file_size == 2048


# --------------------------------------------------------------------------- #
# 10. MediaDownloadService.process_media_url_sync — yt-dlp + a multi-GB upload
# --------------------------------------------------------------------------- #
def test_process_media_url_sync_downloads_after_ending_the_read(
    db_session, normal_user, monkeypatch
):
    media_file = _make_media_file(db_session, normal_user, filename="pending.mp4")
    db_session.commit()

    # Installed AFTER the fixture's own commit — see the note in the sibling test.
    tracker = _CommitTracker(db_session)
    tracker.install(monkeypatch)

    staged = {
        "media_info": {"id": "abc", "title": "A talk"},
        "media_metadata": {"source": "youtube"},
        "technical_metadata": {"content_type": "video/mp4"},
        "storage_path": f"media/{normal_user.id}/downloaded.mp4",
        "file_size": 4096,
        "thumbnail_path": "thumbnails/downloaded.webp",
        "original_filename": "A talk.mp4",
        "imohash_value": "fp-url",
    }
    received: dict = {}

    def _stage(self, **kwargs):
        tracker.observe("download_and_upload")
        received.update(kwargs)
        for name, value in kwargs.items():
            if name == "progress_callback":
                continue
            _assert_plain(value, f"_download_and_stage_media({name}=)")
        return staged

    monkeypatch.setattr(mds.MediaDownloadService, "_download_and_stage_media", _stage)
    monkeypatch.setattr(mds.MediaDownloadService, "is_valid_media_url", lambda self, url: True)

    applied: dict = {}
    monkeypatch.setattr(
        mds,
        "_update_media_file_with_download_data",
        lambda **kwargs: applied.update(kwargs),
    )

    service = mds.MediaDownloadService()
    returned = service.process_media_url_sync(
        url="https://www.example.com/watch?v=abc",
        db=db_session,
        user=normal_user,
        media_file=media_file,
    )

    assert returned is media_file
    counts = tracker.commits_at("download_and_upload")
    assert counts and counts[0] >= 1, _held(tracker, "download_and_upload")

    # The read phase supplied the two identifiers, so "no transaction held"
    # cannot be satisfied by downloading for nobody.
    assert received["user_id"] == int(normal_user.id)
    assert received["file_id"] == int(media_file.id)
    # ...and the write phase applied what the download produced.
    assert applied["storage_path"] == staged["storage_path"]
    assert applied["imohash_value"] == "fp-url"
    assert applied["source_url"] == "https://www.example.com/watch?v=abc"
