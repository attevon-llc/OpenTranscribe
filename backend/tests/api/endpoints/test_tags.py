"""Tag endpoint tests.

The creation endpoints resolve names through ``app/services/tag_service.py``:
normalized-exact matching, a 50-character clamp, and rejection of names that are
empty once normalized. A near match is deliberately NOT resolved here — the name
came from a person, so a fuzzy hit may only be offered as a suggestion.
"""

import uuid as _uuid

from fastapi import status


def test_list_tags(client, user_token_headers):
    """Test listing all tags"""
    response = client.get("/api/tags", headers=user_token_headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_list_tags_unauthorized(client):
    """Test that unauthorized users cannot list tags"""
    response = client.get("/api/tags")
    assert response.status_code == 401  # Unauthorized


def test_create_tag(client, user_token_headers, db_session):
    """Test creating a new tag"""
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    tag_data = {"name": f"test_tag_{unique_id}"}
    response = client.post("/api/tags", headers=user_token_headers, json=tag_data)
    assert response.status_code == 200, f"Create tag failed: {response.json()}"
    tag = response.json()
    assert "uuid" in tag or "id" in tag
    assert tag["name"] == tag_data["name"]


def test_create_duplicate_tag(client, user_token_headers, db_session):
    """Test creating a duplicate tag returns existing tag"""
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    tag_data = {"name": f"duplicate_tag_{unique_id}"}

    # First create a tag
    first_response = client.post("/api/tags", headers=user_token_headers, json=tag_data)
    assert first_response.status_code == 200

    # Try to create the same tag again - should return existing tag
    response = client.post("/api/tags", headers=user_token_headers, json=tag_data)
    assert response.status_code == 200


def test_create_tag_case_insensitive_returns_existing(client, user_token_headers, db_session):
    """A name differing only by case resolves to the existing tag."""
    name = f"Interview-{_uuid.uuid4().hex[:8]}"

    first = client.post("/api/tags", headers=user_token_headers, json={"name": name})
    assert first.status_code == status.HTTP_200_OK

    second = client.post("/api/tags", headers=user_token_headers, json={"name": name.upper()})
    assert second.status_code == status.HTTP_200_OK
    assert second.json()["uuid"] == first.json()["uuid"]
    assert second.json()["name"] == name


def test_create_tag_separator_variants_return_existing(client, user_token_headers, db_session):
    """Hyphen / underscore / whitespace variants collapse onto the same tag."""
    base = f"quarterly{_uuid.uuid4().hex[:8]}"

    first = client.post("/api/tags", headers=user_token_headers, json={"name": f"{base}-notes"})
    assert first.status_code == status.HTTP_200_OK

    for variant in (f"{base}_notes", f"{base} notes", f"  {base}   notes "):
        response = client.post("/api/tags", headers=user_token_headers, json={"name": variant})
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["uuid"] == first.json()["uuid"]


def test_create_tag_near_match_is_not_auto_resolved(client, user_token_headers, db_session):
    """A near match is a distinct tag on the manual path — fuzzy only ever suggests."""
    suffix = _uuid.uuid4().hex[:8]

    q3 = client.post(
        "/api/tags", headers=user_token_headers, json={"name": f"q3-earnings-{suffix}"}
    )
    assert q3.status_code == status.HTTP_200_OK

    q4 = client.post(
        "/api/tags", headers=user_token_headers, json={"name": f"q4-earnings-{suffix}"}
    )
    assert q4.status_code == status.HTTP_200_OK
    assert q4.json()["uuid"] != q3.json()["uuid"]
    assert q4.json()["name"] == f"q4-earnings-{suffix}"


def test_create_tag_truncates_long_name(client, user_token_headers, db_session):
    """An over-long name is clamped to the stored width instead of erroring."""
    long_name = f"retro-{_uuid.uuid4().hex[:8]}-" + ("x" * 80)

    response = client.post("/api/tags", headers=user_token_headers, json={"name": long_name})

    assert response.status_code == status.HTTP_200_OK, response.json()
    assert response.json()["name"] == long_name[:50]


def test_create_tag_rejects_blank_name(client, user_token_headers, db_session):
    """A name that is empty after normalization is rejected, not stored blank."""
    for blank in ("", "   ", "-", "__"):
        response = client.post("/api/tags", headers=user_token_headers, json={"name": blank})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, blank
