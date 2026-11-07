"""v459 migration + detection-arm consistency (governance audit outbox, issue #1329).

The idempotency and downgrade checks run the revision's real SQL in a PRIVATE scratch database
(precedent: ``test_v458_migration_consistency.py``), so no DDL touches the shared suite database;
each such test takes the suite DDL lock EXCLUSIVE, which is what ``test_ddl_marker_discipline``
looks for in a test that runs DDL without ``db_session``.
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

REVISION = "v459_add_audit_outbox"
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
    name = f"ot_v459_{uuid.uuid4().hex[:10]}_test"
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


def _insert(conn, **overrides) -> None:
    values = {"u": str(uuid.uuid4()), "status": "pending", "sent_at": None, **overrides}
    conn.execute(
        text(
            "INSERT INTO audit_outbox (event_uuid, event_type, payload, status, sent_at) "
            "VALUES (CAST(:u AS uuid), 'collection.membership.change', '{}'::jsonb, :status, "
            ":sent_at)"
        ),
        values,
    )


# --- no database -----------------------------------------------------------------------------


def test_revision_chain_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_PATH.parents[1]))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v458_speaker_machine_identity_index"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_the_model_and_the_revision_agree_on_the_statuses():
    from app.models.audit_outbox import STATUSES

    module = _revision_module()
    assert tuple(module.STATUSES) == STATUSES
    literal = ", ".join(f"'{status}'" for status in STATUSES)
    assert f"CHECK (status IN ({literal}))" in module.UPGRADE_SQL


def test_the_outbox_is_classified_as_an_exempt_audit_shaped_table():
    from app.db.audit_protection import EXEMPT_TABLES

    assert EXEMPT_TABLES["audit_outbox"].strip()


# --- the migrated suite database -------------------------------------------------------------


def test_detection_arm_returns_v459_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_the_live_table_matches_the_model(db_session):
    from app.models.audit_outbox import AuditOutbox

    conn = db_session.connection()
    live = {c["name"] for c in inspect(conn).get_columns("audit_outbox")}
    assert live == set(AuditOutbox.__table__.columns.keys())
    names = {i["name"] for i in inspect(conn).get_indexes("audit_outbox")}
    assert {"uq_audit_outbox_event_uuid", "ix_audit_outbox_due", "ix_audit_outbox_sent_at"} <= names


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "lost"},
        {"status": "sent"},  # sent with no sent_at
        {"status": "pending", "sent_at": "2026-01-01T00:00:00Z"},
    ],
)
def test_the_checks_refuse_an_unknown_status_and_a_sent_mark_out_of_step(db_session, bad):
    conn = db_session.connection()
    with pytest.raises(IntegrityError), conn.begin_nested():
        _insert(conn, **bad)


def test_an_event_uuid_is_stored_once(db_session):
    conn = db_session.connection()
    event_uuid = str(uuid.uuid4())
    _insert(conn, u=event_uuid)
    with pytest.raises(IntegrityError), conn.begin_nested():
        _insert(conn, u=event_uuid)


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
                            "WHERE conrelid = 'audit_outbox'::regclass"
                        )
                    )
                }
                assert {"ck_audit_outbox_status", "ck_audit_outbox_sent_at"} <= constraints
                _insert(conn)
            with scratch.begin() as conn:
                conn.execute(text(module.DOWNGRADE_SQL))
                conn.execute(text(module.DOWNGRADE_SQL))
                assert "audit_outbox" not in inspect(conn).get_table_names()
