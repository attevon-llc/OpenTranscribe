"""``/api/org-admin/files`` over HTTP: organization-scoped quarantine and release (#1253).

Every property below is one whose removal is silent. The cross-tenant cases are the ones
that matter most: the routes hold the power to hide a file from its owner, so an
organization administrator reaching another tenant's file would be a takedown of someone
else's data. The authority cases pin "platform wins": an organization can release what an
organization placed, never what the operator placed.

Built on ``support_world`` (two organizations, an org admin for the first, a platform
admin, a super_admin, real bearer tokens, the real dependency chain; only the cloud IdP
step ``resolve_org_context`` is replaced). The ``org.takedown`` capability is OFF unless a
test asks for it, exactly as in the community edition.
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import MagicMock

import pytest

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.core.config import settings
from app.models.organization import OrganizationMembership
from tests.fixtures.support_world import GRANT_HEADER
from tests.fixtures.support_world import login_headers
from tests.fixtures.support_world import make_file
from tests.fixtures.support_world import make_user

BASE = "/api/org-admin/files"
PLATFORM = "/api/admin/files"
NOT_FOUND = {"detail": "File not found"}
OPERATOR_CONTACT = "abuse@operator.example"


def _quarantine(client, headers, file_uuid, reason="org notice", **body):
    return client.post(
        f"{BASE}/{file_uuid}/quarantine", headers=headers, json={"reason": reason, **body}
    )


def _release(client, headers, file_uuid, **body):
    return client.post(f"{BASE}/{file_uuid}/release", headers=headers, json=body)


def _platform_quarantine(client, headers, file_uuid, reason="operator notice", **body):
    return client.post(
        f"{PLATFORM}/{file_uuid}/quarantine",
        headers=headers,
        json={"reason": reason, "operator_action_reason": "operator action (test)", **body},
    )


def _state(db, file):
    db.refresh(file)
    return (
        file.is_quarantined,
        file.legal_hold,
        file.quarantine_reason,
        file.takedown_origin,
        file.quarantined_by,
        file.status,
    )


def _code(response) -> str:
    return str(response.json()["detail"]["code"])


@pytest.fixture
def org_takedown_on():
    """Enable ``org.takedown`` (and ``organizations``) for one test, as the cloud resolver does."""
    from app.core.capabilities import COMMUNITY_CAPABILITIES
    from app.core.capabilities import reset_capability_resolver
    from app.core.capabilities import set_capability_resolver

    set_capability_resolver(
        lambda _r: {**COMMUNITY_CAPABILITIES, "organizations": True, "org.takedown": True}
    )
    yield
    reset_capability_resolver()


@pytest.fixture
def world(support_world, org_takedown_on):
    """``support_world`` plus an administrator of the SECOND organization and a member."""
    w = support_world
    admin_b = make_user(w.db, role="user", label="orgb_admin")
    w.db.add(
        OrganizationMembership(organization_id=w.other_org.id, user_id=admin_b.id, role="org:admin")
    )
    w.db.commit()
    w.roles[admin_b.id] = (w.other_org.id, "org:admin")
    w.roles[w.owner.id] = (w.org.id, "org:member")
    w.admin_a_headers = w.org_admin_headers
    w.admin_b_headers = login_headers(w.client, admin_b)
    w.admin_b = admin_b
    w.file2 = make_file(w.db, w.owner, w.org.id)
    return w


@pytest.fixture
def audit(monkeypatch):
    """Capture every takedown audit event instead of writing it."""
    fake = MagicMock()
    monkeypatch.setattr("app.services.takedown_notices.audit_logger", fake)
    return fake


@pytest.fixture
def storage(monkeypatch):
    """Record every storage and cache side effect a takedown has."""
    tag = MagicMock(return_value=True)
    hold = MagicMock()
    bump = MagicMock()
    monkeypatch.setattr("app.services.minio_service.set_object_quarantine_tag", tag)
    monkeypatch.setattr("app.services.minio_service.set_object_legal_hold", hold)
    monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", bump)
    return MagicMock(tag=tag, hold=hold, bump=bump)


@pytest.fixture
def notices(monkeypatch):
    """Capture the owner notices."""
    fake = MagicMock()
    monkeypatch.setattr("app.services.notification_service.send_task_notification", fake)
    return fake


def _events(audit, event_type=None, outcome=None):
    out = []
    for call in audit.log.call_args_list:
        kw = call.kwargs
        if event_type is not None and kw["event_type"] != event_type:
            continue
        if outcome is not None and kw["outcome"] != outcome:
            continue
        out.append(kw)
    return out


# --------------------------------------------------------------------------- #
# Tenant isolation, both directions                                            #
# --------------------------------------------------------------------------- #
class TestTenantIsolation:
    def test_each_list_shows_only_its_own_organizations_files(self, world):
        w = world
        assert _quarantine(w.client, w.admin_a_headers, w.file.uuid).status_code == 200
        assert _quarantine(w.client, w.admin_b_headers, w.other_org_file.uuid).status_code == 200

        a = w.client.get(f"{BASE}/quarantined", headers=w.admin_a_headers).json()
        b = w.client.get(f"{BASE}/quarantined", headers=w.admin_b_headers).json()

        assert [f["uuid"] for f in a["files"]] == [str(w.file.uuid)] and a["total"] == 1
        assert [f["uuid"] for f in b["files"]] == [str(w.other_org_file.uuid)] and b["total"] == 1

    def test_the_list_total_counts_only_the_callers_organization(self, world):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid)
        _quarantine(w.client, w.admin_a_headers, w.file2.uuid)
        _quarantine(w.client, w.admin_b_headers, w.other_org_file.uuid)
        listing = w.client.get(f"{BASE}/quarantined?limit=1", headers=w.admin_a_headers).json()
        assert listing["total"] == 2 and len(listing["files"]) == 1

    @pytest.mark.parametrize("attacker", ["a", "b"])
    def test_quarantine_cannot_reach_another_tenants_file(self, world, storage, attacker):
        w = world
        headers = w.admin_a_headers if attacker == "a" else w.admin_b_headers
        foreign = w.other_org_file if attacker == "a" else w.file
        before = _state(w.db, foreign)

        responses = [
            _quarantine(w.client, headers, foreign.uuid),
            _quarantine(w.client, headers, w.personal_file.uuid),
            _quarantine(w.client, headers, uuid_pkg.uuid4()),
        ]

        # A foreign file, a personal file and a missing file are one and the same answer.
        assert [r.status_code for r in responses] == [404, 404, 404]
        assert [r.json() for r in responses] == [NOT_FOUND] * 3
        assert _state(w.db, foreign) == before
        assert _state(w.db, w.personal_file)[0] is False
        storage.tag.assert_not_called()
        storage.bump.assert_not_called()

    @pytest.mark.parametrize("attacker", ["a", "b"])
    def test_release_cannot_reach_another_tenants_file(self, world, attacker):
        w = world
        headers = w.admin_a_headers if attacker == "a" else w.admin_b_headers
        foreign, owner_headers = (
            (w.other_org_file, w.admin_b_headers)
            if attacker == "a"
            else (w.file, w.admin_a_headers)
        )
        assert _quarantine(w.client, owner_headers, foreign.uuid).status_code == 200
        before = _state(w.db, foreign)

        response = _release(w.client, headers, foreign.uuid)

        assert response.status_code == 404 and response.json() == NOT_FOUND
        assert _state(w.db, foreign) == before
        assert before[0] is True

    def test_a_malformed_uuid_is_a_format_error_not_an_existence_signal(self, world):
        w = world
        assert _quarantine(w.client, w.admin_a_headers, "not-a-uuid").status_code == 400

    def test_a_probe_is_audited_in_the_actors_organization_with_no_file_reference(
        self, world, audit
    ):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.other_org_file.uuid)

        (event,) = _events(audit, AuditEventType.ADMIN_FILE_QUARANTINE, AuditOutcome.FAILURE)
        assert event["organization_id"] == w.org.id, "the prober's organization, never the victim's"
        assert event["error_code"] == "takedown_file_not_found"
        assert "file_uuid" not in event["details"] and "file_id" not in event["details"]
        assert event.get("target_user_id") is None


# --------------------------------------------------------------------------- #
# Who may call at all                                                          #
# --------------------------------------------------------------------------- #
class TestWhoMayCall:
    def _all(self, w, headers):
        u = uuid_pkg.uuid4()
        return [
            w.client.get(f"{BASE}/quarantined", headers=headers),
            w.client.post(f"{BASE}/{u}/quarantine", headers=headers, json={"reason": "x"}),
            w.client.post(f"{BASE}/{u}/release", headers=headers, json={}),
        ]

    def test_an_org_member_is_forbidden_on_all_three(self, world):
        w = world
        assert [r.status_code for r in self._all(w, w.owner_headers)] == [403, 403, 403]

    def test_a_platform_admin_with_no_organization_is_forbidden(self, world):
        w = world
        assert [r.status_code for r in self._all(w, w.admin_headers)] == [403, 403, 403]

    def test_anonymous_is_unauthorized(self, world):
        w = world
        w.client.cookies.clear()  # login set the httpOnly session cookie on this client
        assert [r.status_code for r in self._all(w, {})] == [401, 401, 401]

    def test_the_owner_cannot_release_their_own_quarantined_file(self, world):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid)
        assert _release(w.client, w.owner_headers, w.file.uuid).status_code == 403
        assert (
            w.client.post(
                f"{PLATFORM}/{w.file.uuid}/release", headers=w.owner_headers, json={}
            ).status_code
            == 403
        )
        assert _state(w.db, w.file)[0] is True


class TestCapability:
    """``org.takedown`` is server-enforced: off means the routes do not exist."""

    def test_every_route_is_404_when_the_capability_is_off(self, support_world):
        w = support_world  # no org_takedown_on: the community default
        u = uuid_pkg.uuid4()
        w.client.cookies.clear()  # the session cookie would make the "anonymous" call signed in
        all_callers: tuple[dict[str, str], ...] = (
            w.org_admin_headers,
            w.admin_headers,
            w.super_headers,
            {},
        )
        for headers in all_callers:
            codes = [
                w.client.get(f"{BASE}/quarantined", headers=headers).status_code,
                w.client.post(
                    f"{BASE}/{u}/quarantine", headers=headers, json={"reason": "x"}
                ).status_code,
                w.client.post(f"{BASE}/{u}/release", headers=headers, json={}).status_code,
            ]
            assert codes == [404, 404, 404], f"a 403/401 here would advertise the surface: {codes}"

    def test_a_superuser_gets_no_bypass(self, support_world, monkeypatch):
        """These are not a platform surface, so ``is_superuser`` must not unlock them.

        ``require_capability`` resolves the bypass user through its own short-lived
        session, which cannot see the fixture's uncommitted user, so the lookup is
        answered directly: without this the 404 below would hold whether or not the
        route opted out of the bypass.
        """
        w = support_world
        superuser = MagicMock(is_superuser=True)
        monkeypatch.setattr(
            "app.api.endpoints.auth.get_optional_current_user", lambda _request, _db: superuser
        )
        assert w.client.get(f"{BASE}/quarantined", headers=w.super_headers).status_code == 404

    def test_the_default_is_off_for_the_community_edition(self):
        from app.core.capabilities import CAPABILITY_AUDIENCE
        from app.core.capabilities import COMMUNITY_CAPABILITIES

        assert COMMUNITY_CAPABILITIES["org.takedown"] is False
        assert CAPABILITY_AUDIENCE["org.takedown"] == "org_admin"

    def test_the_platform_queue_is_not_capability_gated(self, world):
        """An operator must always be able to act on a legal notice."""
        from app.core.capabilities import COMMUNITY_CAPABILITIES
        from app.core.capabilities import set_capability_resolver

        w = world
        set_capability_resolver(
            lambda _r: {**COMMUNITY_CAPABILITIES, "org.takedown": False, "admin.takedown": False}
        )
        assert w.client.get(f"{PLATFORM}/quarantined", headers=w.admin_headers).status_code == 200


# --------------------------------------------------------------------------- #
# What an organization administrator can do                                    #
# --------------------------------------------------------------------------- #
class TestOrganizationTakedown:
    def test_quarantine_hides_the_file_and_records_the_organization_as_holder(self, world):
        w = world
        response = _quarantine(w.client, w.admin_a_headers, w.file.uuid, "policy 4.2")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_quarantined"] is True and body["takedown_origin"] == "organization"
        assert _state(w.db, w.file)[:4] == (True, False, "policy 4.2", "organization")
        # Hidden from the owner (the whole point of a takedown).
        assert w.client.get(f"/api/files/{w.file.uuid}", headers=w.owner_headers).status_code == 404

    def test_it_never_places_a_legal_hold_even_when_asked(self, world, storage):
        """Delegating the hold is an open authorization decision (a hold blocks the
        owner's account deletion), so the organization route cannot place one."""
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid, legal_hold=True)
        assert _state(w.db, w.file)[1] is False
        storage.hold.assert_not_called()

    def test_release_restores_the_file_and_clears_the_origin(self, world):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid)
        response = _release(w.client, w.admin_a_headers, w.file.uuid, reason="withdrawn")
        assert response.status_code == 200, response.text
        assert response.json()["takedown_origin"] is None
        assert _state(w.db, w.file)[:4] == (False, False, None, None)
        assert w.client.get(f"/api/files/{w.file.uuid}", headers=w.owner_headers).status_code == 200

    def test_releasing_a_file_that_is_not_held_is_a_conflict(self, world):
        w = world
        assert _release(w.client, w.admin_a_headers, w.file.uuid).status_code == 409

    def test_the_reason_is_bounded(self, world):
        w = world
        assert _quarantine(w.client, w.admin_a_headers, w.file.uuid, "x" * 2001).status_code == 422
        assert (
            _release(w.client, w.admin_a_headers, w.file.uuid, reason="x" * 2001).status_code == 422
        )


# --------------------------------------------------------------------------- #
# Platform wins                                                                #
# --------------------------------------------------------------------------- #
class TestPlatformWins:
    @pytest.fixture
    def operator_held(self, world):
        w = world
        r = _platform_quarantine(w.client, w.admin_headers, w.file.uuid, "operator notice")
        assert r.status_code == 200, r.text
        assert r.json()["takedown_origin"] == "platform"
        return w

    def test_an_organization_cannot_release_the_operators_takedown(self, operator_held, storage):
        w = operator_held
        before = _state(w.db, w.file)
        storage.tag.reset_mock()
        storage.bump.reset_mock()

        response = _release(w.client, w.admin_a_headers, w.file.uuid)

        assert response.status_code == 403 and _code(response) == "takedown_platform_origin"
        assert _state(w.db, w.file) == before
        storage.tag.assert_not_called()
        storage.bump.assert_not_called()

    def test_an_organization_cannot_overwrite_the_operators_takedown(
        self, operator_held, storage, notices
    ):
        """The refusal must come BEFORE any side effect: the reason, the object tags, the
        cache version and the owner notice all stay untouched."""
        w = operator_held
        before = _state(w.db, w.file)
        storage.tag.reset_mock()
        storage.bump.reset_mock()
        notices.reset_mock()

        response = _quarantine(w.client, w.admin_a_headers, w.file.uuid, "a rewritten reason")

        assert response.status_code == 409 and _code(response) == "takedown_platform_origin"
        assert _state(w.db, w.file) == before
        assert before[2] == "operator notice"
        storage.tag.assert_not_called()
        storage.bump.assert_not_called()
        notices.assert_not_called()

    def test_a_dual_role_user_gets_no_extra_authority_on_the_organization_route(
        self, operator_held
    ):
        """A super_admin who is also an org admin acts as the organization here; the platform
        role cannot be used to reach the operator's takedown through this door."""
        w = operator_held
        w.db.add(
            OrganizationMembership(
                organization_id=w.org.id, user_id=w.super_admin.id, role="org:admin"
            )
        )
        w.db.commit()
        w.roles[w.super_admin.id] = (w.org.id, "org:admin")
        assert w.super_admin.is_superuser is True

        response = _release(w.client, w.super_headers, w.file.uuid)

        assert response.status_code == 403 and _code(response) == "takedown_platform_origin"
        assert _state(w.db, w.file)[0] is True

    def test_the_operator_can_release_it(self, operator_held):
        w = operator_held
        r = w.client.post(f"{PLATFORM}/{w.file.uuid}/release", headers=w.admin_headers, json={})
        assert r.status_code == 200
        assert _state(w.db, w.file)[:4] == (False, False, None, None)

    def test_the_operator_can_release_an_organizations_takedown(self, world):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid)
        r = w.client.post(f"{PLATFORM}/{w.file.uuid}/release", headers=w.admin_headers, json={})
        assert r.status_code == 200, r.text
        assert _state(w.db, w.file)[0] is False

    def test_the_operator_escalates_an_organization_takedown_and_the_organization_is_locked_out(
        self, world, audit
    ):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid, "the org's reason")
        r = _platform_quarantine(w.client, w.admin_headers, w.file.uuid, "operator reason")
        assert r.status_code == 200, r.text

        assert _state(w.db, w.file)[:4] == (True, True, "operator reason", "platform")
        escalation = _events(audit, AuditEventType.ADMIN_FILE_QUARANTINE, AuditOutcome.SUCCESS)[-1]
        assert escalation["details"]["previous_origin"] == "organization"
        assert escalation["details"]["previous_reason"] == "the org's reason"
        assert _release(w.client, w.admin_a_headers, w.file.uuid).status_code == 403

    def test_a_released_but_still_held_file_stays_the_operators(self, operator_held):
        w = operator_held
        r = w.client.post(
            f"{PLATFORM}/{w.file.uuid}/release",
            headers=w.admin_headers,
            json={"clear_legal_hold": False},
        )
        assert r.status_code == 200 and r.json()["takedown_origin"] == "platform"
        assert _state(w.db, w.file)[:2] == (False, True)

        refused = _release(w.client, w.admin_a_headers, w.file.uuid)
        assert refused.status_code == 403 and _code(refused) == "takedown_platform_origin"
        listing = w.client.get(
            f"{BASE}/quarantined?include_legal_holds=true", headers=w.admin_a_headers
        ).json()
        assert [(f["uuid"], f["can_release"]) for f in listing["files"]] == [
            (str(w.file.uuid), False)
        ]

    def test_an_unrelated_writer_that_sets_only_the_flag_is_still_the_operators(self, world):
        """Fail closed against any code path (or extension) that holds a file without
        recording an origin: the database reads it as the operator's."""
        w = world
        w.file.is_quarantined = True
        w.db.commit()
        w.db.refresh(w.file)
        assert w.file.takedown_origin == "platform"
        assert _release(w.client, w.admin_a_headers, w.file.uuid).status_code == 403


# --------------------------------------------------------------------------- #
# The shape of the queue                                                       #
# --------------------------------------------------------------------------- #
class TestQueuePayload:
    def test_a_platform_row_discloses_neither_reason_nor_actor(self, world):
        w = world
        _platform_quarantine(w.client, w.admin_headers, w.file.uuid, "privileged legal basis")
        _quarantine(w.client, w.admin_a_headers, w.file2.uuid, "org reason")

        rows = {
            f["uuid"]: f
            for f in w.client.get(f"{BASE}/quarantined", headers=w.admin_a_headers).json()["files"]
        }

        operator_row = rows[str(w.file.uuid)]
        assert (
            operator_row["takedown_origin"] == "platform" and operator_row["can_release"] is False
        )
        assert "quarantine_reason" not in operator_row
        assert "quarantined_by_display" not in operator_row
        assert "privileged legal basis" not in str(operator_row)
        assert w.admin.email not in str(operator_row)

        own_row = rows[str(w.file2.uuid)]
        assert own_row["takedown_origin"] == "organization" and own_row["can_release"] is True
        assert own_row["quarantine_reason"] == "org reason"
        assert own_row["quarantined_by_display"]

    def test_a_former_members_address_is_not_shown(self, world):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid)
        row = w.client.get(f"{BASE}/quarantined", headers=w.admin_a_headers).json()["files"][0]
        # `owner` has a membership row (member) and a full name: the name wins; an address is
        # only ever a fallback while the person is still a member.
        assert row["owner_display"] == w.owner.full_name
        w.owner.full_name = None
        w.db.query(OrganizationMembership).filter_by(user_id=w.owner.id).delete()
        w.db.commit()
        row = w.client.get(f"{BASE}/quarantined", headers=w.admin_a_headers).json()["files"][0]
        assert w.owner.email not in str(row) and row["owner_display"] == "Former member"


# --------------------------------------------------------------------------- #
# Audit                                                                        #
# --------------------------------------------------------------------------- #
class TestAudit:
    def test_each_action_writes_one_event_naming_actor_role_authority_and_tenant(
        self, world, audit
    ):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid, "r1")
        _release(w.client, w.admin_a_headers, w.file.uuid, reason="r2")

        (q,) = _events(audit, AuditEventType.ADMIN_FILE_QUARANTINE, AuditOutcome.SUCCESS)
        (r,) = _events(audit, AuditEventType.ADMIN_FILE_RELEASE, AuditOutcome.SUCCESS)
        for event in (q, r):
            assert event["organization_id"] == w.org.id
            assert event["target_user_id"] == w.owner.id
            assert event["user_id"] == w.org_admin.id
            assert event["details"]["actor_role"] == "org_admin"
            assert event["details"]["authority"] == "organization"
            assert event["details"]["file_uuid"] == str(w.file.uuid)
        assert q["details"]["reason"] == "r1"
        assert r["details"]["reason"] == "r2"

    def test_a_platform_action_is_recorded_as_such(self, world, audit):
        w = world
        _platform_quarantine(w.client, w.admin_headers, w.file.uuid)
        (event,) = _events(audit, AuditEventType.ADMIN_FILE_QUARANTINE, AuditOutcome.SUCCESS)
        assert event["details"]["actor_role"] == "platform_admin"
        assert event["details"]["authority"] == "platform"

    def test_a_refused_attempt_is_one_failure_event_and_changes_nothing(self, world, audit):
        w = world
        _platform_quarantine(w.client, w.admin_headers, w.file.uuid)
        audit.reset_mock()

        _release(w.client, w.admin_a_headers, w.file.uuid)

        (event,) = _events(audit)
        assert event["event_type"] == AuditEventType.ADMIN_FILE_RELEASE
        assert event["outcome"] == AuditOutcome.FAILURE
        assert event["error_code"] == "takedown_platform_origin"
        assert event["organization_id"] == w.org.id
        assert event["details"]["actor_role"] == "org_admin"

    def test_a_refusal_below_the_route_is_never_stamped_with_the_targets_organization(
        self, world, audit
    ):
        """The service re-checks the tenant itself; its denial must not land in the victim's log."""
        from app.services.takedown_service import TakedownActor
        from app.services.takedown_service import TakedownRefusedError
        from app.services.takedown_service import quarantine_file

        w = world
        foreign = make_file(w.db, w.owner, w.other_org.id)
        actor = TakedownActor.organization(w.org_admin, w.org.id)

        with pytest.raises(TakedownRefusedError) as refused:
            quarantine_file(
                w.db,
                foreign,
                actor=actor,
                reason="probe",
                operator_action_reason="operator action (test)",
            )

        assert refused.value.status_code == 404
        (event,) = _events(audit)
        assert event["outcome"] == AuditOutcome.FAILURE
        assert event["organization_id"] == w.org.id
        assert event["target_user_id"] is None
        assert "file_uuid" not in event["details"]
        assert _state(w.db, foreign)[0] is False


# --------------------------------------------------------------------------- #
# What the owner is told                                                       #
# --------------------------------------------------------------------------- #
class TestOwnerNotice:
    def _takedown_notice(self, notices):
        (call,) = [c for c in notices.call_args_list if c.args[1] == "file_takedown"]
        return call.args[0], call.kwargs

    def test_an_organization_takedown_names_the_organization_not_the_operator(
        self, world, notices, monkeypatch
    ):
        w = world
        monkeypatch.setattr(settings, "ABUSE_CONTACT_EMAIL", OPERATOR_CONTACT)
        _quarantine(w.client, w.admin_a_headers, w.file.uuid, "policy 4.2")

        user_id, kwargs = self._takedown_notice(notices)
        message = kwargs["message"]
        assert user_id == w.owner.id
        assert "administrator of your organization" in message
        assert "policy 4.2" in message
        assert OPERATOR_CONTACT not in message and kwargs["extra"]["abuse_contact_email"] == ""
        assert w.org_admin.email not in message, "the acting administrator is never disclosed"
        assert kwargs["extra"]["origin"] == "organization"

    def test_a_platform_takedown_still_points_at_the_operators_contact(
        self, world, notices, monkeypatch
    ):
        w = world
        monkeypatch.setattr(settings, "ABUSE_CONTACT_EMAIL", OPERATOR_CONTACT)
        _platform_quarantine(w.client, w.admin_headers, w.file.uuid)

        _, kwargs = self._takedown_notice(notices)
        assert OPERATOR_CONTACT in kwargs["message"]
        assert "service operator" in kwargs["message"]
        assert kwargs["extra"]["origin"] == "platform"

    def test_the_release_reason_is_never_echoed_to_the_owner(self, world, notices):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid)
        _release(w.client, w.admin_a_headers, w.file.uuid, reason="internal-only detail")
        released = [c for c in notices.call_args_list if c.args[1] == "file_takedown_released"]
        assert len(released) == 1 and "internal-only detail" not in str(released[0])


# --------------------------------------------------------------------------- #
# The routes themselves                                                        #
# --------------------------------------------------------------------------- #
def _walk(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _walk(dep)


def _org_takedown_routes():
    from app.main import app

    return [
        r
        for r in app.routes
        if getattr(r, "path", "").startswith(BASE) and getattr(r, "dependant", None) is not None
    ]


class TestRouteTable:
    def test_the_walk_finds_exactly_the_three_routes(self):
        """An empty walk would pass every assertion below, so pin what it must find."""
        found = {(m, r.path) for r in _org_takedown_routes() for m in r.methods}
        assert found == {
            ("GET", f"{BASE}/quarantined"),
            ("POST", f"{BASE}/{{file_uuid}}/quarantine"),
            ("POST", f"{BASE}/{{file_uuid}}/release"),
        }

    def test_every_route_requires_an_org_admin_and_the_capability_without_bypass(self):
        from app.api.deps_context import require_org_admin

        routes = _org_takedown_routes()
        assert len(routes) == 3
        for route in routes:
            calls = list(_walk(route.dependant))
            assert require_org_admin in calls, route.path
            capability = [
                c for c in calls if getattr(c, "__qualname__", "").startswith("require_capability.")
            ]
            assert len(capability) == 1, route.path
            bound = dict(
                zip(
                    capability[0].__code__.co_freevars,
                    (cell.cell_contents for cell in capability[0].__closure__),
                    strict=True,
                )
            )
            assert bound == {"key": "org.takedown", "platform_admin_bypass": False}, route.path


class TestRateLimit:
    def test_organization_actions_are_limited_per_user(self, world, rate_limiting_enabled):
        from app.core.constants import ORG_TAKEDOWN_RATE_LIMIT

        w = world
        budget = int(ORG_TAKEDOWN_RATE_LIMIT.split("/")[0])
        for _ in range(budget):
            assert _quarantine(w.client, w.admin_a_headers, uuid_pkg.uuid4()).status_code == 404
        assert _quarantine(w.client, w.admin_a_headers, uuid_pkg.uuid4()).status_code == 429
        # Another administrator's bucket is untouched.
        assert _quarantine(w.client, w.admin_b_headers, uuid_pkg.uuid4()).status_code == 404


class TestPlatformRoutesStayPlatformOnly:
    @pytest.mark.parametrize("method", ["get", "post"])
    def test_a_support_grant_is_refused_on_the_platform_takedown_routes(self, world, method):
        """The operator path needs no grant and accepts none (seam v8). The grant here is a
        well-formed header for a real, active grant, so the refusal is the route's."""
        w = world
        grant = w.active_grant(level="write")
        headers = {**w.admin_headers, GRANT_HEADER: grant}
        if method == "get":
            r = w.client.get(f"{PLATFORM}/quarantined", headers=headers)
        else:
            r = w.client.post(
                f"{PLATFORM}/{w.file.uuid}/quarantine",
                headers=headers,
                json={"reason": "x", "operator_action_reason": "operator action (test)"},
            )
        assert r.status_code == 403 and _code(r) == "support_grant_action_not_permitted"
        assert _state(w.db, w.file)[0] is False

    def test_the_platform_queue_marks_who_holds_each_file(self, world):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid)
        _platform_quarantine(w.client, w.admin_headers, w.file2.uuid)
        rows = {
            f["uuid"]: f["takedown_origin"]
            for f in w.client.get(f"{PLATFORM}/quarantined", headers=w.admin_headers).json()[
                "files"
            ]
        }
        assert rows == {str(w.file.uuid): "organization", str(w.file2.uuid): "platform"}


class TestNoticeRecords:
    """Every takedown carries a record, including one an organization placed (issue #1254)."""

    @staticmethod
    def _links(w):
        from app.models.takedown import TakedownNoticeFile

        w.db.expire_all()
        return (
            w.db.query(TakedownNoticeFile)
            .filter(TakedownNoticeFile.file_uuid == w.file.uuid)
            .order_by(TakedownNoticeFile.id)
            .all()
        )

    def test_an_organization_takedown_is_recorded_with_the_organizations_authority(self, world):
        w = world
        assert (
            _quarantine(w.client, w.admin_a_headers, w.file.uuid, "the org's reason").status_code
            == 200
        )

        (link,) = self._links(w)
        assert link.status == "open" and link.owner_reason == "the org's reason"
        assert link.organization_id == w.org.id
        assert link.notice.notice_type == "operator_action"
        assert link.notice.recorded_by_authority == "organization"
        assert link.notice.basis == "the org's reason"
        assert link.notice.requester_reference is None

    def test_releasing_it_closes_the_record_and_keeps_the_reason_the_owner_saw(self, world):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid, "the org's reason")

        r = _release(w.client, w.admin_a_headers, w.file.uuid, reason="mistake")

        assert r.status_code == 200
        (link,) = self._links(w)
        assert (link.status, link.outcome_note) == ("released", "mistake")
        assert link.owner_reason == "the org's reason" and link.outcome_at is not None

    def test_an_operator_escalation_adds_a_platform_record_beside_the_organizations(self, world):
        w = world
        _quarantine(w.client, w.admin_a_headers, w.file.uuid, "the org's reason")
        assert _platform_quarantine(w.client, w.admin_headers, w.file.uuid).status_code == 200

        first, second = self._links(w)
        assert (first.notice.recorded_by_authority, second.notice.recorded_by_authority) == (
            "organization",
            "platform",
        )
        assert _release(w.client, w.admin_a_headers, w.file.uuid).status_code == 403
        assert [lk.status for lk in self._links(w)] == ["open", "open"], "a refusal writes nothing"

    def test_a_refused_organization_takedown_leaves_no_record(self, world):
        w = world
        _platform_quarantine(w.client, w.admin_headers, w.file.uuid)
        before = len(self._links(w))

        refused = _quarantine(w.client, w.admin_a_headers, w.file.uuid, "overwrite")

        assert refused.status_code == 409
        assert len(self._links(w)) == before
