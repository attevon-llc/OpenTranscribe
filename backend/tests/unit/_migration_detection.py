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

import importlib.util
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.engine import make_url

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


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


def load_revision_module(revision: str) -> ModuleType:
    """Import an alembic revision file by id, to read its ``UPGRADE_SQL`` and friends."""
    spec = importlib.util.spec_from_file_location(revision, _VERSIONS / f"{revision}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_follows_with_single_head(revision: str, down_revision: str) -> None:
    """Assert *revision* sits directly after *down_revision* and the chain has one head."""
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_VERSIONS.parent))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(revision).down_revision == down_revision
    assert len(set(scripts.get_heads())) == 1


@contextmanager
def scratch_connection(prefix: str) -> Iterator[Connection]:
    """A transactional connection to a private throwaway database.

    Lets a revision's real SQL run without touching the shared test database. This does
    NOT take the DDL lock: the caller must hold ``acquire_ddl_lock_exclusive`` itself, in
    the test body, where ``test_ddl_marker_discipline`` can see it.
    """
    from app.core.config import settings

    name = f"{prefix}_{uuid.uuid4().hex[:10]}_test"
    url = make_url(settings.DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    scratch = create_engine(url.set(database=name))
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        with scratch.begin() as conn:
            yield conn
    finally:
        scratch.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
