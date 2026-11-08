"""The "files awaiting search indexing" queries run without a per-file SubPlan (scale plan 5.3).

``count_files_awaiting_index`` feeds a gauge on every maintenance tick and
``find_files_awaiting_reindex`` drives the sweep. Both read each file's LATEST
``search_indexing`` row through a scalar subquery correlated to ``media_file`` and referenced
twice, which PostgreSQL planned as two SubPlans per file (EXPLAIN on a 30,000-file seed:
loops=20000 and loops=10108). They now join one ``DISTINCT ON (media_file_id)`` pass.

Two checks: the plan holds no SubPlan (with a control showing the old shape does), and the
answers equal the old correlated formulation, kept here as the oracle, on a seeded matrix.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import event
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.services.search import index_retry

pytestmark = pytest.mark.unit


def _file(db, user, *, segments=True, completed_ago_h=1.0, status="completed"):
    row = MediaFile(
        uuid=uuid_pkg.uuid4(),
        filename="awaiting.wav",
        storage_path=f"media/test/{uuid_pkg.uuid4()}.wav",
        content_type="audio/wav",
        file_size=1,
        user_id=user.id,
        status=status,
        completed_at=datetime.now(UTC) - timedelta(hours=completed_ago_h),
    )
    db.add(row)
    db.flush()
    if segments:
        db.add(
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(), media_file_id=row.id, start_time=0, end_time=1, text="x"
            )
        )
    db.flush()
    return row


def _task(db, user, media, status, *, age_min, updated=True, task_type="search_indexing"):
    when = datetime.now(UTC) - timedelta(minutes=age_min)
    db.add(
        Task(
            id=str(uuid_pkg.uuid4()),
            user_id=user.id,
            media_file_id=media.id,
            task_type=task_type,
            status=status,
            created_at=when,
            updated_at=when if updated else None,
        )
    )
    db.flush()


def _matrix(db, user) -> dict[str, MediaFile]:
    f = {name: _file(db, user) for name in ("missing", "failed", "recovered", "relapsed")}
    f |= {name: _file(db, user) for name in ("pending", "in_progress", "ok", "other_type")}
    f["old_missing"] = _file(db, user, completed_ago_h=24 * 365)
    f["no_transcript"] = _file(db, user, segments=False)
    f["not_completed"] = _file(db, user, status="processing")
    f["null_updated"] = _file(db, user)
    _task(db, user, f["failed"], "failed", age_min=5)
    _task(db, user, f["recovered"], "failed", age_min=30)
    _task(db, user, f["recovered"], "completed", age_min=5)
    _task(db, user, f["relapsed"], "completed", age_min=30)
    _task(db, user, f["relapsed"], "failed", age_min=5)
    _task(db, user, f["pending"], "pending", age_min=5)
    _task(db, user, f["in_progress"], "in_progress", age_min=5)
    _task(db, user, f["ok"], "completed", age_min=5)
    _task(db, user, f["other_type"], "failed", age_min=5, task_type="transcription")
    # updated_at NULL: the COALESCE falls back to created_at (newer than the failure).
    _task(db, user, f["null_updated"], "failed", age_min=30)
    _task(db, user, f["null_updated"], "completed", age_min=5, updated=False)
    db.commit()
    return f


def _old_latest():
    """The pre-rewrite formulation, verbatim: a correlated scalar subquery."""
    return (
        select(Task.status)
        .where(
            Task.media_file_id == MediaFile.id,
            Task.task_type == index_retry.SEARCH_INDEXING_TASK_TYPE,
        )
        .order_by(func.coalesce(Task.updated_at, Task.created_at).desc(), Task.created_at.desc())
        .limit(1)
        .correlate(MediaFile)
        .scalar_subquery()
    )


def _old_base(db, *columns):
    has_segments = exists(
        select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == MediaFile.id)
    )
    return db.query(*columns).filter(MediaFile.status == FileStatus.COMPLETED, has_segments)


def _old_count(db):
    latest = _old_latest()
    return (
        _old_base(db, func.count(MediaFile.id))
        .filter(
            or_(
                latest.in_(("failed", "pending", "in_progress")),
                index_retry._never_attempted(latest),
            )
        )
        .scalar()
    )


def _old_find(db, limit):
    latest = _old_latest()
    rows = (
        _old_base(db, MediaFile.id, MediaFile.uuid, MediaFile.user_id)
        .filter(or_(latest == "failed", index_retry._never_attempted(latest)))
        .order_by(MediaFile.id)
        .limit(limit)
        .all()
    )
    return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]


def test_answers_equal_the_correlated_formulation(db_session, normal_user):
    f = _matrix(db_session, normal_user)
    ours = {m.id: name for name, m in f.items()}

    found = index_retry.find_files_awaiting_reindex(db_session, limit=100_000)

    assert found == _old_find(db_session, 100_000)
    assert index_retry.count_files_awaiting_index(db_session) == _old_count(db_session)
    # Control: the matrix really separates the cases, so equality is not two empty answers.
    assert sorted(ours[i] for i, _, _ in found if i in ours) == [
        "failed",
        "missing",
        "other_type",
        "relapsed",
    ]


def _plans(db, call) -> list[str]:
    seen: list[tuple[str, object]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append((statement, parameters))

    conn = db.connection()
    event.listen(conn, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(conn, "before_cursor_execute", record)
    assert seen, "the call ran no SELECT"
    return [
        "\n".join(r[0] for r in conn.exec_driver_sql("EXPLAIN " + sql, params).all())
        for sql, params in seen
    ]


@pytest.mark.parametrize(
    "call",
    [
        lambda db: index_retry.count_files_awaiting_index(db),
        lambda db: index_retry.find_files_awaiting_reindex(db, limit=100),
    ],
    ids=["count", "find"],
)
def test_the_plan_has_no_per_file_subplan(db_session, normal_user, call):
    _matrix(db_session, normal_user)

    plans = _plans(db_session, lambda: call(db_session))
    control = _plans(db_session, lambda: _old_count(db_session))

    assert all("SubPlan" not in plan for plan in plans), "\n\n".join(plans)
    assert any("SubPlan" in plan for plan in control)  # control: the detector can fire
