"""``organization_service.resolve_owner_org_id`` — the last-resort tenant guess.

Only the community-edition invariant matters here: the membership table
determines exactly one thing — whether a file gets stamped with an
organization id at all — and it must NEVER attribute a user to an org they
are not a member of, or to an org whose membership belongs to someone else.
That would be a cross-tenant data leak the moment the returned id is used to
stamp a re-ingested file (see the module docstring: storage recovery is the
only remaining caller).

All tests run against the real Postgres via the savepoint-rolled-back
``db_session`` fixture — no mocking, this module is pure SQL.
"""

import uuid as uuid_pkg

import pytest

from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.services.organization_service import resolve_owner_org_id


def _mk_org(db_session, *, active: bool = True) -> Organization:
    uid = uuid_pkg.uuid4().hex[:8]
    org = Organization(
        external_org_id=f"org-{uid}",
        name=f"Org {uid}",
        is_active=active,
    )
    db_session.add(org)
    db_session.flush()
    return org


def _mk_membership(db_session, *, org: Organization, user_id: int, role: str = "org:member"):
    membership = OrganizationMembership(organization_id=org.id, user_id=user_id, role=role)
    db_session.add(membership)
    db_session.flush()
    return membership


def test_user_with_no_membership_resolves_to_personal_scope(db_session, normal_user):
    """No membership at all — the community-edition default — returns None."""
    result = resolve_owner_org_id(db_session, normal_user.id)

    assert result is None


def test_user_with_one_active_org_resolves_to_it(db_session, normal_user):
    org = _mk_org(db_session, active=True)
    _mk_membership(db_session, org=org, user_id=normal_user.id)

    result = resolve_owner_org_id(db_session, normal_user.id)

    assert result == org.id


def test_membership_in_an_inactive_org_is_not_returned(db_session, normal_user):
    """An inactive org must never be handed back as the owner's scope.

    Defect shape this guards: an org disabled by an admin (deprovisioned,
    suspended) must stop attributing new files to it, even though the
    membership row itself is untouched.
    """
    inactive_org = _mk_org(db_session, active=False)
    _mk_membership(db_session, org=inactive_org, user_id=normal_user.id)

    result = resolve_owner_org_id(db_session, normal_user.id)

    assert result is None


def test_active_membership_returned_even_when_an_inactive_one_also_exists(db_session, normal_user):
    """Control for the test above: filtering happens, it isn't accidental total exclusion."""
    inactive_org = _mk_org(db_session, active=False)
    active_org = _mk_org(db_session, active=True)
    # Inactive membership created first, so a naive "first row" read would
    # need to also filter, not just order, to get this right.
    _mk_membership(db_session, org=inactive_org, user_id=normal_user.id)
    _mk_membership(db_session, org=active_org, user_id=normal_user.id)

    result = resolve_owner_org_id(db_session, normal_user.id)

    assert result == active_org.id


def test_multiple_active_orgs_resolves_to_the_earliest_membership(db_session, normal_user):
    """Multi-org users get the FIRST membership by id, a documented guess.

    Both orgs are created up front (org ids are a shared sequence under
    parallel test workers, so their relative order isn't asserted). The
    memberships are then added in the OPPOSITE order to org creation, so a
    result keyed on membership-id order provably disagrees with one keyed on
    org-id order — the test can only pass if the function really orders by
    ``OrganizationMembership.id``.
    """
    org_created_first = _mk_org(db_session, active=True)
    org_created_second = _mk_org(db_session, active=True)

    # Join the org created SECOND first, so its membership row gets the
    # lower membership id despite the higher org id.
    joined_first = _mk_membership(db_session, org=org_created_second, user_id=normal_user.id)
    joined_second = _mk_membership(db_session, org=org_created_first, user_id=normal_user.id)
    assert joined_first.id < joined_second.id

    result = resolve_owner_org_id(db_session, normal_user.id)

    assert result == org_created_second.id


@pytest.fixture
def two_isolated_users(db_session, normal_user, admin_user):
    """Two distinct users, each a member of their OWN org only."""
    org_a = _mk_org(db_session, active=True)
    org_b = _mk_org(db_session, active=True)
    _mk_membership(db_session, org=org_a, user_id=normal_user.id)
    _mk_membership(db_session, org=org_b, user_id=admin_user.id)
    return org_a, org_b


def test_never_resolves_to_another_users_organization(
    db_session, normal_user, admin_user, two_isolated_users
):
    """Tenant isolation: resolving user A's org must never hand back user B's.

    This is the property that matters for issue #474's tenant-isolation
    check: the join filters strictly on ``OrganizationMembership.user_id``,
    so cross-tenant attribution here would mean a re-ingested file getting
    stamped with a completely unrelated organization.
    """
    org_a, org_b = two_isolated_users

    result_a = resolve_owner_org_id(db_session, normal_user.id)
    result_b = resolve_owner_org_id(db_session, admin_user.id)

    assert result_a == org_a.id
    assert result_b == org_b.id
    assert result_a != result_b


def test_user_with_no_membership_never_inherits_an_unrelated_org(
    db_session, normal_user, two_isolated_users
):
    """A third, membership-less user must resolve to None, not org A or org B."""
    from app.core.security import get_password_hash
    from app.models.user import User

    uid = uuid_pkg.uuid4().hex[:8]
    bystander = User(
        email=f"bystander_{uid}@example.com",
        full_name="Bystander",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(bystander)
    db_session.flush()

    result = resolve_owner_org_id(db_session, bystander.id)

    assert result is None
