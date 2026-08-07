"""Shared helper for the per-revision detection-arm tests.

Every ``test_v3NN_migration_consistency.py`` asserts that
``_detect_schema_version()`` recognises the live (fully-migrated) test database. The
naive form — ``== REVISION`` — is only true while that revision is head, so each new
revision silently turned its predecessor's test red. Three were already failing that
way before this helper existed.

What the assertion is actually *for* is that the ladder never stamps a database
**lower** than the revision whose markers it carries: stamping low is the failure that
silently skips DDL. So compare positions in the chain instead of identities.
"""

from __future__ import annotations

from pathlib import Path


def _chain_order() -> list[str]:
    """Revision ids oldest-first, read from the alembic chain itself."""
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    return [rev.revision for rev in reversed(list(scripts.walk_revisions()))]


def assert_detected_at_or_after(conn, tables: list[str], revision: str) -> str:
    """Assert the untracked-DB detector stamps *revision* or something later.

    Args:
        conn: Live connection to the migrated test database.
        tables: Table names, as ``_detect_schema_version`` expects.
        revision: The revision under test.

    Returns:
        The revision the detector actually chose, for further assertions.
    """
    from app.db.migrations import _detect_schema_version

    detected = _detect_schema_version(conn, tables)
    order = _chain_order()
    assert detected in order, f"detector returned an unknown revision {detected!r}"
    assert order.index(detected) >= order.index(revision), (
        f"a schema carrying {revision}'s markers was stamped {detected!r}, which is "
        "EARLIER in the chain — that database would never receive the missing DDL"
    )
    return detected
