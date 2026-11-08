"""The #1337 synthetic seed: row counts, determinism, and refusal of non-isolated targets."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Table
from sqlalchemy import Text
from sqlalchemy import cast
from sqlalchemy import false
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.engine import Connection

from app.scripts.corpus_injection.env import LiveStackRefusedError
from app.scripts.scale_dataset import __main__ as cli
from app.scripts.scale_dataset import pg_synthetic as pg
from app.scripts.scale_dataset.pg_governance import seed_all

# Both seed suites write fixed natural keys (perf-N@loadtest.invalid, loadtest-tag-N), so two
# of them open at once on the shared database block on each other's uncommitted rows.
pytestmark = pytest.mark.xdist_group("scale_dataset_seed")

TINY = pg.Shape(
    users=5,
    files=20,
    heavy_user_files=8,
    speakers_per_file=10,
    segments_per_file=4,
    tasks_per_file=3,
    profiles=7,
    tags=6,
    tags_per_file=3,
    orgs=2,
    holds=2,
    hold_files=4,
    collections=3,
    collection_members=6,
    collection_shares=2,
    clusters=2,
    cluster_members=6,
    matches=10,
    hidden=4,
    conversations=2,
    messages=4,
    comments=10,
    user_tags=4,
)


# table -> (id range column filter, deterministic columns). server-default timestamps excluded.
_FINGERPRINT_COLUMNS: dict[str, tuple[str, ...]] = {
    "user": ("id", "uuid", "email", "role"),
    "media_file": (
        "id",
        "uuid",
        "user_id",
        "filename",
        "file_size",
        "content_type",
        "duration",
        "upload_time",
        "metadata_important",
        "is_quarantined",
        "legal_hold",
        "deleted_at",
    ),
    "speaker_profile": ("id", "uuid", "user_id", "name"),
    "speaker": (
        "id",
        "uuid",
        "user_id",
        "media_file_id",
        "name",
        "display_name",
        "suggestion_source",
        "identity_source",
        "profile_id",
        "verified",
    ),
    "transcript_segment": ("id", "media_file_id", "speaker_id", "start_time", "text"),
    "task": ("id", "user_id", "media_file_id", "task_type", "status"),
    "tag": ("id", "uuid", "name"),
    "file_tag": ("id", "media_file_id", "tag_id"),
}


def _fingerprint_tables() -> dict[str, Table]:
    from app.db.base import Base

    return {name: Base.metadata.tables[name] for name in _FINGERPRINT_COLUMNS}


# Columns holding another table's id: compared relative to that table's first seeded row too.
_REFERENCES = {
    "user_id": "user",
    "media_file_id": "media_file",
    "speaker_id": "speaker",
    "profile_id": "speaker_profile",
    "tag_id": "tag",
}


def _in_ranges(column, ranges: list[tuple[int, int]]):
    return or_(*(column.between(lo, hi) for lo, hi in ranges)) if ranges else false()


def fingerprint(conn: Connection, ledger: pg.ClaimLedger) -> dict[str, str]:
    """md5 of the generated columns of exactly the rows the seed wrote.

    Rows are selected by the id ranges the seed claimed (``ledger``), never by "id above the
    maximum read before seeding": on the shared database other workers commit rows between that
    read and the claim, and those foreign rows would enter the fingerprint.

    Ids, and columns holding another table's id, are replaced by the row's ordinal among that
    table's seeded rows. The seed claims each id range from the table's sequence, so absolute ids
    depend on what else the shared database has handed out, and one table can hold several
    claimed ranges (core tags and the activity seed's personal tags) with a gap between them that
    a parallel writer decides. The ordinal is the same in every run; the raw offset is not.
    """
    tables = _fingerprint_tables()
    ranks = {
        name: select(
            table.c.id.label("id"), func.row_number().over(order_by=table.c.id).label("rn")
        )
        .where(_in_ranges(table.c.id, ledger.ranges.get(name, [])))
        .subquery()
        for name, table in tables.items()
        if name != "task"
    }
    out: dict[str, str] = {}
    for name, table in tables.items():
        parts = []
        for c in _FINGERPRINT_COLUMNS[name]:
            ref = name if c == "id" else _REFERENCES.get(c)
            col = table.c[c]
            if ref is not None and ref in ranks:
                rank = ranks[ref]
                ordinal = select(rank.c.rn).where(rank.c.id == col).scalar_subquery()
                parts.append(cast(func.coalesce(ordinal, -1), Text))
            else:
                parts.append(cast(col, Text))
        row = func.concat_ws("|", *parts)
        agg = func.string_agg(row, aggregate_order_by(literal(","), table.c.id))
        stmt = select(func.md5(func.coalesce(agg, "")))
        if name == "task":  # varchar ids, so select the seeded rows by their prefix
            stmt = stmt.where(table.c.id.like("loadtest-%"))
        else:
            stmt = stmt.where(_in_ranges(table.c.id, ledger.ranges.get(name, [])))
        out[name] = conn.execute(stmt).scalar_one()
    return out


def _seed(db_session, seed: str = "t1337", before_seed=None):
    conn = db_session.connection()
    if before_seed is not None:
        before_seed()
    ledger = pg.ClaimLedger()
    counts = seed_all(conn, TINY, seed, "x-not-a-real-hash", ledger)
    return counts, fingerprint(conn, ledger)


def test_tiny_seed_writes_the_requested_row_counts(db_session):
    counts, _ = _seed(db_session)

    core = {
        k: counts[k]
        for k in (
            "user",
            "media_file",
            "speaker_profile",
            "speaker",
            "transcript_segment",
            "task",
            "tag",
            "file_tag",
        )
    }
    assert core == {
        "user": 5,
        "media_file": 20,
        "speaker_profile": 7,
        "speaker": 200,
        "transcript_segment": 80,
        "task": 60,
        "tag": 6,
        "file_tag": 60,
    }
    conn = db_session.connection()
    marked = conn.execute(
        text("SELECT count(*) FROM media_file WHERE metadata_important->>'loadtest' = 't1337'")
    ).scalar_one()
    assert marked == 20
    owners = conn.execute(
        text(
            'SELECT count(*) FROM media_file m JOIN "user" u ON u.id = m.user_id '
            "WHERE m.metadata_important->>'loadtest' = 't1337' "
            "AND u.email = 'perf-2@loadtest.invalid'"
        )
    ).scalar_one()
    assert owners == 8
    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM media_file WHERE completed_at IS NOT NULL AND storage_path LIKE 'loadtest/t1337/%'"
            )
        ).scalar_one()
        == 0
    )


def test_two_seeds_generate_identical_rows(db_session):
    first_counts, first = _seed(db_session)
    db_session.rollback()  # discard the first seed; sequences are not rolled back, ids are base-relative
    second_counts, second = _seed(db_session)

    assert first_counts == second_counts
    assert first == second
    assert all(len(v) == 32 for v in first.values())


def test_a_row_committed_by_another_writer_before_the_seed_is_not_fingerprinted(db_session):
    """Foreign rows that land between the caller's last read and the seed's claim stay out.

    This is the full-suite race: another xdist worker commits a user on the shared database
    after the fingerprint's baseline was read. The fingerprint is keyed on the claimed ranges,
    so the foreign row, which sits below them, cannot change it.
    """
    from sqlalchemy.orm import Session

    from app.db.base import engine
    from app.models.user import User

    _, clean = _seed(db_session)
    db_session.rollback()

    foreign_ids: list[int] = []

    def commit_foreign_user():
        with Session(engine) as other:
            user = User(
                email=f"foreign-{uuid.uuid4().hex}@example.com",
                hashed_password="x-not-a-real-hash",
                full_name="Foreign Writer",
                role="user",
            )
            other.add(user)
            other.commit()
            foreign_ids.append(user.id)

    try:
        _, raced = _seed(db_session, before_seed=commit_foreign_user)
    finally:
        with Session(engine) as other:
            other.query(User).filter(User.id.in_(foreign_ids)).delete()
            other.commit()

    assert foreign_ids, "the foreign writer must have committed"
    assert raced == clean


def test_claimed_id_ranges_are_past_the_sequence_before_any_row_is_written(db_session):
    """A claim moves the sequence beyond the range, so another writer's ``nextval`` cannot land in it.

    The seed used to take its base from ``max(id)`` and bump the sequence only at the end, so
    for the whole seed an ordinary insert on the shared database drew ids inside the seeded
    range and the two deadlocked on ``media_file_pkey`` (full-sweep failure of
    ``test_scope_purge_holds``). A private temp table keeps this off the shared sequences.
    """
    from sqlalchemy import Column
    from sqlalchemy import Integer
    from sqlalchemy import MetaData

    conn = db_session.connection()
    conn.execute(text("CREATE TEMP TABLE claim_probe (id serial PRIMARY KEY)"))
    conn.execute(text("INSERT INTO claim_probe DEFAULT VALUES"))
    probe = Table("claim_probe", MetaData(), Column("id", Integer, primary_key=True))

    first = pg._claim_ids(conn, probe, 10)
    second = pg._claim_ids(conn, probe, 10)
    drawn = conn.execute(
        text("SELECT nextval(pg_get_serial_sequence('claim_probe', 'id'))")
    ).scalar_one()

    assert first >= 1, "the claim starts above the existing row"
    assert second >= first + 10, "a second claim does not overlap the first"
    assert drawn > second + 10, "an ordinary insert draws from beyond every claimed range"


def test_a_different_seed_id_changes_the_rows(db_session):
    _, first = _seed(db_session, "a1337")
    db_session.rollback()
    _, second = _seed(db_session, "b1337")

    assert first["media_file"] != second["media_file"]


def test_a_seed_id_with_sql_metacharacters_is_refused(db_session):
    with pytest.raises(ValueError, match="seed id"):
        pg.seed_dataset(db_session.connection(), TINY, "x'; DROP TABLE media_file;--", "h")


def test_the_cli_refuses_the_dev_stack_port(monkeypatch):
    monkeypatch.setenv("POSTGRES_PORT", "5176")

    with pytest.raises(LiveStackRefusedError):
        cli.require_isolated_target(fresh="perf1337", throwaway=False)


def test_the_cli_refuses_an_undeclared_target(monkeypatch):
    monkeypatch.setenv("POSTGRES_PORT", "5276")

    with pytest.raises(cli.NotIsolatedError):
        cli.require_isolated_target(fresh=None, throwaway=False)
    with pytest.raises(cli.NotIsolatedError):
        cli.require_isolated_target(fresh="a", throwaway=True)


def test_the_cli_accepts_a_declared_isolated_target(monkeypatch):
    monkeypatch.setenv("POSTGRES_PORT", "5276")

    assert cli.require_isolated_target(fresh="perf1337", throwaway=False) == "fresh:perf1337"
    assert cli.require_isolated_target(fresh=None, throwaway=True) == "throwaway-db"
