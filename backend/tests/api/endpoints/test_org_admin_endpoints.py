"""``/api/org-admin`` over HTTP — the three irreversible tenant surfaces.

``tests/test_org_admin_gdpr.py`` covers the *service* functions and calls
``get_org_audit_logs`` as a plain Python function. Not one request in the repo ever
reached these routes through the router, which left four properties completely
unasserted — every one of them a control whose removal is silent:

* **The ``confirm=true`` double opt-in.** ``POST /gdpr/erase-organization`` schedules an
  irreversible whole-tenant erasure (since #1105 it runs after a delay, with re-auth). Regressing the guard to a no-op means the
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

The scheduling call is recorded in the tests that check *routing*, because what is under
test there is which org id and which actor the endpoint hands it, and an immediate erasure
is a failure. One test lets the executor run the real ``erase_organization`` so the confirmed
path is proved to actually destroy something, later. The re-auth, listing and cancel rules
are in ``tests/api/test_org_admin_erasure.py``.
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
    org.owner_user_id = normal_user.id  # organization erasure is the owner's alone (v468)
    db_session.commit()
    org_context(org_id=org.id, org_role="org:admin", only_for=normal_user.id)
    return user_token_headers


#: The org admin's re-authentication (``normal_user``'s password in conftest).
REAUTH = {"password": "password123"}


@pytest.fixture
def erasure_recorder(monkeypatch, tmp_path) -> list[dict]:
    """Record every scheduled erasure the routes ask for, and fail on any immediate one.

    Returns the call list; empty means the endpoint refused before scheduling, which is the
    assertion the ``confirm``, membership and re-auth guards need. The real ``schedule`` still
    runs, so a 202 is backed by a real ledger entry. Since #1105 neither route erases anything
    itself: an immediate call into the erasure services is a failure, not a recording.
    """
    from app.core.config import settings
    from app.services import scheduled_erasure_service as scheduled

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    calls: list[dict] = []
    real_schedule = scheduled.schedule

    def _record(db, **kwargs):
        organization = kwargs.get("organization")
        subject_user = kwargs.get("subject_user")
        calls.append(
            {
                "kind": kwargs["subject_type"],
                "org_id": organization.id if organization is not None else None,
                "user_id": subject_user.id if subject_user is not None else None,
                "actor_user_id": kwargs["actor"].id,
                "actor_kind": kwargs["actor_kind"],
            }
        )
        return real_schedule(db, **kwargs)

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("an organization admin's request must not erase anything now")

    monkeypatch.setattr(scheduled, "schedule", _record)
    monkeypatch.setattr("app.services.gdpr_erasure_service.erase_organization", _must_not_run)
    monkeypatch.setattr("app.services.gdpr_erasure_service.erase_org_member_data", _must_not_run)
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

    def test_confirmed_request_targets_the_callers_own_org_and_records_the_actor(
        self, client, org, normal_user, org_admin_request, erasure_recorder
    ):
        """The org id is taken from the resolved context, never from the request —
        a client-supplied org id here would be cross-tenant erasure."""
        response = client.post(
            f"{ERASE_ORG_PATH}?confirm=true", headers=org_admin_request, json=REAUTH
        )
        assert response.status_code == 202, response.text
        assert erasure_recorder == [
            {
                "kind": "organization",
                "org_id": org.id,
                "user_id": None,
                "actor_user_id": normal_user.id,
                "actor_kind": "org_admin",
            }
        ]

    def test_confirmed_request_destroys_nothing_until_the_executor_runs_it(
        self, client, org, org_admin_request, db_session, monkeypatch, tmp_path
    ):
        """The unpatched service, over HTTP and then through the executor: proves
        ``confirm=true`` guards something real, so the 400 above is a control and not a
        formality, and that the request alone deletes nothing (#1105).

        Only the voiceprint sweep is stubbed — it is the one unconditional
        OpenSearch call, and the savepoint rollback cannot undo an index write.
        """
        from app.core.config import settings
        from app.models.erasure import ErasureLedgerEntry
        from app.services import scheduled_erasure_service as scheduled

        monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(
            "app.services.gdpr_erasure_service._erase_speaker_voiceprints",
            lambda **_kwargs: 0,
        )
        org_id = org.id
        response = client.post(
            f"{ERASE_ORG_PATH}?confirm=true", headers=org_admin_request, json=REAUTH
        )
        assert response.status_code == 202, response.text
        db_session.expire_all()
        assert db_session.query(Organization).filter(Organization.id == org_id).first()

        entry = (
            db_session.query(ErasureLedgerEntry)
            .filter(ErasureLedgerEntry.uuid == uuid_pkg.UUID(response.json()["ledger_uuid"]))
            .one()
        )
        assert scheduled._execute_one(db_session, entry.id, entry.scheduled_for) is True
        db_session.expire_all()
        assert db_session.query(Organization).filter(Organization.id == org_id).first() is None

    def test_the_202_body_is_the_request_receipt_not_an_erasure_summary(
        self, client, org_admin_request, erasure_recorder
    ):
        """Before #1105 this route answered 200 with the erasure summary (``complete``,
        ``errors``). It now answers 202 with the receipt; an integrator scripting against it
        reads ``scheduled_for`` and the ledger uuid, and the summary arrives as the erasure
        receipt once the executor has run it."""
        response = client.post(
            f"{ERASE_ORG_PATH}?confirm=true", headers=org_admin_request, json=REAUTH
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["subject_type"] == "organization"
        assert body["status"] == "scheduled"
        assert body["scheduled_for"] > body["requested_at"]
        assert "complete" not in body and "errors" not in body


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

    def test_member_of_the_callers_org_is_scheduled_with_both_scopes_threaded(
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

        response = client.post(
            _erase_user_path(str(other_user.uuid)), headers=org_admin_request, json=REAUTH
        )
        assert response.status_code == 202, response.text
        assert erasure_recorder == [
            {
                "kind": "org_member",
                "user_id": other_user.id,
                "org_id": org.id,
                "actor_user_id": normal_user.id,
                "actor_kind": "org_admin",
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

    def test_target_user_id_outside_the_org_is_403(
        self, client, other_user, org_admin_request, audit_query_recorder
    ):
        """The SUBJECT-side sibling of ``test_user_id_outside_the_org_is_403``
        (issue #443/#828): ``target_user_id`` names who an action was done TO,
        not who did it, but it is exactly as capable of a cross-tenant oracle if
        left unguarded — an org admin could enumerate "was anything ever done to
        user N" for a user outside their own org by supplying an id and reading
        whether any rows came back."""
        response = client.get(
            AUDIT_LOGS_PATH, params={"target_user_id": other_user.id}, headers=org_admin_request
        )
        assert response.status_code == 403, response.text
        assert audit_query_recorder == [], "the refused filter must not reach the query"

    def test_target_user_id_of_a_member_is_allowed_and_scoped(
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
            AUDIT_LOGS_PATH, params={"target_user_id": other_user.id}, headers=org_admin_request
        )
        assert response.status_code == 200, response.text
        assert len(audit_query_recorder) == 1
        sent = audit_query_recorder[0]
        assert sent["target_user_id"] == other_user.id
        assert sent["scope_org_id"] == org.id
        assert set(sent["scope_user_ids"]) == {normal_user.id, other_user.id}

    def test_negative_offset_is_422(self, client, org_admin_request, audit_query_recorder):
        response = client.get(AUDIT_LOGS_PATH, params={"offset": -1}, headers=org_admin_request)
        assert response.status_code == 422, response.text
        assert audit_query_recorder == []
