"""v375 migration + detection-arm consistency (issue #52, RAG chat).

The alembic chain must contain v375 (revises v374), and the untracked-DB
detection in ``app/db/migrations.py`` must recognize a v375-shape schema by its
relational marker (the ``chat_conversation`` table). The detection test runs
against the live test DB (which carries the applied chain), so it also proves
the v375 DDL actually produced the shape the detector keys on.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect


def _versions_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "alembic" / "versions"


def test_v375_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision("v375_add_chat_tables")
    assert rev.down_revision == "v374_add_tag_user_id"
    # v375 is the current head unless a later revision (v376+) has landed.
    heads = set(scripts.get_heads())
    assert "v375_add_chat_tables" in heads or any(
        r.down_revision == "v375_add_chat_tables" for r in scripts.walk_revisions()
    )


def test_v375_single_head():
    """Two heads mean two branches both claimed a revision number."""
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1, "alembic chain must have exactly one head"


def test_v375_migration_is_idempotent_sql():
    """Re-running on a partially-migrated DB must not error."""
    source = (_versions_dir() / "v375_add_chat_tables.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS chat_conversation" in source
    assert "CREATE TABLE IF NOT EXISTS chat_message" in source
    assert source.count("CREATE INDEX IF NOT EXISTS") == 3


def test_v375_migration_is_vendor_neutral():
    """The seam guard greps for vendor nouns — the migration must stay generic."""
    source = (_versions_dir() / "v375_add_chat_tables.py").read_text()
    # Nouns assembled from parts so this test file itself never trips the guard.
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_at_least_v375_on_current_schema(db_session):
    """An untracked DB with the chat tables stamps at v375 or newer.

    Widened deliberately: ``_detect_schema_version`` returns the NEWEST matching
    revision, so pinning an exact value here would break on every subsequent
    migration. The exact stamp for a revision belongs in that revision's own
    suite — v376's is in ``test_v376_migration_consistency.py``.

    "Or newer" is decided by **position in the alembic chain**, not by comparing the
    revision ids as strings. The string form (``detected >= "v375_add_chat_tables"``) reads
    as the same assertion but is lexicographic, so it holds only while every revision
    number has the same number of digits: ``"v3100_…" < "v375_…"``, and a chain that
    reaches v3100 would fail this test for a ladder that was answering correctly.
    """
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, "v375_add_chat_tables")


def test_chat_tables_have_expected_shape(db_session):
    """The applied DDL matches what the models and services assume."""
    conn = db_session.connection()
    inspector = inspect(conn)

    conv_cols = {c["name"] for c in inspector.get_columns("chat_conversation")}
    assert {
        "id",
        "uuid",
        "user_id",
        "organization_id",
        "title",
        "context",
        "llm_config_id",
        "settings",
        "is_archived",
        "last_message_at",
    } <= conv_cols

    msg_cols = {c["name"] for c in inspector.get_columns("chat_message")}
    assert {
        "id",
        "uuid",
        "conversation_id",
        "role",
        "content",
        "citations",
        "msg_metadata",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "tokens_estimated",
        "provider",
        "model",
        "status",
        "error",
    } <= msg_cols


def test_chat_message_cascades_from_conversation(db_session):
    """Deleting a conversation must take its messages with it (GDPR + hard delete)."""
    conn = db_session.connection()
    fks = inspect(conn).get_foreign_keys("chat_message")
    conv_fk = next(fk for fk in fks if fk["referred_table"] == "chat_conversation")
    assert conv_fk["options"].get("ondelete") == "CASCADE"
