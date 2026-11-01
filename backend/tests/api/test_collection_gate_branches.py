"""Collection route branches that no other suite pinned (repo cleanup b11).

The ``media_collections`` split ran the same weakening mutations against the old module and the
split modules; each test below kills a mutant that survived the existing suites on both. Every
test keeps a control, so a pass cannot mean "nothing is listed to anybody".
"""

from __future__ import annotations

from app.models.prompt import SummaryPrompt


def _names(client, path: str = "/api/collections", **params) -> list[str]:
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return [row["name"] for row in response.json()]


def _prompt(gov, owner) -> SummaryPrompt:
    prompt = SummaryPrompt(name="gate prompt", prompt_text="Summarize.", user_id=owner.id)
    gov.db.add(prompt)
    gov.db.commit()
    gov.db.refresh(prompt)
    return prompt


def test_ownership_all_pages_only_the_request_tenant(client, gov):
    """The page is cut from the tenant's ids, not from every accessible id (then filtered)."""
    personal = gov.collection(gov.alice, org=None)
    org_coll = gov.collection(gov.alice)
    with gov.acting_as(gov.alice):
        assert _names(client, ownership="all", limit=1) == [org_coll.name]
    with gov.acting_as(gov.alice, org=None):
        assert _names(client, ownership="all", limit=1) == [personal.name]


def test_a_personal_share_is_listed_as_shared_never_as_mine(client, gov):
    shared = gov.collection(gov.alice, org=None)
    gov.share(shared, gov.bob)
    with gov.acting_as(gov.bob, org=None):
        assert shared.name not in _names(client, ownership="mine")
        assert shared.name in _names(client, ownership="shared")
        assert shared.name in _names(client, ownership="all")


def test_an_org_collection_shared_with_a_member_is_theirs_not_shared(client, gov):
    org_coll = gov.collection(gov.alice)
    gov.share(org_coll, gov.bob)
    with gov.acting_as(gov.bob):
        assert org_coll.name not in _names(client, "/api/collections/shared-with-me")
        assert org_coll.name not in _names(client, ownership="shared")
        assert org_coll.name in _names(client, ownership="mine")


def test_shared_with_me_count_hides_a_quarantined_member(client, gov):
    shared = gov.collection(gov.alice, org=None)
    gov.put(
        shared, gov.file(gov.alice, org=None), gov.file(gov.alice, org=None, is_quarantined=True)
    )
    gov.share(shared, gov.bob)
    with gov.acting_as(gov.bob, org=None):
        rows = client.get("/api/collections/shared-with-me").json()
    assert {r["name"]: r["media_count"] for r in rows} == {shared.name: 1}


def test_shared_by_is_the_share_made_to_the_caller(client, gov):
    shared = gov.collection(gov.alice, org=None)
    gov.share(shared, gov.outsider, by=gov.outsider)
    gov.share(shared, gov.bob, by=gov.alice)
    with gov.acting_as(gov.bob, org=None):
        for path, params in (
            ("/api/collections/shared-with-me", {}),
            ("/api/collections", {"ownership": "shared"}),
        ):
            rows = client.get(path, params=params).json()
            assert [r["shared_by"]["email"] for r in rows] == [gov.alice.email], path


def test_for_files_names_only_the_callers_own_collections(client, gov):
    media_file = gov.file(gov.alice, org=None)
    shared = gov.collection(gov.alice, org=None)
    gov.put(shared, media_file)
    gov.share(shared, gov.bob)
    params = {"file_uuids": [str(media_file.uuid)]}
    with gov.acting_as(gov.bob, org=None):
        assert _names(client, "/api/collections/for-files", **params) == []
    with gov.acting_as(gov.alice, org=None):
        assert _names(client, "/api/collections/for-files", **params) == [shared.name]


def test_collection_detail_hides_a_quarantined_member(client, gov):
    collection = gov.collection(gov.alice, org=None)
    visible = gov.file(gov.alice, org=None)
    gov.put(collection, visible, gov.file(gov.alice, org=None, is_quarantined=True))
    with gov.acting_as(gov.alice, org=None):
        body = client.get(f"/api/collections/{collection.uuid}").json()
    assert [f["uuid"] for f in body["media_files"]] == [str(visible.uuid)]


def test_rename_may_reuse_a_name_held_in_another_tenant(client, gov):
    personal = gov.collection(gov.alice, org=None)
    org_coll = gov.collection(gov.alice)
    taken = gov.collection(gov.alice)
    with gov.acting_as(gov.alice):
        url = f"/api/collections/{org_coll.uuid}"
        assert client.put(url, json={"name": personal.name}).status_code == 200
        assert client.put(url, json={"name": taken.name}).status_code == 400


def test_deleting_a_collection_reindexes_its_members(client, gov, monkeypatch):
    from app.api.endpoints import media_collections

    sent: list[list[int]] = []
    monkeypatch.setattr(
        media_collections.update_file_access_index, "delay", lambda ids: sent.append(list(ids))
    )
    full, empty = gov.collection(gov.alice), gov.collection(gov.alice)
    media_file = gov.file(gov.alice)
    gov.put(full, media_file)
    with gov.acting_as(gov.alice):
        assert client.delete(f"/api/collections/{empty.uuid}").status_code == 200
        assert client.delete(f"/api/collections/{full.uuid}").status_code == 200
    assert sent == [[media_file.id]]


def test_personal_collection_refuses_the_callers_own_org_file(client, gov):
    collection = gov.collection(gov.alice, org=None)
    org_file, own_file = gov.file(gov.alice), gov.file(gov.alice, org=None)
    url = f"/api/collections/{collection.uuid}/media"
    with gov.acting_as(gov.alice, org=None):
        refused = client.post(url, json={"media_file_ids": [str(org_file.uuid)]})
        assert refused.status_code == 404, refused.text
        assert client.post(url, json={"media_file_ids": [str(own_file.uuid)]}).status_code == 200


def test_a_collection_default_prompt_must_be_the_callers(client, gov):
    theirs, mine = _prompt(gov, gov.bob), _prompt(gov, gov.alice)
    with gov.acting_as(gov.alice):
        refused = client.post(
            "/api/collections", json={"name": "p refused", "default_prompt_id": str(theirs.uuid)}
        )
        assert refused.status_code == 404, refused.text
        made = client.post(
            "/api/collections", json={"name": "p made", "default_prompt_id": str(mine.uuid)}
        )
    assert made.status_code == 200, made.text
    assert made.json()["default_prompt_id"] == str(mine.uuid)
