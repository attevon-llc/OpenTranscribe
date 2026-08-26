"""The SCIM 2.0 surface: authentication, semantics, and the refusals.

Three things this suite exists to pin, all of which are the difference between a
provisioning endpoint and a privilege-escalation endpoint:

* **A bearer token is the only credential.** A browser session must not work here,
  or any authenticated user's browser could be made to drive provisioning.
* **Deactivation revokes.** ``active: false`` and ``DELETE`` both disable AND kill
  the sessions; a disabled account whose refresh token keeps rotating is not
  disabled.
* **``super_admin`` is untouchable and unmintable.**

Plus the honesty checks on the ``PATCH`` surface: an unsupported path must be a
``400 invalidPath``, never a 200 for a change that did not happen.
"""

# mypy: disable-error-code="arg-type"
# The suite builds ORM rows and dict payloads and hands them to signatures typed for
# the real schemas. Declared once here rather than as a cast at every call site.
from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core.security import get_password_hash
from app.models.group import MEMBERSHIP_SOURCE_MANUAL
from app.models.group import MEMBERSHIP_SOURCE_SCIM
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.user import User
from app.services import scim_token_service

#: Deliberately NOT pinned to the migration DDL worker. The hazard is real — v382's tests
#: drop and recreate `scim_token`, taking an ACCESS EXCLUSIVE lock that cascades to `user`
#: via the FK, and this suite inserts into both — but `xdist_group` was the wrong tool for
#: it: sharing a worker serialises this suite against 111 DDL-group tests and put its 28
#: tests on the suite's critical path. The advisory lock in `db_session` already gives real
#: cross-worker exclusion (SHARED here, EXCLUSIVE there), which is strictly stronger, so the
#: pin bought nothing (issue #431). Rows here are uniquely named per test (`_unique_email`),
#: so nothing else needs same-worker ordering.

BASE = "/scim/v2"


def _unique_email(prefix: str = "scim") -> str:
    return f"{prefix}-{uuid_pkg.uuid4().hex[:8]}@example.com"


@pytest.fixture
def scim_headers(db_session, admin_user) -> dict[str, str]:
    """A live SCIM token, issued the way a super_admin would."""
    _row, plaintext = scim_token_service.issue_token(
        db_session, name="pytest-idp", created_by=int(admin_user.id)
    )
    return {"Authorization": f"Bearer {plaintext}"}


@pytest.fixture
def existing_user(db_session) -> User:
    user = User(
        email=_unique_email("target"),
        full_name="Ada Lovelace",
        hashed_password=get_password_hash("irrelevant-Passphrase99!"),
        role="user",
        auth_type="local",
        is_active=True,
        is_superuser=False,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


class TestAuthentication:
    def test_no_token_is_a_scim_error_body(self, client):
        response = client.get(f"{BASE}/Users")

        assert response.status_code == 401
        body = response.json()
        assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
        # RFC 7644 §3.12: status is a STRING, and strict clients validate that.
        assert body["status"] == "401"
        assert response.headers["www-authenticate"].startswith("Bearer")

    def test_a_browser_session_does_not_authenticate_scim(self, client, admin_token_headers):
        """Cookies and user JWTs are deliberately not accepted here."""
        response = client.get(f"{BASE}/Users", headers=admin_token_headers)
        assert response.status_code == 401

    def test_an_unknown_token_is_refused(self, client):
        response = client.get(f"{BASE}/Users", headers={"Authorization": "Bearer ot_scim_nope"})
        assert response.status_code == 401

    def test_a_revoked_token_stops_working(self, client, db_session, admin_user):
        row, plaintext = scim_token_service.issue_token(
            db_session, name="to-revoke", created_by=int(admin_user.id)
        )
        headers = {"Authorization": f"Bearer {plaintext}"}
        assert client.get(f"{BASE}/Users", headers=headers).status_code == 200

        scim_token_service.revoke_token(db_session, str(row.uuid))
        assert client.get(f"{BASE}/Users", headers=headers).status_code == 401

    def test_the_plaintext_is_never_stored(self, db_session, admin_user):
        _row, plaintext = scim_token_service.issue_token(
            db_session, name="hash-check", created_by=int(admin_user.id)
        )
        stored = {r.token_hash for r in scim_token_service.list_tokens(db_session)}
        assert plaintext not in stored
        assert scim_token_service.hash_token(plaintext) in stored


class TestDiscovery:
    def test_service_provider_config_reports_what_is_true(self, client, scim_headers):
        body = client.get(f"{BASE}/ServiceProviderConfig", headers=scim_headers).json()

        assert body["patch"]["supported"] is True
        assert body["filter"]["supported"] is True
        # Advertising a capability we do not implement makes a connector issue
        # requests that fail for reasons its administrator cannot see.
        assert body["bulk"]["supported"] is False
        assert body["sort"]["supported"] is False
        assert body["changePassword"]["supported"] is False

    def test_schemas_and_resource_types_are_served(self, client, scim_headers):
        assert client.get(f"{BASE}/ResourceTypes", headers=scim_headers).status_code == 200
        assert client.get(f"{BASE}/ResourceTypes/User", headers=scim_headers).status_code == 200
        assert client.get(f"{BASE}/ResourceTypes/Nope", headers=scim_headers).status_code == 404
        assert client.get(f"{BASE}/Schemas", headers=scim_headers).status_code == 200


class TestUsers:
    def test_create_returns_201_and_the_account_uuid_as_id(self, client, scim_headers):
        email = _unique_email("created")
        response = client.post(
            f"{BASE}/Users",
            headers=scim_headers,
            json={"schemas": [], "userName": email, "name": {"givenName": "Grace"}},
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["userName"] == email
        assert body["active"] is True
        # The hybrid-ID rule: never the integer primary key.
        uuid_pkg.UUID(body["id"])

    def test_a_duplicate_username_is_409_not_a_silent_update(
        self, client, scim_headers, existing_user
    ):
        response = client.post(
            f"{BASE}/Users",
            headers=scim_headers,
            json={"schemas": [], "userName": str(existing_user.email)},
        )
        assert response.status_code == 409
        assert response.json()["scimType"] == "uniqueness"

    def test_filter_by_username(self, client, scim_headers, existing_user):
        response = client.get(
            f"{BASE}/Users",
            headers=scim_headers,
            params={"filter": f'userName eq "{existing_user.email}"'},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["totalResults"] == 1
        assert body["Resources"][0]["userName"] == str(existing_user.email)

    def test_an_unsupported_filter_is_refused_not_ignored(self, client, scim_headers):
        """The whole point: a partial filter implementation returns wrong answers."""
        response = client.get(
            f"{BASE}/Users", headers=scim_headers, params={"filter": 'active eq "true"'}
        )
        assert response.status_code == 400
        assert response.json()["scimType"] == "invalidFilter"

    def test_pagination_is_bounded(self, client, scim_headers):
        body = client.get(
            f"{BASE}/Users", headers=scim_headers, params={"count": 1000000, "startIndex": 1}
        ).json()
        assert body["itemsPerPage"] <= 500
        assert body["startIndex"] == 1

    def test_a_malformed_id_is_a_clean_404(self, client, scim_headers):
        assert client.get(f"{BASE}/Users/not-a-uuid", headers=scim_headers).status_code == 404

    def test_put_replace_user_is_a_genuine_full_replace(self, client, scim_headers, existing_user):
        """RFC 7644 defines ``PUT`` as a full resource replacement: an attribute
        the request omits must be cleared, not left as whatever the resource
        already held. ``scim_service.update_user(..., full_replace=True)`` now
        implements exactly that (issue #582) — a ``PUT`` body that omits
        ``externalId`` clears it rather than leaving the previously-set value.
        """
        set_external_id = client.put(
            f"{BASE}/Users/{existing_user.uuid}",
            headers=scim_headers,
            json={
                "schemas": [],
                "userName": str(existing_user.email),
                "externalId": "external-123",
                "active": True,
            },
        )
        assert set_external_id.status_code == 200, set_external_id.text
        assert set_external_id.json()["externalId"] == "external-123"

        # A second PUT that omits externalId entirely blanks it under a REAL
        # full replace.
        omitted = client.put(
            f"{BASE}/Users/{existing_user.uuid}",
            headers=scim_headers,
            json={"schemas": [], "userName": str(existing_user.email), "active": True},
        )
        assert omitted.status_code == 200, omitted.text
        assert omitted.json()["externalId"] is None, (
            "PUT is a full replace — externalId omitted from the body must be "
            "cleared, not left as the value set by the previous PUT"
        )

    def test_put_replace_user_updates_display_name(self, client, scim_headers, existing_user):
        response = client.put(
            f"{BASE}/Users/{existing_user.uuid}",
            headers=scim_headers,
            json={
                "schemas": [],
                "userName": str(existing_user.email),
                "name": {"formatted": "Ada Byron"},
                "active": True,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["displayName"] == "Ada Byron"


class TestDeactivation:
    def test_active_false_disables_and_revokes(
        self, client, scim_headers, db_session, existing_user
    ):
        from app.auth.token_service import token_service

        token_service.create_refresh_token(
            db=db_session,
            user_id=int(existing_user.id),
            user_uuid=str(existing_user.uuid),
            role=str(existing_user.role),
        )

        response = client.patch(
            f"{BASE}/Users/{existing_user.uuid}",
            headers=scim_headers,
            json={"schemas": [], "Operations": [{"op": "replace", "value": {"active": False}}]},
        )

        assert response.status_code == 200, response.text
        assert response.json()["active"] is False
        db_session.refresh(existing_user)
        assert existing_user.is_active is False

        from app.models.refresh_token import RefreshToken

        live = (
            db_session.query(RefreshToken)
            .filter(
                RefreshToken.user_id == existing_user.id,
                RefreshToken.revoked_at.is_(None),
            )
            .count()
        )
        assert live == 0, "deactivation must revoke sessions, not just clear a flag"

    def test_delete_is_a_soft_disable(self, client, scim_headers, db_session, existing_user):
        """Not a row deletion: a connector dropping scope must not erase content."""
        response = client.delete(f"{BASE}/Users/{existing_user.uuid}", headers=scim_headers)

        assert response.status_code == 204
        db_session.refresh(existing_user)
        assert existing_user.is_active is False
        assert db_session.query(User).filter(User.id == existing_user.id).first() is not None


class TestSuperAdminIsUntouchable:
    def test_scim_cannot_deactivate_a_super_admin(self, client, scim_headers, super_admin_user):
        response = client.patch(
            f"{BASE}/Users/{super_admin_user.uuid}",
            headers=scim_headers,
            json={"schemas": [], "Operations": [{"op": "replace", "value": {"active": False}}]},
        )
        assert response.status_code == 403
        assert response.json()["scimType"] == "mutability"

    def test_scim_cannot_mint_a_super_admin(self, client, scim_headers, db_session):
        """There is no role attribute at all, and a stray one must not be honoured."""
        email = _unique_email("escalate")
        response = client.post(
            f"{BASE}/Users",
            headers=scim_headers,
            json={"schemas": [], "userName": email, "role": "super_admin", "is_superuser": True},
        )

        assert response.status_code == 201
        created = db_session.query(User).filter(User.email == email).first()
        assert created is not None
        assert str(created.role) == "user"
        assert bool(created.is_superuser) is False


class TestPatchHonesty:
    def test_an_unsupported_user_path_is_400_invalid_path(
        self, client, scim_headers, existing_user
    ):
        response = client.patch(
            f"{BASE}/Users/{existing_user.uuid}",
            headers=scim_headers,
            json={
                "schemas": [],
                "Operations": [{"op": "replace", "path": "phoneNumbers", "value": "555"}],
            },
        )
        assert response.status_code == 400
        assert response.json()["scimType"] == "invalidPath"

    def test_remove_on_a_user_is_refused(self, client, scim_headers, existing_user):
        response = client.patch(
            f"{BASE}/Users/{existing_user.uuid}",
            headers=scim_headers,
            json={"schemas": [], "Operations": [{"op": "remove", "path": "displayName"}]},
        )
        assert response.status_code == 400

    def test_the_okta_path_form_works(self, client, scim_headers, existing_user):
        response = client.patch(
            f"{BASE}/Users/{existing_user.uuid}",
            headers=scim_headers,
            json={
                "schemas": [],
                "Operations": [{"op": "replace", "path": "displayName", "value": "Ada L"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["displayName"] == "Ada L"


class TestGroups:
    @pytest.fixture
    def group(self, db_session, admin_user) -> UserGroup:
        row = UserGroup(name=f"scim-group-{uuid_pkg.uuid4().hex[:8]}", owner_id=int(admin_user.id))
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        return row

    def test_create_and_read_back(self, client, scim_headers, existing_user):
        name = f"scim-created-{uuid_pkg.uuid4().hex[:8]}"
        response = client.post(
            f"{BASE}/Groups",
            headers=scim_headers,
            json={
                "schemas": [],
                "displayName": name,
                "members": [{"value": str(existing_user.uuid)}],
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["displayName"] == name
        assert {m["value"] for m in body["members"]} == {str(existing_user.uuid)}

    def test_add_and_remove_members(self, client, scim_headers, group, existing_user):
        add = client.patch(
            f"{BASE}/Groups/{group.uuid}",
            headers=scim_headers,
            json={
                "schemas": [],
                "Operations": [
                    {"op": "add", "path": "members", "value": [{"value": str(existing_user.uuid)}]}
                ],
            },
        )
        assert add.status_code == 200
        assert {m["value"] for m in add.json()["members"]} == {str(existing_user.uuid)}

        # The Entra value-path form, which is not optional in practice.
        remove = client.patch(
            f"{BASE}/Groups/{group.uuid}",
            headers=scim_headers,
            json={
                "schemas": [],
                "Operations": [
                    {"op": "remove", "path": f'members[value eq "{existing_user.uuid}"]'}
                ],
            },
        )
        assert remove.status_code == 200
        assert remove.json()["members"] == []

    def test_a_manual_membership_survives_a_scim_removal(
        self, client, scim_headers, db_session, group, existing_user
    ):
        """Whoever wrote the row owns it — the same rule directory sync obeys."""
        db_session.add(
            UserGroupMember(
                group_id=int(group.id),
                user_id=int(existing_user.id),
                role="member",
                source=MEMBERSHIP_SOURCE_MANUAL,
            )
        )
        db_session.commit()

        response = client.patch(
            f"{BASE}/Groups/{group.uuid}",
            headers=scim_headers,
            json={
                "schemas": [],
                "Operations": [
                    {
                        "op": "remove",
                        "path": "members",
                        "value": [{"value": str(existing_user.uuid)}],
                    }
                ],
            },
        )

        assert response.status_code == 200
        assert {m["value"] for m in response.json()["members"]} == {str(existing_user.uuid)}

    def test_scim_written_memberships_carry_the_scim_source(
        self, client, scim_headers, db_session, group, existing_user
    ):
        client.patch(
            f"{BASE}/Groups/{group.uuid}",
            headers=scim_headers,
            json={
                "schemas": [],
                "Operations": [
                    {"op": "add", "path": "members", "value": [{"value": str(existing_user.uuid)}]}
                ],
            },
        )
        row = (
            db_session.query(UserGroupMember)
            .filter(
                UserGroupMember.group_id == group.id,
                UserGroupMember.user_id == existing_user.id,
            )
            .first()
        )
        assert row is not None
        assert str(row.source) == MEMBERSHIP_SOURCE_SCIM

    def test_an_unknown_member_id_is_400_not_a_silent_skip(self, client, scim_headers, group):
        response = client.patch(
            f"{BASE}/Groups/{group.uuid}",
            headers=scim_headers,
            json={
                "schemas": [],
                "Operations": [
                    {"op": "add", "path": "members", "value": [{"value": str(uuid_pkg.uuid4())}]}
                ],
            },
        )
        assert response.status_code == 400

    def test_an_unsupported_group_path_is_refused(self, client, scim_headers, group):
        response = client.patch(
            f"{BASE}/Groups/{group.uuid}",
            headers=scim_headers,
            json={
                "schemas": [],
                "Operations": [{"op": "replace", "path": "externalId", "value": "x"}],
            },
        )
        assert response.status_code == 400
        assert response.json()["scimType"] == "invalidPath"

    def test_list_groups_envelope(self, client, scim_headers, group):
        response = client.get(f"{BASE}/Groups", headers=scim_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
        assert body["startIndex"] == 1
        assert body["itemsPerPage"] == len(body["Resources"])
        assert body["totalResults"] >= 1
        assert str(group.uuid) in {r["id"] for r in body["Resources"]}
        listed = next(r for r in body["Resources"] if r["id"] == str(group.uuid))
        assert listed["displayName"] == group.name
        assert listed["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:Group"]

    def test_get_group_by_id(self, client, scim_headers, group):
        response = client.get(f"{BASE}/Groups/{group.uuid}", headers=scim_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(group.uuid)
        assert body["displayName"] == group.name
        assert body["members"] == []

    def test_get_group_nonexistent_id_is_a_scim_404(self, client, scim_headers):
        response = client.get(f"{BASE}/Groups/{uuid_pkg.uuid4()}", headers=scim_headers)

        assert response.status_code == 404
        body = response.json()
        assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
        assert body["status"] == "404"

    def test_put_replaces_membership_fully_and_requires_display_name(
        self, client, scim_headers, group, existing_user
    ):
        """``PUT /Groups/{id}`` is a genuine RFC 7644 full replace on both
        attributes (issue #582):

        * ``members`` IS a full replace — ``_resolve_member_ids`` is built from
          exactly the ``members`` the request body carries, so omitting it (an
          empty list, the schema default) clears the group down to zero members.
        * ``displayName`` is REQUIRED, exactly as it is on ``POST /Groups`` —
          ``user_group.name`` is ``NOT NULL``, so there is no value a full
          replace could clear it *to*. A blank/omitted ``displayName`` is
          therefore a 400, not a silent no-op that leaves the old name in place.
        """
        # Seed a member first so the PUT below has something to clear.
        client.patch(
            f"{BASE}/Groups/{group.uuid}",
            headers=scim_headers,
            json={
                "schemas": [],
                "Operations": [
                    {"op": "add", "path": "members", "value": [{"value": str(existing_user.uuid)}]}
                ],
            },
        )

        blank_name = client.put(
            f"{BASE}/Groups/{group.uuid}",
            headers=scim_headers,
            json={"schemas": [], "displayName": "", "members": []},
        )
        assert blank_name.status_code == 400, blank_name.text

        new_name = f"{group.name}-replaced"
        response = client.put(
            f"{BASE}/Groups/{group.uuid}",
            headers=scim_headers,
            json={"schemas": [], "displayName": new_name, "members": []},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        # members: full replace — cleared, because the PUT body named none.
        assert body["members"] == []
        # displayName: full replace — set to exactly what the request carried.
        assert body["displayName"] == new_name

    def test_delete_group_is_a_real_deletion(self, client, scim_headers, db_session, group):
        response = client.delete(f"{BASE}/Groups/{group.uuid}", headers=scim_headers)

        assert response.status_code == 204
        assert db_session.query(UserGroup).filter(UserGroup.id == group.id).first() is None, (
            "unlike a SCIM user, a group holds no content and DELETE really removes the row"
        )


class TestTokenAdminSurface:
    def test_a_plain_admin_cannot_issue_a_token(self, client, admin_token_headers):
        response = client.post(
            "/api/admin/scim-tokens", headers=admin_token_headers, json={"name": "nope"}
        )
        assert response.status_code == 403

    def test_a_super_admin_gets_the_plaintext_exactly_once(self, client, super_admin_token_headers):
        created = client.post(
            "/api/admin/scim-tokens",
            headers=super_admin_token_headers,
            json={"name": "issued-by-test"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["token"].startswith("ot_scim_")

        listed = client.get("/api/admin/scim-tokens", headers=super_admin_token_headers)
        assert listed.status_code == 200
        row = next(r for r in listed.json() if r["uuid"] == created.json()["uuid"])
        assert "token" not in row, "the secret must never be listed"
