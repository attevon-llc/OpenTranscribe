"""v461 migration + detection-arm consistency (a privacy purge's pending centroid work, #1334).

The idempotency and downgrade checks run the revision's real SQL in a PRIVATE scratch database
(precedent: ``test_v459_migration_consistency.py``), so no DDL touches the shared suite database;
each takes the suite DDL lock EXCLUSIVE, which is what ``test_ddl_marker_discipline`` looks for in
a test that runs DDL without ``db_session``.
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.base import engine as shared_engine
from tests.db_locks import acquire_ddl_lock_exclusive

REVISION = "v461_add_privacy_purge_pending"
TABLE = "privacy_purge_pending"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@contextmanager
def _scratch_database() -> Iterator[Engine]:
    """An empty database of our own on the test server."""
    name = f"ot_v461_{uuid.uuid4().hex[:10]}_test"
    url = make_url(settings.DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    scratch = create_engine(url.set(database=name))
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield scratch
    finally:
        scratch.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _insert(conn, *, task_id: str | None = None, scope: str = "{}", ids: str = "[]") -> None:
    conn.execute(
        text(
            "INSERT INTO privacy_purge_pending (task_id, scope, cluster_uuids) "
            "VALUES (:t, CAST(:s AS jsonb), CAST(:c AS jsonb))"
        ),
        {"t": task_id or str(uuid.uuid4()), "s": scope, "c": ids},
    )


# --- no database -----------------------------------------------------------------------------


def test_revision_chain_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_PATH.parents[1]))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v460_add_llm_token_budget"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


# --- the migrated suite database -------------------------------------------------------------


def test_detection_arm_returns_v461_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_the_live_table_matches_the_model_and_names_no_one_by_foreign_key(db_session):
    from app.models.privacy_purge_pending import PrivacyPurgePending

    conn = db_session.connection()
    live = {c["name"] for c in inspect(conn).get_columns(TABLE)}
    assert live == set(PrivacyPurgePending.__table__.columns.keys())
    assert "ix_privacy_purge_pending_created_at" in {
        i["name"] for i in inspect(conn).get_indexes(TABLE)
    }
    # No foreign key: the row must outlive the task row until its documents are gone, and so it
    # is outside the erasure inventory's reach (it holds surrogate ids only).
    assert inspect(conn).get_foreign_keys(TABLE) == []


@pytest.mark.parametrize(
    ("scope", "ids"),
    [("{}", '{"a": 1}'), ("[]", "[]"), ("{}", '"one"')],
)
def test_the_check_refuses_a_list_that_is_not_an_array_or_a_scope_that_is_not_an_object(
    db_session, scope, ids
):
    conn = db_session.connection()
    with pytest.raises(IntegrityError, match="ck_privacy_purge_pending_array"), conn.begin_nested():
        _insert(conn, scope=scope, ids=ids)


def test_one_row_per_purge(db_session):
    conn = db_session.connection()
    task_id = str(uuid.uuid4())
    _insert(conn, task_id=task_id)
    with pytest.raises(IntegrityError), conn.begin_nested():
        _insert(conn, task_id=task_id)


# --- private scratch database: idempotent upgrade, downgrade ---------------------------------


def test_the_upgrade_is_idempotent_and_the_downgrade_removes_the_table():
    module = _revision_module()
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch:
            with scratch.begin() as conn:
                conn.execute(text(module.UPGRADE_SQL))
                conn.execute(text(module.UPGRADE_SQL))
                constraints = {
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'privacy_purge_pending'::regclass"
                        )
                    )
                }
                assert "ck_privacy_purge_pending_array" in constraints
                _insert(conn, ids='["x"]')
            with scratch.begin() as conn:
                conn.execute(text(module.DOWNGRADE_SQL))
                conn.execute(text(module.DOWNGRADE_SQL))
                assert TABLE not in inspect(conn).get_table_names()
