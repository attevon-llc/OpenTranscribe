"""``group_queries`` against the real database: membership, counts, roles and name clashes."""

import uuid as uuid_pkg

import pytest

from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.user import User
from app.services import group_queries


def _user(db, label):
    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=label,
        hashed_password="x",
        is_active=True,
        role="user",
    )
    db.add(user)
    db.flush()
    return user


def _group(db, owner, name):
    group = UserGroup(uuid=uuid_pkg.uuid4(), name=name, owner_id=owner.id)
    db.add(group)
    db.flush()
    return group


def _member(db, group, user, role="member"):
    row = UserGroupMember(group_id=group.id, user_id=user.id, role=role)
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def team(db_session):
    owner = _user(db_session, "owner")
    other = _user(db_session, "other")
    alpha = _group(db_session, owner, f"alpha-{uuid_pkg.uuid4().hex[:6]}")
    beta = _group(db_session, owner, f"beta-{uuid_pkg.uuid4().hex[:6]}")
    _member(db_session, alpha, owner, "owner")
    _member(db_session, alpha, other, "admin")
    _member(db_session, beta, owner, "owner")
    return owner, other, alpha, beta


def test_get_membership_returns_row_or_none(db_session, team):
    owner, other, alpha, beta = team
    membership = group_queries.get_membership(db_session, alpha.id, other.id)
    assert membership is not None
    assert membership.role == "admin"
    assert group_queries.get_membership(db_session, beta.id, other.id) is None


def test_count_members_counts_only_that_group(db_session, team):
    _, _, alpha, beta = team
    assert group_queries.count_members(db_session, alpha.id) == 2
    assert group_queries.count_members(db_session, beta.id) == 1


def test_member_counts_and_roles_are_keyed_by_group(db_session, team):
    owner, other, alpha, beta = team
    ids = [alpha.id, beta.id]
    assert group_queries.member_counts_by_group(db_session, ids) == {alpha.id: 2, beta.id: 1}
    assert group_queries.roles_by_group(db_session, ids, other.id) == {alpha.id: "admin"}
    assert group_queries.roles_by_group(db_session, ids, owner.id) == {
        alpha.id: "owner",
        beta.id: "owner",
    }


def test_list_groups_for_member_filters_by_tenant(db_session, team):
    owner, other, alpha, beta = team
    personal = group_queries.list_groups_for_member(db_session, owner.id, organization_id=None)
    assert {g.id for g in personal} == {alpha.id, beta.id}
    assert group_queries.list_groups_for_member(db_session, other.id, organization_id=None) == [
        alpha
    ]
    assert group_queries.list_groups_for_member(db_session, owner.id, organization_id=987654) == []


def test_group_name_taken_respects_owner_and_exclusion(db_session, team):
    owner, other, alpha, _ = team
    assert group_queries.group_name_taken(db_session, owner.id, alpha.name)
    assert not group_queries.group_name_taken(db_session, other.id, alpha.name)
    assert not group_queries.group_name_taken(
        db_session, owner.id, alpha.name, exclude_group_id=alpha.id
    )


def test_list_members_with_users_loads_every_member(db_session, team):
    _, _, alpha, _ = team
    members = group_queries.list_members_with_users(db_session, alpha.id)
    assert {m.user.full_name for m in members} == {"owner", "other"}
