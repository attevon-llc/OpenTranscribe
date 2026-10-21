"""v441 migration + detection-arm consistency (legal_acceptance)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v441_add_legal_acceptance"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_v441_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v440_add_password_last_self_changed_at"
    assert len(set(scripts.get_heads())) == 1


def test_table_shape(db_session):
    insp = inspect(db_session.connection())
    cols = {c["name"]: c for c in insp.get_columns("legal_acceptance")}
    assert set(cols) == {
        "id",
        "uuid",
        "user_id",
        "document_key",
        "version",
        "sha256",
        "accepted_at",
        "ip_address",
    }
    assert cols["ip_address"]["nullable"] is True
    assert cols["accepted_at"]["type"].timezone is True
    uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints("legal_acceptance")}
    assert ("user_id", "document_key", "sha256") in uniques
    fk = insp.get_foreign_keys("legal_acceptance")[0]
    assert fk["referred_table"] == "user" and fk["options"].get("ondelete") == "CASCADE"


@pytest.mark.ddl_exclusive
def test_upgrade_is_idempotent_and_downgrade_drops_the_table(db_session):
    module = _module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        assert inspect(conn).has_table("legal_acceptance")
        conn.execute(text(module.DOWNGRADE_SQL))
        assert not inspect(conn).has_table("legal_acceptance")
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        assert inspect(conn).has_table("legal_acceptance")
    finally:
        db_session.rollback()


def test_detection_arm_returns_v441_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_table(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP TABLE legal_acceptance"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
