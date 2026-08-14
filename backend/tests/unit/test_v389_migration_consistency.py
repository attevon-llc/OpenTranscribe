"""v389 migration + detection-arm consistency (``erasure_ledger``, issue #442).

The GDPR Art. 17 erasure path recorded nothing durable, so a legal-hold deferral was
forgotten forever and a backup restore resurrected erased subjects with nothing to
reconcile against. This revision adds the table that fixes both.

Like ``v386``-``v388``, this suite **executes** the revision's SQL rather than grepping
its source, and executes it twice, because the startup runner stamps untracked databases
by schema fingerprint and therefore re-runs a revision over its own partial output. A
migration failure is ``SystemExit(1)``, so a non-idempotent revision does not degrade —
the backend refuses to start.

``ck_erasure_ledger_counters_numeric`` gets its own tests because it is the constraint
carrying the design guarantee rather than a shape: a ledger that can hold the personal
data it records the destruction of is not a ledger, and a CHECK that is present but
permissive looks identical to a correct one from the schema.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

#: ``ddl_exclusive`` is applied PER TEST, never to the module (issue #431) —
#: ``tests/unit/test_ddl_marker_discipline.py`` enforces both directions.

REVISION = "v389_add_erasure_ledger"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    """Load the revision by path — ``alembic/`` is not an importable package (see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert(conn, **overrides):
    """Insert one ledger row, returning its id. Raises on a CHECK violation."""
    params = {
        "u": str(uuid_pkg.uuid4()),
        "st": "user",
        "sui": 12345,
        "soi": None,
        "status": "pending",
        "ak": "system",
        "counters": "{}",
    }
    params.update(overrides)
    return conn.execute(
        text(
            "INSERT INTO erasure_ledger "
            "(uuid, subject_type, subject_user_id, subject_organization_id, status, "
            " actor_kind, sla_due_at, counters) "
            "VALUES (:u, :st, :sui, :soi, :status, :ak, now() + interval '30 days', "
            "        CAST(:counters AS jsonb)) RETURNING id"
        ),
        params,
    ).scalar()


def test_v389_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v388_add_user_group_organization_id"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # True while it is head, and still true once v390 revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v389_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_table_its_checks_and_its_indexes_all_exist(db_session):
    """All three kinds of object, not just the table.

    A partial hand-repair is the realistic failure: someone creates the table from the
    model and stops. Without the CHECKs the ledger accepts the personal data it exists
    not to retain; without ``ix_erasure_ledger_open`` the sweep sequentially scans a
    table where nearly every row is 'complete' and irrelevant to it.
    """
    conn = db_session.connection()
    assert "erasure_ledger" in inspect(conn).get_table_names()

    present = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'erasure_ledger'::regclass AND contype = 'c'"
            )
        )
    }
    assert {
        "ck_erasure_ledger_subject",
        "ck_erasure_ledger_status",
        "ck_erasure_ledger_actor_kind",
        "ck_erasure_ledger_deferred_reason",
        "ck_erasure_ledger_counters_numeric",
        "ck_erasure_ledger_subject_identified",
    } <= present

    indexes = {
        row[0]
        for row in conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'erasure_ledger'")
        )
    }
    assert "ix_erasure_ledger_open" in indexes
    assert "ix_erasure_ledger_subject_user_id" in indexes


def test_the_subject_columns_are_deliberately_not_foreign_keys(db_session):
    """``subject_user_id`` / ``subject_organization_id`` must NOT reference anything.

    They name the rows this table exists to record the destruction of. A real FK would
    either block the delete (``NO ACTION``) or null the column (``SET NULL``) — and
    nulling it destroys the only key the restore-detection sweep has. Referential
    integrity is exactly the property these columns must not have, which is precisely
    the kind of deliberate omission a later "cleanup" adds back.
    """
    referencing = {
        row[0]
        for row in db_session.connection().execute(
            text(
                "SELECT a.attname FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
                "WHERE c.conrelid = 'erasure_ledger'::regclass AND c.contype = 'f'"
            )
        )
    }
    assert "subject_user_id" not in referencing
    assert "subject_organization_id" not in referencing
    # The ACTOR is a different person and IS a foreign key, SET NULL like every actor
    # FK since v387 — so this test cannot pass by there being no FKs at all.
    assert referencing == {"actor_user_id"}

    rule = (
        db_session.connection()
        .execute(
            text(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conname = 'erasure_ledger_actor_user_id_fkey'"
            )
        )
        .scalar()
    )
    assert rule == "n", f"expected SET NULL ('n'), found {rule!r}"


def test_counters_accepts_numbers(db_session):
    """The control for the rejection test below — the CHECK must not reject everything."""
    conn = db_session.connection()
    try:
        assert _insert(conn, counters='{"media_files_deleted": 3, "voiceprints_deleted": 0}')
    finally:
        db_session.rollback()


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("an email address", '{"target_email": "alice@example.com"}'),
        ("a filename", '{"file": "board-meeting-2026.mp4"}'),
        ("a nested object", '{"errors": {"email": "alice@example.com"}}'),
        ("an array", '{"errors": ["alice@example.com"]}'),
        ("a boolean", '{"complete": true}'),
        ("a null", '{"subject": null}'),
    ],
)
def test_counters_rejects_anything_that_is_not_a_number(db_session, label, payload):
    """The guarantee, enforced by the DATABASE rather than by the calling code.

    "We erased alice@example.com" containing the address is a copy of the thing that was
    supposed to be destroyed, in a table designed to outlive it. ``counters`` is the only
    schemaless column, so it is the only place that could happen — and it cannot.
    """
    conn = db_session.connection()
    try:
        with pytest.raises(IntegrityError, match="ck_erasure_ledger_counters_numeric"):
            _insert(conn, counters=payload)
    finally:
        db_session.rollback()
    assert label  # the parametrise label is documentation, not an assertion


def test_an_org_member_entry_must_name_both_the_user_and_the_org(db_session):
    """``ck_erasure_ledger_subject_identified``: the sweep must be able to act on it.

    An org-member entry naming only the user is one the reconciliation sweep cannot
    re-run, because it does not know which tenant's rows to erase.
    """
    conn = db_session.connection()
    try:
        with pytest.raises(IntegrityError, match="ck_erasure_ledger_subject_identified"):
            _insert(conn, st="org_member", sui=1, soi=None)
    finally:
        db_session.rollback()

    try:
        assert _insert(conn, st="org_member", sui=1, soi=2)
    finally:
        db_session.rollback()


def test_detection_arm_returns_v389_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped.

    Skip the arm and an untracked database is mis-stamped to the PREVIOUS revision and
    never receives this DDL, so every erasure runs with no ledger and the reconciliation
    sweep queries a table that is not there.
    """
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_counters_check(db_session):
    """The arm keys on the CHECK, not merely on the table existing.

    A hand-made ``erasure_ledger`` without ``ck_erasure_ledger_counters_numeric`` is a
    table that CAN store the personal data the ledger exists not to retain, and
    re-running the revision is the right repair for it — so the detector must not treat
    it as already migrated. Dropping a CHECK (rather than the table) also keeps the
    ``ACCESS EXCLUSIVE`` lock on this one table instead of reaching ``user`` through the
    actor FK.

    Asserted as a *band* — at or after v388, strictly before v389 — because an exact
    ``==`` on a lower revision goes red or vacuous the next time the ladder above it
    changes, which had already happened to three suites in this family.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(
            text("ALTER TABLE erasure_ledger DROP CONSTRAINT ck_erasure_ledger_counters_numeric")
        )
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        # finally, not a trailing call: this mutates the SHARED dev schema, so anything
        # raising in between must still undo it.
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v388_add_user_group_organization_id")
        <= order.index(detected)
        < order.index(REVISION)
    )


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    """The invariant the startup runner depends on, executed rather than asserted about."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))

        assert "erasure_ledger" in inspect(conn).get_table_names()
        # Exactly one of each guarded object survives two runs — the guards are guards,
        # not duplicate-object generators.
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'ck_erasure_ledger_counters_numeric'"
                )
            ).scalar()
            == 1
        )
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'erasure_ledger_actor_user_id_fkey'"
                )
            ).scalar()
            == 1
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_erasure_ledger_open'")
            ).scalar()
            == 1
        )
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_drops_the_table_and_the_upgrade_restores_it(db_session):
    """The downgrade is executed here, not merely read.

    Before issue #431 no downgrade in this chain had ever been run by a test, so
    "``downgrade()`` mirrors ``upgrade()``" was a claim about source text.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways
        assert "erasure_ledger" not in inspect(conn).get_table_names()

        conn.execute(text(module.UPGRADE_SQL))
        assert "erasure_ledger" in inspect(conn).get_table_names()
    finally:
        db_session.rollback()


def test_the_orm_mirrors_the_live_columns(db_session):
    """The half ``test_schema_drift.py`` cannot see: it compares columns, not nullability.

    ``AuthConfigAudit.changed_by`` shipped as ``nullable=False`` against a nullable
    column for exactly this reason (v387).
    """
    from app.db.base import Base

    live = {
        c["name"]: c["nullable"]
        for c in inspect(db_session.connection()).get_columns("erasure_ledger")
    }
    model = Base.metadata.tables["erasure_ledger"]

    assert set(model.columns.keys()) == set(live)
    mismatched = {
        name: (model.columns[name].nullable, live[name])
        for name in live
        if model.columns[name].nullable != live[name]
    }
    assert not mismatched, f"model/DDL nullability disagreement: {mismatched}"
