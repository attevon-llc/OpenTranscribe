"""``/api/org-admin`` over HTTP — the three irreversible tenant surfaces.

``tests/test_org_admin_gdpr.py`` covers the *service* functions and calls
``get_org_audit_logs`` as a plain Python function. Not one request in the repo ever
reached these routes through the router, which left four properties completely
unasserted — every one of them a control whose removal is silent:

* **The ``confirm=true`` double opt-in.** ``POST /gdpr/erase-organization`` is
  irreversible whole-tenant erasure. Regressing the guard to a no-op means the
  first unconfirmed call — a mis-typed curl, a UI button wired without the
  query param — erases the tenant.
* **``require_org_admin`` in the dependency chain.** If it degrades to
  ``get_current_active_user``, any signed-in member of the tenant can erase it.
* **The target-must-be-a-member 403.** Without it an org admin erases a member of
  *another* tenant, and the erasure is scoped by the caller's own org id, so the
  audit trail names the wrong tenant.
* **The ``organizations`` capability answering 404, not 403.** This is the one
  capability whose 404 is never asserted anywhere (``test_cloud_seams_capabilities.py``
  covers ``watch_sources``/``llm``/``engine``/``auth-config``), and it is the only
  one guarding an irreversible verb. A 403 here would tell any community-edition
  user that a whole-tenant erasure route exists on their deployment.

The erasure services themselves are replaced by a recorder in the tests that check
*routing*, because what is under test there is which org id and which actor the
endpoint hands them — not the row deletion, which the service suite already pins.
One test lets the real ``erase_organization`` run so the confirmed path is proved
to actually destroy something.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.organization import Organization
from app.models.organization import OrganizationMembership

ERASE_ORG_PATH = "/api/org-admin/gdpr/erase-organization"
AUDIT_LOGS_PATH = "/api/org-admin/audit-logs"


def _erase_user_path(user_uuid: str) -> str:
    return f"/api/org-admin/gdpr/erase-user/{user_uuid}"


@pytest.fixture
def org(db_session) -> Organization:
    """An organization with no data in it (rows are added per test)."""
    row = Organization(
        external_org_id=f"org_httptest_{uuid_pkg.uuid4().hex[:10]}",
        name=f"HTTP Test Org {uuid_pkg.uuid4().hex[:6]}",
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def org_admin_request(org, normal_user, user_token_headers, org_context, db_session):
    """``normal_user`` is an ``org:admin`` of ``org`` for the whole request chain.

    The platform role stays ``user`` on purpose: org-admin authority comes from the
    membership mirror, never from ``User.role``, and a test that granted platform
    admin as well could not tell the two apart.
    """
    db_session.add(
        OrganizationMembership(organization_id=org.id, user_id=normal_user.id, role="org:admin")
    )
    db_session.commit()
    org_context(org_id=org.id, org_role="org:admin", only_for=normal_user.id)
    return user_token_headers


@pytest.fixture
def erasure_recorder(monkeypatch) -> list[dict]:
    """Record calls to both erasure services instead of performing them.

    Returns the call list; empty means the endpoint refused before the service,
    which is the assertion the ``confirm`` and membership guards need.
    """
    calls: list[dict] = []

    def _record_org(_db, org_id, *, actor_user_id=None, actor_email=None):
        calls.append(
            {
                "kind": "organization",
                "org_id": org_id,
                "actor_user_id": actor_user_id,
                "actor_email": actor_email,
            }
        )
        return {"scope": "organization", "target_id": org_id, "errors": []}

    def _record_member(_db, user_id, org_id, *, actor_user_id=None, actor_email=None):
        calls.append(
            {
                "kind": "member",
                "user_id": user_id,
                "org_id": org_id,
                "actor_user_id": actor_user_id,
                "actor_email": actor_email,
            }
        )
        return {"scope": "org_member", "target_id": user_id, "errors": []}

    monkeypatch.setattr("app.services.gdpr_erasure_service.erase_organization", _record_org)
    monkeypatch.setattr("app.services.gdpr_erasure_service.erase_org_member_data", _record_member)
    return calls


# --------------------------------------------------------------------------- #
# The capability gate: 404, never 403                                          #
# --------------------------------------------------------------------------- #
class TestCommunityEditionIs404:
    """``organizations`` is False in the community edition, so the whole router
    must look like an unknown path. A 403 would advertise the surface."""

    def test_erase_organization_is_404_for_a_plain_user(self, client, user_token_headers):
        response = client.post(f"{ERASE_ORG_PATH}?confirm=true", headers=user_token_headers)
        assert response.status_code == 404, response.text

    def test_erase_user_is_404_for_a_plain_user(self, client, user_token_headers, other_user):
        response = client.post(_erase_user_path(str(other_user.uuid)), headers=user_token_headers)
        assert response.status_code == 404, response.text

    def test_audit_logs_is_404_for_a_plain_user(self, client, user_token_headers):
        response = client.get(AUDIT_LOGS_PATH, headers=user_token_headers)
        assert response.status_code == 404, response.text

    def test_erase_organization_is_404_for_a_platform_admin(self, client, admin_token_headers):
        """A platform admin is not an org admin, and the capability is still off:
        the answer must not become 403 just because the caller is privileged."""
        response = client.post(f"{ERASE_ORG_PATH}?confirm=true", headers=admin_token_headers)
        assert response.status_code == 404, response.text

    def test_erase_organization_is_404_unauthenticated(self, client):
        """The capability dependency is a ROUTER dependency, so it is solved before
        the route's auth dependency: a hidden surface must not leak its existence
        through a 401 either."""
        response = client.post(f"{ERASE_ORG_PATH}?confirm=true")
        assert response.status_code == 404, response.text

    def test_capability_off_erases_nothing(self, client, user_token_headers, erasure_recorder):
        client.post(f"{ERASE_ORG_PATH}?confirm=true", headers=user_token_headers)
        assert erasure_recorder == []


# --------------------------------------------------------------------------- #
# require_org_admin, in the real dependency chain                              #
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("organizations_capability_on")
class TestRequireOrgAdminOverHttp:
    """With the surface enabled, authority comes from the membership mirror."""

    def test_unauthenticated_is_401_once_the_surface_exists(self, client):
        response = client.post(f"{ERASE_ORG_PATH}?confirm=true")
        assert response.status_code == 401, response.text

    def test_signed_in_user_with_no_org_context_cannot_erase_the_org(
        self, client, user_token_headers, erasure_recorder
    ):
        """The degradation that matters: if ``require_org_admin`` were relaxed to
        ``get_current_active_user``, this call would erase a tenant."""
        response = client.post(f"{ERASE_ORG_PATH}?confirm=true", headers=user_token_headers)
        assert response.status_code == 403, response.text
        assert erasure_recorder == []

    def test_plain_org_member_cannot_erase_the_org(
        self, client, org, normal_user, user_token_headers, org_context, erasure_recorder
    ):
        org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)
        response = client.post(f"{ERASE_ORG_PATH}?confirm=true", headers=user_token_headers)
        assert response.status_code == 403, response.text
        assert erasure_recorder == []

    def test_plain_org_member_cannot_erase_a_member(
        self,
        client,
        org,
        normal_user,
        other_user,
        user_token_headers,
        org_context,
        erasure_recorder,
    ):
        org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)
        response = client.post(_erase_user_path(str(other_user.uuid)), headers=user_token_headers)
        assert response.status_code == 403, response.text
        assert erasure_recorder == []

    def test_plain_org_member_cannot_read_the_org_audit_log(
        self, client, org, normal_user, user_token_headers, org_context
    ):
        org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)
        response = client.get(AUDIT_LOGS_PATH, headers=user_token_headers)
        assert response.status_code == 403, response.text

    def test_platform_admin_without_org_context_cannot_read_the_org_audit_log(
        self, client, admin_token_headers
    ):
        """Platform admin ≠ org admin. An org surface answers to the tenant role
        only, so the global audit read stays at its own super-admin route."""
        response = client.get(AUDIT_LOGS_PATH, headers=admin_token_headers)
        assert response.status_code == 403, response.text


# --------------------------------------------------------------------------- #
# The confirm=true double opt-in                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("organizations_capability_on")
class TestEraseOrganizationConfirmGate:
    def test_missing_confirm_is_400(self, client, org_admin_request, erasure_recorder):
        response = client.post(ERASE_ORG_PATH, headers=org_admin_request)
        assert response.status_code == 400, response.text
        assert erasure_recorder == [], "an unconfirmed call must not reach the erasure"

    def test_confirm_false_is_400(self, client, org_admin_request, erasure_recorder):
        response = client.post(f"{ERASE_ORG_PATH}?confirm=false", headers=org_admin_request)
        assert response.status_code == 400, response.text
        assert erasure_recorder == []

    def test_missing_confirm_leaves_the_organization_in_place(
        self, client, org, org_admin_request, db_session
    ):
        """The DB-level statement of the same guard: no service patching at all, so
        this fails if the refusal ever stops being a refusal."""
        response = client.post(ERASE_ORG_PATH, headers=org_admin_request)
        assert response.status_code == 400, response.text
        db_session.expire_all()
        still_there = db_session.query(Organization).filter(Organization.id == org.id).first()
        assert still_there is not None

    def test_confirmed_erasure_targets_the_callers_own_org_and_records_the_actor(
        self, client, org, normal_user, org_admin_request, erasure_recorder
    ):
        """The org id is taken from the resolved context, never from the request —
        a client-supplied org id here would be cross-tenant erasure."""
        response = client.post(f"{ERASE_ORG_PATH}?confirm=true", headers=org_admin_request)
        assert response.status_code == 200, response.text
        assert erasure_recorder == [
            {
                "kind": "organization",
                "org_id": org.id,
                "actor_user_id": normal_user.id,
                "actor_email": normal_user.email,
            }
        ]

    def test_confirmed_erasure_really_destroys_the_org(
        self, client, org, org_admin_request, db_session, monkeypatch
    ):
        """The unpatched service, over HTTP: proves ``confirm=true`` guards
        something real, so the 400 above is a control and not a formality.

        Only the voiceprint sweep is stubbed — it is the one unconditional
        OpenSearch call, and the savepoint rollback cannot undo an index write.
        """
        monkeypatch.setattr(
            "app.services.gdpr_erasure_service._erase_speaker_voiceprints",
            lambda **_kwargs: 0,
        )
        response = client.post(f"{ERASE_ORG_PATH}?confirm=true", headers=org_admin_request)
        assert response.status_code == 200, response.text
        db_session.expire_all()
        assert db_session.query(Organization).filter(Organization.id == org.id).first() is None

    def test_a_partial_erasure_is_visible_in_the_200_body(
        self, client, org, org_admin_request, monkeypatch
    ):
        """An erasure that could not destroy every copy still answers **200**.

        That is the current contract, and it is deliberately pinned here rather
        than left implicit: the only way a caller scripting against this route
        can tell a complete erasure from a partial one is the body. So the body
        must say so — ``complete: false`` plus a populated ``errors`` list — and
        a future move to 207/`Multi-Status` has to come through this test rather
        than silently changing what an integrator sees.

        The failure injected is an unreachable OpenSearch during the biometric
        sweep, which is exactly the outage that used to answer ``errors: []``.
        """
        monkeypatch.setattr("app.services.opensearch_service.opensearch_client", None)
        monkeypatch.setattr("app.services.opensearch_service.client.opensearch_client", None)
        monkeypatch.setattr("app.services.search.indexing_service.opensearch_client", None)

        response = client.post(f"{ERASE_ORG_PATH}?confirm=true", headers=org_admin_request)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["complete"] is False
        assert body["errors"], "a partial erasure must name what survived"
        assert any(e.get("stage") == "voiceprints" for e in body["errors"])


# --------------------------------------------------------------------------- #
# Org-member erasure: the target must be a member of THIS org                  #
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("organizations_capability_on")
class TestEraseOrgMemberTargeting:
    def test_target_outside_the_org_is_403(
        self, client, other_user, org_admin_request, erasure_recorder
    ):
        """``other_user`` has no membership row. Without this 403 an org admin
        erases data belonging to another tenant's member — and the erasure would
        be stamped with the *caller's* org id, so the audit trail would name the
        wrong tenant."""
        response = client.post(_erase_user_path(str(other_user.uuid)), headers=org_admin_request)
        assert response.status_code == 403, response.text
        assert erasure_recorder == []

    def test_target_in_another_org_is_403(
        self, client, other_user, org_admin_request, erasure_recorder, db_session
    ):
        other_org = Organization(
            external_org_id=f"org_other_{uuid_pkg.uuid4().hex[:10]}",
            name="Other Tenant",
            is_active=True,
        )
        db_session.add(other_org)
        db_session.commit()
        db_session.add(
            OrganizationMembership(
                organization_id=other_org.id, user_id=other_user.id, role="org:member"
            )
        )
        db_session.commit()

        response = client.post(_erase_user_path(str(other_user.uuid)), headers=org_admin_request)
        assert response.status_code == 403, response.text
        assert erasure_recorder == []

    def test_unknown_user_uuid_is_404(self, client, org_admin_request, erasure_recorder):
        response = client.post(_erase_user_path(str(uuid_pkg.uuid4())), headers=org_admin_request)
        assert response.status_code == 404, response.text
        assert erasure_recorder == []

    def test_member_of_the_callers_org_is_erased_with_both_scopes_threaded(
        self,
        client,
        org,
        normal_user,
        other_user,
        org_admin_request,
        erasure_recorder,
        db_session,
    ):
        db_session.add(
            OrganizationMembership(organization_id=org.id, user_id=other_user.id, role="org:member")
        )
        db_session.commit()

        response = client.post(_erase_user_path(str(other_user.uuid)), headers=org_admin_request)
        assert response.status_code == 200, response.text
        assert erasure_recorder == [
            {
                "kind": "member",
                "user_id": other_user.id,
                "org_id": org.id,
                "actor_user_id": normal_user.id,
                "actor_email": normal_user.email,
            }
        ]


# --------------------------------------------------------------------------- #
# Audit-log read: the user_id filter cannot point outside the org              #
# --------------------------------------------------------------------------- #
@pytest.fixture
def audit_query_recorder(monkeypatch) -> list[dict]:
    """Record the arguments the audit read would send to OpenSearch."""
    calls: list[dict] = []

    def _record(**kwargs):
        calls.append(kwargs)
        return {"logs": [], "total": 0, "offset": 0, "limit": kwargs.get("limit", 100)}

    monkeypatch.setattr("app.api.endpoints.org_admin.query_audit_logs", _record)
    return calls


@pytest.mark.usefixtures("organizations_capability_on")
class TestOrgAuditLogRead:
    def test_user_id_outside_the_org_is_403(
        self, client, other_user, org_admin_request, audit_query_recorder
    ):
        """Losing this turns the org audit read into a cross-tenant oracle: the
        filter is applied inside ``query_audit_logs``, so an unchecked ``user_id``
        returns another tenant's events verbatim."""
        response = client.get(
            AUDIT_LOGS_PATH, params={"user_id": other_user.id}, headers=org_admin_request
        )
        assert response.status_code == 403, response.text
        assert audit_query_recorder == [], "the refused filter must not reach the query"

    def test_user_id_of_a_member_is_allowed_and_scoped(
        self,
        client,
        org,
        normal_user,
        other_user,
        org_admin_request,
        audit_query_recorder,
        db_session,
    ):
        db_session.add(
            OrganizationMembership(organization_id=org.id, user_id=other_user.id, role="org:member")
        )
        db_session.commit()

        response = client.get(
            AUDIT_LOGS_PATH, params={"user_id": other_user.id}, headers=org_admin_request
        )
        assert response.status_code == 200, response.text
        assert len(audit_query_recorder) == 1
        sent = audit_query_recorder[0]
        assert sent["user_id"] == other_user.id
        assert sent["scope_org_id"] == org.id
        assert set(sent["scope_user_ids"]) == {normal_user.id, other_user.id}

    def test_unfiltered_read_is_scoped_to_the_orgs_members(
        self, client, org, normal_user, other_user, org_admin_request, audit_query_recorder
    ):
        """``other_user`` is not a member, so their id must be absent from the
        scope set even without an explicit filter."""
        response = client.get(AUDIT_LOGS_PATH, headers=org_admin_request)
        assert response.status_code == 200, response.text
        sent = audit_query_recorder[0]
        assert set(sent["scope_user_ids"]) == {normal_user.id}
        assert other_user.id not in sent["scope_user_ids"]

    def test_limit_above_the_cap_is_422(self, client, org_admin_request, audit_query_recorder):
        """``limit`` is ``le=1000``; an unbounded page size on an audit read is a
        cheap way to pull the whole index."""
        response = client.get(AUDIT_LOGS_PATH, params={"limit": 5000}, headers=org_admin_request)
        assert response.status_code == 422, response.text
        assert audit_query_recorder == []

    def test_negative_offset_is_422(self, client, org_admin_request, audit_query_recorder):
        response = client.get(AUDIT_LOGS_PATH, params={"offset": -1}, headers=org_admin_request)
        assert response.status_code == 422, response.text
        assert audit_query_recorder == []
