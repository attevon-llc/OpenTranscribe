"""Tests for the standalone waveform-generation Celery task
(``app/tasks/waveform_generation.py`` -- distinct from
``app/tasks/transcription/waveform_generator.py``'s ``WaveformGenerator``,
which already has coverage and is used here only as the out-of-scope heavy
dependency).

Follows the ``unit/test_dispatch.py`` / ``unit/test_cpu_task.py`` fix-shape
(``backend/tests/CLAUDE.md``): real ``MediaFile`` rows, asserted on after the
call, with only genuinely out-of-process seams patched -- ``session_scope``
(this module opens its own, twice: once in ``generate_waveform_data_task``
and once inside ``_generate_waveform_for_file``) and MinIO
(``download_file``). ``WaveformGenerator`` itself does real ffmpeg
subprocess work to decode audio, which is exactly the heavy/out-of-scope
work ``TranscriptionPipeline`` plays in ``test_cpu_task.py`` -- it is faked
here so these tests assert THIS module's query filtering, counting, and
DB-write behaviour rather than ffmpeg's.
"""

from __future__ import annotations

import io
import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.tasks.waveform_generation import _generate_waveform_for_file
from app.tasks.waveform_generation import generate_waveform_data_task
from app.tasks.waveform_generation import trigger_waveform_generation

_MODULE = "app.tasks.waveform_generation"
_SESSION_SCOPE = f"{_MODULE}.session_scope"
_DOWNLOAD_FILE = f"{_MODULE}.download_file"
_WAVEFORM_GENERATOR = f"{_MODULE}.WaveformGenerator"


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def waveform_seams(db_session):
    """Patch the out-of-process seams. See the module docstring."""
    with (
        patch(_SESSION_SCOPE, lambda: _yield_session(db_session)),
        patch(_DOWNLOAD_FILE) as download_file,
    ):
        download_file.return_value = (io.BytesIO(b"fake-audio-bytes"), 16, "audio/mpeg")
        yield {"download_file": download_file}


@pytest.fixture
def make_media_file(db_session, normal_user):
    def _make(
        *,
        status: FileStatus = FileStatus.COMPLETED,
        content_type: str = "audio/mpeg",
        waveform_data: dict | None = None,
        filename: str = "song.mp3",
    ) -> MediaFile:
        # NOTE: SQLAlchemy's JSONB type maps an explicitly-passed Python `None`
        # to the JSON *null* literal (a real, non-NULL column value) rather
        # than SQL NULL, unless the column is configured with
        # `none_as_null=True` (it is not, here). `waveform_data.is_(None)` in
        # the task's query only matches a genuine SQL NULL, so a NULL
        # waveform must be produced by leaving the kwarg unset entirely.
        kwargs = {}
        if waveform_data is not None:
            kwargs["waveform_data"] = waveform_data
        media_file = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename=filename,
            storage_path=f"waveform_test/{uuid_pkg.uuid4()}.mp3",
            file_size=1024,
            content_type=content_type,
            status=status,
            **kwargs,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)
        return media_file

    return _make


class FakeGenerator:
    """Stand-in for ``WaveformGenerator`` -- see module docstring."""

    def __init__(self, result=None, raise_on_generate: Exception | None = None):
        self._result = result
        self._raise = raise_on_generate

    def generate_waveform_data(self, file_path):
        if self._raise:
            raise self._raise
        return self._result


class TestGenerateWaveformForFile:
    """Direct unit tests of the per-file helper."""

    def test_success_saves_waveform_data_to_the_db(self, db_session, waveform_seams, normal_user):
        media_file = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename="a.mp3",
            storage_path="wf/a.mp3",
            file_size=10,
            content_type="audio/mpeg",
            status=FileStatus.COMPLETED,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)

        waveform_payload = {"peaks": [0.1, 0.2, 0.3], "duration": 12.5}
        with patch(_WAVEFORM_GENERATOR, return_value=FakeGenerator(result=waveform_payload)):
            result = _generate_waveform_for_file(media_file.id, media_file.storage_path, "a.mp3")

        assert result is True
        db_session.refresh(media_file)
        assert media_file.waveform_data == waveform_payload

    def test_none_result_returns_false_and_does_not_write(
        self, db_session, waveform_seams, normal_user
    ):
        media_file = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename="b.mp3",
            storage_path="wf/b.mp3",
            file_size=10,
            content_type="audio/mpeg",
            status=FileStatus.COMPLETED,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)

        with patch(_WAVEFORM_GENERATOR, return_value=FakeGenerator(result=None)):
            result = _generate_waveform_for_file(media_file.id, media_file.storage_path, "b.mp3")

        assert result is False
        db_session.refresh(media_file)
        assert media_file.waveform_data is None

    def test_generator_exception_is_caught_and_returns_false(
        self, db_session, waveform_seams, normal_user
    ):
        media_file = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename="c.mp3",
            storage_path="wf/c.mp3",
            file_size=10,
            content_type="audio/mpeg",
            status=FileStatus.COMPLETED,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)

        with patch(
            _WAVEFORM_GENERATOR,
            return_value=FakeGenerator(raise_on_generate=RuntimeError("ffmpeg exploded")),
        ):
            result = _generate_waveform_for_file(media_file.id, media_file.storage_path, "c.mp3")

        assert result is False

    def test_download_exception_is_caught_and_returns_false(self, db_session, normal_user):
        media_file = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename="d.mp3",
            storage_path="wf/d.mp3",
            file_size=10,
            content_type="audio/mpeg",
            status=FileStatus.COMPLETED,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)

        with patch(_DOWNLOAD_FILE, side_effect=OSError("minio unreachable")):
            result = _generate_waveform_for_file(media_file.id, media_file.storage_path, "d.mp3")

        assert result is False

    def test_missing_media_file_row_returns_false(self, db_session, waveform_seams):
        """file_id has no matching row -- the ``if media_file:`` guard is hit."""
        with patch(
            _WAVEFORM_GENERATOR, return_value=FakeGenerator(result={"peaks": [0.0], "duration": 1})
        ):
            result = _generate_waveform_for_file(999_999_999, "wf/nope.mp3", "nope.mp3")

        assert result is False

    def test_extension_defaults_to_tmp_when_filename_has_none(
        self, db_session, waveform_seams, normal_user
    ):
        media_file = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename="noext",
            storage_path="wf/noext",
            file_size=10,
            content_type="audio/mpeg",
            status=FileStatus.COMPLETED,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)

        captured_paths = []

        class RecordingGenerator(FakeGenerator):
            def generate_waveform_data(self, file_path):
                captured_paths.append(file_path)
                return {"peaks": [1.0], "duration": 1.0}

        with patch(_WAVEFORM_GENERATOR, return_value=RecordingGenerator()):
            result = _generate_waveform_for_file(media_file.id, media_file.storage_path, "noext")

        assert result is True
        assert len(captured_paths) == 1
        assert captured_paths[0].endswith(".tmp")


class TestGenerateWaveformDataTask:
    """The Celery entry point: query filtering, counting, error isolation."""

    def test_no_eligible_files_returns_the_no_op_result(self, db_session, waveform_seams):
        result = generate_waveform_data_task(file_uuid=None, skip_existing=True)
        assert result == {
            "status": "success",
            "message": "No files need waveform generation",
            "processed": 0,
        }

    def test_processes_all_eligible_files_and_counts_success_and_failure(
        self, db_session, waveform_seams, make_media_file
    ):
        succeeding = make_media_file(filename="ok.mp3")
        failing = make_media_file(filename="bad.mp3")

        calls = {"count": 0}

        def fake_ctor():
            call_count = calls["count"]
            calls["count"] += 1
            # First file processed succeeds, second fails.
            return FakeGenerator(result={"peaks": [1.0]} if call_count == 0 else None)

        with patch(_WAVEFORM_GENERATOR, side_effect=fake_ctor):
            result = generate_waveform_data_task(file_uuid=None, skip_existing=True)

        assert result["status"] == "success"
        assert result["total"] == 2
        assert result["processed"] + result["errors"] == 2
        assert result["processed"] == 1
        assert result["errors"] == 1

        db_session.refresh(succeeding)
        db_session.refresh(failing)
        waveform_values = {succeeding.waveform_data is not None, failing.waveform_data is not None}
        assert waveform_values == {True, False}

    def test_non_audio_video_content_type_is_excluded(
        self, db_session, waveform_seams, make_media_file
    ):
        make_media_file(content_type="text/plain", filename="notes.txt")

        result = generate_waveform_data_task(file_uuid=None, skip_existing=True)

        assert result == {
            "status": "success",
            "message": "No files need waveform generation",
            "processed": 0,
        }

    def test_video_content_type_is_included(self, db_session, waveform_seams, make_media_file):
        make_media_file(content_type="video/mp4", filename="clip.mp4")

        with patch(_WAVEFORM_GENERATOR, return_value=FakeGenerator(result={"peaks": [1.0]})):
            result = generate_waveform_data_task(file_uuid=None, skip_existing=True)

        assert result["total"] == 1
        assert result["processed"] == 1

    def test_incomplete_status_is_excluded(self, db_session, waveform_seams, make_media_file):
        make_media_file(status=FileStatus.PROCESSING, filename="mid.mp3")

        result = generate_waveform_data_task(file_uuid=None, skip_existing=True)

        assert result["message"] == "No files need waveform generation"

    def test_skip_existing_true_excludes_files_with_waveform_data(
        self, db_session, waveform_seams, make_media_file
    ):
        make_media_file(waveform_data={"peaks": [9.9]}, filename="already.mp3")

        result = generate_waveform_data_task(file_uuid=None, skip_existing=True)

        assert result["message"] == "No files need waveform generation"

    def test_skip_existing_false_reprocesses_files_with_waveform_data(
        self, db_session, waveform_seams, make_media_file
    ):
        media_file = make_media_file(waveform_data={"peaks": [9.9]}, filename="already.mp3")

        # Scoped by file_uuid: an unscoped skip_existing=False call has no
        # waveform_data filter at all, so it also sweeps up whatever COMPLETED
        # audio/video files already exist in the (live dev-stack) database --
        # this test only cares about re-processing behaviour for ITS file.
        new_payload = {"peaks": [5.0, 6.0]}
        with patch(_WAVEFORM_GENERATOR, return_value=FakeGenerator(result=new_payload)):
            result = generate_waveform_data_task(
                file_uuid=str(media_file.uuid), skip_existing=False
            )

        assert result["total"] == 1
        assert result["processed"] == 1
        db_session.refresh(media_file)
        assert media_file.waveform_data == new_payload

    def test_file_uuid_scopes_to_a_single_file(self, db_session, waveform_seams, make_media_file):
        target = make_media_file(filename="target.mp3")
        make_media_file(filename="other.mp3")

        with patch(_WAVEFORM_GENERATOR, return_value=FakeGenerator(result={"peaks": [1.0]})):
            result = generate_waveform_data_task(file_uuid=str(target.uuid), skip_existing=True)

        assert result["total"] == 1
        assert result["processed"] == 1
        db_session.refresh(target)
        assert target.waveform_data == {"peaks": [1.0]}

    def test_unknown_file_uuid_returns_error_status(self, db_session, waveform_seams):
        result = generate_waveform_data_task(file_uuid=str(uuid_pkg.uuid4()), skip_existing=True)

        assert result["status"] == "error"
        assert "message" in result


class TestTriggerWaveformGeneration:
    """``.delay(...)`` is a no-op under ``SKIP_CELERY`` (conftest's
    ``_skip_celery_dispatch`` autouse fixture patches ``Task.apply_async``),
    so this exercises the real wrapper logic: it forwards kwargs and returns
    the dispatched task's id.
    """

    def test_returns_the_dispatched_task_id(self):
        task_id = trigger_waveform_generation(file_uuid="some-uuid", skip_existing=False)
        assert task_id == "test-task-id"

    def test_defaults_are_forwarded(self):
        # No exception, and the fake dispatch id comes back even with defaults.
        task_id = trigger_waveform_generation()
        assert task_id == "test-task-id"
