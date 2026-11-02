"""v456 (unique object keys, issue #1357): the revision run against populated tables.

Each scenario drops the three indexes inside a savepoint, seeds rows, runs the REAL ``upgrade()``
through an Alembic operations context, and rolls the savepoint back so the live schema is
untouched. The revision must never raise: duplicate thumbnails are resolved, duplicate originals
leave their index unbuilt and reported.
"""

from __future__ import annotations

import importlib.util
import logging
import uuid as uuid_pkg
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Table
from sqlalchemy import text

from app.db import object_key_indexes as oki
from app.models.media import MediaFile
from tests.unit._migration_detection import assert_detected_at_or_after

REVISION = "v456_unique_object_keys"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

pytestmark = pytest.mark.ddl_exclusive


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_parent_resolves_and_the_graph_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_PATH.parents[1]))
    scripts = ScriptDirectory.from_config(config)
    parent = scripts.get_revision(REVISION).down_revision
    assert isinstance(parent, str) and scripts.get_revision(parent) is not None
    assert len(set(scripts.get_heads())) == 1


def test_detection_arm_keys_on_the_playback_index_and_never_stamps_lower(db_session):
    from app.db import migrations

    assert "indexname = 'uq_media_file_playback_path'" in Path(migrations.__file__).read_text()
    assert oki.INDEXES[-1].name == "uq_media_file_playback_path"
    conn = db_session.connection()
    tables = [r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables"))]
    assert_detected_at_or_after(conn, tables, REVISION)


def test_the_model_declares_the_same_indexes_the_database_has(db_session):
    table = MediaFile.__table__
    assert isinstance(table, Table)
    declared = {i.name for i in table.indexes if i.unique and str(i.name).startswith("uq_")}
    assert {i.name for i in oki.INDEXES} <= declared
    assert {i.name for i in oki.INDEXES} == oki.existing_indexes(db_session.connection())


@pytest.fixture
def bare(db_session) -> Iterator[object]:
    """The three indexes dropped inside a savepoint that is rolled back afterwards."""
    nested = db_session.begin_nested()
    conn = db_session.connection()
    for index in oki.INDEXES:
        conn.execute(text(f"DROP INDEX IF EXISTS {index.name}"))
    try:
        yield conn
    finally:
        nested.rollback()


def _rows(db_session, owner, n: int, **fields) -> list[MediaFile]:
    out = []
    for _ in range(n):
        file_uuid = uuid_pkg.uuid4()
        row = MediaFile(
            uuid=file_uuid,
            user_id=owner.id,
            filename=f"v456_{file_uuid.hex[:8]}.mp4",
            storage_path=f"user_{owner.id}/v456_{file_uuid}/original.mp4",
            content_type="video/mp4",
            file_size=1,
            status="completed",
        )
        for k, v in fields.items():
            setattr(row, k, v)
        db_session.add(row)
        out.append(row)
    db_session.flush()
    return out


def _run_upgrade(conn) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        _module().upgrade()


def _run_downgrade(conn) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        _module().downgrade()


def test_a_clean_table_gets_all_three_indexes(db_session, normal_user, bare):
    a, b = _rows(db_session, normal_user, 2, thumbnail_path=None, playback_path="")
    b.storage_path = ""  # the placeholder an upload row carries; repeats are allowed

    _run_upgrade(bare)

    assert oki.existing_indexes(bare) == {i.name for i in oki.INDEXES}
    assert oki.status(bare)["healthy"] is True


def test_shared_legacy_thumbnails_keep_the_lowest_id_and_the_index_is_built(
    db_session, normal_user, bare
):
    shared = f"user_{normal_user.id}/youtube_x{uuid_pkg.uuid4().hex[:6]}/thumbnail.jpg"
    first, second, third = _rows(db_session, normal_user, 3, thumbnail_path=shared)
    playback = f"user_{normal_user.id}/pb/{uuid_pkg.uuid4().hex}.m4a"
    p1, p2 = _rows(db_session, normal_user, 2, playback_path=playback)

    _run_upgrade(bare)

    rows = {
        r[0]: (r[1], r[2])
        for r in bare.execute(
            text("SELECT id, thumbnail_path, playback_path FROM media_file WHERE id = ANY(:ids)"),
            {"ids": [first.id, second.id, third.id, p1.id, p2.id]},
        )
    }
    assert rows[first.id][0] == shared
    assert rows[second.id][0] is None and rows[third.id][0] is None
    assert rows[p1.id][1] == playback and rows[p2.id][1] is None
    assert oki.existing_indexes(bare) == {i.name for i in oki.INDEXES}


def test_shared_originals_never_block_the_upgrade_and_are_reported(
    db_session, normal_user, bare, caplog
):
    key = f"user_{normal_user.id}/dup/{uuid_pkg.uuid4().hex}.mp4"
    first, second = _rows(db_session, normal_user, 2, storage_path=key)

    with caplog.at_level(logging.ERROR):
        _run_upgrade(bare)  # must not raise

    assert "uq_media_file_storage_path" not in oki.existing_indexes(bare)
    assert {"uq_media_file_thumbnail_path", "uq_media_file_playback_path"} <= (
        oki.existing_indexes(bare)
    )
    assert any("uq_media_file_storage_path" in r.getMessage() for r in caplog.records)
    report = oki.status(bare)
    assert report["healthy"] is False
    assert report["storage_path"] == {
        "index_present": False,
        "duplicate_keys": 1,
        "duplicate_rows": 2,
    }
    rows = bare.execute(
        text("SELECT storage_path FROM media_file WHERE id = ANY(:ids)"),
        {"ids": [first.id, second.id]},
    ).all()
    assert [r[0] for r in rows] == [key, key], "original keys are never rewritten by the revision"

    # Once the rows are separated, the next start builds the index.
    bare.execute(
        text("UPDATE media_file SET storage_path = :p WHERE id = :i"),
        {"p": key + ".copy", "i": second.id},
    )
    outcome = oki.ensure_indexes(bare)
    assert outcome["uq_media_file_storage_path"] == "built"
    assert oki.status(bare)["healthy"] is True


def test_the_downgrade_drops_the_indexes_and_loses_no_rows(db_session, normal_user, bare):
    rows = _rows(db_session, normal_user, 2)
    _run_upgrade(bare)
    assert oki.existing_indexes(bare)

    _run_downgrade(bare)

    assert oki.existing_indexes(bare) == set()
    count = bare.execute(
        text("SELECT count(*) FROM media_file WHERE id = ANY(:ids)"),
        {"ids": [r.id for r in rows]},
    ).scalar_one()
    assert count == 2
    _run_upgrade(bare)  # idempotent over its own output
    assert oki.existing_indexes(bare) == {i.name for i in oki.INDEXES}
