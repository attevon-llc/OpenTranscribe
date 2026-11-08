"""The startup thumbnail passes run once, not on every boot (scale plan 5.2, M2 #11).

Every boot used to dispatch the missing-thumbnail backfill (and the WebP migration), which
re-walked every thumbnail-less video and re-queued the files whose generation always fails:
24,920 ``generate_thumbnail`` tasks per restart on the M2 stack. Each pass now sets a
``SystemSettings`` done flag when it reaches the end of the library, whatever the per-file
outcomes, and the startup step dispatches nothing once both flags are set.

The tasks run for real against the test database; only the broker (``apply_async``) and the
object-store seams are replaced. Rendering always fails here, so nothing is written to any
media row (the dev library may hold rows of its own).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import SimpleNamespace

import pytest

from app.core import constants
from app.core.queues import CPUPriority
from app.models.media import MediaFile
from app.models.system_settings import SystemSettings
from app.startup import maintenance
from app.tasks import thumbnail as thumbnail_tasks
from app.tasks import thumbnail_migration as tm

pytestmark = pytest.mark.unit

_FLAGS = (constants.THUMBNAIL_BACKFILL_DONE_KEY, constants.THUMBNAIL_WEBP_MIGRATION_DONE_KEY)


@pytest.fixture
def world(db_session, monkeypatch):
    """Real tasks over ``db_session``; every broker publish is recorded instead of sent."""
    db_session.query(SystemSettings).filter(SystemSettings.key.in_(_FLAGS)).delete()
    db_session.commit()

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(tm, "session_scope", _scope)
    monkeypatch.setattr(tm, "get_file_url", lambda *_a, **_k: "http://minio.invalid/x")
    monkeypatch.setattr(tm, "generate_thumbnail_from_url", lambda _url: None)  # always fails
    monkeypatch.setattr(tm, "upload_file", lambda **_k: pytest.fail("nothing may be uploaded"))
    monkeypatch.setattr(tm, "delete_file", lambda _p: pytest.fail("nothing may be deleted"))
    monkeypatch.setattr("app.utils.boot_once.run_once_per_boot", lambda step, ttl=300: True)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(maintenance.asyncio, "sleep", no_sleep)

    sent: list[tuple[str, dict, int | None]] = []
    for task in (
        tm.migrate_thumbnails_to_webp,
        tm.backfill_missing_thumbnails,
        thumbnail_tasks.generate_thumbnail_task,
    ):

        def apply_async(args=(), kwargs=None, *, priority=None, _name=task.name, **_options):
            sent.append((_name, {"args": tuple(args), **(kwargs or {})}, priority))
            return SimpleNamespace(id=f"{_name}-id")

        monkeypatch.setattr(task, "apply_async", apply_async)
        monkeypatch.setattr(
            task, "delay", lambda *a, _f=apply_async, **k: _f(args=a, kwargs=k, priority=None)
        )
    return sent


def _video(db, user, *, thumbnail_path: str | None) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=user.id,
        filename=f"thumbpass_{file_uuid.hex[:8]}.mp4",
        storage_path=f"media/test/{file_uuid}.mp4",
        thumbnail_path=thumbnail_path,
        content_type="video/mp4",
        file_size=10,
        status="completed",
    )
    db.add(media_file)
    db.commit()
    return media_file


def _boot() -> None:
    asyncio.run(maintenance._run_thumbnail_migration())


def _drain(sent: list[tuple[str, dict, int | None]], start: int, limit: int = 200) -> int:
    """Run every self-chained batch published after ``start`` (the worker's job). Returns runs."""
    chains = {"migrate_thumbnails_to_webp": tm.migrate_thumbnails_to_webp}
    chains["backfill_missing_thumbnails"] = tm.backfill_missing_thumbnails
    runs, cursor = 0, start
    while cursor < len(sent):
        name, kwargs, _ = sent[cursor]
        cursor += 1
        if name in chains:
            runs += 1
            assert runs <= limit, f"{name} kept chaining: {sent[-3:]}"
            chains[name](**{k: v for k, v in kwargs.items() if k != "args"})
    return runs


def _dispatched_at_boot(sent, start: int) -> list[str]:
    return [name for name, _, _ in sent[start:]]


def test_a_second_boot_dispatches_nothing(db_session, normal_user, world):
    sent = world
    _video(db_session, normal_user, thumbnail_path=None)
    _video(db_session, normal_user, thumbnail_path=f"user/{uuid.uuid4().hex}/thumbnail.jpg")

    _boot()
    first_boot = _dispatched_at_boot(sent, 0)
    _drain(sent, 0)
    mark = len(sent)
    _boot()

    assert sorted(first_boot) == ["backfill_missing_thumbnails", "migrate_thumbnails_to_webp"]
    assert _dispatched_at_boot(sent, mark) == []
    assert all(tm.one_time_pass_done(key) for key in _FLAGS)


def test_files_that_always_fail_still_end_the_pass(db_session, normal_user, world):
    """More failing files than one batch: the chain advances and ends, and the flag is set."""
    sent = world
    jpgs = [
        _video(db_session, normal_user, thumbnail_path=f"user/{uuid.uuid4().hex}/thumbnail.jpg")
        for _ in range(3)
    ]

    summary = tm.migrate_thumbnails_to_webp(batch_size=1)
    _drain(sent, 0)

    assert summary["files_failed"] == 1 and summary["has_more"] is True  # control: it failed
    db_session.expire_all()
    still_jpg = db_session.query(MediaFile).filter(MediaFile.id.in_([f.id for f in jpgs])).all()
    assert all(str(f.thumbnail_path).endswith(".jpg") for f in still_jpg)
    assert tm.one_time_pass_done(constants.THUMBNAIL_WEBP_MIGRATION_DONE_KEY)


def test_a_pass_that_errors_is_not_marked_done(db_session, normal_user, world, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(tm, "_load_migration_batch", broken)

    summary = tm.migrate_thumbnails_to_webp(batch_size=1)

    assert summary["error"] == "database unavailable"
    assert not tm.one_time_pass_done(constants.THUMBNAIL_WEBP_MIGRATION_DONE_KEY)


def test_every_thumbnail_dispatch_runs_at_background_priority(db_session, normal_user, world):
    sent = world
    for _ in range(3):
        _video(db_session, normal_user, thumbnail_path=None)

    _boot()
    tm.backfill_missing_thumbnails(batch_size=1)

    children = [s for s in sent if s[0] == "generate_thumbnail"]
    assert children, sent  # control: the backfill really dispatched files
    assert {name for name, _, _ in sent} >= {"generate_thumbnail", "backfill_missing_thumbnails"}
    assert {priority for _, _, priority in sent} == {CPUPriority.MAINTENANCE}, sent
