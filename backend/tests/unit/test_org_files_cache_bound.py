"""The personal-scope voiceprint gate's per-process cache is bounded (concurrency audit L6).

``_org_files_cache`` was a plain dict keyed by every distinct set of user ids a kNN read
ever asked about, with a TTL checked only on read: entries were never evicted, so a
long-lived API process grew it without limit. It is now a ``cachetools.TTLCache`` with a
``maxsize``, under a lock (TTLCache is not thread-safe).
"""

from __future__ import annotations

import threading

import pytest

from app.services.opensearch_service import client as os_client


@pytest.fixture
def no_database(monkeypatch):
    """Answer every lookup without Postgres; count how many reached it."""
    calls: list[tuple[int, ...]] = []

    class _Query:
        def __init__(self, key):
            self._key = key

        def filter(self, *_a):
            return self

        def limit(self, _n):
            return self

        def all(self):
            calls.append(self._key)
            return []

    class _Session:
        def query(self, *_a):
            return _Query(None)

        def close(self):
            pass

    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _Session())
    os_client._org_files_cache.clear()
    yield calls
    os_client._org_files_cache.clear()


def test_the_cache_never_holds_more_than_its_maxsize(no_database):
    limit = os_client._ORG_FILES_CACHE_MAX_ENTRIES

    for user_id in range(limit * 3):
        os_client._org_file_ids_of([user_id])

    assert len(os_client._org_files_cache) <= limit


def test_a_repeat_within_the_ttl_is_served_from_the_cache(no_database):
    os_client._org_file_ids_of([1, 2])
    os_client._org_file_ids_of([2, 1])

    assert len(no_database) == 1


def test_concurrent_readers_do_not_corrupt_the_cache(no_database):
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def reader(offset: int) -> None:
        barrier.wait()
        try:
            for user_id in range(offset, offset + 500):
                os_client._org_file_ids_of([user_id])
        except BaseException as exc:  # recorded, asserted on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=reader, args=(i * 100,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert errors == []
    assert len(os_client._org_files_cache) <= os_client._ORG_FILES_CACHE_MAX_ENTRIES
