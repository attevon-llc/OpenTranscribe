"""Deleting a file must leave no Redis coordination keys named after it.

The search-index sweep claims ``search_index_sweep:<uuid>`` and the reindex dispatcher
``transcript_reindex_debounce:<uuid>`` before queueing work for a file. Both are TTL'd, but a
file deleted inside that window kept them: ``test_account_deletion_leaves_no_residue`` found
both keys after an account delete on a stack whose beat sweep had just touched the file.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import file_cleanup_service as fcs


class _FakeRedis:
    def __init__(self, keys: set[str]) -> None:
        self.keys = set(keys)
        self.deleted: list[str] = []

    def delete(self, *names: str) -> int:
        self.deleted.extend(names)
        self.keys -= set(names)
        return len(names)


def _plan(file_uuid: str) -> dict[str, Any]:
    return {
        "file_uuid": file_uuid,
        "file_id": 1,
        "filename": "f.wav",
        "storage_path": "p",
        "thumbnail_path": None,
        "playback_path": None,
    }


@pytest.fixture
def stubbed_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fcs, "delete_file_storage_artifacts", lambda *a, **k: True)
    monkeypatch.setattr(fcs, "_cleanup_opensearch_for_file", lambda *a, **k: [])


def test_purge_deletes_the_sweep_and_debounce_keys(
    monkeypatch: pytest.MonkeyPatch, stubbed_stores: None
) -> None:
    uuid = "c1cfc796-de94-4628-bfd6-2c051dbf70e7"
    fake = _FakeRedis(
        {
            f"search_index_sweep:{uuid}",
            f"transcript_reindex_debounce:{uuid}",
            "search_index_sweep:other",
        }
    )
    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake)
    assert fcs._purge_external_copies(_plan(uuid)) == []
    assert fake.keys == {"search_index_sweep:other"}, "only this file's keys may be removed"


def test_an_unreachable_redis_does_not_fail_the_purge(
    monkeypatch: pytest.MonkeyPatch, stubbed_stores: None
) -> None:
    def boom() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.core.redis.get_redis", boom)
    # The keys expire on their own TTL; an outage must not turn a finished delete into a failure.
    assert fcs._purge_external_copies(_plan("u-1")) == []
