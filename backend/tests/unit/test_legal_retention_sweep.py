"""The legal acceptance retention sweep (v451, ``tasks/legal_retention.py``)."""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy import text


def _pseudonymised_row(db, *, due: bool) -> int:
    """A row in the post-deletion state, written with triggers off for this transaction."""
    when = datetime.now(UTC) + (timedelta(days=-1) if due else timedelta(days=30))
    db.execute(text("SET LOCAL session_replication_role = replica"))
    row_id = db.execute(
        text(
            "INSERT INTO legal_acceptance (uuid, user_id, document_key, version, sha256,"
            " subject_user_uuid, subject_erased_at, retain_until)"
            " VALUES (:u, NULL, 'sweep-terms', '1', :sha, :s, :e, :r) RETURNING id"
        ),
        {
            "u": uuid_pkg.uuid4(),
            "sha": "e" * 64,
            "s": uuid_pkg.uuid4(),
            "e": when - timedelta(days=400),
            "r": when,
        },
    ).scalar_one()
    db.execute(text("SET LOCAL session_replication_role = origin"))
    return int(row_id)


def _exists(db, row_id: int) -> bool:
    return (
        db.execute(text("SELECT 1 FROM legal_acceptance WHERE id = :i"), {"i": row_id}).scalar()
        is not None
    )


def test_nothing_due_is_a_no_op(db_session):
    from app.tasks.legal_retention import delete_due_acceptances

    db_session.execute(text("SET LOCAL session_replication_role = replica"))
    db_session.execute(text("DELETE FROM legal_acceptance WHERE retain_until <= now()"))
    db_session.execute(text("SET LOCAL session_replication_role = origin"))
    future = _pseudonymised_row(db_session, due=False)
    assert delete_due_acceptances(db_session) == 0
    assert _exists(db_session, future)


def test_the_batch_is_bounded_and_honours_retain_until(db_session):
    from app.tasks.legal_retention import delete_due_acceptances

    due = [_pseudonymised_row(db_session, due=True) for _ in range(3)]
    future = _pseudonymised_row(db_session, due=False)
    assert delete_due_acceptances(db_session, limit=2) == 2
    assert sum(_exists(db_session, i) for i in due) == 1
    assert delete_due_acceptances(db_session, limit=2) >= 1
    assert not any(_exists(db_session, i) for i in due)
    assert _exists(db_session, future)


def test_the_task_is_routed_and_scheduled():
    from app.core.celery import celery_app
    from app.tasks.legal_retention import TASK_NAME

    assert celery_app.conf.task_routes[TASK_NAME] == {"queue": "utility"}
    beat = {v["task"]: v for v in celery_app.conf.beat_schedule.values()}
    assert TASK_NAME in beat
