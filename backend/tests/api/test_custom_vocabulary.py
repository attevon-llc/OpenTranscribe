"""Characterization tests for the custom-vocabulary endpoints.

Covers ``app/api/endpoints/custom_vocabulary.py``: domain listing, list/filter,
create (validation + duplicate 409), update (validation + system-term read-only
gate), delete (single + all + system-term gate), bulk import (validation,
dedup, limits) and export. The owner-vs-other 403 authorization details are
already pinned in ``test_ownership_contracts.py``; this suite locks the
remaining behavior.

System ("user_id IS NULL") rows are created directly on the savepoint-isolated
``db_session`` so the read-only gates can be exercised without touching shared
seed data — every row rolls back at teardown.
"""

from __future__ import annotations

from fastapi import status

_BASE = "/api/custom-vocabulary"


def _vocab_model():
    from app.models.custom_vocabulary import CustomVocabulary

    return CustomVocabulary


def _make_system_term(db_session, *, term="systemword", domain="general"):
    """Create a system (shared, user_id=None) vocabulary row on the test session."""
    model = _vocab_model()
    row = model(user_id=None, term=term, domain=domain, category=None, is_active=True)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _make_user_term(db_session, owner, *, term="userword", domain="general"):
    model = _vocab_model()
    row = model(user_id=owner.id, term=term, domain=domain, category=None, is_active=True)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Auth + domains
# ---------------------------------------------------------------------------


def test_domains_unauthorized(client):
    assert client.get(f"{_BASE}/domains").status_code == status.HTTP_401_UNAUTHORIZED


def test_list_domains(client, user_token_headers):
    resp = client.get(f"{_BASE}/domains", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["domains"] == [
        "medical",
        "legal",
        "corporate",
        "government",
        "technical",
        "general",
    ]


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_empty_shape(client, user_token_headers):
    resp = client.get(_BASE, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    for key in ("terms", "system_terms", "total", "total_system"):
        assert key in body


def test_list_returns_own_and_system(client, user_token_headers, normal_user, db_session):
    _make_user_term(db_session, normal_user, term="mine")
    _make_system_term(db_session, term="shared")
    body = client.get(_BASE, headers=user_token_headers).json()
    own = {t["term"] for t in body["terms"]}
    sys_terms = {t["term"] for t in body["system_terms"]}
    assert "mine" in own
    assert "shared" in sys_terms
    # system rows are flagged is_system
    assert all(t["is_system"] for t in body["system_terms"])


def test_list_other_user_term_invisible(client, other_user_auth_headers, normal_user, db_session):
    _make_user_term(db_session, normal_user, term="secret")
    body = client.get(_BASE, headers=other_user_auth_headers).json()
    assert "secret" not in {t["term"] for t in body["terms"]}


def test_list_invalid_domain_filter_is_400(client, user_token_headers):
    resp = client.get(_BASE, headers=user_token_headers, params={"domain": "astrophysics"})
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid domain 'astrophysics'" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_term(client, user_token_headers):
    resp = client.post(
        _BASE, json={"term": "ELISA", "domain": "medical"}, headers=user_token_headers
    )
    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    assert data["term"] == "ELISA"
    assert data["domain"] == "medical"
    assert data["is_system"] is False


def test_create_defaults_to_general_domain(client, user_token_headers):
    resp = client.post(_BASE, json={"term": "widget"}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.json()["domain"] == "general"


def test_create_empty_term_is_400(client, user_token_headers):
    resp = client.post(_BASE, json={"term": "   "}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "'term' is required and must not be empty"


def test_create_oversized_term_is_400(client, user_token_headers):
    resp = client.post(_BASE, json={"term": "x" * 201}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "must not exceed 200 characters" in resp.json()["detail"]


def test_create_oversized_category_is_400(client, user_token_headers):
    resp = client.post(
        _BASE, json={"term": "abc", "category": "c" * 101}, headers=user_token_headers
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "'category' must not exceed 100 characters" in resp.json()["detail"]


def test_create_invalid_domain_is_400(client, user_token_headers):
    resp = client.post(_BASE, json={"term": "abc", "domain": "bogus"}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid domain 'bogus'" in resp.json()["detail"]


def test_create_duplicate_is_409(client, user_token_headers):
    client.post(_BASE, json={"term": "dupe", "domain": "legal"}, headers=user_token_headers)
    resp = client.post(_BASE, json={"term": "dupe", "domain": "legal"}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_409_CONFLICT
    assert "already exists in domain 'legal'" in resp.json()["detail"]


def test_create_unauthorized(client):
    assert client.post(_BASE, json={"term": "x"}).status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_own_term(client, user_token_headers, normal_user, db_session):
    term = _make_user_term(db_session, normal_user, term="before")
    resp = client.put(f"{_BASE}/{term.id}", json={"term": "after"}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["term"] == "after"


def test_update_nonexistent_is_404(client, user_token_headers):
    resp = client.put(f"{_BASE}/99999999", json={"term": "x"}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Vocabulary term not found"


def test_update_system_term_is_403(client, user_token_headers, db_session):
    """System terms are read-only — even for the term's would-be 'owner' (None)."""
    term = _make_system_term(db_session, term="sysro")
    resp = client.put(f"{_BASE}/{term.id}", json={"term": "hijack"}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "System vocabulary terms are read-only"


def test_update_empty_term_is_400(client, user_token_headers, normal_user, db_session):
    term = _make_user_term(db_session, normal_user, term="keep")
    resp = client.put(f"{_BASE}/{term.id}", json={"term": "  "}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "'term' must not be empty"


def test_update_invalid_domain_is_400(client, user_token_headers, normal_user, db_session):
    term = _make_user_term(db_session, normal_user, term="dom")
    resp = client.put(f"{_BASE}/{term.id}", json={"domain": "bogus"}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid domain 'bogus'" in resp.json()["detail"]


def test_update_toggle_active(client, user_token_headers, normal_user, db_session):
    term = _make_user_term(db_session, normal_user, term="toggle")
    resp = client.put(f"{_BASE}/{term.id}", json={"is_active": False}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["is_active"] is False


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_own_term_204(client, user_token_headers, normal_user, db_session):
    term = _make_user_term(db_session, normal_user, term="goner")
    resp = client.delete(f"{_BASE}/{term.id}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_204_NO_CONTENT


def test_delete_nonexistent_is_404(client, user_token_headers):
    resp = client.delete(f"{_BASE}/99999999", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Vocabulary term not found"


def test_delete_system_term_is_403(client, user_token_headers, db_session):
    term = _make_system_term(db_session, term="sysdel")
    resp = client.delete(f"{_BASE}/{term.id}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "System vocabulary terms cannot be deleted"


def test_delete_all_user_terms(client, user_token_headers, normal_user, db_session):
    _make_user_term(db_session, normal_user, term="a")
    _make_user_term(db_session, normal_user, term="b", domain="legal")
    resp = client.delete(f"{_BASE}/all", headers=user_token_headers)
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    body = client.get(_BASE, headers=user_token_headers).json()
    assert body["total"] == 0


def test_delete_all_by_domain(client, user_token_headers, normal_user, db_session):
    _make_user_term(db_session, normal_user, term="med", domain="medical")
    _make_user_term(db_session, normal_user, term="leg", domain="legal")
    resp = client.delete(f"{_BASE}/all", headers=user_token_headers, params={"domain": "medical"})
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    remaining = {t["term"] for t in client.get(_BASE, headers=user_token_headers).json()["terms"]}
    assert "leg" in remaining
    assert "med" not in remaining


def test_delete_all_invalid_domain_is_400(client, user_token_headers):
    resp = client.delete(f"{_BASE}/all", headers=user_token_headers, params={"domain": "bogus"})
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Bulk import
# ---------------------------------------------------------------------------


def test_bulk_import_creates_and_dedups(client, user_token_headers):
    body = {
        "terms": [
            {"term": "alpha", "domain": "general"},
            {"term": "beta", "domain": "general"},
            {"term": "alpha", "domain": "general"},  # in-batch duplicate → skipped
        ]
    }
    resp = client.post(f"{_BASE}/bulk", json=body, headers=user_token_headers)
    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    assert data["created"] == 2
    assert data["skipped"] == 1


def test_bulk_import_skips_existing(client, user_token_headers, normal_user, db_session):
    _make_user_term(db_session, normal_user, term="exists", domain="general")
    resp = client.post(
        f"{_BASE}/bulk",
        json={"terms": [{"term": "exists", "domain": "general"}]},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.json()["created"] == 0
    assert resp.json()["skipped"] == 1


def test_bulk_import_validation_errors_collected(client, user_token_headers):
    resp = client.post(
        f"{_BASE}/bulk",
        json={"terms": [{"term": ""}, {"term": "ok", "domain": "nope"}]},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    assert data["created"] == 0
    assert data["skipped"] == 2
    assert len(data["errors"]) == 2


def test_bulk_import_terms_not_a_list_is_400(client, user_token_headers):
    resp = client.post(f"{_BASE}/bulk", json={"terms": "nope"}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "'terms' must be a list"


def test_bulk_import_over_limit_is_400(client, user_token_headers):
    resp = client.post(
        f"{_BASE}/bulk",
        json={"terms": [{"term": f"t{i}"} for i in range(1001)]},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "limited to 1000 terms" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_returns_attachment(client, user_token_headers, normal_user, db_session):
    _make_user_term(db_session, normal_user, term="exportme")
    resp = client.get(f"{_BASE}/export", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert "attachment" in resp.headers.get("content-disposition", "")
    data = resp.json()
    assert any(t["term"] == "exportme" for t in data["terms"])
    assert data["exported_by"] == normal_user.email


def test_export_invalid_domain_is_400(client, user_token_headers):
    resp = client.get(f"{_BASE}/export", headers=user_token_headers, params={"domain": "bogus"})
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
