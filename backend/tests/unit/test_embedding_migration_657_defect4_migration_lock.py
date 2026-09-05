"""The migration lock is wired to real pause points (issue #657, defect 4).

Before this fix, ``finalize_v4_migration_task``'s docstring claimed to
"acquire the migration lock (pausing transcription)" while
``migration_lock_service.py`` had zero production callers — the lock was
pure dead code, and live transcription raced a running migration over the
shared (mode, model_name) warm embedding-model cache with no coordination
at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.tasks.transcription import embeddings as emb_mod


@pytest.mark.unit
class TestMigrationLockWaitPoint:
    def test_no_wait_when_lock_is_not_active(self, monkeypatch):
        sleep_calls = []
        monkeypatch.setattr(emb_mod.time, "sleep", lambda s: sleep_calls.append(s))
        fake_lock = MagicMock()
        fake_lock.is_active.return_value = False

        with patch("app.services.migration_lock_service.migration_lock", fake_lock):
            emb_mod._wait_for_migration_lock_to_clear()

        assert sleep_calls == []

    def test_waits_then_proceeds_once_lock_clears(self, monkeypatch):
        sleep_calls = []
        monkeypatch.setattr(emb_mod.time, "sleep", lambda s: sleep_calls.append(s))
        fake_lock = MagicMock()
        # Active for two polls, then clears.
        fake_lock.is_active.side_effect = [True, True, True, False, False]

        with patch("app.services.migration_lock_service.migration_lock", fake_lock):
            emb_mod._wait_for_migration_lock_to_clear()

        assert len(sleep_calls) == 2
        assert all(s == emb_mod._MIGRATION_LOCK_POLL_INTERVAL_SECONDS for s in sleep_calls)

    def test_gives_up_after_bounded_timeout_and_proceeds_anyway(self, monkeypatch, caplog):
        sleep_calls = []
        monkeypatch.setattr(emb_mod.time, "sleep", lambda s: sleep_calls.append(s))
        fake_lock = MagicMock()
        fake_lock.is_active.return_value = True  # never clears

        with caplog.at_level("WARNING"):
            with patch("app.services.migration_lock_service.migration_lock", fake_lock):
                emb_mod._wait_for_migration_lock_to_clear()

        max_polls = (
            emb_mod._MIGRATION_LOCK_WAIT_TIMEOUT_SECONDS
            // emb_mod._MIGRATION_LOCK_POLL_INTERVAL_SECONDS
        )
        assert len(sleep_calls) == max_polls
        assert any("still active" in r.message for r in caplog.records)


@pytest.mark.unit
class TestOrchestratorAcquiresLockBeforeDispatch:
    def test_activate_called_when_files_are_dispatched(self, monkeypatch):
        """migrate_speaker_embeddings_v4_task must acquire the lock once it
        has decided there is real GPU work to dispatch. This is a source
        check rather than a full task run (the task's DB/celery/OpenSearch
        surface is large); it greps the compiled bytecode-backing source for
        the acquire call appearing before the batch-dispatch loop, which is
        cheap, exact, and immune to the whole-task mocking otherwise needed.
        """
        import inspect

        from app.tasks import embedding_migration_v4 as mig

        source = inspect.getsource(mig.migrate_speaker_embeddings_v4_task)
        activate_pos = source.index("migration_lock.activate()")
        dispatch_pos = source.index("extract_v4_embeddings_batch_task.apply_async")
        assert activate_pos < dispatch_pos

    def test_finalize_always_deactivates_lock(self):
        import inspect

        from app.tasks import embedding_migration_v4 as mig

        source = inspect.getsource(mig.finalize_v4_migration_task)
        assert "migration_lock.deactivate()" in source
        # Must be in the finally block, not only a happy-path branch.
        finally_block = source[source.index("finally:") :]
        assert "migration_lock.deactivate()" in finally_block
