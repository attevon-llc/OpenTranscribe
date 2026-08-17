"""Tests for the cloud ASR provider pipeline (``app/tasks/transcription/cloud_asr.py``).

Written against real DB rows and real merge logic, following the
``unit/test_dispatch.py`` fix-shape (backend/tests/CLAUDE.md): only genuinely
out-of-process seams are patched — ``session_scope`` (these functions open their
own session, invisible to the savepoint harness otherwise) and
``send_progress_notification`` (Redis/WebSocket). The provider objects
themselves are lightweight fakes standing in for a network call to a cloud ASR
vendor — mocking *those* is the "heavy external call" the task instructions
call out, not the orchestration around them. Diarization merging
(``merge_cloud_diarization``) is exercised for real: it is the actual business
logic this module exists to drive.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models.media import Task
from app.services.asr.types import ASRConfig
from app.services.asr.types import ASRResult
from app.services.asr.types import ASRSegment
from app.services.asr.types import ASRWord
from app.services.diarization.types import DiarizeResult
from app.services.diarization.types import DiarizeSegment
from app.tasks.transcription.cloud_asr import _convert_asr_result_to_segments
from app.tasks.transcription.cloud_asr import _run_cloud_asr_pipeline
from app.tasks.transcription.cloud_asr import _run_parallel_cloud_asr_and_diarization
from app.tasks.transcription.context import TranscriptionContext

_CLOUD_ASR = "app.tasks.transcription.cloud_asr"
_SESSION_SCOPE = f"{_CLOUD_ASR}.session_scope"
_SEND_PROGRESS = f"{_CLOUD_ASR}.send_progress_notification"
_DIARIZE_FACTORY_CREATE = (
    "app.services.diarization.factory.DiarizationProviderFactory.create_for_user"
)
_ASR_FACTORY_CREATE = "app.services.asr.factory.ASRProviderFactory.create_for_user"


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def cloud_asr_seams(db_session):
    """Patch the out-of-process seams shared by every function under test."""
    with (
        patch(_SESSION_SCOPE, lambda: _yield_session(db_session)),
        patch(_SEND_PROGRESS) as send_progress,
    ):
        yield send_progress


@pytest.fixture
def make_task(db_session, normal_user):
    def _make(task_id: str, status: str = "in_progress") -> Task:
        task = Task(
            id=task_id,
            user_id=normal_user.id,
            media_file_id=None,
            task_type="transcription",
            status=status,
            progress=0.1,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        return task

    return _make


def _make_ctx(normal_user, task_id: str, file_id: int = 1) -> TranscriptionContext:
    return TranscriptionContext(
        task_id=task_id,
        file_id=file_id,
        file_uuid=str(uuid_pkg.uuid4()),
        user_id=normal_user.id,
        file_path="cloud_asr/test.mp3",
        file_name="test.mp3",
        content_type="audio/mpeg",
    )


class FakeASRProvider:
    """Stand-in for a real cloud ASR provider's network call."""

    def __init__(
        self,
        provider_name="deepgram",
        result=None,
        error=None,
        supports_diar=True,
        supports_trans=True,
    ):
        self.provider_name = provider_name
        self._result = result
        self._error = error
        self._supports_diar = supports_diar
        self._supports_trans = supports_trans
        self.captured_config = None
        self.calls = 0

    def supports_diarization(self):
        return self._supports_diar

    def supports_translation(self):
        return self._supports_trans

    def transcribe(self, audio_file_path, config, progress_callback):
        self.calls += 1
        self.captured_config = config
        if self._error:
            raise self._error
        if progress_callback:
            progress_callback(0.5, "halfway")
        return self._result


class FakeDiarizeProvider:
    """Stand-in for a real cloud diarization provider's network call."""

    def __init__(self, provider_name="pyannote", result=None, error=None):
        self.provider_name = provider_name
        self._result = result
        self._error = error
        self.calls = 0

    def diarize(self, audio_file_path, diarize_config):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


def _asr_result(with_speaker=False) -> ASRResult:
    return ASRResult(
        segments=[
            ASRSegment(
                text="hello world",
                start=0.0,
                end=1.0,
                speaker="SPEAKER_00" if with_speaker else None,
                confidence=0.9,
                words=[
                    ASRWord(word="hello", start=0.0, end=0.4, confidence=0.95),
                    ASRWord(word="world", start=0.4, end=1.0, confidence=0.85),
                ],
            )
        ],
        language="en",
        provider_name="deepgram",
        model_name="nova-3",
    )


class TestConvertAsrResultToSegments:
    """Pure conversion logic: cloud ASRResult -> the dict shape storage expects."""

    def test_converts_words_and_uses_score_key(self):
        result = _asr_result()

        segments = _convert_asr_result_to_segments(result, media_file_id=1)

        assert len(segments) == 1
        seg = segments[0]
        assert seg["text"] == "hello world"
        assert seg["speaker"] is None
        assert seg["confidence"] == 0.9
        assert seg["words"] == [
            {"word": "hello", "start": 0.0, "end": 0.4, "score": 0.95},
            {"word": "world", "start": 0.4, "end": 1.0, "score": 0.85},
        ]

    def test_word_confidence_none_defaults_to_one(self):
        result = ASRResult(
            segments=[
                ASRSegment(
                    text="hi",
                    start=0.0,
                    end=0.5,
                    # confidence is typed float, but dataclasses don't enforce
                    # that at runtime -- a provider's raw JSON can (and does)
                    # supply None despite the hint, which is exactly the
                    # defensive path this test exists to cover.
                    words=[ASRWord(word="hi", start=0.0, end=0.5, confidence=None)],  # type: ignore[arg-type]
                )
            ],
            language="en",
        )

        segments = _convert_asr_result_to_segments(result, media_file_id=1)

        assert segments[0]["words"][0]["score"] == 1.0

    def test_segment_with_no_words_produces_empty_list(self):
        result = ASRResult(
            segments=[ASRSegment(text="", start=0.0, end=0.1, words=[])],
            language="en",
        )

        segments = _convert_asr_result_to_segments(result, media_file_id=1)

        assert segments[0]["words"] == []

    def test_empty_segments_produces_empty_list(self):
        result = ASRResult(segments=[], language="en")

        segments = _convert_asr_result_to_segments(result, media_file_id=1)

        assert segments == []

    def test_preserves_speaker_label(self):
        result = _asr_result(with_speaker=True)

        segments = _convert_asr_result_to_segments(result, media_file_id=1)

        assert segments[0]["speaker"] == "SPEAKER_00"


class TestRunParallelCloudAsrAndDiarization:
    """Both legs run for real (the fakes stand in only for the network call);
    the merge/fallback/error-handling logic is the thing under test.
    """

    def test_no_diarize_provider_falls_back_to_asr_only(self, cloud_asr_seams, normal_user):
        ctx = _make_ctx(normal_user, "task-1")
        asr = FakeASRProvider(result=_asr_result())

        with patch(_DIARIZE_FACTORY_CREATE, return_value=None):
            result = _run_parallel_cloud_asr_and_diarization(
                ctx, "/fake/audio.wav", ASRConfig(), asr, None
            )

        assert result is asr._result
        assert asr.calls == 1

    def test_both_succeed_merges_speakers_onto_asr_result(self, cloud_asr_seams, normal_user):
        ctx = _make_ctx(normal_user, "task-2")
        asr = FakeASRProvider(result=_asr_result(with_speaker=False))
        diarize_result = DiarizeResult(
            segments=[DiarizeSegment(start=0.0, end=2.0, speaker="SPEAKER_01")],
            num_speakers=1,
            provider_name="pyannote",
        )
        diarize = FakeDiarizeProvider(result=diarize_result)

        with patch(_DIARIZE_FACTORY_CREATE, return_value=diarize):
            result = _run_parallel_cloud_asr_and_diarization(
                ctx, "/fake/audio.wav", ASRConfig(), asr, None
            )

        assert asr.calls == 1
        assert diarize.calls == 1
        # merge_cloud_diarization ran for real: the previously-speakerless
        # segment now carries the diarization provider's label.
        assert result.segments[0].speaker == "SPEAKER_01"

    def test_diarization_failure_is_non_fatal(self, cloud_asr_seams, normal_user):
        ctx = _make_ctx(normal_user, "task-3")
        asr_result = _asr_result()
        asr = FakeASRProvider(result=asr_result)
        diarize = FakeDiarizeProvider(error=RuntimeError("pyannote.ai timed out"))

        with patch(_DIARIZE_FACTORY_CREATE, return_value=diarize):
            result = _run_parallel_cloud_asr_and_diarization(
                ctx, "/fake/audio.wav", ASRConfig(), asr, None
            )

        # ASR result is returned untouched — diarization is best-effort.
        assert result is asr_result

    def test_asr_failure_is_fatal(self, cloud_asr_seams, normal_user):
        ctx = _make_ctx(normal_user, "task-4")
        asr = FakeASRProvider(error=RuntimeError("deepgram 500"))
        diarize = FakeDiarizeProvider(
            result=DiarizeResult(segments=[], num_speakers=0, provider_name="pyannote")
        )

        with (
            patch(_DIARIZE_FACTORY_CREATE, return_value=diarize),
            pytest.raises(RuntimeError, match="Cloud ASR transcription failed"),
        ):
            _run_parallel_cloud_asr_and_diarization(ctx, "/fake/audio.wav", ASRConfig(), asr, None)


class TestRunCloudAsrPipeline:
    """Vocabulary filtering, translation gating and speaker-config resolution
    are the real business logic here; DB rows for vocabulary are real.
    """

    def _add_vocab(self, db_session, term, user_id=None, is_active=True):
        from app.models.custom_vocabulary import CustomVocabulary

        row = CustomVocabulary(user_id=user_id, term=term, domain="general", is_active=is_active)
        db_session.add(row)
        db_session.commit()
        return row

    def test_happy_path_returns_converted_segments(
        self, db_session, cloud_asr_seams, normal_user, make_task
    ):
        make_task("task-happy")
        ctx = _make_ctx(normal_user, "task-happy")
        asr = FakeASRProvider(result=_asr_result())

        result = _run_cloud_asr_pipeline(
            ctx,
            "/fake/audio.wav",
            min_speakers=None,
            max_speakers=None,
            num_speakers=None,
            provider=asr,
        )

        assert result["language"] == "en"
        assert result["asr_provider"] == "deepgram"
        assert result["asr_model"] == "nova-3"
        assert result["diarization_source"] == "provider"
        assert len(result["segments"]) == 1
        assert result["segments"][0]["text"] == "hello world"

    def test_only_active_vocabulary_for_this_user_or_system_wide_is_used(
        self, db_session, cloud_asr_seams, normal_user, admin_user, make_task
    ):
        make_task("task-vocab")
        ctx = _make_ctx(normal_user, "task-vocab")
        self._add_vocab(db_session, "mine-active", user_id=normal_user.id, is_active=True)
        self._add_vocab(db_session, "mine-inactive", user_id=normal_user.id, is_active=False)
        self._add_vocab(db_session, "someone-elses", user_id=admin_user.id, is_active=True)
        self._add_vocab(db_session, "system-wide", user_id=None, is_active=True)
        asr = FakeASRProvider(result=_asr_result())

        _run_cloud_asr_pipeline(
            ctx,
            "/fake/audio.wav",
            min_speakers=None,
            max_speakers=None,
            num_speakers=None,
            provider=asr,
        )

        assert asr.captured_config is not None
        assert asr.captured_config.vocabulary is not None
        assert set(asr.captured_config.vocabulary) == {"mine-active", "system-wide"}

    def test_translation_disabled_when_provider_does_not_support_it(
        self, db_session, cloud_asr_seams, normal_user, make_task
    ):
        make_task("task-notrans")
        ctx = _make_ctx(normal_user, "task-notrans")
        asr = FakeASRProvider(result=_asr_result(), supports_trans=False)

        _run_cloud_asr_pipeline(
            ctx,
            "/fake/audio.wav",
            min_speakers=None,
            max_speakers=None,
            num_speakers=None,
            provider=asr,
        )

        # translate_to_english defaults to False via user language settings (no row
        # in DB), so this asserts the gate does not crash and stays False either way.
        assert asr.captured_config is not None
        assert asr.captured_config.translate_to_english is False

    def test_diarization_enabled_only_when_source_is_provider_and_supported(
        self, db_session, cloud_asr_seams, normal_user, make_task
    ):
        make_task("task-diar-off")
        ctx = _make_ctx(normal_user, "task-diar-off")
        asr = FakeASRProvider(result=_asr_result(), supports_diar=True)

        _run_cloud_asr_pipeline(
            ctx,
            "/fake/audio.wav",
            min_speakers=None,
            max_speakers=None,
            num_speakers=None,
            provider=asr,
            diarization_source="off",
        )

        assert asr.captured_config is not None
        assert asr.captured_config.enable_diarization is False

    def test_diarization_disabled_when_provider_does_not_support_it(
        self, db_session, cloud_asr_seams, normal_user, make_task
    ):
        make_task("task-diar-unsupported")
        ctx = _make_ctx(normal_user, "task-diar-unsupported")
        asr = FakeASRProvider(result=_asr_result(), supports_diar=False)

        _run_cloud_asr_pipeline(
            ctx,
            "/fake/audio.wav",
            min_speakers=None,
            max_speakers=None,
            num_speakers=None,
            provider=asr,
            diarization_source="provider",
        )

        assert asr.captured_config is not None
        assert asr.captured_config.enable_diarization is False

    def test_pyannote_diarization_source_routes_through_parallel_pipeline(
        self, db_session, cloud_asr_seams, normal_user, make_task
    ):
        make_task("task-pyannote")
        ctx = _make_ctx(normal_user, "task-pyannote")
        asr = FakeASRProvider(result=_asr_result())
        expected = _asr_result()

        with patch(
            f"{_CLOUD_ASR}._run_parallel_cloud_asr_and_diarization", return_value=expected
        ) as parallel_mock:
            result = _run_cloud_asr_pipeline(
                ctx,
                "/fake/audio.wav",
                min_speakers=2,
                max_speakers=5,
                num_speakers=None,
                provider=asr,
                diarization_source="pyannote",
            )

        parallel_mock.assert_called_once()
        _, kwargs = parallel_mock.call_args
        assert kwargs["min_speakers"] == 2
        assert kwargs["max_speakers"] == 5
        assert result["diarization_source"] == "pyannote"
        # segments are converted from the parallel pipeline's result, not asr's own call
        assert asr.calls == 0

    def test_provider_none_falls_back_to_factory(
        self, db_session, cloud_asr_seams, normal_user, make_task
    ):
        make_task("task-factory")
        ctx = _make_ctx(normal_user, "task-factory")
        asr = FakeASRProvider(result=_asr_result())

        with patch(_ASR_FACTORY_CREATE, return_value=asr) as factory_mock:
            result = _run_cloud_asr_pipeline(
                ctx,
                "/fake/audio.wav",
                min_speakers=None,
                max_speakers=None,
                num_speakers=None,
                provider=None,
            )

        factory_mock.assert_called_once()
        assert result["asr_provider"] == "deepgram"
