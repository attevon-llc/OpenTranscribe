"""v467 migration + detection-arm consistency (who placed a content hold: organization or platform).

The idempotency and downgrade checks run the revision's real SQL in a PRIVATE scratch database
(precedent: ``test_v465_migration_consistency.py``), on top of v447's real ``content_hold`` DDL,
so no DDL touches the shared suite database; each takes the suite DDL lock EXCLUSIVE, which is
what ``test_ddl_marker_discipline`` looks for in a test that runs DDL without ``db_session``.
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
from sqlalchemy.exc import InternalError

from app.core.config import settings
from app.db.base import engine as shared_engine
from tests.db_locks import acquire_ddl_lock_exclusive

REVISION = "v467_add_content_hold_authority"
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
_PATH = _VERSIONS / f"{REVISION}.py"

#: The parents v447's ``content_hold`` references, reduced to their keys, for the scratch database.
_PARENTS_SQL = """
CREATE TABLE organization (id serial PRIMARY KEY);
CREATE TABLE collection (id serial PRIMARY KEY);
CREATE TABLE "user" (id serial PRIMARY KEY);
CREATE TABLE media_file (id serial PRIMARY KEY);
INSERT INTO organization (id) VALUES (1);
"""

_REASON = "Litigation matter 2026-14"


def _module(revision: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(revision, _VERSIONS / f"{revision}.py")
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@contextmanager
def _scratch_database() -> Iterator[Engine]:
    """An empty database of our own on the test server."""
    name = f"ot_v467_{uuid.uuid4().hex[:10]}_test"
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


def _hold(conn, *, org_id: int | None, authority: str | None = None) -> None:
    columns = "uuid, organization_id, scope, reason" + (", authority" if authority else "")
    values = ":u, :o, 'file', :r" + (", :a" if authority else "")
    conn.execute(
        text(f"INSERT INTO content_hold ({columns}) VALUES ({values})"),
        {"u": str(uuid.uuid4()), "o": org_id, "r": _REASON, "a": authority},
    )


def _v447_table(conn) -> None:
    conn.execute(text(_PARENTS_SQL))
    conn.execute(text(_module("v447_add_content_hold").UPGRADE_SQL))


def _org_id_nullable(conn) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT is_nullable = 'YES' FROM information_schema.columns "
                "WHERE table_name = 'content_hold' AND column_name = 'organization_id'"
            )
        ).scalar()
    )


# --- no database -----------------------------------------------------------------------------


def test_revision_chain_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_VERSIONS.parent))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v465_add_user_hidden_media"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_the_revision_and_the_model_agree_on_the_authorities():
    from app.models.content_hold import HOLD_AUTHORITIES

    assert _module(REVISION).AUTHORITIES == HOLD_AUTHORITIES
    assert all(f"'{a}'" in _module(REVISION).UPGRADE_SQL for a in HOLD_AUTHORITIES)


def test_the_revision_builds_no_index_itself():
    """The platform index is built CONCURRENTLY by the runner after the upgrade, never in it."""
    assert "CREATE INDEX" not in _module(REVISION).UPGRADE_SQL.upper()


def test_the_spec_is_the_partial_concurrent_index_the_model_declares():
    from app.db.base import Base
    from app.db.hold_indexes import PLATFORM_ACTIVE
    from app.models.content_hold import ContentHold

    assert PLATFORM_ACTIVE.create_sql() == (
        "CREATE INDEX CONCURRENTLY ix_content_hold_platform_active ON content_hold (id) "
        "WHERE authority = 'platform' AND released_at IS NULL"
    )
    names = {i.name for i in Base.metadata.tables[ContentHold.__tablename__].indexes}
    assert PLATFORM_ACTIVE.name in names


# --- the migrated suite database -------------------------------------------------------------


def test_detection_arm_returns_v467_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_the_orm_mirrors_the_columns_and_organization_is_optional(db_session):
    from app.db.base import Base

    conn = db_session.connection()
    live = {c["name"]: c for c in inspect(conn).get_columns("content_hold")}
    assert set(live) == set(Base.metadata.tables["content_hold"].columns.keys())
    assert live["organization_id"]["nullable"] is True
    assert live["authority"]["nullable"] is False


# --- private scratch database ----------------------------------------------------------------


def test_existing_holds_become_organization_holds_and_the_upgrade_is_idempotent():
    module = _module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            _v447_table(conn)
            _hold(conn, org_id=1)
            conn.execute(text(module.UPGRADE_SQL))
            conn.execute(text(module.UPGRADE_SQL))
            assert conn.execute(text("SELECT authority FROM content_hold")).scalars().all() == [
                "organization"
            ]
            assert _org_id_nullable(conn)


@pytest.mark.parametrize(
    ("org_id", "authority", "constraint"),
    [
        (None, "organization", "ck_content_hold_authority_org"),
        (1, "platform", "ck_content_hold_authority_org"),
        (None, None, "ck_content_hold_authority_org"),  # the default is 'organization'
        (None, "operator", "ck_content_hold_authority"),
    ],
)
def test_an_authority_must_match_whether_an_organization_is_named(org_id, authority, constraint):
    module = _module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            _v447_table(conn)
            conn.execute(text(module.UPGRADE_SQL))
            _hold(conn, org_id=None, authority="platform")  # the control: this one is legal
            _hold(conn, org_id=1, authority="organization")
            with pytest.raises(IntegrityError, match=constraint), conn.begin_nested():
                _hold(conn, org_id=org_id, authority=authority)


def test_the_downgrade_refuses_while_a_platform_hold_exists_and_restores_not_null_after():
    module = _module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch:
            with scratch.begin() as conn:
                _v447_table(conn)
                conn.execute(text(module.UPGRADE_SQL))
                _hold(conn, org_id=1, authority="organization")
                _hold(conn, org_id=None, authority="platform")
                _hold(conn, org_id=None, authority="platform")
            with pytest.raises(InternalError, match="2 platform legal hold row"):
                with scratch.begin() as conn:
                    conn.execute(text(module.DOWNGRADE_SQL))
            with scratch.begin() as conn:
                assert "authority" in {c["name"] for c in inspect(conn).get_columns("content_hold")}
                conn.execute(text("DELETE FROM content_hold WHERE authority = 'platform'"))
            with scratch.begin() as conn:
                conn.execute(text(module.DOWNGRADE_SQL))
                conn.execute(text(module.DOWNGRADE_SQL))
                columns = {c["name"] for c in inspect(conn).get_columns("content_hold")}
                assert "authority" not in columns
                assert not _org_id_nullable(conn)
                assert conn.execute(text("SELECT count(*) FROM content_hold")).scalar() == 1
