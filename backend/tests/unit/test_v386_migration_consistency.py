"""v386 migration + detection-arm consistency (share a tag with users and groups).

Head revisions have shipped without a suite before; this one exists because every
guarantee ``tag_share`` makes is enforced by the DDL and nowhere else. The authorization
code reads a grant row and trusts it: that exactly one target is set, that a grant cannot
be duplicated, and that a grant disappears with the tag, the sharer or the recipient. None
of those are re-checked in Python.

Two tests here are not in any other per-revision suite:
:func:`test_rerunning_the_upgrade_is_a_no_op` replays the real SQL (v386 originally used
``op.create_table``, which cannot re-run), and
:func:`test_the_downgrade_really_runs_and_the_upgrade_restores_it` **executes**
``downgrade()`` where the other seven suites only grep its source text.
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

#: `ddl_exclusive` is applied PER TEST below, never to the module. An EXCLUSIVE advisory-lock
#: acquisition drains every other xdist worker, so spending one on a read-only schema
#: assertion turns that assertion into a full-suite barrier — that is what made this group
#: 414 s of a 511 s wall clock. Only the three tests that execute CREATE/DROP carry it
#: (issue #389, #431), and both directions are enforced by
#: `tests/unit/test_ddl_marker_discipline.py`.

REVISION = "v386_add_tag_share"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

_INDEXES = (
    "idx_tag_share_tag_id",
    "idx_tag_share_target_user_id",
    "idx_tag_share_target_group_id",
    "_tag_share_user_uc",
    "_tag_share_group_uc",
)


def _revision_module():
    """Load the revision file by path (``alembic/`` is not importable — see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _alembic_operations(conn):
    """Bind alembic's ``op`` proxy to this test's connection, so ``upgrade()`` really runs.

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


def _insert_user(conn) -> int:
    """A user row owned by this test — never borrowed from ambient data (CI has none)."""
    email = f"v386_{uuid_pkg.uuid4().hex[:8]}@example.com"
    new_id = conn.execute(
        text(
            'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
            "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
        ),
        {"e": email},
    ).scalar()
    return int(new_id)


def _seed_targets(conn) -> tuple[int, int, int]:
    """A user, a tag they own, and a group they own. Returns ``(user_id, tag_id, group_id)``."""
    suffix = uuid_pkg.uuid4().hex[:8]
    user_id = _insert_user(conn)
    tag_id = conn.execute(
        text("INSERT INTO tag (name, user_id) VALUES (:n, :u) RETURNING id"),
        {"n": f"v386-tag-{suffix}", "u": user_id},
    ).scalar()
    group_id = conn.execute(
        text("INSERT INTO user_group (name, owner_id) VALUES (:n, :o) RETURNING id"),
        {"n": f"v386-group-{suffix}", "o": user_id},
    ).scalar()
    return int(user_id), int(tag_id), int(group_id)


def _grant(
    conn,
    tag_id: int,
    sharer_id: int,
    *,
    user: int | None = None,
    group: int | None = None,
):
    """Insert one grant row. ``target_type`` follows whichever target was given."""
    return conn.execute(
        text(
            "INSERT INTO tag_share (tag_id, shared_by_id, target_type, target_user_id, "
            "target_group_id) VALUES (:t, :s, :ty, :u, :g)"
        ),
        {
            "t": tag_id,
            "s": sharer_id,
            "ty": "user" if user is not None else "group",
            "u": user,
            "g": group,
        },
    )


def _grants(conn, where: str, **params) -> int:
    """Count ``tag_share`` rows matching one WHERE clause.

    The clause is interpolated; the *values* are always bound. Every caller passes a literal.
    """
    count = conn.execute(text(f"SELECT count(*) FROM tag_share WHERE {where}"), params).scalar()
    return int(count)


def test_v386_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v385_drop_orphan_tables"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # Head now; still true once v387 revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v386_migration_is_vendor_neutral():
    """The CI seam guard greps for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_v386_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_table(db_session):
    """Drop ``tag_share`` and the ladder must stop matching v386.

    The half that proves the arm is wired to its probe rather than passing for an
    unrelated reason. Asserted as a *band* — at or after v385, strictly before v386 —
    because an exact ``==`` on a lower revision goes red or vacuous the next time the
    ladder above it changes, which is what happened to three of these suites already.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    conn.execute(text("DROP TABLE IF EXISTS tag_share"))
    tables = inspect(conn).get_table_names()
    detected = _detect_schema_version(conn, tables)
    db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v385_drop_orphan_tables") <= order.index(detected) < order.index(REVISION)
    ), (
        f"a database without tag_share was stamped {detected!r}; it must land below "
        f"{REVISION} so the table is actually created"
    )


def test_the_table_has_the_documented_shape(db_session):
    """Set equality, not a subset: it is also how ``permission`` is asserted ABSENT.

    ``collection_share`` has one because a collection carries files you might be allowed to
    change; a tag share grants vocabulary. A column the authorization code would always read
    as "viewer" would be a field pretending to be a choice, so the exact column set is what
    stops one being added without that argument being had again.
    """
    conn = db_session.connection()
    columns = {c["name"]: c for c in inspect(conn).get_columns("tag_share")}

    assert set(columns) == {
        "id",
        "uuid",
        "tag_id",
        "shared_by_id",
        "target_type",
        "target_user_id",
        "target_group_id",
        "created_at",
    }
    assert columns["target_type"]["nullable"] is False
    # Nullable by design: exactly ONE of the two is set, enforced by the CHECK below.
    assert columns["target_user_id"]["nullable"] is True
    assert columns["target_group_id"]["nullable"] is True


@pytest.mark.parametrize("shape", ["neither", "both"])
def test_the_target_check_rejects_a_bad_target(db_session, shape):
    """``_tag_share_target_check``: exactly one target, never both and never neither.

    ``neither`` is a row the authorization query would join to nobody — invisible rather
    than harmless, since it reads as a grant that exists. ``both`` is two grants sharing
    one revocation: deleting it removes access nobody meant to remove.
    """
    conn = db_session.connection()
    user_id, tag_id, group_id = _seed_targets(conn)
    targets = {} if shape == "neither" else {"user": user_id, "group": group_id}

    with pytest.raises(IntegrityError):
        _grant(conn, tag_id, user_id, **targets)
    db_session.rollback()


def test_a_single_target_is_accepted(db_session):
    """The positive half — asserting the constraint's text is not enough (see v380)."""
    conn = db_session.connection()
    user_id, tag_id, group_id = _seed_targets(conn)

    _grant(conn, tag_id, user_id, user=user_id)
    _grant(conn, tag_id, user_id, group=group_id)

    assert _grants(conn, "tag_id = :t", t=tag_id) == 2
    db_session.rollback()


def test_the_unique_indexes_are_partial(db_session):
    """Partial and unique, both. A plain composite unique would not do the job."""
    conn = db_session.connection()
    rows = conn.execute(
        text(
            "SELECT c.relname AS name, i.indisunique AS is_unique, "
            "(i.indpred IS NOT NULL) AS is_partial "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE i.indrelid = 'tag_share'::regclass"
        )
    ).all()
    shapes = {r.name: (r.is_unique, r.is_partial) for r in rows}

    assert shapes["_tag_share_user_uc"] == (True, True)
    assert shapes["_tag_share_group_uc"] == (True, True)


@pytest.mark.parametrize("target", ["user", "group"])
def test_a_duplicate_grant_is_rejected(db_session, target):
    """One grant per (tag, recipient) — both halves, since they are separate indexes."""
    conn = db_session.connection()
    user_id, tag_id, group_id = _seed_targets(conn)
    kwargs = {"user": user_id} if target == "user" else {"group": group_id}

    _grant(conn, tag_id, user_id, **kwargs)
    with pytest.raises(IntegrityError):
        _grant(conn, tag_id, user_id, **kwargs)
    db_session.rollback()


def test_a_composite_unique_would_admit_a_duplicate_group_grant(db_session):
    """The reason the uniques are PARTIAL, demonstrated rather than asserted in a comment.

    Under ``UNIQUE (tag_id, target_user_id, target_group_id)`` two identical group grants
    are ``(t, NULL, g)`` twice, and Postgres treats NULLs as distinct — so the row pair the
    real table rejects above is accepted here. A temp table is used deliberately: it lives
    in a session-private ``pg_temp_*`` schema, so this needs no cross-worker DDL isolation.
    """
    conn = db_session.connection()
    conn.execute(
        text(
            "CREATE TEMP TABLE v386_naive_grant (tag_id INTEGER NOT NULL, "
            "target_user_id INTEGER, target_group_id INTEGER, "
            "UNIQUE (tag_id, target_user_id, target_group_id)) ON COMMIT DROP"
        )
    )
    for _ in range(2):
        conn.execute(
            text(
                "INSERT INTO v386_naive_grant (tag_id, target_user_id, target_group_id) "
                "VALUES (1, NULL, 7)"
            )
        )

    assert conn.execute(text("SELECT count(*) FROM v386_naive_grant")).scalar() == 2
    db_session.rollback()


def test_every_foreign_key_cascades(db_session):
    """A grant must never outlive the tag, the sharer or the recipient.

    A dangling grant is not inert: the authorization path reads grant rows to decide who
    sees a tag, and a row pointing at a deleted recipient id is a grant to whoever next
    receives that id.
    """
    conn = db_session.connection()
    rules = dict(
        conn.execute(
            text(
                "SELECT conname, confdeltype FROM pg_constraint "
                "WHERE conrelid = 'tag_share'::regclass AND contype = 'f'"
            )
        ).all()
    )

    assert len(rules) == 4, f"expected four FKs, found {sorted(rules)}"
    assert set(rules.values()) == {"c"}, f"every FK must be ON DELETE CASCADE ('c'): {rules}"


def test_deleting_the_tag_removes_the_grant(db_session):
    """The CASCADE that matters most — a merged or renamed-away tag leaves no grants."""
    conn = db_session.connection()
    user_id, tag_id, group_id = _seed_targets(conn)
    _grant(conn, tag_id, user_id, group=group_id)

    conn.execute(text("DELETE FROM tag WHERE id = :t"), {"t": tag_id})

    assert _grants(conn, "tag_id = :t", t=tag_id) == 0
    db_session.rollback()


def test_deleting_the_recipient_removes_the_grant(db_session):
    """Account deletion must not leave a grant pointing at a reusable user id."""
    conn = db_session.connection()
    owner_id, tag_id, _ = _seed_targets(conn)
    # A bare user, not another _seed_targets(): `tag.user_id` is a plain FK with no ON
    # DELETE rule, so deleting a user who owns a tag fails on that constraint instead of
    # exercising this one.
    recipient_id = _insert_user(conn)
    _grant(conn, tag_id, owner_id, user=recipient_id)

    conn.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": recipient_id})

    assert _grants(conn, "target_user_id = :u", u=recipient_id) == 0
    db_session.rollback()


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    """The invariant in backend/alembic/CLAUDE.md, and the reason v386 was rewritten.

    ``op.create_table`` emits a bare ``CREATE TABLE``: re-run against a database that
    already has the table it raises ``DuplicateTable``, the runner calls ``SystemExit(1)``,
    and the backend does not start. Replaying the real SQL twice is the only assertion that
    would have caught it — the source-text checks the other suites use all passed.
    """
    module = _revision_module()
    conn = db_session.connection()

    conn.execute(text(module.UPGRADE_SQL))
    conn.execute(text(module.UPGRADE_SQL))

    assert (
        conn.execute(
            text("SELECT count(*) FROM pg_constraint WHERE conname = '_tag_share_target_check'")
        ).scalar()
        == 1
    )
    indexes = {ix["name"] for ix in inspect(conn).get_indexes("tag_share")}
    assert set(_INDEXES) <= indexes
    db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_really_runs_and_the_upgrade_restores_it(db_session):
    """Execute ``downgrade()`` and ``upgrade()``, rather than grepping their source.

    Every other "downgrade mirrors the upgrade" test in this directory reads the revision's
    source text, so a downgrade that referenced a misspelled index, or dropped the wrong
    object, would pass. This calls the functions through alembic's own ``op``.

    Run twice on the way down, which is the other half of the same barrier's cost: every
    statement is ``DROP … IF EXISTS``, so a database that died mid-downgrade recovers. Folded
    into this test rather than given its own, because each ``ddl_exclusive`` test drains
    every other xdist worker and one drain covers both claims.
    """
    module = _revision_module()
    conn = db_session.connection()

    with _alembic_operations(conn):
        module.downgrade()
        module.downgrade()

    assert not inspect(conn).has_table("tag_share"), "downgrade left the table behind"
    leftover = [
        name
        for name in _INDEXES
        if conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar()
    ]
    assert leftover == [], f"downgrade left indexes behind: {leftover}"

    with _alembic_operations(conn):
        module.upgrade()

    assert inspect(conn).has_table("tag_share"), "the pair does not round-trip"
    db_session.rollback()
