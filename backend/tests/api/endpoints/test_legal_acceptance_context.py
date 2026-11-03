"""Acceptance context (user agent, locale, organization) and org-scope acceptance (v450)."""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.legal import registry
from app.models.legal_acceptance import LegalAcceptance
from app.models.organization import Organization

TERMS = "opentranscribe-terms-of-use"


@pytest.fixture(autouse=True)
def _default_provider():
    registry.set_document_provider(None)
    yield
    registry.set_document_provider(None)


def _body(key: str = TERMS, **over):
    doc = registry.get_document(key)
    assert doc is not None
    return {"key": doc.key, "version": doc.version, "sha256": doc.sha256, **over}


def _org(db, label: str) -> Organization:
    org = Organization(
        external_org_id=f"org_{label}_{uuid_pkg.uuid4().hex[:8]}", name=label, is_active=True
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _rows(db, **filters) -> list[LegalAcceptance]:
    db.expire_all()
    rows: list[LegalAcceptance] = db.query(LegalAcceptance).filter_by(**filters).all()
    return rows


def test_user_agent_is_stored_and_truncated(client, db_session, normal_user, user_token_headers):
    headers = {**user_token_headers, "User-Agent": "UA/" + "x" * 900}
    r = client.post("/api/legal/acknowledgments", json=_body(), headers=headers)
    assert r.status_code == 200, r.text
    (row,) = _rows(db_session, user_id=normal_user.id, document_key=TERMS)
    assert row.user_agent is not None and row.user_agent.startswith("UA/xxx")
    assert len(row.user_agent) == 512
    assert row.scope == "user" and row.organization_id is None


def test_locale_from_body_wins_over_header(client, db_session, normal_user, user_token_headers):
    headers = {**user_token_headers, "Accept-Language": "fr-FR,fr;q=0.9"}
    r = client.post("/api/legal/acknowledgments", json=_body(locale="de-AT"), headers=headers)
    assert r.status_code == 200, r.text
    assert _rows(db_session, user_id=normal_user.id, document_key=TERMS)[0].locale == "de-AT"


def test_locale_falls_back_to_accept_language(client, db_session, normal_user, user_token_headers):
    headers = {**user_token_headers, "Accept-Language": "pt-BR,pt;q=0.8"}
    r = client.post("/api/legal/acknowledgments", json=_body(), headers=headers)
    assert r.status_code == 200, r.text
    assert _rows(db_session, user_id=normal_user.id, document_key=TERMS)[0].locale == "pt-BR"


def test_an_invalid_locale_is_refused(client, user_token_headers):
    r = client.post(
        "/api/legal/acknowledgments", json=_body(locale="not a locale!"), headers=user_token_headers
    )
    assert r.status_code == 422


def test_a_malformed_accept_language_is_ignored(
    client, db_session, normal_user, user_token_headers
):
    headers = {**user_token_headers, "Accept-Language": "<script>"}
    r = client.post("/api/legal/acknowledgments", json=_body(), headers=headers)
    assert r.status_code == 200, r.text
    assert _rows(db_session, user_id=normal_user.id, document_key=TERMS)[0].locale is None


def test_personal_acceptance_in_an_org_records_the_context(
    client, db_session, normal_user, user_token_headers, org_context
):
    org = _org(db_session, "ctx")
    org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)
    r = client.post("/api/legal/acknowledgments", json=_body(), headers=user_token_headers)
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "user"
    (row,) = _rows(db_session, user_id=normal_user.id, document_key=TERMS)
    assert row.scope == "user"
    assert row.organization_id == org.id and row.organization_uuid == org.uuid


def test_org_admin_accepts_for_the_org_idempotently(
    client, db_session, normal_user, user_token_headers, org_context
):
    org = _org(db_session, "adm")
    org_context(org_id=org.id, org_role="org:admin", only_for=normal_user.id)
    body = _body(scope="organization")
    r1 = client.post("/api/legal/acknowledgments", json=body, headers=user_token_headers)
    r2 = client.post("/api/legal/acknowledgments", json=body, headers=user_token_headers)
    assert r1.status_code == 200 and r2.status_code == 200, r1.text
    assert r1.json() == r2.json()
    assert r1.json()["scope"] == "organization"
    assert r1.json()["organization_uuid"] == str(org.uuid)
    rows = _rows(db_session, organization_uuid=org.uuid, scope="organization")
    assert len(rows) == 1 and rows[0].user_id == normal_user.id

    docs = client.get("/api/legal/documents", headers=user_token_headers).json()
    terms = next(d for d in docs if d["key"] == TERMS)
    assert terms["organization_acknowledged"] is True
    assert terms["organization_acknowledged_at"] is not None
    assert terms["acknowledged"] is False  # an org acceptance is not a personal one


def test_org_member_cannot_accept_for_the_org(
    client, db_session, normal_user, user_token_headers, org_context
):
    org = _org(db_session, "mem")
    org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)
    r = client.post(
        "/api/legal/acknowledgments", json=_body(scope="organization"), headers=user_token_headers
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "legal_org_acceptance_forbidden"
    assert _rows(db_session, organization_uuid=org.uuid) == []


def test_org_scope_needs_an_org_context(client, user_token_headers):
    r = client.post(
        "/api/legal/acknowledgments", json=_body(scope="organization"), headers=user_token_headers
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "legal_org_acceptance_forbidden"


def test_operator_acknowledgment_cannot_be_org_scoped(
    client, db_session, super_admin_user, super_admin_token_headers, org_context
):
    org = _org(db_session, "op")
    org_context(org_id=org.id, org_role="org:admin", only_for=super_admin_user.id)
    r = client.post(
        "/api/legal/acknowledgments",
        json=_body(registry.OPERATOR_ACKNOWLEDGMENT_KEY, scope="organization"),
        headers=super_admin_token_headers,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "legal_scope_not_supported"


def test_an_admin_of_two_orgs_accepts_the_same_text_for_both(
    client, db_session, normal_user, user_token_headers, org_context
):
    """Fails on the v441 plain UNIQUE(user_id, document_key, sha256)."""
    x, y = _org(db_session, "x"), _org(db_session, "y")
    body = _body(scope="organization")
    org_context(org_id=x.id, org_role="org:admin", only_for=normal_user.id)
    assert (
        client.post("/api/legal/acknowledgments", json=body, headers=user_token_headers).status_code
        == 200
    )
    org_context(org_id=y.id, org_role="org:admin", only_for=normal_user.id)
    r = client.post("/api/legal/acknowledgments", json=body, headers=user_token_headers)
    assert r.status_code == 200, r.text
    got = {row.organization_uuid for row in _rows(db_session, user_id=normal_user.id)}
    assert got == {x.uuid, y.uuid}


def test_org_admin_lists_only_their_orgs_acceptances(
    client,
    db_session,
    normal_user,
    user_token_headers,
    admin_user,
    admin_token_headers,
    org_context,
):
    org = _org(db_session, "lst")
    org_context(org_id=org.id, org_role="org:admin", only_for=normal_user.id)
    client.post(
        "/api/legal/acknowledgments", json=_body(scope="organization"), headers=user_token_headers
    )
    client.post("/api/legal/acknowledgments", json=_body(), headers=admin_token_headers)

    r = client.get("/api/legal/acknowledgments?organization=current", headers=user_token_headers)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1 and rows[0]["scope"] == "organization"
    assert rows[0]["organization_uuid"] == str(org.uuid)

    # A plain member of no org cannot use the org list, nor the instance list.
    org_context(org_id=None, org_role=None)
    r = client.get("/api/legal/acknowledgments?organization=current", headers=user_token_headers)
    assert r.status_code == 403
    assert client.get("/api/legal/acknowledgments", headers=user_token_headers).status_code == 403
    all_rows = client.get("/api/legal/acknowledgments", headers=admin_token_headers).json()
    assert any(row["user_email"] == admin_user.email for row in all_rows)
