"""v390 migration + detection-arm consistency (``media_file.recorded_date`` + provenance).

Like ``v386``-``v389``, this suite **executes** the revision's SQL rather than grepping its
source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output.

What gets its own test here is the part with a *decision* in it, and for v390 that is the
CHECK constraints rather than the columns. The columns are hygiene; the constraints are the
feature. ``ck_media_file_recorded_date_provenance`` is the whole point of the revision — it
makes "a date whose origin nobody recorded" a state the database refuses to hold, so the
rule survives a caller who forgets, a future bulk backfill, and a `psql` session. A test
that only proved the columns exist would pass just as happily against a schema where that
rule had been dropped, which is the shape this repo keeps finding.

``test_the_source_vocabulary_matches_the_python_enum`` exists for a different reason: the
CHECK lists the six source names in SQL and ``app.core.enums.RecordedDateSource`` lists them
in Python, and a migration is frozen while an enum is not. Adding a seventh source to the
enum without a follow-up revision would produce an ``IntegrityError`` at write time on a
value the application believes is legal.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

#: ``ddl_exclusive`` is applied PER TEST, never to the module: every EXCLUSIVE advisory
#: lock drains all other xdist workers (issue #431).

REVISION = "v390_add_recorded_date_provenance"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

#: The recorded-date objects this revision owns. Named explicitly rather than derived by
#: prefix so that dropping one and renaming it to match the prefix cannot pass.
_CONSTRAINTS = (
    "ck_media_file_recorded_date_source",
    "ck_media_file_recorded_date_provenance",
    "ck_media_file_recorded_date_confidence",
    "ck_media_file_recorded_date_locked_is_manual",
)
_COLUMNS = (
    "recorded_date",
    "recorded_date_source",
    "recorded_date_confidence",
    "recorded_date_candidates",
    "recorded_date_locked",
)


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
            {"e": f"v390_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_file(conn, user_id: int, **recorded) -> int:
    """Insert a media_file, optionally with recorded-date columns set."""
    params = {
        "u": str(uuid_pkg.uuid4()),
        "uid": user_id,
        "rd": recorded.get("recorded_date"),
        "src": recorded.get("recorded_date_source"),
        "conf": recorded.get("recorded_date_confidence"),
        "locked": recorded.get("recorded_date_locked", False),
    }
    return int(
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, user_id, filename, storage_path, file_size, "
                "content_type, recorded_date, recorded_date_source, recorded_date_confidence, "
                "recorded_date_locked) "
                "VALUES (:u, :uid, 'v390.wav', 'x/v390.wav', 1, 'audio/wav', "
                ":rd, :src, :conf, :locked) RETURNING id"
            ),
            params,
        ).scalar()
    )


def test_v390_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v389_add_file_facts"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # True while it is head, and still true once v391 revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v390_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_columns_its_constraints_and_its_index_all_exist(db_session):
    """All of them, not just the columns.

    The guarded CHECK block is separate from the ``ADD COLUMN`` block precisely because
    ``ADD COLUMN IF NOT EXISTS`` is a no-op, so a database that got the columns from an
    earlier interrupted run and never got the rules is a reachable state.
    """
    conn = db_session.connection()
    columns = {c["name"] for c in inspect(conn).get_columns("media_file")}
    for required in _COLUMNS:
        assert required in columns, f"media_file is missing {required}"

    for name in _CONSTRAINTS:
        assert conn.execute(
            text("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = :n)"), {"n": name}
        ).scalar(), f"missing constraint {name}"

    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = 'ix_media_file_recorded_date')"
        )
    ).scalar()


def test_the_index_is_partial_so_unresolved_rows_cost_nothing(db_session):
    """A full index here would carry every row on a deployment that has resolved none.

    Asserted on the stored predicate rather than on the index's existence, because the ORM
    declares this one with ``postgresql_where`` and a plain ``index=True`` would produce an
    index of the same name with no predicate — identical to every check that only asks
    whether the name is present.
    """
    conn = db_session.connection()
    definition = conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_media_file_recorded_date'")
    ).scalar()
    assert definition is not None
    assert "WHERE (recorded_date IS NOT NULL)" in definition, definition


def test_a_date_without_a_source_is_refused(db_session):
    """The constraint that carries the design, exercised rather than described.

    A derived date the user cannot see the origin of is worse than no date: it answers
    "3 meetings in March" when the truth is 5 and offers no way to find out. This is what
    makes "always record the source" a property of the schema instead of a convention.
    """
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        with pytest.raises(IntegrityError):
            _new_file(conn, user_id, recorded_date="2025-03-15T10:00:00Z")
    finally:
        db_session.rollback()


def test_a_source_without_a_date_is_allowed(db_session):
    """The negative control for the rule above — it must not have banned the honest gap.

    ``source='none'`` with a NULL date is exactly how the resolver records "every source
    was absent, and we did look". A constraint written as an equivalence rather than an
    implication would reject it, and the suite would still look green without this test.
    """
    conn = db_session.connection()
    try:
        file_id = _new_file(conn, _new_user(conn), recorded_date_source="none")
        assert (
            conn.execute(
                text("SELECT recorded_date_source FROM media_file WHERE id = :f"), {"f": file_id}
            ).scalar()
            == "none"
        )
    finally:
        db_session.rollback()


def test_an_unknown_source_name_is_refused(db_session):
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        with pytest.raises(IntegrityError):
            _new_file(
                conn,
                user_id,
                recorded_date="2025-03-15T10:00:00Z",
                recorded_date_source="vibes",
            )
    finally:
        db_session.rollback()


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_a_confidence_outside_zero_to_one_is_refused(db_session, confidence):
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        with pytest.raises(IntegrityError):
            _new_file(
                conn,
                user_id,
                recorded_date="2025-03-15T10:00:00Z",
                recorded_date_source="filename",
                recorded_date_confidence=confidence,
            )
    finally:
        db_session.rollback()


def test_a_locked_row_whose_source_is_not_manual_is_refused(db_session):
    """``recorded_date_locked`` means "a human typed this", and only that.

    Without the constraint a re-derivation could overwrite the source to ``filename`` and
    leave the lock set, producing a row that is protected from further correction while no
    longer holding the value the human entered — worse than either state alone.
    """
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        with pytest.raises(IntegrityError):
            _new_file(
                conn,
                user_id,
                recorded_date="2025-03-15T10:00:00Z",
                recorded_date_source="filename",
                recorded_date_locked=True,
            )
    finally:
        db_session.rollback()


def test_a_locked_manual_row_is_allowed(db_session):
    """Negative control for the rule above: the state it must permit."""
    conn = db_session.connection()
    try:
        file_id = _new_file(
            conn,
            _new_user(conn),
            recorded_date="2025-03-15T10:00:00Z",
            recorded_date_source="manual",
            recorded_date_locked=True,
        )
        assert conn.execute(
            text("SELECT recorded_date_locked FROM media_file WHERE id = :f"), {"f": file_id}
        ).scalar()
    finally:
        db_session.rollback()


def test_the_source_vocabulary_matches_the_python_enum(db_session):
    """The SQL CHECK and ``RecordedDateSource`` must list the same six names.

    A migration is frozen the moment it ships; an enum is not. Adding a seventh source in
    Python without a follow-up revision produces an ``IntegrityError`` at write time on a
    value the application is certain is legal — and it would surface on a user's file, not
    in a test, because nothing else compares the two lists.
    """
    from app.core.enums import RecordedDateSource

    conn = db_session.connection()
    definition = conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_media_file_recorded_date_source'"
        )
    ).scalar()
    assert definition is not None
    for member in RecordedDateSource:
        assert f"'{member.value}'" in definition, (
            f"RecordedDateSource.{member.name} is not in the CHECK — writing it raises"
        )


def test_precedence_ranks_every_derivable_source_exactly_once():
    """Precedence is a policy, so it must be total over the sources that can win.

    A source missing from ``PRECEDENCE`` would never be selected however confident it was —
    a candidate silently discarded, which is the failure mode this whole change exists to
    stop. ``NONE`` is excluded because it is the absence of a candidate.
    """
    from app.core.enums import PRECEDENCE
    from app.core.enums import RecordedDateSource

    assert len(PRECEDENCE) == len(set(PRECEDENCE)), "a source is ranked twice"
    assert set(PRECEDENCE) == set(RecordedDateSource) - {RecordedDateSource.NONE}
    assert PRECEDENCE[0] is RecordedDateSource.MANUAL, (
        "a hand-entered date must outrank every derived source"
    )


def test_the_orm_declares_every_constraint_the_database_enforces(db_session):
    """``media_file`` must not gain the 25th DDL-only divergence.

    ``tests/unit/test_orm_ddl_divergence.py`` reached an empty allowlist by declaring all 24
    of them; a new constraint added in raw SQL and not on the model reopens that list. Kept
    here as well as there so the failure names *this* revision.
    """
    from app.db.base import Base

    table = Base.metadata.tables["media_file"]
    declared = {c.name for c in table.constraints if c.name} | {i.name for i in table.indexes}

    conn = db_session.connection()
    live = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'media_file' AND c.contype = 'c'"
            )
        )
    } | {
        row[0]
        for row in conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'media_file' AND indexname LIKE '%recorded_date%'"
            )
        )
    }
    missing = live - declared
    assert not missing, f"the database enforces {sorted(missing)} and the ORM declares none of it"


def test_the_orm_mirrors_the_new_columns_nullability(db_session):
    """The half ``test_schema_drift.py`` cannot see: it compares columns, not nullability.

    ``recorded_date_locked`` is the one that matters — it is ``NOT NULL DEFAULT false`` in
    the database, and an ORM that thinks it is nullable would happily flush a NULL.
    """
    from app.db.base import Base

    live = {
        c["name"]: c["nullable"] for c in inspect(db_session.connection()).get_columns("media_file")
    }
    model = Base.metadata.tables["media_file"]
    for name in _COLUMNS:
        assert name in live, f"{name} is not in the database"
        assert model.columns[name].nullable == live[name], f"{name} nullability disagrees"
    assert live["recorded_date_locked"] is False, "the lock flag must not be nullable"


def test_detection_arm_returns_v390_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped.

    Skip the arm and an untracked database is mis-stamped to the PREVIOUS revision and never
    receives this DDL, so every write of a resolved date fails on a column that is not there.
    """
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_provenance_check(db_session):
    """Drop the marker and the ladder must stop matching v390.

    The arm keys on the CHECK rather than on the column deliberately: a database left with
    the columns but not the rules by a partial run must fall through and re-run, not be
    stamped as done. This test is what makes that choice real — it drops **only** the
    constraint and leaves every column in place.

    Asserted as a *band* — at or after v389, strictly before v390 — because an exact ``==``
    on a lower revision goes red or vacuous the next time the ladder above it changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(
            text("ALTER TABLE media_file DROP CONSTRAINT ck_media_file_recorded_date_provenance")
        )
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        # finally, not a trailing call: this mutates the SHARED dev schema.
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert order.index("v389_add_file_facts") <= order.index(detected) < order.index(REVISION)


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    """The invariant the startup runner depends on, executed rather than asserted about."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))

        for name in _CONSTRAINTS:
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
                    "WHERE indexname = 'ix_media_file_recorded_date'"
                )
            ).scalar()
            == 1
        )
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_checks_are_added_even_when_the_columns_already_exist(db_session):
    """``ADD COLUMN IF NOT EXISTS`` is a no-op, so an inline CHECK would be skipped forever.

    Simulates the real partial-run state: a database that got the columns from an earlier,
    interrupted run and is missing only the rules that make them trustworthy.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        for name in _CONSTRAINTS:
            conn.execute(text(f"ALTER TABLE media_file DROP CONSTRAINT {name}"))
        conn.execute(text(module.UPGRADE_SQL))
        for name in _CONSTRAINTS:
            assert conn.execute(
                text("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = :n)"), {"n": name}
            ).scalar(), f"{name} was not restored by a re-run over existing columns"
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_the_columns_and_the_upgrade_restores_them(db_session):
    """The downgrade is executed here, not merely read.

    Before issue #431 no downgrade in this chain had ever been run by a test, so "downgrade
    mirrors upgrade" was a claim about source text.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways
        remaining = {c["name"] for c in inspect(conn).get_columns("media_file")}
        assert not (set(_COLUMNS) & remaining), (
            f"downgrade left {sorted(set(_COLUMNS) & remaining)}"
        )

        conn.execute(text(module.UPGRADE_SQL))
        restored = {c["name"] for c in inspect(conn).get_columns("media_file")}
        assert set(_COLUMNS) <= restored
    finally:
        db_session.rollback()
