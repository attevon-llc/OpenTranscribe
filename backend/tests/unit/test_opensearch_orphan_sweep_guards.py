"""Guards on the beat-scheduled OpenSearch orphan sweep (``opensearch_orphan_cleanup``).

The sweep deletes every OpenSearch document whose identifier is absent from a set
read out of PostgreSQL. With no guard on that set, a PostgreSQL restored EMPTY
makes every document an orphan — the exact shape of this project's June 2026
incident (Postgres emptied, OpenSearch intact) and the task runs four times a day.

Each test below names the deletion it prevents. All of them exercise
``_cleanup_index_by_field`` / ``run_orphan_cleanup`` directly against a stub
OpenSearch client, so nothing here touches a live index.
"""

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.tasks import opensearch_integrity_task as sweeper

_SUMMARY_INDEX = "transcript_summaries"
_TRANSCRIPT_INDEX = "transcripts"

_UUID_A = "11111111-1111-1111-1111-111111111111"
_UUID_B = "22222222-2222-2222-2222-222222222222"


def _stub_client(
    total_docs: int,
    bucket_keys: list[Any],
    doc_count: int = 1,
    count_raises: bool = False,
) -> MagicMock:
    """Build a stub OpenSearch client for one index.

    Args:
        total_docs: What ``count`` reports for the index.
        bucket_keys: Distinct identifier values the terms aggregation returns.
        doc_count: Documents behind each bucket.
        count_raises: Make ``count`` fail, as an unreachable cluster would.

    Returns:
        A ``MagicMock`` shaped like the OpenSearch client this module uses.
    """
    client = MagicMock()
    client.indices.exists.return_value = True
    if count_raises:
        client.count.side_effect = RuntimeError("cluster did not answer")
    else:
        client.count.return_value = {"count": total_docs}
    client.search.return_value = {
        "aggregations": {
            "file_ids": {"buckets": [{"key": k, "doc_count": doc_count} for k in bucket_keys]}
        }
    }
    client.delete_by_query.return_value = {"deleted": len(bucket_keys) * doc_count}
    return client


def test_empty_db_set_refuses_to_wipe_the_int_keyed_summary_index():
    """Defect: an empty PostgreSQL result wiped ``transcript_summaries`` outright.

    The old key coercion read its type off ``next(iter(valid_values), None)``,
    which is ``None`` for an empty set, so it fell through to ``int(key)`` —
    succeeding for this index and marking every summary an orphan.
    """
    client = _stub_client(total_docs=500, bucket_keys=[1, 2, 3], doc_count=100)

    result = sweeper._cleanup_index_by_field(client, _SUMMARY_INDEX, "file_id", set(), int)

    assert result["refused"] == "empty_valid_set"
    assert result["deleted_docs"] == 0
    client.delete_by_query.assert_not_called()


def test_empty_db_set_refuses_the_uuid_keyed_index_without_raising():
    """Defect: the same empty set raised ``ValueError`` for UUID-keyed indices.

    ``int("11111111-…")`` was evaluated outside the guarded ``try``, so the run
    aborted mid-way — after earlier indices in the same pass had already had
    documents deleted. The refusal must be a return value, not an exception.
    """
    client = _stub_client(total_docs=2, bucket_keys=[_UUID_A, _UUID_B])

    result = sweeper._cleanup_index_by_field(client, _TRANSCRIPT_INDEX, "file_uuid", set(), str)

    assert result["refused"] == "empty_valid_set"
    client.delete_by_query.assert_not_called()


def test_ratio_guard_blocks_a_large_unforced_sweep():
    """Defect: no ratio guard — a partially lost database deleted at any scale.

    A non-empty but truncated ``valid_values`` (partial restore, a filtered query,
    a tenant migration mid-flight) sailed through the empty-set case. Deleting 30%
    of an index now requires being asked twice.
    """
    orphans = [_UUID_A, _UUID_B, "3", "4", "5", "6"]
    client = _stub_client(total_docs=1000, bucket_keys=orphans, doc_count=50)

    result = sweeper._cleanup_index_by_field(
        client, _TRANSCRIPT_INDEX, "file_uuid", {"kept-uuid"}, str
    )

    assert result["refused"] == "ratio_guard"
    assert result["orphaned_docs"] == 300
    client.delete_by_query.assert_not_called()


def test_force_overrides_the_ratio_guard():
    """The ratio guard is an explicit double opt-in, not a hard ceiling.

    Control for the test above: same inputs, ``force=True``, opposite outcome —
    so the guard cannot be satisfied by a code path that never deletes anything.
    """
    orphans = [_UUID_A, _UUID_B, "3", "4", "5", "6"]
    client = _stub_client(total_docs=1000, bucket_keys=orphans, doc_count=50)

    result = sweeper._cleanup_index_by_field(
        client, _TRANSCRIPT_INDEX, "file_uuid", {"kept-uuid"}, str, force=True
    )

    assert result["refused"] is None
    assert result["deleted_docs"] == 300


def test_routine_small_sweep_still_deletes_without_force():
    """A guard that blocks ordinary cleanup would be turned off, so it must not.

    Below :data:`_ORPHAN_DELETE_FLOOR_DOCS` the ratio is irrelevant: one genuine
    orphan in a three-document index is 33% of it.
    """
    client = _stub_client(total_docs=1000, bucket_keys=[_UUID_A], doc_count=5)

    result = sweeper._cleanup_index_by_field(client, _TRANSCRIPT_INDEX, "file_uuid", {_UUID_B}, str)

    assert result["refused"] is None
    assert result["deleted_docs"] == 5


def test_unanswered_document_count_fails_closed():
    """Defect: the total came from a suppressed ``count`` and defaulted to 0.

    A ratio computed against an unknown total is not a ratio. With the count
    unavailable the sweep must refuse rather than treat 0 as "small".
    """
    orphans = [_UUID_A, _UUID_B]
    client = _stub_client(total_docs=0, bucket_keys=orphans, doc_count=40, count_raises=True)

    result = sweeper._cleanup_index_by_field(
        client, _TRANSCRIPT_INDEX, "file_uuid", {"kept-uuid"}, str
    )

    assert result["refused"] == "ratio_guard"
    client.delete_by_query.assert_not_called()


def test_key_of_the_wrong_type_is_never_treated_as_an_orphan():
    """Defect: a non-numeric key in an int-keyed index raised out of the loop.

    The key type now comes from the index config, and a value that cannot be
    coerced to it is uncomparable — so it is left alone instead of aborting the
    run (or, with the old inference, being deleted).
    """
    client = _stub_client(total_docs=10, bucket_keys=[7, "not-an-int"], doc_count=1)

    result = sweeper._cleanup_index_by_field(client, _SUMMARY_INDEX, "file_id", {7}, int)

    assert result["orphaned_docs"] == 0
    client.delete_by_query.assert_not_called()


@pytest.fixture
def sweep_notifications():
    """Run ``run_orphan_cleanup`` against stubs and capture its notifications.

    Yields the patched ``send_ws_event`` mock. The three PostgreSQL readers are
    stubbed so no live database or index is involved.
    """
    client = _stub_client(total_docs=10, bucket_keys=[_UUID_A])
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch.object(sweeper, "_get_all_file_ids_from_db", return_value={1}),
        patch.object(sweeper, "_get_all_file_uuids_from_db", return_value={_UUID_A}),
        patch.object(sweeper, "_get_all_speaker_uuids_from_db", return_value={_UUID_A}),
        patch.object(sweeper, "send_ws_event") as ws_event,
    ):
        yield ws_event


def test_progress_events_go_to_the_requesting_admin(sweep_notifications):
    """Defect: every event was sent to a hardcoded ``user_id=1``.

    Progress and completion went to whichever account holds id 1 — not
    necessarily an admin, and never the admin who started the run, whose UI
    therefore waited forever.
    """
    sweeper.run_orphan_cleanup(dry_run=True, user_id=4242)

    recipients = {call.args[0] for call in sweep_notifications.call_args_list}
    assert recipients == {4242}


def test_scheduled_run_notifies_nobody(sweep_notifications):
    """The beat-scheduled sweep has no requester, so it must not toast a user.

    Control for the test above: the same code path with ``user_id=None`` sends
    nothing, instead of four notifications a day to user 1.
    """
    sweeper.run_orphan_cleanup(dry_run=True)

    assert sweep_notifications.call_count == 0
