"""The scale seed leaves sequences usable and supports adding activity to a restored snapshot.

M2 tool bugs T2, T3, T9: a snapshot whose sequences sat behind ``max(id)`` made every create
500, a restored snapshot could not take activity rows, and seeded chat citations lacked the
``id`` the chat API requires.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import text

from app.schemas.chat import Citation
from app.scripts.scale_dataset import pg_synthetic as pg
from app.scripts.scale_dataset.pg_governance import seed_activity_only
from app.scripts.scale_dataset.pg_governance import seed_all
from app.scripts.scale_dataset.pg_synthetic import SEEDED_TABLES
from app.scripts.scale_dataset.pg_synthetic import realign_sequences

# Seeds write fixed natural keys (perf-N@loadtest.invalid) into uncommitted rows, so two of
# these open on different workers block on each other's unique index while each still holds the
# shared DDL lock, which stalls every ddl_exclusive test queued behind them. One group, one worker.
pytestmark = pytest.mark.xdist_group("scale_dataset_seed")

SHAPE = pg.Shape(
    users=24,
    files=600,
    heavy_user_files=120,
    speakers_per_file=3,
    segments_per_file=4,
    tasks_per_file=1,
    profiles=60,
    tags=10,
    tags_per_file=2,
    orgs=2,
    holds=4,
    hold_files=10,
    collections=60,
    collection_members=200,
    collection_shares=4,
    clusters=4,
    cluster_members=8,
    matches=10,
    hidden=6,
    conversations=60,
    messages=240,
    comments=600,
    user_tags=100,
    waveform_pm=1000,
)
NO_ACTIVITY = pg.Shape(**{**SHAPE.__dict__, "comments": 0, "user_tags": 0, "waveform_pm": 0})


def _behind(conn, tables) -> list[str]:
    behind = []
    for t in tables:
        top = conn.execute(text(f'SELECT coalesce(max(id), 0) FROM "{t}"')).scalar_one()
        last = conn.execute(
            text(
                "SELECT coalesce(pg_sequence_last_value(pg_get_serial_sequence(:t, 'id')::regclass), 0)"
            ),
            {"t": f'"{t}"'},
        ).scalar_one()
        if last < top:
            behind.append(f"{t}: last_value {last} < max(id) {top}")
    return behind


def test_every_seeded_table_sequence_is_at_or_past_max_id(db_session):
    conn = db_session.connection()
    seed_all(conn, SHAPE, "seq1337", "x-not-a-real-hash")

    assert _behind(conn, SEEDED_TABLES) == []


def test_realign_repairs_a_sequence_left_behind_by_an_old_snapshot(db_session):
    conn = db_session.connection()
    seed_all(conn, SHAPE, "seq1337", "x-not-a-real-hash")
    conn.execute(text("SELECT setval(pg_get_serial_sequence('chat_conversation', 'id'), 1)"))
    assert _behind(conn, ("chat_conversation",)) != []

    realign_sequences(conn, ("chat_conversation",))

    assert _behind(conn, ("chat_conversation",)) == []


def test_seeded_chat_citations_carry_an_id_the_chat_api_accepts(db_session):
    conn = db_session.connection()
    seed_all(conn, SHAPE, "seq1337", "x-not-a-real-hash")

    rows = (
        conn.execute(text("SELECT citations FROM chat_message WHERE citations IS NOT NULL"))
        .scalars()
        .all()
    )

    assert rows
    for citations in rows:
        for citation in citations:
            assert "id" in citation
            Citation.model_validate(citation)


def test_activity_only_adds_rows_to_a_seeded_database_without_touching_users(db_session):
    conn = db_session.connection()
    seed_all(conn, NO_ACTIVITY, "seq1337", "x-not-a-real-hash")
    users = conn.execute(text('SELECT count(*), max(id) FROM "user"')).one()
    assert conn.execute(text("SELECT count(*) FROM comment")).scalar_one() == 0

    counts = seed_activity_only(conn, SHAPE, "seq1337")

    assert counts["comment"] == SHAPE.comments
    assert conn.execute(text("SELECT count(*) FROM comment")).scalar_one() == SHAPE.comments
    assert conn.execute(text('SELECT count(*), max(id) FROM "user"')).one() == users
    assert _behind(conn, ("comment", "tag", "file_tag")) == []


def test_activity_only_twice_is_a_no_op(db_session):
    conn = db_session.connection()
    seed_all(conn, NO_ACTIVITY, "seq1337", "x-not-a-real-hash")
    seed_activity_only(conn, SHAPE, "seq1337")

    assert seed_activity_only(conn, SHAPE, "seq1337") == {}


def test_the_seeded_admin_is_a_super_admin_so_the_audit_log_route_answers(db_session):
    # T5: /api/admin/audit-logs is super-admin only; perf-1 was a plain admin and got 403
    conn = db_session.connection()
    seed_all(conn, NO_ACTIVITY, "seq1337", "x-not-a-real-hash")

    row = conn.execute(
        text("SELECT role, is_superuser FROM \"user\" WHERE email = 'perf-1@loadtest.invalid'")
    ).one()

    assert tuple(row) == ("super_admin", True)


def test_activity_only_repairs_the_sequences_an_older_snapshot_left_behind(db_session):
    conn = db_session.connection()
    seed_all(conn, NO_ACTIVITY, "seq1337", "x-not-a-real-hash")
    conn.execute(text("SELECT setval(pg_get_serial_sequence('chat_conversation', 'id'), 1)"))
    conn.execute(text("SELECT setval(pg_get_serial_sequence('collection', 'id'), 1)"))

    seed_activity_only(conn, SHAPE, "seq1337")

    assert _behind(conn, SEEDED_TABLES) == []


def test_claiming_zero_ids_from_a_never_drawn_sequence_leaves_it_alone(db_session):
    """``setval(seq, 0)`` is out of range, so a zero-row claim must not issue one."""
    conn = db_session.connection()
    conn.execute(text("CREATE TEMP TABLE claim_zero_probe (id serial PRIMARY KEY)"))
    table = Table("claim_zero_probe", MetaData(), Column("id", Integer, primary_key=True))

    base = pg._claim_ids(conn, table, 0)

    assert base == 0
    last = conn.execute(
        text(
            "SELECT pg_sequence_last_value(pg_get_serial_sequence('claim_zero_probe', 'id')::regclass)"
        )
    ).scalar_one()
    assert last is None, "the sequence must still be undrawn"
