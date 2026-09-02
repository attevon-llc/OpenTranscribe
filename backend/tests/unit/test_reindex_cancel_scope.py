"""The cancel flag names the run it cancels (issue #691).

``POST /search/reindex`` fans out one coordinator per owner (#627) while
``POST /search/reindex/stop`` set a single ``reindex_cancel:{caller}`` key, so an
admin could start a deployment-wide re-embed and cancel only their own share of
it. The endpoint half of the fix — flagging every owner in the recorded fan-out —
is covered in ``tests/api/endpoints/test_search_admin_routes.py``. This module
covers the half that makes it *work*: the coordinator that most needs to see the
flag has not started yet.

With fewer Celery workers than owners the caller's coordinator runs while owners
B..N sit in the broker queue, and every coordinator clears the cancel flag on
entry — a flag left by an earlier run must not abort the next legitimate reindex
after its first file. A flag carrying only ``"1"`` would therefore be erased by
the very coordinator it was written for. Naming the run is what separates "I was
cancelled before I started" from "someone else's flag is lying around", and both
directions are pinned here: the first as an abort, the second as a run that
proceeds.

**Nothing here reaches Redis, Postgres or OpenSearch.** The Redis client is a
dict-backed stand-in, and the one coordinator test that is expected to *proceed*
is stopped at its first database call by a marker exception, so no file is ever
re-embedded.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.services.search import reindex_cancel

#: Raised in place of the coordinator's first DB access, so "it got past the
#: cancellation check" is observable without running a real re-index.
REACHED_FILE_SNAPSHOT = "REACHED-THE-FILE-SNAPSHOT"


class _StandInRedis:
    """The five operations the cancel flag, the fan-out record and the lock use."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self.store.pop(key, None) is not None)


@pytest.fixture
def fake_redis():
    """Point every ``get_redis()`` the cancel plane resolves at one dict.

    ``reindex_cancel`` imports ``get_redis`` inside each function body precisely
    so this single patch reaches it; ``reindex_task`` binds it at module import,
    hence the second target.
    """
    fake = _StandInRedis()
    with (
        patch("app.core.redis.get_redis", return_value=fake),
        patch("app.tasks.reindex_task.get_redis", return_value=fake),
    ):
        yield fake


# ---------------------------------------------------------------------------
# consume_pending_cancel — the one decision a coordinator makes on entry
# ---------------------------------------------------------------------------
def test_a_cancel_naming_this_run_is_reported_and_cleared(fake_redis):
    """The stop landed while this coordinator was queued: it must not index."""
    fake_redis.store["reindex_cancel:7"] = "run-A"

    assert reindex_cancel.consume_pending_cancel(7, "run-A") is True
    assert "reindex_cancel:7" not in fake_redis.store


def test_a_cancel_naming_another_run_is_cleared_and_not_honoured(fake_redis):
    """A flag from an earlier run is stale — clearing it is the pre-#691 behaviour.

    This is the direction that keeps a *second* re-index startable while the first
    one is still cancelling. Honouring any truthy flag here would let one run
    inherit another's cancellation, and the panel would look permanently broken.
    """
    fake_redis.store["reindex_cancel:7"] = "run-A"

    assert reindex_cancel.consume_pending_cancel(7, "run-B") is False
    assert "reindex_cancel:7" not in fake_redis.store


def test_the_legacy_flag_value_never_cancels_a_queued_run(fake_redis):
    """``"1"`` is what ``stop`` writes when it has no run id to name.

    It is truthy on purpose — the batch workers of an already-running coordinator
    still stop on it — but it must never equal a task id, or a maintenance-era
    flag would abort a brand new coordinator.
    """
    fake_redis.store["reindex_cancel:7"] = reindex_cancel.LEGACY_CANCEL_VALUE

    assert reindex_cancel.consume_pending_cancel(7, "run-B") is False
    assert "reindex_cancel:7" not in fake_redis.store


def test_no_pending_cancel_is_not_a_cancellation(fake_redis):
    assert reindex_cancel.consume_pending_cancel(7, "run-B") is False


def test_a_bytes_valued_flag_still_matches_its_run(fake_redis):
    """``get_redis()`` sets no ``decode_responses``, so values come back as bytes.

    Comparing a task id against ``b"run-A"`` is always False, which would silently
    reduce the fix to the per-owner behaviour it replaces.
    """
    fake_redis.store["reindex_cancel:7"] = b"run-A"  # type: ignore[assignment]

    assert reindex_cancel.consume_pending_cancel(7, "run-A") is True


# ---------------------------------------------------------------------------
# The batch worker's read — the same run-naming rule, mid-run
# ---------------------------------------------------------------------------
def test_a_batch_worker_stops_on_a_flag_naming_its_own_run(fake_redis):
    from app.tasks import reindex_task as rix

    fake_redis.store["reindex_cancel:7"] = "run-mine"

    assert rix._is_cancellation_requested(7, "run-mine") is True


def test_a_batch_worker_stops_on_the_legacy_flag(fake_redis):
    """``stop`` writes ``"1"`` for a run it has no id for — a maintenance-dispatched
    coordinator. Its workers must still stop, exactly as before #691."""
    from app.tasks import reindex_task as rix

    fake_redis.store["reindex_cancel:7"] = reindex_cancel.LEGACY_CANCEL_VALUE

    assert rix._is_cancellation_requested(7, "run-mine") is True


def test_a_batch_worker_ignores_a_flag_naming_a_finished_run(fake_redis):
    """Why the workers had to become run-aware as well, not just the coordinators.

    ``stop`` now flags several owners from a fan-out record that carries its own
    TTL. A record outliving its coordinators, plus a later stop, would write flags
    for owners whose ``search_index_maintenance`` runs had since started on their
    own schedule — cancelling a live, unrelated reindex. Before #691 ``stop``
    could only ever flag the caller, so this could not happen.
    """
    from app.tasks import reindex_task as rix

    fake_redis.store["reindex_cancel:7"] = "run-that-already-finished"

    assert rix._is_cancellation_requested(7, "run-mine") is False


def test_a_caller_with_no_run_identity_still_honours_any_flag(fake_redis):
    """The legacy batch message: a pre-upgrade coordinator queued it without a
    ``run_id``, and it must keep behaving as it did rather than becoming
    uncancellable."""
    from app.tasks import reindex_task as rix

    fake_redis.store["reindex_cancel:7"] = "run-that-already-finished"

    assert rix._is_cancellation_requested(7) is True


# ---------------------------------------------------------------------------
# The fan-out record — what `stop` enumerates
# ---------------------------------------------------------------------------
def test_the_fanout_record_round_trips_owner_ids_as_ints(fake_redis):
    """JSON object keys are strings; the owners must come back as ints.

    ``stop`` formats them straight into ``reindex_cancel:{user_id}``, so a string
    key writes ``reindex_cancel:41`` from ``"41"`` and happens to look right —
    until it is compared against an int elsewhere.
    """
    reindex_cancel.record_fanout(3, {41: "coordinator-41", 42: "coordinator-42"})

    assert reindex_cancel.read_fanout(3) == {41: "coordinator-41", 42: "coordinator-42"}


def test_an_admin_with_no_recorded_fanout_reads_an_empty_mapping(fake_redis):
    assert reindex_cancel.read_fanout(3) == {}


def test_an_unreadable_fanout_record_is_discarded_rather_than_raising(fake_redis):
    """A corrupt payload must degrade `stop` to the caller, not 500 the endpoint."""
    fake_redis.store["reindex_fanout:3"] = "{not json"

    assert reindex_cancel.read_fanout(3) == {}


def test_an_empty_dispatch_records_nothing(fake_redis):
    """No coordinators, no record — a stale record would outlive its run."""
    reindex_cancel.record_fanout(3, {})

    assert "reindex_fanout:3" not in fake_redis.store


def test_request_cancel_flags_every_owner_with_its_own_run_id(fake_redis):
    flagged = reindex_cancel.request_cancel({42: "coordinator-42", 41: "coordinator-41"})

    assert flagged == [41, 42]
    assert fake_redis.store["reindex_cancel:41"] == "coordinator-41"
    assert fake_redis.store["reindex_cancel:42"] == "coordinator-42"


def test_clearing_the_fanout_leaves_the_cancel_flags_alone(fake_redis):
    """The record is consumed by `stop`; the flags are consumed by the coordinators."""
    reindex_cancel.record_fanout(3, {41: "coordinator-41"})
    reindex_cancel.request_cancel({41: "coordinator-41"})

    reindex_cancel.clear_fanout(3)

    assert "reindex_fanout:3" not in fake_redis.store
    assert fake_redis.store["reindex_cancel:41"] == "coordinator-41"


def test_a_stored_record_carries_the_expiry_that_bounds_a_crashed_run(fake_redis):
    """Asserted on the module constant, which the endpoint and the docs both cite.

    A record with no TTL survives the coordinators it describes and a later `stop`
    then flags owners whose runs finished hours ago.
    """
    assert reindex_cancel.FANOUT_TTL_SECONDS == 3600
    assert reindex_cancel.CANCEL_TTL_SECONDS == 3600


# ---------------------------------------------------------------------------
# The coordinator itself — the queued-run abort, and its control
# ---------------------------------------------------------------------------
@contextmanager
def _busy_index_structure_lock():
    """Report the shared-index structure lock as held elsewhere.

    ⚠️ Yielding True instead would run the real ``_check_and_recreate_stale_index``
    and ``recreate_index_for_dimension`` against whatever OpenSearch this process
    is configured for — and the second of those **deletes the chunks index** when
    the stored dimension disagrees. The busy branch is a documented no-op path
    (the run still indexes its own files; the next pass re-checks the structure),
    so it exercises the coordinator without ever touching a cluster.
    """
    yield False


def _raise_at_the_file_snapshot():
    raise RuntimeError(REACHED_FILE_SNAPSHOT)


def _run_coordinator(*, user_id: int, task_id: str):
    """Run the real coordinator eagerly with every slow seam stopped short.

    The DB snapshot is replaced by a marker exception, which the coordinator's own
    handler turns into ``{"error": ...}`` — so "it proceeded" and "it aborted" are
    two different return values from one unmodified code path.
    """
    from app.tasks import reindex_task as rix

    # ``session_scope`` is patched at its DEFINING module, not on ``rix``: the
    # coordinator re-imports it in its own body, so a module-attribute patch is
    # shadowed and the real query runs against whatever database is configured.
    with (
        patch.object(rix, "_index_structure_lock", _busy_index_structure_lock),
        patch.object(rix, "_ensure_neural_pipeline_ready", return_value=False),
        patch.object(rix, "_restore_normal_mode"),
        patch.object(rix, "_clear_stale_progress"),
        patch("app.db.session_utils.session_scope", _raise_at_the_file_snapshot),
    ):
        return rix.reindex_transcripts_task.apply(
            kwargs={"user_id": user_id}, task_id=task_id
        ).result


def test_a_coordinator_cancelled_while_queued_aborts_and_releases_its_lock(fake_redis):
    """The case the per-owner flag could not express.

    ``stop`` writes ``reindex_cancel:{owner} = <that owner's coordinator id>``
    while the coordinator is still in the broker queue. Before #691 the value was
    ``"1"``, the coordinator cleared it on entry and re-embedded the owner's whole
    account anyway — which on a single-worker deployment is every owner but the
    caller.
    """
    fake_redis.store["reindex_cancel:7"] = "run-queued"

    result = _run_coordinator(user_id=7, task_id="run-queued")

    assert result["status"] == "cancelled"
    assert "reindex_cancel:7" not in fake_redis.store
    assert "reindex_lock:7" not in fake_redis.store


def test_a_coordinator_carrying_a_stale_flag_still_runs(fake_redis):
    """The control: without it, "abort on any flag" would pass the test above.

    A flag naming some other run is the residue of a previous reindex. Aborting on
    it would make every reindex after a cancelled one a silent no-op, which is a
    worse defect than the one being fixed.
    """
    fake_redis.store["reindex_cancel:7"] = "some-older-run"

    result = _run_coordinator(user_id=7, task_id="run-fresh")

    assert REACHED_FILE_SNAPSHOT in result["error"]
    assert "reindex_cancel:7" not in fake_redis.store


def test_an_uncancelled_coordinator_runs(fake_redis):
    """The second control: no flag at all must behave exactly as it always has."""
    result = _run_coordinator(user_id=7, task_id="run-fresh")

    assert REACHED_FILE_SNAPSHOT in result["error"]


def test_the_two_key_shapes_match_what_the_startup_sweep_expects(fake_redis):
    """``app/main.py`` sweeps ``reindex_cancel:*`` on API restart, deliberately.

    A cancel request is meaningless after a restart and clearing one can only
    cause a reindex to run, never to delete. ``reindex_fanout:*`` is deliberately
    NOT in that list — the API process restarts independently of the workers still
    executing the fan-out, and deleting the record takes ``stop``'s only handle on
    them with it. Both properties depend on these two prefixes staying distinct.
    """
    reindex_cancel.request_cancel({41: "coordinator-41"})
    reindex_cancel.record_fanout(3, {41: "coordinator-41"})

    assert reindex_cancel.CANCEL_KEY.format(user_id=41) == "reindex_cancel:41"
    assert reindex_cancel.FANOUT_KEY.format(admin_id=3) == "reindex_fanout:3"
    assert sorted(fake_redis.store) == ["reindex_cancel:41", "reindex_fanout:3"]
