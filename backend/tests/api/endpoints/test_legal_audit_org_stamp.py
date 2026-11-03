"""The acceptance audit event is stamped with the organization, so the org audit log shows it."""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest

from app.auth.audit import AuditEventType
from app.legal import registry
from app.models.legal_acceptance import LegalAcceptance
from app.models.organization import Organization

TERMS = "opentranscribe-terms-of-use"


@pytest.fixture(autouse=True)
def _default_provider():
    registry.set_document_provider(None)
    yield
    registry.set_document_provider(None)


@pytest.fixture
def audit_calls(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("app.api.endpoints.legal.audit_logger.log", lambda **kw: calls.append(kw))
    return calls


def _body(**over):
    doc = registry.get_document(TERMS)
    assert doc is not None
    return {"key": doc.key, "version": doc.version, "sha256": doc.sha256, **over}


def _org(db) -> Organization:
    org = Organization(external_org_id=f"org_aud_{uuid_pkg.uuid4().hex[:8]}", name="aud")
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _legal_events(calls):
    return [c for c in calls if c["event_type"] == AuditEventType.LEGAL_ACKNOWLEDGED]


def test_org_acceptance_event_carries_the_org(
    client, db_session, normal_user, user_token_headers, org_context, audit_calls
):
    org = _org(db_session)
    org_context(org_id=org.id, org_role="org:admin", only_for=normal_user.id)
    r = client.post(
        "/api/legal/acknowledgments",
        json=_body(scope="organization", locale="es"),
        headers=user_token_headers,
    )
    assert r.status_code == 200, r.text
    (event,) = _legal_events(audit_calls)
    row = db_session.query(LegalAcceptance).filter_by(organization_uuid=org.uuid).one()
    assert event["organization_id"] == org.id
    assert event["details"]["scope"] == "organization"
    assert event["details"]["acceptance_uuid"] == str(row.uuid)
    assert event["details"]["locale"] == "es"
    assert all(isinstance(v, str) for v in event["details"].values())

    # Re-accepting the same text records nothing new.
    again = client.post(
        "/api/legal/acknowledgments", json=_body(scope="organization"), headers=user_token_headers
    )
    assert again.status_code == 200
    assert len(_legal_events(audit_calls)) == 1


def test_personal_acceptance_in_an_org_is_stamped_too(
    client, db_session, normal_user, user_token_headers, org_context, audit_calls
):
    org = _org(db_session)
    org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)
    assert (
        client.post(
            "/api/legal/acknowledgments", json=_body(), headers=user_token_headers
        ).status_code
        == 200
    )
    (event,) = _legal_events(audit_calls)
    assert event["organization_id"] == org.id
    assert event["details"]["scope"] == "user"
    assert "locale" not in event["details"]


def test_personal_workspace_acceptance_has_no_org(client, user_token_headers, audit_calls):
    assert (
        client.post(
            "/api/legal/acknowledgments", json=_body(), headers=user_token_headers
        ).status_code
        == 200
    )
    (event,) = _legal_events(audit_calls)
    assert event["organization_id"] is None
