"""What the pipeline does when speaker matching raises ``OpenSearchUnavailableError``.

The matching service no longer reads an index outage as "no match"; it raises and keeps
nothing. Each caller must then make sure matching runs again rather than recording the
file as done or failed for good:

* the voiceprint stage task (``extract_speaker_embeddings``) retries itself;
* the two inline paths (``finalize_transcription`` for this server's engine, and the
  rediarize post stage) hand the file to that task instead of matching inline.

Every case has a control where matching succeeds, so a caller that always handed off or
always retried could not pass.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.services.opensearch_service.client import OpenSearchUnavailableError
from tests.unit._plan_contexts import plan_context

FILE_UUID = "11111111-2222-3333-4444-555555555555"


@contextmanager
def _no_db():
    yield MagicMock()


@pytest.fixture
def published(monkeypatch) -> list[dict[str, Any]]:
    """Every dispatch of the voiceprint stage task."""
    sink: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "app.tasks.speaker_embedding_task.extract_speaker_embeddings_task.apply_async",
        lambda *a, **kw: sink.append(kw),
    )
    return sink


@pytest.fixture
def matching(monkeypatch) -> SimpleNamespace:
    """The matching service call, failing with an index outage while ``state.down``."""
    state = SimpleNamespace(down=True, calls=0)

    def process(self, **kw):
        state.calls += 1
        if state.down:
            raise OpenSearchUnavailableError("speaker kNN failed: connection refused")
        return []

    monkeypatch.setattr(
        "app.services.speaker_matching_service.SpeakerMatchingService"
        ".process_speaker_embeddings_native",
        process,
    )
    monkeypatch.setattr(
        "app.services.permission_service.PermissionService.get_accessible_profile_ids_for_file",
        staticmethod(lambda db, user_id, file_id: set()),
    )
    return state


# ---------------------------------------------------------------------------
# The voiceprint stage task retries
# ---------------------------------------------------------------------------


@pytest.fixture
def voiceprint_task(monkeypatch) -> SimpleNamespace:
    from app.tasks import speaker_embedding_task as mod

    record = SimpleNamespace(phase3=0, failed=[])
    monkeypatch.setattr(
        mod,
        "_load_speaker_embedding_inputs",
        lambda file_uuid, task_id: {
            "file_id": 1,
            "user_id": 7,
            "processed_segments": [{"speaker_id": 1, "start": 0.0, "end": 1.0}],
        },
    )
    monkeypatch.setattr(mod, "voiceprints_allowed", lambda file_id: True)
    monkeypatch.setattr(mod, "_extract_aggregated_embeddings", lambda *a: {1: np.ones(4)})
    for quiet in ("_set_task_progress", "_close_pipeline_task"):
        monkeypatch.setattr(mod, quiet, lambda *a, **k: None)
    monkeypatch.setattr(mod, "session_scope", _no_db)
    monkeypatch.setattr("app.services.minio_service.cleanup_temp_audio", lambda *a: None)
    monkeypatch.setattr(
        "app.utils.task_utils.update_task_status",
        lambda db, task_id, status, **kw: record.failed.append(status),
    )
    return record


def _phase3_that(outcome: Exception | None, record: SimpleNamespace):
    def phase3(*_a, **_k) -> int:
        record.phase3 += 1
        if outcome is not None:
            raise outcome
        return 1

    return phase3


def test_the_voiceprint_stage_retries_an_index_outage(voiceprint_task, monkeypatch):
    from app.tasks import speaker_embedding_task as mod

    outage = OpenSearchUnavailableError("speaker kNN failed")
    monkeypatch.setattr(
        mod, "_match_and_finalize_embeddings", _phase3_that(outage, voiceprint_task)
    )

    result = mod.extract_speaker_embeddings_task.apply(args=[FILE_UUID]).get()

    assert voiceprint_task.phase3 == 1 + mod.extract_speaker_embeddings_task.max_retries
    assert result["status"] == "error"
    assert voiceprint_task.failed == ["failed"], "recorded failed once, after the last retry"


def test_any_other_matching_error_still_fails_at_once(voiceprint_task, monkeypatch):
    from app.tasks import speaker_embedding_task as mod

    monkeypatch.setattr(
        mod, "_match_and_finalize_embeddings", _phase3_that(ValueError("bad"), voiceprint_task)
    )

    result = mod.extract_speaker_embeddings_task.apply(args=[FILE_UUID]).get()

    assert voiceprint_task.phase3 == 1
    assert result == {"status": "error", "message": "bad"}


# ---------------------------------------------------------------------------
# finalize_transcription (this server's engine matched inline)
# ---------------------------------------------------------------------------


_POSTPROCESS = "app.tasks.transcription.postprocess"


@pytest.fixture
def finalize(monkeypatch, privacy_allows_all) -> SimpleNamespace:
    record = SimpleNamespace(metered=[], completed=[])
    for name in ("enrich_and_dispatch", "send_completion_notification"):
        monkeypatch.setattr(f"{_POSTPROCESS}.{name}", MagicMock())
    monkeypatch.setattr(f"{_POSTPROCESS}.send_progress_notification", lambda *a, **k: None)
    monkeypatch.setattr(f"{_POSTPROCESS}.update_task_status", lambda *a, **k: None)
    monkeypatch.setattr(f"{_POSTPROCESS}.record_inline_voiceprints", lambda *a: None)
    monkeypatch.setattr(f"{_POSTPROCESS}.session_scope", _no_db)
    monkeypatch.setattr(
        f"{_POSTPROCESS}.update_task_status_with_duration",
        lambda db, task_id, status, **kw: record.completed.append(status) or (None, 1.0),
    )
    monkeypatch.setattr(
        f"{_POSTPROCESS}._fire_completion_metering", lambda db, **kw: record.metered.append(kw)
    )
    return record


def _inline_result() -> dict[str, Any]:
    return {
        "status": "success",
        "file_uuid": FILE_UUID,
        "file_id": 1,
        "user_id": 7,
        "task_id": "task-1",
        "speaker_mapping": {"SPEAKER_00": 1},
        "native_embeddings": {"SPEAKER_00": [1.0, 0.0, 0.0, 0.0]},
        "use_native_embeddings": True,
        "asr_provider": "local",
        "downstream_tasks": None,
        "diarization_disabled": False,
        "pipeline_plan": plan_context("local_gpu", "provider"),
    }


def test_an_inline_matching_outage_hands_the_file_to_the_voiceprint_stage(
    finalize, matching, published
):
    from app.tasks.transcription.postprocess import _finalize

    result = _finalize(_inline_result())

    assert result["status"] == "success", result
    assert len(published) == 1, "the voiceprint stage must take over matching"
    assert finalize.metered == [], "the voiceprint stage is now the run's terminus"


def test_inline_matching_that_answers_completes_the_run_here(finalize, matching, published):
    from app.tasks.transcription.postprocess import _finalize

    matching.down = False

    result = _finalize(_inline_result())

    assert result["status"] == "success", result
    assert matching.calls == 1
    assert published == []
    assert len(finalize.metered) == 1


# ---------------------------------------------------------------------------
# The rediarize post stage
# ---------------------------------------------------------------------------


def _post_stage_voiceprints(monkeypatch) -> bool:
    from app.tasks import rediarize_task as mod

    monkeypatch.setattr(mod, "voiceprints_allowed", lambda file_id: True)
    monkeypatch.setattr("app.tasks.transcription.embeddings.voiceprints_allowed", lambda f: True)
    monkeypatch.setattr("app.tasks.transcription.embeddings.session_scope", _no_db)
    monkeypatch.setattr(
        "app.tasks.transcription.embeddings.record_inline_voiceprints", lambda *a: None
    )
    monkeypatch.setattr(
        "app.tasks.transcription.embeddings.update_task_status", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "app.tasks.transcription.embeddings._should_use_native_embeddings", lambda result: True
    )
    detection = mod._Detection(
        frame=pd.DataFrame(),
        overlap_info={},
        native_embeddings={"SPEAKER_00": np.ones(4)},
        provider="native",
        model="m",
    )
    meta = {
        "file_id": 1,
        "user_id": 7,
        "storage_path": "user_7/x.wav",
        "filename": "x.wav",
        "content_type": "audio/wav",
    }
    return mod._voiceprint_stage(
        meta, FILE_UUID, "t", detection, [], {"SPEAKER_00": 1}, "pipeline-1"
    )


def test_a_post_stage_matching_outage_hands_the_file_to_the_voiceprint_stage(
    matching, published, monkeypatch
):
    owns_audio = _post_stage_voiceprints(monkeypatch)

    assert owns_audio is True
    assert len(published) == 1
    assert published[0]["kwargs"]["closes_pipeline"] is False


def test_post_stage_matching_that_answers_dispatches_nothing(matching, published, monkeypatch):
    matching.down = False

    owns_audio = _post_stage_voiceprints(monkeypatch)

    assert owns_audio is False
    assert matching.calls == 1
    assert published == []
