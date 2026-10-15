"""The storage orphan sweep (#1212), against a real MinIO and a real Postgres.

Every object lives in a uuid-named THROWAWAY bucket that ``settings.MEDIA_BUCKET_NAME`` is
pointed at for the test, so the real media bucket is never listed, read or written. The
"existing file" is a real committed ``media_file`` row (removed in teardown); the orphan ids
are far above any real sequence value and asserted absent. The sweep treats objects younger
than its grace period as untouchable, so the tests that expect deletion move the sweep's
clock forward instead of waiting an hour.
"""

from __future__ import annotations

import io
import os
import uuid as uuid_pkg
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text

from tests.integration.deletion_seed import DELRES_PREFIX
from tests.integration.deletion_seed import new_tag

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SKIP_S3", "True").lower() == "true",
        reason="needs a reachable MinIO (export MINIO_PORT)",
    ),
]

#: Every shape of object a file owns, relative to its ``user_<u>/file_<f>/`` prefix.
_FILE_OBJECTS = (
    "original.wav",
    "thumbnail.webp",
    "original.wav.playback.m4a",
    "derived/masked/redacted.wav",
    "waveform.json",
)
_ORPHAN_BASE = 2_000_000_000


@pytest.fixture
def engine():
    from app.core.config import settings

    eng = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def bucket(monkeypatch) -> Iterator[str]:
    """A throwaway bucket the sweep is pointed at; emptied and removed even on failure."""
    from minio.deleteobjects import DeleteObject

    from app.core.config import settings
    from app.services.minio_service import minio_client

    name = f"e2e-orphan-sweep-{uuid_pkg.uuid4().hex[:12]}"
    monkeypatch.setattr(settings, "MEDIA_BUCKET_NAME", name)
    try:
        minio_client.make_bucket(name)
        yield name
    finally:
        # make_bucket may have failed after the server created it, so the cleanup keys on the
        # bucket existing, not on the fixture having reached the yield.
        if minio_client.bucket_exists(name):
            names = [
                DeleteObject(o.object_name) for o in minio_client.list_objects(name, recursive=True)
            ]
            if names:
                list(minio_client.remove_objects(name, names))
            minio_client.remove_bucket(name)


@pytest.fixture
def existing_file(engine) -> Iterator[int]:
    """A real, committed ``media_file`` row; yields its id."""
    with engine.begin() as conn:
        owner = conn.execute(text('SELECT id FROM "user" ORDER BY id LIMIT 1')).scalar_one()
        file_id = conn.execute(
            text(
                "INSERT INTO media_file (uuid, user_id, filename, storage_path, file_size, "
                "content_type, status) VALUES (gen_random_uuid(), :u, :f, 'none', 1, "
                "'audio/wav', 'completed') RETURNING id"
            ),
            {"u": owner, "f": f"{DELRES_PREFIX}{new_tag()}.wav"},
        ).scalar_one()
    try:
        yield int(file_id)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM media_file WHERE id = :i"), {"i": file_id})


@pytest.fixture
def old_clock(monkeypatch):
    """Make everything in the bucket look older than the grace period."""
    from app.tasks import storage_orphan_sweep_task as sweeper

    real_now = sweeper._now
    monkeypatch.setattr(
        sweeper, "_now", lambda: real_now() + timedelta(seconds=sweeper.GRACE_SECONDS * 2)
    )


@pytest.fixture
def orphan_ids(engine) -> list[int]:
    ids = [_ORPHAN_BASE + uuid_pkg.uuid4().int % 1_000_000 * 10 + i for i in range(3)]
    with engine.connect() as conn:
        present = conn.execute(
            text("SELECT count(*) FROM media_file WHERE id = ANY(:ids)"), {"ids": ids}
        ).scalar_one()
    assert present == 0, "the chosen orphan ids collide with real rows"
    return ids


def _put(bucket: str, key: str, size: int = 10) -> None:
    from app.services.minio_service import minio_client

    minio_client.put_object(bucket, key, io.BytesIO(b"x" * size), size)


def _put_file(bucket: str, file_id: int, user_id: int = 7) -> int:
    """Lay down every object shape for a file; return the total bytes."""
    total = 0
    for rel in _FILE_OBJECTS:
        _put(bucket, f"user_{user_id}/file_{file_id}/{rel}", 10)
        total += 10
    return total


def _keys(bucket: str) -> set[str]:
    from app.services.minio_service import minio_client

    return {o.object_name for o in minio_client.list_objects(bucket, recursive=True)}


def _file_keys(bucket: str, file_id: int) -> set[str]:
    return {k for k in _keys(bucket) if f"/file_{file_id}/" in k}


def _put_out_of_scope(bucket: str) -> set[str]:
    """Keys the sweep must never touch, whatever Postgres says."""
    keys = {
        "media/7/aaaa.wav",
        "user_7/youtube_abc123/thumbnail.jpg",
        "avatars/user_7.png",
        "exports/bulk-1.zip",
        "user_7/notes.txt",
    }
    for key in keys:
        _put(bucket, key)
    return keys


def test_dry_run_lists_orphans_with_counts_and_bytes_and_deletes_nothing(
    bucket, existing_file, orphan_ids, old_clock
):
    from app.tasks import storage_orphan_sweep_task as sweeper

    kept_bytes = _put_file(bucket, existing_file)
    orphan_bytes = {fid: _put_file(bucket, fid) for fid in orphan_ids}
    out_of_scope = _put_out_of_scope(bucket)
    before = _keys(bucket)

    report = sweeper.run_storage_orphan_sweep(dry_run=True)

    assert report["refused"] is None
    assert report["orphan_files"] == 3
    assert report["orphan_objects"] == 3 * len(_FILE_OBJECTS)
    assert report["orphan_bytes"] == sum(orphan_bytes.values())
    assert {o["file_id"] for o in report["orphans"]} == set(orphan_ids)
    assert all(o["objects"] == len(_FILE_OBJECTS) for o in report["orphans"])
    # Out-of-scope keys are not even counted into the file-shaped total.
    assert report["total_objects"] == 4 * len(_FILE_OBJECTS)
    assert report["total_bytes"] == kept_bytes + sum(orphan_bytes.values())
    assert report["deleted_objects"] == 0
    assert _keys(bucket) == before
    assert out_of_scope <= before


def test_confirmed_sweep_removes_orphans_and_never_the_objects_of_an_existing_file(
    bucket, existing_file, orphan_ids, old_clock
):
    from app.tasks import storage_orphan_sweep_task as sweeper

    _put_file(bucket, existing_file)
    for fid in orphan_ids:
        _put_file(bucket, fid)
    out_of_scope = _put_out_of_scope(bucket)
    kept = _file_keys(bucket, existing_file)
    assert len(kept) == len(_FILE_OBJECTS)

    result = sweeper.run_storage_orphan_sweep(dry_run=False)

    assert result["refused"] is None
    assert result["deleted_files"] == 3
    assert result["deleted_objects"] == 3 * len(_FILE_OBJECTS)
    assert result["deleted_bytes"] == 3 * len(_FILE_OBJECTS) * 10
    assert result["failed_objects"] == 0
    for fid in orphan_ids:
        assert _file_keys(bucket, fid) == set()
    assert _file_keys(bucket, existing_file) == kept
    assert out_of_scope <= _keys(bucket)


def test_force_never_overrides_an_empty_postgres_side(bucket, orphan_ids, old_clock, monkeypatch):
    from app.tasks import storage_orphan_sweep_task as sweeper

    for fid in orphan_ids:
        _put_file(bucket, fid)
    before = _keys(bucket)
    monkeypatch.setattr(sweeper, "_get_all_file_ids_from_db", set)

    for force in (False, True):
        result = sweeper.run_storage_orphan_sweep(dry_run=False, force=force)
        assert result["refused"] == "empty_valid_set"
        assert result["deleted_objects"] == 0
    assert _keys(bucket) == before


def test_ratio_guard_refuses_a_large_share_and_force_overrides_it(
    bucket, existing_file, engine, old_clock
):
    from app.tasks import storage_orphan_sweep_task as sweeper

    _put_file(bucket, existing_file)
    # 30 single-object orphans: above the 25-object floor and ~86% of the 35 file-shaped
    # objects, far over the 10% limit.
    big = [_ORPHAN_BASE + 5_000_000 + i for i in range(30)]
    for fid in big:
        _put(bucket, f"user_7/file_{fid}/original.wav")
    kept = _file_keys(bucket, existing_file)

    dry = sweeper.run_storage_orphan_sweep(dry_run=True)
    assert dry["refused"] == "ratio_guard"
    assert dry["orphan_files"] == 30

    unforced = sweeper.run_storage_orphan_sweep(dry_run=False)
    assert unforced["refused"] == "ratio_guard"
    assert unforced["deleted_objects"] == 0
    assert len(_keys(bucket)) == 30 + len(kept)

    forced = sweeper.run_storage_orphan_sweep(dry_run=False, force=True)
    assert forced["refused"] is None
    assert forced["deleted_files"] == 30
    assert _keys(bucket) == kept


def test_recent_objects_are_skipped(bucket, existing_file, orphan_ids):
    """No ``old_clock``: everything just uploaded is inside the grace period."""
    from app.tasks import storage_orphan_sweep_task as sweeper

    _put_file(bucket, existing_file)
    _put_file(bucket, orphan_ids[0])
    before = _keys(bucket)

    result = sweeper.run_storage_orphan_sweep(dry_run=False)

    assert result["skipped_recent_files"] == 1
    assert result["orphan_files"] == 0
    assert result["deleted_objects"] == 0
    assert _keys(bucket) == before


def test_a_file_whose_row_appears_after_the_snapshot_is_kept(
    bucket, engine, old_clock, monkeypatch
):
    """The window the pre-delete re-check closes: row created after the valid-id snapshot."""
    from app.tasks import storage_orphan_sweep_task as sweeper

    with engine.begin() as conn:
        owner = conn.execute(text('SELECT id FROM "user" ORDER BY id LIMIT 1')).scalar_one()
        late_id = conn.execute(
            text(
                "INSERT INTO media_file (uuid, user_id, filename, storage_path, file_size, "
                "content_type, status) VALUES (gen_random_uuid(), :u, :f, 'none', 1, "
                "'audio/wav', 'completed') RETURNING id"
            ),
            {"u": owner, "f": f"{DELRES_PREFIX}{new_tag()}.wav"},
        ).scalar_one()
    try:
        _put_file(bucket, late_id)
        real_snapshot = sweeper._get_all_file_ids_from_db()
        stale = real_snapshot - {late_id}
        assert stale, "the database must hold at least one other file for this test"
        monkeypatch.setattr(sweeper, "_get_all_file_ids_from_db", lambda: stale)
        kept = _file_keys(bucket, late_id)

        result = sweeper.run_storage_orphan_sweep(dry_run=False)

        assert result["rechecked_present"] == 1
        assert result["deleted_objects"] == 0
        assert _file_keys(bucket, late_id) == kept

        # The PER-FILE re-check, immediately before the delete, is a second net: let the
        # batch re-check lie (as if the row appeared a moment later) and it must still hold.
        real_present = sweeper._file_ids_present
        calls: list[int] = []

        def lying_first_call(keys):
            calls.append(1)
            return set() if len(calls) == 1 else real_present(keys)

        monkeypatch.setattr(sweeper, "_file_ids_present", lying_first_call)
        result = sweeper.run_storage_orphan_sweep(dry_run=False)

        assert len(calls) == 2
        assert result["rechecked_present"] == 1
        assert result["deleted_objects"] == 0
        assert _file_keys(bucket, late_id) == kept
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM media_file WHERE id = :i"), {"i": late_id})


@pytest.fixture
def real_redis_restored() -> Iterator[object]:
    """The stack's real Redis; the sweep's last-run key is put back as it was found."""
    from app.core.redis import get_redis
    from app.tasks import storage_orphan_sweep_task as sweeper

    r = get_redis()
    before = r.get(sweeper._REDIS_LAST_RUN_KEY)
    assert not r.exists(sweeper._REDIS_LOCK_KEY), "a real storage sweep is in progress"
    try:
        yield r
    finally:
        r.delete(sweeper._REDIS_LOCK_KEY)
        if before is None:
            r.delete(sweeper._REDIS_LAST_RUN_KEY)
        else:
            r.set(sweeper._REDIS_LAST_RUN_KEY, before, ex=86400 * 7)


def test_the_task_audits_counters_only(
    bucket, existing_file, orphan_ids, old_clock, real_redis_restored, monkeypatch
):
    from app.auth import audit as audit_module
    from app.auth.audit import AuditEventType
    from app.tasks import storage_orphan_sweep_task as sweeper

    for fid in orphan_ids:
        _put_file(bucket, fid)
    events: list[dict] = []
    monkeypatch.setattr(audit_module.audit_logger, "log", lambda **kwargs: events.append(kwargs))

    sweeper.storage_orphan_sweep_task.run(user_id=None, force=False)

    assert not real_redis_restored.exists(sweeper._REDIS_LOCK_KEY), "the lock was not released"

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == AuditEventType.ADMIN_DATA_INTEGRITY_STORAGE_PURGE
    assert event["event_type"].value == "admin.data_integrity.storage_purge"
    details = event["details"]
    assert details["deleted_files"] == 3
    assert details["deleted_objects"] == 3 * len(_FILE_OBJECTS)
    assert details["deleted_bytes"] == 3 * len(_FILE_OBJECTS) * 10
    # Counters (and a fixed refusal code / flag) only: no key, id list or free text.
    assert all(
        v is None or isinstance(v, int | bool) or v == "storage_purge_completed"
        for v in details.values()
    )
    rendered = repr(event)
    assert bucket not in rendered
    assert "user_7" not in rendered
    assert all(str(fid) not in rendered for fid in orphan_ids)
