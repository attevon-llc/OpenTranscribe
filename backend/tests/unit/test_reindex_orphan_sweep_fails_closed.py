"""The post-reindex orphan sweep must refuse an incomplete tally.

`_cleanup_orphaned_chunks` deletes every file in the chunks index that is not in
"the set of files this run indexed". That set lives in Redis and is accumulated by
the batch workers, so an *incomplete* set is indistinguishable, at the point of
deletion, from a corpus that really is mostly orphaned.

It is not hypothetical. Measured on an isolated stack while a 432-file reindex was
running: the API process restarted (a source edit under `uvicorn --reload`), its
startup sweep cleared `reindex_lock:*` / `reindex_state:*` / `reindex_uuids:*`,
`search_index_maintenance` saw no lock and dispatched a second coordinator, that
coordinator rewrote the shared state with `worker_count=1`, the in-flight batch
workers incremented into it, and completion fired holding 22 of 432 uuids. The
sweep then issued a `delete_by_query` covering **195,930 documents**. The corpus
went from 432 files / 208,333 chunks to 252 files / 111,097 chunks.

Two fixes, one test file: the API no longer clears a live reindex's coordination
state (`test_the_api_startup_sweep_leaves_reindex_coordination_alone`), and the
sweep declines when the tally does not add up. The second is the load-bearing one —
the first only removes the trigger that was found.

Skipping the sweep leaves stale documents, which the next full reindex removes.
Running it on a bad tally deletes the index, which nothing recovers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

MAIN_PY = Path(__file__).resolve().parents[3] / "backend" / "app" / "main.py"


class _FakeRedis:
    """Just enough Redis for the completion handler's reads.

    The state hash is typed loosely on purpose: a real client with
    ``decode_responses`` off hands back **bytes** keys and values, and reading
    that hash as if it were empty is one of the ways the sweep deletes an index.
    """

    def __init__(self, state: dict[Any, Any], uuids: set[str]) -> None:
        self._state: dict[Any, Any] = dict(state)
        self._uuids = set(uuids)
        self.deleted: list[tuple] = []

    def hgetall(self, key: str) -> dict[Any, Any]:  # noqa: ARG002
        return dict(self._state)

    def smembers(self, key: str) -> set[str]:  # noqa: ARG002
        return set(self._uuids)

    def delete(self, *keys: str) -> int:
        self.deleted.append(keys)
        return len(keys)


@pytest.fixture
def swept(monkeypatch) -> list[set[str]]:
    """Record every call the completion handler makes to the destructive sweep."""
    from app.tasks import reindex_task

    calls: list[set[str]] = []

    def _record(user_id: int, indexed_file_uuids: set[str]) -> int:  # noqa: ARG001
        calls.append(set(indexed_file_uuids))
        return 0

    monkeypatch.setattr(reindex_task, "_cleanup_orphaned_chunks", _record)
    # Everything else the handler does reaches OpenSearch, Redis or a websocket.
    for name in (
        "_restore_normal_mode",
        "_refresh_index_and_clear_cache",
        "_force_merge_after_reindex",
    ):
        monkeypatch.setattr(reindex_task, name, lambda: None)
    monkeypatch.setattr(reindex_task, "send_ws_event", lambda *a, **k: None)
    return calls


def _complete(redis: Any) -> None:
    from app.tasks.reindex_task import _handle_reindex_completion

    _handle_reindex_completion(redis, 1, "reindex_state:1", "reindex_uuids:1")


def test_a_complete_full_reindex_still_sweeps(swept) -> None:
    """The control. Without it every assertion below passes on a dead sweep."""
    uuids = {f"uuid-{i}" for i in range(432)}
    _complete(_FakeRedis({"partial": "0", "total": "432", "indexed": "432"}, uuids))

    assert swept == [uuids], "the sweep must still run when the run really finished"


def test_a_reset_coordination_state_does_not_sweep(swept) -> None:
    """The measured incident: 22 of 432 uuids survived, 195,930 docs were targeted."""
    _complete(_FakeRedis({"partial": "0", "total": "432"}, {f"uuid-{i}" for i in range(22)}))

    assert swept == [], (
        "the sweep ran with 22 of 432 files accounted for — that deletes the other 410"
    )


def test_an_empty_tally_does_not_sweep(swept) -> None:
    """Zero indexed files makes every document in the index look orphaned."""
    _complete(_FakeRedis({"partial": "0", "total": "432"}, set()))

    assert swept == []


def test_failed_files_count_towards_the_tally(swept) -> None:
    """A file that failed was *reached*; it is not evidence of a truncated run."""
    uuids = {f"uuid-{i}" for i in range(430)}
    _complete(_FakeRedis({"partial": "0", "total": "432", "failed": "2"}, uuids))

    assert swept == [uuids]


def test_a_partial_reindex_never_sweeps(swept) -> None:
    _complete(_FakeRedis({"partial": "1", "total": "3"}, {"uuid-0", "uuid-1", "uuid-2"}))

    assert swept == []


def test_byte_keyed_redis_state_reads_the_same(swept) -> None:
    """`decode_responses` is not guaranteed; a bytes hash must not read as absent.

    A `partial` field read as missing defaults to "0" — i.e. a partial reindex
    would sweep. That is the same deletion by a different route.
    """
    redis = _FakeRedis({}, {"uuid-0"})
    redis._state = {b"partial": b"1", b"total": b"3"}
    _complete(redis)

    assert swept == []


class _FakeOpenSearch:
    """Answers the sweep's aggregation, and records any delete it issues."""

    def __init__(self, indexed: set[str]) -> None:
        self._indexed = set(indexed)
        self.deletes: list[dict[str, Any]] = []
        self.indices = self

    def exists(self, *, index: str) -> bool:  # noqa: ARG002
        return True

    def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return {
            "aggregations": {
                "file_uuids": {"buckets": [{"key": key} for key in sorted(self._indexed)]}
            }
        }

    def delete_by_query(self, *, index: str, body: dict[str, Any], **kwargs: Any) -> dict:  # noqa: ARG002
        self.deletes.append(body)
        return {"deleted": 1}


def _sweep(monkeypatch, indexed_in_index: set[str], this_run: set[str]) -> _FakeOpenSearch:
    from app.tasks import reindex_task

    client = _FakeOpenSearch(indexed_in_index)
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", client)
    reindex_task._cleanup_orphaned_chunks(1, this_run)
    return client


def test_the_ratio_guard_refuses_a_sweep_that_would_delete_most_of_the_corpus(monkeypatch) -> None:
    """The backstop the tally check cannot provide.

    The tally compares the uuid set against the coordinator's own `total`, so a
    second coordinator that resets the shared state truncates BOTH and they agree.
    This guard compares against the index, which no Redis reset can rewrite.
    """
    client = _sweep(
        monkeypatch,
        indexed_in_index={f"uuid-{i}" for i in range(432)},
        this_run={f"uuid-{i}" for i in range(14)},
    )

    assert client.deletes == [], "the sweep would have deleted 418 of 432 files on a 14-file tally"


def test_a_handful_of_real_orphans_is_still_swept(monkeypatch) -> None:
    """The control. A guard that refuses everything is a deleted feature."""
    client = _sweep(
        monkeypatch,
        indexed_in_index={f"uuid-{i}" for i in range(432)},
        this_run={f"uuid-{i}" for i in range(2, 432)},
    )

    assert len(client.deletes) == 1
    assert set(client.deletes[0]["query"]["bool"]["must"][1]["terms"]["file_uuid"]) == {
        "uuid-0",
        "uuid-1",
    }


def test_a_tiny_index_is_exempt_from_the_ratio(monkeypatch) -> None:
    """One orphan in a four-file index is 25% and is still an orphan."""
    client = _sweep(
        monkeypatch,
        indexed_in_index={"a", "b", "c", "d"},
        this_run={"a", "b", "c"},
    )

    assert len(client.deletes) == 1


def test_the_api_startup_sweep_leaves_reindex_coordination_alone() -> None:
    """The API process must not delete keys a running Celery worker owns.

    `app/main.py` runs in a container that restarts independently of the workers.
    Asserted against the source because the function is a lifespan side effect
    over a live Redis, and the property worth protecting is which *patterns* it
    names.
    """
    source = MAIN_PY.read_text(encoding="utf-8")
    for pattern in ('"reindex_lock:*"', '"reindex_state:*"', '"reindex_uuids:*"'):
        assert f"        {pattern},\n" not in source, (
            f"{pattern} is back in the startup sweep. An API restart during a reindex "
            f"then unblocks a second coordinator over the first one's state, and the "
            f"orphan sweep deletes what the first run had not yet reached."
        )
    assert '"reindex_cancel:*"' in source, (
        "a cancel request IS meaningless after a restart and should still be cleared — "
        "if this went too, the sweep list was edited without reading why"
    )
