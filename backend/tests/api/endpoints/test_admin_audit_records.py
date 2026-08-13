"""Endpoint-level audit-record tests for the admin account-management surface.

These are FedRAMP AU-2/AU-3/AU-12 and GDPR Art. 30 assertions, and they are
deliberately **per ENDPOINT**. ``unit/test_audit_event_emitters.py`` asserts each
``AuditEventType`` member is emitted *somewhere*, which is per MEMBER — that is
exactly why an unaudited admin delete-user endpoint went unnoticed while
``ADMIN_USER_DELETE`` had an emitter elsewhere. Every member exercised below
(``ADMIN_USER_DELETE``, ``AUTH_TOKEN_REVOKE``, ``AUTH_LOGOUT_ALL``,
``AUTH_MFA_DISABLE``, ``AUTH_ACCOUNT_DISABLED``, ``AUTH_ACCOUNT_UNLOCK``) already
had an emitter *somewhere*, so that test reported this whole surface as covered.

Each test pins the ACTOR and the TARGET, not merely that an event fired: asserting
"an event fired" would still pass with the two swapped, which is literally the
defect ``test_gdpr_erasure_names_the_acting_super_admin`` covers.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome


def _collect(monkeypatch, module) -> list[dict]:
    """Collect the kwargs of every ``audit_logger.log`` call made by *module*."""
    events: list[dict] = []
    monkeypatch.setattr(module.audit_logger, "log", lambda **kw: events.append(kw))
    return events


def _of_type(events: list[dict], event_type: AuditEventType) -> list[dict]:
    return [e for e in events if e["event_type"] is event_type]


# ---------------------------------------------------------------------------
# POST /admin/gdpr/erase-user/{uuid} — the actor
# ---------------------------------------------------------------------------
def test_gdpr_erasure_names_the_acting_super_admin(
    client, super_admin_token_headers, super_admin_user, normal_user, monkeypatch
):
    """A staff-initiated erasure must not be attributed to the data subject.

    ``admin_erase_user`` is the ONLY caller of ``erase_user`` that is a person,
    and it passed neither ``actor_user_id`` nor ``actor_email`` — so every record
    it produced read ``actor_email: "data-subject-webhook"``, the service's
    default meaning "the user deleted their own IdP account". 100% of platform
    erasures were therefore recorded as self-service deletions that never
    happened. The org-admin twin ``erase_org_member_data`` has always passed them.

    The target assertions are the other half: an actor-only fix would satisfy a
    test that just looked for a non-webhook email while losing who was erased —
    and this record outlives the account, so it is the only place that survives.
    """
    from app.services import gdpr_erasure_service

    events = _collect(monkeypatch, gdpr_erasure_service)

    # Snapshot both sides before the request: the target row is destroyed by it.
    target_id, target_email = normal_user.id, str(normal_user.email)
    actor_id, actor_email = super_admin_user.id, str(super_admin_user.email)

    response = client.post(
        f"/api/admin/gdpr/erase-user/{normal_user.uuid}", headers=super_admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    erasures = _of_type(events, AuditEventType.ADMIN_USER_DELETE)
    assert len(erasures) == 1, f"expected exactly one erasure record, got {events}"

    details = erasures[0]["details"]
    assert details["action"] == "gdpr_erasure"
    # Who did it.
    assert details["actor_user_id"] == actor_id
    assert details["actor_email"] == actor_email
    assert details["actor_email"] != "data-subject-webhook"
    # Who it was done to.
    assert details["target_user_id"] == target_id
    assert details["target_email"] == target_email
    assert erasures[0]["user_id"] == target_id


def test_gdpr_erasure_still_reports_the_webhook_when_there_is_no_actor(db_session, normal_user):
    """The control for the test above: the fallback is not simply dead.

    ``erase_user`` is also the cloud ``user.deleted`` webhook's entry point, where
    there genuinely is no operator. If the fix had been to make ``actor_email``
    required, or to stamp the default with something else, this direction would
    break silently — and a self-service deletion would then be indistinguishable
    from a staff one, which is the same defect pointing the other way.
    """
    from unittest.mock import patch

    from app.services import gdpr_erasure_service

    events: list[dict] = []
    with patch.object(gdpr_erasure_service.audit_logger, "log", lambda **kw: events.append(kw)):
        gdpr_erasure_service.erase_user(db_session, int(normal_user.id))

    erasures = _of_type(events, AuditEventType.ADMIN_USER_DELETE)
    assert len(erasures) == 1
    assert erasures[0]["details"]["actor_email"] == "data-subject-webhook"
    assert erasures[0]["details"]["actor_user_id"] is None


# ---------------------------------------------------------------------------
# DELETE /admin/users/{uuid}/sessions — mass revocation
# ---------------------------------------------------------------------------
def _issue_a_session(db_session, user) -> str:
    """Give *user* one live refresh-token row; return its JTI."""
    from app.auth.token_service import token_service

    _token, row = token_service.create_refresh_token(
        db=db_session,
        user_id=int(user.id),
        user_uuid=str(user.uuid),
        role=str(user.role),
    )
    return str(row.jti)


def test_admin_session_termination_revokes_through_token_service(
    client, admin_token_headers, normal_user, db_session
):
    """The termination must reach the Redis blacklist and the revocation epoch.

    This endpoint used to set ``RefreshToken.revoked_at`` inline, bypassing
    ``token_service`` entirely — so it wrote no blacklist entry and, more
    seriously, no **per-user revocation epoch**. Access tokens are stateless and
    have no row to revoke; the epoch is the only mechanism that reaches them. An
    admin force-logging-out a compromised account therefore left it authenticated
    for the remaining access-token lifetime, which is a correctness bug before it
    is an audit one.

    Asserting on the store rather than on a mock is the point: a call to
    ``revoke_all_sessions`` that did not stamp the epoch would pass a
    ``assert_called_once`` and fail here.
    """
    from app.auth.token_service import REVOKED_TOKEN_PREFIX
    from app.auth.token_service import USER_REVOCATION_EPOCH_PREFIX
    from app.auth.token_service import token_service

    jti = _issue_a_session(db_session, normal_user)
    user_uuid = str(normal_user.uuid)
    assert token_service.store.get(f"{REVOKED_TOKEN_PREFIX}{jti}") is None
    token_service.store.delete(f"{USER_REVOCATION_EPOCH_PREFIX}{user_uuid}")

    response = client.delete(f"/api/admin/users/{user_uuid}/sessions", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["sessions_terminated"] >= 1

    assert token_service.store.get(f"{REVOKED_TOKEN_PREFIX}{jti}") is not None, (
        "the refresh token's JTI never reached the revocation list — the endpoint "
        "is mutating revoked_at directly again"
    )
    assert token_service.store.get(f"{USER_REVOCATION_EPOCH_PREFIX}{user_uuid}") is not None, (
        "no revocation epoch was stamped, so already-issued access tokens survive the forced logout"
    )


def test_mass_revocation_writes_an_audit_record(
    client, admin_token_headers, normal_user, db_session, monkeypatch
):
    """``revoke_all_user_tokens_in_transaction`` audits the bulk revocation.

    ``revoke_token`` calls itself "the single choke point for every revocation
    path" and audits accordingly — but it is the SINGLE-token path, and the bulk
    method does not route through it. So the path used by admin password reset,
    role change, lock, MFA reset, SCIM deactivation and directory sync produced no
    ``AUTH_TOKEN_REVOKE`` record at all.

    The record names the TARGET, because this layer has no request and therefore
    no actor; the endpoint's own ``AUTH_LOGOUT_ALL`` (asserted below) names the
    admin, and the two correlate by ``request_id``.
    """
    from app.auth import token_service as token_service_module

    _issue_a_session(db_session, normal_user)
    target_id = normal_user.id
    events = _collect(monkeypatch, token_service_module)

    response = client.delete(
        f"/api/admin/users/{normal_user.uuid}/sessions", headers=admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    bulk = [
        e
        for e in _of_type(events, AuditEventType.AUTH_TOKEN_REVOKE)
        if e["details"].get("scope") == "all_user_tokens"
    ]
    assert len(bulk) == 1, f"expected exactly one bulk revocation record, got {events}"
    record = bulk[0]
    assert record["outcome"] is AuditOutcome.SUCCESS
    assert record["user_id"] == target_id
    assert record["details"]["sessions_revoked"] >= 1
    assert record["details"]["user_uuid"] == str(normal_user.uuid)


def test_admin_session_termination_audits_the_acting_admin(
    client, admin_token_headers, admin_user, normal_user, db_session, monkeypatch
):
    """The endpoint's own record names WHO forced the logout, and from where."""
    from app.api.endpoints import admin as admin_module

    _issue_a_session(db_session, normal_user)
    events = _collect(monkeypatch, admin_module)

    response = client.delete(
        f"/api/admin/users/{normal_user.uuid}/sessions", headers=admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    logouts = _of_type(events, AuditEventType.AUTH_LOGOUT_ALL)
    assert len(logouts) == 1, f"expected exactly one AUTH_LOGOUT_ALL, got {events}"
    record = logouts[0]
    assert record["user_id"] == admin_user.id
    assert record["username"] == str(admin_user.email)
    assert record["details"]["target_user"] == str(normal_user.uuid)
    assert record["details"]["sessions_terminated"] >= 1


# ---------------------------------------------------------------------------
# POST /admin/users/{uuid}/mfa/reset — the outcome
# ---------------------------------------------------------------------------
def _enrol_mfa(db_session, user) -> None:
    from app.models.user_mfa import UserMFA

    db_session.add(UserMFA(user_id=int(user.id), totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True))
    db_session.commit()


def test_mfa_reset_of_an_enrolled_account_is_recorded_as_a_disable(
    client, super_admin_token_headers, super_admin_user, normal_user, db_session, monkeypatch
):
    """The positive control: when a factor really was in force, SUCCESS is right.

    It also covers the reason nobody noticed the outcome bug. The enrolled branch
    nulled ``totp_secret``, which ``v200`` made NOT NULL, so this request was a
    **500** for every account that actually had MFA — while an account with no MFA
    took the no-op path and answered 200 with a SUCCESS disable record. The only
    working case was the one recorded wrongly. Hence the state assertion below: a
    record without a real state change is what this endpoint has always produced.
    """
    from app.api.endpoints import admin as admin_module
    from app.models.user_mfa import UserMFA

    _enrol_mfa(db_session, normal_user)
    events = _collect(monkeypatch, admin_module)

    response = client.post(
        f"/api/admin/users/{normal_user.uuid}/mfa/reset", headers=super_admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    # The state really changed — not just the record.
    db_session.expire_all()
    assert db_session.query(UserMFA).filter(UserMFA.user_id == normal_user.id).first() is None

    disables = _of_type(events, AuditEventType.AUTH_MFA_DISABLE)
    assert len(disables) == 1, f"expected exactly one AUTH_MFA_DISABLE, got {events}"
    record = disables[0]
    assert record["outcome"] is AuditOutcome.SUCCESS
    assert record["error_code"] is None
    assert record["details"]["mfa_was_enabled"] is True
    assert record["details"]["target_user"] == str(normal_user.uuid)
    assert record["user_id"] == super_admin_user.id


def test_mfa_reset_of_an_unenrolled_account_is_not_recorded_as_a_disable(
    client, super_admin_token_headers, normal_user, monkeypatch
):
    """A reset that disabled nothing must not read as "MFA was removed".

    The ``audit_logger.log`` call sat OUTSIDE the ``if mfa_settings:`` block and
    always reported ``AUTH_MFA_DISABLE`` / ``SUCCESS``, so a reset against an
    account with no second factor recorded a disable that did nothing — an event
    a reviewer would read as this account having had MFA stripped on that date.

    The attempt is still recorded (a run of resets against accounts with no MFA is
    itself worth seeing); only the outcome changes. Dropping the record entirely
    would trade one wrong answer for a blind spot.
    """
    from app.api.endpoints import admin as admin_module

    events = _collect(monkeypatch, admin_module)

    response = client.post(
        f"/api/admin/users/{normal_user.uuid}/mfa/reset", headers=super_admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    disables = _of_type(events, AuditEventType.AUTH_MFA_DISABLE)
    assert len(disables) == 1, f"expected exactly one AUTH_MFA_DISABLE, got {events}"
    record = disables[0]
    assert record["outcome"] is AuditOutcome.FAILURE
    assert record["error_code"] == "MFA_NOT_ENROLLED"
    assert record["details"]["mfa_was_enabled"] is False


# ---------------------------------------------------------------------------
# source_ip / user_agent on the four endpoints that could not obtain them
# ---------------------------------------------------------------------------
#: (route builder, headers fixture name, expected event type). Each of these four
#: handlers declared no ``Request`` parameter, so ``source_ip`` and ``user_agent``
#: were *unobtainable* rather than merely omitted — every record they wrote
#: carried ``null`` for both, in a log whose purpose is to say where an action
#: came from (AU-3 requires the source of the event, and the CEF/SIEM stream maps
#: ``source_ip`` to ``src``).
_CLIENT_INFO_ROUTES = [
    ("POST", "/api/admin/users/{uuid}/unlock", "admin", AuditEventType.AUTH_ACCOUNT_UNLOCK),
    ("POST", "/api/admin/users/{uuid}/lock", "admin", AuditEventType.AUTH_ACCOUNT_DISABLED),
    ("DELETE", "/api/admin/users/{uuid}/sessions", "admin", AuditEventType.AUTH_LOGOUT_ALL),
    (
        "POST",
        "/api/admin/users/{uuid}/mfa/reset",
        "super_admin",
        AuditEventType.AUTH_MFA_DISABLE,
    ),
]


@pytest.mark.parametrize(("method", "template", "tier", "event_type"), _CLIENT_INFO_ROUTES)
def test_admin_account_routes_record_where_the_request_came_from(
    client,
    admin_token_headers,
    super_admin_token_headers,
    normal_user,
    monkeypatch,
    method,
    template,
    tier,
    event_type,
):
    """Every one of these four must record ``source_ip`` and ``user_agent``.

    A test asserting only that the event fired passes with both fields ``null``,
    which is the state all four were in.
    """
    from app.api.endpoints import admin as admin_module

    headers = admin_token_headers if tier == "admin" else super_admin_token_headers
    headers = {**headers, "User-Agent": "ot-audit-probe/1.0"}
    events = _collect(monkeypatch, admin_module)

    response = client.request(method, template.format(uuid=normal_user.uuid), headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text

    matching = _of_type(events, event_type)
    assert len(matching) == 1, f"expected exactly one {event_type}, got {events}"
    record = matching[0]
    assert record["source_ip"], f"{template} recorded no source_ip"
    assert record["user_agent"] == "ot-audit-probe/1.0"
