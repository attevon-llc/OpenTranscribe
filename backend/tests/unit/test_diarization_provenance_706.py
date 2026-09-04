"""Persistence of the engine that ACTUALLY served diarization (issue #706).

Before this file, ``media_file.diarization_provider`` was never written for a locally
diarized file — the only producer was ``diarization_merge.py``'s in-memory
``ASRResult.metadata`` dict on the cloud-ASR merge path (covered by
``test_diarization_merge.py``, unchanged here). Verified live: native diarization ran
(``native diarization done in 3.1s: ...`` in the worker log) and the column recorded NULL.

The fix threads a resolved ``(provider, model)`` pair from the diarizer instance that
actually ran — never from ``tc.diarizer_backend``, which only names what was CONFIGURED —
through ``engine/job.py``'s dataclasses into ``finalize.py`` and ``storage.py``. This file
pins three layers of that chain:

1. The diarizer objects themselves (``diarizer_native.py`` / ``diarizer.py``) record the
   right ``last_provider``/``last_model`` after a call, including the native engine's
   OWN internal PyAnnote failover — the exact case the issue calls out as the one a naive
   "record the configured backend" fix would get wrong.
2. ``engine/job.py``'s ``JobResult``/``RawInferenceResult`` carry those two fields through
   ``serialize``/``deserialize``/``to_pipeline_dict`` unchanged.
3. ``finalize.py`` -> ``storage.py`` persists the resolved value onto the real
   ``MediaFile`` row (queryable per-file via the same ORM the file-detail API reads), for
   both a native-served run and a pyannote-fallback-served run, and never on a disabled run.

Vocabulary: ``diarization_provider`` is ``"native"``, ``"pyannote"``, or ``None``
(disabled, or never resolved). There is no separate "fallback" string — a fallback run is
``"pyannote"``, indistinguishable from a deployment configured pyannote directly, which is
correct: both mean "PyAnnote actually ran this job," and the FROM-native context is only
interesting operationally (a log line), not as a persisted third state.
"""

from __future__ import annotations

import socket
import uuid as uuid_pkg
from contextlib import contextmanager

import numpy as np
import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.tasks.transcription import finalize as finalize_mod
from app.tasks.transcription.context import TranscriptionContext
from app.transcription.config import TranscriptionConfig
from app.transcription.diarize_result import DiarizeResult
from app.transcription.diarizer import SpeakerDiarizer
from app.transcription.diarizer_native import NATIVE_MODEL_NAME
from app.transcription.diarizer_native import NativeSpeakerDiarizer
from app.transcription.engine.job import JobResult
from app.transcription.engine.job import RawInferenceResult

pytestmark = pytest.mark.unit


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# 1. The diarizer objects resolve their own identity
# ---------------------------------------------------------------------------


class _FakePyannoteFallback:
    """Stand-in for the in-process PyAnnote fork: no GPU weights, real behaviour."""

    def __init__(self, config):
        self.config = config
        self.is_loaded = False
        self._model_name = "pyannote/speaker-diarization-community-1"

    def load_model(self) -> None:
        self.is_loaded = True

    def unload_model(self) -> None:
        self.is_loaded = False

    def diarize(self, audio):
        return (
            DiarizeResult(
                start=np.array([0.0]),
                end=np.array([1.0]),
                speaker=np.array(["SPEAKER_00"], dtype=object),
            ),
            {"count": 0, "duration": 0.0, "regions": []},
            {"SPEAKER_00": np.array([0.1, 0.2], dtype=np.float32)},
        )


class TestNativeSpeakerDiarizerIdentity:
    def test_a_successful_native_call_records_native_and_its_model(self, monkeypatch):
        config = TranscriptionConfig(diarizer_backend="native")
        native = NativeSpeakerDiarizer(config, base_url="http://127.0.0.1:1")
        native.is_loaded = True

        def _fake_post_own_copy(audio, timeout):
            return (
                {
                    "exclusive_segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
                    "segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
                    "centroids": [],
                    "num_speakers": 1,
                },
                None,
            )

        monkeypatch.setattr(native, "_post_own_copy", _fake_post_own_copy)

        audio = np.zeros(16000, dtype=np.float32)
        result, _overlap, _emb = native.diarize(audio)

        assert isinstance(result, DiarizeResult)
        assert native.last_provider == "native"
        assert native.last_model == NATIVE_MODEL_NAME

    def test_an_internal_pyannote_failover_records_pyannote_not_native(self, monkeypatch):
        """The exact case a naive fix keyed on tc.diarizer_backend would get wrong.

        The sidecar is genuinely unreachable (closed port, real ECONNREFUSED) and
        ``diarize()`` falls back internally — the caller never sees an exception, only a
        result — so the resolved identity can ONLY come from the object that did the work.
        """
        closed_port = _free_port()
        monkeypatch.setattr("app.transcription.diarizer.SpeakerDiarizer", _FakePyannoteFallback)
        config = TranscriptionConfig(diarizer_backend="native")
        native = NativeSpeakerDiarizer(config, base_url=f"http://127.0.0.1:{closed_port}")
        native.is_loaded = True

        audio = np.zeros(16000, dtype=np.float32)
        result, _overlap, _emb = native.diarize(audio)

        assert isinstance(result, DiarizeResult)
        assert native.last_provider == "pyannote", (
            "tc.diarizer_backend is still 'native' here — reading THAT instead of the "
            "diarizer's own resolved state would reproduce issue #706"
        )
        assert native.last_model == "pyannote/speaker-diarization-community-1"

    def test_recovering_after_a_failover_reports_native_again(self, monkeypatch):
        """last_provider is per-call state, not sticky once a fallback has fired."""
        config = TranscriptionConfig(diarizer_backend="native")
        native = NativeSpeakerDiarizer(config, base_url="http://127.0.0.1:1")
        native.is_loaded = True
        native.last_provider = "pyannote"
        native.last_model = "pyannote/speaker-diarization-community-1"

        def _fake_post_own_copy(audio, timeout):
            return (
                {
                    "exclusive_segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
                    "segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
                    "centroids": [],
                    "num_speakers": 1,
                },
                None,
            )

        monkeypatch.setattr(native, "_post_own_copy", _fake_post_own_copy)

        native.diarize(np.zeros(16000, dtype=np.float32))

        assert native.last_provider == "native"
        assert native.last_model == NATIVE_MODEL_NAME


class TestSpeakerDiarizerIdentity:
    def test_last_provider_is_always_pyannote(self):
        assert SpeakerDiarizer.last_provider == "pyannote"

    def test_last_model_reflects_whichever_model_load_model_actually_loaded(self):
        config = TranscriptionConfig(diarizer_backend="pyannote")
        diarizer = SpeakerDiarizer(config)
        assert diarizer.last_model is None, "nothing loaded yet"

        diarizer._model_name = "pyannote/speaker-diarization-3.1"  # the v3 fallback path
        assert diarizer.last_model == "pyannote/speaker-diarization-3.1"


# ---------------------------------------------------------------------------
# 2. engine/job.py dataclasses carry the two fields through unchanged
# ---------------------------------------------------------------------------


class TestJobResultAndRawInferenceResultRoundTrip:
    def test_job_result_to_pipeline_dict_includes_resolved_provider_and_model(self):
        jr = JobResult(
            segments=[{"start": 0.0, "end": 1.0, "text": "hi"}],
            language="en",
            diarization_provider="native",
            diarization_model=NATIVE_MODEL_NAME,
        )
        out = jr.to_pipeline_dict()
        assert out["diarization_provider"] == "native"
        assert out["diarization_model"] == NATIVE_MODEL_NAME

    def test_job_result_omits_the_keys_when_diarization_did_not_run(self):
        jr = JobResult(segments=[], language="en")
        out = jr.to_pipeline_dict()
        assert "diarization_provider" not in out
        assert "diarization_model" not in out

    def test_raw_inference_result_serialize_deserialize_round_trip(self):
        raw = RawInferenceResult(
            task_id="t1",
            audio_path="",
            audio_duration_s=10.0,
            language="en",
            raw_segments=[],
            diarize_records=[],
            overlap_info={},
            native_speaker_embeddings=None,
            config_snapshot={},
            diarization_provider="pyannote",
            diarization_model="pyannote/speaker-diarization-community-1",
        )
        payload = raw.serialize()
        assert payload["diarization_provider"] == "pyannote"
        assert payload["diarization_model"] == "pyannote/speaker-diarization-community-1"

        restored = RawInferenceResult.deserialize(payload)
        assert restored.diarization_provider == "pyannote"
        assert restored.diarization_model == "pyannote/speaker-diarization-community-1"

    def test_raw_inference_result_deserialize_defaults_to_none_for_old_payloads(self):
        """A payload from before this field existed (a redelivered Celery message,
        or a payload serialized by an older worker during a rolling deploy) must not
        KeyError."""
        restored = RawInferenceResult.deserialize(
            {
                "task_id": "t1",
                "audio_path": "",
                "audio_duration_s": 1.0,
                "language": "en",
                "raw_segments": [],
            }
        )
        assert restored.diarization_provider is None
        assert restored.diarization_model is None


# ---------------------------------------------------------------------------
# 3. finalize.py -> storage.py persists the resolved value on the real row
# ---------------------------------------------------------------------------


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def finalize_seams(db_session, monkeypatch):
    """Patch finalize.py's out-of-process seams: DB session, notifications, background work.

    Follows the ``test_cpu_task.py`` fix-shape (backend/tests/CLAUDE.md): only genuinely
    out-of-process work is patched (websocket notifications, the background thread this
    function fires off for embeddings/indexing) — the speaker/segment/status DB writes run
    for real against the test session.
    """
    monkeypatch.setattr(finalize_mod, "session_scope", lambda: _yield_session(db_session))
    monkeypatch.setattr(finalize_mod, "send_progress_notification", lambda *a, **k: None)
    monkeypatch.setattr(finalize_mod, "send_transcript_ready_notification", lambda *a, **k: None)
    # The background thread does its own embedding-matching/indexing DB work on a second
    # session this test does not control; replacing it with a no-op keeps the test scoped
    # to the CRITICAL path this issue is about.
    monkeypatch.setattr(finalize_mod, "_run_post_gpu_background", lambda *a, **k: None)


@pytest.fixture
def processing_media_file(db_session, normal_user) -> MediaFile:
    mf = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename="finalize_706_test.mp3",
        storage_path=f"user_{normal_user.id}/finalize_706_test.mp3",
        file_size=2048,
        content_type="audio/mpeg",
        status=FileStatus.PROCESSING,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def _ctx(media_file: MediaFile, normal_user) -> TranscriptionContext:
    return TranscriptionContext(
        task_id=f"task-706-{media_file.id}",
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=normal_user.id,
        file_path="x/finalize_706_test.mp3",
        file_name="finalize_706_test.mp3",
        content_type="audio/mpeg",
    )


class TestProcessAndSaveCriticalPersistsResolvedDiarization:
    """``_process_and_save_critical`` — the Engine (gpu-split-aware) finalize path."""

    def test_native_served_run_persists_native_on_the_row(
        self, db_session, finalize_seams, normal_user, processing_media_file
    ):
        result = {
            "segments": [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_00"}],
            "language": "en",
            "asr_provider": "local",
            "asr_model": "large-v3-turbo",
            "diarization_provider": "native",
            "diarization_model": NATIVE_MODEL_NAME,
            "diarization_disabled": False,
        }

        finalize_mod._process_and_save_critical(
            _ctx(processing_media_file, normal_user), result, preprocess_context={}
        )

        db_session.refresh(processing_media_file)
        assert processing_media_file.status == FileStatus.COMPLETED
        assert processing_media_file.diarization_provider == "native"
        assert processing_media_file.diarization_model == NATIVE_MODEL_NAME

    def test_pyannote_fallback_served_run_persists_pyannote_on_the_row(
        self, db_session, finalize_seams, normal_user, processing_media_file
    ):
        result = {
            "segments": [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_00"}],
            "language": "en",
            "asr_provider": "local",
            "asr_model": "large-v3-turbo",
            "diarization_provider": "pyannote",
            "diarization_model": "pyannote/speaker-diarization-community-1",
            "diarization_disabled": False,
        }

        finalize_mod._process_and_save_critical(
            _ctx(processing_media_file, normal_user), result, preprocess_context={}
        )

        db_session.refresh(processing_media_file)
        assert processing_media_file.status == FileStatus.COMPLETED
        assert processing_media_file.diarization_provider == "pyannote"

    def test_diarization_disabled_run_leaves_provider_unset(
        self, db_session, finalize_seams, normal_user, processing_media_file
    ):
        """A user who explicitly skipped diarization must not get a fabricated engine name."""
        result = {
            "segments": [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_00"}],
            "language": "en",
            "asr_provider": "local",
            "asr_model": "large-v3-turbo",
            "diarization_disabled": True,
        }

        finalize_mod._process_and_save_critical(
            _ctx(processing_media_file, normal_user), result, preprocess_context={}
        )

        db_session.refresh(processing_media_file)
        assert processing_media_file.status == FileStatus.COMPLETED
        assert processing_media_file.diarization_provider is None


class TestProcessTranscriptionResultPersistsResolvedDiarization:
    """``_process_transcription_result`` — the legacy/CPU-fallback finalize path.

    Same derivation logic, duplicated verbatim in the source (see finalize.py) — covered
    separately so a future de-duplication cannot silently drop one branch's fix.
    """

    def test_native_served_run_persists_native_on_the_row(
        self, db_session, finalize_seams, normal_user, processing_media_file
    ):
        result = {
            "segments": [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_00"}],
            "language": "en",
            "asr_provider": "local",
            "asr_model": "large-v3-turbo",
            "diarization_provider": "native",
            "diarization_model": NATIVE_MODEL_NAME,
            "diarization_disabled": False,
        }

        finalize_mod._process_transcription_result(
            _ctx(processing_media_file, normal_user), result, audio_file_path="x.wav"
        )

        db_session.refresh(processing_media_file)
        assert processing_media_file.status == FileStatus.COMPLETED
        assert processing_media_file.diarization_provider == "native"
        assert processing_media_file.diarization_model == NATIVE_MODEL_NAME
