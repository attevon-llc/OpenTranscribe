"""v452 migration and detection arm (``erasure_receipt``, issue #1353).

Executes the revision's SQL (twice: the startup runner re-runs a revision over its own partial
output) instead of grepping its source. The downgrade DROPS compliance evidence, so like v389's
it is never executed here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Table
from sqlalchemy import text

REVISION = "v452_add_erasure_receipt"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain_has_one_head_and_chains_after_v451():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == (
        "v451_legal_acceptance_survives_user_deletion"
    )
    assert len(set(scripts.get_heads())) == 1


def test_the_downgrade_says_it_destroys_evidence():
    assert "compliance evidence" in (_module().__doc__ or "")


def test_the_revision_is_vendor_neutral():
    source = _PATH.read_text().lower()
    for noun in ("cl" + "erk", "str" + "ipe"):
        assert noun not in source


def test_every_check_the_model_declares_exists_in_the_database(db_session):
    from app.models.erasure import ErasureReceipt

    table = cast(Table, ErasureReceipt.__table__)
    declared = {c.name for c in table.constraints if c.name}
    present = {
        r[0]
        for r in db_session.connection().execute(
            text("SELECT conname FROM pg_constraint WHERE conrelid = 'erasure_receipt'::regclass")
        )
    }
    assert {str(n) for n in declared if str(n).startswith("ck_")} <= present


@pytest.mark.ddl_exclusive
def test_the_upgrade_is_idempotent(db_session):
    conn = db_session.connection()
    before = conn.execute(
        text("SELECT count(*) FROM pg_constraint WHERE conrelid = 'erasure_receipt'::regclass")
    ).scalar_one()
    conn.execute(text(_module().UPGRADE_SQL))
    conn.execute(text(_module().UPGRADE_SQL))
    after = conn.execute(
        text("SELECT count(*) FROM pg_constraint WHERE conrelid = 'erasure_receipt'::regclass")
    ).scalar_one()
    assert before == after > 0


def test_detection_arm_keys_on_the_last_check_created():
    module = _module()
    last = module._CHECKS[-1][0]
    assert last == "ck_erasure_receipt_digest"
    from app.db import migrations

    source = Path(migrations.__file__).read_text()
    assert f"conname = '{last}'" in source
    assert f'return "{REVISION}"' in source
