"""``NOT IN (subquery)`` sites are ``NOT EXISTS`` so Postgres can plan an anti-join (#1337 G13).

``x NOT IN (SELECT ...)`` is NULL-sensitive, so the planner keeps a non-hashed SubPlan: 82 ms
against 5.8 ms for the ``NOT EXISTS`` form on 2,000 tags and 300,000 ``file_tag`` rows. The tests
assert the plan SHAPE (node types, never costs), which does not depend on table size.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.services.file_availability import file_is_available
from app.services.tag_collisions import list_unused_tag_rows
from tests.query_budget import count_statements


def _explain(db, query) -> str:
    compiled = query.statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return "\n".join(str(r[0]) for r in db.execute(text(f"EXPLAIN {compiled}")))


def test_unused_tags_query_is_not_a_not_in_subplan(db_session, normal_user):
    with count_statements(db_session.connection()) as log:
        list_unused_tag_rows(db_session, user_id=normal_user.id, organization_id=None)

    sql = " ".join(log.matching(r"FROM tag\b"))
    assert "NOT (EXISTS" in sql or "NOT EXISTS" in sql, log.describe()
    assert not log.matching(r"tag\.id NOT IN"), log.describe()


def test_available_file_predicate_plans_an_anti_join_and_keeps_media_file_in_scope(db_session):
    # The speaker listing joins media_file in the OUTER query; the subquery must still read it.
    query = (
        db_session.query(Speaker.id)
        .outerjoin(MediaFile, MediaFile.id == Speaker.media_file_id)
        .filter(Speaker.media_file_id.is_not(None), file_is_available(Speaker.media_file_id))
    )

    plan = _explain(db_session, query)

    assert "Anti Join" in plan, plan
    assert "SubPlan" not in plan, plan
    assert "NOT (SELECT" not in plan


def test_available_file_predicate_drops_trashed_and_quarantined_files_only(db_session, normal_user):
    import uuid
    from datetime import UTC
    from datetime import datetime

    from fixtures.tag_world import make_file

    live, trashed, held = (make_file(db_session, normal_user) for _ in range(3))
    trashed.deleted_at = datetime.now(UTC)
    trashed.purge_after = datetime.now(UTC)
    trashed.trash_origin = "user"
    held.is_quarantined = True
    speakers = []
    for f in (live, trashed, held):
        s = Speaker(uuid=uuid.uuid4(), name="S", user_id=normal_user.id, media_file_id=f.id)
        db_session.add(s)
        speakers.append(s)
    db_session.flush()

    kept = {
        row.id
        for row in db_session.query(Speaker.id).filter(
            Speaker.id.in_([s.id for s in speakers]), file_is_available(Speaker.media_file_id)
        )
    }

    assert kept == {speakers[0].id}  # control: the live file's speaker IS kept
