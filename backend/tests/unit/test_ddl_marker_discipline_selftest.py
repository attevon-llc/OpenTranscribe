"""Guard the guard: every ``_executes_ddl`` rule needs a must-fire and a must-clean case.

``test_ddl_marker_discipline.py`` decides which tests need ``@pytest.mark.ddl_exclusive``.
A rule in it that matches *nothing* reports zero findings, which is indistinguishable from
a clean suite — the failure mode that hid two dead detectors in each of the test auditors
(issue #431), and the one that let
``test_v381_migration_consistency.py::test_rerunning_the_upgrade_is_a_no_op`` execute
``ALTER TABLE "user" ADD COLUMN`` twice with no marker: the scanner resolved a DDL string
from a literal, an f-string and a local ``ast.Name``, but not from an ``ast.Attribute``
(``module.UPGRADE_SQL``), so the SQL was in ``alembic/versions/`` where nothing looked.

The cases below are written against the REAL revisions rather than invented SQL, so they
also pin the facts the marking decisions rest on: v381's ``UPGRADE_SQL`` is DDL, and
v379's ``RENAME_SQL`` / v377's ``RETIRED_AUTH_CONFIG_KEYS_SQL`` are not. Flagging those two
would force a marker onto a pure-DML test and buy back the stop-the-world barrier this
whole mechanism exists to remove.
"""

from __future__ import annotations

import ast
import textwrap

from tests.unit.test_ddl_marker_discipline import _any_revision_ddl_constants
from tests.unit.test_ddl_marker_discipline import _ddl_attributes
from tests.unit.test_ddl_marker_discipline import _ddl_constants
from tests.unit.test_ddl_marker_discipline import _executes_ddl
from tests.unit.test_ddl_marker_discipline import _revision_ddl_constants


def _scan(source: str) -> bool:
    """True when the scanner says ANY test in this synthetic module executes DDL.

    ``any`` rather than the first match: a case that carries two tests (one source-text
    assertion, one injection payload) has to be clean for *both*, or the second is
    unexamined and the must-stay-clean claim is only half made.
    """
    tree = ast.parse(textwrap.dedent(source))
    consts, attrs = _ddl_constants(tree), _ddl_attributes(tree)
    return any(
        _executes_ddl(node, consts, attrs)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )


#: The shape that got through: DDL resolved off a revision module's constant.
_ATTRIBUTE_DDL = """
    REVISION = "v381_approval_state"

    def test_rerunning_the_upgrade_is_a_no_op(db_session):
        conn = db_session.connection()
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.CONSTRAINT_SQL))
"""

#: Same shape, DML payload. v379 is a pure data migration — its ``RENAME_SQL`` is UPDATE
#: and DELETE only.
_ATTRIBUTE_DML = """
    REVISION = "v379_rename_keycloak_config_to_oidc"

    def test_rerunning_the_rename_is_a_no_op(db_session):
        conn = db_session.connection()
        conn.execute(text(module.RENAME_SQL))
"""

#: v377's retired-keys statement is a guarded DELETE, executed the same way.
_ATTRIBUTE_DELETE = """
    REVISION = "v377_harden_user_auth_invariants"

    def test_retired_pki_config_keys_are_deleted(db_session):
        conn = db_session.connection()
        conn.execute(text(module.RETIRED_AUTH_CONFIG_KEYS_SQL))
"""

#: A revision replayed through its own entry point puts no SQL in the test file at all.
_ENTRYPOINT_CALL = """
    REVISION = "v386_add_tag_share"

    def test_the_downgrade_actually_runs(db_session):
        with _alembic_operations(db_session.connection()):
            module.downgrade()
"""

#: Source-text assertions and injection payloads mention DDL without running any.
_MENTIONS_ONLY = """
    def test_the_migration_is_idempotent():
        assert "CREATE TABLE IF NOT EXISTS tag_share" in source

    def test_search_rejects_an_injection_payload(client):
        response = client.get("/api/search", params={"q": "'; DROP TABLE media_file; --"})
        assert response.status_code == 422
"""


def test_ddl_on_a_revision_attribute_is_detected() -> None:
    """MUST FIRE. This is the blind spot; if it stops firing, v381 goes unmarked again."""
    assert _scan(_ATTRIBUTE_DDL) is True


def test_a_dml_revision_attribute_is_not_detected() -> None:
    """MUST STAY CLEAN. Marking v379's UPDATE replay would add a barrier protecting nothing."""
    assert _scan(_ATTRIBUTE_DML) is False


def test_a_delete_revision_attribute_is_not_detected() -> None:
    """MUST STAY CLEAN. ``_SQL`` in the name is not evidence of DDL — the value decides."""
    assert _scan(_ATTRIBUTE_DELETE) is False


def test_calling_a_revision_entrypoint_is_detected() -> None:
    """MUST FIRE. ``module.downgrade()`` runs every DROP in the revision (issue #431)."""
    assert _scan(_ENTRYPOINT_CALL) is True


def test_ddl_only_mentioned_in_a_string_is_not_detected() -> None:
    """MUST STAY CLEAN. The over-marking half of the guard depends on this staying false."""
    assert _scan(_MENTIONS_ONLY) is False


def test_the_local_constant_rule_still_fires() -> None:
    """MUST FIRE. ``_GUARD_SQL``-style constants (test_uuid7_migration_guard.py)."""
    assert (
        _scan("""
        _GUARD_SQL = "ALTER TABLE media_file ALTER COLUMN uuid TYPE uuid USING uuid::uuid"

        def test_guard(db_session):
            db_session.connection().execute(text(_GUARD_SQL))
    """)
        is True
    )


def test_the_temp_table_exemption_still_holds() -> None:
    """MUST STAY CLEAN. ``pg_temp_*`` is session-private, so it needs no isolation."""
    assert (
        _scan("""
        def test_scratch(db_session):
            db_session.connection().execute(text("CREATE TEMP TABLE scratch (a int)"))
    """)
        is False
    )


def test_revision_resolution_reads_the_named_revision_only() -> None:
    """``UPGRADE_SQL`` exists in more than one revision, with different contents.

    Resolving the attribute against every revision at once would let v380's DDL decide
    v379's verdict. The ``REVISION`` constant is what keeps the lookup exact.
    """
    assert "UPGRADE_SQL" in _revision_ddl_constants("v381_approval_state")
    assert _revision_ddl_constants("v379_rename_keycloak_config_to_oidc") == frozenset()


def test_the_fallback_union_is_not_empty() -> None:
    """A suite naming no revision falls back to the union — which must resolve something.

    An empty union would silently turn the attribute rule off for every such suite while
    still reading as an implemented safety net.
    """
    assert "UPGRADE_SQL" in _any_revision_ddl_constants()


def test_the_format_template_shape_resolves() -> None:
    """v381's ``CONSTRAINT_SQL`` is ``_CONSTRAINT_TEMPLATE.format(...)``, not a literal.

    Its ``ALTER TABLE … ADD CONSTRAINT`` / ``CREATE INDEX`` is only visible if the resolver
    follows the ``.format`` call back to the template.
    """
    assert "CONSTRAINT_SQL" in _revision_ddl_constants("v381_approval_state")
