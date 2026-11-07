"""Self-test for ``tests/query_budget.count_statements`` (issue #1337).

Must-fire: a lazy-loading loop is an N+1, so its count grows with the rows.
Must-stay-clean: the same read through ``selectinload`` costs the same for 1 and 5 rows.
Also pins that a statement on the app engine (a path that opens its own ``SessionLocal``) is
counted, and that harness SAVEPOINT noise is not.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import selectinload

from app.models.media import MediaFile
from app.models.speaker import Speaker
from tests.query_budget import count_statements


def _files_with_speakers(db_session, user, n: int) -> list[int]:
    ids = []
    for i in range(n):
        mf = MediaFile(
            user_id=user.id,
            filename=f"qb-{uuid.uuid4().hex[:6]}-{i}.mp3",
            storage_path=f"qb/{uuid.uuid4().hex}",
            file_size=1,
            content_type="audio/mpeg",
        )
        db_session.add(mf)
        db_session.flush()
        db_session.add(Speaker(user_id=user.id, media_file_id=mf.id, name="SPEAKER_00"))
        ids.append(mf.id)
    db_session.flush()
    db_session.expire_all()
    return ids


def _lazy_loop(db_session, ids: list[int]) -> int:
    with count_statements(db_session.connection()) as log:
        for mf in db_session.scalars(select(MediaFile).where(MediaFile.id.in_(ids))):
            assert len(mf.speakers) == 1
    return log.count


def _eager(db_session, ids: list[int]) -> int:
    with count_statements(db_session.connection()) as log:
        rows = db_session.scalars(
            select(MediaFile).where(MediaFile.id.in_(ids)).options(selectinload(MediaFile.speakers))
        ).all()
        assert all(len(mf.speakers) == 1 for mf in rows)
    return log.count


def test_a_lazy_loop_is_counted_as_growing_with_rows(db_session, normal_user):
    one = _files_with_speakers(db_session, normal_user, 1)
    lazy_one = _lazy_loop(db_session, one)
    five = _files_with_speakers(db_session, normal_user, 5)
    db_session.expire_all()
    lazy_five = _lazy_loop(db_session, five)

    assert lazy_five - lazy_one == 4, (lazy_one, lazy_five)


def test_an_eager_load_costs_the_same_for_one_row_and_five(db_session, normal_user):
    one = _files_with_speakers(db_session, normal_user, 1)
    eager_one = _eager(db_session, one)
    five = _files_with_speakers(db_session, normal_user, 5)
    db_session.expire_all()
    eager_five = _eager(db_session, five)

    assert eager_one == eager_five and eager_one >= 2


def test_savepoints_are_not_counted_and_matching_filters(db_session):
    with count_statements(db_session.connection()) as log:
        db_session.connection().execute(text("SAVEPOINT qb_sp"))
        db_session.connection().execute(text("RELEASE SAVEPOINT qb_sp"))
        db_session.connection().execute(text("SELECT 1 AS qb_marker"))

    assert log.count == 1
    assert log.matching(r"qb_marker") == log.statements


def test_a_statement_on_the_app_engine_is_counted():
    from app.db.base import SessionLocal

    with count_statements() as log:
        with SessionLocal() as own:
            own.execute(text("SELECT 1 AS qb_own_session"))

    assert log.matching("qb_own_session"), log.describe()


def test_an_engine_and_its_connection_do_not_double_count(db_session):
    conn = db_session.connection()
    with count_statements(conn, conn.engine) as log:
        conn.execute(text("SELECT 1 AS qb_once"))

    assert len(log.matching("qb_once")) == 1
