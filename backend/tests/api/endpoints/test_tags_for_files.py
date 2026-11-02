"""Characterization of ``GET /api/tags/for-files`` (what a bulk selection already carries).

Written before the tags backend rewrite. A file the caller cannot open (another user's,
another tenant's) is answered exactly like an unknown uuid, so the route is no existence
signal (D5).
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.tag_recorders import share_file
from fixtures.tag_world import acting_as
from fixtures.tag_world import attach
from fixtures.tag_world import build_org_world
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word

from app.core.config import settings
from app.models.media import MediaFile
from app.services.platform_access import reset_tenancy_mode_cache

URL = "/api/tags/for-files"


def _query(*files: MediaFile) -> list[tuple[str, str]]:
    return [("file_uuids", str(f.uuid)) for f in files]


def _unknown(client, headers=None):
    return client.get(URL, headers=headers, params=[("file_uuids", str(uuid.uuid4()))])


def test_rows_are_ordered_by_file_count_descending_then_name(
    client, db_session, normal_user, user_token_headers
):
    first, second = make_file(db_session, normal_user), make_file(db_session, normal_user)
    on_both = make_tag(db_session, word("zz-both"), user_id=normal_user.id)
    beta = make_tag(db_session, word("b-beta"), user_id=normal_user.id)
    alpha = make_tag(db_session, word("a-alpha"), user_id=normal_user.id)
    attach(db_session, first, on_both)
    attach(db_session, second, on_both)
    attach(db_session, first, beta)
    attach(db_session, second, alpha)

    response = client.get(URL, headers=user_token_headers, params=_query(first, second))

    assert response.status_code == 200, response.text
    rows = response.json()
    # "zz-both" sorts last by name yet comes first because two files carry it.
    assert [r["name"] for r in rows] == [on_both.name, alpha.name, beta.name]
    assert [r["file_count"] for r in rows] == [2, 1, 1]
    assert {r["selection_size"] for r in rows} == {2}


def test_row_shape_has_no_internal_id_and_reports_the_source(
    client, db_session, normal_user, user_token_headers
):
    media_file = make_file(db_session, normal_user)
    tag = make_tag(db_session, word("shape"), user_id=normal_user.id)
    attach(db_session, media_file, tag)

    (row,) = client.get(URL, headers=user_token_headers, params=_query(media_file)).json()

    assert set(row) == {
        "uuid",
        "name",
        "source",
        "ownership",
        "can_write",
        "file_count",
        "selection_size",
    }
    assert row["uuid"] == str(tag.uuid)
    assert row["source"] == "manual"
    assert row["can_write"] is True


def test_ownership_is_reported_per_row(
    client, db_session, normal_user, other_user, user_token_headers
):
    media_file = make_file(db_session, normal_user)
    mine = make_tag(db_session, word("mine"), user_id=normal_user.id)
    theirs = make_tag(db_session, word("theirs"), user_id=other_user.id)
    system = make_tag(db_session, word("system"), user_id=None)
    for tag in (mine, theirs, system):
        attach(db_session, media_file, tag)

    rows = client.get(URL, headers=user_token_headers, params=_query(media_file)).json()

    assert {r["name"]: r["ownership"] for r in rows} == {
        mine.name: "mine",
        theirs.name: "shared_with_me",
        system.name: "system",
    }


def test_a_repeated_uuid_counts_once_in_selection_size_and_file_count(
    client, db_session, normal_user, user_token_headers
):
    media_file = make_file(db_session, normal_user)
    tag = make_tag(db_session, word("dup"), user_id=normal_user.id)
    attach(db_session, media_file, tag)

    (row,) = client.get(
        URL, headers=user_token_headers, params=_query(media_file, media_file)
    ).json()

    assert (row["file_count"], row["selection_size"]) == (1, 1)


def test_a_file_shared_with_the_caller_is_counted_and_its_tags_returned(
    client, db_session, normal_user, other_user, user_token_headers
):
    """Control for the 404s below: access through a share is access."""
    mine_file = make_file(db_session, normal_user)
    shared_file = make_file(db_session, other_user)
    share_file(db_session, other_user, normal_user, shared_file)
    visible = make_tag(db_session, word("visible"), user_id=normal_user.id)
    theirs = make_tag(db_session, word("theirs"), user_id=other_user.id)
    attach(db_session, mine_file, visible)
    attach(db_session, shared_file, theirs)

    response = client.get(URL, headers=user_token_headers, params=_query(mine_file, shared_file))

    assert response.status_code == 200, response.text
    rows = response.json()
    assert sorted(r["name"] for r in rows) == sorted([visible.name, theirs.name])
    assert {r["selection_size"] for r in rows} == {2}


def test_an_inaccessible_file_among_accessible_ones_is_answered_like_an_unknown_uuid(
    client, db_session, normal_user, other_user, user_token_headers
):
    mine_file = make_file(db_session, normal_user)
    theirs_file = make_file(db_session, other_user)
    tag = make_tag(db_session, word("oracle"), user_id=normal_user.id)
    secret = make_tag(db_session, word("secret"), user_id=other_user.id)
    attach(db_session, mine_file, tag)
    attach(db_session, theirs_file, secret)

    response = client.get(URL, headers=user_token_headers, params=_query(mine_file, theirs_file))
    unknown = client.get(
        URL,
        headers=user_token_headers,
        params=[*_query(mine_file), ("file_uuids", str(uuid.uuid4()))],
    )

    assert response.status_code == 404, response.text
    assert response.json() == unknown.json() == {"detail": "File not found"}
    assert secret.name not in response.text


def test_only_inaccessible_files_are_answered_like_an_unknown_uuid(
    client, db_session, other_user, user_token_headers
):
    theirs_file = make_file(db_session, other_user)
    attach(db_session, theirs_file, make_tag(db_session, word("x"), user_id=other_user.id))

    response = client.get(URL, headers=user_token_headers, params=_query(theirs_file))

    assert response.status_code == 404, response.text
    assert response.json() == _unknown(client, user_token_headers).json()


def test_another_tenants_file_is_answered_like_an_unknown_uuid(client, db_session):
    w = build_org_world(db_session)
    org_file = make_file(db_session, w.alice, w.org_a.id)
    personal_file = make_file(db_session, w.alice, None)

    with acting_as(w.alice, None):
        cross = client.get(URL, params=_query(org_file))
        unknown = _unknown(client)
        own = client.get(URL, params=_query(personal_file))

    assert cross.status_code == 404, cross.text
    assert cross.json() == unknown.json()
    assert own.status_code == 200, own.text


@pytest.fixture
def single_tenant(monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()
    yield
    reset_tenancy_mode_cache()


def test_a_single_tenant_admin_may_select_any_users_file(
    client, db_session, normal_user, admin_token_headers, single_tenant
):
    """Control: the gallery lists every file to such an admin, so the selection is valid."""
    media_file = make_file(db_session, normal_user)

    response = client.get(URL, headers=admin_token_headers, params=_query(media_file))

    assert response.status_code == 200, response.text


def test_an_unknown_file_uuid_is_a_404_with_a_fixed_message(client, user_token_headers):
    response = client.get(
        URL, headers=user_token_headers, params=[("file_uuids", str(uuid.uuid4()))]
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "File not found"


def test_one_unknown_uuid_among_known_ones_fails_the_whole_request(
    client, db_session, normal_user, user_token_headers
):
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, make_tag(db_session, word("k"), user_id=normal_user.id))

    response = client.get(
        URL,
        headers=user_token_headers,
        params=[*_query(media_file), ("file_uuids", str(uuid.uuid4()))],
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "File not found"


def test_no_uuids_is_a_422(client, user_token_headers):
    assert client.get(URL, headers=user_token_headers).status_code == 422


def test_a_malformed_uuid_is_a_422(client, user_token_headers):
    response = client.get(URL, headers=user_token_headers, params=[("file_uuids", "not-a-uuid")])

    assert response.status_code == 422


def test_unauthenticated_is_a_401(client):
    assert client.get(URL, params=[("file_uuids", str(uuid.uuid4()))]).status_code == 401


def test_a_file_with_no_tags_gives_an_empty_list(
    client, db_session, normal_user, user_token_headers
):
    media_file = make_file(db_session, normal_user)

    response = client.get(URL, headers=user_token_headers, params=_query(media_file))

    assert response.status_code == 200
    assert response.json() == []
