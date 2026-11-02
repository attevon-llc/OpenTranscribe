"""Characterization of ``GET /api/tags/for-files`` (what a bulk selection already carries).

Written before the tags backend rewrite. Every assertion describes what the route does
today; the two tests whose name says ``pins_current_behaviour`` pin behaviour the owner has
decided to change (an inaccessible file is accepted and counted, rather than answered like
an unknown one), so the lane that changes it flips them red first.
"""

from __future__ import annotations

import uuid

from app.models.media import MediaFile
from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.tag_service import normalize_tag_name

URL = "/api/tags/for-files"


def _word(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _tag(db, name: str, *, user_id: int | None) -> Tag:
    tag = Tag(name=name, user_id=user_id, source="manual", normalized_name=normalize_tag_name(name))
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def _file(db, owner) -> MediaFile:
    media_file = MediaFile(
        filename=f"forfiles_{uuid.uuid4().hex[:8]}.mp3",
        storage_path=f"forfiles/{uuid.uuid4().hex}",
        file_size=1,
        content_type="audio/mpeg",
        user_id=owner.id,
        status="completed",
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _attach(db, media_file, tag) -> None:
    db.add(FileTag(media_file_id=media_file.id, tag_id=tag.id, source="manual"))
    db.commit()


def _query(*files: MediaFile) -> list[tuple[str, str]]:
    return [("file_uuids", str(f.uuid)) for f in files]


def test_rows_are_ordered_by_file_count_descending_then_name(
    client, db_session, normal_user, user_token_headers
):
    first, second = _file(db_session, normal_user), _file(db_session, normal_user)
    on_both = _tag(db_session, _word("zz-both"), user_id=normal_user.id)
    beta = _tag(db_session, _word("b-beta"), user_id=normal_user.id)
    alpha = _tag(db_session, _word("a-alpha"), user_id=normal_user.id)
    for tag in (on_both,):
        _attach(db_session, first, tag)
        _attach(db_session, second, tag)
    _attach(db_session, first, beta)
    _attach(db_session, second, alpha)

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
    media_file = _file(db_session, normal_user)
    tag = _tag(db_session, _word("shape"), user_id=normal_user.id)
    _attach(db_session, media_file, tag)

    (row,) = client.get(URL, headers=user_token_headers, params=_query(media_file)).json()

    assert set(row) == {
        "uuid",
        "name",
        "source",
        "ownership",
        "file_count",
        "selection_size",
    }
    assert row["uuid"] == str(tag.uuid)
    assert row["source"] == "manual"


def test_ownership_is_reported_per_row(
    client, db_session, normal_user, other_user, user_token_headers
):
    media_file = _file(db_session, normal_user)
    mine = _tag(db_session, _word("mine"), user_id=normal_user.id)
    theirs = _tag(db_session, _word("theirs"), user_id=other_user.id)
    system = _tag(db_session, _word("system"), user_id=None)
    for tag in (mine, theirs, system):
        _attach(db_session, media_file, tag)

    rows = client.get(URL, headers=user_token_headers, params=_query(media_file)).json()

    assert {r["name"]: r["ownership"] for r in rows} == {
        mine.name: "mine",
        theirs.name: "shared_with_me",
        system.name: "system",
    }


def test_a_repeated_uuid_counts_once_in_selection_size_and_file_count(
    client, db_session, normal_user, user_token_headers
):
    media_file = _file(db_session, normal_user)
    tag = _tag(db_session, _word("dup"), user_id=normal_user.id)
    _attach(db_session, media_file, tag)

    (row,) = client.get(
        URL, headers=user_token_headers, params=_query(media_file, media_file)
    ).json()

    assert (row["file_count"], row["selection_size"]) == (1, 1)


def test_tags_on_a_file_the_caller_cannot_access_are_not_returned(
    client, db_session, normal_user, other_user, user_token_headers
):
    mine_file = _file(db_session, normal_user)
    theirs_file = _file(db_session, other_user)
    visible = _tag(db_session, _word("visible"), user_id=normal_user.id)
    secret = _tag(db_session, _word("secret"), user_id=other_user.id)
    _attach(db_session, mine_file, visible)
    _attach(db_session, theirs_file, secret)

    rows = client.get(URL, headers=user_token_headers, params=_query(mine_file, theirs_file)).json()

    assert [r["name"] for r in rows] == [visible.name]


def test_inaccessible_file_is_accepted_and_counted_pins_current_behaviour(
    client, db_session, normal_user, other_user, user_token_headers
):
    """D5: an existing but inaccessible file is not a 404 and still inflates selection_size.

    The owner has decided it must be answered exactly like a nonexistent file (404).
    """
    mine_file = _file(db_session, normal_user)
    theirs_file = _file(db_session, other_user)
    tag = _tag(db_session, _word("oracle"), user_id=normal_user.id)
    _attach(db_session, mine_file, tag)

    response = client.get(URL, headers=user_token_headers, params=_query(mine_file, theirs_file))

    assert response.status_code == 200, response.text
    (row,) = response.json()
    assert row["file_count"] == 1
    assert row["selection_size"] == 2


def test_only_inaccessible_files_give_an_empty_list_not_a_404_pins_current_behaviour(
    client, db_session, other_user, user_token_headers
):
    """D5 again: the sole file belongs to someone else, yet the answer is 200 and ``[]``."""
    theirs_file = _file(db_session, other_user)
    _attach(db_session, theirs_file, _tag(db_session, _word("x"), user_id=other_user.id))

    response = client.get(URL, headers=user_token_headers, params=_query(theirs_file))

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_an_unknown_file_uuid_is_a_404_with_a_fixed_message(client, user_token_headers):
    response = client.get(
        URL, headers=user_token_headers, params=[("file_uuids", str(uuid.uuid4()))]
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "File not found"


def test_one_unknown_uuid_among_known_ones_fails_the_whole_request(
    client, db_session, normal_user, user_token_headers
):
    media_file = _file(db_session, normal_user)
    _attach(db_session, media_file, _tag(db_session, _word("k"), user_id=normal_user.id))

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
    media_file = _file(db_session, normal_user)

    response = client.get(URL, headers=user_token_headers, params=_query(media_file))

    assert response.status_code == 200
    assert response.json() == []
