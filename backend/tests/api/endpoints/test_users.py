"""User endpoint tests."""

import uuid as uuid_pkg

import pytest

from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.user import User


@pytest.fixture(autouse=True)
def _spy_storage_reclaim_phase(monkeypatch):
    """Neutralize the object-storage/OpenSearch reclaim phase (issue #695).

    Same rationale as ``test_admin.py``'s sibling fixture — this module's deletion
    tests seed rows but never upload real objects. A spy, not a silent stub: it
    asserts the plan is shaped like an ``AccountPurgePlan`` before reporting a clean
    sweep.
    """
    import app.services.file_cleanup_service as fcs

    calls: list[fcs.AccountPurgePlan] = []

    def _spy(plan: fcs.AccountPurgePlan) -> list[dict]:
        assert isinstance(plan, fcs.AccountPurgePlan)
        for file_plan in plan.files:
            assert "file_uuid" in file_plan
            assert "storage_path" in file_plan
        calls.append(plan)
        return []

    monkeypatch.setattr(fcs, "purge_account_external_copies", _spy)
    yield calls


#: The five fields ``users.py`` strips on both update paths. Sending any one of
#: them must leave the stored value alone.
#:
#: Each entry is ``(field, escalating_value, expected_unchanged_value)``. Before
#: this was parametrized, the suite asserted only ``is_active`` — so a regression
#: that stopped stripping ``role`` (a plain admin promoting themselves, or any
#: user promoting themselves through ``PUT /users/me``) left the tests green.
PRIVILEGED_FIELDS = [
    ("is_active", False, True),
    ("is_superuser", True, False),
    ("role", "super_admin", "user"),
    ("auth_type", "ldap", "local"),
    ("allow_local_fallback", True, False),
]


def test_get_users(client, admin_token_headers, normal_user, admin_user):
    """Test listing all users (admin only endpoint)"""
    response = client.get("/api/users", headers=admin_token_headers)
    assert response.status_code == 200
    users = response.json()
    assert isinstance(users, list)
    # With fixtures, we should have at least 2 users
    assert len(users) >= 2

    # Basic schema validation - response uses uuid not id
    assert "uuid" in users[0]
    assert "email" in users[0]
    assert "full_name" in users[0]
    assert "is_active" in users[0]
    assert "role" in users[0]


def test_get_users_unauthorized(client, user_token_headers):
    """Test that regular users cannot list all users"""
    response = client.get("/api/users", headers=user_token_headers)
    assert response.status_code == 403, (
        response.text
    )  # authenticated but not an admin: get_current_admin_user/get_current_active_superuser raise 403


def test_get_current_user(client, user_token_headers, normal_user):
    """Test getting current user info"""
    response = client.get("/api/users/me", headers=user_token_headers)
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["email"] == normal_user.email
    assert user_data["full_name"] == normal_user.full_name
    # Response uses uuid not id
    assert user_data["uuid"] == str(normal_user.uuid)


def test_update_current_user(client, user_token_headers):
    """Test updating current user info"""
    update_data = {"full_name": "Updated Name"}
    response = client.put("/api/users/me", headers=user_token_headers, json=update_data)
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["full_name"] == "Updated Name"


def test_get_user_by_uuid(client, admin_token_headers, normal_user):
    """Test getting user by UUID (admin only)"""
    response = client.get(f"/api/users/{normal_user.uuid}", headers=admin_token_headers)
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["email"] == normal_user.email
    assert user_data["uuid"] == str(normal_user.uuid)


def test_get_user_by_uuid_invalid(client, admin_token_headers):
    """Test getting user with invalid UUID format"""
    response = client.get("/api/users/not-a-valid-uuid", headers=admin_token_headers)
    assert response.status_code == 400
    assert "Invalid UUID format" in response.json()["detail"]


def test_get_user_by_uuid_unauthorized(client, user_token_headers, admin_user):
    """Test that regular users cannot get other users by UUID"""
    response = client.get(f"/api/users/{admin_user.uuid}", headers=user_token_headers)
    assert response.status_code == 403, (
        response.text
    )  # authenticated but not an admin: get_current_admin_user/get_current_active_superuser raise 403


def test_update_user(client, super_admin_token_headers, normal_user):
    """Test updating a user including privileged fields (super_admin only)"""
    update_data = {"full_name": "Admin Updated User", "is_active": False}
    response = client.put(
        f"/api/users/{normal_user.uuid}", headers=super_admin_token_headers, json=update_data
    )
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["full_name"] == "Admin Updated User"
    assert user_data["is_active"] is False


@pytest.mark.parametrize(("field", "sent", "expected"), PRIVILEGED_FIELDS)
def test_update_user_admin_cannot_set_privileged_fields(
    client, admin_token_headers, normal_user, db_session, field, sent, expected
):
    """A plain admin's write must not carry ANY of the five privileged fields.

    This asserted only ``is_active``, which is the least dangerous of the five: a
    regression that stopped stripping ``role`` is privilege escalation — an admin
    promoting an account (or themselves, this route accepts their own uuid) to
    ``super_admin``, the tier that owns auth config and the audit log — and it
    would have left the single-field version of this test green.
    """
    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"full_name": "Admin Updated Name", field: sent},
    )
    assert response.status_code == 200, response.text
    assert response.json()["full_name"] == "Admin Updated Name"
    db_session.refresh(normal_user)
    assert getattr(normal_user, field) == expected


@pytest.mark.parametrize(("field", "sent", "expected"), PRIVILEGED_FIELDS)
def test_update_self_cannot_set_privileged_fields(
    client, user_token_headers, normal_user, db_session, field, sent, expected
):
    """``PUT /users/me`` strips the same five. Without that, self-promotion to
    ``super_admin`` is one request from any account, and the handler's own comment
    says so — but nothing tested it for any field."""
    response = client.put(
        "/api/users/me",
        headers=user_token_headers,
        json={"full_name": "Self Updated Name", field: sent},
    )
    assert response.status_code == 200, response.text
    db_session.refresh(normal_user)
    assert getattr(normal_user, field) == expected


def test_super_admin_can_still_set_a_privileged_field(
    client, super_admin_token_headers, normal_user, db_session
):
    """The control for the two stripping tests above: the fields are genuinely
    settable at the right tier, so a handler that stripped them for EVERYONE
    would not pass all three."""
    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=super_admin_token_headers,
        json={"role": "admin"},
    )
    assert response.status_code == 200, response.text
    db_session.refresh(normal_user)
    assert normal_user.role == "admin"


def test_update_user_sets_account_expiration(client, admin_token_headers, normal_user):
    """account_expires_at (AC-2 time-boxed accounts) is admin-tier, not super_admin-only —
    it is enforced on every request by dependencies.py but, before this write path, had
    no way to ever be set."""
    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"account_expires_at": "2099-01-01T00:00:00"},
    )
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["account_expires_at"] is not None
    assert user_data["account_expires_at"].startswith("2099-01-01")


def test_update_user_clears_account_expiration(client, admin_token_headers, normal_user):
    """`None` clears a previously-set expiration rather than being ignored as unset —
    `exclude_unset=True` means the key must actually be present in the payload."""
    client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"account_expires_at": "2099-01-01T00:00:00"},
    )
    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"account_expires_at": None},
    )
    assert response.status_code == 200
    assert response.json()["account_expires_at"] is None


def test_expired_account_is_refused_on_the_next_request(
    client, admin_token_headers, normal_user, user_token_headers, db_session
):
    """The read half (dependencies.py:_enforce_account_expiry) already enforced this,
    checked per-request rather than at login — this pins that the write half added here
    (setting the column through the admin API) actually reaches it end-to-end."""
    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"account_expires_at": "2020-01-01T00:00:00"},
    )
    assert response.status_code == 200

    response = client.get("/api/users/me", headers=user_token_headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "account_expired"


def test_update_user_invalid_uuid(client, admin_token_headers):
    """Test updating user with invalid UUID format"""
    update_data = {"full_name": "Should Fail"}
    response = client.put(
        "/api/users/not-a-valid-uuid", headers=admin_token_headers, json=update_data
    )
    assert response.status_code == 400
    assert "Invalid UUID format" in response.json()["detail"]


def test_update_user_unauthorized(client, user_token_headers, admin_user):
    """Test that regular users cannot update other users"""
    update_data = {"full_name": "Should Fail"}
    response = client.put(
        f"/api/users/{admin_user.uuid}", headers=user_token_headers, json=update_data
    )
    assert response.status_code == 403, (
        response.text
    )  # authenticated but not an admin: get_current_admin_user/get_current_active_superuser raise 403


def test_delete_user(client, admin_token_headers, normal_user, db_session):
    """Deleting a user with real owned data removes ALL of it.

    ``DELETE /api/users/{uuid}`` is a second entry point into the same three helpers
    that ``DELETE /api/admin/users/{uuid}`` uses, and it differs in one way that
    matters: there is no ``begin_nested()`` savepoint and no ``except Exception``
    around the cascade, so a foreign-key failure here propagates as an unhandled 500
    with a half-deleted account. Like its admin twin, this test used to delete the bare
    ``normal_user`` fixture — an account owning nothing, so every ``if <ids>:`` branch
    in both helpers was skipped and the assertion held regardless of their contents.
    """
    from tests.user_owned_rows import seed_owned_rows

    owned = seed_owned_rows(db_session, normal_user)
    owned.assert_all_present(db_session)

    response = client.delete(f"/api/users/{normal_user.uuid}", headers=admin_token_headers)
    assert response.status_code == 204, response.text

    db_session.expire_all()
    assert db_session.query(User).filter(User.id == owned.user_id).first() is None
    assert owned.remaining(db_session) == {}


def test_delete_user_invalid_uuid(client, admin_token_headers):
    """Test deleting user with invalid UUID format"""
    response = client.delete("/api/users/not-a-valid-uuid", headers=admin_token_headers)
    assert response.status_code == 400
    assert "Invalid UUID format" in response.json()["detail"]


def test_delete_user_unauthorized(client, user_token_headers, admin_user):
    """Test that regular users cannot delete other users"""
    response = client.delete(f"/api/users/{admin_user.uuid}", headers=user_token_headers)
    assert response.status_code == 403, (
        response.text
    )  # authenticated but not an admin: get_current_admin_user/get_current_active_superuser raise 403


# --------------------------------------------------------------------------- #
# GET /users/search — the sharing autocomplete, and the only plain-user-tier    #
# route that reads other accounts' identities                                  #
# --------------------------------------------------------------------------- #
SEARCH_PATH = "/api/users/search"


def _make_org(db_session, label: str) -> Organization:
    org = Organization(
        external_org_id=f"org_{label}_{uuid_pkg.uuid4().hex[:10]}",
        name=f"{label} Org",
        is_active=True,
    )
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    return org


def _join(db_session, org, user, role: str = "org:member") -> None:
    db_session.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role=role))
    db_session.commit()


def _emails(response) -> set[str]:
    return {row["email"] for row in response.json()}


def test_user_search_requires_a_session(client):
    response = client.get(SEARCH_PATH, params={"q": "test"})
    assert response.status_code == 401, response.text


def test_user_search_requires_two_characters(client, user_token_headers):
    """A one-character query returns a fifth of the directory per request; the
    minimum is the only brake on enumeration this route has."""
    response = client.get(SEARCH_PATH, params={"q": "a"}, headers=user_token_headers)
    assert response.status_code == 422, response.text


def test_user_search_finds_another_account_by_email(client, user_token_headers, other_user):
    """The feature: sharing autocomplete. This is also the control for the
    tenant-scoping tests below — without it, a gate that returned nothing at all
    would look like a pass."""
    response = client.get(
        SEARCH_PATH, params={"q": other_user.email[:6]}, headers=user_token_headers
    )
    assert response.status_code == 200, response.text
    assert other_user.email in _emails(response)


def test_user_search_never_returns_the_caller(client, user_token_headers, normal_user):
    response = client.get(
        SEARCH_PATH, params={"q": normal_user.email[:6]}, headers=user_token_headers
    )
    assert response.status_code == 200, response.text
    assert normal_user.email not in _emails(response)


def test_user_search_omits_deactivated_accounts(client, user_token_headers, other_user, db_session):
    other_user.is_active = False
    db_session.commit()
    response = client.get(
        SEARCH_PATH, params={"q": other_user.email[:6]}, headers=user_token_headers
    )
    assert response.status_code == 200, response.text
    assert other_user.email not in _emails(response)


def test_user_search_does_not_leak_another_tenants_accounts(
    client, user_token_headers, normal_user, other_user, db_session, org_context
):
    """The defect this route shipped with: it filtered on ``User.id !=
    current_user.id`` and ``is_active`` and NOTHING else — no ``ctx.org_id``, no
    ``RequestContext`` at all — while sitting at the plain ``user`` tier. In an
    org deployment that made it a cross-tenant directory: any member of any tenant
    could page every other tenant's active accounts, email plus full name, out of
    it 20 at a time from a two-character query.
    """
    home = _make_org(db_session, "home")
    elsewhere = _make_org(db_session, "elsewhere")
    _join(db_session, home, normal_user, role="org:admin")
    _join(db_session, elsewhere, other_user)
    org_context(org_id=home.id, org_role="org:admin", only_for=normal_user.id)

    response = client.get(
        SEARCH_PATH, params={"q": other_user.email[:6]}, headers=user_token_headers
    )
    assert response.status_code == 200, response.text
    assert other_user.email not in _emails(response)


def test_user_search_still_finds_a_member_of_the_callers_own_tenant(
    client, user_token_headers, normal_user, other_user, db_session, org_context
):
    """The control for the test above: same org context, same query, and the
    member of the caller's OWN tenant is still returned — so the gate is scoping,
    not just emptying the result set."""
    home = _make_org(db_session, "home")
    _join(db_session, home, normal_user, role="org:admin")
    _join(db_session, home, other_user)
    org_context(org_id=home.id, org_role="org:admin", only_for=normal_user.id)

    response = client.get(
        SEARCH_PATH, params={"q": other_user.email[:6]}, headers=user_token_headers
    )
    assert response.status_code == 200, response.text
    assert other_user.email in _emails(response)


def test_user_search_in_personal_scope_omits_org_members(
    client, user_token_headers, other_user, db_session, org_context
):
    """Personal scope mirrors ``scope_to_context``: org rows do not leak into it.
    In the community edition the membership table is empty, so this branch admits
    every account and behaviour is unchanged — which is what the two tests above
    with no org context assert."""
    elsewhere = _make_org(db_session, "elsewhere")
    _join(db_session, elsewhere, other_user)
    org_context(org_id=None, org_role=None)

    response = client.get(
        SEARCH_PATH, params={"q": other_user.email[:6]}, headers=user_token_headers
    )
    assert response.status_code == 200, response.text
    assert other_user.email not in _emails(response)


def test_user_search_is_reachable_by_a_plain_user(client, user_token_headers, other_user):
    """The tenant gate must not have quietly raised the tier: this is sharing
    autocomplete and every account needs it."""
    response = client.get(
        SEARCH_PATH, params={"q": other_user.full_name[:5]}, headers=user_token_headers
    )
    assert response.status_code == 200, response.text
