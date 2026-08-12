"""The tenant boundary the group plane never had (v388, issue #431).

``user_group`` was the one user-owned table with **no** ``organization_id`` column, so
there was nothing for ``scope_to_context`` to filter on and ``endpoints/groups.py`` had no
tenant term anywhere. The concrete consequence, and the reason this suite leads with it:
``add_member`` resolved its target purely by UUID, so a group admin in tenant A could add a
member of tenant B — and because collections are shared with *groups*, that handed the
outsider a sharing surface reaching across the boundary.

Every test here drives the real HTTP chain (bearer token → ``get_current_active_user``'s
lifecycle gates → ``get_current_context``) and real ``organization_membership`` rows, because
``_same_tenant`` reads that table rather than any token claim. Overriding
``get_current_context`` would remove the very gates under test; ``org_context`` replaces only
``resolve_org_context``, which is the one step needing a cloud IdP.

Each cross-tenant assertion is paired with a same-tenant **control** in the same shape. A
"denied" test with no matching "allowed" test cannot distinguish a working boundary from a
route that is simply broken for everyone.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.organization import Organization
from app.models.organization import OrganizationMembership


def _csrf(client, headers: dict[str, str]) -> dict[str, str]:
    """Add the double-submit CSRF header.

    A ``TestClient`` carries a cookie jar, so it looks like a browser to the CSRF
    middleware and every mutation must echo the token or it is a 403 — which would make
    these tests pass for the wrong reason (403 is also what a tenant denial could look
    like, so an un-echoed token would be indistinguishable from a working boundary).
    """
    client.get("/api/auth/session")
    token = client.cookies.get("csrf_token")
    assert token, "no csrf_token cookie issued — the double-submit helper is out of date"
    return {**headers, "X-CSRF-Token": token}


def _org(db_session, name: str) -> Organization:
    org = Organization(name=name, slug=f"{name}-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.flush()
    return org


def _join(db_session, org: Organization, user, role: str = "org:member") -> None:
    db_session.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role=role))
    db_session.flush()


def _group(db_session, owner, *, organization_id: int | None) -> UserGroup:
    """A group owned by ``owner``, stamped into ``organization_id`` (None = personal)."""
    group = UserGroup(
        name=f"g-{uuid_pkg.uuid4().hex[:10]}",
        description="tenancy fixture",
        owner_id=owner.id,
        organization_id=organization_id,
    )
    db_session.add(group)
    db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=owner.id, role="owner"))
    db_session.flush()
    return group


# ---------------------------------------------------------------- add_member


def test_add_member_refuses_a_target_from_another_tenant(
    client, user_token_headers, normal_user, other_user, db_session, org_context
):
    """THE defect v388 exists to close.

    404 rather than 403, with the same ``User not found`` detail an unknown UUID gets, so
    the response cannot be used to confirm that an account exists in another tenant.
    """
    org_a, org_b = _org(db_session, "tenant-a"), _org(db_session, "tenant-b")
    _join(db_session, org_a, normal_user, "org:admin")
    _join(db_session, org_b, other_user)
    org_context(org_id=org_a.id, org_role="org:admin", only_for=normal_user.id)

    group = _group(db_session, normal_user, organization_id=org_a.id)

    response = client.post(
        f"/api/groups/{group.uuid}/members",
        json={"user_uuid": str(other_user.uuid), "role": "member"},
        headers=_csrf(client, user_token_headers),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "User not found"
    assert (
        db_session.query(UserGroupMember)
        .filter_by(group_id=group.id, user_id=other_user.id)
        .first()
        is None
    ), "the membership row was written despite the 404"


def test_add_member_accepts_a_target_in_the_same_tenant(
    client, user_token_headers, normal_user, other_user, db_session, org_context
):
    """The control for the test above — identical setup, both accounts in ONE org.

    Without this, a ``groups.py`` that rejected every target would pass the denial test.
    """
    org_a = _org(db_session, "tenant-a")
    _join(db_session, org_a, normal_user, "org:admin")
    _join(db_session, org_a, other_user)
    org_context(org_id=org_a.id, org_role="org:admin", only_for=normal_user.id)

    group = _group(db_session, normal_user, organization_id=org_a.id)

    response = client.post(
        f"/api/groups/{group.uuid}/members",
        json={"user_uuid": str(other_user.uuid), "role": "member"},
        headers=_csrf(client, user_token_headers),
    )

    assert response.status_code == 201
    assert response.json()["email"] == other_user.email


def test_add_member_in_personal_scope_refuses_an_org_member(
    client, user_token_headers, normal_user, other_user, db_session, org_context
):
    """The personal branch of ``_same_tenant``, which is the community-edition path.

    Personal scope admits only accounts with no org membership at all — the same rule as
    ``scope_to_context``'s personal branch and the gate on ``GET /users/search``. Without
    it, a personal-scope group would be a back door into an org member's account.
    """
    org_b = _org(db_session, "tenant-b")
    _join(db_session, org_b, other_user)
    org_context(org_id=None, org_role=None)

    group = _group(db_session, normal_user, organization_id=None)

    response = client.post(
        f"/api/groups/{group.uuid}/members",
        json={"user_uuid": str(other_user.uuid), "role": "member"},
        headers=_csrf(client, user_token_headers),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "User not found"


def test_add_member_is_unchanged_when_nobody_has_a_membership(
    client, user_token_headers, normal_user, other_user, db_session
):
    """Community-edition invariance, asserted rather than assumed.

    ``organization`` and ``organization_membership`` are empty in the community edition, so
    every account is in personal scope and every group is unstamped — the tenant terms must
    be no-ops. This is the test that would fail if v388 accidentally made the community
    edition stricter.
    """
    group = _group(db_session, normal_user, organization_id=None)

    response = client.post(
        f"/api/groups/{group.uuid}/members",
        json={"user_uuid": str(other_user.uuid), "role": "member"},
        headers=_csrf(client, user_token_headers),
    )

    assert response.status_code == 201


# ------------------------------------------------------- resolving a group


def test_a_group_in_another_tenant_is_404(
    client, user_token_headers, normal_user, other_user, db_session, org_context
):
    """Out-of-tenant is indistinguishable from nonexistent, by design.

    403 would confirm the group exists and let an attacker enumerate another tenant's
    groups by UUID; 404 is the same answer a bogus UUID gets. Same reasoning as
    ``require_capability``'s 404 and the tag plane's 404-on-non-writable.
    """
    org_a, org_b = _org(db_session, "tenant-a"), _org(db_session, "tenant-b")
    _join(db_session, org_a, normal_user)
    _join(db_session, org_b, other_user)
    org_context(org_id=org_a.id, org_role="org:member", only_for=normal_user.id)

    # A group in tenant B that `normal_user` is even a MEMBER of: membership alone must not
    # be enough, or the boundary would depend on nobody ever having been added.
    foreign = _group(db_session, other_user, organization_id=org_b.id)
    db_session.add(UserGroupMember(group_id=foreign.id, user_id=normal_user.id, role="member"))
    db_session.flush()

    response = client.get(f"/api/groups/{foreign.uuid}", headers=user_token_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Group not found"


def test_the_same_group_is_reachable_from_its_own_tenant(
    client, user_token_headers, normal_user, db_session, org_context
):
    """Control for the 404 above: the identical request from inside the tenant is a 200."""
    org_a = _org(db_session, "tenant-a")
    _join(db_session, org_a, normal_user)
    org_context(org_id=org_a.id, org_role="org:member", only_for=normal_user.id)

    group = _group(db_session, normal_user, organization_id=org_a.id)

    response = client.get(f"/api/groups/{group.uuid}", headers=user_token_headers)

    assert response.status_code == 200
    assert response.json()["uuid"] == str(group.uuid)


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("put", ""),
        ("delete", ""),
        ("post", "/members"),
    ],
)
def test_every_mutating_route_refuses_an_out_of_tenant_group(
    client, user_token_headers, normal_user, other_user, db_session, org_context, method, suffix
):
    """The boundary is in one helper, so prove all of its callers actually use it.

    Parametrised rather than written once: the fix was a per-route substitution, and a
    single missed call site is the whole vulnerability back again.
    """
    org_a, org_b = _org(db_session, "tenant-a"), _org(db_session, "tenant-b")
    _join(db_session, org_a, normal_user, "org:admin")
    _join(db_session, org_b, other_user)
    org_context(org_id=org_a.id, org_role="org:admin", only_for=normal_user.id)

    foreign = _group(db_session, other_user, organization_id=org_b.id)
    db_session.add(UserGroupMember(group_id=foreign.id, user_id=normal_user.id, role="owner"))
    db_session.flush()

    url = f"/api/groups/{foreign.uuid}{suffix}"
    headers = _csrf(client, user_token_headers)
    payloads = {
        "put": {"name": "renamed-by-an-outsider"},
        "post": {"user_uuid": str(normal_user.uuid), "role": "member"},
        "delete": None,
    }
    response = getattr(client, method)(
        url, headers=headers, **({"json": payloads[method]} if payloads[method] else {})
    )

    assert response.status_code == 404
    db_session.rollback()


# ------------------------------------------------------------------ listing


def test_list_groups_omits_a_personal_group_while_in_org_context(
    client, user_token_headers, normal_user, db_session, org_context
):
    """Switching scope must switch which groups exist.

    Membership already limits the listing to the caller's own groups, so without the
    tenant term an org-context session would still see every personal group it belongs to —
    the listing would silently span tenants even though each group's detail page is gated.
    """
    org_a = _org(db_session, "tenant-a")
    _join(db_session, org_a, normal_user)
    org_context(org_id=org_a.id, org_role="org:member", only_for=normal_user.id)

    personal = _group(db_session, normal_user, organization_id=None)
    in_org = _group(db_session, normal_user, organization_id=org_a.id)

    listed = {g["uuid"] for g in client.get("/api/groups", headers=user_token_headers).json()}

    assert str(in_org.uuid) in listed
    assert str(personal.uuid) not in listed


def test_create_group_stamps_the_active_organization(
    client, user_token_headers, normal_user, db_session, org_context
):
    """A group created while working in an org belongs to the org, not the creator.

    Asserted against the stored row rather than the response, because the response schema
    deliberately does not expose ``organization_id`` — so only the database can say whether
    the stamp happened.
    """
    org_a = _org(db_session, "tenant-a")
    _join(db_session, org_a, normal_user)
    org_context(org_id=org_a.id, org_role="org:member", only_for=normal_user.id)

    name = f"stamped-{uuid_pkg.uuid4().hex[:8]}"
    response = client.post(
        "/api/groups",
        json={"name": name, "description": "created in org scope"},
        headers=_csrf(client, user_token_headers),
    )
    assert response.status_code == 201

    stored = db_session.query(UserGroup).filter_by(name=name).one()
    assert stored.organization_id == org_a.id
