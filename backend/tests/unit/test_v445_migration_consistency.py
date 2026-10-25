"""v445 migration + detection-arm consistency (organization policy, issues #1280, #1279)."""

from __future__ import annotations

import importlib.util
import re
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

REVISION = "v445_add_organization_policy"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_LAST_CHECK = "ck_organization_policy_floor_custom_words_array"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_v445_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v444_add_takedown_notification"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


@pytest.fixture
def org_id(db_session):
    return db_session.execute(
        text(
            "INSERT INTO organization (uuid, name, is_active) VALUES (:u, 'o', true) RETURNING id"
        ),
        {"u": uuid_pkg.uuid4()},
    ).scalar()


def _insert(conn, org, **cols):
    names = ["organization_id", *cols]
    conn.execute(
        text(
            f"INSERT INTO organization_policy ({', '.join(names)}) "
            f"VALUES ({', '.join(':' + n for n in names)})"
        ),
        {"organization_id": org, **cols},
    )


def test_a_bare_row_inherits_everything(db_session, org_id):
    """A row created for any reason must not switch a feature off or add a floor."""
    conn = db_session.connection()
    _insert(conn, org_id)

    row = (
        conn.execute(
            text("SELECT * FROM organization_policy WHERE organization_id = :o"), {"o": org_id}
        )
        .mappings()
        .one()
    )

    assert row["voiceprint_matching_enabled"] is True
    assert row["gender_inference_enabled"] is True
    assert row["floor_forced_categories"] == []
    assert row["floor_forced_pii_entities"] == []
    assert row["floor_forced_custom_words"] == []
    assert row["floor_toxicity_threshold"] is None
    assert row["floor_export_redacted"] is False
    assert row["floor_redact_before_llm"] is False
    assert row["updated_at"] is not None
    assert row["updated_by_user_id"] is None


@pytest.mark.parametrize(
    "column,value",
    [
        ("floor_toxicity_threshold", 1.5),
        ("floor_toxicity_threshold", -0.1),
        ("floor_forced_categories", '{"a": 1}'),
        ("floor_forced_pii_entities", '"pii"'),
        ("floor_forced_custom_words", "3"),
    ],
)
def test_checks_reject_values_outside_the_contract(db_session, org_id, column, value):
    conn = db_session.connection()
    placeholder = ":v" if column == "floor_toxicity_threshold" else "CAST(:v AS jsonb)"
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(
                text(
                    f"INSERT INTO organization_policy (organization_id, {column}) "
                    f"VALUES (:o, {placeholder})"
                ),
                {"o": org_id, "v": value},
            )


@pytest.mark.parametrize("threshold", [0.0, 0.5, 1.0])
def test_the_toxicity_bounds_are_inclusive(db_session, org_id, threshold):
    conn = db_session.connection()
    _insert(conn, org_id, floor_toxicity_threshold=threshold)

    stored = conn.execute(
        text("SELECT floor_toxicity_threshold FROM organization_policy WHERE organization_id = :o"),
        {"o": org_id},
    ).scalar()
    assert stored == threshold


def test_one_row_per_organization(db_session, org_id):
    conn = db_session.connection()
    _insert(conn, org_id)
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            _insert(conn, org_id)


def test_deleting_the_organization_deletes_its_policy(db_session, org_id):
    conn = db_session.connection()
    _insert(conn, org_id)

    conn.execute(text("DELETE FROM organization WHERE id = :o"), {"o": org_id})

    assert conn.execute(text("SELECT count(*) FROM organization_policy")).scalar() == 0


def test_deleting_the_last_editor_keeps_the_policy(db_session, org_id, normal_user):
    conn = db_session.connection()
    _insert(conn, org_id, updated_by_user_id=normal_user.id, voiceprint_matching_enabled=False)

    conn.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": normal_user.id})

    row = conn.execute(
        text(
            "SELECT voiceprint_matching_enabled, updated_by_user_id FROM organization_policy "
            "WHERE organization_id = :o"
        ),
        {"o": org_id},
    ).one()
    assert row[0] is False and row[1] is None


@pytest.mark.ddl_exclusive
def test_upgrade_is_idempotent(db_session):
    conn = db_session.connection()
    names = "SELECT count(*) FROM pg_constraint WHERE conrelid = 'organization_policy'::regclass"
    before = conn.execute(text(names)).scalar()
    conn.execute(text(_module().UPGRADE_SQL))
    conn.execute(text(_module().UPGRADE_SQL))
    assert conn.execute(text(names)).scalar() == before


def test_downgrade_drops_only_this_revisions_table():
    assert re.findall(r"DROP TABLE IF EXISTS (\w+)", _module().DOWNGRADE_SQL) == [
        "organization_policy"
    ]


@pytest.mark.ddl_exclusive
def test_downgrade_then_upgrade_restores_the_same_shape(db_session):
    conn = db_session.connection()
    shape = (
        "SELECT (SELECT count(*) FROM pg_constraint "
        "WHERE conrelid = 'organization_policy'::regclass), "
        "(SELECT count(*) FROM information_schema.columns WHERE table_name = 'organization_policy')"
    )
    before = tuple(conn.execute(text(shape)).one())

    conn.execute(text(_module().DOWNGRADE_SQL))
    assert "organization_policy" not in inspect(conn).get_table_names()
    conn.execute(text(_module().UPGRADE_SQL))

    assert tuple(conn.execute(text(shape)).one()) == before


def test_detection_arm_returns_v445_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_last_check(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE organization_policy DROP CONSTRAINT {_LAST_CHECK}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
