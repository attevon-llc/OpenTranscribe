"""Tag mutations are refused under a support-access grant (D12).

Every tag mutation carries ``refuse_under_support_grant``, as ``POST /api/tags`` always did,
so a *write* grant is answered ``support_grant_action_not_permitted`` and nothing changes. A
*read* grant is still turned away earlier by the HTTP-method check
(``support_grant_write_required``). Reads under a grant are untouched.
"""

from __future__ import annotations

from fixtures.tag_world import attach
from fixtures.tag_world import make_tag
from fixtures.tag_world import word

from app.models.tag import FileTag
from app.models.tag import Tag


def _code(response) -> str | None:
    body = response.json()
    detail = body.get("detail")
    code: str | None = detail.get("code") if isinstance(detail, dict) else body.get("code")
    return code


def _exists(db, tag: Tag) -> bool:
    db.expire_all()
    return db.query(Tag).filter(Tag.id == tag.id).first() is not None


def _refused(response) -> None:
    assert response.status_code == 403, response.text
    assert _code(response) == "support_grant_action_not_permitted"


def test_create_tag_is_refused_under_a_write_grant(support_world):
    w = support_world
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.post("/api/tags", headers=headers, json={"name": word("create")})

    assert response.status_code == 403
    assert _code(response) == "support_grant_action_not_permitted"


def test_attach_under_a_write_grant_is_refused(support_world):
    w = support_world
    headers = w.with_grant(w.active_grant(level="write"))
    name = word("sa-attach")

    response = w.client.post(
        f"/api/tags/files/{w.file.uuid}/tags", headers=headers, json={"name": name}
    )

    _refused(response)
    assert w.db.query(FileTag).filter(FileTag.media_file_id == w.file.id).count() == 0
    assert w.db.query(Tag).filter(Tag.name == name).first() is None


def test_detach_under_a_write_grant_is_refused(support_world):
    w = support_world
    tag = make_tag(w.db, word("sa-detach"), user_id=w.owner.id, org_id=w.org.id)
    attach(w.db, w.file, tag)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.delete(f"/api/tags/files/{w.file.uuid}/tags/{tag.name}", headers=headers)

    _refused(response)
    assert w.db.query(FileTag).filter(FileTag.media_file_id == w.file.id).count() == 1


def test_the_file_owner_can_still_attach_and_detach_without_a_grant(support_world):
    """Control: the refusal is about the grant, not the route."""
    w = support_world
    w.roles[w.owner.id] = (w.org.id, "org:member")
    name = word("sa-owner")

    attached = w.client.post(
        f"/api/tags/files/{w.file.uuid}/tags", headers=w.owner_headers, json={"name": name}
    )
    detached = w.client.delete(
        f"/api/tags/files/{w.file.uuid}/tags/{name}", headers=w.owner_headers
    )

    assert attached.status_code == 200, attached.text
    assert detached.status_code == 204, detached.text


def test_attach_under_a_read_grant_is_refused_by_the_method_check(support_world):
    w = support_world
    headers = w.with_grant(w.active_grant(level="read"))

    response = w.client.post(
        f"/api/tags/files/{w.file.uuid}/tags", headers=headers, json={"name": word("ro")}
    )

    assert response.status_code == 403
    assert _code(response) == "support_grant_write_required"


def test_rename_under_a_write_grant_is_refused(support_world):
    w = support_world
    tag = make_tag(w.db, word("sa-rename"), user_id=w.admin.id, org_id=w.org.id)
    original = tag.name
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.patch(
        f"/api/tags/{tag.uuid}", headers=headers, json={"name": word("sa-renamed")}
    )

    _refused(response)
    w.db.expire_all()
    assert w.db.get(Tag, tag.id).name == original


def test_delete_under_a_write_grant_is_refused(support_world):
    w = support_world
    tag = make_tag(w.db, word("sa-delete"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.delete("/api/tags", headers=headers, params={"tag_uuids": [str(tag.uuid)]})

    _refused(response)
    assert _exists(w.db, tag)


def test_merge_under_a_write_grant_is_refused(support_world):
    w = support_world
    survivor = make_tag(w.db, word("sa-survivor"), user_id=w.admin.id, org_id=w.org.id)
    doomed = make_tag(w.db, word("sa-doomed"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.post(
        f"/api/tags/{survivor.uuid}/merge",
        headers=headers,
        json={"source_uuids": [str(doomed.uuid)]},
    )

    _refused(response)
    assert _exists(w.db, survivor)
    assert _exists(w.db, doomed)


def test_promote_under_a_write_grant_is_refused(support_world):
    w = support_world
    tag = make_tag(w.db, word("sa-promote"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.post(
        "/api/tags/promote", headers=headers, json={"tag_uuids": [str(tag.uuid)]}
    )

    _refused(response)
    w.db.expire_all()
    assert w.db.get(Tag, tag.id).organization_id == w.org.id


def test_cleanup_under_a_write_grant_is_refused(support_world):
    w = support_world
    unused = make_tag(w.db, word("sa-cleanup"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.delete("/api/tags/cleanup", headers=headers)

    _refused(response)
    assert _exists(w.db, unused)


def test_tag_reads_under_a_grant_are_still_answered(support_world):
    """Control: only mutations are refused; listing and impact still work under a grant."""
    w = support_world
    tag = make_tag(w.db, word("sa-read"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))

    listed = w.client.get("/api/tags", headers=headers)
    impact = w.client.get(
        "/api/tags/impact", headers=headers, params={"tag_uuids": [str(tag.uuid)]}
    )

    assert listed.status_code == 200, listed.text
    assert str(tag.uuid) in {row["uuid"] for row in listed.json()}
    assert impact.status_code == 200, impact.text


def test_rename_under_a_read_grant_is_refused_by_the_method_check(support_world):
    w = support_world
    tag = make_tag(w.db, word("sa-ro-rename"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="read"))

    response = w.client.patch(f"/api/tags/{tag.uuid}", headers=headers, json={"name": word("x")})

    assert response.status_code == 403
    assert _code(response) == "support_grant_write_required"
