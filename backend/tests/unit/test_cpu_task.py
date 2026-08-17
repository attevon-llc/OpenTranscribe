"""Tests for the CPU-only lightweight transcription task
(``app/tasks/transcription/cpu_task.py``).

Follows the ``unit/test_dispatch.py`` fix-shape (backend/tests/CLAUDE.md): real
``MediaFile``/``Task`` rows, asserted on after the call, with only genuinely
out-of-process seams patched — the two ``session_scope`` import sites (this
module opens its own session, and so does ``context.py``'s failure handler),
MinIO (``download_temp_audio``), and outbound notifications. The actual model
inference (``TranscriptionPipeline.process``) and the downstream
speaker/DB-save step (``_process_and_save_critical``, owned by
``finalize.py`` — a different module) are mocked as the heavy/out-of-scope
work; everything cpu_task.py itself does — status transitions, the
``diarization_disabled`` flag, validation-error and exception handling — is
asserted against real DB state.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.tasks.transcription.context import TranscriptionContext
from app.tasks.transcription.cpu_task import _run_cpu_transcription
from app.tasks.transcription.cpu_task import transcribe_cpu_task

_CPU_TASK = "app.tasks.transcription.cpu_task"
_CONTEXT = "app.tasks.transcription.context"
_SESSION_SCOPE = f"{_CPU_TASK}.session_scope"
_CONTEXT_SESSION_SCOPE = f"{_CONTEXT}.session_scope"
_SEND_PROGRESS = f"{_CPU_TASK}.send_progress_notification"
_CONTEXT_SEND_ERROR = f"{_CONTEXT}.send_error_notification"
_DOWNLOAD_TEMP_AUDIO = "app.services.minio_service.download_temp_audio"
_PROCESS_AND_SAVE = f"{_CPU_TASK}._process_and_save_critical"


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def cpu_task_seams(db_session):
    """Patch the out-of-process seams. See the module docstring."""
    with (
        patch(_SESSION_SCOPE, lambda: _yield_session(db_session)),
        patch(_CONTEXT_SESSION_SCOPE, lambda: _yield_session(db_session)),
        patch(_SEND_PROGRESS) as send_progress,
        patch(_CONTEXT_SEND_ERROR) as send_error,
        patch(_DOWNLOAD_TEMP_AUDIO) as download_temp_audio,
    ):
        yield {
            "send_progress": send_progress,
            "send_error": send_error,
            "download_temp_audio": download_temp_audio,
        }


@pytest.fixture
def make_media_file(db_session, normal_user):
    def _make(status: FileStatus = FileStatus.PROCESSING) -> MediaFile:
        media_file = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename="cpu_task_test.mp3",
            storage_path="cpu_task/cpu_task_test.mp3",
            file_size=1024,
            content_type="audio/mpeg",
            status=status,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)
        return media_file

    return _make


@pytest.fixture
def make_task(db_session, normal_user):
    def _make(media_file: MediaFile, task_id: str, status: str = "in_progress") -> Task:
        task = Task(
            id=task_id,
            user_id=normal_user.id,
            media_file_id=media_file.id,
            task_type="transcription",
            status=status,
            progress=0.22,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        return task

    return _make


class TestRunCpuTranscription:
    """Config-building and result-shaping logic, independent of the Celery task."""

    def _ctx(self, normal_user) -> TranscriptionContext:
        return TranscriptionContext(
            task_id="cputask-inner",
            file_id=1,
            file_uuid=str(uuid_pkg.uuid4()),
            user_id=normal_user.id,
            file_path="cpu/x.mp3",
            file_name="x.mp3",
            content_type="audio/mpeg",
        )

    def test_annotates_result_and_forces_diarization_disabled(
        self, db_session, cpu_task_seams, normal_user
    ):
        ctx = self._ctx(normal_user)
        captured = {}

        class FakePipeline:
            def __init__(self, config):
                captured["config"] = config

            def process(self, audio_file_path, progress_callback=None, task_id=None):
                captured["audio_file_path"] = audio_file_path
                if progress_callback:
                    progress_callback(0.5, "halfway")
                return {"segments": [{"text": "hi", "words": []}], "language": "en"}

        with patch("app.transcription.TranscriptionPipeline", FakePipeline):
            result = _run_cpu_transcription(
                ctx,
                "/fake/audio.wav",
                source_language="en",
                translate_to_english=False,
                whisper_model="tiny",
            )

        assert result["asr_provider"] == "local"
        assert result["diarization_disabled"] is True
        assert result["asr_model"] == "tiny"
        assert captured["audio_file_path"] == "/fake/audio.wav"
        # min_speakers/max_speakers are hardcoded to 1 for the CPU lightweight path
        assert captured["config"].min_speakers == 1
        assert captured["config"].max_speakers == 1
        assert captured["config"].model_name == "tiny"

    def test_whisper_model_outside_lightweight_set_is_ignored(
        self, db_session, cpu_task_seams, normal_user
    ):
        ctx = self._ctx(normal_user)
        captured = {}

        class FakePipeline:
            def __init__(self, config):
                captured["config"] = config

            def process(self, *a, **kw):
                return {"segments": []}

        with patch("app.transcription.TranscriptionPipeline", FakePipeline):
            _run_cpu_transcription(
                ctx,
                "/fake/audio.wav",
                source_language="en",
                translate_to_english=False,
                whisper_model="large-v3-turbo",  # not in LIGHTWEIGHT_MODELS
            )

        # The override is dropped; for_cpu_lightweight's own default wins instead.
        assert captured["config"].model_name != "large-v3-turbo"

    def test_non_dict_result_is_returned_unmodified(self, db_session, cpu_task_seams, normal_user):
        ctx = self._ctx(normal_user)
        sentinel = object()

        class FakePipeline:
            def __init__(self, config):
                pass

            def process(self, *a, **kw):
                return sentinel

        with patch("app.transcription.TranscriptionPipeline", FakePipeline):
            result = _run_cpu_transcription(
                ctx, "/fake/audio.wav", source_language="en", translate_to_english=False
            )

        assert result is sentinel


class TestTranscribeCpuTask:
    """The Celery entry point: orchestration, DB side effects, error handling."""

    def _preprocess_context(self, media_file: MediaFile, task_id: str, user_id: int) -> dict:
        return {
            "task_id": task_id,
            "file_uuid": str(media_file.uuid),
            "file_id": media_file.id,
            "user_id": user_id,
            "storage_path": media_file.storage_path,
            "file_name": media_file.filename,
            "content_type": media_file.content_type,
        }

    def test_happy_path_marks_diarization_disabled_and_returns_finalize_result(
        self, db_session, cpu_task_seams, make_media_file, make_task, normal_user
    ):
        media_file = make_media_file()
        task_id = str(uuid_pkg.uuid4())
        make_task(media_file, task_id)
        preprocess_context = self._preprocess_context(media_file, task_id, normal_user.id)
        expected = {"status": "success", "file_id": media_file.id}

        with (
            patch(
                f"{_CPU_TASK}._run_cpu_transcription", return_value={"segments": [{"text": "hi"}]}
            ),
            patch(_PROCESS_AND_SAVE, return_value=expected) as finalize_mock,
        ):
            result = transcribe_cpu_task.run(preprocess_context)

        assert result == expected
        db_session.refresh(media_file)
        assert media_file.diarization_disabled is True
        cpu_task_seams["download_temp_audio"].assert_called_once()
        called_uuid, local_path = cpu_task_seams["download_temp_audio"].call_args[0]
        assert called_uuid == str(media_file.uuid)
        assert local_path.endswith("audio.wav")
        finalize_mock.assert_called_once()
        ctx_arg, result_arg, preprocess_arg = finalize_mock.call_args[0]
        assert ctx_arg.file_id == media_file.id
        assert preprocess_arg == preprocess_context

    def test_no_speech_detected_marks_file_error_via_validation(
        self, db_session, cpu_task_seams, make_media_file, make_task, normal_user
    ):
        media_file = make_media_file()
        task_id = str(uuid_pkg.uuid4())
        make_task(media_file, task_id)
        preprocess_context = self._preprocess_context(media_file, task_id, normal_user.id)

        with (
            patch(f"{_CPU_TASK}._run_cpu_transcription", return_value={"segments": []}),
            patch(_PROCESS_AND_SAVE) as finalize_mock,
        ):
            result = transcribe_cpu_task.run(preprocess_context)

        assert result["status"] == "error"
        assert result["file_uuid"] == str(media_file.uuid)
        finalize_mock.assert_not_called()
        db_session.refresh(media_file)
        assert media_file.status == FileStatus.ERROR
        task = db_session.query(Task).filter(Task.id == task_id).one()
        assert task.status == "failed"
        cpu_task_seams["send_error"].assert_called_once()

    def test_download_failure_on_a_retryable_attempt_reraises_without_marking_error(
        self, db_session, cpu_task_seams, make_media_file, make_task, normal_user
    ):
        """A ConnectionError with retries still available must NOT run the failure
        side effects (ERROR status, user notification) — those would fire BEFORE
        Celery's autoretry_for wrapper ever sees the exception, producing a false
        failure notification for a transient blip the task is about to retry."""
        media_file = make_media_file()
        task_id = str(uuid_pkg.uuid4())
        make_task(media_file, task_id)
        preprocess_context = self._preprocess_context(media_file, task_id, normal_user.id)
        cpu_task_seams["download_temp_audio"].side_effect = ConnectionError("MinIO unreachable")

        with pytest.raises(ConnectionError):
            transcribe_cpu_task.run(preprocess_context)

        db_session.refresh(media_file)
        # The download failure happens before anything in this task writes a new
        # status, so an untouched file must still read exactly its starting state.
        assert media_file.status == FileStatus.PROCESSING
        task = db_session.query(Task).filter(Task.id == task_id).one()
        assert task.status == "in_progress"
        cpu_task_seams["send_error"].assert_not_called()

    def test_download_failure_after_retries_exhausted_marks_file_error(
        self, db_session, cpu_task_seams, make_media_file, make_task, normal_user
    ):
        """Once retries are exhausted this IS the final failure — Celery will not
        attempt again — so it must still be reported like any other terminal error."""
        media_file = make_media_file()
        task_id = str(uuid_pkg.uuid4())
        make_task(media_file, task_id)
        preprocess_context = self._preprocess_context(media_file, task_id, normal_user.id)
        cpu_task_seams["download_temp_audio"].side_effect = ConnectionError("MinIO unreachable")

        transcribe_cpu_task.push_request(id=task_id, retries=transcribe_cpu_task.max_retries)
        try:
            with pytest.raises(ConnectionError):
                transcribe_cpu_task.run(preprocess_context)
        finally:
            transcribe_cpu_task.pop_request()

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.ERROR
        task = db_session.query(Task).filter(Task.id == task_id).one()
        assert task.status == "failed"
        cpu_task_seams["send_error"].assert_called_once()

    def test_finalize_failure_marks_file_error_and_reraises(
        self, db_session, cpu_task_seams, make_media_file, make_task, normal_user
    ):
        media_file = make_media_file()
        task_id = str(uuid_pkg.uuid4())
        make_task(media_file, task_id)
        preprocess_context = self._preprocess_context(media_file, task_id, normal_user.id)

        with (
            patch(
                f"{_CPU_TASK}._run_cpu_transcription", return_value={"segments": [{"text": "hi"}]}
            ),
            patch(_PROCESS_AND_SAVE, side_effect=RuntimeError("DB save exploded")),
            pytest.raises(RuntimeError, match="DB save exploded"),
        ):
            transcribe_cpu_task.run(preprocess_context)

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.ERROR
        task = db_session.query(Task).filter(Task.id == task_id).one()
        assert task.status == "failed"
        assert task.error_message
