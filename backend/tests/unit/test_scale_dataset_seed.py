"""The #1337 synthetic seed: row counts, determinism, and refusal of non-isolated targets."""

from __future__ import annotations

import pytest
from sqlalchemy import Table
from sqlalchemy import Text
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import literal
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


def fingerprint(conn: Connection, base_ids: dict[str, int] | None = None) -> dict[str, str]:
    """md5 of the generated columns per table (rows with id > ``base_ids[table]``).

    Ids are taken relative to the table's first seeded row: the seed claims its range from the
    table's sequence, so the absolute ids depend on what else the shared database has handed
    out, while the rows' shape does not.
    """
    tables = _fingerprint_tables()
    first: dict[str, int] = {}
    for name, table in tables.items():
        if name != "task":
            lowest = conn.execute(
                select(func.min(table.c.id)).where(table.c.id > (base_ids or {}).get(name, 0))
            ).scalar_one()
            first[name] = int(lowest) - 1 if lowest is not None else 0
    out: dict[str, str] = {}
    for name, table in tables.items():
        parts = []
        for c in _FINGERPRINT_COLUMNS[name]:
            ref = name if c == "id" else _REFERENCES.get(c)
            col = table.c[c]
            parts.append(
                cast(col - first[ref], Text)
                if ref is not None and ref in first
                else cast(col, Text)
            )
        row = func.concat_ws("|", *parts)
        agg = func.string_agg(row, aggregate_order_by(literal(","), table.c.id))
        stmt = select(func.md5(func.coalesce(agg, "")))
        if name == "task":  # varchar ids, so select the seeded rows by their prefix
            stmt = stmt.where(table.c.id.like("loadtest-%"))
        else:
            stmt = stmt.where(table.c.id > (base_ids or {}).get(name, 0))
        out[name] = conn.execute(stmt).scalar_one()
    return out


def current_max_ids(conn: Connection) -> dict[str, int]:
    """Max id per fingerprinted table, taken before seeding so a fingerprint covers only new rows."""
    return {
        name: int(conn.execute(select(func.coalesce(func.max(table.c.id), 0))).scalar_one())
        for name, table in _fingerprint_tables().items()
        if name != "task"
    }


def _seed(db_session, seed: str = "t1337"):
    conn = db_session.connection()
    base = current_max_ids(conn)
    counts = seed_all(conn, TINY, seed, "x-not-a-real-hash")
    return counts, fingerprint(conn, base)


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
