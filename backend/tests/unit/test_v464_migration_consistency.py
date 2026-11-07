"""v464 migration + detection-arm consistency (one open scheduled erasure per subject, #1105).

The idempotency and downgrade checks run the revision's real SQL in a PRIVATE scratch database
(precedent: ``test_v461_migration_consistency.py``), so no DDL touches the shared suite database;
each takes the suite DDL lock EXCLUSIVE, which is what ``test_ddl_marker_discipline`` looks for in
a test that runs DDL without ``db_session``.
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import InternalError

from app.core.config import settings
from app.db.base import engine as shared_engine
from tests.db_locks import acquire_ddl_lock_exclusive

REVISION = "v464_generalise_erasure_grace_index"
NEW_INDEX = "uq_erasure_ledger_open_grace_subject"
OLD_INDEX = "uq_erasure_ledger_open_self_request"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

#: The v455-shaped part of erasure_ledger the revision touches, for the scratch database.
_LEDGER_SQL = """
CREATE TABLE erasure_ledger (
    id serial PRIMARY KEY,
    subject_type varchar(20) NOT NULL,
    subject_user_id integer,
    subject_organization_id integer,
    status varchar(20) NOT NULL,
    deferred_reason varchar(20)
);
CREATE UNIQUE INDEX uq_erasure_ledger_open_self_request ON erasure_ledger (subject_user_id)
    WHERE deferred_reason = 'grace_period' AND status = 'deferred';
"""


def _revision_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@contextmanager
def _scratch_database() -> Iterator[Engine]:
    """An empty database of our own on the test server."""
    name = f"ot_v464_{uuid.uuid4().hex[:10]}_test"
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


def _open(conn, subject_type: str, user_id: int | None, org_id: int | None) -> None:
    conn.execute(
        text(
            "INSERT INTO erasure_ledger (subject_type, subject_user_id, subject_organization_id, "
            "status, deferred_reason) VALUES (:t, :u, :o, 'deferred', 'grace_period')"
        ),
        {"t": subject_type, "u": user_id, "o": org_id},
    )


def _indexes(conn) -> set[str]:
    return {i["name"] for i in inspect(conn).get_indexes("erasure_ledger")}


# --- no database -----------------------------------------------------------------------------


def test_revision_chain_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_PATH.parents[1]))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v463_add_media_file_trash"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


# --- the migrated suite database -------------------------------------------------------------


def test_detection_arm_returns_v464_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_the_live_ledger_carries_the_new_index_and_not_the_old_one(db_session):
    names = _indexes(db_session.connection())
    assert NEW_INDEX in names
    assert OLD_INDEX not in names


def _ledger_row(db_session, **subject):
    """The open grace-period entry, or None when the database refused it."""
    from app.services import erasure_ledger_service as ledger

    now = datetime(2026, 11, 1, tzinfo=UTC)
    return ledger.record_request(
        db_session,
        status="deferred",
        deferred_reason="grace_period",
        scheduled_for=now + timedelta(days=3),
        requested_at=now,
        **subject,
    )


def test_a_member_erasure_and_the_same_persons_own_request_can_both_be_open(
    db_session, normal_user, tmp_path, monkeypatch
):
    """v455's index was unique on the user alone, so these two collided."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    from app.models.organization import Organization

    orgs = [Organization(external_org_id=f"org_v464_{i}", name=f"v464 {i}") for i in range(2)]
    db_session.add_all(orgs)
    db_session.commit()

    own = _ledger_row(
        db_session,
        subject_type="user",
        subject_user_id=normal_user.id,
        actor_kind="data_subject",
    )
    members = [
        _ledger_row(
            db_session,
            subject_type="org_member",
            subject_user_id=normal_user.id,
            subject_organization_id=org.id,
            actor_kind="org_admin",
        )
        for org in orgs
    ]

    assert own is not None
    assert all(m is not None for m in members), "the ledger refused a member erasure"
    assert len({own.uuid, *(m.uuid for m in members)}) == 3


# --- private scratch database: idempotent upgrade, uniqueness, downgrade ----------------------


@pytest.mark.parametrize(
    ("first", "second", "collides"),
    [
        (("user", 1, None), ("user", 1, None), True),
        (("user", 1, None), ("org_member", 1, 7), False),
        (("org_member", 1, 7), ("org_member", 1, 8), False),
        (("org_member", 1, 7), ("org_member", 1, 7), True),
        (("organization", None, 7), ("organization", None, 7), True),
        (("organization", None, 7), ("org_member", 1, 7), False),
    ],
)
def test_one_open_request_per_subject(first, second, collides):
    module = _revision_module()
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            conn.execute(text(_LEDGER_SQL))
            conn.execute(text(module.UPGRADE_SQL))
            _open(conn, *first)
            if collides:
                with pytest.raises(IntegrityError, match=NEW_INDEX), conn.begin_nested():
                    _open(conn, *second)
            else:
                _open(conn, *second)


def test_the_upgrade_is_idempotent_and_the_downgrade_restores_the_v455_index():
    module = _revision_module()
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch:
            with scratch.begin() as conn:
                conn.execute(text(_LEDGER_SQL))
                conn.execute(text(module.UPGRADE_SQL))
                conn.execute(text(module.UPGRADE_SQL))
                assert NEW_INDEX in _indexes(conn)
                assert OLD_INDEX not in _indexes(conn)
                _open(conn, "user", 1, None)
            with scratch.begin() as conn:
                conn.execute(text(module.DOWNGRADE_SQL))
                conn.execute(text(module.DOWNGRADE_SQL))
                assert OLD_INDEX in _indexes(conn)
                assert NEW_INDEX not in _indexes(conn)


def test_the_downgrade_refuses_while_a_member_erasure_is_open():
    module = _revision_module()
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch:
            with scratch.begin() as conn:
                conn.execute(text(_LEDGER_SQL))
                conn.execute(text(module.UPGRADE_SQL))
                _open(conn, "org_member", 1, 7)
            with (
                pytest.raises(InternalError, match="Cannot downgrade v464"),
                scratch.begin() as conn,
            ):
                conn.execute(text(module.DOWNGRADE_SQL))
