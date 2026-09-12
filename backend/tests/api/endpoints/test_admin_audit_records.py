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

from typing import Any

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


def _of_action(events: list[dict], event_type: AuditEventType, action: str) -> list[dict]:
    """Select by event type AND ``details.action``.

    One erasure now emits THREE ``ADMIN_USER_DELETE`` records — the ledger's
    ``gdpr_erasure_requested`` and ``gdpr_erasure_recorded`` bracketing the service's
    own ``gdpr_erasure`` (issue #442). Selecting on the event type alone would either
    fail on the count or, worse, assert against whichever record happened to be first.
    """
    return [e for e in _of_type(events, event_type) if e.get("details", {}).get("action") == action]


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

    erasures = _of_action(events, AuditEventType.ADMIN_USER_DELETE, "gdpr_erasure")
    assert len(erasures) == 1, f"expected exactly one erasure record, got {events}"

    # Who did it — top-level and typed, per the #443/#828 convention: `user_id`/
    # `username` are ALWAYS the actor.
    assert erasures[0]["user_id"] == actor_id
    assert erasures[0]["username"] == actor_email
    # Who it was done to — also top-level, never only in `details`.
    assert erasures[0]["target_user_id"] == target_id
    assert erasures[0]["target_username"] == target_email
    # The webhook marker is a NARRATIVE detail distinguishing self-service from
    # staff, not the attribution — that lives in the top-level fields above.
    assert erasures[0]["details"]["actor_email"] != "data-subject-webhook"

    # The ledger's own two records bracket this one and share its event type, so
    # `user_id`/`target_user_id` must mean the same thing in all three or the audit
    # stream cannot be queried by actor OR by subject (issue #443/#828). A first
    # version of the ledger put the SUBJECT in `user_id` (matching the erasure
    # record here, which used to put the target there too) — but that agreement
    # was an accident of this one sequence: `erase_org_member_data`'s own record
    # has always put the ACTOR in `user_id`, so the ledger's old convention
    # disagreed with THAT sequence instead. Both queries now work everywhere.
    ledger = [
        e
        for e in _of_type(events, AuditEventType.ADMIN_USER_DELETE)
        if e["details"]["action"] in {"gdpr_erasure_requested", "gdpr_erasure_recorded"}
    ]
    assert len(ledger) == 2, f"expected the ledger to bracket the erasure, got {events}"
    for record in ledger:
        assert record["user_id"] == actor_id, (
            "A ledger record does not attribute `user_id` to the acting admin, so "
            "'which erasures did admin Y run' would miss this record."
        )
        assert record["target_user_id"] == target_id, (
            "A ledger record does not name the data subject in `target_user_id`, "
            "so 'everything done TO user X' would miss this record."
        )


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

    erasures = _of_action(events, AuditEventType.ADMIN_USER_DELETE, "gdpr_erasure")
    assert len(erasures) == 1
    assert erasures[0]["details"]["actor_email"] == "data-subject-webhook"
    # No human actor: `user_id` is None, never backfilled with the subject or a
    # placeholder (issue #443/#828).
    assert erasures[0]["user_id"] is None


def test_the_full_erasure_sequence_agrees_on_actor_and_subject_everywhere(
    client, super_admin_token_headers, super_admin_user, normal_user, monkeypatch
):
    """The defect was CROSS-RECORD inconsistency, so a per-record test cannot catch
    a regression here — only a test that queries the WHOLE three-record sequence
    by each field, and checks it gets the same three records back either way, can.

    Before the #828 decision, ``erase_user``'s own record happened to key
    ``user_id`` on the subject (matching the ledger's two records, by an accident
    of THIS sequence's history) — so a query by SUBJECT returned all three, but a
    query by ACTOR returned only the two ledger records with the actor buried in
    ``details``, never the erasure record itself, which carried no actor at the
    top level at all. Query by actor and by subject must return the identical
    three-record set now.
    """
    from app.services import gdpr_erasure_service

    events = _collect(monkeypatch, gdpr_erasure_service)

    target_id = int(normal_user.id)
    actor_id = int(super_admin_user.id)

    response = client.post(
        f"/api/admin/gdpr/erase-user/{normal_user.uuid}", headers=super_admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    sequence = _of_type(events, AuditEventType.ADMIN_USER_DELETE)
    # Non-vacuity check: a query that matched nothing would satisfy the equality
    # assertions below by both being empty.
    assert len(sequence) == 3, f"expected the full erasure+ledger sequence, got {events}"

    by_subject = [e for e in sequence if e.get("target_user_id") == target_id]
    by_actor = [e for e in sequence if e.get("user_id") == actor_id]

    assert len(by_subject) == 3, (
        f"querying by subject returned a PARTIAL set ({len(by_subject)} of 3) — "
        f"at least one record in the sequence does not name the subject: {sequence}"
    )
    assert len(by_actor) == 3, (
        f"querying by actor returned a PARTIAL set ({len(by_actor)} of 3) — at "
        f"least one record in the sequence does not name the actor: {sequence}"
    )
    assert by_subject == by_actor == sequence


class _FilteringFakeAuditOS:
    """An in-memory OpenSearch stand-in that actually FILTERS, not just records.

    ``_FakeAuditOS`` elsewhere in this repo (``tests/test_org_admin_gdpr.py``) only
    captures the query body — enough to assert on the request shape, but it would
    let a filter that never reaches OpenSearch pass vacuously, since a query that
    is silently ignored returns everything and "everything" can equal "the right
    three records" if nothing else is in the index. This fake actually applies the
    ``must`` clauses ``query_audit_logs`` builds, so a ``target_user_id`` filter
    that FastAPI silently drops (unknown query param -> ignored, not a 422) shows
    up as too many rows coming back, not as a false green.

    Supports exactly the clause shapes this module emits: ``term`` and ``terms``.
    Anything else raises, deliberately — a shape this fake cannot evaluate must
    not be silently treated as "matches everything".
    """

    class _Indices:
        """Stub for ``client.indices`` — the real one is only asked whether the
        monthly index exists / to create it; this fake needs no actual index."""

        def exists(self, index):
            return True

        def create(self, index, body):
            pass

    def __init__(self):
        self.docs: list[dict] = []
        self.indices = self._Indices()

    def index(self, index, body):  # noqa: A002 - matches opensearch-py's kwarg name
        self.docs.append(dict(body))

    def search(self, index, body):  # noqa: A002
        query = body["query"]
        if query == {"match_all": {}}:
            hits = list(self.docs)
        else:
            clauses = query["bool"]["must"]
            hits = [d for d in self.docs if all(self._clause_matches(d, c) for c in clauses)]
        return {
            "hits": {
                "hits": [{"_source": h} for h in hits],
                "total": {"value": len(hits)},
            }
        }

    @staticmethod
    def _clause_matches(doc: dict[str, Any], clause: dict[str, Any]) -> bool:
        if "term" in clause:
            ((field, value),) = clause["term"].items()
            return bool(doc.get(field) == value)
        if "terms" in clause:
            ((field, values),) = clause["terms"].items()
            return bool(doc.get(field) in values)
        raise AssertionError(f"unsupported clause shape in test fake: {clause}")


def test_erasure_sequence_is_queryable_through_the_real_audit_log_endpoint(
    client,
    super_admin_token_headers,
    super_admin_user,
    normal_user,
    monkeypatch,
):
    """The read-path half of issue #443/#828's fix, through the REAL HTTP endpoint.

    ``test_the_full_erasure_sequence_agrees_on_actor_and_subject_everywhere`` above
    proves the three records are internally consistent — but it reads them from
    the captured ``audit_logger.log`` kwargs, never through ``query_audit_logs`` or
    ``GET /admin/audit-logs`` at all. Before this fix, ``target_user_id`` had no
    filter on either: the subject-correct records this session produced were
    unqueryable by subject through the one supported read path, which is WORSE
    than the pre-#828 state where ``user_id`` held the subject at some sites and
    at least THAT filter found something. A "show me everything done to user X"
    compliance query (GDPR Art. 15) must return this sequence.

    A decoy document — same event type, a DIFFERENT actor and a DIFFERENT
    subject, seeded directly into the fake index rather than produced by a real
    erasure — is what makes this test able to fail. Without it, an ignored
    ``target_user_id`` filter would still return exactly the erasure's own three
    records (nothing else is in this test's index), which would pass by
    accident. With the decoy present, a silently-ignored filter returns all
    FOUR records and the ``== 3`` assertions catch it.
    """
    from app.auth import audit as audit_module

    fake = _FilteringFakeAuditOS()
    monkeypatch.setattr(audit_module.audit_logger, "_opensearch_client", fake)
    monkeypatch.setattr(audit_module, "_build_audit_opensearch_client", lambda: fake)
    monkeypatch.setattr(audit_module.settings, "AUDIT_LOG_TO_OPENSEARCH", True)

    target_id = int(normal_user.id)
    actor_id = int(super_admin_user.id)
    decoy_actor_id = -998
    decoy_target_id = -999
    fake.docs.append(
        {
            "timestamp": "2020-01-01T00:00:00+00:00",
            "event_type": AuditEventType.ADMIN_USER_DELETE.value,
            "outcome": AuditOutcome.SUCCESS.value,
            "user_id": decoy_actor_id,
            "target_user_id": decoy_target_id,
            "details": {"action": "gdpr_erasure", "decoy": True},
        }
    )

    response = client.post(
        f"/api/admin/gdpr/erase-user/{normal_user.uuid}", headers=super_admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    # 3 real records + the 1 decoy planted above.
    assert len(fake.docs) == 4, f"expected 3 erasure records plus the decoy, got {fake.docs}"

    by_subject = client.get(
        "/api/admin/audit-logs",
        params={
            "target_user_id": target_id,
            "event_type": AuditEventType.ADMIN_USER_DELETE.value,
        },
        headers=super_admin_token_headers,
    )
    assert by_subject.status_code == status.HTTP_200_OK, by_subject.text
    subject_data = by_subject.json()
    # Non-vacuity: the equality below would also pass at 0 == 0.
    assert subject_data["total"] > 0, f"query by subject returned nothing at all: {subject_data}"
    assert subject_data["total"] == 3, (
        f"query by target_user_id did not return exactly the erasure's own three "
        f"records — either the filter is not reaching OpenSearch (returned "
        f"everything, including the decoy) or it is over-narrowing: {subject_data}"
    )
    assert len(subject_data["logs"]) == 3
    assert all(log["target_user_id"] == target_id for log in subject_data["logs"])
    assert not any(log["target_user_id"] == decoy_target_id for log in subject_data["logs"])

    by_actor = client.get(
        "/api/admin/audit-logs",
        params={"user_id": actor_id, "event_type": AuditEventType.ADMIN_USER_DELETE.value},
        headers=super_admin_token_headers,
    )
    assert by_actor.status_code == status.HTTP_200_OK, by_actor.text
    actor_data = by_actor.json()
    assert actor_data["total"] > 0, f"query by actor returned nothing at all: {actor_data}"
    assert actor_data["total"] == 3, (
        f"query by user_id did not return exactly the erasure's own three records: {actor_data}"
    )
    assert len(actor_data["logs"]) == 3
    assert all(log["user_id"] == actor_id for log in actor_data["logs"])
    assert not any(log["user_id"] == decoy_actor_id for log in actor_data["logs"])

    subject_actions = {log["details"]["action"] for log in subject_data["logs"]}
    actor_actions = {log["details"]["action"] for log in actor_data["logs"]}
    assert (
        subject_actions
        == actor_actions
        == {"gdpr_erasure", "gdpr_erasure_requested", "gdpr_erasure_recorded"}
    )


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
