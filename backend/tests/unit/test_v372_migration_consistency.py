"""v372 migration + detection-arm consistency (issue #262a/#262c).

The alembic chain must contain v372 (revises v371), and the untracked-DB
detection in ``app/db/migrations.py`` must recognize a v372-shape schema by
its sole relational marker (``watch_source.organization_id``). The detection
test runs against the live test DB (which carries the applied chain), so it
also proves the v372 DDL actually produced the shape the detector keys on.
"""

from __future__ import annotations

from sqlalchemy import inspect


def test_v372_revision_chain():
    from pathlib import Path

    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision("v372_add_audit_organization_id")
    assert rev.down_revision == "v371_repair_cloud_seams_columns"
    # v372 is the current head unless a later revision (v373+) has landed.
    heads = set(scripts.get_heads())
    assert "v372_add_audit_organization_id" in heads or any(
        r.down_revision == "v372_add_audit_organization_id" for r in scripts.walk_revisions()
    )


def test_v372_migration_is_vendor_neutral():
    """The seam guard greps for vendor nouns — the migration must stay generic."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "v372_add_audit_organization_id.py"
    ).read_text()
    # Nouns assembled from parts so this test file itself never trips the guard.
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_v372_on_current_schema(db_session):
    """An untracked DB with the current (post-v372) schema stamps at v372."""
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) == "v372_add_audit_organization_id"


def test_watch_source_org_column_exists(db_session):
    """The v372 DDL produced the column + partial index the code relies on."""
    from sqlalchemy import text

    conn = db_session.connection()
    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='watch_source' AND column_name='organization_id')"
        )
    ).scalar()
    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes "
            "WHERE indexname='ix_watch_source_organization_id')"
        )
    ).scalar()
