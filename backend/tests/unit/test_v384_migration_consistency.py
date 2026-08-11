"""v384 migration + detection-arm consistency (collapsible reasoning display).

The alembic chain must contain v384 (revises v383), and the untracked-DB
detection in ``app/db/migrations.py`` must recognize a v384-shape schema by its
single marker column (``chat_message.reasoning_content``). Same shape as
``test_v373_migration_consistency.py`` — one nullable column, no CHECK, so one
marker is the whole revision.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v384_add_chat_reasoning_content"


def test_v384_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    assert rev.down_revision == "v383_saml_auth_type"

    # v384 is the current head unless a later revision has landed.
    heads = set(scripts.get_heads())
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v384_migration_is_vendor_neutral():
    """The seam guard greps for vendor nouns — the migration must stay generic."""
    source = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
    ).read_text()
    # Nouns assembled from parts so this test file itself never trips the guard.
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_v384_or_later_on_current_schema(db_session):
    """An untracked DB with the current schema stamps at v384 or a later arm.

    Uses ``assert_detected_at_or_after`` (chain-position comparison, INCLUSIVE of
    ``REVISION`` itself) rather than the ``revisions_at_or_after`` fixture — that
    fixture's ``iterate_revisions("heads", base)`` is exclusive of ``base``, which
    is fine for an older revision (something later always covers it) but wrong
    for testing the CURRENT head against itself: with no later revision to be
    "at or after", it returns an empty set and the assertion can never pass.
    """
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


@pytest.mark.ddl_exclusive
def test_detection_needs_the_marker_column(db_session):
    """Dropping the column must stamp lower so the DDL still runs on upgrade."""
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    conn.execute(text("ALTER TABLE chat_message DROP COLUMN IF EXISTS reasoning_content"))
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) != REVISION
    db_session.rollback()


def test_reasoning_content_column_exists_and_is_nullable(db_session):
    conn = db_session.connection()
    columns = {c["name"]: c for c in inspect(conn).get_columns("chat_message")}
    assert "reasoning_content" in columns
    assert columns["reasoning_content"]["nullable"] is True


def test_migration_did_not_backfill_empty_strings(db_session):
    """v384 must leave pre-existing rows NULL, never `''`.

    The application distinguishes "no reasoning" (None → hide the collapsible
    block) from "reasoning was an empty string" — a backfilled `''` would render
    an empty section on every pre-v384 message.

    This used to assert `count(reasoning_content IS NOT NULL) == 0`, i.e. that no
    row ANYWHERE had reasoning content. That was strictly stronger than the
    invariant above and only held on a database nobody had used: the app now ships
    reasoning-model support (the `mock-reasoning` scenario model and the
    collapsible display exist for it), so any legitimate use of the feature made
    this fail. It did, on the dev database — 4 rows, all with real content and
    zero empty strings, i.e. the invariant held and only the assertion was wrong
    (issue #398).

    Asserting the absence of `''` keeps the migration's actual contract pinned
    while surviving real use of the feature it covers.
    """
    conn = db_session.connection()
    empty_strings = conn.execute(
        text("SELECT count(*) FROM chat_message WHERE reasoning_content = ''")
    ).scalar()
    assert empty_strings == 0, (
        f"{empty_strings} chat_message row(s) have reasoning_content = '' — v384 "
        "must leave pre-existing rows NULL so the UI can tell 'no reasoning' from "
        "'empty reasoning'."
    )


def test_reasoning_content_has_no_server_default(db_session):
    """A server default is the other way `''` could appear on a pre-v384 row.

    The column check above proves nullability; this proves nothing is silently
    filling the column in, which is what would turn a NULL into an empty string
    for every row inserted after the migration.
    """
    conn = db_session.connection()
    columns = {c["name"]: c for c in inspect(conn).get_columns("chat_message")}
    assert columns["reasoning_content"].get("default") is None


def test_downgrade_mirrors_the_upgrade():
    """The downgrade drops exactly the column the upgrade added."""
    import importlib.util

    revision_path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
    spec = importlib.util.spec_from_file_location(REVISION, revision_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    import inspect as py_inspect

    down = py_inspect.getsource(module.downgrade)
    assert "reasoning_content" in down
    assert "IF EXISTS" in down
