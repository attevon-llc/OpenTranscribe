"""``/api/legal`` documents + acknowledgments, the provider seam, and the wizard status flags."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.legal import registry
from app.models.legal_acceptance import LegalAcceptance

KEY = registry.OPERATOR_ACKNOWLEDGMENT_KEY


@pytest.fixture(autouse=True)
def _default_provider():
    registry.set_document_provider(None)
    yield
    registry.set_document_provider(None)


def _current():
    doc = registry.get_document(KEY)
    assert doc is not None
    return doc


def _body(doc=None, **over):
    doc = doc or _current()
    return {"key": doc.key, "version": doc.version, "sha256": doc.sha256, **over}


def test_list_documents_exposes_four_drafts(client, user_token_headers):
    r = client.get("/api/legal/documents", headers=user_token_headers)
    assert r.status_code == 200
    rows = {d["key"]: d for d in r.json()}
    assert KEY in rows and len(rows) == 4
    assert all(d["status"] == "draft" for d in rows.values())
    assert rows[KEY]["requires_acknowledgment"] is True
    assert rows[KEY]["acknowledged"] is False
    assert rows[KEY]["sha256"] == _current().sha256


def test_documents_require_authentication(client):
    assert client.get("/api/legal/documents").status_code == 401


def test_get_document_returns_exact_served_text(client, user_token_headers):
    r = client.get(f"/api/legal/documents/{KEY}", headers=user_token_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["markdown"] == _current().markdown
    assert body["sha256"] == _current().sha256
    assert client.get("/api/legal/documents/nope", headers=user_token_headers).status_code == 404


def test_served_markdown_drops_front_matter_but_the_hash_covers_the_whole_file(
    client, user_token_headers
):
    """The reader sees the document, not its metadata; the fingerprint still covers every byte."""
    import hashlib

    doc = _current()
    body = client.get(f"/api/legal/documents/{KEY}", headers=user_token_headers).json()

    assert doc.content.decode("utf-8").startswith("---")
    assert not body["markdown"].lstrip().startswith("---")
    assert f"key: {KEY}" not in body["markdown"]
    assert body["sha256"] == hashlib.sha256(doc.content).hexdigest()


def test_super_admin_accepts_and_it_is_idempotent(
    client, db_session, super_admin_user, super_admin_token_headers
):
    r1 = client.post("/api/legal/acknowledgments", json=_body(), headers=super_admin_token_headers)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/legal/acknowledgments", json=_body(), headers=super_admin_token_headers)
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    rows = db_session.query(LegalAcceptance).filter_by(user_id=super_admin_user.id).all()
    assert len(rows) == 1 and rows[0].sha256 == _current().sha256

    listed = client.get("/api/legal/documents", headers=super_admin_token_headers).json()
    mine = next(d for d in listed if d["key"] == KEY)
    assert mine["acknowledged"] is True and mine["acknowledged_at"] is not None


def test_wrong_sha_or_version_is_409_and_stores_nothing(
    client, db_session, super_admin_user, super_admin_token_headers
):
    for over in ({"sha256": "0" * 64}, {"version": "999"}):
        r = client.post(
            "/api/legal/acknowledgments", json=_body(**over), headers=super_admin_token_headers
        )
        assert r.status_code == 409
    assert db_session.query(LegalAcceptance).filter_by(user_id=super_admin_user.id).count() == 0


def test_non_super_admin_cannot_acknowledge_operator_key(
    client, db_session, admin_token_headers, user_token_headers
):
    for headers in (admin_token_headers, user_token_headers):
        r = client.post("/api/legal/acknowledgments", json=_body(), headers=headers)
        assert r.status_code == 403
    assert db_session.query(LegalAcceptance).count() == 0


def test_unknown_key_is_404(client, super_admin_token_headers):
    r = client.post(
        "/api/legal/acknowledgments",
        json={"key": "nope", "version": "1", "sha256": "0" * 64},
        headers=super_admin_token_headers,
    )
    assert r.status_code == 404


def test_admin_lists_acknowledgments_user_cannot(
    client, super_admin_user, super_admin_token_headers, admin_token_headers, user_token_headers
):
    client.post("/api/legal/acknowledgments", json=_body(), headers=super_admin_token_headers)
    r = client.get("/api/legal/acknowledgments", headers=admin_token_headers)
    assert r.status_code == 200
    rec = next(x for x in r.json() if x["user_email"] == super_admin_user.email)
    assert rec["key"] == KEY and rec["sha256"] == _current().sha256 and rec["accepted_at"]
    assert client.get("/api/legal/acknowledgments", headers=user_token_headers).status_code == 403


class _OtherProvider:
    def __init__(self):
        self.operator = _current()  # captured before this provider is installed
        self.doc = registry.LegalDocument(
            key="other-terms",
            title="Other",
            version="1",
            status="final",
            audience="x",
            content=b"other text",
        )

    def list_documents(self):
        return [self.doc, self.operator]

    def get_document(self, key):
        return {self.doc.key: self.doc, KEY: self.operator}.get(key)


def test_provider_seam_swap_turns_off_requires_acknowledgment(client, user_token_headers):
    registry.set_document_provider(_OtherProvider())
    rows = {
        d["key"]: d for d in client.get("/api/legal/documents", headers=user_token_headers).json()
    }
    assert set(rows) == {"other-terms", KEY}
    assert rows[KEY]["requires_acknowledgment"] is False
    got = client.get("/api/legal/documents/other-terms", headers=user_token_headers).json()
    assert got["markdown"] == "other text"


def test_wizard_status_reports_legal_state(client, super_admin_token_headers):
    url = "/api/admin/first-run-wizard/status"
    s = client.get(url, headers=super_admin_token_headers).json()
    assert s["legal_required"] is True and s["legal_acknowledged"] is False
    client.post("/api/legal/acknowledgments", json=_body(), headers=super_admin_token_headers)
    s = client.get(url, headers=super_admin_token_headers).json()
    assert s["legal_required"] is True and s["legal_acknowledged"] is True


def test_wizard_status_not_required_with_other_provider(client, super_admin_token_headers):
    registry.set_document_provider(_OtherProvider())
    s = client.get("/api/admin/first-run-wizard/status", headers=super_admin_token_headers).json()
    assert s["legal_required"] is False and s["legal_acknowledged"] is True


def test_deleting_the_user_removes_their_acceptances(
    client, db_session, super_admin_user, super_admin_token_headers
):
    client.post("/api/legal/acknowledgments", json=_body(), headers=super_admin_token_headers)
    uid = super_admin_user.id
    assert db_session.query(LegalAcceptance).filter_by(user_id=uid).count() == 1
    db_session.execute(text('DELETE FROM "user" WHERE id = :i'), {"i": uid})
    db_session.expire_all()
    assert db_session.query(LegalAcceptance).filter_by(user_id=uid).count() == 0
