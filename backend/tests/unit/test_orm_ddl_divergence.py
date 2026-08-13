"""A constraint the database enforces but Python never declares is invisible until it fires.

``transcript_segment`` carries ``uq_transcript_segment_content`` =
``UNIQUE(media_file_id, start_time, end_time, md5(text))``. It exists in the database. It
is declared **nowhere in Python** — not on the model, not in ``__table_args__`` — so it is
invisible to the ORM, to mypy, and to anyone reading ``app/models/media.py``. It surfaced
for the first time in its life as a runtime ``IntegrityError`` that aborted an entire
eval-corpus injection run: a turn-per-segment corpus collides on it where ASR-produced
segments never do. ``rg uq_transcript_segment_content`` over the whole tree finds two hits,
both prose — no test had ever exercised it.

Expression indexes are exactly the ones SQLAlchemy cannot always express declaratively, so
"hard to declare" quietly became "not declared", and the omission is invisible by
construction. It was not the only one: this module was written measuring **23**
disagreements, including ``UNIQUE(user_id, setting_key)`` on ``user_setting``, whose
``__table_args__`` in ``app/models/prompt.py`` carried the comment "Ensure unique setting
keys per user" above nothing but ``{"extend_existing": True}`` — the declaration was
intended and lost.

**All 23 are now declared** and ``_ALLOWLIST`` is empty; the per-object inventory of what
each declaration must match, including the two properties this module deliberately does not
compare (deferrability and FK ``ON DELETE``), lives in
``tests/unit/test_declared_constraints_match_ddl.py``. An empty allowlist is the point of
the gate, not the end of it: the next constraint added in raw SQL and not on a model lands
here as a failure.

Both sides are derived mechanically, never hand-copied: the database side from
``pg_index``/``pg_constraint`` against the live migrated schema, the ORM side from
``Base.metadata``. The derivation and the diff live in ``_orm_ddl_divergence.py``, which
documents why uniqueness is compared *semantically* and CHECKs *by name*.

Relationship to the neighbours: ``test_schema_drift.py`` gates tables and columns and
deliberately does not gate constraints (naming noise made it a 395-entry changelog);
``test_schema_constraint_rejections.py`` proves a constraint *rejects* what it should;
this module proves the constraint is *visible in Python at all*. A rule can pass the other
two and still be a trap for the next writer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column
from sqlalchemy import Float
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import text

from tests.unit import _orm_ddl_divergence as derive

#: The known disagreements, ``<table>::<object>::<category>`` → written reason.
#:
#: A reason beginning ``BACKLOG`` is deferred work: the rule *is* expressible in SQLAlchemy
#: and the entry only records that nobody has done it yet. ``ACCEPTED`` is the other legal
#: prefix and means the divergence is a design decision. Every entry needs one or the
#: other, and a **stale** entry — one whose finding is gone — fails the run, so the list can
#: only shrink deliberately. Declaring one of these on its model is the fix; deleting its
#: line here is part of that same commit.
#:
#: **It is empty**, and that is a measured state rather than a starting one: the 23 entries
#: this module shipped with — nine DB-only unique rules (three of them expression indexes,
#: which an ``Index`` taking a ``text()`` key expresses perfectly well), two shape
#: disagreements reported from both sides, and ten DB-only CHECKs — were each declared on
#: their model in one commit that deleted this list line by line, with
#: ``alembic.autogenerate.compare_metadata`` measured before and after to prove the
#: declarations added no operation in either direction.
_ALLOWLIST: dict[str, str] = {}

_LEGAL_PREFIXES = ("BACKLOG", "ACCEPTED")

#: The only ``public`` table with no model. Named rather than filtered silently: table-level
#: drift belongs to ``test_schema_drift.py``, but if a second orphan table appears the
#: constraints it carries would be dropped from this diff without a word.
_TABLES_WITHOUT_A_MODEL = frozenset({"alembic_version"})


def _sides(conn):
    """Both rule sets plus the ORM's table list, derived from live sources."""
    from app.db.base import Base

    return {
        "db_uniques": derive.db_unique_rules(conn),
        "orm_uniques": derive.orm_unique_rules(Base.metadata),
        "db_checks": derive.db_check_constraints(conn),
        "orm_checks": derive.orm_check_constraints(Base.metadata),
        "orm_table_names": frozenset(Base.metadata.tables),
    }


def _divergences(conn) -> list[derive.Divergence]:
    return derive.find_divergences(**_sides(conn))


def _render(items: list[derive.Divergence]) -> str:
    return "\n  ".join(f"{d.key}\n      {d.detail}" for d in items)


# ----------------------------------------------------------------------- the gate


def test_no_undeclared_database_constraint_is_unaccounted_for(db_session):
    """A new divergence must be declared on the model or admitted here in writing.

    This is the whole point: an ``IntegrityError`` from a rule with no Python declaration
    reaches a developer as a 500 from a constraint name they have never seen. Adding the
    declaration is a *visibility* change — it must add and drop no DDL — so the cost of
    clearing a finding is a line in a model and a line deleted from ``_ALLOWLIST``.
    """
    unaccounted = [d for d in _divergences(db_session.connection()) if d.key not in _ALLOWLIST]
    assert not unaccounted, (
        f"{len(unaccounted)} constraint(s) disagree between the database and "
        "Base.metadata and are not in _ALLOWLIST. Declare them on the model (and prove "
        "`alembic revision --autogenerate` adds and drops nothing), or add an entry with a "
        "written reason:\n  " + _render(unaccounted)
    )


def test_the_allowlist_is_honest(db_session):
    """A stale entry reads as a deliberate exemption while exempting nothing.

    Written as one test rather than a ``parametrize`` over the allowlist so the empty case
    — the goal — passes instead of reporting a permanent skip.
    """
    live = {d.key for d in _divergences(db_session.connection())}
    stale = sorted(key for key in _ALLOWLIST if key not in live)
    unexplained = sorted(key for key, reason in _ALLOWLIST.items() if not reason.strip())
    unclassified = sorted(
        key for key, reason in _ALLOWLIST.items() if not reason.strip().startswith(_LEGAL_PREFIXES)
    )

    assert not stale, (
        "these divergences no longer exist — delete their lines in the same commit that "
        f"declared them: {stale}"
    )
    assert not unexplained, f"allowlist entries need a written reason: {unexplained}"
    assert not unclassified, (
        "every reason must begin BACKLOG (deferred, declarable) or ACCEPTED (a design "
        f"decision), so a green run is never read as a clean tree: {unclassified}"
    )


def test_the_diff_covers_every_table_that_has_a_model(db_session):
    """The scoping assumption, asserted instead of assumed.

    ``find_divergences`` ignores constraints on tables absent from ``Base.metadata``,
    because a table with no model at all is ``test_schema_drift.py``'s finding. That is
    only safe while the excluded set is what it is here.
    """
    conn = db_session.connection()
    from app.db.base import Base

    orphans = derive.db_tables(conn) - set(Base.metadata.tables)
    assert orphans == _TABLES_WITHOUT_A_MODEL, (
        "the set of tables excluded from this diff changed. A new orphan table's "
        f"constraints are being silently dropped from the comparison: {sorted(orphans)}"
    )


# ------------------------------------------------------------- guard the guard


#: The finding this module was written for. Now cleared — ``app/models/media.py`` declares
#: the index — which is exactly why the two guards below have to work by *withdrawing* the
#: declaration rather than by observing the finding.
_MD5_FINDING = "transcript_segment::uq_transcript_segment_content::db-only-unique"


def _md5_rules(sides) -> set:
    """The ORM-side rule keys covering ``transcript_segment``'s md5 expression index."""
    return {
        rule
        for rule, names in sides["orm_uniques"].items()
        if rule[0] == "transcript_segment" and "uq_transcript_segment_content" in names
    }


def test_the_detector_still_finds_the_constraint_that_caused_this_module(db_session):
    """Withdraw the declaration and ``uq_transcript_segment_content`` must be reported again.

    Every assertion above compares against ``find_divergences``. A derivation that matched
    nothing would report zero findings, which is indistinguishable from a clean tree — and
    that is not a hypothetical: it is how two detectors in ``scripts/audit-tests.py`` and
    two in the frontend auditor were found dead. This guard used to assert the finding was
    *present*, which stopped being a must-fire case the moment somebody did the work; it now
    asserts the detector would report it the moment the declaration is deleted, which is the
    property that actually needs protecting.
    """
    conn = db_session.connection()
    sides = _sides(conn)
    declared = _md5_rules(sides)
    assert len(declared) == 1, (
        "app/models/media.py must declare exactly one uq_transcript_segment_content rule; "
        f"got {declared}"
    )
    assert _MD5_FINDING not in {d.key for d in derive.find_divergences(**sides)}

    sides["orm_uniques"] = {
        rule: names for rule, names in sides["orm_uniques"].items() if rule not in declared
    }
    assert _MD5_FINDING in {d.key for d in derive.find_divergences(**sides)}


def test_the_matcher_matches_the_md5_index_rather_than_missing_it_on_both_sides(db_session):
    """The matcher must MATCH an expression index, not agree by being blind to it twice.

    A normaliser that mangled ``md5(text)`` differently on each side would have reported the
    finding for the wrong reason and gone on reporting it after somebody fixed the model — a
    permanently red gate proving nothing. The converse failure is the one that matters now
    that the model declares it: a derivation returning *nothing* for an expression index on
    both sides also produces no finding, and looks identical to a match. So the rule key is
    asserted to exist on both sides and to be the same key, and an independently built
    declaration (the real ``Index`` API over a throwaway ``MetaData``) must produce that
    identical key.
    """
    conn = db_session.connection()
    sides = _sides(conn)

    declared = _md5_rules(sides)
    assert len(declared) == 1
    rule = declared.pop()
    assert "md5" in "".join(rule[1]), f"the md5 expression was lost in normalisation: {rule}"
    assert rule in sides["db_uniques"], (
        "the ORM rule for uq_transcript_segment_content does not appear in the database "
        f"rule set — the two sides normalise differently. ORM: {rule}"
    )
    assert sides["db_uniques"][rule] == ["uq_transcript_segment_content"]

    simulated = MetaData()
    segment = Table(
        "transcript_segment",
        simulated,
        Column("id", Integer, primary_key=True),
        Column("media_file_id", Integer),
        Column("start_time", Float),
        Column("end_time", Float),
        Column("text", String),
    )
    Index(
        "uq_transcript_segment_content",
        segment.c.media_file_id,
        segment.c.start_time,
        segment.c.end_time,
        text("md5(text)"),
        unique=True,
    )
    assert rule in derive.orm_unique_rules(simulated)


def test_removing_a_correctly_declared_constraint_makes_it_a_finding(db_session):
    """The converse: the matcher must not be matching everything vacuously.

    ``_group_member_uc`` is declared on ``app/models/group.py`` and enforced by the
    database, so it is absent from the findings. Drop it from the ORM side and it has to
    appear — otherwise ``find_divergences`` returning a short list would say nothing about
    the 85 rules it claims to have matched.
    """
    expected = "user_group_member::_group_member_uc::db-only-unique"
    conn = db_session.connection()
    sides = _sides(conn)
    declared = {
        rule
        for rule, names in sides["orm_uniques"].items()
        if "_group_member_uc" in names and rule[0] == "user_group_member"
    }
    assert len(declared) == 1, f"expected exactly one _group_member_uc rule, got {declared}"
    assert expected not in {d.key for d in derive.find_divergences(**sides)}

    sides["orm_uniques"] = {
        rule: names for rule, names in sides["orm_uniques"].items() if rule not in declared
    }
    assert expected in {d.key for d in derive.find_divergences(**sides)}


def test_the_orm_side_reads_column_level_unique_declarations(db_session):
    """``unique=True`` on a column, with and without ``index=True``, land in different places.

    Without ``index=True`` SQLAlchemy emits an anonymous ``UniqueConstraint``; with it, a
    unique ``Index``. A derivation reading only ``table.constraints`` would miss every
    ``uuid`` column in the schema and report ~40 false divergences; one reading only
    ``table.indexes`` would miss ``analytics.media_file_id``. Both spellings are asserted
    because both are in use.
    """
    from app.db.base import Base

    rules = derive.orm_unique_rules(Base.metadata)
    assert ("analytics", ("media_file_id",), None) in rules, "bare unique=True was not read"
    assert ("analytics", ("uuid",), None) in rules, "unique=True + index=True was not read"


@pytest.mark.ddl_exclusive
def test_the_database_side_sees_a_constraint_created_after_import(db_session):
    """Prove the pg_catalog queries are live rather than a snapshot that happens to agree.

    A fresh table is used rather than an ALTER on a real one so nothing else in the schema
    is touched, and the ``finally`` rollback is not optional: this runs against the SHARED
    dev database, and a leaked probe table would show up as a permanent finding for every
    later run. ``ddl_exclusive`` makes ``db_session`` take the advisory lock EXCLUSIVE, so
    the CREATE cannot deadlock against another xdist worker's DML.
    """
    conn = db_session.connection()
    try:
        conn.execute(
            text(
                "CREATE TABLE _orm_ddl_divergence_probe ("
                "  id integer PRIMARY KEY,"
                "  a integer,"
                "  b text,"
                "  CONSTRAINT uq_orm_ddl_divergence_probe UNIQUE (a, b),"
                "  CONSTRAINT ck_orm_ddl_divergence_probe CHECK (a > 0))"
            )
        )
        uniques = derive.db_unique_rules(conn)
        checks = derive.db_check_constraints(conn)
    finally:
        # finally, not a trailing call: an assertion above must not be able to leave DDL
        # behind in the shared dev schema.
        db_session.rollback()

    assert uniques[("_orm_ddl_divergence_probe", ("a", "b"), None)] == [
        "uq_orm_ddl_divergence_probe"
    ]
    assert ("_orm_ddl_divergence_probe", "ck_orm_ddl_divergence_probe") in checks
    # And the rollback is asserted, not trusted: a savepoint that failed to undo the CREATE
    # would leave a table nothing owns in the live dev database, whose only symptom would be
    # this suite reporting a permanent divergence for it.
    assert "_orm_ddl_divergence_probe" not in derive.db_tables(db_session.connection())
