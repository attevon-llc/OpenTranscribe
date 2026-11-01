"""Pins for ``app/services/tag_scope.py`` — the tag plane's tenant boundary.

Every predicate here decides whether one tenant's tag name can reach another
tenant (resolution, listing, the impact preview, merge's cross-tenant refusal).
The API suites exercise the common paths; these tests pin the individual arms
so that a one-token change in a predicate (a dropped ``organization_id IS NULL``,
a widened share arm, a lost quarantine exclusion) turns a specific test red.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.tenancy import UNSCOPED
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.sharing import TagShare
from app.models.tag import FileTag
from app.models.tag import Tag
from app.models.user import User
from app.services.tag_scope import OWNERSHIP_MINE
from app.services.tag_scope import OWNERSHIP_SHARED_WITH_ME
from app.services.tag_scope import OWNERSHIP_SYSTEM
from app.services.tag_scope import TAG_OWNERSHIPS
from app.services.tag_scope import accessible_file_ids_subquery
from app.services.tag_scope import in_tenant
from app.services.tag_scope import is_system_tag
from app.services.tag_scope import owned_or_system
from app.services.tag_scope import system_tag
from app.services.tag_scope import tag_in_tenant
from app.services.tag_scope import tag_ownership
from app.services.tag_scope import tenant_first
from app.services.tag_scope import tenant_key
from app.services.tag_scope import visible_to

# --------------------------------------------------------------------------- #
# Pure (no database) predicates, on transient Tag rows                         #
# --------------------------------------------------------------------------- #


def _row(user_id: int | None, org_id: int | None) -> Tag:
    return Tag(name="t", user_id=user_id, organization_id=org_id)


def test_ownership_vocabulary_is_the_three_wire_values():
    assert TAG_OWNERSHIPS == ("mine", "system", "shared_with_me")
    assert (OWNERSHIP_MINE, OWNERSHIP_SYSTEM, OWNERSHIP_SHARED_WITH_ME) == TAG_OWNERSHIPS


@pytest.mark.parametrize(
    ("user_id", "org_id", "expected"),
    [
        (None, None, True),
        # An org tag whose creator's account is gone keeps its org: NOT system.
        (None, 7, False),
        (3, None, False),
        (3, 7, False),
    ],
)
def test_is_system_tag_needs_no_owner_and_no_tenant(user_id, org_id, expected):
    assert is_system_tag(_row(user_id, org_id)) is expected


@pytest.mark.parametrize(
    ("user_id", "org_id", "key"),
    [
        (None, None, ("system",)),
        (None, 7, ("org", 7)),
        (3, 7, ("org", 7)),
        (3, None, ("user", 3)),
    ],
)
def test_tenant_key_names_system_org_or_personal_space(user_id, org_id, key):
    assert tenant_key(_row(user_id, org_id)) == key


@pytest.mark.parametrize(
    ("user_id", "org_id", "caller", "caller_org", "expected"),
    [
        # Organization context: any tag of that org, whoever created it.
        (9, 7, 1, 7, True),
        (None, 7, 1, 7, True),
        (1, 8, 1, 7, False),  # the caller's own tag, but in another org
        (1, None, 1, 7, False),  # the caller's personal tag is not in an org tenant
        (None, None, 1, 7, False),  # system vocabulary is excluded here
        # Personal scope: only the caller's own org-less tags.
        (1, None, 1, None, True),
        (1, 7, 1, None, False),  # own org tag is not in the personal workspace
        (9, None, 1, None, False),  # someone else's personal tag
        (None, None, 1, None, False),
    ],
)
def test_tag_in_tenant_python_form(user_id, org_id, caller, caller_org, expected):
    assert tag_in_tenant(_row(user_id, org_id), caller, caller_org) is expected


def test_tag_ownership_classifies_by_callers_relationship():
    assert tag_ownership(_row(None, None), 1) == OWNERSHIP_SYSTEM
    assert tag_ownership(_row(1, None), 1) == OWNERSHIP_MINE
    assert tag_ownership(_row(1, 7), 1) == OWNERSHIP_MINE
    assert tag_ownership(_row(2, 7), 1) == OWNERSHIP_SHARED_WITH_ME
    # An orphaned org tag is a colleague's vocabulary, never "system".
    assert tag_ownership(_row(None, 7), 1) == OWNERSHIP_SHARED_WITH_ME


def test_tenant_first_orders_by_organization_then_user():
    cols = tenant_first()
    assert [c.key for c in cols] == ["organization_id", "user_id"]


# --------------------------------------------------------------------------- #
# Database-backed predicates                                                   #
# --------------------------------------------------------------------------- #


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _user(db) -> User:
    user = User(
        email=f"{_uniq('scope')}@example.com",
        full_name="scope user",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.flush()
    return user


def _org(db) -> Organization:
    org = Organization(external_org_id=_uniq("org"), name=_uniq("Org"), is_active=True)
    db.add(org)
    db.flush()
    return org


def _tag(db, *, user_id: int | None, org_id: int | None = None) -> Tag:
    name = _uniq("tag")
    tag = Tag(name=name, normalized_name=name.replace("-", " "), user_id=user_id)
    tag.organization_id = org_id
    db.add(tag)
    db.flush()
    return tag


def _file(db, owner: User, org_id: int | None = None, *, quarantined: bool = False) -> MediaFile:
    file_uuid = uuid.uuid4()
    media = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=org_id,
        filename="scope.wav",
        storage_path=f"scope/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1,
        status="completed",
    )
    media.is_quarantined = quarantined
    db.add(media)
    db.flush()
    return media


def _attach(db, tag: Tag, media: MediaFile) -> None:
    db.add(FileTag(tag_id=tag.id, media_file_id=media.id))
    db.flush()


def _share_to_user(db, tag: Tag, by: User, target: User) -> None:
    db.add(
        TagShare(tag_id=tag.id, shared_by_id=by.id, target_type="user", target_user_id=target.id)
    )
    db.flush()


def _ids(db, predicate) -> set[int]:
    return {row[0] for row in db.query(Tag.id).filter(predicate).all()}


def test_in_tenant_org_context_takes_every_tag_of_the_org_and_nothing_else(db_session):
    db = db_session
    me, colleague = _user(db), _user(db)
    org, other_org = _org(db), _org(db)
    mine_in_org = _tag(db, user_id=me.id, org_id=org.id)
    colleagues = _tag(db, user_id=colleague.id, org_id=org.id)
    orphaned = _tag(db, user_id=None, org_id=org.id)
    other_orgs = _tag(db, user_id=me.id, org_id=other_org.id)
    personal = _tag(db, user_id=me.id)
    system = _tag(db, user_id=None)

    got = _ids(db, in_tenant(me.id, org.id))

    assert {mine_in_org.id, colleagues.id, orphaned.id} <= got
    assert not ({other_orgs.id, personal.id, system.id} & got)


def test_in_tenant_personal_scope_is_only_my_orgless_tags(db_session):
    db = db_session
    me, other = _user(db), _user(db)
    org = _org(db)
    personal = _tag(db, user_id=me.id)
    my_org_tag = _tag(db, user_id=me.id, org_id=org.id)
    others_personal = _tag(db, user_id=other.id)
    system = _tag(db, user_id=None)

    got = _ids(db, in_tenant(me.id, None))

    assert personal.id in got
    assert not ({my_org_tag.id, others_personal.id, system.id} & got)


def test_system_tag_sql_requires_no_owner_and_no_org(db_session):
    db = db_session
    me = _user(db)
    org = _org(db)
    system = _tag(db, user_id=None)
    orphaned_org = _tag(db, user_id=None, org_id=org.id)
    owned = _tag(db, user_id=me.id)

    got = _ids(db, system_tag())

    assert system.id in got
    assert not ({orphaned_org.id, owned.id} & got)


def test_owned_or_system_adds_system_to_the_tenant_and_nothing_wider(db_session):
    db = db_session
    me, other = _user(db), _user(db)
    org = _org(db)
    mine = _tag(db, user_id=me.id)
    system = _tag(db, user_id=None)
    others = _tag(db, user_id=other.id)
    in_org = _tag(db, user_id=other.id, org_id=org.id)

    personal = _ids(db, owned_or_system(me.id, None))
    assert {mine.id, system.id} <= personal
    assert not ({others.id, in_org.id} & personal)

    org_ctx = _ids(db, owned_or_system(me.id, org.id))
    assert {in_org.id, system.id} <= org_ctx
    assert not ({mine.id, others.id} & org_ctx)


def test_tenant_first_puts_the_tenant_row_ahead_of_a_same_named_system_row(db_session):
    db = db_session
    me = _user(db)
    org = _org(db)
    name = _uniq("dup")
    system = Tag(name=name, normalized_name=name, user_id=None)
    tenant = Tag(name=name, normalized_name=name, user_id=me.id)
    tenant.organization_id = org.id
    db.add_all([system, tenant])
    db.flush()

    first = (
        db.query(Tag)
        .filter(Tag.normalized_name == name, owned_or_system(me.id, org.id))
        .order_by(*tenant_first())
        .first()
    )

    assert first is not None
    assert first.id == tenant.id


def test_accessible_file_ids_subquery_applies_the_tenant_gate(db_session):
    from sqlalchemy import select

    db = db_session
    me = _user(db)
    org = _org(db)
    personal_file = _file(db, me)
    org_file = _file(db, me, org.id)

    def ids(scope) -> set[int]:
        sub = accessible_file_ids_subquery(db, me.id, scope)
        return {row[0] for row in db.execute(select(sub)).all()}

    assert ids(None) >= {personal_file.id}
    assert org_file.id not in ids(None)
    assert ids(org.id) >= {org_file.id}
    assert personal_file.id not in ids(org.id)
    # Omitting the scope is the documented legacy "no gate" answer.
    assert ids(UNSCOPED) >= {personal_file.id, org_file.id}
    default_sub = accessible_file_ids_subquery(db, me.id)
    assert {row[0] for row in db.execute(select(default_sub)).all()} >= {
        personal_file.id,
        org_file.id,
    }


class TestVisibleTo:
    """Each arm of the read predicate, and the tenant bound on the widened ones."""

    def test_unshared_foreign_tag_is_invisible_but_own_and_system_are_visible(self, db_session):
        db = db_session
        me, other = _user(db), _user(db)
        mine = _tag(db, user_id=me.id)
        system = _tag(db, user_id=None)
        foreign = _tag(db, user_id=other.id)

        got = _ids(db, visible_to(db, me.id, None))

        assert {mine.id, system.id} <= got
        assert foreign.id not in got

    def test_direct_share_makes_a_foreign_tag_visible_only_to_the_recipient(self, db_session):
        db = db_session
        me, other, bystander = _user(db), _user(db), _user(db)
        shared = _tag(db, user_id=other.id)
        _share_to_user(db, shared, other, me)

        assert shared.id in _ids(db, visible_to(db, me.id, None))
        assert shared.id not in _ids(db, visible_to(db, bystander.id, None))

    def test_group_share_reaches_group_members_only(self, db_session):
        db = db_session
        me, other, bystander = _user(db), _user(db), _user(db)
        group = UserGroup(owner_id=other.id, name=_uniq("grp"))
        db.add(group)
        db.flush()
        db.add(UserGroupMember(group_id=group.id, user_id=me.id, role="member"))
        shared = _tag(db, user_id=other.id)
        db.add(
            TagShare(
                tag_id=shared.id,
                shared_by_id=other.id,
                target_type="group",
                target_group_id=group.id,
            )
        )
        db.flush()

        assert shared.id in _ids(db, visible_to(db, me.id, None))
        assert shared.id not in _ids(db, visible_to(db, bystander.id, None))

    def test_a_share_never_crosses_the_tenant_boundary(self, db_session):
        db = db_session
        me, other = _user(db), _user(db)
        org, other_org = _org(db), _org(db)
        in_my_org = _tag(db, user_id=other.id, org_id=org.id)
        in_other_org = _tag(db, user_id=other.id, org_id=other_org.id)
        personal_of_other = _tag(db, user_id=other.id)
        for tag in (in_my_org, in_other_org, personal_of_other):
            _share_to_user(db, tag, other, me)

        in_org_ctx = _ids(db, visible_to(db, me.id, org.id))
        assert in_my_org.id in in_org_ctx
        assert not ({in_other_org.id, personal_of_other.id} & in_org_ctx)

        in_personal_ctx = _ids(db, visible_to(db, me.id, None))
        assert personal_of_other.id in in_personal_ctx
        assert not ({in_my_org.id, in_other_org.id} & in_personal_ctx)

    def test_a_tag_on_an_accessible_file_is_visible_but_not_one_on_an_inaccessible_file(
        self, db_session
    ):
        db = db_session
        me, other = _user(db), _user(db)
        foreign_on_mine = _tag(db, user_id=other.id)
        foreign_on_theirs = _tag(db, user_id=other.id)
        _attach(db, foreign_on_mine, _file(db, me))
        _attach(db, foreign_on_theirs, _file(db, other))

        got = _ids(db, visible_to(db, me.id, None))

        assert foreign_on_mine.id in got
        assert foreign_on_theirs.id not in got

    def test_a_tag_on_a_quarantined_file_stops_surfacing(self, db_session):
        db = db_session
        me, other = _user(db), _user(db)
        tag = _tag(db, user_id=other.id)
        _attach(db, tag, _file(db, me, quarantined=True))

        assert tag.id not in _ids(db, visible_to(db, me.id, None))

    def test_a_foreign_tenant_tag_on_an_accessible_file_stays_hidden(self, db_session):
        db = db_session
        me, other = _user(db), _user(db)
        org, other_org = _org(db), _org(db)
        legacy_stray = _tag(db, user_id=other.id, org_id=other_org.id)
        _attach(db, legacy_stray, _file(db, me, org.id))

        assert legacy_stray.id not in _ids(db, visible_to(db, me.id, org.id))

    def test_an_org_colleagues_tag_is_visible_in_the_org_but_not_in_personal_scope(
        self, db_session
    ):
        db = db_session
        me, colleague = _user(db), _user(db)
        org = _org(db)
        colleagues = _tag(db, user_id=colleague.id, org_id=org.id)

        assert colleagues.id in _ids(db, visible_to(db, me.id, org.id))
        assert colleagues.id not in _ids(db, visible_to(db, me.id, None))
