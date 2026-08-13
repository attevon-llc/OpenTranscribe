"""v389 migration + detection-arm consistency (``file_facts``).

The deterministic ingest artifacts of #383 Phase 2 land in their own table. Like
``v386``-``v388``, this suite **executes** the revision's SQL rather than grepping its
source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output. A
migration failure is ``SystemExit(1)``, so a non-idempotent revision does not degrade —
the backend refuses to start.

Two things get their own tests because they are the parts with a *decision* in them: the
``ON DELETE CASCADE`` (every other FK style in this schema is deliberately ``NO ACTION``,
so the exception needs a reason and a test), and the guarded FK, which a plain
``CREATE TABLE IF NOT EXISTS`` would silently skip on a database created by an earlier
partial run.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

#: ``ddl_exclusive`` is applied PER TEST, never to the module: every EXCLUSIVE advisory
#: lock drains all other xdist workers (issue #431).

REVISION = "v389_add_file_facts"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    """Load the revision by path — ``alembic/`` is not an importable package (see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_user(conn) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"v389_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_file(conn, user_id: int) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, user_id, filename, storage_path, file_size, "
                "content_type) VALUES (:u, :uid, 'v389.wav', 'x/v389.wav', 1, 'audio/wav') "
                "RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id},
        ).scalar()
    )


def _new_facts(conn, file_id: int, **overrides) -> int:
    params = {
        "f": file_id,
        "gv": "1.1.1",
        "fp": "0" * 64,
        "dw": 100,
        "sc": 2,
        "ms": 42,
        **overrides,
    }
    return int(
        conn.execute(
            text(
                "INSERT INTO file_facts (media_file_id, generator_version, source_fingerprint, "
                "facts, digest, keyphrases, digest_word_count, section_count, generation_ms) "
                "VALUES (:f, :gv, :fp, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, :dw, :sc, :ms) "
                "RETURNING id"
            ),
            params,
        ).scalar()
    )


def test_v389_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v388_add_user_group_organization_id"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # True while it is head, and still true once v390 revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v389_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_table_its_constraints_and_its_index_all_exist(db_session):
    """All of them, not just the table.

    A partial hand-repair is the realistic failure: the guarded FK block is separate from
    the ``CREATE TABLE``, so a database that got the table without the FK is reachable.
    """
    conn = db_session.connection()
    columns = {c["name"]: c for c in inspect(conn).get_columns("file_facts")}
    for required in (
        "media_file_id",
        "generator_version",
        "source_fingerprint",
        "facts",
        "digest",
        "keyphrases",
        "digest_word_count",
        "section_count",
        "generation_ms",
        "generated_at",
    ):
        assert required in columns, f"file_facts is missing {required}"

    for name in (
        "uq_file_facts_media_file",
        "ck_file_facts_digest_word_count",
        "ck_file_facts_section_count",
        "ck_file_facts_ms",
        "file_facts_media_file_id_fkey",
    ):
        assert conn.execute(
            text("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = :n)"), {"n": name}
        ).scalar(), f"missing constraint {name}"

    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes "
            "WHERE indexname = 'ix_file_facts_generator_version')"
        )
    ).scalar()


def test_the_fk_cascades_so_deleting_a_file_takes_its_artifacts(db_session):
    """The one FK in this schema that is deliberately NOT ``NO ACTION``.

    The org-stamp house rule is NO ACTION so a tenant delete cannot silently strip rows of
    their tenancy. Here the row is *derived* from the file, so leaving it behind would be
    an orphan nothing can regenerate or reach — and a cleanup pass somebody has to
    remember. Executed rather than read off ``confdeltype``, because the behaviour is what
    matters.
    """
    conn = db_session.connection()
    try:
        file_id = _new_file(conn, _new_user(conn))
        _new_facts(conn, file_id)
        assert (
            conn.execute(
                text("SELECT count(*) FROM file_facts WHERE media_file_id = :f"), {"f": file_id}
            ).scalar()
            == 1
        )

        conn.execute(text("DELETE FROM media_file WHERE id = :f"), {"f": file_id})

        assert (
            conn.execute(
                text("SELECT count(*) FROM file_facts WHERE media_file_id = :f"), {"f": file_id}
            ).scalar()
            == 0
        )
    finally:
        db_session.rollback()


def test_a_second_row_for_one_file_is_refused(db_session):
    """``UNIQUE (media_file_id)`` is what makes regeneration an upsert, not a race."""
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    try:
        file_id = _new_file(conn, _new_user(conn))
        _new_facts(conn, file_id)
        with pytest.raises(IntegrityError):
            _new_facts(conn, file_id)
    finally:
        db_session.rollback()


@pytest.mark.parametrize(
    ("column", "value"),
    [("dw", -1), ("sc", -1), ("ms", -1)],
)
def test_the_check_constraints_refuse_negative_counts(db_session, column, value):
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    try:
        file_id = _new_file(conn, _new_user(conn))
        with pytest.raises(IntegrityError):
            _new_facts(conn, file_id, **{column: value})
    finally:
        db_session.rollback()


def test_the_orm_declares_every_constraint_the_database_enforces(db_session):
    """This table must not become the 25th DDL-only divergence.

    ``.rag-403/ddl-orm-divergence.md`` catalogues 24 constraints Postgres enforces and
    Python never stated; ``uq_transcript_segment_content`` is the one that aborted an
    eval-corpus load as an ``IntegrityError`` naming a constraint that appeared nowhere in
    the tree. A new table is the cheapest possible moment to not repeat that.
    """
    from app.db.base import Base

    table = Base.metadata.tables["file_facts"]
    declared = {c.name for c in table.constraints if c.name} | {i.name for i in table.indexes}

    conn = db_session.connection()
    live = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = 'file_facts' "
                "AND c.contype IN ('u','c','f')"
            )
        )
    } | {
        row[0]
        for row in conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'file_facts'")
        )
        if not row[0].endswith("_pkey")
    }
    # The UNIQUE constraint also creates an index of the same name; one declaration covers
    # both, so compare on names rather than object classes.
    missing = live - declared
    assert not missing, f"the database enforces {sorted(missing)} and the ORM declares none of it"


def test_the_orm_mirrors_the_columns_nullability(db_session):
    """The half ``test_schema_drift.py`` cannot see: it compares columns, not nullability."""
    from app.db.base import Base

    live = {
        c["name"]: c["nullable"] for c in inspect(db_session.connection()).get_columns("file_facts")
    }
    model = Base.metadata.tables["file_facts"]
    assert set(live) == set(model.columns.keys()), (
        "the ORM and the table do not even agree on the column set, so the per-column "
        "comparison below would silently check a subset"
    )
    for name, nullable in live.items():
        assert model.columns[name].nullable == nullable, f"{name} nullability disagrees"


def test_detection_arm_returns_v389_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped.

    Skip the arm and an untracked database is mis-stamped to the PREVIOUS revision and
    never receives this DDL, so the artifact writer inserts into a table that is not there.
    """
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_table(db_session):
    """Drop the marker and the ladder must stop matching v389.

    Asserted as a *band* — at or after v388, strictly before v389 — because an exact ``==``
    on a lower revision goes red or vacuous the next time the ladder above it changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP TABLE file_facts"))
        tables = [t for t in inspect(conn).get_table_names() if t != "file_facts"]
        detected = _detect_schema_version(conn, tables)
    finally:
        # finally, not a trailing call: this mutates the SHARED dev schema.
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v388_add_user_group_organization_id")
        <= order.index(detected)
        < order.index(REVISION)
    )


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    """The invariant the startup runner depends on, executed rather than asserted about."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))

        assert "file_facts" in inspect(conn).get_table_names()
        for name in ("file_facts_media_file_id_fkey", "uq_file_facts_media_file"):
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = :n"), {"n": name}
                ).scalar()
                == 1
            ), f"{name} was created twice — the guard is not a guard"
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes "
                    "WHERE indexname = 'ix_file_facts_generator_version'"
                )
            ).scalar()
            == 1
        )
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_fk_is_added_even_when_the_table_already_exists(db_session):
    """``CREATE TABLE IF NOT EXISTS`` is a no-op, so an inline FK would be skipped forever.

    Simulates the real partial-run state: a database that got the table from an earlier,
    interrupted run and is missing only the constraint.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text("ALTER TABLE file_facts DROP CONSTRAINT file_facts_media_file_id_fkey"))
        conn.execute(text(module.UPGRADE_SQL))
        assert conn.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_constraint "
                "WHERE conname = 'file_facts_media_file_id_fkey')"
            )
        ).scalar()
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_the_table_and_the_upgrade_restores_it(db_session):
    """The downgrade is executed here, not merely read.

    Before issue #431 no downgrade in this chain had ever been run by a test, so
    "``downgrade()`` mirrors ``upgrade()``" was a claim about source text.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways
        assert "file_facts" not in inspect(conn).get_table_names()

        conn.execute(text(module.UPGRADE_SQL))
        assert "file_facts" in inspect(conn).get_table_names()
    finally:
        db_session.rollback()
