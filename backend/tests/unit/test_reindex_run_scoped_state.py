"""Two overlapping reindex runs must not share coordination state.

`0a781227` made the post-reindex orphan sweep **fail closed** after a measured
incident: a 432-file / 208,333-chunk index was swept down to 252 files / 111,097
chunks by one backend restart. That fix stops the deletion. It did not fix the
race underneath it, and this module is that residual.

The race: `reindex_state:{user_id}` and `reindex_uuids:{user_id}` were keyed on
the user alone, so two coordinators for the same user addressed the same hash and
the same uuid set. `reindex_lock:{user_id}` normally refuses the second one — but
it carries `ex=3600`, so a reindex that outlives an hour lets the lock lapse, the
next `search_index_maintenance` tick sees no lock, and a second coordinator
starts. It then deleted the first run's state, rewrote it with its own
`worker_count`, and the first run's in-flight batch workers incremented into it.
Completion fires early, holding a fraction of the uuids actually indexed.

With the sweep failing closed that is no longer a deletion, but the reindex is
still **wrong** — it merely fails safe. Appending the coordinator's task id to
the state keys makes the two runs disjoint, so a late coordinator cannot write
into an earlier run's state at all.

Every test here is written to fail against the pre-fix shape; the two `_control`
tests fail if the fix over-corrects into refusing legitimate work.
"""

from __future__ import annotations

import builtins
import contextlib
from typing import Any

import pytest

USER_ID = 1


class _FakeRedis:
    """Enough Redis for the reindex coordination protocol, with real semantics.

    Two behaviours are modelled deliberately rather than conveniently:
    ``SET .. NX`` refuses an existing key (that is the mutual exclusion under
    test), and ``EXPIRE`` on a **missing** key does nothing and returns 0 — which
    is why the coordinator's `expire` on the not-yet-created uuid set was a no-op.
    """

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}

    def _exists(self, key: str) -> bool:
        return key in self.strings or key in self.hashes or key in self.sets

    def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.strings:
            return None
        self.strings[key] = str(value)
        if ex is not None:
            self.ttls[key] = ex
        return True

    def get(self, key: str) -> str | None:
        return self.strings.get(key)

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            for store in (self.strings, self.hashes, self.sets):
                if key in store:
                    del store[key]
                    removed += 1
            self.ttls.pop(key, None)
        return removed

    def expire(self, key: str, seconds: int) -> int:
        if not self._exists(key):
            return 0
        self.ttls[key] = seconds
        return 1

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)

    def hset(
        self,
        key: str,
        field: str | None = None,
        value: Any = None,
        mapping: dict[str, Any] | None = None,
    ) -> int:
        entry = self.hashes.setdefault(key, {})
        if mapping:
            entry.update({k: str(v) for k, v in mapping.items()})
        if field is not None:
            entry[field] = str(value)
        return 1

    def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        entry = self.hashes.setdefault(key, {})
        entry[field] = str(int(entry.get(field, 0)) + amount)
        return int(entry[field])

    def sadd(self, key: str, *values: str) -> int:
        self.sets.setdefault(key, set()).update(str(v) for v in values)
        return len(values)

    def smembers(self, key: str) -> builtins.set[str]:
        # `builtins.set`, not `set`: this class defines a `set()` method (the Redis
        # command), which shadows the builtin inside the class body, so a bare
        # `set[str]` annotation resolves to the method and is not a valid type.
        return set(self.sets.get(key, set()))


class _FakeQuery:
    def __init__(self, rows: list[tuple[int]]) -> None:
        self._rows = rows

    def filter(self, *args: Any, **kwargs: Any) -> _FakeQuery:  # noqa: ARG002
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> _FakeQuery:  # noqa: ARG002
        return self

    def all(self) -> list[tuple[int]]:
        return list(self._rows)


class _FakeSession:
    def __init__(self, file_ids: list[int]) -> None:
        self._rows = [(fid,) for fid in file_ids]

    def query(self, *args: Any, **kwargs: Any) -> _FakeQuery:  # noqa: ARG002
        return _FakeQuery(self._rows)


class _FakeTracker:
    """Stands in for ProgressTracker; the reindex protocol under test is Redis."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    @staticmethod
    def get_state(task_type: str, user_id: int) -> None:  # noqa: ARG004
        return None

    def start(self, message: str = "") -> None:
        pass

    def resume_from_state(self, state: Any) -> None:
        pass

    def complete(self, message: str = "") -> None:
        pass


@pytest.fixture
def redis(monkeypatch) -> _FakeRedis:
    """One fake Redis shared by every seam in `reindex_task`."""
    from app.tasks import reindex_task

    fake = _FakeRedis()
    monkeypatch.setattr(reindex_task, "get_redis", lambda: fake)
    monkeypatch.setattr(reindex_task, "send_ws_event", lambda *a, **k: None)
    monkeypatch.setattr("app.services.progress_tracker.ProgressTracker", _FakeTracker)
    monkeypatch.setattr(
        "app.services.progress_tracker.emit_progress_notification", lambda *a, **k: None
    )
    return fake


@pytest.fixture
def dispatched(monkeypatch, redis) -> list[list[Any]]:  # noqa: ARG001
    """Capture the ``args`` of every `reindex_batch_task` the coordinator queues."""
    from app.tasks import reindex_task

    calls: list[list[Any]] = []

    def _capture(args: list[Any], **kwargs: Any) -> None:  # noqa: ARG001
        calls.append(list(args))

    monkeypatch.setattr(reindex_task.reindex_batch_task, "apply_async", _capture)
    for name in (
        "_restore_normal_mode",
        "_set_bulk_indexing_mode",
        "_check_and_recreate_stale_index",
    ):
        monkeypatch.setattr(reindex_task, name, lambda: None)
    monkeypatch.setattr(reindex_task, "_ensure_neural_pipeline_ready", lambda: False)
    monkeypatch.setattr(
        "app.services.search.indexing_service.recreate_index_for_dimension", lambda dim: True
    )
    monkeypatch.setattr(
        "app.services.search.settings_service.get_search_embedding_dimension", lambda: 384
    )
    return calls


@pytest.fixture
def swept(monkeypatch, redis) -> list[set[str]]:  # noqa: ARG001
    """Record every call the completion handler makes to the destructive sweep."""
    from app.tasks import reindex_task

    calls: list[set[str]] = []

    def _record(user_id: int, indexed_file_uuids: set[str]) -> int:  # noqa: ARG001
        calls.append(set(indexed_file_uuids))
        return 0

    monkeypatch.setattr(reindex_task, "_cleanup_orphaned_chunks", _record)
    for name in (
        "_restore_normal_mode",
        "_refresh_index_and_clear_cache",
        "_force_merge_after_reindex",
    ):
        monkeypatch.setattr(reindex_task, name, lambda: None)
    return calls


def _run_coordinator(monkeypatch, file_ids: list[int], run_id: str | None) -> dict[str, Any]:
    """Drive the real coordinator with a chosen Celery task id."""
    from app.tasks import reindex_task
    from app.tasks.reindex_task import reindex_transcripts_task

    @contextlib.contextmanager
    def _scope():
        yield _FakeSession(file_ids)

    monkeypatch.setattr(reindex_task, "session_scope", _scope)
    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)

    if run_id is not None:
        reindex_transcripts_task.push_request(id=run_id)
    try:
        result: dict[str, Any] = reindex_transcripts_task.run(user_id=USER_ID)
        return result
    finally:
        if run_id is not None:
            reindex_transcripts_task.pop_request()


def _run_batch(monkeypatch, file_ids: list[int], *args: Any) -> dict[str, Any]:
    """Drive the real batch worker over `file_ids`, indexing 7 chunks per file."""
    from app.tasks import reindex_task

    def _page(ids: list[int]) -> list[tuple[str, dict[str, Any]]]:
        return [
            (
                f"uuid-{fid}",
                {
                    "file_id": fid,
                    "file_uuid": f"uuid-{fid}",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "A"}],
                    "title": f"file {fid}",
                    "speakers": ["A"],
                    "tags": [],
                    "upload_time": None,
                    "language": "en",
                    "content_type": "audio/wav",
                    "duration": 1.0,
                    "file_size": 10,
                    "collection_ids": [],
                    "accessible_user_ids": [USER_ID],
                    "organization_id": None,
                },
            )
            for fid in ids
        ]

    class _FakeIndexing:
        def reindex_transcript(self, **kwargs: Any) -> int:  # noqa: ARG002
            return 7

    monkeypatch.setattr(reindex_task, "_load_reindex_page", _page)
    monkeypatch.setattr(reindex_task, "_is_cancellation_requested", lambda user_id: False)  # noqa: ARG005
    monkeypatch.setattr(
        "app.services.search.indexing_service.TranscriptIndexingService", _FakeIndexing
    )
    result: dict[str, Any] = reindex_task.reindex_batch_task(file_ids, USER_ID, *args)
    return result


# --------------------------------------------------------------------------- #
# The headline: two coordinators, one user, disjoint state
# --------------------------------------------------------------------------- #


def test_two_overlapping_coordinators_do_not_share_state(monkeypatch, redis, dispatched) -> None:
    """The residual of the data-loss bug, reproduced end to end.

    Run A starts, its workers get part way, its one-hour lock lapses, and a
    second coordinator starts over the top of it. Before the fix, B's
    `_clear_stale_progress` deleted A's state hash and uuid set and B rewrote the
    hash with its own `worker_count` — so A's tally read as 4 of 432 files and
    its uuid set as empty.
    """
    _run_coordinator(monkeypatch, list(range(1, 433)), run_id="run-a")
    a_state, a_uuids = "reindex_state:1:run-a", "reindex_uuids:1:run-a"
    assert redis.hgetall(a_state) != {}, "run A never got a state hash of its own to protect"
    a_workers = int(redis.hget(a_state, "worker_count"))

    # A's batch workers get part way through their partitions.
    for i in range(1, 41):
        redis.hincrby(a_state, "indexed", 1)
        redis.sadd(a_uuids, f"uuid-{i}")

    # An hour passes under a still-running reindex: the lock expires.
    redis.delete("reindex_lock:1")
    _run_coordinator(monkeypatch, list(range(1, 433)), run_id="run-b")

    assert redis.hgetall(a_state) != {}, "run A's coordination state was deleted by run B"
    assert int(redis.hget(a_state, "total")) == 432
    assert int(redis.hget(a_state, "indexed")) == 40, "run B reset run A's progress counters"
    assert int(redis.hget(a_state, "worker_count")) == a_workers, (
        "run B rewrote run A's worker_count — A's completion then fires early"
    )
    assert redis.smembers(a_uuids) == {f"uuid-{i}" for i in range(1, 41)}, (
        "run B emptied the uuid set the orphan sweep reads as 'the files this run indexed'"
    )
    assert redis.hgetall("reindex_state:1:run-b") != {}, "run B needs its own state to be a control"


def test_a_late_coordinator_writes_only_its_own_keys(monkeypatch, redis, dispatched) -> None:
    """The general form: no run may address the user-scoped pair at all.

    Stated separately from the test above because that one checks A survives;
    this one checks nothing is written where a *pre-upgrade* run's state lives.
    """
    _run_coordinator(monkeypatch, [1, 2, 3], run_id="run-a")

    assert redis.hgetall("reindex_state:1") == {}, "the user-scoped state hash must stay unused"
    assert redis.smembers("reindex_uuids:1") == set()
    assert int(redis.hget("reindex_state:1:run-a", "total")) == 3


def test_the_coordinator_hands_every_batch_its_run_id(monkeypatch, redis, dispatched) -> None:
    """Batches re-derive the key, so the run id has to travel with the message."""
    _run_coordinator(monkeypatch, [1, 2, 3, 4, 5], run_id="run-a")

    assert dispatched, "the coordinator dispatched nothing at all"
    partitioned: list[int] = []
    for args in dispatched:
        partitioned.extend(args[0])
    assert sorted(partitioned) == [1, 2, 3, 4, 5], "every file must land in exactly one partition"
    assert [args[1:] for args in dispatched] == [[USER_ID, "run-a"]] * len(dispatched)


def test_a_coordinator_with_no_celery_request_id_still_gets_its_own_run(
    monkeypatch, redis, dispatched
) -> None:
    """A direct/eager invocation must not fall back onto the shared key shape.

    `self.request.id` is None outside a Celery request. Formatting that into the
    key produces `reindex_state:1:None` at best and the aliasing bug at worst.
    """
    _run_coordinator(monkeypatch, [1, 2], run_id=None)

    assert redis.hgetall("reindex_state:1") == {}, "no request id fell back to the shared key"
    run_scoped = [k for k in redis.hashes if k.startswith("reindex_state:1:")]
    assert len(run_scoped) == 1, f"expected exactly one run-scoped state hash, got {run_scoped}"
    assert "None" not in run_scoped[0], f"the run id is a literal None in {run_scoped[0]}"
    assert int(redis.hget(run_scoped[0], "total")) == 2


def test_the_lock_stays_user_scoped(monkeypatch, redis, dispatched) -> None:
    """The control on the fix's blast radius.

    Run-scoping the *lock* would permit the concurrency instead of preventing it,
    and `search_maintenance_task` / `speaker_embedding_consistency` both scan
    `reindex_lock:*` to detect a reindex in flight.
    """
    _run_coordinator(monkeypatch, [1, 2], run_id="run-a")

    assert redis.get("reindex_lock:1") == "run-a"
    second = _run_coordinator(monkeypatch, [1, 2], run_id="run-b")

    assert second == {"status": "skipped", "message": "Reindex already in progress"}
    assert redis.hgetall("reindex_state:1:run-b") == {}, "the refused run wrote state anyway"


# --------------------------------------------------------------------------- #
# Batch workers
# --------------------------------------------------------------------------- #


def test_a_batch_from_one_run_cannot_increment_another_runs_counters(monkeypatch, redis) -> None:
    """The mechanism by which completion fired early on a truncated tally."""
    redis.hset("reindex_state:1:run-b", mapping={"total": 432, "indexed": 0, "worker_count": 4})
    # Two workers, so this one finishing is not the last: completion stays out of it.
    redis.hset("reindex_state:1:run-a", mapping={"total": 4, "indexed": 0, "worker_count": 2})

    stats = _run_batch(monkeypatch, [1, 2], "run-a")

    assert stats["indexed"] == 2
    assert int(redis.hget("reindex_state:1:run-a", "indexed") or 0) == 2, (
        "run A's worker did not increment run A's OWN state hash"
    )
    assert redis.smembers("reindex_uuids:1:run-a") == {"uuid-1", "uuid-2"}
    assert int(redis.hget("reindex_state:1:run-b", "indexed")) == 0, (
        "run A's worker incremented run B's counter"
    )
    assert redis.smembers("reindex_uuids:1:run-b") == set()
    assert redis.hgetall("reindex_state:1") == {}, (
        "the batch worker used the shared user-scoped hash, which every run aliases onto"
    )


def test_a_legacy_two_argument_batch_message_still_coordinates(monkeypatch, redis, swept) -> None:
    """The back-compat decision, asserted rather than assumed.

    A Celery signature is a wire contract. Batch messages queued by a
    pre-upgrade coordinator arrive with `[file_ids, user_id]` and no run id, so
    `run_id` defaults instead of being required — a required third parameter
    fails those messages at dispatch and abandons their files. The default
    selects the **legacy** user-only keys, which is what that coordinator wrote.
    """
    redis.hset("reindex_state:1", mapping={"total": 4, "indexed": 0, "worker_count": 2})
    redis.set("reindex_lock:1", "pre-upgrade-coordinator")

    first = _run_batch(monkeypatch, [1, 2])

    assert first["indexed"] == 2
    assert int(redis.hget("reindex_state:1", "indexed")) == 2, (
        "a legacy message must coordinate against the pair its own coordinator wrote"
    )
    assert redis.smembers("reindex_uuids:1") == {"uuid-1", "uuid-2"}
    assert swept == [], "one of two workers finished; completion must not have fired yet"

    second = _run_batch(monkeypatch, [3, 4])

    assert second["indexed"] == 2
    assert swept == [{f"uuid-{i}" for i in range(1, 5)}], "the legacy run must still complete"
    assert redis.hgetall("reindex_state:1") == {}, "the legacy pair must still be cleaned up"
    assert redis.get("reindex_lock:1") is None, (
        "a legacy completion has no run id to check ownership with, so it releases as before"
    )


def test_the_indexed_uuid_set_gets_a_ttl(monkeypatch, redis) -> None:
    """A crashed run leaks its own key pair now, so both halves need the expiry.

    The coordinator's `expire` on the uuid set never did anything: the set does
    not exist until the first `sadd`, and Redis `EXPIRE` on a missing key is a
    no-op. The batch worker sets it at the first point the key exists.
    """
    redis.hset("reindex_state:1:run-a", mapping={"total": 4, "indexed": 0, "worker_count": 2})

    _run_batch(monkeypatch, [1, 2], "run-a")

    assert redis.ttl("reindex_uuids:1:run-a") == 86400
    assert redis.smembers("reindex_uuids:1:run-a") == {"uuid-1", "uuid-2"}


def test_the_coordinator_cannot_ttl_a_set_that_does_not_exist_yet(
    monkeypatch, redis, dispatched
) -> None:
    """The reason the TTL moved. Guards the test above from being trivially true."""
    _run_coordinator(monkeypatch, [1, 2], run_id="run-a")

    assert redis.ttl("reindex_state:1:run-a") == 86400
    assert redis.ttl("reindex_uuids:1:run-a") == -1, (
        "the uuid set carries a TTL already, so it existed at coordinator time — which "
        "would mean the batch-side expiry this documents is unnecessary, and the previous "
        "fix's comment about the sweep input's TTL was accurate after all"
    )


# --------------------------------------------------------------------------- #
# Completion
# --------------------------------------------------------------------------- #


def _complete(redis_client: Any, run_id: str | None, state: str, uuids: str) -> None:
    from app.tasks.reindex_task import _handle_reindex_completion

    _handle_reindex_completion(redis_client, USER_ID, state, uuids, run_id)


def test_an_earlier_runs_completion_does_not_release_a_later_runs_lock(redis, swept) -> None:
    """Otherwise the cascade continues: A frees B's lock and C starts over B."""
    redis.set("reindex_lock:1", "run-b")
    redis.hset("reindex_state:1:run-a", mapping={"partial": 0, "total": 2, "indexed": 2})
    redis.sadd("reindex_uuids:1:run-a", "uuid-1", "uuid-2")

    _complete(redis, "run-a", "reindex_state:1:run-a", "reindex_uuids:1:run-a")

    assert redis.get("reindex_lock:1") == "run-b", "run A released the lock run B now holds"
    assert redis.hgetall("reindex_state:1:run-a") == {}, "run A must still clean up its own keys"
    assert swept == [{"uuid-1", "uuid-2"}]


def test_a_run_that_still_holds_the_lock_releases_it(redis, swept) -> None:
    """The control. A guard that never releases the lock blocks reindexing forever."""
    redis.set("reindex_lock:1", "run-a")
    redis.hset("reindex_state:1:run-a", mapping={"partial": 0, "total": 2, "indexed": 2})
    redis.sadd("reindex_uuids:1:run-a", "uuid-1", "uuid-2")

    _complete(redis, "run-a", "reindex_state:1:run-a", "reindex_uuids:1:run-a")

    assert redis.get("reindex_lock:1") is None
    assert swept == [{"uuid-1", "uuid-2"}]


def test_a_completion_whose_state_expired_does_not_sweep(redis, swept) -> None:
    """An expired state hash reads as `total=0`, which skipped the tally check.

    The uuid set's TTL starts at the first `sadd` and the hash's at dispatch, so
    the hash is the one that goes first — leaving a partial uuid set and no total
    to measure it against. `expected and accounted < expected` is False when
    `expected` is 0, so the sweep ran on whatever fraction survived.
    """
    redis.sadd("reindex_uuids:1:run-a", *[f"uuid-{i}" for i in range(22)])

    _complete(redis, "run-a", "reindex_state:1:run-a", "reindex_uuids:1:run-a")

    assert swept == [], "swept on 22 uuids with no total to compare them against"
