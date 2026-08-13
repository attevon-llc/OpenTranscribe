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
construction. It is not the only one: this module measures **23** disagreements, including
``UNIQUE(user_id, setting_key)`` on ``user_setting``, whose ``__table_args__`` in
``app/models/prompt.py`` still carries the comment "Ensure unique setting keys per user"
above nothing but ``{"extend_existing": True}`` — the declaration was intended and lost.

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
#: There is currently no ``ACCEPTED`` entry. That is the honest state: every one of these
#: 23 can be written down, including the three expression indexes (an ``Index`` taking a
#: ``text()`` key expresses ``md5(text)``, ``lower(claim_value)`` and ``COALESCE(user_id, 0)``
#: perfectly well) and both partial indexes.
_ALLOWLIST: dict[str, str] = {
    # ---------------------------------------------------------- uniqueness (DB only)
    "transcript_segment::uq_transcript_segment_content::db-only-unique": (
        "BACKLOG: the constraint this module exists for. UNIQUE(media_file_id, start_time, "
        "end_time, md5(text)), added by v071 as a real constraint and swapped to a "
        "functional unique index by v353 — which is why app/db/migrations.py's v071/v073 "
        "detection arms probe pg_constraint for it and find nothing on a modern schema "
        "(correct: those arms describe the schema as it was before v353). Any writer "
        "inserting segments that are not ASR output can collide on it."
    ),
    "user_setting::user_setting_user_id_setting_key_key::db-only-unique": (
        "BACKLOG: UNIQUE(user_id, setting_key). app/models/prompt.py's __table_args__ holds "
        "the comment 'Ensure unique setting keys per user' followed only by "
        "{'extend_existing': True} — the declaration was intended and never written."
    ),
    "speaker::speaker_user_id_media_file_id_name_key::db-only-unique": (
        "BACKLOG: plain UNIQUE(user_id, media_file_id, name). Speaker labels are unique per "
        "file per owner; the diarization writers rely on it and none of them can see it."
    ),
    "speaker_match::speaker_match_speaker1_id_speaker2_id_key::db-only-unique": (
        "BACKLOG: plain UNIQUE(speaker1_id, speaker2_id). Pairs with speaker_match_check "
        "below — together they say a match is stored once, in canonical order."
    ),
    "topic_suggestion::topic_suggestion_media_file_id_key::db-only-unique": (
        "BACKLOG: plain UNIQUE(media_file_id) — at most one suggestion row per file, which "
        "is what makes the topic writer an upsert rather than an append."
    ),
    "user::user_pki_cert_unique::db-only-unique": (
        "BACKLOG: UNIQUE(pki_serial_number, pki_issuer_dn) DEFERRABLE INITIALLY DEFERRED. "
        "Its deferrability IS covered — test_schema_constraint_rejections.py asserts it is "
        "the schema's only deferred constraint, in both directions — but the columns it "
        "spans appear nowhere in app/models/user.py."
    ),
    "summary_prompt::unique_system_default_per_content_type::db-only-unique": (
        "BACKLOG: partial UNIQUE(content_type) WHERE is_system_default = true. Expressible "
        "as Index(..., unique=True, postgresql_where=...), the shape app/models/media.py "
        "already uses for uq_tag_user_name/uq_tag_system_name."
    ),
    "group_mapping::uq_group_mapping_ldap_claim_ci::db-only-unique": (
        "BACKLOG: expression AND partial — UNIQUE(lower(claim_value)) WHERE source='ldap'. "
        "api/endpoints/admin_group_mappings.py catches its IntegrityError to return a 409 "
        "instead of a 500, so the rule is load-bearing in the API and undeclared in the model."
    ),
    "custom_vocabulary::_custom_vocab_unique::db-only-unique": (
        "BACKLOG: UNIQUE(COALESCE(user_id, 0), term, domain). This is the one case with a "
        "written rationale already in the model — app/models/custom_vocabulary.py explains "
        "that a plain UniqueConstraint(user_id, term, domain) would be WRONG, because "
        "NULL != NULL would let duplicate system terms through. That argument is against "
        "the wrong spelling, not against declaring it: Index(text('COALESCE(user_id, 0)'), "
        "'term', 'domain', unique=True) is the right one."
    ),
    # ------------------------------------------ uniqueness (shape disagrees, both ways)
    "usage_event::uq_usage_event_idempotency_key::db-only-unique": (
        "BACKLOG: the database enforces this PARTIALLY — WHERE idempotency_key IS NOT NULL "
        "— while the model declares a total unique= on the column. Pairs with the "
        "usage_event::(idempotency_key)::orm-only-unique entry: one disagreement, reported "
        "from both sides on purpose, because which side is authoritative is the question."
    ),
    "usage_event::(idempotency_key)::orm-only-unique": (
        "BACKLOG: the other half of the entry above. Behaviourally equivalent today (a "
        "total UNIQUE also admits many NULLs), so this is a create_all/autogenerate "
        "fidelity bug rather than a live enforcement gap — Base.metadata.create_all would "
        "build a total index the migrations never build."
    ),
    "user::uq_user_external_id::db-only-unique": (
        "BACKLOG: same shape disagreement as usage_event — the database index is partial on "
        "external_id IS NOT NULL, the model's is total. Pairs with "
        "user::ix_user_external_id::orm-only-unique."
    ),
    "user::ix_user_external_id::orm-only-unique": (
        "BACKLOG: the other half of the entry above. The model's total unique index does "
        "not exist in the database under any name."
    ),
    # ------------------------------------------------------------- CHECK (DB only)
    "user::ck_user_role_valid::db-only-check": (
        "BACKLOG: role IN (user, admin, super_admin) — the v369 invariant behind the sole "
        "authorization truth. app/auth/roles.VALID_ROLES is the Python-side list and "
        "test_schema_constraint_rejections.py exercises the rejection, but the CHECK itself "
        "is not on the model, so the two lists can drift apart silently."
    ),
    "user::ck_user_superuser_matches_role::db-only-check": (
        "BACKLOG: is_superuser = (role = 'super_admin'). The DDL is the only thing stopping "
        "a write that sets one without the other; app/models/CLAUDE.md documents the rule in "
        "prose and app/models/user.py declares nothing."
    ),
    "user::ck_user_auth_type_valid::db-only-check": (
        "BACKLOG: auth_type IN (local, ldap, oidc, pki, proxy, saml), widened by v378/v383. "
        "A sixth provider added to the model without widening this CHECK refuses every "
        "login for it — v378 had to delete a duplicate CHECK for exactly that reason."
    ),
    "user::ck_user_approval_status_valid::db-only-check": (
        "BACKLOG: approval_status IN (pending, approved, rejected) — v381. The approval "
        "helpers read the column fail-safe, so this CHECK is what keeps that sound."
    ),
    "user_invitation::ck_user_invitation_role_valid::db-only-check": (
        "BACKLOG: role IN (user, admin, super_admin) on invitations. Mirrors "
        "ck_user_role_valid; app/models/invitation.py declares neither."
    ),
    "user_invitation::ck_user_invitation_auth_type_valid::db-only-check": (
        "BACKLOG: auth_type value set on invitations, mirroring ck_user_auth_type_valid."
    ),
    "user_group_member::_user_group_member_role_check::db-only-check": (
        "BACKLOG: role IN (owner, admin, member). app/models/group.py DOES declare the "
        "sibling ck_user_group_member_source_valid from MEMBERSHIP_SOURCES, which is what "
        "makes the missing role CHECK next to it easy to read as intentional."
    ),
    "collection_share::_collection_share_permission_check::db-only-check": (
        "BACKLOG: permission IN (viewer, editor). PermissionService trusts the stored value, "
        "so an unknown permission would be an unhandled branch in the authorization path."
    ),
    "collection_share::_collection_share_target_type_check::db-only-check": (
        "BACKLOG: target_type IN (user, group). app/models/sharing.py declares the sibling "
        "_collection_share_target_check (exactly one target) but not this one — while "
        "tag_share declares BOTH halves, so the shape to copy is already in the tree."
    ),
    "speaker_match::speaker_match_check::db-only-check": (
        "BACKLOG: speaker1_id < speaker2_id. A canonical-ordering rule every writer of a "
        "SpeakerMatch must obey, discoverable only by triggering it; "
        "test_schema_constraint_rejections.py::test_a_reversed_speaker_pair_is_rejected "
        "exercises it, and app/models/media.py still says nothing about it."
    ),
}

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


def test_the_detector_finds_the_constraint_that_caused_this_module(db_session):
    """``uq_transcript_segment_content`` must be reported, or nothing here means anything.

    Every assertion above compares against ``find_divergences``. A derivation that matched
    nothing would report zero findings, which is indistinguishable from a clean tree — and
    that is not a hypothetical: it is how two detectors in ``scripts/audit-tests.py`` and
    two in the frontend auditor were found dead.
    """
    keys = {d.key for d in _divergences(db_session.connection())}
    assert "transcript_segment::uq_transcript_segment_content::db-only-unique" in keys


def test_declaring_the_md5_index_clears_its_finding(db_session):
    """The matcher must be able to MATCH an expression index, not just fail to find one.

    A normaliser that mangled ``md5(text)`` differently on each side would report the
    finding above for the wrong reason and would go on reporting it after somebody fixed
    the model — a permanently red gate that proves nothing. So the fix is simulated here:
    the declaration is built with the real ``Index`` API, run through the real ORM-side
    derivation, and the finding must disappear.
    """
    conn = db_session.connection()
    sides = _sides(conn)
    assert "transcript_segment::uq_transcript_segment_content::db-only-unique" in {
        d.key for d in derive.find_divergences(**sides)
    }

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
    sides["orm_uniques"] = {**sides["orm_uniques"], **derive.orm_unique_rules(simulated)}

    assert "transcript_segment::uq_transcript_segment_content::db-only-unique" not in {
        d.key for d in derive.find_divergences(**sides)
    }


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
