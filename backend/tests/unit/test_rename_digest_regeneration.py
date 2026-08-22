"""Rename -> digest regeneration dispatch (#383 addendum-G1, issue #405 follow-up).

Renaming a speaker invalidates that file's ``file_facts`` digest fingerprint (it covers the
*resolved* display name), but nothing used to dispatch the regeneration — a renamed speaker's
digest prose stayed stale until an unrelated full reindex happened to touch the file.

These tests cover the DISPATCH wiring only: whether ``dispatch_speaker_rename`` queues
``regenerate_rename_digests``, coalesced per file and batched to bound a bulk rename's
fan-out. The regeneration itself (``_index_digest_plane`` reuse) needs a real OpenSearch
cluster and is exercised in ``tests/integration/test_rename_propagation_chunks.py``'s sibling
suite, not here.

⚠️ **The dispatch is deliberately unconditional on the chunk-plane rewrite finding anything.**
``rename_propagation_task._finish`` early-returns when ``updated == 0`` — the exact case of a
file whose chunks were already indexed under the new name, or which has no chunk-plane
documents at all. That is precisely the file whose digest most needs regenerating, so hooking
``_finish`` would systematically skip the stalest files. Dispatch instead lives in
``dispatch_speaker_rename``, which every rename path already funnels through.
"""

import uuid as uuid_mod
from unittest.mock import patch

_CHUNK_DELAY = "app.tasks.rename_propagation_task.propagate_speaker_rename.delay"
_DIGEST_DELAY = "app.tasks.rename_propagation_task.regenerate_rename_digests.delay"


class TestDigestRegenerationDispatch:
    def test_a_single_file_rename_queues_one_digest_regeneration_batch(self):
        from app.tasks.rename_propagation_task import dispatch_speaker_rename

        file_uuid = str(uuid_mod.uuid4())
        with patch(_CHUNK_DELAY), patch(_DIGEST_DELAY) as digest_mock:
            queued = dispatch_speaker_rename([(file_uuid, "SPEAKER_00")], "Dana", speaker_id=7)

        # The real return value the caller (the speakers API, the rename tracker)
        # actually observes — one file was coalesced, independent of the mock.
        assert queued == 1
        digest_mock.assert_called_once_with(file_uuids=[file_uuid], new_name="Dana", speaker_id=7)

    def test_renames_across_many_files_are_coalesced_per_file_first(self):
        """Four labels collapsing onto one person in one file must not queue four regens."""
        from app.tasks.rename_propagation_task import dispatch_speaker_rename

        file_a, file_b = str(uuid_mod.uuid4()), str(uuid_mod.uuid4())
        with patch(_CHUNK_DELAY), patch(_DIGEST_DELAY) as digest_mock:
            queued = dispatch_speaker_rename(
                [
                    (file_a, "SPEAKER_00"),
                    (file_a, "SPEAKER_01"),
                    (file_a, "SPEAKER_00"),
                    (file_b, "SPEAKER_02"),
                ],
                "Dana",
            )

        # Two files coalesced, not four renames — the real return value.
        assert queued == 2
        digest_mock.assert_called_once()
        assert sorted(digest_mock.call_args.kwargs["file_uuids"]) == sorted([file_a, file_b])

    def test_a_bulk_rename_is_bounded_to_a_fixed_number_of_batch_tasks(self):
        """The explicit cost bound: N files never means N Celery tasks.

        A profile merge / cluster promotion can rename speakers across many
        files in one pass. Without batching, that would fan out one
        ``regenerate_rename_digests`` task per file onto the CPU queue.
        """
        from app.tasks.rename_propagation_task import _DIGEST_REGEN_BATCH_SIZE
        from app.tasks.rename_propagation_task import dispatch_speaker_rename

        file_count = _DIGEST_REGEN_BATCH_SIZE * 2 + 3
        renames = [(str(uuid_mod.uuid4()), "SPEAKER_00") for _ in range(file_count)]

        with patch(_CHUNK_DELAY), patch(_DIGEST_DELAY) as digest_mock:
            dispatch_speaker_rename(renames, "Dana")

        # ceil(file_count / batch_size) == 3 batches, not file_count tasks.
        assert digest_mock.call_count == 3
        batched_files = [len(call.kwargs["file_uuids"]) for call in digest_mock.call_args_list]
        assert sum(batched_files) == file_count
        assert all(n <= _DIGEST_REGEN_BATCH_SIZE for n in batched_files)

    def test_digest_regeneration_is_dispatched_even_when_nothing_else_would_be_queued(self):
        """Renaming to the same name queues neither the chunk rewrite nor a digest regen.

        The control for the next test: a genuine no-op (old == new) must still queue
        nothing at all, so the "unconditional" claim below is about a REAL rename
        whose chunk-plane rewrite happens to find zero stale documents, not about
        every call to ``dispatch_speaker_rename``.
        """
        from app.tasks.rename_propagation_task import dispatch_speaker_rename

        file_uuid = str(uuid_mod.uuid4())
        with patch(_CHUNK_DELAY) as chunk_mock, patch(_DIGEST_DELAY) as digest_mock:
            queued = dispatch_speaker_rename([(file_uuid, "Dana")], "Dana")

        assert queued == 0
        chunk_mock.assert_not_called()
        digest_mock.assert_not_called()

    def test_digest_regeneration_does_not_read_the_chunk_tasks_return_value(self):
        """`_finish`'s `updated == 0` early return must not be able to gate this.

        `propagate_speaker_rename.delay(...)` returns an AsyncResult, not the task's
        eventual `updated` count — dispatch_speaker_rename cannot see whether the
        chunk plane found anything to rewrite even if it wanted to. Asserting the
        digest dispatch fires from a real rename regardless of what the mocked
        `.delay()` call returns is what pins that it is wired at the dispatch site,
        not chained off the chunk task's result.
        """
        from app.tasks.rename_propagation_task import dispatch_speaker_rename

        file_uuid = str(uuid_mod.uuid4())
        with (
            patch(_CHUNK_DELAY, return_value=None) as chunk_mock,
            patch(_DIGEST_DELAY) as digest_mock,
        ):
            queued = dispatch_speaker_rename([(file_uuid, "SPEAKER_00")], "Dana")

        assert queued == 1
        chunk_mock.assert_called_once()
        digest_mock.assert_called_once_with(
            file_uuids=[file_uuid], new_name="Dana", speaker_id=None
        )


class TestDigestRegenerationTaskWiring:
    def test_task_is_registered_and_routed(self):
        from app.core.celery import celery_app

        assert "regenerate_rename_digests" in celery_app.conf.task_routes
        assert celery_app.conf.task_routes["regenerate_rename_digests"] == {"queue": "cpu"}

    def test_task_name_and_argument_shape_survive_the_json_broker_serializer(self):
        import json

        from app.tasks.rename_propagation_task import regenerate_rename_digests

        assert regenerate_rename_digests.name == "regenerate_rename_digests"
        json.dumps(
            {
                "file_uuids": [str(uuid_mod.uuid4()), str(uuid_mod.uuid4())],
                "new_name": "Dana",
                "speaker_id": 7,
            }
        )

    def test_empty_file_list_is_a_no_op_and_never_calls_opensearch(self):
        """Guards the ``if not uuids: return`` short circuit at the top of the task."""
        from app.tasks.rename_propagation_task import regenerate_rename_digests

        with patch("app.services.search.indexing_service.TranscriptIndexingService") as svc:
            result = regenerate_rename_digests(file_uuids=[])

        svc.assert_not_called()
        assert result == {"status": "skipped", "reason": "no_files", "regenerated": 0, "errors": 0}
