"""Authorization of the tag routes that touch a file or rewrite a tag.

Three rules, each proved from both sides (the allowed case is the control):

* Single-file attach and detach need ``editor`` on the file, the same bar as the bulk rail; a
  read-only share is refused 403 "Requires editor permission on this file".
* Every tag mutation refuses a support-access grant, a WRITE grant included, as ``POST /tags``
  always did. Reads under a grant are untouched.
* ``GET /tags/for-files`` and single-file attach/detach answer a file the caller cannot open at
  all exactly like an unknown uuid (404 "File not found"); a share below the required level
  still answers 403, because that caller can already see the file.
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.support_world import make_file
from fixtures.tenant_rows import make_tag

from app.core.constants import TAG_SOURCE_MANUAL
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import Tag
from app.models.sharing import CollectionShare
from app.utils.uuid_helpers import get_file_by_uuid_with_permission


def _word(label: str = "ta") -> str:
    return f"{label}{uuid.uuid4().hex[:8]}"


def _attach_url(media_file) -> str:
    return f"/api/tags/files/{media_file.uuid}/tags"


def _links(db, media_file) -> list[FileTag]:
    db.expire_all()
    rows: list[FileTag] = db.query(FileTag).filter(FileTag.media_file_id == media_file.id).all()
    return rows


def _link(db, media_file, tag) -> None:
    db.add(FileTag(media_file_id=media_file.id, tag_id=tag.id, source=TAG_SOURCE_MANUAL))
    db.commit()


def _share(db, media_file, owner, target, permission: str) -> None:
    """Put ``media_file`` in a collection of ``owner`` shared with ``target``."""
    collection = Collection(user_id=owner.id, name=_word("coll"), description="tag authz")
    db.add(collection)
    db.commit()
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


def _code(response) -> str:
    return str(response.json()["detail"]["code"])


def _refused_by_grant(response) -> None:
    assert response.status_code == 403, response.text
    assert _code(response) == "support_grant_action_not_permitted"


def _exists(db, tag: Tag) -> bool:
    db.expire_all()
    return db.query(Tag).filter(Tag.id == tag.id).first() is not None


# --- 1. attach / detach need editor ---------------------------------------------------------


def test_a_viewer_share_cannot_attach(
    client, db_session, normal_user, other_user, user_token_headers
):
    media_file = make_file(db_session, other_user, None)
    _share(db_session, media_file, other_user, normal_user, "viewer")
    name = _word("viewer")

    response = client.post(_attach_url(media_file), headers=user_token_headers, json={"name": name})

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Requires editor permission on this file"
    assert _links(db_session, media_file) == []
    assert db_session.query(Tag).filter(Tag.name == name).first() is None


def test_a_viewer_share_cannot_detach(
    client, db_session, normal_user, other_user, user_token_headers
):
    media_file = make_file(db_session, other_user, None)
    tag = make_tag(db_session, name=_word("vd"), user_id=other_user.id, org_id=None)
    _link(db_session, media_file, tag)
    _share(db_session, media_file, other_user, normal_user, "viewer")

    response = client.delete(f"{_attach_url(media_file)}/{tag.name}", headers=user_token_headers)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Requires editor permission on this file"
    assert len(_links(db_session, media_file)) == 1


def test_an_editor_share_can_attach_and_detach(
    client, db_session, normal_user, other_user, user_token_headers
):
    media_file = make_file(db_session, other_user, None)
    _share(db_session, media_file, other_user, normal_user, "editor")
    name = _word("editor")

    attached = client.post(_attach_url(media_file), headers=user_token_headers, json={"name": name})
    detached = client.delete(f"{_attach_url(media_file)}/{name}", headers=user_token_headers)

    assert attached.status_code == 200, attached.text
    assert detached.status_code == 204, detached.text
    assert _links(db_session, media_file) == []


# --- 2. a support grant never rewrites tags -------------------------------------------------


def test_attach_under_a_write_grant_is_refused(support_world):
    w = support_world
    headers = w.with_grant(w.active_grant(level="write"))
    name = _word("sa-attach")

    response = w.client.post(_attach_url(w.file), headers=headers, json={"name": name})

    _refused_by_grant(response)
    assert _links(w.db, w.file) == []
    assert w.db.query(Tag).filter(Tag.name == name).first() is None


def test_detach_under_a_write_grant_is_refused(support_world):
    w = support_world
    tag = make_tag(w.db, name=_word("sa-detach"), user_id=w.owner.id, org_id=w.org.id)
    _link(w.db, w.file, tag)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.delete(f"{_attach_url(w.file)}/{tag.name}", headers=headers)

    _refused_by_grant(response)
    assert len(_links(w.db, w.file)) == 1


def test_rename_under_a_write_grant_is_refused(support_world):
    w = support_world
    tag = make_tag(w.db, name=_word("sa-rename"), user_id=w.admin.id, org_id=w.org.id)
    original = tag.name
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.patch(f"/api/tags/{tag.uuid}", headers=headers, json={"name": _word()})

    _refused_by_grant(response)
    w.db.expire_all()
    assert w.db.get(Tag, tag.id).name == original


def test_merge_under_a_write_grant_is_refused(support_world):
    w = support_world
    survivor = make_tag(w.db, name=_word("sa-keep"), user_id=w.admin.id, org_id=w.org.id)
    doomed = make_tag(w.db, name=_word("sa-gone"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.post(
        f"/api/tags/{survivor.uuid}/merge",
        headers=headers,
        json={"source_uuids": [str(doomed.uuid)]},
    )

    _refused_by_grant(response)
    assert _exists(w.db, survivor)
    assert _exists(w.db, doomed)


def test_delete_under_a_write_grant_is_refused(support_world):
    w = support_world
    tag = make_tag(w.db, name=_word("sa-delete"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.delete("/api/tags", headers=headers, params={"tag_uuids": [str(tag.uuid)]})

    _refused_by_grant(response)
    assert _exists(w.db, tag)


def test_promote_under_a_write_grant_is_refused(support_world):
    w = support_world
    tag = make_tag(w.db, name=_word("sa-promote"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.post(
        "/api/tags/promote", headers=headers, json={"tag_uuids": [str(tag.uuid)]}
    )

    _refused_by_grant(response)
    w.db.expire_all()
    assert w.db.get(Tag, tag.id).organization_id == w.org.id


def test_cleanup_under_a_write_grant_is_refused(support_world):
    w = support_world
    unused = make_tag(w.db, name=_word("sa-cleanup"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.delete("/api/tags/cleanup", headers=headers)

    _refused_by_grant(response)
    assert _exists(w.db, unused)


def test_tag_reads_under_a_write_grant_are_still_answered(support_world):
    """Control: only mutations are refused."""
    w = support_world
    headers = w.with_grant(w.active_grant(level="write"))

    listed = w.client.get("/api/tags", headers=headers)
    on_files = w.client.get(
        "/api/tags/for-files", headers=headers, params={"file_uuids": [str(w.file.uuid)]}
    )

    assert listed.status_code == 200, listed.text
    assert on_files.status_code == 200, on_files.text


def test_the_file_owner_can_still_attach_and_detach_without_a_grant(support_world):
    """Control: the refusal is about the grant, not the route."""
    w = support_world
    w.roles[w.owner.id] = (w.org.id, "org:member")
    name = _word("owner")

    attached = w.client.post(_attach_url(w.file), headers=w.owner_headers, json={"name": name})
    detached = w.client.delete(f"{_attach_url(w.file)}/{name}", headers=w.owner_headers)

    assert attached.status_code == 200, attached.text
    assert detached.status_code == 204, detached.text


# --- 3. no existence oracle -----------------------------------------------------------------


def _unknown_file_answer(client, headers):
    return client.post(f"/api/tags/files/{uuid.uuid4()}/tags", headers=headers, json={"name": "x"})


def test_attach_to_a_strangers_file_reads_like_an_unknown_uuid(
    client, db_session, other_user, user_token_headers
):
    media_file = make_file(db_session, other_user, None)

    response = client.post(_attach_url(media_file), headers=user_token_headers, json={"name": "x"})
    unknown = _unknown_file_answer(client, user_token_headers)

    assert response.status_code == unknown.status_code == 404, response.text
    assert response.json() == unknown.json() == {"detail": "File not found"}
    assert _links(db_session, media_file) == []


def test_detach_from_a_strangers_file_reads_like_an_unknown_uuid(
    client, db_session, other_user, user_token_headers
):
    media_file = make_file(db_session, other_user, None)
    tag = make_tag(db_session, name=_word("sd"), user_id=other_user.id, org_id=None)
    _link(db_session, media_file, tag)

    response = client.delete(f"{_attach_url(media_file)}/{tag.name}", headers=user_token_headers)
    unknown = client.delete(
        f"/api/tags/files/{uuid.uuid4()}/tags/{tag.name}", headers=user_token_headers
    )

    assert response.status_code == unknown.status_code == 404, response.text
    assert response.json() == unknown.json() == {"detail": "File not found"}
    assert len(_links(db_session, media_file)) == 1


def test_attach_to_another_tenants_file_reads_like_an_unknown_uuid(support_world):
    w = support_world  # the owner sits in no organization, the file in ``other_org``

    response = w.client.post(
        _attach_url(w.other_org_file), headers=w.owner_headers, json={"name": "x"}
    )
    unknown = _unknown_file_answer(w.client, w.owner_headers)

    assert response.status_code == unknown.status_code == 404, response.text
    assert response.json() == unknown.json() == {"detail": "File not found"}
    assert _links(w.db, w.other_org_file) == []


def test_for_files_reads_a_strangers_file_like_an_unknown_uuid(
    client, db_session, other_user, user_token_headers
):
    media_file = make_file(db_session, other_user, None)

    response = client.get(
        "/api/tags/for-files",
        headers=user_token_headers,
        params={"file_uuids": [str(media_file.uuid)]},
    )
    unknown = client.get(
        "/api/tags/for-files",
        headers=user_token_headers,
        params={"file_uuids": [str(uuid.uuid4())]},
    )

    assert response.status_code == unknown.status_code == 404, response.text
    assert response.json() == unknown.json() == {"detail": "File not found"}


def test_for_files_refuses_the_whole_selection_when_one_file_is_out_of_reach(
    client, db_session, normal_user, other_user, user_token_headers
):
    mine = make_file(db_session, normal_user, None)
    theirs = make_file(db_session, other_user, None)

    response = client.get(
        "/api/tags/for-files",
        headers=user_token_headers,
        params={"file_uuids": [str(mine.uuid), str(theirs.uuid)]},
    )

    assert response.status_code == 404, response.text


def test_for_files_still_answers_for_own_and_shared_files(
    client, db_session, normal_user, other_user, user_token_headers
):
    mine = make_file(db_session, normal_user, None)
    shared = make_file(db_session, other_user, None)
    _share(db_session, shared, other_user, normal_user, "viewer")
    tag = make_tag(db_session, name=_word("fof"), user_id=normal_user.id, org_id=None)
    _link(db_session, mine, tag)

    response = client.get(
        "/api/tags/for-files",
        headers=user_token_headers,
        params={"file_uuids": [str(mine.uuid), str(shared.uuid)]},
    )

    assert response.status_code == 200, response.text
    assert {row["name"] for row in response.json()} == {tag.name}


def test_a_share_below_the_required_level_stays_403_not_404(
    client, db_session, normal_user, other_user, user_token_headers
):
    """That caller can already see the file, so concealing it would only mislead."""
    media_file = make_file(db_session, other_user, None)
    _share(db_session, media_file, other_user, normal_user, "viewer")

    response = client.post(_attach_url(media_file), headers=user_token_headers, json={"name": "x"})

    assert response.status_code == 403, response.text


# --- the helper's opt-in flag, directly -----------------------------------------------------


@pytest.mark.parametrize("conceal, status_code", [(False, 403), (True, 404)])
def test_conceal_inaccessible_only_changes_the_no_access_answer(
    db_session, normal_user, other_user, conceal, status_code
):
    from fastapi import HTTPException

    media_file = make_file(db_session, other_user, None)

    with pytest.raises(HTTPException) as caught:
        get_file_by_uuid_with_permission(
            db_session, media_file.uuid, normal_user.id, conceal_inaccessible=conceal
        )

    assert caught.value.status_code == status_code


def test_the_helper_does_not_conceal_by_default_for_an_out_of_scope_file(db_session, other_user):
    from fastapi import HTTPException

    media_file = make_file(db_session, other_user, None)

    with pytest.raises(HTTPException) as caught:
        get_file_by_uuid_with_permission(
            db_session, media_file.uuid, other_user.id + 10_000, organization_id=987654
        )

    assert caught.value.status_code == 403
