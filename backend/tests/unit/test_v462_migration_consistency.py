"""v462 migration + detection-arm consistency (the speaker identity history, issue #1336).

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
from pathlib import Path
from types import ModuleType

from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db.base import engine as shared_engine
from tests.db_locks import acquire_ddl_lock_exclusive

REVISION = "v462_add_speaker_identity_event"
TABLE = "speaker_identity_event"
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
_PATH = _VERSIONS / f"{REVISION}.py"

#: The three parents the revision references, reduced to their keys, for the scratch database.
_PARENTS_SQL = """
CREATE TABLE "user" (id SERIAL PRIMARY KEY);
CREATE TABLE organization (id SERIAL PRIMARY KEY);
CREATE TABLE speaker (id SERIAL PRIMARY KEY);
"""


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _scratch_database() -> Iterator[Engine]:
    """An empty database of our own on the test server."""
    name = f"ot_v462_{uuid.uuid4().hex[:10]}_test"
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


# --- no database -----------------------------------------------------------------------------


def test_revision_chain_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_VERSIONS.parent))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v461_add_privacy_purge_pending"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_the_frozen_kinds_equal_the_models_and_the_check():
    from app.models.speaker_identity_event import EVENT_KINDS

    module = _load(REVISION)
    assert tuple(module.KINDS) == EVENT_KINDS
    check = re.search(r"CHECK \(kind IN \(([^)]*)\)\)", module.TABLE_SQL)
    assert check is not None
    assert tuple(re.findall(r"'(\w+)'", check.group(1))) == EVENT_KINDS


def test_the_purge_switch_is_the_one_the_purge_sets():
    from app.models.speaker_identity_event import PURGE_SWITCH

    module = _load(REVISION)
    assert module.PURGE_SWITCH == PURGE_SWITCH
    assert f"current_setting('{PURGE_SWITCH}', true)" in module.GUARD_SQL


# --- the migrated suite database -------------------------------------------------------------


def test_detection_arm_returns_v462_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_the_live_table_matches_the_model(db_session):
    from app.models.speaker_identity_event import SpeakerIdentityEvent

    conn = db_session.connection()
    insp = inspect(conn)
    assert {c["name"] for c in insp.get_columns(TABLE)} == set(
        SpeakerIdentityEvent.__table__.columns.keys()
    )
    assert {i["name"] for i in insp.get_indexes(TABLE)} >= {
        "ix_speaker_identity_event_speaker_created",
        "ix_speaker_identity_event_organization_id",
        "ix_speaker_identity_event_actor_user_id",
    }
    rules = {
        fk["constrained_columns"][0]: fk["options"].get("ondelete")
        for fk in insp.get_foreign_keys(TABLE)
    }
    assert rules == {
        "speaker_id": "CASCADE",
        "organization_id": "CASCADE",
        "actor_user_id": "SET NULL",
    }


# --- private scratch database: idempotent upgrade, downgrade ---------------------------------


def test_the_upgrade_is_idempotent_and_the_downgrade_removes_the_table():
    module = _load(REVISION)
    guard_functions = _load("v453_audit_row_immutability").FUNCTIONS_SQL
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch:
            with scratch.begin() as conn:
                conn.execute(text(_PARENTS_SQL + guard_functions))
                for _ in range(2):
                    conn.execute(text(module.TABLE_SQL))
                    conn.execute(text(module.GUARD_SQL))
                constraints = {
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'speaker_identity_event'::regclass"
                        )
                    )
                }
                assert {
                    "ck_speaker_identity_event_kind",
                    "ck_speaker_identity_event_actor",
                    "ck_speaker_identity_event_json",
                } <= constraints
                triggers = set(
                    conn.execute(
                        text(
                            "SELECT tgname FROM pg_trigger "
                            "WHERE tgrelid = 'speaker_identity_event'::regclass "
                            "AND NOT tgisinternal"
                        )
                    ).scalars()
                )
                assert triggers == {
                    "trg_speaker_identity_event_guard",
                    "trg_audit_guard_truncate_speaker_identity_event",
                }
            with scratch.begin() as conn:
                conn.execute(text(module.DOWNGRADE_SQL))
                conn.execute(text(module.DOWNGRADE_SQL))
                assert TABLE not in inspect(conn).get_table_names()
                assert not conn.execute(
                    text("SELECT to_regprocedure('speaker_identity_event_guard()')")
                ).scalar()
