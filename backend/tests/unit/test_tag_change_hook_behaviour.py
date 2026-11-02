"""Characterization of ``on_tags_changed`` best-effort behaviour.

Written before the tags backend rewrite: Redis and the broker may each fail without failing
the mutation that already committed, and the returned list is the deduplicated ids either way.
"""

from __future__ import annotations

from typing import Any

import pytest
from fixtures.tag_world import make_file

from app.services.tag_service import on_tags_changed


class _Task:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[int]] = []
        self.fail = fail

    def delay(self, file_ids):
        self.calls.append(list(file_ids))
        if self.fail:
            raise ConnectionError("broker is down")


class _Cache:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[tuple[str, int | None]] = []
        self.fail = fail

    def _record(self, name: str, uid: int | None = None) -> None:
        self.events.append((name, uid))
        if self.fail:
            raise ConnectionError("redis is down")

    def invalidate_tags_global(self):
        self._record("global")

    def invalidate_tags(self, uid):
        self._record("tags", uid)

    def invalidate_user_files(self, uid):
        self._record("files", uid)


@pytest.fixture
def task(monkeypatch):
    from app.tasks import search_file_field_tasks

    recorder = _Task()
    monkeypatch.setattr(search_file_field_tasks, "update_file_tags_index", recorder)
    return recorder


@pytest.fixture
def cache(monkeypatch):
    from app.services import redis_cache_service

    fake = _Cache()
    for attr in ("invalidate_tags_global", "invalidate_tags", "invalidate_user_files"):
        monkeypatch.setattr(redis_cache_service.redis_cache, attr, getattr(fake, attr))
    return fake


def test_ids_are_deduplicated_in_first_seen_order_and_none_is_skipped(db_session, task, cache):
    mixed: list[Any] = [30, None, 10, 30, 20, 10]
    result = on_tags_changed(db_session, mixed)

    assert result == [30, 10, 20]
    assert task.calls == [[30, 10, 20]]


def test_numeric_strings_are_coerced_to_int(db_session, task, cache):
    strings: list[Any] = ["7", 8, "7"]
    assert on_tags_changed(db_session, strings) == [7, 8]
    assert task.calls == [[7, 8]]


def test_no_files_enqueues_nothing_and_returns_an_empty_list(db_session, task, cache):
    assert on_tags_changed(db_session) == []
    assert on_tags_changed(db_session, []) == []
    only_none: list[Any] = [None]
    assert on_tags_changed(db_session, only_none) == []
    assert task.calls == []


def test_a_redis_failure_is_swallowed_and_the_reindex_is_still_enqueued(
    db_session, task, monkeypatch
):
    from app.services import redis_cache_service

    broken = _Cache(fail=True)
    for attr in ("invalidate_tags_global", "invalidate_tags", "invalidate_user_files"):
        monkeypatch.setattr(redis_cache_service.redis_cache, attr, getattr(broken, attr))

    result = on_tags_changed(db_session, [5, 6], user_id=1, system_scope=True)

    assert result == [5, 6]
    assert task.calls == [[5, 6]]
    assert broken.events, "the failing cache was attempted"


def test_an_enqueue_failure_is_swallowed_and_the_ids_are_still_returned(
    db_session, monkeypatch, cache
):
    from app.tasks import search_file_field_tasks

    failing = _Task(fail=True)
    monkeypatch.setattr(search_file_field_tasks, "update_file_tags_index", failing)

    result = on_tags_changed(db_session, [4, 4, 9], user_id=1)

    assert result == [4, 9]
    assert failing.calls == [[4, 9]]
    assert ("tags", 1) in cache.events, "cache invalidation ran before the enqueue failed"


def test_both_failing_still_returns_the_ids(db_session, monkeypatch):
    from app.services import redis_cache_service
    from app.tasks import search_file_field_tasks

    broken = _Cache(fail=True)
    for attr in ("invalidate_tags_global", "invalidate_tags", "invalidate_user_files"):
        monkeypatch.setattr(redis_cache_service.redis_cache, attr, getattr(broken, attr))
    monkeypatch.setattr(search_file_field_tasks, "update_file_tags_index", _Task(fail=True))

    assert on_tags_changed(db_session, [1, 2], user_id=3) == [1, 2]


def test_the_actor_and_every_file_owner_have_their_caches_dropped(
    db_session, normal_user, other_user, task, cache
):
    shared = make_file(db_session, other_user)

    on_tags_changed(db_session, [shared.id], user_id=normal_user.id)

    assert {e for e in cache.events if e[0] in ("tags", "files")} == {
        ("tags", normal_user.id),
        ("files", normal_user.id),
        ("tags", other_user.id),
        ("files", other_user.id),
    }
    assert ("global", None) not in cache.events


def test_system_scope_drops_the_global_tag_cache_first(db_session, normal_user, task, cache):
    on_tags_changed(db_session, user_id=normal_user.id, system_scope=True)

    assert cache.events[0] == ("global", None)
    assert ("tags", normal_user.id) in cache.events
    assert task.calls == []


def test_without_a_user_or_files_no_per_user_cache_is_touched(db_session, task, cache):
    on_tags_changed(db_session)

    assert cache.events == []
