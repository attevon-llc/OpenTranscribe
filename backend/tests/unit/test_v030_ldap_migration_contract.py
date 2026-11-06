"""``v030_add_ldap_auth``: its identity, and the schema it produces, executed for real.

The revision is applied history: every database that exists has run it, and
``schema_detect/legacy.py`` stamps it when ``user.auth_type`` exists. So whatever its source
text looks like, it must keep (1) its revision identity and (2) the exact schema it leaves
behind, from each starting shape a real database can be in. Nothing executed it before this
module (gap G-9 of the P3 spec).

How the schema is compared: each test creates a **private scratch database** on the test
server, builds a ``user`` table in one of three starting shapes, runs the revision's own
``upgrade()`` / ``downgrade()`` through Alembic's ``Operations`` proxy, and reflects the result
from the catalogs (columns with type, length, nullability and default in ordinal order; every
index with its definition; every unique constraint with its columns). The reflected snapshot
is compared with ``CONTRACT``, written out literally below. A private database rather than a
schema in the shared one because the revision's catalog probes do not filter by schema
(spec D-8): the shared database's real ``user`` table would answer them.

The scanner in ``test_ddl_marker_discipline.py`` cannot tell that the DDL targets a private
database, so each test takes the suite's DDL lock EXCLUSIVE on the shared database for its
duration (one drain per test) rather than widening that module's allowlist.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db.base import engine as shared_engine
from tests.db_locks import acquire_ddl_lock_exclusive

REVISION = "v030_add_ldap_auth"
_BACKEND = Path(__file__).resolve().parents[2]
_REVISION_PATH = _BACKEND / "alembic" / "versions" / f"{REVISION}.py"
_GUARD = _BACKEND.parent / "scripts" / "guards" / "check_migration_safety.py"

#: The ``user`` table before v030 ran (the columns are illustrative; v030 touches none of them).
PRE_V030_TABLE = 'CREATE TABLE "user" (id SERIAL PRIMARY KEY, email VARCHAR(255) NOT NULL)'

#: A database built from ``database/init_db.sql``: both columns already there, ``ldap_uid``
#: UNIQUE under Postgres's auto-generated constraint name, ``auth_type`` already widened.
INIT_DB_TABLE = (
    'CREATE TABLE "user" (id SERIAL PRIMARY KEY, email VARCHAR(255) NOT NULL, '
    "auth_type VARCHAR(20) DEFAULT 'local' NOT NULL, ldap_uid VARCHAR(255) UNIQUE NULL)"
)

#: A hand-repaired database: ``ldap_uid`` added as a bare column, nothing else.
BARE_COLUMN_TABLE = (
    'CREATE TABLE "user" (id SERIAL PRIMARY KEY, email VARCHAR(255) NOT NULL, '
    "ldap_uid VARCHAR(255) NULL)"
)

_ID = ("id", "integer", None, "NO", "nextval('user_id_seq'::regclass)")
_EMAIL = ("email", "character varying", 255, "NO", None)
_PKEY_INDEX = ("user_pkey", 'CREATE UNIQUE INDEX user_pkey ON public."user" USING btree (id)')
_LDAP_INDEX = (
    "idx_user_ldap_uid",
    'CREATE INDEX idx_user_ldap_uid ON public."user" USING btree (ldap_uid)',
)

#: What a pre-v030 table looks like after ``upgrade()``. This is the schema contract.
CONTRACT: dict[str, Any] = {
    "columns": [
        _ID,
        _EMAIL,
        ("auth_type", "character varying", 10, "NO", "'local'::character varying"),
        ("ldap_uid", "character varying", 255, "YES", None),
    ],
    "indexes": sorted(
        [
            _PKEY_INDEX,
            _LDAP_INDEX,
            (
                "uq_user_ldap_uid",
                'CREATE UNIQUE INDEX uq_user_ldap_uid ON public."user" USING btree (ldap_uid)',
            ),
        ]
    ),
    "unique_constraints": [("uq_user_ldap_uid", ["ldap_uid"])],
}


def _revision_module() -> ModuleType:
    """Load the revision by path: ``alembic/`` is not an importable package."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _scratch_database(starting_table: str) -> Iterator[Engine]:
    """A database of our own on the test server, holding only ``starting_table``."""
    name = f"ot_p3_v030_{uuid.uuid4().hex[:10]}_test"
    url = make_url(settings.DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    scratch = create_engine(url.set(database=name))
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        with scratch.begin() as conn:
            conn.execute(text(starting_table))
        yield scratch
    finally:
        scratch.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@contextmanager
def _operations(scratch: Engine) -> Iterator[None]:
    """Install Alembic's ``op`` proxy on one transaction of the scratch database."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with scratch.begin() as conn, Operations.context(MigrationContext.configure(connection=conn)):
        yield


def _snapshot(scratch: Engine) -> dict[str, Any]:
    with scratch.connect() as conn:
        columns = [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT column_name, data_type, character_maximum_length, is_nullable, "
                    "column_default FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'user' "
                    "ORDER BY ordinal_position"
                )
            )
        ]
        indexes = sorted(
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() AND tablename = 'user'"
                )
            )
        )
        uniques = sorted(
            (row[0], list(row[1]))
            for row in conn.execute(
                text(
                    "SELECT c.conname, array_agg(a.attname ORDER BY a.attnum) "
                    "FROM pg_constraint c JOIN pg_attribute a "
                    "ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
                    "WHERE c.conrelid = '\"user\"'::regclass AND c.contype = 'u' "
                    "GROUP BY c.conname"
                )
            )
        )
    return {"columns": columns, "indexes": indexes, "unique_constraints": uniques}


class TestIdentity:
    def test_the_revision_identifiers(self):
        module = _revision_module()

        assert (module.revision, module.down_revision) == (REVISION, "v020_add_system_settings")
        assert (module.branch_labels, module.depends_on) == (None, None)

    def test_it_stays_in_the_graph_between_v020_and_v031(self):
        from alembic.script import ScriptDirectory

        from app.db.migrations import get_alembic_config

        config = get_alembic_config()
        config.set_main_option("script_location", str(_BACKEND / "alembic"))
        scripts = ScriptDirectory.from_config(config)

        assert scripts.get_revision(REVISION).down_revision == "v020_add_system_settings"
        assert scripts.get_revision("v031_add_keycloak_pki_auth").down_revision == REVISION

    def test_the_migration_safety_guard_counts_are_unchanged(self):
        spec = importlib.util.spec_from_file_location("_v030_guard", _GUARD)
        assert spec is not None and spec.loader is not None
        guard = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = guard
        spec.loader.exec_module(guard)

        rules = sorted(r for r, _ in guard.findings_for(ast.parse(_REVISION_PATH.read_text())))

        # The baseline holds exactly R1 = 1 and R6 = 1 and fails on a count that moves.
        assert rules == ["R1", "R6"]

    def test_the_docstring_does_not_claim_ldap_uid_holds_samaccountname(self):
        """P3 defect D-9: ``ldap_uid`` holds the configured username attribute, whatever it is."""
        assert "samaccountname" not in _REVISION_PATH.read_text().lower()


class TestSchema:
    def test_a_pre_v030_table_gets_the_contract_and_downgrade_restores_it(self):
        module = _revision_module()
        with shared_engine.connect() as lock_conn, lock_conn.begin():
            acquire_ddl_lock_exclusive(lock_conn)
            with _scratch_database(PRE_V030_TABLE) as scratch:
                before = _snapshot(scratch)

                with _operations(scratch):
                    module.upgrade()
                assert _snapshot(scratch) == CONTRACT

                with _operations(scratch):
                    module.upgrade()
                assert _snapshot(scratch) == CONTRACT, "a second upgrade must be a no-op"

                with _operations(scratch):
                    module.downgrade()
                assert _snapshot(scratch) == before

                with _operations(scratch):
                    module.downgrade()
                assert _snapshot(scratch) == before, "a second downgrade must be a no-op"

    def test_an_init_db_table_gets_the_index_and_no_second_unique_constraint(self):
        module = _revision_module()
        with shared_engine.connect() as lock_conn, lock_conn.begin():
            acquire_ddl_lock_exclusive(lock_conn)
            with _scratch_database(INIT_DB_TABLE) as scratch:
                before = _snapshot(scratch)

                with _operations(scratch):
                    module.upgrade()
                after = _snapshot(scratch)

                assert after["columns"] == before["columns"], "existing columns are left alone"
                assert after["unique_constraints"] == [("user_ldap_uid_key", ["ldap_uid"])]
                assert after["indexes"] == sorted([*before["indexes"], _LDAP_INDEX])

                with _operations(scratch):
                    module.downgrade()
                assert _snapshot(scratch) == {
                    "columns": [_ID, _EMAIL],
                    "indexes": [_PKEY_INDEX],
                    "unique_constraints": [],
                }

    def test_a_pre_existing_bare_column_gets_the_index_but_no_unique_constraint(self):
        """The unique constraint is created only when this revision created the column."""
        module = _revision_module()
        with shared_engine.connect() as lock_conn, lock_conn.begin():
            acquire_ddl_lock_exclusive(lock_conn)
            with _scratch_database(BARE_COLUMN_TABLE) as scratch:
                with _operations(scratch):
                    module.upgrade()
                after = _snapshot(scratch)

                assert after["columns"] == [
                    _ID,
                    _EMAIL,
                    ("ldap_uid", "character varying", 255, "YES", None),
                    ("auth_type", "character varying", 10, "NO", "'local'::character varying"),
                ]
                assert after["unique_constraints"] == []
                assert after["indexes"] == sorted([_PKEY_INDEX, _LDAP_INDEX])
