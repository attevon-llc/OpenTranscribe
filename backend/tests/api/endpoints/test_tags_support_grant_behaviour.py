"""Which tag mutations a support-access grant reaches today (defect D12).

Only ``POST /api/tags`` carries ``refuse_under_support_grant``. Every other tag mutation
is refused under a *read* grant by the HTTP-method check alone and allowed under a *write*
grant. Tests whose name says ``pins_current_behaviour`` pin that gap; the owner has decided
every tag mutation must be refused under a grant, so the lane that fixes it flips them red.
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


def test_create_tag_is_refused_under_a_write_grant(support_world):
    w = support_world
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.post("/api/tags", headers=headers, json={"name": word("create")})

    assert response.status_code == 403
    assert _code(response) == "support_grant_action_not_permitted"


def test_attach_under_a_write_grant_is_allowed_pins_current_behaviour(support_world):
    w = support_world
    headers = w.with_grant(w.active_grant(level="write"))
    name = word("sa-attach")

    response = w.client.post(
        f"/api/tags/files/{w.file.uuid}/tags", headers=headers, json={"name": name}
    )

    assert response.status_code == 200, response.text
    assert w.db.query(FileTag).filter(FileTag.media_file_id == w.file.id).count() == 1


def test_detach_under_a_write_grant_is_allowed_pins_current_behaviour(support_world):
    w = support_world
    tag = make_tag(w.db, word("sa-detach"), user_id=w.owner.id, org_id=w.org.id)
    attach(w.db, w.file, tag)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.delete(f"/api/tags/files/{w.file.uuid}/tags/{tag.name}", headers=headers)

    assert response.status_code == 204, response.text
    assert w.db.query(FileTag).filter(FileTag.media_file_id == w.file.id).count() == 0


def test_attach_under_a_read_grant_is_refused_by_the_method_check(support_world):
    w = support_world
    headers = w.with_grant(w.active_grant(level="read"))

    response = w.client.post(
        f"/api/tags/files/{w.file.uuid}/tags", headers=headers, json={"name": word("ro")}
    )

    assert response.status_code == 403
    assert _code(response) == "support_grant_write_required"


def test_rename_under_a_write_grant_is_allowed_pins_current_behaviour(support_world):
    w = support_world
    tag = make_tag(w.db, word("sa-rename"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))
    new_name = word("sa-renamed")

    response = w.client.patch(f"/api/tags/{tag.uuid}", headers=headers, json={"name": new_name})

    assert response.status_code == 200, response.text
    w.db.expire_all()
    assert w.db.get(Tag, tag.id).name == new_name


def test_delete_under_a_write_grant_is_allowed_pins_current_behaviour(support_world):
    w = support_world
    tag = make_tag(w.db, word("sa-delete"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.delete("/api/tags", headers=headers, params={"tag_uuids": [str(tag.uuid)]})

    assert response.status_code == 200, response.text
    assert not _exists(w.db, tag)


def test_merge_under_a_write_grant_is_allowed_pins_current_behaviour(support_world):
    w = support_world
    survivor = make_tag(w.db, word("sa-survivor"), user_id=w.admin.id, org_id=w.org.id)
    doomed = make_tag(w.db, word("sa-doomed"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="write"))

    response = w.client.post(
        f"/api/tags/{survivor.uuid}/merge",
        headers=headers,
        json={"source_uuids": [str(doomed.uuid)]},
    )

    assert response.status_code == 200, response.text
    assert _exists(w.db, survivor)
    assert not _exists(w.db, doomed)


def test_rename_under_a_read_grant_is_refused_by_the_method_check(support_world):
    w = support_world
    tag = make_tag(w.db, word("sa-ro-rename"), user_id=w.admin.id, org_id=w.org.id)
    headers = w.with_grant(w.active_grant(level="read"))

    response = w.client.patch(f"/api/tags/{tag.uuid}", headers=headers, json={"name": word("x")})

    assert response.status_code == 403
    assert _code(response) == "support_grant_write_required"
