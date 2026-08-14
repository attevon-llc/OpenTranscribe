"""v391 migration + detection-arm consistency (``media_file.redaction_coverage``).

Like ``v386``-``v390``, this suite **executes** the revision's SQL rather than grepping its
source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output.

The part with a *decision* in it is the **type**. JSONB was the obvious proposal for
"coverage"; the column holds a closed four-name vocabulary that exactly one reader
consults by primary key, and nothing filters, aggregates or joins on it. So the tests
that matter here assert the array is a real ``TEXT[]`` — a list goes in, a list comes
back, and a set-membership predicate runs in SQL — because a column that had silently
become ``TEXT`` or ``JSONB`` would still satisfy "the column exists" and would still
round-trip through SQLAlchemy for the one shape the app happens to write today.

There is no CHECK on the element vocabulary, deliberately: a stray name grants no
coverage (the gap is ``required - covered``), while the hazardous state — a **missing**
name — is exactly what no CHECK can see.
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

REVISION = "v391_add_redaction_coverage"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_COLUMN = "redaction_coverage"


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
            {"e": f"v391_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_file(conn, user_id: int, coverage=None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, user_id, filename, storage_path, file_size, "
                "content_type, redaction_coverage) "
                "VALUES (:u, :uid, 'v391.wav', 'x/v391.wav', 1, 'audio/wav', :cov) RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id, "cov": coverage},
        ).scalar()
    )


def test_v391_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v390_add_recorded_date_provenance"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # True while it is head, and still true once v392 revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v391_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_column_exists_and_is_a_text_array(db_session):
    """The type is the decision, so the type is what gets asserted.

    ``information_schema`` reports an array as ``ARRAY`` with the element type in
    ``element_types``; a ``TEXT`` or ``JSONB`` column would report neither, while still
    passing any check that only asks whether the name is present.
    """
    conn = db_session.connection()
    row = conn.execute(
        text(
            "SELECT data_type, udt_name FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = :c"
        ),
        {"c": _COLUMN},
    ).one_or_none()
    assert row is not None, "media_file is missing redaction_coverage"
    assert row[0] == "ARRAY", f"expected a postgres array, got {row[0]}"
    assert row[1] == "_text", f"expected an array of text, got {row[1]}"


def test_a_coverage_list_round_trips_and_stays_queryable(db_session):
    """A list in, a list out — and a set predicate that runs in SQL.

    The second half is the justification for the type over JSONB, exercised rather than
    argued: an operator asking "which files were scanned without PII coverage" writes
    ``NOT ('pii' = ANY(redaction_coverage))`` against this column, with no ``->>`` and no
    cast. If it ever silently became JSONB, this is the assertion that would notice.
    """
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        covered = _new_file(conn, user_id, coverage=["profanity", "pii", "toxicity"])
        uncovered = _new_file(conn, user_id, coverage=["profanity", "toxicity"])

        assert conn.execute(
            text("SELECT redaction_coverage FROM media_file WHERE id = :f"), {"f": covered}
        ).scalar() == ["profanity", "pii", "toxicity"]

        gaps = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT id FROM media_file WHERE user_id = :u "
                    "AND redaction_coverage IS NOT NULL "
                    "AND NOT ('pii' = ANY(redaction_coverage))"
                ),
                {"u": user_id},
            )
        }
        assert gaps == {uncovered}
    finally:
        db_session.rollback()


def test_a_row_may_carry_no_coverage_at_all(db_session):
    """The negative control for the column, and the state every pre-v391 row is in.

    NULL is load-bearing rather than an oversight: it is the honest record for a scan
    that ran before the column existed, and ``coverage.uncovered_detectors`` reads it as
    "unknown, no worse than yesterday" instead of refusing every legacy file on upgrade
    day. A NOT NULL column with a ``'{}'`` default would have made that unrepresentable
    and turned the upgrade into an outage.
    """
    conn = db_session.connection()
    try:
        file_id = _new_file(conn, _new_user(conn), coverage=None)
        assert (
            conn.execute(
                text("SELECT redaction_coverage FROM media_file WHERE id = :f"), {"f": file_id}
            ).scalar()
            is None
        )
    finally:
        db_session.rollback()


def test_the_orm_mirrors_the_column(db_session):
    """The half ``test_schema_drift.py`` cannot see: it compares names, not nullability."""
    from app.db.base import Base

    live = {
        c["name"]: c["nullable"] for c in inspect(db_session.connection()).get_columns("media_file")
    }
    model = Base.metadata.tables["media_file"]
    assert _COLUMN in live, "the column is not in the database"
    assert model.columns[_COLUMN].nullable == live[_COLUMN]
    assert live[_COLUMN] is True, "a pre-v391 row must be able to say nothing at all"


def test_detection_arm_returns_v391_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped.

    Skip the arm and an untracked database is mis-stamped to the PREVIOUS revision and
    never receives this DDL. That failure is quiet in a specific and bad way here: the
    ORM would raise ``UndefinedColumn`` on every redaction scan, so redaction stops on a
    deployment whose transcripts still flow to LLM providers.
    """
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_column(db_session):
    """Drop the marker and the ladder must stop matching v391.

    Asserted as a *band* — at or after v390, strictly before v391 — because an exact
    ``==`` on a lower revision goes red or vacuous the next time the ladder changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE media_file DROP COLUMN {_COLUMN}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        # finally, not a trailing call: this mutates the SHARED dev schema.
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v390_add_recorded_date_provenance")
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
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'media_file' AND column_name = :c"
                ),
                {"c": _COLUMN},
            ).scalar()
            == 1
        )
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_the_column_and_the_upgrade_restores_it(db_session):
    """The downgrade is executed here, not merely read.

    Before issue #431 no downgrade in this chain had ever been run by a test, so
    "downgrade mirrors upgrade" was a claim about source text.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways
        assert _COLUMN not in {c["name"] for c in inspect(conn).get_columns("media_file")}

        conn.execute(text(module.UPGRADE_SQL))
        assert _COLUMN in {c["name"] for c in inspect(conn).get_columns("media_file")}
    finally:
        db_session.rollback()
