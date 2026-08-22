"""v397 migration + detection-arm consistency (document tenancy backfill).

The alembic chain must contain v397 (revises v396), and the untracked-DB detection in
``app/db/migrations.py`` must recognise a v397-shape schema.

v397 adds **no DDL** — it is a pure data migration, same shape as
``v379_rename_keycloak_config_to_oidc`` — so its detection arm cannot probe for a
column. The fingerprint is instead an explicit ``system_settings`` completion marker
row, inserted by the same idempotent SQL that runs the backfill.

The substantive test is :func:`test_the_backfill_stamps_only_null_organization_id_from_
the_watch_source`: it seeds a document imported through a watch source whose
``organization_id`` was never stamped (simulating a pre-fix row), runs the revision's
UPDATE, and asserts the org is picked up — while a document that already carries an
``organization_id`` (even a DIFFERENT one from its source) and a manually-uploaded
document with no watch-source link at all are both left untouched.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v397_backfill_document_tenancy_and_hash"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

MARKER_KEY = "documents.tenancy_backfill_v397"


def _revision_module():
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_user(conn) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"v397_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_org(conn) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO organization (uuid, name) VALUES (gen_random_uuid(), :n) RETURNING id"
            ),
            {"n": f"v397-org-{uuid_pkg.uuid4().hex[:8]}"},
        ).scalar()
    )


def _new_watch_source(conn, user_id: int, organization_id: int | None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO watch_source (uuid, name, source_type, user_id, organization_id) "
                "VALUES (gen_random_uuid(), :n, 'local', :u, :o) RETURNING id"
            ),
            {"n": f"v397-source-{uuid_pkg.uuid4().hex[:8]}", "u": user_id, "o": organization_id},
        ).scalar()
    )


def _new_document(conn, user_id: int, organization_id: int | None = None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO document (uuid, user_id, organization_id, filename, storage_path, "
                "file_size, content_type) VALUES (gen_random_uuid(), :uid, :org, 'v397.pdf', "
                "'x/v397.pdf', 1, 'application/pdf') RETURNING id"
            ),
            {"uid": user_id, "org": organization_id},
        ).scalar()
    )


def _link_watch_source_file(conn, watch_source_id: int, document_id: int) -> None:
    conn.execute(
        text(
            "INSERT INTO watch_source_file (uuid, watch_source_id, remote_path, filename, "
            "document_id) VALUES (gen_random_uuid(), :ws, :p, 'v397.pdf', :doc)"
        ),
        {"ws": watch_source_id, "p": f"/v397/{uuid_pkg.uuid4().hex[:8]}.pdf", "doc": document_id},
    )


def test_v397_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v396_add_document_chunk_redaction_cache"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v397_migration_is_vendor_neutral():
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_backfill_stamps_only_null_organization_id_from_the_watch_source(db_session):
    """The three cases that must be told apart, all in one seed."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        org_id = _new_org(conn)
        other_org_id = _new_org(conn)
        source = _new_watch_source(conn, user_id, org_id)

        # Case 1: watch-sourced, org never stamped (the pre-fix row) -> backfilled.
        needs_backfill = _new_document(conn, user_id, organization_id=None)
        _link_watch_source_file(conn, source, needs_backfill)

        # Case 2: watch-sourced, already carries a DIFFERENT org -> left alone. A
        # backfill that overwrote an existing value would be indistinguishable from
        # a tenant-scope bug of its own.
        already_scoped = _new_document(conn, user_id, organization_id=other_org_id)
        _link_watch_source_file(conn, source, already_scoped)

        # Case 3: no watch-source link at all (a manual upload) -> stays NULL. There
        # is no recoverable signal for these; see the revision's own docstring for
        # why guessing from current org membership is deliberately not done here.
        manual_upload = _new_document(conn, user_id, organization_id=None)

        conn.execute(text(module.UPGRADE_SQL))

        rows = dict(
            conn.execute(
                text("SELECT id, organization_id FROM document WHERE id IN (:a, :b, :c)"),
                {"a": needs_backfill, "b": already_scoped, "c": manual_upload},
            ).all()
        )
        assert rows[needs_backfill] == org_id
        assert rows[already_scoped] == other_org_id
        assert rows[manual_upload] is None
    finally:
        db_session.rollback()


def test_rerunning_the_upgrade_is_a_no_op(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        org_id = _new_org(conn)
        source = _new_watch_source(conn, user_id, org_id)
        document_id = _new_document(conn, user_id, organization_id=None)
        _link_watch_source_file(conn, source, document_id)

        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))

        stamped = conn.execute(
            text("SELECT organization_id FROM document WHERE id = :d"), {"d": document_id}
        ).scalar()
        assert stamped == org_id

        marker_count = conn.execute(
            text("SELECT count(*) FROM system_settings WHERE key = :k"), {"k": MARKER_KEY}
        ).scalar()
        assert marker_count == 1, "ON CONFLICT DO NOTHING must not duplicate the marker row"
    finally:
        db_session.rollback()


def test_the_marker_row_is_inserted(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text("DELETE FROM system_settings WHERE key = :k"), {"k": MARKER_KEY})
        conn.execute(text(module.UPGRADE_SQL))
        row = conn.execute(
            text("SELECT value FROM system_settings WHERE key = :k"), {"k": MARKER_KEY}
        ).first()
        assert row is not None
        assert row[0] == "true"
    finally:
        db_session.rollback()


def test_detection_arm_returns_v397_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_detection_stamps_lower_without_the_marker(db_session):
    """Remove the marker and the ladder must stop matching v397.

    No ``ddl_exclusive`` needed — unlike the DDL revisions' downgrade tests, this
    deletes one ``system_settings`` DML row inside the savepoint-isolated
    ``db_session`` (matching ``v379``'s equivalent test, which uses a plain INSERT
    for the same reason: no ``ALTER TABLE``/``DROP`` anywhere in this revision).
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DELETE FROM system_settings WHERE key = :k"), {"k": MARKER_KEY})
        tables = inspect(conn).get_table_names()
        detected = _detect_schema_version(conn, tables)
    finally:
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v396_add_document_chunk_redaction_cache")
        <= order.index(detected)
        < order.index(REVISION)
    )


def test_downgrade_removes_only_the_marker_not_the_backfilled_organization_id(db_session):
    """The additive-only downgrade: reversible plumbing, irreversible tenancy fact."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        org_id = _new_org(conn)
        source = _new_watch_source(conn, user_id, org_id)
        document_id = _new_document(conn, user_id, organization_id=None)
        _link_watch_source_file(conn, source, document_id)

        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))

        marker = conn.execute(
            text("SELECT count(*) FROM system_settings WHERE key = :k"), {"k": MARKER_KEY}
        ).scalar()
        assert marker == 0

        stamped = conn.execute(
            text("SELECT organization_id FROM document WHERE id = :d"), {"d": document_id}
        ).scalar()
        assert stamped == org_id, "downgrade must not undo the tenancy correction"

        # Upgrade again — the marker is re-inserted and the (already-correct, hence
        # no-op) backfill runs again cleanly.
        conn.execute(text(module.UPGRADE_SQL))
        marker = conn.execute(
            text("SELECT count(*) FROM system_settings WHERE key = :k"), {"k": MARKER_KEY}
        ).scalar()
        assert marker == 1
    finally:
        db_session.rollback()
