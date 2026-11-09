"""The ``/api/tags`` routes: create, list, unused, collisions, cleanup, operations, file listing.

Status codes and details are asserted exactly. Requests run as the stock users of
``conftest.py`` in their personal workspace; tags are named under a fresh stem so rows of
other tests and seeded system tags never decide an assertion.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fixtures.tag_recorders import record_hook
from fixtures.tag_recorders import record_reindex
from fixtures.tag_recorders import share_file
from fixtures.tag_world import attach
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word

from app.core.config import settings
from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.formatting_service import FormattingService
from app.services.permission_service import PermissionService
from tests.state_resets import reset_tenancy_mode_cache

_ANY_UUID = "00000000-0000-7000-8000-000000000001"


def _exists(db, tag_id: int) -> bool:
    db.expire_all()
    return db.query(Tag).filter(Tag.id == tag_id).first() is not None


def _listed(client, headers, stem: str, **params) -> dict[str, dict]:
    response = client.get("/api/tags", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return {row["name"]: row for row in response.json() if row["name"].lower().startswith(stem)}


@pytest.fixture
def single_tenant(monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()
    yield
    reset_tenancy_mode_cache()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/api/tags", {}),
        ("post", "/api/tags", {"json": {"name": "x"}}),
        ("get", "/api/tags/collisions", {}),
        ("get", f"/api/tags/impact?tag_uuids={_ANY_UUID}", {}),
        ("patch", f"/api/tags/{_ANY_UUID}", {"json": {"name": "x"}}),
        (
            "post",
            f"/api/tags/{_ANY_UUID}/merge",
            {"json": {"source_uuids": [_ANY_UUID]}},
        ),
    ],
)
def test_every_tag_route_needs_a_signed_in_user(client, method, path, kwargs):
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/tags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "-", "__"])
def test_create_with_a_blank_name_is_a_422(client, user_token_headers, blank):
    response = client.post("/api/tags", headers=user_token_headers, json={"name": blank})
    assert response.status_code == 422
    assert response.json() == {"detail": "Tag name is required"}


def test_create_resolves_variants_onto_one_row_and_a_near_match_onto_another(
    client, user_token_headers, monkeypatch, normal_user
):
    hook = record_hook(monkeypatch, "app.api.endpoints.tags.crud")
    stem = word("cr")

    def create(name: str) -> dict:
        response = client.post("/api/tags", headers=user_token_headers, json={"name": name})
        assert response.status_code == 200, response.text
        body: dict = response.json()
        return body

    first = create(f"{stem}-q3-earnings")
    again = [create(n)["uuid"] for n in (f"{stem}-q3-earnings", f"{stem.upper()}_Q3 EARNINGS")]
    near = create(f"{stem}-q4-earnings")

    assert set(first) == {"uuid", "name", "source", "ownership", "can_write"}
    assert (first["name"], first["source"], first["ownership"]) == (
        f"{stem}-q3-earnings",
        "manual",
        "mine",
    )
    assert again == [first["uuid"], first["uuid"]]
    assert near["uuid"] != first["uuid"]
    assert hook.calls[0] == {"files": [], "user_id": normal_user.id, "system_scope": False}


def test_create_clamps_an_over_long_name(client, user_token_headers, db_session, normal_user):
    long_name = word("Long") + "-" + "y" * 80
    response = client.post("/api/tags", headers=user_token_headers, json={"name": long_name})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == long_name[:50]
    row = db_session.query(Tag).filter(Tag.uuid == uuid.UUID(response.json()["uuid"])).one()
    assert (row.name, row.user_id, row.organization_id) == (long_name[:50], normal_user.id, None)


def test_two_users_creating_one_name_in_personal_scope_get_two_rows(
    client, user_token_headers, other_user_auth_headers
):
    name = word("Twin")
    mine = client.post("/api/tags", headers=user_token_headers, json={"name": name})
    theirs = client.post("/api/tags", headers=other_user_auth_headers, json={"name": name})
    assert (mine.status_code, theirs.status_code) == (200, 200)
    assert mine.json()["uuid"] != theirs.json()["uuid"]


# ---------------------------------------------------------------------------
# GET /api/tags and /unused
# ---------------------------------------------------------------------------


def test_an_unknown_list_scope_is_a_422(client, user_token_headers):
    response = client.get("/api/tags", headers=user_token_headers, params={"scope": "ours"})
    assert response.status_code == 422


def test_each_scope_returns_only_rows_with_that_ownership(
    client, db_session, normal_user, other_user, user_token_headers
):
    stem = word("scope")
    mine = make_tag(db_session, f"{stem}-mine", user_id=normal_user.id)
    system = make_tag(db_session, f"{stem}-system", user_id=None)
    theirs = make_tag(db_session, f"{stem}-theirs", user_id=other_user.id)
    shared = make_file(db_session, other_user)
    attach(db_session, shared, theirs)
    share_file(db_session, other_user, normal_user, shared)

    expected = {"mine": mine, "system": system, "shared_with_me": theirs}
    for scope, tag in expected.items():
        rows = _listed(client, user_token_headers, stem, scope=scope)
        assert {(r["uuid"], r["ownership"]) for r in rows.values()} == {(str(tag.uuid), scope)}
    everything = _listed(client, user_token_headers, stem, scope="all")
    assert {r["uuid"] for r in everything.values()} == {str(t.uuid) for t in expected.values()}
    assert everything[theirs.name]["usage_count"] == 1

    refused = client.patch(
        f"/api/tags/{theirs.uuid}", headers=user_token_headers, json={"name": word("hijack")}
    )
    assert refused.status_code == 404
    assert refused.json() == {"detail": "Tag not found"}


def test_unused_filter_and_unused_route_agree_on_usage(
    client, db_session, normal_user, other_user, user_token_headers
):
    stem = word("unu")
    on_own_file = make_tag(db_session, f"{stem}-own", user_id=normal_user.id)
    on_hidden_file = make_tag(db_session, f"{stem}-hidden", user_id=normal_user.id)
    attach(db_session, make_file(db_session, normal_user), on_own_file)
    attach(db_session, make_file(db_session, other_user), on_hidden_file)

    listed = _listed(client, user_token_headers, stem)
    unused_filter = _listed(client, user_token_headers, stem, unused="true")
    unused_route = client.get("/api/tags/unused", headers=user_token_headers)

    assert (
        listed[on_own_file.name]["usage_count"],
        listed[on_hidden_file.name]["usage_count"],
    ) == (
        1,
        0,
    )
    assert set(unused_filter) == {on_hidden_file.name}
    assert unused_route.status_code == 200
    assert [r["name"] for r in unused_route.json() if r["name"].startswith(stem)] == [
        on_hidden_file.name
    ]


@pytest.mark.parametrize("path", ["/api/tags?unused=true", "/api/tags/unused"])
def test_a_failing_access_query_propagates_instead_of_an_empty_list(
    client, user_token_headers, monkeypatch, path
):
    def broken(*args, **kwargs):
        raise RuntimeError("access query failed")

    monkeypatch.setattr(PermissionService, "get_accessible_file_ids_subquery", broken)
    with pytest.raises(RuntimeError, match="access query failed"):
        client.get(path, headers=user_token_headers)


# ---------------------------------------------------------------------------
# GET /api/tags/collisions and the colliding filter
# ---------------------------------------------------------------------------


def test_collisions_cluster_variants_suggest_near_matches_and_are_stable(
    client, db_session, normal_user, user_token_headers
):
    stem = word("col")
    plain = make_tag(db_session, f"{stem}-q3-earnings", user_id=normal_user.id)
    variant = make_tag(db_session, f"{stem}_Q3_Earnings", user_id=normal_user.id)
    near = make_tag(db_session, f"{stem}-q4-earnings", user_id=normal_user.id)
    lone = make_tag(db_session, f"{stem}-unique", user_id=normal_user.id)
    attach(db_session, make_file(db_session, normal_user), variant)

    first = client.get("/api/tags/collisions", headers=user_token_headers)
    second = client.get("/api/tags/collisions", headers=user_token_headers)

    assert first.status_code == 200
    assert first.content == second.content
    ours = [c for c in first.json() if c["normalized_name"].startswith(stem.replace("-", " "))]
    assert len(ours) == 1
    cluster = ours[0]
    assert [(m["uuid"], m["suggested_survivor"]) for m in cluster["members"]] == [
        (str(variant.uuid), True),
        (str(plain.uuid), False),
    ]
    assert cluster["suggested_survivor_uuid"] == str(variant.uuid)
    assert [s["uuid"] for s in cluster["suggestions"]] == [str(near.uuid)]

    colliding = _listed(client, user_token_headers, stem, colliding="true")
    assert set(colliding) == {plain.name, variant.name}
    both = _listed(client, user_token_headers, stem, colliding="true", unused="true")
    assert set(both) == {plain.name}
    assert lone.name in _listed(client, user_token_headers, stem)


# ---------------------------------------------------------------------------
# PATCH, merge, DELETE and impact
# ---------------------------------------------------------------------------


def test_rename_route_plain_held_and_confirmed(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    record_reindex(monkeypatch)
    target = make_tag(db_session, word("Target"), user_id=normal_user.id)
    renamed = make_tag(db_session, word("Renamed"), user_id=normal_user.id)
    attach(db_session, make_file(db_session, normal_user), renamed)
    url = f"/api/tags/{renamed.uuid}"

    new_name = word("Fresh")
    plain = client.patch(url, headers=user_token_headers, json={"name": new_name})
    assert plain.status_code == 200, plain.text
    assert plain.json()["tag"] == {
        "uuid": str(renamed.uuid),
        "name": new_name,
        "source": "manual",
        "ownership": "mine",
        "can_write": True,
    }
    assert (plain.json()["merged"], plain.json()["requires_confirmation"]) == (False, False)

    held = client.patch(url, headers=user_token_headers, json={"name": target.name.upper()})
    assert held.status_code == 200
    assert held.json()["requires_confirmation"] is True
    assert held.json()["merged"] is False
    assert held.json()["tag"]["uuid"] == str(target.uuid)
    assert held.json()["impact"]["accessible_file_count"] == 1
    assert held.json()["impact"]["total_file_count"] == 1
    assert _exists(db_session, renamed.id)

    confirmed = client.patch(
        url, headers=user_token_headers, json={"name": target.name, "confirm_merge": True}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["merged"] is True
    assert confirmed.json()["tag"]["uuid"] == str(target.uuid)
    assert confirmed.json()["deleted_uuids"] == [str(renamed.uuid)]
    assert not _exists(db_session, renamed.id)


def test_merge_route_folds_and_rejects_an_unknown_source(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    record_reindex(monkeypatch)
    target = make_tag(db_session, word("mt"), user_id=normal_user.id)
    source = make_tag(db_session, word("ms"), user_id=normal_user.id)

    unknown = client.post(
        f"/api/tags/{target.uuid}/merge",
        headers=user_token_headers,
        json={"source_uuids": [str(source.uuid), str(uuid.uuid4())]},
    )
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Tag not found"}
    assert _exists(db_session, source.id)

    merged = client.post(
        f"/api/tags/{target.uuid}/merge",
        headers=user_token_headers,
        json={"source_uuids": [str(source.uuid)]},
    )
    assert merged.status_code == 200
    assert merged.json()["merged"] is True
    assert merged.json()["deleted_uuids"] == [str(source.uuid)]
    assert not _exists(db_session, source.id)


def test_delete_route_reports_deleted_uuids_and_drops_them_from_the_list(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    record_reindex(monkeypatch)
    stem = word("del")
    a = make_tag(db_session, f"{stem}-a", user_id=normal_user.id)
    b = make_tag(db_session, f"{stem}-b", user_id=normal_user.id)

    response = client.delete(
        "/api/tags", headers=user_token_headers, params={"tag_uuids": [str(b.uuid), str(a.uuid)]}
    )

    assert response.status_code == 200, response.text
    assert response.json()["deleted_uuids"] == [str(a.uuid), str(b.uuid)]
    assert response.json()["impact"]["total_file_count"] == 0
    assert response.json()["tag"] is None
    assert _listed(client, user_token_headers, stem) == {}


def test_delete_of_another_users_tag_is_a_404(client, db_session, other_user, user_token_headers):
    theirs = make_tag(db_session, word("nope"), user_id=other_user.id)
    response = client.delete(
        "/api/tags", headers=user_token_headers, params={"tag_uuids": [str(theirs.uuid)]}
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Tag not found"}
    assert _exists(db_session, theirs.id)


def test_impact_separates_accessible_from_total(
    client, db_session, normal_user, other_user, user_token_headers
):
    tag = make_tag(db_session, word("imp"), user_id=normal_user.id)
    attach(db_session, make_file(db_session, normal_user), tag)
    attach(db_session, make_file(db_session, other_user), tag)

    response = client.get(
        "/api/tags/impact", headers=user_token_headers, params={"tag_uuids": [str(tag.uuid)]}
    )

    assert response.status_code == 200
    assert response.json() == {
        "tags": [
            {
                "uuid": str(tag.uuid),
                "name": tag.name,
                "accessible_file_count": 1,
                "total_file_count": 2,
            }
        ],
        "accessible_file_count": 1,
        "total_file_count": 2,
    }


def test_promote_needs_deployment_scope(client, db_session, normal_user, user_token_headers):
    tag = make_tag(db_session, word("pr"), user_id=normal_user.id)
    response = client.post(
        "/api/tags/promote", headers=user_token_headers, json={"tag_uuids": [str(tag.uuid)]}
    )
    assert response.status_code == 403
    assert response.json() == {
        "detail": "Only admin users can promote tags to the shared vocabulary"
    }
    db_session.expire_all()
    assert db_session.get(Tag, tag.id).user_id == normal_user.id


def test_promote_by_a_single_tenant_admin_moves_the_tag_into_the_shared_vocabulary(
    client, db_session, admin_user, admin_token_headers, single_tenant, monkeypatch
):
    record_reindex(monkeypatch)
    tag = make_tag(db_session, word("prom"), user_id=admin_user.id)
    response = client.post(
        "/api/tags/promote", headers=admin_token_headers, json={"tag_uuids": [str(tag.uuid)]}
    )
    assert response.status_code == 200, response.text
    db_session.expire_all()
    row = db_session.get(Tag, tag.id)
    assert (row.user_id, row.organization_id) == (None, None)


# ---------------------------------------------------------------------------
# DELETE /api/tags/cleanup
# ---------------------------------------------------------------------------


def test_cleanup_is_admin_only(client, db_session, normal_user, user_token_headers):
    unused = make_tag(db_session, word("cu"), user_id=normal_user.id)
    response = client.delete("/api/tags/cleanup", headers=user_token_headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "Only admin users can clean up unused tags"}
    assert _exists(db_session, unused.id)


def test_cleanup_mine_removes_only_the_callers_unreferenced_tags(
    client, db_session, admin_user, normal_user, admin_token_headers, monkeypatch
):
    record_reindex(monkeypatch)
    own_unused = make_tag(db_session, word("own"), user_id=admin_user.id)
    own_used = make_tag(db_session, word("used"), user_id=admin_user.id)
    others = make_tag(db_session, word("other"), user_id=normal_user.id)
    system = make_tag(db_session, word("sys"), user_id=None)
    attach(db_session, make_file(db_session, normal_user), own_used)

    ids = [t.id for t in (own_unused, own_used, others, system)]

    response = client.delete("/api/tags/cleanup", headers=admin_token_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "mine"
    assert body["message"] == f"{body['deleted_count']} unused tags deleted successfully"
    assert body["deleted_count"] == 1
    assert [_exists(db_session, tag_id) for tag_id in ids] == [
        False,
        True,
        True,
        True,
    ]


def test_cleanup_with_an_unknown_scope_is_a_422(client, admin_token_headers):
    response = client.delete(
        "/api/tags/cleanup", headers=admin_token_headers, params={"scope": "everyone"}
    )
    assert response.status_code == 422


def test_a_deployment_wide_cleanup_needs_confirmation_then_sweeps_every_account(
    client, db_session, admin_user, normal_user, admin_token_headers, single_tenant, monkeypatch
):
    record_reindex(monkeypatch)
    others_unused = make_tag(db_session, word("ou"), user_id=normal_user.id)
    others_used = make_tag(db_session, word("ok"), user_id=normal_user.id)
    system = make_tag(db_session, word("ks"), user_id=None)
    attach(db_session, make_file(db_session, normal_user), others_used)
    ids = [t.id for t in (others_unused, others_used, system)]

    unconfirmed = client.delete(
        "/api/tags/cleanup", headers=admin_token_headers, params={"scope": "all_users"}
    )
    assert unconfirmed.status_code == 400
    assert unconfirmed.json() == {
        "detail": "Deployment-wide tag cleanup deletes other users' tags and cannot be "
        "undone. Retry with confirm=true, or use scope=mine."
    }
    assert _exists(db_session, ids[0])

    confirmed = client.delete(
        "/api/tags/cleanup",
        headers=admin_token_headers,
        params={"scope": "all_users", "confirm": "true"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["scope"] == "all_users"
    assert [_exists(db_session, tag_id) for tag_id in ids] == [
        False,
        True,
        True,
    ]


def test_cleanup_passes_the_sweep_width_by_keyword(
    client, admin_user, admin_token_headers, single_tenant, monkeypatch
):
    calls: list[dict] = []

    def sweep(db, **kwargs):
        calls.append(kwargs)
        return 7

    monkeypatch.setattr("app.api.endpoints.tags.crud.cleanup_unreferenced_tags", sweep)
    mine = client.delete("/api/tags/cleanup", headers=admin_token_headers)
    wide = client.delete(
        "/api/tags/cleanup",
        headers=admin_token_headers,
        params={"scope": "all_users", "confirm": "true"},
    )

    assert mine.json() == {
        "deleted_count": 7,
        "scope": "mine",
        "message": "7 unused tags deleted successfully",
    }
    assert wide.status_code == 200
    assert calls == [
        {"acting_user_id": admin_user.id, "organization_id": None, "all_users": False},
        {"acting_user_id": admin_user.id, "organization_id": None, "all_users": True},
    ]


def test_a_failing_cleanup_is_a_fixed_500_and_the_error_is_logged(
    client, admin_token_headers, monkeypatch, caplog
):
    def sweep(db, **kwargs):
        raise RuntimeError("sweep exploded at row 42")

    monkeypatch.setattr("app.api.endpoints.tags.crud.cleanup_unreferenced_tags", sweep)
    with caplog.at_level(logging.ERROR):
        response = client.delete("/api/tags/cleanup", headers=admin_token_headers)

    assert response.status_code == 500
    assert response.content == b'{"detail":"Could not clean up unused tags."}'
    assert "sweep exploded at row 42" in caplog.text


# ---------------------------------------------------------------------------
# GET /api/tags/{uuid}/files
# ---------------------------------------------------------------------------


def test_tag_files_lists_accessible_files_with_the_true_total(
    client, db_session, normal_user, other_user, user_token_headers
):
    tag = make_tag(db_session, word("tf"), user_id=None)
    files = [make_file(db_session, normal_user) for _ in range(3)]
    for media_file in files:
        attach(db_session, media_file, tag)
    attach(db_session, make_file(db_session, other_user), tag)

    response = client.get(
        f"/api/tags/{tag.uuid}/files", headers=user_token_headers, params={"limit": 2}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert len(body["files"]) == 2
    newest = sorted(files, key=lambda f: (f.upload_time, f.id), reverse=True)[0]
    assert body["files"][0] == {
        "uuid": str(newest.uuid),
        "display_title": newest.filename,
        "status": "completed",
        "formatted_duration": FormattingService.format_duration(newest.duration),
    }
    assert {f["uuid"] for f in body["files"]} <= {str(f.uuid) for f in files}


@pytest.mark.parametrize("limit", [0, 201])
def test_tag_files_limit_out_of_range_is_a_422(
    client, db_session, normal_user, user_token_headers, limit
):
    tag = make_tag(db_session, word("lim"), user_id=normal_user.id)
    response = client.get(
        f"/api/tags/{tag.uuid}/files", headers=user_token_headers, params={"limit": limit}
    )
    assert response.status_code == 422


def test_tag_files_of_an_unknown_or_invisible_tag_is_a_404(
    client, db_session, other_user, user_token_headers
):
    invisible = make_tag(db_session, word("inv"), user_id=other_user.id)
    for tag_uuid in (uuid.uuid4(), invisible.uuid):
        response = client.get(f"/api/tags/{tag_uuid}/files", headers=user_token_headers)
        assert response.status_code == 404
        assert response.json() == {"detail": "Tag not found"}


def test_attach_creates_an_attributed_row_and_one_association(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    reindex = record_reindex(monkeypatch)
    media_file = make_file(db_session, normal_user)
    name = word("Attached")
    response = client.post(
        f"/api/tags/files/{media_file.uuid}/tags", headers=user_token_headers, json={"name": name}
    )
    assert response.status_code == 200, response.text
    row = db_session.query(Tag).filter(Tag.uuid == uuid.UUID(response.json()["uuid"])).one()
    assert (row.user_id, row.organization_id, row.source) == (normal_user.id, None, "manual")
    links = db_session.query(FileTag).filter(FileTag.media_file_id == media_file.id).all()
    assert [(link.tag_id, link.source) for link in links] == [(row.id, "manual")]
    assert reindex.calls == [[media_file.id]]
