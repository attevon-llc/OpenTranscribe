"""v387 migration + detection-arm consistency (deletable actors, tag_share's missing CHECK).

Three repairs in one revision, and each one is only *worth* anything if it is enforced
by the schema rather than remembered by a deletion helper:

1. Five ``ON DELETE SET NULL`` rules on the "who did this" FKs into ``user``, so deleting
   an admin who ever changed auth config, quarantined somebody else's file, or shared
   somebody else's prompt stops being an undiagnosable
   ``500 "User deletion failed"``.
2. ``_tag_share_target_type_check`` — the guard ``v386`` left off while mirroring
   ``collection_share``.
3. The duplicate ``users_role_check`` dropped, so a future widening of the role set
   cannot be applied to one of a pair and silently refused by the other (the ``v380``
   bug, on ``role``).

Following ``test_v386_migration_consistency.py``, this suite **executes** the revision's
SQL rather than grepping its source text, twice, to prove the re-run is a no-op — the
invariant the startup runner depends on, since a migration failure is ``SystemExit(1)``
and the backend then does not start.
"""

from __future__ import annotations

import contextlib
import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

#: `ddl_exclusive` is applied PER TEST, never to the module — every EXCLUSIVE advisory
#: lock drains all other xdist workers, so a read-only schema assertion carrying it costs
#: a full-suite barrier for nothing (issue #431). Only the tests that execute
#: ALTER/DROP carry it, and `tests/unit/test_ddl_marker_discipline.py` enforces both
#: directions.

REVISION = "v387_actor_fks_and_tag_share_check"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

#: The five FKs the revision re-points, as ``(table, column, constraint)``. Mirrors the
#: ``VALUES`` list inside ``ACTOR_FK_SQL``; :func:`test_the_revision_and_this_suite_agree`
#: keeps the two from drifting.
_ACTOR_FKS = (
    ("auth_config", "created_by", "auth_config_created_by_fkey"),
    ("auth_config", "updated_by", "auth_config_updated_by_fkey"),
    ("auth_config_audit", "changed_by", "auth_config_audit_changed_by_fkey"),
    ("media_file", "quarantined_by", "media_file_quarantined_by_fkey"),
    ("summary_prompt", "shared_by", "summary_prompt_shared_by_fkey"),
)

SET_NULL = "n"
NO_ACTION = "a"


def _revision_module():
    """Load the revision file by path (``alembic/`` is not importable — see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _alembic_operations(conn):
    """Bind alembic's ``op`` proxy to this test's connection so ``upgrade()`` really runs.

    ``Operations.context()`` has no ``try``/``finally`` around its yield: a migration that
    raised would leave the proxy installed, pointing at a connection this test is about to
    close, for every later test on this xdist worker.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    cm = Operations.context(MigrationContext.configure(connection=conn))
    cm.__enter__()
    try:
        yield
    finally:
        cm.__exit__(None, None, None)


def _delete_rules(conn) -> dict[str, str]:
    """``constraint name -> confdeltype`` for the five actor FKs."""
    rows = conn.execute(
        text("SELECT conname, confdeltype FROM pg_constraint WHERE conname = ANY(:names)"),
        {"names": [fk for _, _, fk in _ACTOR_FKS]},
    ).all()
    return {row.conname: row.confdeltype for row in rows}


def _insert_user(conn, *, role: str = "admin") -> int:
    """A user row owned by this test — never borrowed from ambient data (CI has none)."""
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, :su, :role, 'local') RETURNING id"
            ),
            {
                "e": f"v387_{uuid_pkg.uuid4().hex[:10]}@example.com",
                "su": role == "super_admin",
                "role": role,
            },
        ).scalar()
    )


def test_v387_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v386_add_tag_share"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # Head now; still true once v388 revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v387_migration_is_vendor_neutral():
    """The CI seam guard greps for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_revision_and_this_suite_agree_on_the_five_fks():
    """Guard the guard: this suite's ``_ACTOR_FKS`` must BE the revision's list.

    The revision drives its repair from a PL/pgSQL ``VALUES`` list, which no Python
    import can read. If a sixth FK is added there and not here, every assertion below
    still passes while saying nothing about it — the shape that makes a test look like
    coverage it is not.
    """
    sql = _revision_module().ACTOR_FK_SQL
    for table, column, constraint in _ACTOR_FKS:
        assert f"'{table}'" in sql, f"{table} missing from the revision's VALUES list"
        assert f"'{column}'" in sql
        assert f"'{constraint}'" in sql
    # And the converse: no constraint name in the revision is unaccounted for here.
    named = {line.strip() for line in sql.splitlines() if "_fkey'" in line}
    assert len(named) == len(_ACTOR_FKS), (
        f"the revision names {len(named)} FK constraint(s), this suite tracks {len(_ACTOR_FKS)}"
    )


def test_detection_arm_returns_v387_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_tag_share_check(db_session):
    """Drop ``_tag_share_target_type_check`` and the ladder must stop matching v387.

    The half that proves the arm is wired to its probe rather than passing for an
    unrelated reason. Asserted as a *band* — at or after v386, strictly before v387 —
    because an exact ``==`` on a lower revision goes red or vacuous the next time the
    ladder above it changes, which is what happened to three of these suites already.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    conn.execute(text("ALTER TABLE tag_share DROP CONSTRAINT _tag_share_target_type_check"))
    tables = inspect(conn).get_table_names()
    detected = _detect_schema_version(conn, tables)
    db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert order.index("v386_add_tag_share") <= order.index(detected) < order.index(REVISION), (
        f"a database without _tag_share_target_type_check was stamped {detected!r}; it "
        f"must land below {REVISION} so the constraint is actually added"
    )


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_while_the_duplicate_role_check_survives(db_session):
    """The third marker is an ABSENCE, so it needs its own arm — re-ADD the duplicate.

    A revision that REMOVES an object is fingerprinted by that object being gone (the
    ``v385`` shape). Restoring ``users_role_check`` must take the ladder back below v387,
    or a database that still carries the duplicate is stamped as done and never gets the
    drop — leaving exactly the two-constraint state the revision exists to end.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    conn.execute(
        text(
            'ALTER TABLE "user" ADD CONSTRAINT users_role_check '
            "CHECK (role IN ('user', 'admin', 'super_admin'))"
        )
    )
    # finally, not a trailing call: this re-adds a constraint to the SHARED dev schema, so
    # anything that raises in between must still undo it. A leaked `users_role_check` is
    # invisible until some later run reports two role CHECKs and blames the migration.
    try:
        tables = inspect(conn).get_table_names()
        detected = _detect_schema_version(conn, tables)
    finally:
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert order.index("v386_add_tag_share") <= order.index(detected) < order.index(REVISION)


def test_all_five_actor_fks_are_set_null(db_session):
    """The whole point of part 1, as one assertion over the live schema.

    ``SET NULL`` rather than a sixth entry in each hand-maintained deletion list, because
    there are two independent deletion paths and nothing compares them — and a third
    path, or a hand-run ``DELETE FROM "user"``, would need the same additions again.
    """
    rules = _delete_rules(db_session.connection())

    assert set(rules) == {fk for _, _, fk in _ACTOR_FKS}, f"missing FK constraints: {rules}"
    wrong = {name: rule for name, rule in rules.items() if rule != SET_NULL}
    assert wrong == {}, (
        f"these actor FKs are not ON DELETE SET NULL: {wrong} "
        "('a' = NO ACTION, the state that made the owning admin undeletable)"
    )


def test_the_audit_column_became_nullable(db_session):
    """``auth_config_audit.changed_by`` had to lose ``NOT NULL`` for ``SET NULL`` to work.

    An ``ON DELETE SET NULL`` on a ``NOT NULL`` column is a rule that can only ever fail.
    Asserted separately from the FK rule because it is the one column where the change
    has a cost — attribution degrades to NULL — and the trade-off should fail visibly if
    it is ever reverted half-way.
    """
    nullable = {
        col["name"]: col["nullable"]
        for col in inspect(db_session.connection()).get_columns("auth_config_audit")
    }
    assert nullable["changed_by"] is True
    # The record itself must still be complete: only the actor is allowed to go.
    assert nullable["config_key"] is False
    assert nullable["change_type"] is False


def test_the_orm_mirrors_the_nullability_the_migration_created(db_session):
    """The half of a SET NULL change that no existing gate covers.

    ``test_schema_drift.py`` compares tables and columns, **not nullability**, so a
    column the migration made nullable while the model still says ``nullable=False``
    passes every check in the repo — right up until SQLAlchemy loads a NULL into a
    non-Optional attribute, or an ORM insert re-asserts NOT NULL against a database
    that no longer has it. That is exactly what ``AuthConfigAudit.changed_by`` was.

    Derived from the same ``_ACTOR_FKS`` list the FK rules are, so a sixth actor FK
    added to the revision is checked here without editing this test.
    """
    from app.db.base import Base

    mismatches = []
    for table, column, _fk in _ACTOR_FKS:
        live = {
            col["name"]: col["nullable"]
            for col in inspect(db_session.connection()).get_columns(table)
        }
        model_table = Base.metadata.tables[table]
        model_nullable = model_table.columns[column].nullable
        if model_nullable != live[column]:
            mismatches.append(
                f"{table}.{column}: model nullable={model_nullable}, database={live[column]}"
            )

    assert not mismatches, "ORM and database disagree on nullability:\n  " + "\n  ".join(mismatches)


def test_deleting_the_admin_blanks_the_audit_actor_and_keeps_the_row(db_session):
    """The behaviour, not just the rule: the audit entry outlives its author.

    Deleting the record of a change because its author left is the opposite of an audit
    trail — and ``endpoints/auth_config.get_audit_log`` was already written for a missing
    actor (``if audit.changed_by is not None``, rendered as unknown). Until v387 that
    branch was unreachable, because the FK made the deletion impossible.
    """
    conn = db_session.connection()
    admin_id = _insert_user(conn)
    key = f"v387_probe_{uuid_pkg.uuid4().hex[:8]}"
    conn.execute(
        text(
            "INSERT INTO auth_config_audit (uuid, config_key, new_value, changed_by, "
            "change_type) VALUES (:u, :k, 'true', :cb, 'update')"
        ),
        {"u": uuid_pkg.uuid4(), "k": key, "cb": admin_id},
    )

    conn.execute(text('DELETE FROM "user" WHERE id = :i'), {"i": admin_id})

    row = conn.execute(
        text("SELECT changed_by, new_value FROM auth_config_audit WHERE config_key = :k"),
        {"k": key},
    ).one()
    assert row.changed_by is None
    assert row.new_value == "true"
    db_session.rollback()


def test_deleting_the_reviewer_keeps_the_quarantined_file(db_session):
    """``media_file.quarantined_by``: a takedown never deletes rows.

    The file belongs to a *different* account, so it survives the reviewing admin's
    deletion — and the takedown itself must survive with it, or an account deletion would
    silently release every file that admin had ever quarantined.
    """
    conn = db_session.connection()
    owner_id = _insert_user(conn, role="user")
    reviewer_id = _insert_user(conn)
    fuuid = uuid_pkg.uuid4()
    file_id = conn.execute(
        text(
            "INSERT INTO media_file (uuid, filename, storage_path, content_type, file_size, "
            "user_id, status, is_quarantined, quarantine_reason, quarantined_by) "
            "VALUES (:u, :f, :p, 'video/mp4', 10, :o, 'completed', true, 'DMCA', :r) "
            "RETURNING id"
        ),
        {
            "u": fuuid,
            "f": f"{fuuid.hex[:8]}.mp4",
            "p": f"m/{fuuid}.mp4",
            "o": owner_id,
            "r": reviewer_id,
        },
    ).scalar()

    conn.execute(text('DELETE FROM "user" WHERE id = :i'), {"i": reviewer_id})

    row = conn.execute(
        text("SELECT is_quarantined, quarantined_by FROM media_file WHERE id = :i"),
        {"i": file_id},
    ).one()
    assert row.is_quarantined is True
    assert row.quarantined_by is None
    db_session.rollback()


def test_deleting_the_sharer_keeps_the_owners_prompt_shared(db_session):
    """``summary_prompt.shared_by``: an admin may flip sharing on somebody else's prompt.

    So this column points at a row the owner-scoped deletion sweep never matches. The
    prompt is not the sharer's to destroy, and ``is_shared`` must not silently flip back —
    that would withdraw a shared prompt from everyone using it.
    """
    conn = db_session.connection()
    owner_id = _insert_user(conn, role="user")
    sharer_id = _insert_user(conn)
    prompt_id = conn.execute(
        text(
            "INSERT INTO summary_prompt (uuid, name, prompt_text, user_id, is_shared, "
            "shared_by, tags) VALUES (:u, :n, 'summarise', :o, true, :s, '[]'::jsonb) "
            "RETURNING id"
        ),
        {
            "u": uuid_pkg.uuid4(),
            "n": f"v387-{uuid_pkg.uuid4().hex[:8]}",
            "o": owner_id,
            "s": sharer_id,
        },
    ).scalar()

    conn.execute(text('DELETE FROM "user" WHERE id = :i'), {"i": sharer_id})

    row = conn.execute(
        text("SELECT is_shared, shared_by FROM summary_prompt WHERE id = :i"), {"i": prompt_id}
    ).one()
    assert row.is_shared is True
    assert row.shared_by is None
    db_session.rollback()


def test_the_tag_share_target_type_check_rejects_a_third_value(db_session):
    """Part 2. ``target_type`` selects which nullable target column the resolver reads.

    A third value is a grant no branch matches: it exists, resolves to nobody, and still
    appears in the owner's list of who the tag is shared with.
    """
    conn = db_session.connection()
    user_id = _insert_user(conn, role="user")
    tag_id = conn.execute(
        text("INSERT INTO tag (uuid, name, user_id) VALUES (:u, :n, :o) RETURNING id"),
        {"u": uuid_pkg.uuid4(), "n": f"v387-{uuid_pkg.uuid4().hex[:8]}", "o": user_id},
    ).scalar()

    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO tag_share (uuid, tag_id, shared_by_id, target_type, "
                "target_user_id) VALUES (:u, :t, :s, 'anyone', :tu)"
            ),
            {"u": uuid_pkg.uuid4(), "t": tag_id, "s": user_id, "tu": user_id},
        )
    db_session.rollback()


def test_the_duplicate_role_check_is_gone(db_session):
    """Part 3, and the exact assertion ``v380`` made for ``auth_type``.

    One rule, one owner. Two constraints with identical bodies means the next widening
    reaches one of them and the other keeps refusing — which does not fail at migration
    time, it fails at every login of the new kind.
    """
    module = _revision_module()
    names = (
        db_session.connection()
        .execute(
            text(
                "SELECT c.conname FROM pg_constraint c JOIN pg_class t ON c.conrelid = t.oid "
                "WHERE t.relname = 'user' AND c.contype = 'c' "
                "AND pg_get_constraintdef(c.oid) LIKE '%role%' "
                "AND pg_get_constraintdef(c.oid) NOT LIKE '%is_superuser%'"
            )
        )
        .scalars()
        .all()
    )

    assert list(names) == ["ck_user_role_valid"], f"expected one role CHECK, found {names}"
    for legacy in module.LEGACY_ROLE_CONSTRAINTS:
        assert legacy not in names


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    """The invariant in backend/alembic/CLAUDE.md, executed rather than asserted about.

    The startup runner stamps *untracked* databases by schema fingerprint, so a revision
    routinely re-runs against a database that already carries part of its changes — and a
    migration failure is ``SystemExit(1)``, i.e. the backend refuses to start. Replaying
    the real SQL twice is the only assertion that catches a non-idempotent statement;
    v386 shipped one (``op.create_table``) past every source-text check in this directory.
    """
    module = _revision_module()
    conn = db_session.connection()

    conn.execute(text(module.UPGRADE_SQL))
    conn.execute(text(module.UPGRADE_SQL))

    rules = _delete_rules(conn)
    assert set(rules.values()) == {SET_NULL}
    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conname = '_tag_share_target_type_check'"
            )
        ).scalar()
        == 1
    )
    assert (
        conn.execute(
            text("SELECT count(*) FROM pg_constraint WHERE conname = 'users_role_check'")
        ).scalar()
        == 0
    )
    db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_upgrade_repairs_a_reverted_schema(db_session):
    """Run ``downgrade()`` then ``upgrade()`` through alembic's own ``op``, twice down.

    Every other "downgrade mirrors the upgrade" test in this directory reads the
    revision's source *text*, so a downgrade naming a misspelled constraint would pass.
    This executes both. The double downgrade is the other half of the same barrier's
    cost — each ``ddl_exclusive`` test drains every xdist worker, so one drain covers
    both claims (issue #431).

    The round trip is what proves the guard conditions are real: after the downgrade the
    five FKs are back to ``NO ACTION`` and the duplicate CHECK is back, so the second
    ``upgrade()`` has to take the repair branch it skipped in the test above.
    """
    module = _revision_module()
    conn = db_session.connection()

    with _alembic_operations(conn):
        module.downgrade()
        module.downgrade()

    reverted = _delete_rules(conn)
    assert set(reverted.values()) == {NO_ACTION}, f"downgrade left SET NULL behind: {reverted}"
    assert (
        conn.execute(
            text("SELECT count(*) FROM pg_constraint WHERE conname = 'users_role_check'")
        ).scalar()
        == 1
    ), "downgrade must restore the legacy CHECK it removed"

    with _alembic_operations(conn):
        module.upgrade()

    assert set(_delete_rules(conn).values()) == {SET_NULL}, "the pair does not round-trip"
    db_session.rollback()
