"""Characterization of ``POST /tags/files/{uuid}/tags`` and ``DELETE /tags/files/{uuid}/tags/{name}``.

Written before the tags backend rewrite. Tests whose name says ``pins_current_behaviour``
pin behaviour the owner has decided to change (D2 viewer can attach, D3 ownership label,
D10 non-string name); the lane that changes it flips them red first.
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.tag_world import acting_as
from fixtures.tag_world import attach
from fixtures.tag_world import build_org_world
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word

from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.sharing import CollectionShare
from app.models.tag import FileTag
from app.models.tag import Tag


class _RecordingTask:
    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    def delay(self, file_ids):
        self.calls.append(list(file_ids))


@pytest.fixture
def reindex(monkeypatch):
    from app.tasks import search_file_field_tasks

    recorder = _RecordingTask()
    monkeypatch.setattr(search_file_field_tasks, "update_file_tags_index", recorder)
    return recorder


@pytest.fixture
def org_world(db_session):
    return build_org_world(db_session)


def _share(db, owner, target, media_file, permission: str) -> None:
    collection = Collection(name=word("coll"), user_id=owner.id)
    db.add(collection)
    db.commit()
    db.refresh(collection)
    db.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=target.id,
            permission=permission,
        )
    )
    db.commit()


def _links(db, media_file) -> list[FileTag]:
    links: list[FileTag] = db.query(FileTag).filter(FileTag.media_file_id == media_file.id).all()
    return links


def _attach_url(media_file) -> str:
    return f"/api/tags/files/{media_file.uuid}/tags"


# ---------------------------------------------------------------------------
# POST: validation
# ---------------------------------------------------------------------------


def test_attach_without_a_name_key_is_a_422(client, db_session, normal_user, user_token_headers):
    media_file = make_file(db_session, normal_user)

    response = client.post(_attach_url(media_file), headers=user_token_headers, json={"other": "x"})

    assert response.status_code == 422
    assert response.json()["detail"] == "Tag name is required"
    assert _links(db_session, media_file) == []


def test_attach_with_an_empty_body_object_is_a_422(
    client, db_session, normal_user, user_token_headers
):
    media_file = make_file(db_session, normal_user)

    response = client.post(_attach_url(media_file), headers=user_token_headers, json={})

    assert response.status_code == 422
    assert response.json()["detail"] == "Tag name is required"


@pytest.mark.parametrize("blank", ["", "   ", "-", "__"])
def test_attach_with_a_name_that_normalizes_to_nothing_is_a_422(
    client, db_session, normal_user, user_token_headers, blank
):
    media_file = make_file(db_session, normal_user)

    response = client.post(
        _attach_url(media_file), headers=user_token_headers, json={"name": blank}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Tag name is required"
    assert _links(db_session, media_file) == []


def test_attach_to_an_unknown_file_is_a_404(client, user_token_headers):
    response = client.post(
        f"/api/tags/files/{uuid.uuid4()}/tags", headers=user_token_headers, json={"name": "x"}
    )

    assert response.status_code == 404


def test_attach_with_a_non_string_name_is_unhandled_pins_current_behaviour(
    client, db_session, normal_user, user_token_headers
):
    """D10: ``TagBase(name=123)`` raises a pydantic error inside the handler.

    No handler converts it to a 4xx, so the TestClient re-raises the server error. The
    assertion names the exception type so a rewrite that returns a clean 422 turns this red.
    """
    from pydantic import ValidationError

    media_file = make_file(db_session, normal_user)

    with pytest.raises(ValidationError):
        client.post(_attach_url(media_file), headers=user_token_headers, json={"name": 123})

    assert _links(db_session, media_file) == []


# ---------------------------------------------------------------------------
# POST: idempotence and the reindex hook
# ---------------------------------------------------------------------------


def test_attach_is_idempotent_one_association_and_one_reindex(
    client, db_session, normal_user, user_token_headers, reindex
):
    media_file = make_file(db_session, normal_user)
    name = word("idem")

    first = client.post(_attach_url(media_file), headers=user_token_headers, json={"name": name})
    second = client.post(_attach_url(media_file), headers=user_token_headers, json={"name": name})
    variant = client.post(
        _attach_url(media_file), headers=user_token_headers, json={"name": name.upper()}
    )

    assert first.status_code == second.status_code == variant.status_code == 200
    assert first.json()["uuid"] == second.json()["uuid"] == variant.json()["uuid"]
    assert len(_links(db_session, media_file)) == 1
    assert reindex.calls == [[media_file.id]]


def test_attach_records_a_manual_association_and_a_manual_tag(
    client, db_session, normal_user, user_token_headers, reindex
):
    media_file = make_file(db_session, normal_user)

    response = client.post(
        _attach_url(media_file), headers=user_token_headers, json={"name": word("src")}
    )

    assert response.status_code == 200
    (link,) = _links(db_session, media_file)
    assert link.source == "manual"
    tag = db_session.query(Tag).filter(Tag.id == link.tag_id).one()
    assert (tag.source, tag.user_id) == ("manual", normal_user.id)


def test_attach_response_carries_only_uuid_name_source_ownership(
    client, db_session, normal_user, user_token_headers, reindex
):
    media_file = make_file(db_session, normal_user)

    body = client.post(
        _attach_url(media_file), headers=user_token_headers, json={"name": word("shape")}
    ).json()

    assert set(body) == {"uuid", "name", "source", "ownership"}
    assert body["ownership"] == "mine"


# ---------------------------------------------------------------------------
# D2: a viewer may attach and detach today
# ---------------------------------------------------------------------------


def test_viewer_shared_file_can_be_tagged_pins_current_behaviour(
    client, db_session, normal_user, other_user, user_token_headers, reindex
):
    """D2: the single-file route loads the file at ``viewer``; the bulk rail needs ``editor``."""
    media_file = make_file(db_session, other_user)
    _share(db_session, other_user, normal_user, media_file, "viewer")
    name = word("viewer")

    response = client.post(_attach_url(media_file), headers=user_token_headers, json={"name": name})

    assert response.status_code == 200, response.text
    (link,) = _links(db_session, media_file)
    tag = db_session.query(Tag).filter(Tag.id == link.tag_id).one()
    # Resolved in the file's tenant (personal) as the *caller's* own row.
    assert tag.user_id == normal_user.id
    assert reindex.calls == [[media_file.id]]


def test_viewer_shared_file_can_be_detached_pins_current_behaviour(
    client, db_session, normal_user, other_user, user_token_headers, reindex
):
    """D2, detach half."""
    media_file = make_file(db_session, other_user)
    tag = make_tag(db_session, word("v-detach"), user_id=other_user.id)
    attach(db_session, media_file, tag)
    _share(db_session, other_user, normal_user, media_file, "viewer")

    response = client.delete(f"{_attach_url(media_file)}/{tag.name}", headers=user_token_headers)

    assert response.status_code == 204, response.text
    assert _links(db_session, media_file) == []


def test_a_stranger_cannot_attach_to_someone_elses_file(
    client, db_session, other_user, user_token_headers
):
    media_file = make_file(db_session, other_user)

    response = client.post(_attach_url(media_file), headers=user_token_headers, json={"name": "x"})

    assert response.status_code == 403
    assert _links(db_session, media_file) == []


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


def test_detach_removes_the_association_keeps_the_tag_and_reindexes(
    client, db_session, normal_user, user_token_headers, reindex
):
    media_file = make_file(db_session, normal_user)
    tag = make_tag(db_session, word("keep"), user_id=normal_user.id)
    attach(db_session, media_file, tag)

    response = client.delete(f"{_attach_url(media_file)}/{tag.name}", headers=user_token_headers)

    assert response.status_code == 204
    assert response.content == b""
    assert _links(db_session, media_file) == []
    assert db_session.query(Tag).filter(Tag.id == tag.id).first() is not None
    assert reindex.calls == [[media_file.id]]


def test_detach_matches_the_stored_name_case_sensitively(
    client, db_session, normal_user, user_token_headers, reindex
):
    media_file = make_file(db_session, normal_user)
    tag = make_tag(db_session, f"Mixed-{uuid.uuid4().hex[:6]}", user_id=normal_user.id)
    attach(db_session, media_file, tag)

    wrong_case = client.delete(
        f"{_attach_url(media_file)}/{tag.name.lower()}", headers=user_token_headers
    )

    assert wrong_case.status_code == 404
    assert wrong_case.json()["detail"] == "Tag not found"
    assert len(_links(db_session, media_file)) == 1
    assert reindex.calls == []


def test_detach_of_a_tag_not_on_this_file_is_a_404_even_if_the_tag_exists(
    client, db_session, normal_user, user_token_headers, reindex
):
    media_file = make_file(db_session, normal_user)
    elsewhere = make_file(db_session, normal_user)
    tag = make_tag(db_session, word("elsewhere"), user_id=normal_user.id)
    attach(db_session, elsewhere, tag)

    response = client.delete(f"{_attach_url(media_file)}/{tag.name}", headers=user_token_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Tag not found"
    assert len(_links(db_session, elsewhere)) == 1
    assert reindex.calls == []


def test_detach_does_not_touch_another_users_same_named_tag(
    client, db_session, normal_user, other_user, user_token_headers, reindex
):
    name = word("twin")
    mine_file = make_file(db_session, normal_user)
    theirs_file = make_file(db_session, other_user)
    mine = make_tag(db_session, name, user_id=normal_user.id)
    theirs = make_tag(db_session, name, user_id=other_user.id)
    attach(db_session, mine_file, mine)
    attach(db_session, theirs_file, theirs)

    response = client.delete(f"{_attach_url(mine_file)}/{name}", headers=user_token_headers)

    assert response.status_code == 204
    assert _links(db_session, mine_file) == []
    assert len(_links(db_session, theirs_file)) == 1


def test_detach_of_a_name_containing_a_slash_cannot_be_routed_pins_current_behaviour(
    client, db_session, normal_user, user_token_headers
):
    """D9: the name is a single path segment, so ``a/b`` never reaches the handler."""
    media_file = make_file(db_session, normal_user)
    tag = make_tag(db_session, "alpha/beta", user_id=normal_user.id)
    attach(db_session, media_file, tag)

    response = client.delete(f"{_attach_url(media_file)}/alpha/beta", headers=user_token_headers)

    assert response.status_code == 404
    assert len(_links(db_session, media_file)) == 1


# ---------------------------------------------------------------------------
# D3: the ownership label on the bare-ORM response
# ---------------------------------------------------------------------------


def test_attach_labels_a_colleagues_org_tag_as_mine_pins_current_behaviour(client, org_world):
    """D3: ``Tag`` infers ownership from ``user_id`` alone, so the creator's row reads ``mine``."""
    w = org_world
    name = word("colleague")
    alice_tag = make_tag(w.db, name, user_id=w.alice.id, org_id=w.org_a.id)
    bob_file = make_file(w.db, w.bob, w.org_a.id)

    with acting_as(w.bob, w.org_a.id):
        response = client.post(_attach_url(bob_file), json={"name": name})

    assert response.status_code == 200, response.text
    assert response.json()["uuid"] == str(alice_tag.uuid)
    assert response.json()["ownership"] == "mine"


def test_attach_labels_an_unattributed_org_tag_as_system_pins_current_behaviour(client, org_world):
    """D3: an org tag whose creator is gone has ``user_id`` NULL and so reads ``system``."""
    w = org_world
    name = word("orphan")
    orphan = make_tag(w.db, name, user_id=None, org_id=w.org_a.id)
    bob_file = make_file(w.db, w.bob, w.org_a.id)

    with acting_as(w.bob, w.org_a.id):
        response = client.post(_attach_url(bob_file), json={"name": name})

    assert response.status_code == 200, response.text
    assert response.json()["uuid"] == str(orphan.uuid)
    assert response.json()["ownership"] == "system"


def test_attach_to_a_personal_file_creates_the_callers_personal_row(client, org_world):
    """A personal file gets the caller's personal row, with no organization."""
    w = org_world
    name = word("filetenant")
    personal_file = make_file(w.db, w.alice, None)

    with acting_as(w.alice, None):
        response = client.post(_attach_url(personal_file), json={"name": name})

    assert response.status_code == 200, response.text
    tag = w.db.query(Tag).filter(Tag.name == name).one()
    assert (tag.user_id, tag.organization_id) == (w.alice.id, None)
