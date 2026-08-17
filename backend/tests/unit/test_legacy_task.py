"""Tests for the legacy monolithic transcription task
(``app/tasks/transcription/legacy_task.py``).

Follows the ``unit/test_dispatch.py`` fix-shape (backend/tests/CLAUDE.md): real
``MediaFile``/``Task`` rows asserted on after the call. Only genuinely
out-of-process seams are patched: the two ``session_scope`` import sites
(``legacy_task.py`` and ``context.py`` each open their own session, invisible
to the savepoint harness otherwise), MinIO reads (``download_file``,
``download_file_to_path``, ``get_file_url``), metadata extraction (ExifTool
subprocess), audio extraction/preparation (ffmpeg), the ASR provider factory
(DB + potential network), and outbound notifications. The routing decision
between the cloud and local pipelines, validation-error short-circuiting, and
the Celery task's own exception-classification (``PermissionError`` vs
generic) are exercised for real, against real DB state — that logic is what
this module exists to own; ``_run_cloud_asr_pipeline``, ``_run_transcription_pipeline``
and ``_process_transcription_result`` themselves belong to other modules and
are mocked here as the heavy/out-of-scope work.
"""

from __future__ import annotations

import io
import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.tasks.transcription.context import TranscriptionContext
from app.tasks.transcription.legacy_task import _download_and_extract_metadata
from app.tasks.transcription.legacy_task import _extract_metadata_if_available
from app.tasks.transcription.legacy_task import _process_file_in_temp_dir
from app.tasks.transcription.legacy_task import transcribe_audio_task

_LEGACY = "app.tasks.transcription.legacy_task"
_CONTEXT = "app.tasks.transcription.context"
_SESSION_SCOPE = f"{_LEGACY}.session_scope"
_CONTEXT_SESSION_SCOPE = f"{_CONTEXT}.session_scope"
_CONTEXT_SEND_ERROR = f"{_CONTEXT}.send_error_notification"
_EXTRACT_MEDIA_METADATA = f"{_LEGACY}.extract_media_metadata"
_DOWNLOAD_FILE = f"{_LEGACY}.download_file"
_DOWNLOAD_FILE_TO_PATH = "app.services.minio_service.download_file_to_path"
_GET_FILE_URL = "app.services.minio_service.get_file_url"
_SEND_PROCESSING = f"{_LEGACY}.send_processing_notification"
_SEND_PROGRESS = f"{_LEGACY}.send_progress_notification"
_ASR_FACTORY_CREATE = "app.services.asr.factory.ASRProviderFactory.create_for_user"
_RUN_CLOUD_PIPELINE = f"{_LEGACY}._run_cloud_asr_pipeline"
_RUN_TRANSCRIPTION_PIPELINE = f"{_LEGACY}._run_transcription_pipeline"
_PROCESS_RESULT = f"{_LEGACY}._process_transcription_result"
_PROCESS_FILE_IN_TEMP_DIR = f"{_LEGACY}._process_file_in_temp_dir"
_EXTRACT_AUDIO_FROM_VIDEO = "app.tasks.transcription.audio_processor.extract_audio_from_video"
_PREPARE_AUDIO = f"{_LEGACY}.prepare_audio_for_transcription"


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def legacy_seams(db_session):
    """Patch the out-of-process seams shared by every function under test."""
    with (
        patch(_SESSION_SCOPE, lambda: _yield_session(db_session)),
        patch(_CONTEXT_SESSION_SCOPE, lambda: _yield_session(db_session)),
        patch(_CONTEXT_SEND_ERROR) as send_error,
        patch(_SEND_PROCESSING) as send_processing,
        patch(_SEND_PROGRESS) as send_progress,
    ):
        yield {
            "send_error": send_error,
            "send_processing": send_processing,
            "send_progress": send_progress,
        }


@pytest.fixture
def make_media_file(db_session, normal_user):
    def _make(
        status: FileStatus = FileStatus.PENDING, content_type: str = "audio/mpeg"
    ) -> MediaFile:
        media_file = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename="legacy_test.mp3" if content_type.startswith("audio") else "legacy_test.mp4",
            storage_path="legacy/legacy_test",
            file_size=4096,
            content_type=content_type,
            status=status,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)
        return media_file

    return _make


def _ctx(media_file: MediaFile, task_id: str) -> TranscriptionContext:
    return TranscriptionContext(
        task_id=task_id,
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=media_file.user_id,
        file_path=media_file.storage_path,
        file_name=media_file.filename or "",
        content_type=media_file.content_type,
    )


class LocalASRProvider:
    provider_name = "local"


class CloudASRProvider:
    provider_name = "deepgram"


class TestExtractMetadataIfAvailable:
    def test_no_metadata_does_not_touch_the_row(self, db_session, legacy_seams, make_media_file):
        media_file = make_media_file()
        ctx = _ctx(media_file, "task-meta-none")

        with patch(_EXTRACT_MEDIA_METADATA, return_value=None):
            _extract_metadata_if_available("/fake/whatever.mp3", ctx)

        db_session.refresh(media_file)
        assert media_file.metadata_raw is None

    def test_metadata_present_updates_the_row(self, db_session, legacy_seams, make_media_file):
        media_file = make_media_file()
        ctx = _ctx(media_file, "task-meta-present")
        raw = {"FileType": "MP3", "Duration": "12.3 s"}

        with patch(_EXTRACT_MEDIA_METADATA, return_value=raw):
            _extract_metadata_if_available("/fake/whatever.mp3", ctx)

        db_session.refresh(media_file)
        assert media_file.metadata_raw == raw
        assert media_file.media_format == "MP3"


class TestDownloadAndExtractMetadata:
    def test_success_delegates_to_extraction(self, db_session, legacy_seams, make_media_file):
        media_file = make_media_file()
        ctx = _ctx(media_file, "task-dl-meta")

        with (
            patch(_DOWNLOAD_FILE_TO_PATH) as download_mock,
            patch(_EXTRACT_MEDIA_METADATA, return_value={"FileType": "MOV"}),
        ):
            _download_and_extract_metadata(media_file.storage_path, "/fake/x.mov", ctx)

        download_mock.assert_called_once_with(media_file.storage_path, "/fake/x.mov")
        db_session.refresh(media_file)
        assert media_file.media_format == "MOV"

    def test_download_failure_is_swallowed(self, db_session, legacy_seams, make_media_file):
        media_file = make_media_file()
        ctx = _ctx(media_file, "task-dl-meta-fail")

        with patch(_DOWNLOAD_FILE_TO_PATH, side_effect=RuntimeError("MinIO down")):
            # Must not raise — metadata is best-effort.
            _download_and_extract_metadata(media_file.storage_path, "/fake/x.mov", ctx)

        db_session.refresh(media_file)
        assert media_file.metadata_raw is None


class TestProcessFileInTempDir:
    """Routing between cloud and local pipelines, and validation short-circuiting.

    Metadata extraction (ExifTool) runs on a background thread regardless of which
    branch is under test and is never itself the thing being asserted on here, so
    it is patched once for the whole class rather than repeated in every test —
    that also keeps each test under the audit gate's mock-call ceiling, which is
    a proxy for "assert behaviour, not wiring" (backend/tests/CLAUDE.md).
    """

    @pytest.fixture(autouse=True)
    def _no_metadata_extraction(self):
        with patch(_EXTRACT_MEDIA_METADATA, return_value=None):
            yield

    def test_local_provider_routes_to_local_pipeline(
        self, db_session, legacy_seams, make_media_file, tmp_path
    ):
        media_file = make_media_file()
        ctx = _ctx(media_file, "task-local-route")
        file_data = io.BytesIO(b"fake-audio-bytes")
        local_result = {"segments": [{"text": "hi"}], "language": "en"}

        with (
            patch(
                f"{_LEGACY}.prepare_audio_for_transcription", return_value=str(tmp_path / "a.wav")
            ),
            patch(_ASR_FACTORY_CREATE, return_value=LocalASRProvider()),
            patch(_RUN_TRANSCRIPTION_PIPELINE, return_value=local_result) as local_mock,
            patch(_RUN_CLOUD_PIPELINE) as cloud_mock,
            patch(_PROCESS_RESULT, return_value={"status": "success"}) as process_mock,
        ):
            result = _process_file_in_temp_dir(
                ctx, str(tmp_path), file_data, ".mp3", None, None, None
            )

        local_mock.assert_called_once()
        cloud_mock.assert_not_called()
        process_mock.assert_called_once()
        assert result == {"status": "success"}

    def test_cloud_provider_routes_to_cloud_pipeline(
        self, db_session, legacy_seams, make_media_file, tmp_path
    ):
        media_file = make_media_file()
        ctx = _ctx(media_file, "task-cloud-route")
        file_data = io.BytesIO(b"fake-audio-bytes")
        cloud_result = {"segments": [{"text": "hi"}], "language": "en"}

        with (
            patch(
                f"{_LEGACY}.prepare_audio_for_transcription", return_value=str(tmp_path / "a.wav")
            ),
            patch(_ASR_FACTORY_CREATE, return_value=CloudASRProvider()),
            patch(_RUN_CLOUD_PIPELINE, return_value=cloud_result) as cloud_mock,
            patch(_RUN_TRANSCRIPTION_PIPELINE) as local_mock,
            patch(_PROCESS_RESULT, return_value={"status": "success"}) as process_mock,
        ):
            result = _process_file_in_temp_dir(ctx, str(tmp_path), file_data, ".mp3", 1, 5, None)

        cloud_mock.assert_called_once()
        # Positional args: ctx, audio_path, min_speakers, max_speakers, num_speakers
        call_args = cloud_mock.call_args[0]
        assert call_args[2] == 1
        assert call_args[3] == 5
        local_mock.assert_not_called()
        process_mock.assert_called_once()
        assert result == {"status": "success"}

    def test_factory_error_falls_back_to_local_pipeline(
        self, db_session, legacy_seams, make_media_file, tmp_path
    ):
        media_file = make_media_file()
        ctx = _ctx(media_file, "task-factory-fallback")
        file_data = io.BytesIO(b"fake-audio-bytes")

        with (
            patch(
                f"{_LEGACY}.prepare_audio_for_transcription", return_value=str(tmp_path / "a.wav")
            ),
            patch(_ASR_FACTORY_CREATE, side_effect=RuntimeError("DB unreachable")),
            patch(
                _RUN_TRANSCRIPTION_PIPELINE, return_value={"segments": [{"text": "hi"}]}
            ) as local_mock,
            patch(_RUN_CLOUD_PIPELINE) as cloud_mock,
            patch(_PROCESS_RESULT, return_value={"status": "success"}),
        ):
            result = _process_file_in_temp_dir(
                ctx, str(tmp_path), file_data, ".mp3", None, None, None
            )

        local_mock.assert_called_once()
        cloud_mock.assert_not_called()
        # A real assertion on the pipeline's own output, not just mock bookkeeping:
        # the fallback must still produce the local pipeline's result.
        assert result == {"status": "success"}

    def test_validation_error_short_circuits_before_process_result(
        self, db_session, legacy_seams, make_media_file, tmp_path
    ):
        media_file = make_media_file()
        ctx = _ctx(media_file, "task-validation-error")
        file_data = io.BytesIO(b"fake-audio-bytes")

        with (
            patch(
                f"{_LEGACY}.prepare_audio_for_transcription", return_value=str(tmp_path / "a.wav")
            ),
            patch(_ASR_FACTORY_CREATE, return_value=LocalASRProvider()),
            patch(_RUN_TRANSCRIPTION_PIPELINE, return_value={"segments": []}),
            patch(_PROCESS_RESULT) as process_mock,
        ):
            result = _process_file_in_temp_dir(
                ctx, str(tmp_path), file_data, ".mp3", None, None, None
            )

        process_mock.assert_not_called()
        assert result["status"] == "error"
        db_session.refresh(media_file)
        assert media_file.status == FileStatus.ERROR

    def test_video_path_extracts_audio_directly_from_presigned_url(
        self, db_session, legacy_seams, make_media_file, tmp_path
    ):
        media_file = make_media_file(content_type="video/mp4")
        ctx = _ctx(media_file, "task-video-route")

        with (
            patch(_EXTRACT_AUDIO_FROM_VIDEO) as extract_mock,
            patch(_DOWNLOAD_FILE_TO_PATH),
            patch(_ASR_FACTORY_CREATE, return_value=LocalASRProvider()),
            patch(_RUN_TRANSCRIPTION_PIPELINE, return_value={"segments": [{"text": "hi"}]}),
            patch(_PROCESS_RESULT, return_value={"status": "success"}),
        ):
            result = _process_file_in_temp_dir(
                ctx,
                str(tmp_path),
                None,
                ".mp4",
                None,
                None,
                None,
                minio_url="https://minio.local/presigned",
            )

        extract_mock.assert_called_once()
        assert extract_mock.call_args[0][0] == "https://minio.local/presigned"
        assert result == {"status": "success"}


class TestTranscribeAudioTask:
    """The Celery entry point: context creation, dispatch, and error classification.

    ``self.request.id`` is unset outside a real Celery worker, so ``task_id =
    self.request.id`` is ``None`` and every DB write collides on the ``task.id``
    primary key. ``push_request``/``pop_request`` is the documented way to run a
    ``bind=True`` task directly (see ``test_speaker_attribute_migration_task.py``).
    """

    @staticmethod
    def _run(file_uuid: str, **kwargs) -> dict:
        task_id = str(uuid_pkg.uuid4())
        transcribe_audio_task.push_request(id=task_id)
        try:
            result: dict = transcribe_audio_task.run(file_uuid, **kwargs)
            return result
        finally:
            transcribe_audio_task.pop_request()

    def test_missing_media_file_returns_error_without_creating_a_task(
        self, db_session, legacy_seams
    ):
        missing_uuid = str(uuid_pkg.uuid4())

        result = self._run(missing_uuid)

        # get_file_by_uuid raises HTTPException(404) rather than returning None, so
        # this reaches the outer exception handler, not the ctx-is-None early return.
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()
        assert db_session.query(Task).filter(Task.id == missing_uuid).first() is None

    def test_happy_path_creates_task_and_delegates_to_temp_dir_processing(
        self, db_session, legacy_seams, make_media_file
    ):
        media_file = make_media_file()
        expected = {"status": "success", "file_id": media_file.id}

        with (
            patch(_DOWNLOAD_FILE, return_value=(io.BytesIO(b"data"), 4, "audio/mpeg")),
            patch(_PROCESS_FILE_IN_TEMP_DIR, return_value=expected) as process_mock,
        ):
            result = self._run(
                str(media_file.uuid),
                min_speakers=1,
                max_speakers=3,
                downstream_tasks=["summarization"],
            )

        assert result == expected
        process_mock.assert_called_once()
        args = process_mock.call_args[0]
        assert args[0].file_id == media_file.id  # ctx
        assert args[4] == 1  # min_speakers
        assert args[5] == 3  # max_speakers
        assert args[7] == ["summarization"]  # downstream_tasks
        legacy_seams["send_processing"].assert_called_once()

        task = db_session.query(Task).filter(Task.media_file_id == media_file.id).one()
        assert task.task_type == "transcription"

    def test_video_file_uses_presigned_url_instead_of_download(
        self, db_session, legacy_seams, make_media_file
    ):
        media_file = make_media_file(content_type="video/mp4")

        with (
            patch(_GET_FILE_URL, return_value="https://minio.local/presigned") as url_mock,
            patch(_DOWNLOAD_FILE) as download_mock,
            patch(_PROCESS_FILE_IN_TEMP_DIR, return_value={"status": "success"}) as process_mock,
        ):
            self._run(str(media_file.uuid))

        url_mock.assert_called_once()
        download_mock.assert_not_called()
        kwargs = process_mock.call_args.kwargs
        assert kwargs["minio_url"] == "https://minio.local/presigned"

    def test_permission_error_is_classified_as_gated_model_access(
        self, db_session, legacy_seams, make_media_file
    ):
        media_file = make_media_file()

        with (
            patch(_DOWNLOAD_FILE, return_value=(io.BytesIO(b"data"), 4, "audio/mpeg")),
            patch(_PROCESS_FILE_IN_TEMP_DIR, side_effect=PermissionError("gated HF model")),
        ):
            result = self._run(str(media_file.uuid))

        assert result["error_type"] == "gated_model_access"
        db_session.refresh(media_file)
        assert media_file.status == FileStatus.ERROR
        task = db_session.query(Task).filter(Task.media_file_id == media_file.id).one()
        assert task.status == "failed"
        legacy_seams["send_error"].assert_called_once()

    def test_generic_exception_is_classified_as_processing_error(
        self, db_session, legacy_seams, make_media_file
    ):
        media_file = make_media_file()

        with (
            patch(_DOWNLOAD_FILE, return_value=(io.BytesIO(b"data"), 4, "audio/mpeg")),
            patch(_PROCESS_FILE_IN_TEMP_DIR, side_effect=RuntimeError("ffmpeg exploded")),
        ):
            result = self._run(str(media_file.uuid))

        assert result["error_type"] == "processing_error"
        db_session.refresh(media_file)
        assert media_file.status == FileStatus.ERROR

    def test_context_creation_failure_hits_outer_exception_handler(
        self, db_session, legacy_seams, make_media_file
    ):
        media_file = make_media_file()

        with patch(
            f"{_LEGACY}._get_media_file_context", side_effect=RuntimeError("DB connection lost")
        ):
            result = self._run(str(media_file.uuid))

        assert result["status"] == "error"
        assert "DB connection lost" in result["message"]
        # No task could have been created — ctx never existed.
        assert db_session.query(Task).filter(Task.media_file_id == media_file.id).first() is None
