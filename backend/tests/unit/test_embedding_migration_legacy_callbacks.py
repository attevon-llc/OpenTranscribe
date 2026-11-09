"""The legacy v4 embedding task hands ``process_batch_pipelined`` callbacks that return None.

``process_batch_pipelined`` declares ``Callable[[str], None]`` / ``Callable[[str, Exception | None], None]``;
the task used to pass lambdas that returned whatever ``increment_processed`` returned.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.tasks.embedding_migration_v4 import extract_v4_embeddings_task

pytestmark = pytest.mark.unit


@contextmanager
def _captured_callbacks():
    """Run the task with the pipeline stubbed; yield (progress mock, callbacks) while patched."""
    progress = MagicMock()
    progress.increment_processed.return_value = True
    progress.get_status.return_value = {"processed_files": 0, "total_files": 5}
    captured: dict = {}

    def fake_pipeline(**kwargs):
        captured.update(kwargs)

    with (
        patch("app.tasks.embedding_migration_v4.migration_progress", progress),
        patch("app.services.speaker_embedding_service.get_cached_embedding_service"),
        patch("app.services.speaker_analysis_models.EmbeddingModelAdapter"),
        patch("app.services.speaker_analysis_models.MultiModelRunner"),
        patch("app.tasks.migration_pipeline.prepare_file", return_value=object()),
        patch("app.tasks.migration_pipeline.process_batch_pipelined", side_effect=fake_pipeline),
        patch("app.tasks.migration_pipeline.cleanup_gpu_memory"),
    ):
        extract_v4_embeddings_task.run("file-uuid")
        yield progress, captured


def test_success_callback_records_a_success_and_returns_none():
    with _captured_callbacks() as (progress, captured):
        assert captured["on_file_success"]("file-uuid") is None
        progress.increment_processed.assert_called_with(success=True)


def test_failure_callback_records_the_failed_file_and_returns_none():
    with _captured_callbacks() as (progress, captured):
        assert captured["on_file_failure"]("file-uuid", RuntimeError("boom")) is None
        progress.increment_processed.assert_called_with(success=False, file_uuid="file-uuid")
