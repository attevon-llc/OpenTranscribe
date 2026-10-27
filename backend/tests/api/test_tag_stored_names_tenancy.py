"""A tag edit rewrites the stored tag-name lists of the tag's own tenant only.

``WatchSource.tag_names`` holds names, reapplied on every poll. Renaming, merging or deleting
a tag keeps the sources of that tag's tenant in step, and must not reach a source that belongs
to any other tenant, whoever happens to use the same word.

Each test seeds one list per tenant (the tag's own personal workspace, an organization, a
colleague's personal workspace) and drives the real routes. The first assertion in each is the
allowed-case control: the tag's own source IS rewritten, so "nothing changed anywhere" cannot
pass.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.media import Tag
from app.models.watch_source import WatchSource
from app.services.tag_service import normalize_tag_name


def _source(db, *, user_id: int, org_id: int | None, names: list[str]) -> WatchSource:
    source = WatchSource(
        name=f"ws-{uuid.uuid4().hex[:8]}",
        source_type="local",
        local_path="inbox",
        user_id=user_id,
        organization_id=org_id,
        tag_names=names,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _tag(db, *, name: str, user_id: int | None, org_id: int | None) -> Tag:
    tag = Tag(
        name=name,
        normalized_name=normalize_tag_name(name),
        user_id=user_id,
        organization_id=org_id,
    )
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def _names(db, source: WatchSource) -> list[str]:
    db.expire_all()
    stored = db.get(WatchSource, source.id).tag_names
    assert isinstance(stored, list)
    return list(stored)


@pytest.fixture
def lists(support_world):
    """The owner's tag ``word`` and one source naming it per tenant."""
    w = support_world
    word = f"word{uuid.uuid4().hex[:6]}"
    tag = _tag(w.db, name=word, user_id=w.owner.id, org_id=None)
    own = _source(w.db, user_id=w.owner.id, org_id=None, names=[word, "keep"])
    in_org = _source(w.db, user_id=w.owner.id, org_id=w.org.id, names=[word, "keep"])
    in_other_org = _source(w.db, user_id=w.owner.id, org_id=w.other_org.id, names=[word, "keep"])
    colleague = _source(w.db, user_id=w.admin.id, org_id=None, names=[word, "keep"])
    return w, word, tag, own, [in_org, in_other_org, colleague]


def _untouched(db, sources, word):
    for source in sources:
        assert _names(db, source) == [word, "keep"], source.id


def test_renaming_a_tag_rewrites_only_its_own_tenants_lists(lists):
    w, word, tag, own, others = lists
    renamed = f"{word}x"
    response = w.client.patch(
        f"/api/tags/{tag.uuid}", headers=w.owner_headers, json={"name": renamed}
    )
    assert response.status_code == 200, response.text
    assert _names(w.db, own) == [renamed, "keep"]
    _untouched(w.db, others, word)


def test_deleting_a_tag_strips_only_its_own_tenants_lists(lists):
    w, word, tag, own, others = lists
    response = w.client.delete(
        "/api/tags", headers=w.owner_headers, params={"tag_uuids": [str(tag.uuid)]}
    )
    assert response.status_code == 200, response.text
    assert _names(w.db, own) == ["keep"]
    _untouched(w.db, others, word)


def test_merging_a_tag_rewrites_only_its_own_tenants_lists(lists):
    w, word, tag, own, others = lists
    survivor = _tag(w.db, name=f"{word}-survivor", user_id=w.owner.id, org_id=None)
    response = w.client.post(
        f"/api/tags/{survivor.uuid}/merge",
        headers=w.owner_headers,
        json={"source_uuids": [str(tag.uuid)]},
    )
    assert response.status_code == 200, response.text
    assert _names(w.db, own) == [survivor.name, "keep"]
    _untouched(w.db, others, word)


def test_an_organization_tag_edit_stays_inside_the_organization(support_world):
    w = support_world
    word = f"orgword{uuid.uuid4().hex[:6]}"
    tag = _tag(w.db, name=word, user_id=w.org_admin.id, org_id=w.org.id)
    in_org = _source(w.db, user_id=w.owner.id, org_id=w.org.id, names=[word])
    in_other_org = _source(w.db, user_id=w.owner.id, org_id=w.other_org.id, names=[word])
    personal = _source(w.db, user_id=w.owner.id, org_id=None, names=[word])
    response = w.client.delete(
        "/api/tags", headers=w.org_admin_headers, params={"tag_uuids": [str(tag.uuid)]}
    )
    assert response.status_code == 200, response.text
    assert _names(w.db, in_org) == []
    assert _names(w.db, in_other_org) == [word]
    assert _names(w.db, personal) == [word]
