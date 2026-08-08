"""Tag endpoint tests.

The creation endpoints resolve names through ``app/services/tag_service.py``:
normalized-exact matching, a 50-character clamp, and rejection of names that are
empty once normalized. A near match is deliberately NOT resolved here — the name
came from a person, so a fuzzy hit may only be offered as a suggestion.
"""

import uuid as _uuid

import pytest
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
    assert client.post("/api/tags/accept", json={"tag_uuids": [tag_uuid]}).status_code == 401
    assert client.post("/api/tags/reject", json={"tag_uuids": [tag_uuid]}).status_code == 401
    assert (
        client.get(
            "/api/tags/review-impact", params={"tag_uuids": [tag_uuid], "action": "reject"}
        ).status_code
        == 401
    )


# ---------------------------------------------------------------------------
# Accept / reject (R7, R15)
#
# Service behavior lives in tests/unit/test_tag_review.py; these assert the
# wiring — UUID path/query params, the per-tag outcome list, and the removed /
# retained split reaching the client.
# ---------------------------------------------------------------------------


def _auto_tag(db_session, name: str):
    """Create a tag the auto-labeler owns — the API has no path that writes one."""
    from app.core.constants import TAG_SOURCE_AUTO_AI
    from app.models.media import Tag
    from app.services.tag_service import normalize_tag_name

    tag = Tag(name=name, source=TAG_SOURCE_AUTO_AI, normalized_name=normalize_tag_name(name))
    db_session.add(tag)
    db_session.commit()
    return tag


def test_accept_endpoint_marks_the_tag_accepted(client, user_token_headers, db_session):
    tag = _auto_tag(db_session, f"accept-me-{_uuid.uuid4().hex[:8]}")

    response = client.post(
        "/api/tags/accept", headers=user_token_headers, json={"tag_uuids": [str(tag.uuid)]}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["tags"][0]["uuid"] == str(tag.uuid)
    assert body["tags"][0]["outcome"] == "accepted"
    db_session.refresh(tag)
    assert tag.source == "ai_accepted"


def test_accept_endpoint_reports_ineligible_tags_without_failing(
    client, user_token_headers, db_session
):
    """A mixed multi-select must not 4xx the whole call."""
    suffix = _uuid.uuid4().hex[:8]
    auto = _auto_tag(db_session, f"mix-auto-{suffix}")
    manual = _create_tag(client, user_token_headers, f"mix-manual-{suffix}")

    response = client.post(
        "/api/tags/accept",
        headers=user_token_headers,
        json={"tag_uuids": [str(auto.uuid), manual["uuid"]]},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    outcomes = {entry["uuid"]: entry["outcome"] for entry in response.json()["tags"]}
    assert outcomes[str(auto.uuid)] == "accepted"
    assert outcomes[manual["uuid"]] == "not_applicable"


def _file_for(db_session, owner):
    from app.models.media import MediaFile

    file_uuid = str(_uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="tag_review_api.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
    )
    db_session.add(media_file)
    db_session.commit()
    return media_file


def test_review_impact_endpoint_reports_the_reject_split(
    client, user_token_headers, db_session, normal_user
):
    from app.core.constants import TAG_SOURCE_AUTO_AI
    from app.core.constants import TAG_SOURCE_MANUAL
    from app.models.media import FileTag

    tag = _auto_tag(db_session, f"split-{_uuid.uuid4().hex[:8]}")
    auto_file = _file_for(db_session, normal_user)
    manual_file = _file_for(db_session, normal_user)
    db_session.add(FileTag(media_file_id=auto_file.id, tag_id=tag.id, source=TAG_SOURCE_AUTO_AI))
    db_session.add(FileTag(media_file_id=manual_file.id, tag_id=tag.id, source=TAG_SOURCE_MANUAL))
    db_session.commit()

    response = client.get(
        "/api/tags/review-impact",
        headers=user_token_headers,
        params={"tag_uuids": [str(tag.uuid)], "action": "reject"},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["applied"] is False
    assert body["removed_association_count"] == 1
    assert body["retained_association_count"] == 1
    assert body["tags"][0]["tag_removed"] is False


def test_reject_endpoint_removes_the_tag_when_nothing_human_remains(
    client, user_token_headers, db_session
):
    from app.models.media import Tag

    tag = _auto_tag(db_session, f"reject-me-{_uuid.uuid4().hex[:8]}")

    response = client.post(
        "/api/tags/reject", headers=user_token_headers, json={"tag_uuids": [str(tag.uuid)]}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["tags"][0]["outcome"] == "rejected"
    assert body["deleted_uuids"] == [str(tag.uuid)]
    assert db_session.query(Tag).filter(Tag.name == tag.name).first() is None


def test_review_endpoints_404_on_an_unknown_tag(client, user_token_headers, db_session):
    unknown = str(_uuid.uuid4())

    for path in ("/api/tags/accept", "/api/tags/reject"):
        response = client.post(path, headers=user_token_headers, json={"tag_uuids": [unknown]})
        assert response.status_code == status.HTTP_404_NOT_FOUND, path


# ---------------------------------------------------------------------------
# Collisions and listing filters (R3, R11, R14)
#
# Clustering, suggestion ranking, and survivor preselection are covered in
# tests/unit/test_tag_collisions.py; these assert the wiring — the query
# parameters, the cluster shape on the wire, and that a broken read errors
# instead of rendering an empty page.
# ---------------------------------------------------------------------------


def _raw_tag(db_session, name: str, *, normalized=..., source="manual"):
    """Insert a tag row directly — the API has no path that writes a collision."""
    from app.models.media import Tag
    from app.services.tag_service import normalize_tag_name

    stored = normalize_tag_name(name) if normalized is ... else normalized
    tag = Tag(name=name, source=source, normalized_name=stored)
    db_session.add(tag)
    db_session.commit()
    return tag


def test_unused_listing_agrees_with_the_usage_count(
    client, user_token_headers, db_session, normal_user, other_user
):
    """A tag whose only files are inaccessible reads as 0 *and* as unused.

    ``/tags`` scoped ``usage_count`` to the caller's accessible files while
    ``/tags/unused`` counted usage globally, so this tag reported ``0`` in one
    place and was missing from the other.
    """
    from app.models.media import FileTag

    tag = _create_tag(client, user_token_headers, f"foreign-{_uuid.uuid4().hex[:8]}")
    hidden_file = _file_for(db_session, other_user)
    tag_row = _raw_tag_row(db_session, tag["uuid"])
    db_session.add(FileTag(media_file_id=hidden_file.id, tag_id=tag_row.id, source="manual"))
    db_session.commit()

    listed = client.get("/api/tags", headers=user_token_headers)
    assert listed.status_code == status.HTTP_200_OK, listed.text
    counts = {entry["uuid"]: entry["usage_count"] for entry in listed.json()}
    assert counts[tag["uuid"]] == 0

    unused = client.get("/api/tags/unused", headers=user_token_headers)
    assert unused.status_code == status.HTTP_200_OK, unused.text
    assert tag["uuid"] in {entry["uuid"] for entry in unused.json()}

    filtered = client.get("/api/tags", headers=user_token_headers, params={"unused": True})
    assert filtered.status_code == status.HTTP_200_OK, filtered.text
    assert tag["uuid"] in {entry["uuid"] for entry in filtered.json()}


def _raw_tag_row(db_session, tag_uuid: str):
    from app.models.media import Tag

    return db_session.query(Tag).filter(Tag.uuid == tag_uuid).one()


def test_unused_filter_drops_a_tag_the_caller_uses(
    client, user_token_headers, db_session, normal_user
):
    """The filter narrows — a tag on the caller's own file must not come back."""
    from app.models.media import FileTag

    tag = _create_tag(client, user_token_headers, f"inuse-{_uuid.uuid4().hex[:8]}")
    media_file = _file_for(db_session, normal_user)
    tag_row = _raw_tag_row(db_session, tag["uuid"])
    db_session.add(FileTag(media_file_id=media_file.id, tag_id=tag_row.id, source="manual"))
    db_session.commit()

    response = client.get("/api/tags", headers=user_token_headers, params={"unused": True})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert tag["uuid"] not in {entry["uuid"] for entry in response.json()}


def test_awaiting_review_filter_returns_only_auto_labeled_tags(
    client, user_token_headers, db_session
):
    suffix = _uuid.uuid4().hex[:8]
    auto = _auto_tag(db_session, f"await-auto-{suffix}")
    manual = _create_tag(client, user_token_headers, f"await-manual-{suffix}")
    accepted = _raw_tag(db_session, f"await-accepted-{suffix}", source="ai_accepted")
    legacy = _raw_tag(db_session, f"await-legacy-{suffix}", source=None)

    response = client.get("/api/tags", headers=user_token_headers, params={"awaiting_review": True})

    assert response.status_code == status.HTTP_200_OK, response.text
    returned = {entry["uuid"] for entry in response.json()}
    assert str(auto.uuid) in returned
    assert manual["uuid"] not in returned
    assert str(accepted.uuid) not in returned
    assert str(legacy.uuid) not in returned


def test_collisions_endpoint_ships_clusters_survivor_and_suggestions(
    client, user_token_headers, db_session
):
    """Grouping, ranking, and preselection arrive pre-computed."""
    suffix = _uuid.uuid4().hex[:8]
    first = _raw_tag(db_session, f"q3-earnings-{suffix}")
    second = _raw_tag(db_session, f"Q3 Earnings {suffix}")
    near = _raw_tag(db_session, f"q4-earnings-{suffix}")
    normalized = f"q3 earnings {suffix}"

    response = client.get("/api/tags/collisions", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    clusters = [c for c in response.json() if c["normalized_name"] == normalized]
    assert len(clusters) == 1
    cluster = clusters[0]
    assert {member["uuid"] for member in cluster["members"]} == {
        str(first.uuid),
        str(second.uuid),
    }
    assert cluster["suggested_survivor_uuid"] in {str(first.uuid), str(second.uuid)}
    assert sum(member["suggested_survivor"] for member in cluster["members"]) == 1
    assert str(near.uuid) in {s["uuid"] for s in cluster["suggestions"]}

    repeat = client.get("/api/tags/collisions", headers=user_token_headers)
    assert repeat.json() == response.json()


def test_colliding_filter_narrows_the_tag_list(client, user_token_headers, db_session):
    suffix = _uuid.uuid4().hex[:8]
    first = _raw_tag(db_session, f"clash-{suffix}")
    second = _raw_tag(db_session, f"CLASH-{suffix}")
    alone = _create_tag(client, user_token_headers, f"solo-{suffix}")

    response = client.get("/api/tags", headers=user_token_headers, params={"colliding": True})

    assert response.status_code == status.HTTP_200_OK, response.text
    returned = {entry["uuid"] for entry in response.json()}
    assert {str(first.uuid), str(second.uuid)} <= returned
    assert alone["uuid"] not in returned


def _break_permission_scoping(monkeypatch):
    """Make the accessible-files gate raise, the way a query bug would."""
    from app.services.permission_service import PermissionService

    def _boom(*args, **kwargs):
        raise RuntimeError("accessible-file subquery is broken")

    monkeypatch.setattr(PermissionService, "get_accessible_file_ids_subquery", staticmethod(_boom))


def test_list_tags_surfaces_a_query_error(client, user_token_headers, db_session, monkeypatch):
    """A broken read must error, not render an empty tag page.

    The request carries a filter so it bypasses the read-through cache — a
    cached hit would prove nothing about the query underneath.
    """
    _break_permission_scoping(monkeypatch)

    with pytest.raises(RuntimeError):
        client.get("/api/tags", headers=user_token_headers, params={"unused": True})


def test_list_unused_tags_surfaces_a_query_error(
    client, user_token_headers, db_session, monkeypatch
):
    """Same for the unused listing — an empty list is not an error report."""
    _break_permission_scoping(monkeypatch)

    with pytest.raises(RuntimeError):
        client.get("/api/tags/unused", headers=user_token_headers)


def test_collision_endpoints_require_authentication(client, db_session):
    assert client.get("/api/tags/collisions").status_code == 401
