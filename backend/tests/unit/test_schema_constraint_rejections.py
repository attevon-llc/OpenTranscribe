"""Constraints that had no rejection test — the ones where the DDL is the only rule.

The live schema carries 17 CHECK constraints, 72 UNIQUE constraints and 10 partial
unique indexes; roughly 11 had a test that watched one reject something. The rest were
asserted only by existing — which proves the constraint is *there*, not that it is
*right*: a CHECK on the wrong column, or a UNIQUE that Postgres's NULL semantics let
through, both exist perfectly happily.

Every constraint below is a rule that lives in the DDL and nowhere else — the
authorization and pairing code reads a row and trusts its shape. Each test states the
concrete thing that gets in if the constraint goes.

Not covered here, deliberately: ``_tag_share_target_check`` and both ``tag_share``
partial uniques already have rejection tests in
``tests/unit/test_v386_migration_consistency.py``, and duplicating them would put the
same rule in two places — the failure mode half this suite exists to prevent.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth.roles import VALID_ROLES

_SEEDED = "seeded-by-test-schema-constraint-rejections"


def _new_user(conn, *, role: str = "user") -> int:
    """A user row owned by this test — never borrowed from ambient data (CI has none)."""
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, :su, :role, 'local') RETURNING id"
            ),
            {
                "e": f"constraint-{uuid_pkg.uuid4().hex[:10]}@example.com",
                "su": role == "super_admin",
                "role": role,
            },
        ).scalar()
    )


def _new_media_file(conn, user_id: int) -> int:
    fuuid = uuid_pkg.uuid4()
    return int(
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, filename, storage_path, content_type, "
                "file_size, user_id, status) VALUES (:u, :f, :p, 'video/mp4', 10, :uid, "
                "'completed') RETURNING id"
            ),
            {"u": fuuid, "f": f"{fuuid.hex[:8]}.mp4", "p": f"m/{fuuid}.mp4", "uid": user_id},
        ).scalar()
    )


def _new_speaker(conn, user_id: int, media_file_id: int) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO speaker (uuid, name, user_id, media_file_id) "
                "VALUES (:u, :n, :uid, :mid) RETURNING id"
            ),
            {
                "u": uuid_pkg.uuid4(),
                "n": f"SPK-{uuid_pkg.uuid4().hex[:6]}",
                "uid": user_id,
                "mid": media_file_id,
            },
        ).scalar()
    )


# ---------------------------------------------------------------------------
# user.role — the v380 bug shape, on a different column
# ---------------------------------------------------------------------------


def test_role_has_exactly_one_check_constraint(db_session):
    """``user.role`` must be governed by ONE constraint, not two identical ones.

    It had two with byte-identical bodies: ``ck_user_role_valid`` and the older
    ``users_role_check`` (``v200``). That is precisely the shape ``v380`` had to repair
    on ``auth_type``, where the widening was applied to one constraint and the other
    went on refusing the new value — which does not fail during the migration. It fails
    later, at every login of the new kind, as a ``CheckViolation`` on JIT provisioning.
    ``v387`` drops the duplicate; this is the assertion that keeps it dropped.
    """
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
    assert list(names) == ["ck_user_role_valid"], (
        f"expected exactly one role CHECK, found {names}. Two constraints saying the "
        "same thing means the next widening only reaches one of them."
    )


@pytest.mark.parametrize("role", sorted(VALID_ROLES))
def test_every_valid_role_is_accepted(db_session, role):
    """The positive half. Asserting a constraint's text is not enough (see v380).

    Parametrised over ``auth.roles.VALID_ROLES``, the Python side of the same rule, so
    adding a role there without widening the CHECK fails here rather than at that
    account's first login.
    """
    conn = db_session.connection()
    user_id = _new_user(conn, role=role)

    stored = conn.execute(text('SELECT role FROM "user" WHERE id = :i'), {"i": user_id}).scalar()
    assert stored == role
    db_session.rollback()


def test_an_unknown_role_is_rejected(db_session):
    """``role`` is the sole authorization truth, so an unrecognised value is a hole.

    Every privilege gate compares against a known role name; a row carrying ``owner``
    would be denied everywhere, which reads as a broken account rather than as bad data,
    and a value like ``superadmin`` (no underscore) would be denied *silently* while
    looking correct in the admin list.
    """
    conn = db_session.connection()
    with pytest.raises(IntegrityError):
        _new_user(conn, role="superadmin")
    db_session.rollback()


def test_the_superuser_mirror_check_still_rejects_a_mismatch(db_session):
    """``ck_user_superuser_matches_role``: ``is_superuser`` is DERIVED, never independent.

    Both are read across the codebase, and a row where they disagree is either a
    privilege the role does not grant or a role whose privilege is missing — with no way
    to tell which was intended.
    """
    conn = db_session.connection()
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, true, 'user', 'local')"
            ),
            {"e": f"mirror-{uuid_pkg.uuid4().hex[:8]}@example.com"},
        )
    db_session.rollback()


# ---------------------------------------------------------------------------
# speaker_match — the constraint that makes the UNIQUE mean anything
# ---------------------------------------------------------------------------


def test_a_reversed_speaker_pair_is_rejected(db_session):
    """``speaker_match_check`` (``speaker1_id < speaker2_id``) is what canonicalises a pair.

    ``UNIQUE (speaker1_id, speaker2_id)`` treats ``(3, 7)`` and ``(7, 3)`` as different
    rows, so without the ordering CHECK the same match can be stored twice with **no
    error at all** — the worst failure mode here, because nothing surfaces and the
    duplicate rows then double-count in every consumer of the match table.
    """
    conn = db_session.connection()
    user_id = _new_user(conn)
    media_file_id = _new_media_file(conn, user_id)
    low = _new_speaker(conn, user_id, media_file_id)
    high = _new_speaker(conn, user_id, media_file_id)
    assert low < high, "sequence handed out ids in an unexpected order"

    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO speaker_match (uuid, speaker1_id, speaker2_id, confidence) "
                "VALUES (:u, :a, :b, 0.9)"
            ),
            {"u": uuid_pkg.uuid4(), "a": high, "b": low},
        )
    db_session.rollback()


def test_a_speaker_cannot_match_itself(db_session):
    """The strict ``<`` also rules out ``speaker1_id = speaker2_id``.

    A self-match is a 1.0-confidence row that makes every speaker look linked to
    something, which is exactly the input that would make a merge heuristic collapse
    unrelated speakers.
    """
    conn = db_session.connection()
    user_id = _new_user(conn)
    speaker_id = _new_speaker(conn, user_id, _new_media_file(conn, user_id))

    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO speaker_match (uuid, speaker1_id, speaker2_id, confidence) "
                "VALUES (:u, :a, :a, 1.0)"
            ),
            {"u": uuid_pkg.uuid4(), "a": speaker_id},
        )
    db_session.rollback()


def test_the_canonical_speaker_pair_is_accepted(db_session):
    """The control: the CHECK rejects the ORDER, not every insert."""
    conn = db_session.connection()
    user_id = _new_user(conn)
    media_file_id = _new_media_file(conn, user_id)
    low = _new_speaker(conn, user_id, media_file_id)
    high = _new_speaker(conn, user_id, media_file_id)

    conn.execute(
        text(
            "INSERT INTO speaker_match (uuid, speaker1_id, speaker2_id, confidence) "
            "VALUES (:u, :a, :b, 0.9)"
        ),
        {"u": uuid_pkg.uuid4(), "a": low, "b": high},
    )
    count = conn.execute(
        text("SELECT count(*) FROM speaker_match WHERE speaker1_id = :a"), {"a": low}
    ).scalar()
    assert count == 1
    db_session.rollback()


# ---------------------------------------------------------------------------
# user_pki_cert_unique — the only DEFERRABLE constraint in the schema
# ---------------------------------------------------------------------------


def test_the_pki_cert_unique_is_deferred_to_commit(db_session):
    """``user_pki_cert_unique`` is ``DEFERRABLE INITIALLY DEFERRED`` — the only one.

    Every other constraint here raises at the offending statement. This one does not:
    the duplicate ``INSERT`` succeeds and the violation surfaces at **COMMIT**. Any
    handler shaped ``try: db.add(...); db.flush() except IntegrityError:`` therefore
    misses it entirely, and the error arrives later, attributed to whatever code
    happened to be committing.

    The assertion is the pair: the second insert must NOT raise, and the commit must.
    Asserting only the second half would pass for an ordinary, non-deferred UNIQUE.
    """
    conn = db_session.connection()
    serial = f"SER{uuid_pkg.uuid4().hex[:10].upper()}"
    issuer = f"CN=Test Issuer {uuid_pkg.uuid4().hex[:8]}"

    for _ in range(2):
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type, pki_serial_number, pki_issuer_dn) "
                "VALUES (:e, 'x', true, false, 'user', 'pki', :serial, :issuer)"
            ),
            {
                "e": f"pki-{uuid_pkg.uuid4().hex[:10]}@example.com",
                "serial": serial,
                "issuer": issuer,
            },
        )

    # Both rows are in. Under a non-deferred UNIQUE the loop above would have raised.
    duplicates = conn.execute(
        text('SELECT count(*) FROM "user" WHERE pki_serial_number = :s'), {"s": serial}
    ).scalar()
    assert duplicates == 2

    with pytest.raises(IntegrityError):
        # SET CONSTRAINTS IMMEDIATE forces the deferred check now, which is what COMMIT
        # would do — done explicitly because the savepoint harness never really commits.
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    db_session.rollback()


def test_the_pki_cert_unique_is_the_only_deferred_constraint(db_session):
    """Pin the exception so a second one is a deliberate act.

    The prose above ("any handler catching IntegrityError around add() misses it")
    is only safe advice while this is the *only* deferred constraint. A second one added
    without that being noticed puts a silent write path in whatever code touches it.
    """
    names = (
        db_session.connection()
        .execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE connamespace = 'public'::regnamespace AND condeferrable"
            )
        )
        .scalars()
        .all()
    )
    assert sorted(names) == ["user_pki_cert_unique"]


# ---------------------------------------------------------------------------
# user_invitation — an invitation is a deferred account creation
# ---------------------------------------------------------------------------


def _invite(conn, *, role: str, created_by: int) -> None:
    conn.execute(
        text(
            "INSERT INTO user_invitation (uuid, email, token_hash, role, auth_type, "
            "created_by_id, expires_at) VALUES (:u, :e, :t, :role, 'local', :cb, "
            "now() + interval '7 days')"
        ),
        {
            "u": uuid_pkg.uuid4(),
            "e": f"invite-{uuid_pkg.uuid4().hex[:10]}@example.com",
            "t": uuid_pkg.uuid4().hex,
            "role": role,
            "cb": created_by,
        },
    )


def test_an_invitation_with_an_unknown_role_is_rejected(db_session):
    """``ck_user_invitation_role_valid``: an invitation is a *deferred* account creation.

    The role is copied onto the account when the invitee registers, so a value the
    ``user`` CHECK would refuse becomes a redemption that fails at the last step — after
    the invitee has already followed the link and set a password.
    """
    conn = db_session.connection()
    admin_id = _new_user(conn, role="admin")

    with pytest.raises(IntegrityError):
        _invite(conn, role="root", created_by=admin_id)
    db_session.rollback()


def test_an_invitation_may_mint_a_super_admin_and_the_gate_is_in_code(db_session):
    """The CHECK deliberately permits ``super_admin``; the privilege gate is elsewhere.

    Worth pinning explicitly, because the neighbouring ``ck_group_mapping_role_capped``
    caps an IdP-granted role at ``admin`` — so the two constraints look inconsistent
    until you know that here the restriction is intentional and enforced by
    ``endpoints/auth/invitations.py``'s ``ELEVATED_ROLES`` check (only a ``super_admin``
    may invite one). Reading the cap as universal and "tightening" this CHECK would
    break the documented bootstrap path for a second super_admin.
    """
    from app.api.endpoints.auth.invitations import ELEVATED_ROLES

    conn = db_session.connection()
    super_admin_id = _new_user(conn, role="super_admin")
    _invite(conn, role="super_admin", created_by=super_admin_id)

    stored = conn.execute(
        text("SELECT count(*) FROM user_invitation WHERE created_by_id = :i"),
        {"i": super_admin_id},
    ).scalar()
    assert stored == 1
    assert "super_admin" in ELEVATED_ROLES
    db_session.rollback()


# ---------------------------------------------------------------------------
# user_group_member.role
# ---------------------------------------------------------------------------


def test_an_unknown_group_member_role_is_rejected(db_session):
    """``_user_group_member_role_check`` — ``owner`` | ``admin`` | ``member``.

    These names collide with the *platform* roles but mean something else entirely
    (authority inside one group). An unrecognised value here is read by the group
    permission checks as "not owner, not admin", i.e. silently demoted to member, which
    is a permission loss nobody is told about.
    """
    conn = db_session.connection()
    owner_id = _new_user(conn)
    group_id = conn.execute(
        text("INSERT INTO user_group (uuid, name, owner_id) VALUES (:u, :n, :o) RETURNING id"),
        {"u": uuid_pkg.uuid4(), "n": f"grp-{uuid_pkg.uuid4().hex[:8]}", "o": owner_id},
    ).scalar()

    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO user_group_member (uuid, group_id, user_id, role, source) "
                "VALUES (:u, :g, :m, 'super_admin', 'manual')"
            ),
            {"u": uuid_pkg.uuid4(), "g": group_id, "m": owner_id},
        )
    db_session.rollback()


# ---------------------------------------------------------------------------
# collection_share — three CHECKs and two partial uniques, none previously tested
# ---------------------------------------------------------------------------


def _share_collection(
    conn,
    collection_id: int,
    sharer_id: int,
    *,
    target_type: str = "user",
    user: int | None = None,
    group: int | None = None,
    permission: str = "viewer",
):
    conn.execute(
        text(
            "INSERT INTO collection_share (uuid, collection_id, shared_by_id, target_type, "
            "target_user_id, target_group_id, permission) "
            "VALUES (:u, :c, :s, :tt, :tu, :tg, :p)"
        ),
        {
            "u": uuid_pkg.uuid4(),
            "c": collection_id,
            "s": sharer_id,
            "tt": target_type,
            "tu": user,
            "tg": group,
            "p": permission,
        },
    )


@pytest.fixture
def shareable_collection(db_session):
    """A user, a collection they own, and a group they own. ``(user_id, col_id, group_id)``."""
    conn = db_session.connection()
    suffix = uuid_pkg.uuid4().hex[:8]
    user_id = _new_user(conn)
    collection_id = conn.execute(
        text("INSERT INTO collection (uuid, name, user_id) VALUES (:u, :n, :o) RETURNING id"),
        {"u": uuid_pkg.uuid4(), "n": f"col-{suffix}", "o": user_id},
    ).scalar()
    group_id = conn.execute(
        text("INSERT INTO user_group (uuid, name, owner_id) VALUES (:u, :n, :o) RETURNING id"),
        {"u": uuid_pkg.uuid4(), "n": f"grp-{suffix}", "o": user_id},
    ).scalar()
    return int(user_id), int(collection_id), int(group_id)


def test_an_unknown_share_permission_is_rejected(db_session, shareable_collection):
    """``_collection_share_permission_check`` — ``viewer`` | ``editor``.

    ``PermissionService`` decides write access by comparing this string. An unrecognised
    value is not read as "no access": it is read as "not editor", so a grant meant to
    confer editing silently confers read-only — a share that looks correct in the UI and
    is not.
    """
    user_id, collection_id, _ = shareable_collection
    conn = db_session.connection()

    with pytest.raises(IntegrityError):
        _share_collection(conn, collection_id, user_id, user=user_id, permission="owner")
    db_session.rollback()


def test_an_unknown_share_target_type_is_rejected(db_session, shareable_collection):
    """``_collection_share_target_type_check`` — ``user`` | ``group``.

    This is the constraint ``tag_share`` was missing until ``v387``: ``target_type``
    selects which of the two nullable target columns the resolver reads, so a third
    value is a grant no branch matches — a row that exists, resolves to nobody, and
    still shows in the owner's list of who the collection is shared with.
    """
    user_id, collection_id, _ = shareable_collection
    conn = db_session.connection()

    with pytest.raises(IntegrityError):
        _share_collection(conn, collection_id, user_id, target_type="everyone", user=user_id)
    db_session.rollback()


@pytest.mark.parametrize("shape", ["neither", "both"])
def test_a_share_must_name_exactly_one_target(db_session, shareable_collection, shape):
    """``_collection_share_target_check`` — never both, never neither.

    ``neither`` is a grant the resolver joins to nobody, which reads as an existing
    grant rather than as an absent one. ``both`` is two grants sharing one revocation:
    deleting it removes access from a party nobody meant to touch.
    """
    user_id, collection_id, group_id = shareable_collection
    conn = db_session.connection()
    targets = {} if shape == "neither" else {"user": user_id, "group": group_id}

    with pytest.raises(IntegrityError):
        _share_collection(conn, collection_id, user_id, **targets)
    db_session.rollback()


@pytest.mark.parametrize("target", ["user", "group"])
def test_a_duplicate_collection_share_is_rejected(db_session, shareable_collection, target):
    """``_collection_share_user_uc`` / ``_collection_share_group_uc`` — one grant per pair.

    Both halves, because they are two separate partial indexes and only one would notice
    if the other were dropped. Two grants for one recipient means revoking the share
    leaves the other row behind, so access survives its own removal.
    """
    user_id, collection_id, group_id = shareable_collection
    conn = db_session.connection()
    kwargs = (
        {"target_type": "user", "user": user_id}
        if target == "user"
        else {"target_type": "group", "group": group_id}
    )

    _share_collection(conn, collection_id, user_id, **kwargs)
    with pytest.raises(IntegrityError):
        _share_collection(conn, collection_id, user_id, **kwargs)
    db_session.rollback()


def test_the_collection_share_uniques_are_partial(db_session):
    """Partial AND unique, both — a plain composite UNIQUE would not do the job.

    Postgres treats NULLs as distinct, so ``UNIQUE (collection_id, target_user_id,
    target_group_id)`` admits two identical group grants as ``(c, NULL, g)`` twice. This
    is the same reasoning ``tag_share`` is indexed on, asserted here for the table it was
    copied from.
    """
    rows = (
        db_session.connection()
        .execute(
            text(
                "SELECT c.relname AS name, i.indisunique AS is_unique, "
                "(i.indpred IS NOT NULL) AS is_partial "
                "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = 'collection_share'::regclass"
            )
        )
        .all()
    )
    shapes = {row.name: (row.is_unique, row.is_partial) for row in rows}

    assert shapes["_collection_share_user_uc"] == (True, True)
    assert shapes["_collection_share_group_uc"] == (True, True)


# ---------------------------------------------------------------------------
# summary_prompt / file_tag
# ---------------------------------------------------------------------------


def test_only_one_system_default_prompt_per_content_type(db_session):
    """``unique_system_default_per_content_type`` is PARTIAL on ``is_system_default = true``.

    The prompt resolver picks *the* system default for a content type with a single-row
    read; two of them makes which prompt runs depend on plan order, so the same
    transcript summarises differently on different days. The index has to be partial —
    a plain ``UNIQUE (content_type)`` would forbid a second *user* prompt for that type,
    which is the normal case.
    """
    conn = db_session.connection()
    content_type = f"seeded-{uuid_pkg.uuid4().hex[:8]}"

    def _insert(*, system_default: bool) -> None:
        conn.execute(
            text(
                "INSERT INTO summary_prompt (uuid, name, prompt_text, is_system_default, "
                "content_type, tags) VALUES (:u, :n, :t, :sd, :ct, '[]'::jsonb)"
            ),
            {
                "u": uuid_pkg.uuid4(),
                "n": f"p-{uuid_pkg.uuid4().hex[:8]}",
                "t": _SEEDED,
                "sd": system_default,
                "ct": content_type,
            },
        )

    _insert(system_default=True)
    # The partial predicate at work: a NON-default prompt for the same content type is
    # fine. Without this line the test would pass against a plain UNIQUE(content_type).
    _insert(system_default=False)

    with pytest.raises(IntegrityError):
        _insert(system_default=True)
    db_session.rollback()


def test_one_file_cannot_carry_the_same_tag_twice(db_session):
    """``file_tag UNIQUE (media_file_id, tag_id)`` is what makes tagging idempotent.

    Every interactive tag path relies on it: ``POST /tags/files/{uuid}/tags`` dedupes by
    tag id on ``file_tag``, so a repeat post is a no-op *because the constraint says so*.
    Without it the detail page renders the tag twice and the gallery's ALL-filter has to
    count ``DISTINCT Tag.name`` to compensate.
    """
    conn = db_session.connection()
    user_id = _new_user(conn)
    media_file_id = _new_media_file(conn, user_id)
    tag_id = conn.execute(
        text("INSERT INTO tag (uuid, name, user_id) VALUES (:u, :n, :o) RETURNING id"),
        {"u": uuid_pkg.uuid4(), "n": f"t-{uuid_pkg.uuid4().hex[:8]}", "o": user_id},
    ).scalar()

    def _attach() -> None:
        conn.execute(
            text("INSERT INTO file_tag (uuid, media_file_id, tag_id) VALUES (:u, :m, :t)"),
            {"u": uuid_pkg.uuid4(), "m": media_file_id, "t": tag_id},
        )

    _attach()
    with pytest.raises(IntegrityError):
        _attach()
    db_session.rollback()


def test_tag_share_target_type_is_constrained_like_collection_share(db_session):
    """The schema gap ``v387`` closed: ``tag_share.target_type`` had no CHECK at all.

    ``collection_share`` has carried ``_collection_share_target_type_check`` since it was
    created. ``v386`` mirrored everything else about that table into ``tag_share`` —
    target shape, CASCADEs, partial uniques — and left this one guard off, so the
    permitted values lived only in a comment in ``models/sharing.py``
    (``String(20)  # "user" or "group"``). An unenforced comment is not a constraint.
    """
    conn = db_session.connection()
    suffix = uuid_pkg.uuid4().hex[:8]
    user_id = _new_user(conn)
    tag_id = conn.execute(
        text("INSERT INTO tag (uuid, name, user_id) VALUES (:u, :n, :o) RETURNING id"),
        {"u": uuid_pkg.uuid4(), "n": f"ts-{suffix}", "o": user_id},
    ).scalar()

    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO tag_share (uuid, tag_id, shared_by_id, target_type, "
                "target_user_id) VALUES (:u, :t, :s, 'everyone', :tu)"
            ),
            {"u": uuid_pkg.uuid4(), "t": tag_id, "s": user_id, "tu": user_id},
        )
    db_session.rollback()


def test_both_share_tables_constrain_target_type(db_session):
    """The two grant tables must not drift again.

    ``tag_share`` was created as a copy of ``collection_share`` precisely so there would
    be one set of rules. Comparing the two CHECK bodies is what makes that claim
    checkable: a widening applied to one is a difference here, not a discovery later.
    """
    conn = db_session.connection()
    bodies = {}
    for table in ("collection_share", "tag_share"):
        bodies[table] = conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
            {"name": f"_{table}_target_type_check"},
        ).scalar()

    assert bodies["collection_share"] is not None
    assert bodies["tag_share"] is not None
    permitted = {
        table: {value for value in ("user", "group", "everyone") if f"'{value}'" in body}
        for table, body in bodies.items()
    }
    assert permitted["collection_share"] == permitted["tag_share"] == {"user", "group"}
