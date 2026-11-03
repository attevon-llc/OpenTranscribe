"""v455 migration and detection arm (self-service erasure request, issues #1354/#1355).

Executes the revision's SQL instead of grepping its source: the upgrade twice (the startup runner
re-runs a revision over its own partial output) and the downgrade's refusal while a request is open.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Table
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

REVISION = "v455_add_self_service_erasure_request"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chains_after_v452_and_the_graph_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    # The exact parent moves as revisions are inserted before this one; pin that it resolves.
    parent = scripts.get_revision(REVISION).down_revision
    assert isinstance(parent, str) and scripts.get_revision(parent) is not None
    assert len(set(scripts.get_heads())) == 1


def test_detection_arm_keys_on_the_last_object_created():
    from app.db import migrations

    source = Path(migrations.__file__).read_text()
    assert "indexname = 'uq_erasure_ledger_open_self_request'" in source
    assert f'return "{REVISION}"' in source
    sql = _module().UPGRADE_SQL
    assert sql.rindex("uq_erasure_ledger_open_self_request") > sql.rindex(
        "ix_erasure_ledger_due_grace"
    )


def test_the_model_checks_and_indexes_exist_in_the_database(db_session):
    from app.models.erasure import ErasureLedgerEntry

    conn = db_session.connection()
    checks = {
        r[0]
        for r in conn.execute(
            text("SELECT conname FROM pg_constraint WHERE conrelid = 'erasure_ledger'::regclass")
        )
    }
    indexes = {
        r[0]
        for r in conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'erasure_ledger'")
        )
    }
    table = cast(Table, ErasureLedgerEntry.__table__)
    declared = {c.name for c in table.constraints if c.name}
    assert {str(n) for n in declared if str(n).startswith("ck_")} <= checks
    assert {"uq_erasure_ledger_open_self_request", "ix_erasure_ledger_due_grace"} <= indexes


@pytest.mark.ddl_exclusive
def test_the_upgrade_is_idempotent(db_session):
    conn = db_session.connection()
    count = text("SELECT count(*) FROM pg_constraint WHERE conrelid = 'erasure_ledger'::regclass")
    before = conn.execute(count).scalar_one()
    conn.execute(text(_module().UPGRADE_SQL))
    conn.execute(text(_module().UPGRADE_SQL))
    assert conn.execute(count).scalar_one() == before


@pytest.mark.ddl_exclusive
def test_the_downgrade_refuses_while_a_request_is_open(db_session, normal_user):
    from datetime import UTC
    from datetime import datetime

    from app.models.erasure import ErasureLedgerEntry

    now = datetime.now(UTC)
    db_session.add(
        ErasureLedgerEntry(
            subject_type="user",
            subject_user_id=normal_user.id,
            subject_user_uuid=normal_user.uuid,
            status="deferred",
            deferred_reason="grace_period",
            actor_kind="data_subject",
            requested_at=now,
            sla_due_at=now,
            scheduled_for=now,
            counters={},
        )
    )
    db_session.commit()
    nested = db_session.begin_nested()
    with pytest.raises(DBAPIError, match="self-service erasure requests are still open"):
        db_session.connection().execute(text(_module().DOWNGRADE_SQL))
    nested.rollback()
