"""``_repair_skipped_v230`` must run the v230 revision's own ``upgrade()`` against real Postgres.

It used to import ``alembic.versions.v230...`` (not importable: the installed library owns the
``alembic`` name) and swallow the ImportError into a raw-SQL fallback, so the primary path never
ran. Here the fallback is gone, so a broken primary path fails the test instead of hiding.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.migration_connection import create_migration_engine
from app.db.migrations import _check_column_exists
from app.db.migrations import _repair_skipped_v230
from app.db.migrations import get_alembic_config

pytestmark = pytest.mark.integration


@pytest.fixture
def skipped_v230_schema():
    engine = create_migration_engine()
    with engine.begin() as conn:
        for stmt in (
            "ALTER TABLE media_file DROP COLUMN IF EXISTS upload_batch_id",
            "ALTER TABLE file_tag DROP COLUMN IF EXISTS created_at",
        ):
            conn.execute(text(stmt))
    yield engine
    engine.dispose()


def test_repair_restores_v230_columns_via_revision_upgrade(skipped_v230_schema):
    engine = skipped_v230_schema
    _repair_skipped_v230(get_alembic_config())
    with engine.connect() as conn:
        assert _check_column_exists(conn, "media_file", "upload_batch_id")
        # Only the revision's own upgrade() adds this; the old raw-SQL fallback never did.
        assert _check_column_exists(conn, "file_tag", "created_at")
        assert _check_column_exists(conn, "tag", "normalized_name")


def test_repair_is_idempotent_when_schema_current(skipped_v230_schema):
    engine = skipped_v230_schema
    _repair_skipped_v230(get_alembic_config())
    _repair_skipped_v230(get_alembic_config())
    with engine.connect() as conn:
        assert _check_column_exists(conn, "media_file", "upload_batch_id")
        assert _check_column_exists(conn, "file_tag", "created_at")
