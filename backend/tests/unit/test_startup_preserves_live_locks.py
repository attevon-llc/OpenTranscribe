"""API startup must not delete locks or run state that live workers hold (audit M4).

``_clear_stale_task_state`` runs in the API process, which restarts on its own while the
Celery workers keep running. It used to delete every lock and progress key it could find,
so an API restart mid-run let a second copy of a running task start and wiped the UI's
progress. Recovery from a genuinely dead holder now comes from leases that expire; these
tests pin both halves against a real Redis.
"""

from __future__ import annotations

import time

import pytest

from app.core import redis as core_redis
from app.core.config import settings
from app.startup.maintenance import _clear_stale_task_state
from app.utils import task_lock
from app.utils.redis_lease import make_lease_lock

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]

LEASE = 2


@pytest.fixture
def r(local_model_redis, monkeypatch):
    local_model_redis.flushdb()
    monkeypatch.setattr(core_redis, "get_redis", lambda: local_model_redis)
    monkeypatch.setattr(task_lock, "get_request_redis", lambda: local_model_redis)
    monkeypatch.setattr(task_lock, "task_lock_manager", task_lock.TaskLockManager())
    if "TASK_LOCK_LEASE_SECONDS" in type(settings).model_fields:
        monkeypatch.setattr(settings, "TASK_LOCK_LEASE_SECONDS", LEASE)
    yield local_model_redis
    local_model_redis.flushdb()


def test_a_restart_leaves_held_locks_and_live_run_state_intact(r):
    live_state = {
        "auto_label_lock:42": "1",
        "auto_label_progress:42": "1",
        "task_progress:clustering:42": "{}",
        "embedding_migration:status": "{}",
        "embedding_migration:lock": "1",
        "speaker_attr_migration:batch_task_ids": "[]",
        "embedding_consistency_running": "1",
    }
    for key, value in live_state.items():
        r.set(key, value, ex=3600)
    r.set("reindex_cancel:42", "1", ex=3600)

    held = ["system.health_check", "recluster_speakers_user_42", "search_index_maintenance"]
    with (
        task_lock.task_lock_manager.acquire_lock(held[0], timeout=600) as a,
        task_lock.task_lock_manager.acquire_lock(held[1], timeout=600) as b,
        task_lock.task_lock_manager.acquire_lock(held[2], timeout=600) as c,
    ):
        assert (a, b, c) == (True, True, True)
        _clear_stale_task_state()
        surviving = {key for key in [*live_state, *held] if r.exists(key)}
        assert surviving == {*live_state, *held}
        # A held lock is still the holder's: a second taker is refused.
        with task_lock.task_lock_manager.acquire_lock(held[0], timeout=600) as second:
            assert second is False

    # The one key with no owner to outlive: a cancel request from before the restart.
    assert r.exists("reindex_cancel:42") == 0


def test_a_dead_holders_lock_expires_without_startup_deleting_it(r):
    """A holder that died stops renewing; its lock frees itself within one lease."""
    dead = make_lease_lock(r, "normalize_embeddings_lock", LEASE)
    assert dead.acquire(blocking=False)  # acquired, never renewed, never released

    deadline = time.monotonic() + LEASE + 2
    while time.monotonic() < deadline:
        if not r.exists("normalize_embeddings_lock"):
            break
        time.sleep(0.1)
    assert r.exists("normalize_embeddings_lock") == 0
    with task_lock.task_lock_manager.acquire_lock("normalize_embeddings_lock", 600) as ok:
        assert ok is True


def _ttl_seen_inside(r, key: str, run) -> int:
    """Run *run* with its body stubbed to record *key*'s TTL while the lock is held."""
    seen: list[int] = []

    def body(*_args, **_kwargs):
        seen.append(int(r.ttl(key)))
        raise RuntimeError("stop here")

    run(body)
    assert seen, f"the locked body never ran for {key}"
    return seen[0]


def _run_normalize(r, monkeypatch, body):
    from app.tasks import speaker_embedding_migration as normalize

    monkeypatch.setattr(normalize, "_run_normalize_embeddings", body)
    with pytest.raises(RuntimeError):
        normalize.normalize_speaker_embeddings_task.apply(kwargs={}).get(propagate=True)


def _run_integrity(r, monkeypatch, body):
    from app.tasks import opensearch_integrity_task as integrity

    monkeypatch.setattr(integrity, "_notify", lambda *a, **k: None)
    # Bound at import time, so it needs the test Redis too.
    monkeypatch.setattr(integrity, "get_redis", lambda: r)
    monkeypatch.setattr(integrity, "run_orphan_cleanup", body)
    integrity.opensearch_orphan_cleanup_task.apply(kwargs={}).get()


def _run_combined(r, monkeypatch, body):
    from app.tasks import combined_speaker_analysis_task as combined

    monkeypatch.setattr(combined.combined_migration_progress, "is_running", lambda: False)
    monkeypatch.setattr(combined, "get_redis", lambda: r)
    monkeypatch.setattr(combined, "session_scope", body)
    combined.migrate_speakers_combined_task.apply(kwargs={"user_id": 1}).get()


def _run_attrs(r, monkeypatch, body):
    from app.tasks import speaker_attribute_migration_task as attrs

    monkeypatch.setattr(attrs.attribute_migration_progress, "is_running", lambda: False)
    monkeypatch.setattr(attrs.attribute_migration_progress, "_redis_client", r)
    monkeypatch.setattr(attrs, "session_scope", body)
    attrs.migrate_speaker_attributes_task.apply(kwargs={"user_id": 1}).get()


@pytest.mark.parametrize(
    ("key", "run"),
    [
        ("normalize_embeddings_lock", _run_normalize),  # was a fixed 600 s
        ("data_integrity_running", _run_integrity),  # was a fixed 900 s
        ("combined_speaker_migration:orchestrator_lock", _run_combined),  # was 3600 s
        ("speaker_attr_migration:orchestrator_lock", _run_attrs),  # was 3600 s
    ],
    ids=["normalize", "orphan-cleanup", "combined-orchestrator", "attribute-orchestrator"],
)
def test_formerly_fixed_ttl_locks_are_now_short_renewed_leases(r, monkeypatch, key, run):
    """Each of these had a fixed TTL that only startup's deletion could cut short."""
    ttl = _ttl_seen_inside(r, key, lambda body: run(r, monkeypatch, body))
    assert 0 < ttl <= LEASE, f"{key} held with a fixed {ttl} s TTL, not a lease"
    assert r.exists(key) == 0, "the lock outlived its run"
