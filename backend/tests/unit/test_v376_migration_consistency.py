"""v376 (chat projects) schema consistency — issue #360.

Pins the two properties the DDL exists to guarantee and that a later refactor
could plausibly break: ``chat_conversation.project_id`` is NULLABLE (so every
pre-v376 conversation keeps working, ungrouped) and its FK is ON DELETE SET
NULL (so deleting a project never destroys the threads inside it).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy import text


def _versions_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "alembic" / "versions"


def test_revision_chains_onto_v375():
    source = (_versions_dir() / "v376_add_chat_projects.py").read_text()
    assert 'revision = "v376_add_chat_projects"' in source
    assert 'down_revision = "v375_add_chat_tables"' in source


def test_ddl_is_idempotent():
    """Every revision in this project must be safe to re-run."""
    source = (_versions_dir() / "v376_add_chat_projects.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS chat_project" in source
    assert source.count("IF NOT EXISTS") >= 4


def test_detection_arm_returns_v376_on_current_schema(db_session):
    """An untracked DB with chat_project stamps at v376."""
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) == "v376_add_chat_projects"


def test_chat_project_table_exists(db_session):
    tables = inspect(db_session.connection()).get_table_names()
    assert "chat_project" in tables


def test_project_id_is_nullable(db_session):
    """NULL = ungrouped, which is every conversation created before v376."""
    columns = {
        c["name"]: c for c in inspect(db_session.connection()).get_columns("chat_conversation")
    }
    assert "project_id" in columns
    assert columns["project_id"]["nullable"] is True


def test_project_fk_is_set_null_not_cascade(db_session):
    """Deleting a project must leave its conversations behind, ungrouped.

    Asserted against the live catalog rather than the migration text: a later
    revision could alter the constraint without touching v376's source.
    """
    row = db_session.execute(
        text("""
            SELECT rc.delete_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.referential_constraints rc
              ON tc.constraint_name = rc.constraint_name
            WHERE tc.table_name = 'chat_conversation'
              AND tc.constraint_type = 'FOREIGN KEY'
              AND kcu.column_name = 'project_id'
        """)
    ).first()
    assert row is not None, "no FK on chat_conversation.project_id"
    assert row[0] == "SET NULL"
