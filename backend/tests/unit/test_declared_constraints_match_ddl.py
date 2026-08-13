"""Every constraint declared on a model must match the object Postgres actually has.

``test_orm_ddl_divergence.py`` answers "is anything the database enforces missing from
Python?" — a whole-schema diff, deliberately name-blind for uniqueness and deliberately
silent about two properties: **deferrability** and **FK ``ON DELETE``**. That silence is a
real gap, because both change *behaviour* rather than existence:

* a ``UniqueConstraint`` declared without ``deferrable=``/``initially=`` describes a rule
  that fires at ``flush()``, while ``user_pki_cert_unique`` (v070) is
  ``DEFERRABLE INITIALLY DEFERRED`` and fires at **COMMIT** — a different and much more
  confusing failure point, since the statement that caused it returned long ago;
* a ``ForeignKey`` declared without ``ondelete=`` tells SQLAlchemy it must null out or
  delete children itself, while ``file_tag``'s two FKs are ``ON DELETE CASCADE`` and the
  database has already done it.

So this module is the *inventory*: one hand-written entry per object declared as part of
the ORM↔DDL visibility fix, naming the exact rule, and asserted three ways — the
expectation written here, the object in ``Base.metadata``, and the row in
``pg_constraint``/``pg_index``. Two of the three agreeing is not enough; a hand-written
expectation that matched nothing on either side would pass a two-way comparison of the
other two, and the whole reason these constraints went undeclared for years is that
nothing ever compared them to anything.

Expression and predicate text is compared through ``_orm_ddl_divergence.normalise`` rather
than a second copy of that logic: Postgres reprints ``lower(claim_value)`` as
``lower((claim_value)::text)`` and ``source = 'ldap'`` as ``(source)::text = 'ldap'::text``,
and a comparison that did not collapse casts would fail on correct declarations.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import Index
from sqlalchemy import UniqueConstraint
from sqlalchemy import text

from app.auth.constants import VALID_AUTH_TYPES
from app.db.base import Base
from tests.unit import _orm_ddl_divergence as derive


@dataclass(frozen=True)
class UniqueDecl:
    """A ``UniqueConstraint`` the database holds in ``pg_constraint``."""

    name: str
    table: str
    columns: tuple[str, ...]
    deferred: bool = False


@dataclass(frozen=True)
class IndexDecl:
    """A unique ``Index`` the database holds in ``pg_index`` — no ``pg_constraint`` row.

    ``keys`` are written the way the *declaration* spells them; both sides go through
    ``normalise`` so Postgres's cast-laden reprint compares equal.
    """

    name: str
    table: str
    keys: tuple[str, ...]
    predicate: str | None = None


@dataclass(frozen=True)
class CheckDecl:
    """A named CHECK. ``values`` is the literal set it admits — empty for boolean logic."""

    name: str
    table: str
    values: frozenset[str] = field(default_factory=frozenset)


#: §A — plain UNIQUE constraints. ``user_pki_cert_unique`` is the only deferred one in the
#: whole schema (``test_schema_constraint_rejections.py`` pins that from the DB side).
UNIQUE_CONSTRAINTS = [
    UniqueDecl(
        "speaker_user_id_media_file_id_name_key", "speaker", ("user_id", "media_file_id", "name")
    ),
    UniqueDecl(
        "speaker_match_speaker1_id_speaker2_id_key", "speaker_match", ("speaker1_id", "speaker2_id")
    ),
    UniqueDecl("topic_suggestion_media_file_id_key", "topic_suggestion", ("media_file_id",)),
    UniqueDecl("user_setting_user_id_setting_key_key", "user_setting", ("user_id", "setting_key")),
    UniqueDecl(
        "user_pki_cert_unique", "user", ("pki_serial_number", "pki_issuer_dn"), deferred=True
    ),
]

#: §C + §E — unique INDEXES. Expression, partial, or (the last two) created by ``v366``
#: with ``CREATE UNIQUE INDEX`` where the model used to say ``UniqueConstraint``.
UNIQUE_INDEXES = [
    IndexDecl(
        "uq_transcript_segment_content",
        "transcript_segment",
        ("media_file_id", "start_time", "end_time", "md5(text)"),
    ),
    IndexDecl(
        "uq_group_mapping_ldap_claim_ci",
        "group_mapping",
        ("lower(claim_value)",),
        predicate="source = 'ldap'",
    ),
    IndexDecl(
        "unique_system_default_per_content_type",
        "summary_prompt",
        ("content_type",),
        predicate="is_system_default = true",
    ),
    IndexDecl(
        "uq_usage_event_idempotency_key",
        "usage_event",
        ("idempotency_key",),
        predicate="idempotency_key IS NOT NULL",
    ),
    IndexDecl("uq_user_external_id", "user", ("external_id",), predicate="external_id IS NOT NULL"),
    IndexDecl(
        "_custom_vocab_unique", "custom_vocabulary", ("COALESCE(user_id, 0)", "term", "domain")
    ),
    IndexDecl(
        "_watch_source_file_path_unique", "watch_source_file", ("watch_source_id", "remote_path")
    ),
    IndexDecl(
        "_watch_source_email_unique", "watch_source_email", ("watch_source_id", "email_config_id")
    ),
]

#: §B — named CHECKs. ``speaker_match_check`` is boolean logic (``speaker1_id <
#: speaker2_id``) and carries no literals, so its entry asserts existence and body shape
#: only; every other one is an enum whose value set IS its enforcement content.
CHECK_CONSTRAINTS = [
    CheckDecl(
        "_collection_share_permission_check", "collection_share", frozenset({"viewer", "editor"})
    ),
    CheckDecl(
        "_collection_share_target_type_check", "collection_share", frozenset({"user", "group"})
    ),
    CheckDecl(
        "_user_group_member_role_check",
        "user_group_member",
        frozenset({"owner", "admin", "member"}),
    ),
    CheckDecl("speaker_match_check", "speaker_match"),
    CheckDecl("ck_user_role_valid", "user", frozenset({"user", "admin", "super_admin"})),
    CheckDecl("ck_user_superuser_matches_role", "user", frozenset({"super_admin"})),
    CheckDecl(
        "ck_user_auth_type_valid",
        "user",
        frozenset({"local", "ldap", "oidc", "pki", "proxy", "saml"}),
    ),
    CheckDecl(
        "ck_user_approval_status_valid", "user", frozenset({"pending", "approved", "rejected"})
    ),
    CheckDecl(
        "ck_user_invitation_role_valid",
        "user_invitation",
        frozenset({"user", "admin", "super_admin"}),
    ),
    CheckDecl(
        "ck_user_invitation_auth_type_valid",
        "user_invitation",
        frozenset({"local", "ldap", "oidc", "pki", "proxy", "saml"}),
    ),
]

#: §D — the only two FKs in the schema whose ``ON DELETE`` the ORM did not mirror.
#: ``confdeltype`` is a one-char code; ``c`` is CASCADE, ``a`` (the default) is NO ACTION.
CASCADE_FKS = [
    ("file_tag", "file_tag_media_file_id_fkey", "media_file_id"),
    ("file_tag", "file_tag_tag_id_fkey", "tag_id"),
]


def _orm_table(name: str):
    return Base.metadata.tables[name]


def _orm_constraint(table_name: str, constraint_name: str, kind):
    matches = [
        c
        for c in _orm_table(table_name).constraints
        if isinstance(c, kind) and c.name == constraint_name
    ]
    assert len(matches) == 1, (
        f"{table_name} must declare exactly one {kind.__name__} named {constraint_name!r}; "
        f"found {len(matches)}. The database has it — see the module docstring."
    )
    return matches[0]


def _orm_index(table_name: str, index_name: str) -> Index:
    matches: list[Index] = [i for i in _orm_table(table_name).indexes if i.name == index_name]
    assert len(matches) == 1, (
        f"{table_name} must declare exactly one Index named {index_name!r}; found {len(matches)}"
    )
    return matches[0]


def _pg_constraint(conn, table_name: str, constraint_name: str):
    row = conn.execute(
        text(
            "SELECT con.contype, con.condeferrable, con.condeferred, "
            "       pg_get_constraintdef(con.oid) "
            "  FROM pg_constraint con "
            "  JOIN pg_class cl ON cl.oid = con.conrelid "
            "  JOIN pg_namespace n ON n.oid = cl.relnamespace "
            " WHERE n.nspname = 'public' AND cl.relname = :t AND con.conname = :c"
        ),
        {"t": table_name, "c": constraint_name},
    ).one_or_none()
    assert row is not None, (
        f"pg_constraint has no {constraint_name!r} on {table_name}. Either the migration "
        "that created it was reverted, or this inventory names an object that never existed "
        "— in which case every assertion keyed on it has been passing vacuously."
    )
    return row


def _pg_index(conn, table_name: str, index_name: str):
    row = conn.execute(
        text(
            "SELECT ix.indisunique, "
            "       (SELECT array_agg(pg_get_indexdef(ix.indexrelid, k, true) ORDER BY k) "
            "          FROM generate_series(1, ix.indnkeyatts) AS k), "
            "       pg_get_expr(ix.indpred, ix.indrelid) "
            "  FROM pg_index ix "
            "  JOIN pg_class cl ON cl.oid = ix.indrelid "
            "  JOIN pg_class i ON i.oid = ix.indexrelid "
            "  JOIN pg_namespace n ON n.oid = cl.relnamespace "
            " WHERE n.nspname = 'public' AND cl.relname = :t AND i.relname = :x"
        ),
        {"t": table_name, "x": index_name},
    ).one_or_none()
    assert row is not None, f"pg_index has no {index_name!r} on {table_name}"
    return row


# ------------------------------------------------------------------ §A unique constraints


@pytest.mark.parametrize("decl", UNIQUE_CONSTRAINTS, ids=lambda d: d.name)
def test_a_declared_unique_constraint_matches_pg_constraint(decl: UniqueDecl, db_session):
    orm = _orm_constraint(decl.table, decl.name, UniqueConstraint)
    assert tuple(c.name for c in orm.columns) == decl.columns

    contype, condeferrable, condeferred, definition = _pg_constraint(
        db_session.connection(), decl.table, decl.name
    )
    assert contype == "u", f"{decl.name} is not a UNIQUE constraint in the database: {definition}"
    db_columns = tuple(
        part.strip().strip('"')
        for part in definition[definition.index("(") + 1 : definition.index(")")].split(",")
    )
    assert db_columns == decl.columns, (
        f"{decl.name} spans {db_columns} in the database, {decl.columns} on the model"
    )
    assert (condeferrable, condeferred) == (decl.deferred, decl.deferred)
    assert (bool(orm.deferrable), orm.initially == "DEFERRED" if orm.initially else False) == (
        decl.deferred,
        decl.deferred,
    ), (
        f"{decl.name}: the model says deferrable={orm.deferrable!r} initially={orm.initially!r}, "
        f"the database says condeferrable={condeferrable} condeferred={condeferred}. A "
        "deferred constraint fails at COMMIT, not at flush — declaring it without "
        'deferrable=True, initially="DEFERRED" moves the failure."'
    )


def test_the_pki_certificate_constraint_is_the_deferred_one_on_both_sides(db_session):
    """The trap, asserted on its own rather than only inside a parametrised sweep.

    ``deferrable``/``initially`` are the two kwargs most likely to be dropped as noise by a
    later edit, and the whole parametrised test above would still pass for the other four
    entries if this one silently lost them — so the property gets a named test whose failure
    message says what breaks.
    """
    orm = _orm_constraint("user", "user_pki_cert_unique", UniqueConstraint)
    assert orm.deferrable is True
    assert orm.initially == "DEFERRED"

    deferred_in_db = {
        row[0]
        for row in db_session.connection().execute(
            text(
                "SELECT con.conname FROM pg_constraint con "
                "  JOIN pg_namespace n ON n.oid = con.connamespace "
                " WHERE n.nspname = 'public' AND con.condeferred"
            )
        )
    }
    assert deferred_in_db == {"user_pki_cert_unique"}


# ------------------------------------------------------------------- §C/§E unique indexes


@pytest.mark.parametrize("decl", UNIQUE_INDEXES, ids=lambda d: d.name)
def test_a_declared_unique_index_matches_pg_index(decl: IndexDecl, db_session):
    orm = _orm_index(decl.table, decl.name)
    assert orm.unique is True, f"{decl.name} must be declared unique=True"

    expected_keys = tuple(derive.normalise(k) for k in decl.keys)
    orm_keys = tuple(
        derive.normalise(getattr(expr, "name", None) or expr) for expr in orm.expressions
    )
    assert orm_keys == expected_keys, (
        f"{decl.name}: the model indexes {orm_keys}, this inventory expects {expected_keys}"
    )
    orm_predicate = derive.normalise(orm.dialect_kwargs.get("postgresql_where"))
    assert orm_predicate == derive.normalise(decl.predicate)

    indisunique, db_keys, db_predicate = _pg_index(db_session.connection(), decl.table, decl.name)
    assert indisunique is True
    assert tuple(derive.normalise(k) for k in db_keys) == expected_keys, (
        f"{decl.name}: the database indexes {db_keys}, this inventory expects {list(decl.keys)}"
    )
    assert derive.normalise(db_predicate) == derive.normalise(decl.predicate), (
        f"{decl.name}: the database predicate is {db_predicate!r}, this inventory expects "
        f"{decl.predicate!r}. A partial unique index and a total one are different objects."
    )


@pytest.mark.parametrize(
    ("table", "name"),
    [("usage_event", "idempotency_key"), ("user", "external_id")],
)
def test_a_partially_unique_column_carries_no_total_unique_declaration(table, name, db_session):
    """The shape disagreement, from the side the model used to get wrong.

    Both columns carried ``unique=True``, which declares a TOTAL unique object. The database
    has a PARTIAL one in each case, and the two are different objects even though they
    enforce the same thing while the column is nullable. Asserting only that the partial
    index is declared would not catch a re-added ``unique=True`` beside it.
    """
    column = _orm_table(table).columns[name]
    assert column.unique in (None, False), (
        f"{table}.{name} declares a total unique= constraint; the database's rule is partial"
    )
    total = [
        obj
        for obj in list(_orm_table(table).constraints) + list(_orm_table(table).indexes)
        if isinstance(obj, (UniqueConstraint, Index))
        and getattr(obj, "unique", True)
        and [c.name for c in obj.columns] == [name]
        # `is None`, not falsiness: a SQLAlchemy clause raises TypeError on bool().
        and obj.dialect_kwargs.get("postgresql_where") is None
    ]
    assert not total, f"{table}.{name} still carries a total unique object: {total}"


# --------------------------------------------------------------------------- §B CHECKs


@pytest.mark.parametrize("decl", CHECK_CONSTRAINTS, ids=lambda d: d.name)
def test_a_declared_check_admits_exactly_what_the_database_admits(decl: CheckDecl, db_session):
    orm = _orm_constraint(decl.table, decl.name, CheckConstraint)
    contype, _, _, definition = _pg_constraint(db_session.connection(), decl.table, decl.name)
    assert contype == "c"

    db_values = derive.literals(definition)
    orm_values = derive.literals(str(orm.sqltext))
    assert db_values == decl.values, (
        f"{decl.name}: the database admits {sorted(db_values)}, this inventory says "
        f"{sorted(decl.values)} — the DDL changed and nothing else noticed"
    )
    assert orm_values == decl.values, (
        f"{decl.name}: the model admits {sorted(orm_values)}, the database admits "
        f"{sorted(db_values)}"
    )


def test_the_auth_type_check_is_a_superset_of_the_code_and_not_derived_from_it(db_session):
    """``auth/constants.py`` requires containment, not equality — in that direction only.

    A value must be able to exist in the DDL before any code supports it, which is how a
    widening migration ships ahead of its provider. So the CHECK body on ``user`` and
    ``user_invitation`` is written as a literal string; building it from
    ``VALID_AUTH_TYPES`` the way ``group.py`` builds its bodies from
    ``MEMBERSHIP_SOURCES_SQL`` would encode equality and make that ordering illegal. The
    assertion here is therefore ``<=``, deliberately not ``==``: this test must keep passing
    on the day the database admits a seventh value.
    """
    conn = db_session.connection()
    supported = set(VALID_AUTH_TYPES)
    for table, name in (
        ("user", "ck_user_auth_type_valid"),
        ("user_invitation", "ck_user_invitation_auth_type_valid"),
    ):
        _, _, _, definition = _pg_constraint(conn, table, name)
        orm = _orm_constraint(table, name, CheckConstraint)
        assert supported <= derive.literals(definition), (
            f"{name} does not admit every value in VALID_AUTH_TYPES — every login with a "
            "missing value is refused by the database"
        )
        assert supported <= derive.literals(str(orm.sqltext)), (
            f"the {name} body declared on the model does not admit every value in VALID_AUTH_TYPES"
        )


# ------------------------------------------------------------------------ §D FK ON DELETE


@pytest.mark.parametrize(("table", "fk_name", "column"), CASCADE_FKS, ids=lambda v: str(v))
def test_a_cascading_foreign_key_declares_the_ondelete_the_database_has(
    table, fk_name, column, db_session
):
    declared = [fk for fk in _orm_table(table).foreign_keys if fk.parent.name == column]
    assert len(declared) == 1, f"{table}.{column} must have exactly one ForeignKey"
    assert declared[0].ondelete == "CASCADE", (
        f"{table}.{column} is ON DELETE CASCADE in the database but the model declares "
        f"ondelete={declared[0].ondelete!r}, so the ORM believes it must delete these rows "
        "itself"
    )

    confdeltype = (
        db_session.connection()
        .execute(
            text(
                "SELECT con.confdeltype FROM pg_constraint con "
                "  JOIN pg_class cl ON cl.oid = con.conrelid "
                "  JOIN pg_namespace n ON n.oid = cl.relnamespace "
                " WHERE n.nspname = 'public' AND cl.relname = :t AND con.conname = :c"
            ),
            {"t": table, "c": fk_name},
        )
        .scalar_one()
    )
    assert confdeltype == "c", (
        f"{fk_name} is not ON DELETE CASCADE in the database (confdeltype={confdeltype!r})"
    )


# ------------------------------------------------------------------- guard the inventory


def test_the_catalog_lookups_would_notice_an_object_that_is_not_there(db_session):
    """A helper that silently returned nothing would make every test above vacuous.

    ``_pg_constraint``/``_pg_index`` assert on ``one_or_none()``, so a typo'd name has to
    fail rather than return an empty match — but that is only true if the assertion inside
    them actually runs. Both are driven with a name that cannot exist.
    """
    conn = db_session.connection()
    with pytest.raises(AssertionError, match="no 'ck_no_such_constraint'"):
        _pg_constraint(conn, "user", "ck_no_such_constraint")
    with pytest.raises(AssertionError, match="no 'ix_no_such_index'"):
        _pg_index(conn, "user", "ix_no_such_index")


def test_the_orm_lookups_would_notice_a_declaration_that_is_not_there(db_session):
    """The same guard for the model side, which is the side these tests exist to police."""
    with pytest.raises(AssertionError, match="exactly one CheckConstraint"):
        _orm_constraint("user", "ck_not_declared_anywhere", CheckConstraint)
    with pytest.raises(AssertionError, match="exactly one Index"):
        _orm_index("user", "ix_not_declared_anywhere")


def test_the_inventory_covers_every_object_the_visibility_fix_declared(db_session):
    """25 objects, counted, so a silently deleted entry is a failure rather than a shorter run.

    A parametrised sweep over a list cannot report entries that were removed from the list.
    The count is asserted against the divergence catalogue this work came from: 5 UNIQUE
    constraints + 8 unique indexes (6 expression/partial + the 2 that v366 created as
    indexes where the model said UniqueConstraint) + 10 CHECKs + 2 FKs.
    """
    assert len(UNIQUE_CONSTRAINTS) == 5
    assert len(UNIQUE_INDEXES) == 8
    assert len(CHECK_CONSTRAINTS) == 10
    assert len(CASCADE_FKS) == 2
    names = (
        [d.name for d in UNIQUE_CONSTRAINTS]
        + [d.name for d in UNIQUE_INDEXES]
        + [d.name for d in CHECK_CONSTRAINTS]
        + [fk[1] for fk in CASCADE_FKS]
    )
    assert len(names) == len(set(names)), "duplicate entry in the inventory"


def test_no_declared_foreign_key_constraint_object_shadows_the_column_level_ones(db_session):
    """``file_tag``'s FKs are column-level, so a table-level duplicate would be a second rule.

    Cheap, but it is the failure mode a later "let's declare it properly" edit produces: a
    ``ForeignKeyConstraint`` in ``__table_args__`` alongside the ``ForeignKey`` on the
    column, which SQLAlchemy renders as two constraints where the database has one.
    """
    table_level = [
        c for c in _orm_table("file_tag").constraints if isinstance(c, ForeignKeyConstraint)
    ]
    assert len(table_level) == 2, (
        "file_tag should carry exactly the two FK constraints SQLAlchemy derives from its "
        f"column-level ForeignKeys; found {[c.name for c in table_level]}"
    )
