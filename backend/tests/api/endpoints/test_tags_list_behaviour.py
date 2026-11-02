"""Characterization of ``GET /api/tags``, ``/unused`` and ``/collisions`` and of ``POST /api/tags``.

Written before the tags backend rewrite: ordering, the read-through cache contract, the
repair writes the two collision-aware reads commit (D4), and the ownership label on
``POST /api/tags`` (D3). Tests whose name says ``pins_current_behaviour`` pin behaviour the
owner has decided to change.
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

from app.core.config import settings
from app.models.tag import Tag


@pytest.fixture
def org_world(db_session):
    return build_org_world(db_session)


class _FakeCache:
    """Records what the endpoint asks of the read-through cache."""

    def __init__(self, stored=None) -> None:
        self.stored = stored
        self.gets: list[str] = []
        self.sets: list[tuple[str, object, int | None]] = []

    def get(self, key):
        self.gets.append(key)
        return self.stored

    def set(self, key, value, ttl=None):
        self.sets.append((key, value, ttl))
        return True


@pytest.fixture
def fake_cache(monkeypatch):
    from app.services.redis_cache_service import redis_cache

    cache = _FakeCache()
    monkeypatch.setattr(redis_cache, "get", cache.get)
    monkeypatch.setattr(redis_cache, "set", cache.set)
    monkeypatch.setattr(settings, "READ_CACHE_ENABLED", True)
    return cache


def _forbid_listing(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the listing query ran although the cache should have answered")

    monkeypatch.setattr("app.api.endpoints.tags.crud.list_tags_filtered", boom)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_list_orders_by_usage_descending_then_name(
    client, db_session, normal_user, user_token_headers
):
    stem = uuid.uuid4().hex[:6]
    heavy = make_tag(db_session, f"{stem}-m", user_id=normal_user.id)
    alpha = make_tag(db_session, f"{stem}-a", user_id=normal_user.id)
    beta = make_tag(db_session, f"{stem}-b", user_id=normal_user.id)
    unused = make_tag(db_session, f"{stem}-z", user_id=normal_user.id)
    first, second = make_file(db_session, normal_user), make_file(db_session, normal_user)
    attach(db_session, first, heavy)
    attach(db_session, second, heavy)
    attach(db_session, first, beta)
    attach(db_session, second, alpha)

    rows = client.get("/api/tags", headers=user_token_headers).json()

    mine = [(r["name"], r["usage_count"]) for r in rows if r["name"].startswith(stem)]
    assert mine == [(heavy.name, 2), (alpha.name, 1), (beta.name, 1), (unused.name, 0)]


def test_unused_endpoint_is_ordered_by_name(client, db_session, normal_user, user_token_headers):
    stem = uuid.uuid4().hex[:6]
    for suffix in ("c", "a", "b"):
        make_tag(db_session, f"{stem}-{suffix}", user_id=normal_user.id)

    rows = client.get("/api/tags/unused", headers=user_token_headers).json()

    assert [r["name"] for r in rows if r["name"].startswith(stem)] == [
        f"{stem}-a",
        f"{stem}-b",
        f"{stem}-c",
    ]


def test_list_row_shape(client, db_session, normal_user, user_token_headers):
    tag = make_tag(db_session, word("shape"), user_id=normal_user.id)

    rows = client.get("/api/tags", headers=user_token_headers).json()

    (row,) = [r for r in rows if r["uuid"] == str(tag.uuid)]
    assert set(row) == {"uuid", "name", "source", "ownership", "usage_count"}


# ---------------------------------------------------------------------------
# Read-through cache
# ---------------------------------------------------------------------------


def test_unfiltered_personal_list_is_stored_in_the_cache_as_json_dicts(
    client, db_session, normal_user, user_token_headers, fake_cache
):
    from app.services.redis_cache_service import TTL_TAGS

    tag = make_tag(db_session, word("cached"), user_id=normal_user.id)

    response = client.get("/api/tags", headers=user_token_headers)

    assert response.status_code == 200
    assert fake_cache.gets == [f"cache:tags:{normal_user.id}"]
    ((key, value, ttl),) = fake_cache.sets
    assert key == f"cache:tags:{normal_user.id}"
    assert ttl == TTL_TAGS == 300
    assert all(type(item) is dict for item in value)
    mine = [item for item in value if item["uuid"] == str(tag.uuid)]
    assert mine == [
        {
            "uuid": str(tag.uuid),
            "name": tag.name,
            "source": "manual",
            "ownership": "mine",
            "usage_count": 0,
        }
    ]
    assert value == response.json()


def test_a_cache_hit_is_served_without_querying(
    client, user_token_headers, fake_cache, monkeypatch
):
    fake_cache.stored = [
        {
            "uuid": str(uuid.uuid4()),
            "name": "from-the-cache",
            "source": None,
            "ownership": "system",
            "usage_count": 7,
        }
    ]
    _forbid_listing(monkeypatch)

    response = client.get("/api/tags", headers=user_token_headers)

    assert response.status_code == 200, response.text
    assert [(r["name"], r["usage_count"]) for r in response.json()] == [("from-the-cache", 7)]
    assert fake_cache.sets == []


def test_an_empty_cached_list_is_a_hit(client, user_token_headers, fake_cache, monkeypatch):
    fake_cache.stored = []
    _forbid_listing(monkeypatch)

    response = client.get("/api/tags", headers=user_token_headers)

    assert response.json() == []
    assert fake_cache.sets == []


@pytest.mark.parametrize(
    "query",
    [{"unused": "true"}, {"colliding": "true"}, {"scope": "mine"}, {"scope": "system"}],
    ids=["unused", "colliding", "scope-mine", "scope-system"],
)
def test_filtered_requests_never_touch_the_cache(client, user_token_headers, fake_cache, query):
    response = client.get("/api/tags", headers=user_token_headers, params=query)

    assert response.status_code == 200
    assert fake_cache.gets == []
    assert fake_cache.sets == []


def test_scope_all_spelled_out_is_still_cached(client, user_token_headers, normal_user, fake_cache):
    response = client.get("/api/tags", headers=user_token_headers, params={"scope": "all"})

    assert response.status_code == 200
    assert fake_cache.gets == [f"cache:tags:{normal_user.id}"]


def test_the_cache_is_off_when_the_flag_is_off(client, user_token_headers, fake_cache, monkeypatch):
    monkeypatch.setattr(settings, "READ_CACHE_ENABLED", False)

    assert client.get("/api/tags", headers=user_token_headers).status_code == 200

    assert fake_cache.gets == []
    assert fake_cache.sets == []


def test_an_organization_request_is_never_cached(client, org_world, fake_cache):
    w = org_world

    with acting_as(w.alice, w.org_a.id, "org:admin"):
        response = client.get("/api/tags")

    assert response.status_code == 200
    assert fake_cache.gets == []
    assert fake_cache.sets == []


def test_a_personal_request_through_the_context_override_is_cached(client, org_world, fake_cache):
    w = org_world

    with acting_as(w.alice, None):
        assert client.get("/api/tags").status_code == 200

    assert fake_cache.gets == [f"cache:tags:{w.alice.id}"]
    assert len(fake_cache.sets) == 1


# ---------------------------------------------------------------------------
# D4: GETs that write a normalization repair
# ---------------------------------------------------------------------------


def _count_commits(monkeypatch, db_session) -> list[int]:
    commits: list[int] = []
    original = db_session.commit

    def counting():
        commits.append(1)
        return original()

    monkeypatch.setattr(db_session, "commit", counting)
    return commits


def _stale_system_tag(db_session) -> Tag:
    tag = Tag(name=f"Sys Stale {uuid.uuid4().hex[:6]}", user_id=None, normalized_name="stale")
    db_session.add(tag)
    db_session.commit()
    db_session.refresh(tag)
    return tag


def test_collisions_get_repairs_a_system_tags_normalization_for_any_user_pins_current_behaviour(
    client, db_session, user_token_headers, monkeypatch
):
    """D4: a plain user's GET rewrites and commits a *system* row's stored normalization."""
    system = _stale_system_tag(db_session)
    commits = _count_commits(monkeypatch, db_session)

    response = client.get("/api/tags/collisions", headers=user_token_headers)

    assert response.status_code == 200, response.text
    db_session.refresh(system)
    assert system.normalized_name == system.name.lower()
    assert commits, "the repair is committed inside the GET"


def test_colliding_filter_also_commits_the_repair_pins_current_behaviour(
    client, db_session, user_token_headers, monkeypatch
):
    """D4: ``GET /tags?colliding=true`` goes through the same repairing pass."""
    system = _stale_system_tag(db_session)
    commits = _count_commits(monkeypatch, db_session)

    response = client.get("/api/tags", headers=user_token_headers, params={"colliding": "true"})

    assert response.status_code == 200, response.text
    db_session.refresh(system)
    assert system.normalized_name == system.name.lower()
    assert commits


def test_a_plain_list_does_not_repair_or_commit(
    client, db_session, user_token_headers, monkeypatch
):
    system = _stale_system_tag(db_session)
    commits = _count_commits(monkeypatch, db_session)

    assert client.get("/api/tags", headers=user_token_headers).status_code == 200

    db_session.refresh(system)
    assert system.normalized_name == "stale"
    assert commits == []


def test_a_collisions_call_with_nothing_to_repair_does_not_commit(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    make_tag(db_session, word("clean"), user_id=normal_user.id)
    first = client.get("/api/tags/collisions", headers=user_token_headers)
    assert first.status_code == 200
    commits = _count_commits(monkeypatch, db_session)

    second = client.get("/api/tags/collisions", headers=user_token_headers)

    assert second.json() == first.json()
    assert commits == []


# ---------------------------------------------------------------------------
# D3: ownership on POST /api/tags
# ---------------------------------------------------------------------------


def test_create_in_personal_scope_reports_mine(client, user_token_headers):
    body = client.post("/api/tags", headers=user_token_headers, json={"name": word("own")}).json()

    assert set(body) == {"uuid", "name", "source", "ownership"}
    assert body["ownership"] == "mine"


def test_create_returning_a_colleagues_org_tag_reports_mine_pins_current_behaviour(
    client, org_world
):
    """D3: the list endpoint would call this row ``shared_with_me`` for bob."""
    w = org_world
    name = word("shared-word")
    alice_tag = make_tag(w.db, name, user_id=w.alice.id, org_id=w.org_a.id)

    with acting_as(w.bob, w.org_a.id):
        created = client.post("/api/tags", json={"name": name})
        listed = {r["uuid"]: r["ownership"] for r in client.get("/api/tags").json()}

    assert created.json()["uuid"] == str(alice_tag.uuid)
    assert created.json()["ownership"] == "mine"
    assert listed[str(alice_tag.uuid)] == "shared_with_me"


def test_create_returning_an_unattributed_org_tag_reports_system_pins_current_behaviour(
    client, org_world
):
    """D3: an org tag with no creator is not system; the list says ``shared_with_me``."""
    w = org_world
    name = word("orphan")
    orphan = make_tag(w.db, name, user_id=None, org_id=w.org_a.id)

    with acting_as(w.bob, w.org_a.id):
        created = client.post("/api/tags", json={"name": name})
        listed = {r["uuid"]: r["ownership"] for r in client.get("/api/tags").json()}

    assert created.json()["uuid"] == str(orphan.uuid)
    assert created.json()["ownership"] == "system"
    assert listed[str(orphan.uuid)] == "shared_with_me"


def test_create_in_an_org_attributes_a_new_row_to_the_org_and_the_creator(client, org_world):
    w = org_world
    name = word("fresh-org")

    with acting_as(w.bob, w.org_a.id):
        created = client.post("/api/tags", json={"name": name})

    assert created.status_code == 200, created.text
    row = w.db.query(Tag).filter(Tag.name == name).one()
    assert (row.user_id, row.organization_id) == (w.bob.id, w.org_a.id)
