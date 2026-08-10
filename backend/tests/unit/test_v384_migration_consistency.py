"""v384 migration + detection-arm consistency (collapsible reasoning display).

The alembic chain must contain v384 (revises v383), and the untracked-DB
detection in ``app/db/migrations.py`` must recognize a v384-shape schema by its
single marker column (``chat_message.reasoning_content``). Same shape as
``test_v373_migration_consistency.py`` — one nullable column, no CHECK, so one
marker is the whole revision.
"""

from __future__ import annotations

from pathlib import Path

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


def test_existing_rows_have_no_reasoning_content(db_session):
    """Every row that predates v384 must read back as NULL, not empty string.

    The application distinguishes "no reasoning" (None, hide the UI block) from
    "reasoning was an empty string" (would never happen, but NULL is still the
    correct default) — a backfilled `''` would make the collapsible section
    render empty on every pre-v384 message.
    """
    conn = db_session.connection()
    non_null = conn.execute(
        text("SELECT count(*) FROM chat_message WHERE reasoning_content IS NOT NULL")
    ).scalar()
    assert non_null == 0


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
