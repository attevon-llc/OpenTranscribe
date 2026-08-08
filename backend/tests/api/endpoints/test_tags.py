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


# ---------------------------------------------------------------------------
# Rename / merge / delete
#
# The service-layer behavior is covered in tests/unit/test_tag_operations.py;
# these assert only the wiring — UUID path params, the confirmation round trip,
# and the impact shape that reaches the client.
# ---------------------------------------------------------------------------


def _create_tag(client, headers, name: str) -> dict:
    response = client.post("/api/tags", headers=headers, json={"name": name})
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def test_rename_tag_endpoint_applies_a_plain_rename(client, user_token_headers, db_session):
    tag = _create_tag(client, user_token_headers, f"before-{_uuid.uuid4().hex[:8]}")
    new_name = f"after-{_uuid.uuid4().hex[:8]}"

    response = client.patch(
        f"/api/tags/{tag['uuid']}", headers=user_token_headers, json={"name": new_name}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["merged"] is False
    assert body["requires_confirmation"] is False
    assert body["tag"]["name"] == new_name
    assert body["tag"]["uuid"] == tag["uuid"]


def test_rename_onto_existing_tag_asks_for_confirmation_then_merges(
    client, user_token_headers, db_session
):
    """R8: the collision comes back as an impact preview, not a silent merge."""
    suffix = _uuid.uuid4().hex[:8]
    target = _create_tag(client, user_token_headers, f"Interview-{suffix}")
    tag = _create_tag(client, user_token_headers, f"interviewing-{suffix}")

    held = client.patch(
        f"/api/tags/{tag['uuid']}", headers=user_token_headers, json={"name": target["name"]}
    )
    assert held.status_code == status.HTTP_200_OK, held.text
    assert held.json()["requires_confirmation"] is True
    assert held.json()["merged"] is False
    assert "accessible_file_count" in held.json()["impact"]
    assert "total_file_count" in held.json()["impact"]

    confirmed = client.patch(
        f"/api/tags/{tag['uuid']}",
        headers=user_token_headers,
        json={"name": target["name"], "confirm_merge": True},
    )
    assert confirmed.status_code == status.HTTP_200_OK, confirmed.text
    assert confirmed.json()["merged"] is True
    assert confirmed.json()["tag"]["uuid"] == target["uuid"]
    assert tag["uuid"] in confirmed.json()["deleted_uuids"]


def test_merge_endpoint_folds_sources_into_the_path_tag(client, user_token_headers, db_session):
    suffix = _uuid.uuid4().hex[:8]
    survivor = _create_tag(client, user_token_headers, f"survivor-{suffix}")
    first = _create_tag(client, user_token_headers, f"dupe-a-{suffix}")
    second = _create_tag(client, user_token_headers, f"dupe-b-{suffix}")

    response = client.post(
        f"/api/tags/{survivor['uuid']}/merge",
        headers=user_token_headers,
        json={"source_uuids": [first["uuid"], second["uuid"]]},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["merged"] is True
    assert sorted(body["deleted_uuids"]) == sorted([first["uuid"], second["uuid"]])


def test_merge_endpoint_404s_on_an_unknown_tag(client, user_token_headers, db_session):
    survivor = _create_tag(client, user_token_headers, f"survivor-{_uuid.uuid4().hex[:8]}")

    response = client.post(
        f"/api/tags/{survivor['uuid']}/merge",
        headers=user_token_headers,
        json={"source_uuids": [str(_uuid.uuid4())]},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_delete_endpoint_removes_the_tags_and_reports_impact(
    client, user_token_headers, db_session
):
    suffix = _uuid.uuid4().hex[:8]
    first = _create_tag(client, user_token_headers, f"gone-a-{suffix}")
    second = _create_tag(client, user_token_headers, f"gone-b-{suffix}")

    response = client.request(
        "DELETE",
        "/api/tags",
        headers=user_token_headers,
        params={"tag_uuids": [first["uuid"], second["uuid"]]},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert sorted(body["deleted_uuids"]) == sorted([first["uuid"], second["uuid"]])
    assert body["impact"]["total_file_count"] == 0

    listed = client.get("/api/tags", headers=user_token_headers)
    names = {tag["name"] for tag in listed.json()}
    assert first["name"] not in names
    assert second["name"] not in names


def test_impact_endpoint_reports_accessible_and_global_counts(
    client, user_token_headers, db_session
):
    """The preview cannot understate the operation — both counts are surfaced."""
    tag = _create_tag(client, user_token_headers, f"impact-{_uuid.uuid4().hex[:8]}")

    response = client.get(
        "/api/tags/impact", headers=user_token_headers, params={"tag_uuids": [tag["uuid"]]}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["accessible_file_count"] == 0
    assert body["total_file_count"] == 0
    assert body["tags"][0]["uuid"] == tag["uuid"]
    assert body["tags"][0]["name"] == tag["name"]


def test_tag_mutation_endpoints_require_authentication(client, db_session):
    tag_uuid = str(_uuid.uuid4())
    assert client.patch(f"/api/tags/{tag_uuid}", json={"name": "x"}).status_code == 401
    assert (
        client.post(f"/api/tags/{tag_uuid}/merge", json={"source_uuids": [tag_uuid]}).status_code
        == 401
    )
    assert client.get("/api/tags/impact", params={"tag_uuids": [tag_uuid]}).status_code == 401
