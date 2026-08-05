"""v373 migration + detection-arm consistency (issue #262, cluster plane).

The alembic chain must contain v373 (revises v372), and the untracked-DB
detection in ``app/db/migrations.py`` must recognize a v373-shape schema by
its relational marker (``speaker_cluster.organization_id``). The detection
test runs against the live test DB (which carries the applied chain), so it
also proves the v373 DDL actually produced the shape the detector keys on.
"""

from __future__ import annotations

from sqlalchemy import inspect


def test_v373_revision_chain():
    from pathlib import Path

    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision("v373_add_cluster_organization_id")
    assert rev.down_revision == "v372_add_audit_organization_id"
    # v373 is the current head unless a later revision (v374+) has landed.
    heads = set(scripts.get_heads())
    assert "v373_add_cluster_organization_id" in heads or any(
        r.down_revision == "v373_add_cluster_organization_id" for r in scripts.walk_revisions()
    )


def test_v373_migration_is_vendor_neutral():
    """The seam guard greps for vendor nouns — the migration must stay generic."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "v373_add_cluster_organization_id.py"
    ).read_text()
    # Nouns assembled from parts so this test file itself never trips the guard.
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_v373_or_later_on_current_schema(db_session, revisions_at_or_after):
    """An untracked DB with the current schema stamps at v373 or a later arm.

    v373's arm is only the *answer* while v373 is head; once a newer revision
    lands (v374 added ``tag.user_id``) the newest-first ladder correctly returns
    that instead. What must never happen is falling BACK below v373 — that would
    re-stamp a v373-shaped DB at v372 and replay v373's DDL. The precise
    post-v374 assertion lives in ``test_v374_migration_consistency.py``.
    """
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) in revisions_at_or_after(
        "v373_add_cluster_organization_id"
    )


def test_speaker_cluster_org_column_exists(db_session):
    """The v373 DDL produced the column + partial index the code relies on."""
    from sqlalchemy import text

    conn = db_session.connection()
    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='speaker_cluster' AND column_name='organization_id')"
        )
    ).scalar()
    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes "
            "WHERE indexname='ix_speaker_cluster_organization_id')"
        )
    ).scalar()
