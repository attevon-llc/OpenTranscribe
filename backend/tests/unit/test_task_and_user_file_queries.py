"""Task-API and own-file dashboard queries, each against a real database."""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.core.enums import FileStatus
from app.core.tenancy import UNSCOPED
from app.models.media import Task
from app.services import task_endpoint_queries as tq
from app.services import user_file_queries as uq
from app.services.platform_bypass import PlatformBypass
from tests.fixtures.tag_world import make_file
from tests.fixtures.tag_world import make_user

NONE = PlatformBypass.none()


def _task(db, user, media, status="pending", task_type="transcription", created_at=None):
    row = Task(
        id=uuid.uuid4().hex,
        user_id=user.id,
        media_file_id=media.id if media else None,
        task_type=task_type,
        status=status,
        created_at=created_at or datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    return row


def test_visible_file_uuids_and_media_file_lookup_exclude_other_owners(db_session):
    me, other = make_user(db_session, "me"), make_user(db_session, "other")
    mine, theirs = make_file(db_session, me), make_file(db_session, other)

    seen = tq.visible_file_uuids(db_session, [str(mine.uuid), str(theirs.uuid)], me, UNSCOPED, NONE)
    assert seen == {str(mine.uuid)}
    assert tq.find_visible_media_file(db_session, mine.id, me, UNSCOPED, NONE) is not None
    assert tq.find_visible_media_file(db_session, theirs.id, me, UNSCOPED, NONE) is None


def test_quarantined_file_reads_as_absent(db_session):
    me = make_user(db_session, "me")
    media = make_file(db_session, me)
    media.is_quarantined = True
    db_session.commit()

    assert tq.find_visible_media_file(db_session, media.id, me, UNSCOPED, NONE) is None
    assert tq.visible_file_uuids(db_session, [str(media.uuid)], me, UNSCOPED, NONE) == {
        str(media.uuid)
    }


def test_task_rows_carry_active_task_id_and_respect_file_filter(db_session):
    me = make_user(db_session, "me")
    one, two = make_file(db_session, me), make_file(db_session, me)
    first = _task(db_session, me, one)
    one.active_task_id = first.id
    _task(db_session, me, two)
    db_session.commit()

    rows = tq.task_rows_with_active_id(db_session, me, UNSCOPED, NONE)
    assert {r.media_file_id for r in rows} == {one.id, two.id}
    only_one = tq.task_rows_with_active_id(db_session, me, UNSCOPED, NONE, file_id=one.id)
    assert [r.id for r in only_one] == [first.id]
    assert only_one[0].active_task_id == first.id


def test_find_task_row_is_owner_scoped_unless_bypass_sees_all(db_session):
    me, other = make_user(db_session, "me"), make_user(db_session, "other")
    media = make_file(db_session, other)
    theirs = _task(db_session, other, media)

    assert tq.find_task_row(db_session, theirs.id, me, NONE) is None
    assert tq.find_task_row(db_session, theirs.id, other, NONE) is not None
    assert tq.get_task(db_session, theirs.id) is not None
    assert tq.get_task(db_session, "no-such-task") is None


def test_health_counts_and_open_tasks(db_session):
    me = make_user(db_session, "me")
    media = make_file(db_session, me)
    before_tasks = tq.task_status_counts(db_session)
    before_files = tq.file_status_counts(db_session)
    pending = _task(db_session, me, media, "pending")
    running = _task(db_session, me, media, "in_progress", task_type="summarization")
    _task(db_session, me, media, "completed", task_type="topic_extraction")

    after_tasks = tq.task_status_counts(db_session)
    assert after_tasks.total == before_tasks.total + 3
    assert after_tasks.pending == before_tasks.pending + 1
    assert after_tasks.in_progress == before_tasks.in_progress + 1
    assert after_tasks.completed == before_tasks.completed + 1
    assert tq.file_status_counts(db_session).total == before_files.total
    assert {t.id for t in tq.open_tasks_of_file(db_session, media.id)} == {pending.id, running.id}


def test_status_counts_are_scoped_to_the_caller_and_exclude_quarantine(db_session):
    me, other = make_user(db_session, "me"), make_user(db_session, "other")
    make_file(db_session, me)
    gone = make_file(db_session, me)
    gone.is_quarantined = True
    make_file(db_session, other)
    db_session.commit()

    def total(include_quarantined):
        rows = uq.status_count_rows(
            db_session,
            user_id=me.id,
            organization_id=UNSCOPED,
            include_quarantined=include_quarantined,
        )
        return sum(count for _status, count in rows)

    assert total(False) == 1
    assert total(True) == 2


def test_problem_files_are_stuck_or_errored_and_recent_files_are_a_day_deep(db_session):
    now = datetime.now(UTC)
    me = make_user(db_session, "me")
    stuck = make_file(db_session, me)
    stuck.status = FileStatus.PROCESSING
    stuck.upload_time = now - timedelta(hours=3)
    fresh = make_file(db_session, me)
    fresh.status = FileStatus.PROCESSING
    fresh.upload_time = now - timedelta(minutes=5)
    errored = make_file(db_session, me)
    errored.status = FileStatus.ERROR
    errored.upload_time = now - timedelta(days=3)
    db_session.commit()

    def listed(fn):
        return [
            f.id
            for f in fn(
                db_session,
                user_id=me.id,
                organization_id=UNSCOPED,
                include_quarantined=False,
                now=now,
            )
        ]

    assert listed(uq.problem_files) == [stuck.id, errored.id]
    assert listed(uq.recent_files) == [fresh.id, stuck.id]


def test_retry_task_lookups(db_session):
    me = make_user(db_session, "me")
    media = make_file(db_session, me)
    now = datetime.now(UTC)
    old = _task(db_session, me, media, "completed", created_at=now - timedelta(minutes=30))
    newest = _task(db_session, me, media, "in_progress", created_at=now - timedelta(minutes=1))

    assert {t.id for t in uq.tasks_of_file(db_session, media.id)} == {old.id, newest.id}
    recent = uq.latest_task_created_since(db_session, media.id, now - timedelta(minutes=5))
    assert recent is not None
    assert recent.id == newest.id
    assert uq.latest_task_created_since(db_session, media.id, now) is None
    assert [t.id for t in uq.tasks_in_status(db_session, media.id, ["in_progress"])] == [newest.id]
