"""The registry matches the live schema, and no audit-shaped table is left unclassified."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db import audit_protection
from app.db.audit_protection import EXEMPT_TABLES
from app.db.audit_protection import PROTECTED_TABLES
from app.db.audit_protection import ProtectedTable
from app.db.audit_protection import all_protected_tables
from app.db.audit_protection import trigger_arguments

_REVISION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "v457_audit_guard_followups.py"
)

_V453 = "v453_audit_row_immutability.py"

_AUDIT_SHAPED = ("%audit%", "%\\_use", "%ledger%", "%notification%", "%receipt%", "%takedown%")


#: Tables whose name matches the audit-shaped patterns but that hold configuration or work
#: state, not a record of what happened. Each needs its reason.
_NOT_AUDIT = {
    "email_notification_config": "SMTP/notification settings, not a record of events",
}


def test_every_protected_table_exists_and_every_audit_shaped_table_is_classified(db_session):
    conn = db_session.connection()
    found: set[str] = set()
    for pattern in _AUDIT_SHAPED:
        found |= set(
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = "
                    "'public' AND table_type = 'BASE TABLE' AND table_name LIKE :p"
                ),
                {"p": pattern},
            ).scalars()
        )
    assert len(found) >= 5, "the deriving query matched too few tables to prove anything"
    classified = {t.name for t in all_protected_tables()} | set(EXEMPT_TABLES)
    # Work/queue tables that merely share a word (search_indexing tasks etc.) are not audit
    # records; anything else must be classified.
    unclassified = found - classified - set(_NOT_AUDIT)
    assert unclassified == set(), f"classify these in audit_protection.py: {sorted(unclassified)}"
    for table in all_protected_tables():
        cols = set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t"
                ),
                {"t": table.name},
            ).scalars()
        )
        assert cols, f"{table.name} does not exist"
        assert set(table.mutable_columns) <= cols
        assert set(table.nullable_on_cascade) <= cols
        if table.prune_column:
            assert table.prune_column in cols


def test_every_nullable_fk_to_a_deletable_parent_is_declared_clearable(db_session):
    """An ON DELETE SET NULL FK column must be in nullable_on_cascade, else deleting the parent
    (a user, an organization, a file) would fail on the guard."""
    conn = db_session.connection()
    assert len(all_protected_tables()) >= 8
    for table in all_protected_tables():
        rows = conn.execute(
            text(
                "SELECT a.attname FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = "
                "c.conrelid AND a.attnum = ANY (c.conkey) WHERE c.contype = 'f' AND "
                "c.confdeltype = 'n' AND c.conrelid = to_regclass(:t)"
            ),
            {"t": f"public.{table.name}"},
        ).scalars()
        missing = set(rows) - set(table.nullable_on_cascade) - set(table.mutable_columns)
        assert missing == set(), f"{table.name}: SET NULL columns not declared: {sorted(missing)}"


def test_exemptions_carry_a_reason_and_do_not_overlap():
    assert all(reason.strip() for reason in EXEMPT_TABLES.values())
    assert not ({t.name for t in PROTECTED_TABLES} & set(EXEMPT_TABLES))


def test_the_revision_constant_equals_the_registry():
    spec = importlib.util.spec_from_file_location("v457", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = {(t.name, *trigger_arguments(t)) for t in audit_protection.generic_guard_tables()}
    # v457 holds the tables whose trigger arguments changed after v453 (or that v453 did not
    # guard); the rest are still v453's frozen copy. Together they must equal the registry.
    older = importlib.util.spec_from_file_location("v453", _REVISION.with_name(_V453))
    assert older is not None and older.loader is not None
    v453 = importlib.util.module_from_spec(older)
    older.loader.exec_module(v453)
    superseded = {name for name, _, _ in module.GUARDED}
    remaining = {g for g in v453.GUARDED if g[0] not in superseded}
    assert remaining | set(module.GUARDED) == expected
    assert set(v453.PRUNABLE) == {
        (t.name, t.prune_column) for t in audit_protection.prunable_tables()
    }


def test_register_protected_table_rejects_a_duplicate():
    with pytest.raises(ValueError, match="already registered"):
        audit_protection.register_protected_table(ProtectedTable(name="erasure_ledger"))
