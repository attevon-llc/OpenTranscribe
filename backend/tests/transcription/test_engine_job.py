"""Tests for engine job dataclasses: serialize/deserialize round-trips.

Pure Python — no GPU, no Docker, no external services required.
Tests cover JobSpec, JobResult, PreprocessResult, and RawInferenceResult.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from app.transcription.engine.job import JobResult
from app.transcription.engine.job import JobSpec
from app.transcription.engine.job import PreprocessResult
from app.transcription.engine.job import RawInferenceResult
from app.transcription.engine.job import RawTranscriptResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_CONFIG: dict = {"model": "large-v3-turbo", "language": "en"}


def _make_preprocess_result(**overrides) -> PreprocessResult:
    defaults: dict = {
        "task_id": "task-abc-123",
        "file_id": 42,
        "user_id": 7,
        "local_wav_path": "/shared/audio/task-abc-123.wav",
        "minio_temp_object": "temp/task-abc-123.wav",
        "audio_duration_s": 187.5,
        "audio_sample_rate": 16000,
        "audio_channels": 1,
        "audio_size_bytes": 6_000_000,
        "vad_regions": [(0.0, 5.2), (7.8, 12.4), (14.0, 20.0)],
        "config_snapshot": dict(_MINIMAL_CONFIG),
        "stage1_timings": {"convert": 0.42, "vad": 0.11},
    }
    defaults.update(overrides)
    return PreprocessResult(**defaults)


def _make_raw_inference_result(**overrides) -> RawInferenceResult:
    defaults: dict = {
        "task_id": "task-abc-123",
        "audio_path": "/shared/audio/task-abc-123.wav",
        "audio_duration_s": 187.5,
        "language": "en",
        "raw_segments": [
            {"start": 0.0, "end": 3.1, "text": "Hello world", "speaker": "SPEAKER_00"},
            {"start": 3.5, "end": 7.2, "text": "How are you", "speaker": "SPEAKER_01"},
        ],
        "diarize_records": [
            {"start": 0.0, "end": 3.1, "speaker": "SPEAKER_00"},
            {"start": 3.5, "end": 7.2, "speaker": "SPEAKER_01"},
        ],
        "overlap_info": {"count": 2, "duration_s": 0.4},
        "native_speaker_embeddings": None,
        "config_snapshot": dict(_MINIMAL_CONFIG),
        "stage_timings": {"whisper": 12.3, "diarize": 4.5},
    }
    defaults.update(overrides)
    return RawInferenceResult(**defaults)


# ---------------------------------------------------------------------------
# TestJobSpec
# ---------------------------------------------------------------------------


class TestJobSpec:
    """Tests for JobSpec construction and factory method."""

    def test_basic_construction(self):
        spec = JobSpec(audio_path="/data/audio/audio.wav", task_id="t1", file_id=10, user_id=3)

        assert spec.audio_path == "/data/audio/audio.wav"
        assert spec.task_id == "t1"
        assert spec.file_id == 10
        assert spec.user_id == 3

    def test_default_ids_are_zero(self):
        spec = JobSpec(audio_path="/data/audio/audio.wav", task_id="t1")

        assert spec.file_id == 0
        assert spec.user_id == 0

    def test_from_audio_path_sets_path_and_task_id(self):
        spec = JobSpec.from_audio_path("/data/audio/sample.wav", task_id="my-task")

        assert spec.audio_path == "/data/audio/sample.wav"
        assert spec.task_id == "my-task"
        assert spec.file_id == 0
        assert spec.user_id == 0

    def test_from_audio_path_default_task_id_is_empty_string(self):
        spec = JobSpec.from_audio_path("/data/audio/sample.wav")

        assert spec.task_id == ""

    def test_from_audio_path_returns_job_spec_instance(self):
        spec = JobSpec.from_audio_path("/data/audio/sample.wav")

        assert isinstance(spec, JobSpec)


# ---------------------------------------------------------------------------
# TestJobResult
# ---------------------------------------------------------------------------


class TestJobResult:
    """Tests for JobResult.to_pipeline_dict()."""

    def test_to_pipeline_dict_always_includes_segments_and_language(self):
        result = JobResult(segments=[{"text": "Hi"}], language="fr")
        d = result.to_pipeline_dict()

        assert "segments" in d
        assert "language" in d
        assert d["language"] == "fr"
        assert d["segments"] == [{"text": "Hi"}]

    def test_to_pipeline_dict_with_overlap(self):
        result = JobResult(
            segments=[],
            language="en",
            overlap_info={"count": 3, "duration_s": 1.2},
        )
        d = result.to_pipeline_dict()

        assert "overlap_info" in d
        assert d["overlap_info"]["count"] == 3

    def test_to_pipeline_dict_no_overlap(self):
        """Empty overlap_info (count absent or 0) must NOT appear in the dict."""
        result = JobResult(segments=[], language="en", overlap_info={})
        d = result.to_pipeline_dict()

        assert "overlap_info" not in d

    def test_to_pipeline_dict_overlap_count_zero_excluded(self):
        result = JobResult(segments=[], language="en", overlap_info={"count": 0})
        d = result.to_pipeline_dict()

        assert "overlap_info" not in d

    def test_to_pipeline_dict_with_embeddings(self):
        embeddings = {"SPEAKER_00": [0.1, 0.2, 0.3], "SPEAKER_01": [0.4, 0.5, 0.6]}
        result = JobResult(segments=[], language="en", native_speaker_embeddings=embeddings)
        d = result.to_pipeline_dict()

        assert "native_speaker_embeddings" in d
        assert d["native_speaker_embeddings"] == embeddings

    def test_to_pipeline_dict_without_embeddings(self):
        """None embeddings must NOT appear in the dict."""
        result = JobResult(segments=[], language="en", native_speaker_embeddings=None)
        d = result.to_pipeline_dict()

        assert "native_speaker_embeddings" not in d

    def test_to_pipeline_dict_stage_timings_not_in_output(self):
        """stage_timings are internal and not forwarded to the pipeline dict."""
        result = JobResult(segments=[], language="en", stage_timings={"gpu": 10.0})
        d = result.to_pipeline_dict()

        assert "stage_timings" not in d

    def test_to_pipeline_dict_all_fields_present(self):
        result = JobResult(
            segments=[{"text": "Hello"}],
            language="es",
            overlap_info={"count": 1, "duration_s": 0.2},
            native_speaker_embeddings={"SPEAKER_00": [0.9]},
        )
        d = result.to_pipeline_dict()

        assert set(d.keys()) == {
            "segments",
            "language",
            "overlap_info",
            "native_speaker_embeddings",
        }


# ---------------------------------------------------------------------------
# TestPreprocessResult
# ---------------------------------------------------------------------------


class TestPreprocessResult:
    """Tests for PreprocessResult.serialize() / deserialize() round-trips."""

    def test_serialize_deserialize_roundtrip(self):
        original = _make_preprocess_result()
        payload = original.serialize()
        restored = PreprocessResult.deserialize(payload)

        assert restored.task_id == original.task_id
        assert restored.file_id == original.file_id
        assert restored.user_id == original.user_id
        assert restored.local_wav_path == original.local_wav_path
        assert restored.minio_temp_object == original.minio_temp_object
        assert restored.audio_duration_s == pytest.approx(original.audio_duration_s)
        assert restored.audio_sample_rate == original.audio_sample_rate
        assert restored.audio_channels == original.audio_channels
        assert restored.audio_size_bytes == original.audio_size_bytes
        assert restored.vad_regions == original.vad_regions
        assert restored.config_snapshot == original.config_snapshot
        assert restored.stage1_timings == original.stage1_timings

    def test_serialize_deserialize_no_vad(self):
        original = _make_preprocess_result(vad_regions=None)
        payload = original.serialize()
        restored = PreprocessResult.deserialize(payload)

        assert restored.vad_regions is None

    def test_vad_regions_restored_as_tuples(self):
        """After JSON round-trip, lists from JSON must be converted back to tuples."""
        original = _make_preprocess_result(vad_regions=[(1.0, 2.5), (3.0, 4.8)])

        # Simulate JSON serialization (JSON converts tuples → lists)
        json_payload = json.dumps(original.serialize())
        deserialized_payload = json.loads(json_payload)
        restored = PreprocessResult.deserialize(deserialized_payload)

        assert restored.vad_regions is not None
        for region in restored.vad_regions:
            assert isinstance(region, tuple), f"Expected tuple, got {type(region)}: {region}"

    def test_vad_region_values_preserved(self):
        regions = [(0.0, 5.2), (7.8, 12.4), (14.0, 20.0)]
        original = _make_preprocess_result(vad_regions=regions)
        payload = original.serialize()
        restored = PreprocessResult.deserialize(payload)

        assert restored.vad_regions is not None
        assert len(restored.vad_regions) == 3
        for orig_region, rest_region in zip(regions, restored.vad_regions, strict=True):
            assert rest_region[0] == pytest.approx(orig_region[0])
            assert rest_region[1] == pytest.approx(orig_region[1])

    def test_serialize_produces_json_serializable_dict(self):
        original = _make_preprocess_result()
        payload = original.serialize()

        # Must not raise
        json_str = json.dumps(payload)
        assert isinstance(json_str, str)

    def test_config_snapshot_preserved(self):
        config = {"model": "large-v3", "language": "fr", "translate": True, "max_speakers": 5}
        original = _make_preprocess_result(config_snapshot=config)
        restored = PreprocessResult.deserialize(original.serialize())

        assert restored.config_snapshot == config

    def test_stage1_timings_preserved(self):
        timings = {"download": 1.2, "convert": 0.55, "vad": 0.08}
        original = _make_preprocess_result(stage1_timings=timings)
        restored = PreprocessResult.deserialize(original.serialize())

        assert restored.stage1_timings == pytest.approx(timings)

    def test_default_stage1_timings_is_empty_dict(self):
        pr = PreprocessResult(
            task_id="t",
            file_id=1,
            user_id=1,
            local_wav_path="/data/audio/x.wav",
            minio_temp_object="",
            audio_duration_s=10.0,
            audio_sample_rate=16000,
            audio_channels=1,
            audio_size_bytes=320000,
            vad_regions=None,
            config_snapshot={},
        )
        assert pr.stage1_timings == {}

    def test_deserialize_missing_optional_fields_use_defaults(self):
        """Deserialize a minimal payload — optional fields must fall back gracefully."""
        minimal: dict = {
            "task_id": "t1",
            "file_id": 1,
            "user_id": 2,
            "local_wav_path": "/data/audio/x.wav",
            "audio_duration_s": 30.0,
            "vad_regions": None,
        }
        restored = PreprocessResult.deserialize(minimal)

        assert restored.minio_temp_object == ""
        assert restored.audio_sample_rate == 16000
        assert restored.audio_channels == 1
        assert restored.audio_size_bytes == 0
        assert restored.config_snapshot == {}
        assert restored.stage1_timings == {}


# ---------------------------------------------------------------------------
# TestRawInferenceResult
# ---------------------------------------------------------------------------


class TestRawInferenceResult:
    """Tests for RawInferenceResult.serialize() / deserialize() round-trips."""

    def test_serialize_deserialize_roundtrip(self):
        original = _make_raw_inference_result(
            overlap_info={"count": 2, "duration_s": 0.4},
            stage_timings={"whisper": 12.3, "diarize": 4.5},
        )
        payload = original.serialize()
        restored = RawInferenceResult.deserialize(payload)

        assert restored.task_id == original.task_id
        assert restored.audio_path == original.audio_path
        assert restored.audio_duration_s == pytest.approx(original.audio_duration_s)
        assert restored.language == original.language
        assert restored.raw_segments == original.raw_segments
        assert restored.diarize_records == original.diarize_records
        assert restored.overlap_info == original.overlap_info
        assert restored.native_speaker_embeddings is None
        assert restored.config_snapshot == original.config_snapshot
        assert restored.stage_timings == pytest.approx(original.stage_timings)

    def test_serialize_no_embeddings(self):
        """None native_speaker_embeddings serializes to None in the dict."""
        original = _make_raw_inference_result(native_speaker_embeddings=None)
        payload = original.serialize()

        assert payload["native_speaker_embeddings"] is None

    def test_serialize_numpy_embeddings(self):
        """numpy arrays are converted to plain lists during serialization."""
        arr_00 = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        arr_01 = np.array([0.4, 0.5, 0.6], dtype=np.float32)
        embeddings = {"SPEAKER_00": arr_00, "SPEAKER_01": arr_01}

        original = _make_raw_inference_result(native_speaker_embeddings=embeddings)
        payload = original.serialize()

        assert payload["native_speaker_embeddings"] is not None
        for key, val in payload["native_speaker_embeddings"].items():
            assert isinstance(val, list), f"Expected list for {key}, got {type(val)}"

    def test_serialize_numpy_embeddings_values_preserved(self):
        arr = np.array([0.1, 0.2, 0.3], dtype=np.float64)
        original = _make_raw_inference_result(
            native_speaker_embeddings={"SPEAKER_00": arr},
        )
        payload = original.serialize()

        result_list = payload["native_speaker_embeddings"]["SPEAKER_00"]
        assert result_list == pytest.approx([0.1, 0.2, 0.3])

    def test_serialize_list_embeddings_preserved(self):
        """Plain list embeddings (already serialized) also pass through correctly."""
        embeddings = {"SPEAKER_00": [0.1, 0.2, 0.3]}
        original = _make_raw_inference_result(native_speaker_embeddings=embeddings)
        payload = original.serialize()

        assert payload["native_speaker_embeddings"]["SPEAKER_00"] == pytest.approx([0.1, 0.2, 0.3])

    def test_diarize_records_round_trip(self):
        """diarize_records list-of-dicts is preserved exactly through serialize/deserialize."""
        records = [
            {"start": 0.0, "end": 3.1, "speaker": "SPEAKER_00"},
            {"start": 3.5, "end": 7.2, "speaker": "SPEAKER_01"},
            {"start": 8.0, "end": 10.5, "speaker": "SPEAKER_00"},
        ]
        original = _make_raw_inference_result(diarize_records=records)
        payload = original.serialize()
        restored = RawInferenceResult.deserialize(payload)

        assert restored.diarize_records == records

    def test_serialize_produces_json_serializable_dict(self):
        original = _make_raw_inference_result()
        payload = original.serialize()

        # Must not raise — all values must be JSON-safe
        json_str = json.dumps(payload)
        assert isinstance(json_str, str)

    def test_serialize_numpy_embeddings_json_serializable(self):
        arr = np.array([0.7, 0.8, 0.9], dtype=np.float32)
        original = _make_raw_inference_result(native_speaker_embeddings={"SPEAKER_00": arr})
        payload = original.serialize()

        json_str = json.dumps(payload)
        assert isinstance(json_str, str)

    def test_deserialize_missing_optional_fields_use_defaults(self):
        """Deserialize a minimal payload — optional fields fall back to safe defaults."""
        minimal: dict = {
            "task_id": "t1",
            "audio_path": "/data/audio/x.wav",
            "audio_duration_s": 60.0,
            "language": "en",
            "raw_segments": [],
        }
        restored = RawInferenceResult.deserialize(minimal)

        assert restored.diarize_records == []
        assert restored.overlap_info == {}
        assert restored.native_speaker_embeddings is None
        assert restored.config_snapshot == {}
        assert restored.stage_timings == {}

    def test_round_trip_through_json(self):
        """Full JSON encode/decode cycle preserves all scalar fields."""
        original = _make_raw_inference_result(
            overlap_info={"count": 1, "duration_s": 0.2},
        )
        json_str = json.dumps(original.serialize())
        restored = RawInferenceResult.deserialize(json.loads(json_str))

        assert restored.task_id == original.task_id
        assert restored.language == original.language
        assert restored.audio_duration_s == pytest.approx(original.audio_duration_s)
        assert restored.raw_segments == original.raw_segments
        assert restored.diarize_records == original.diarize_records
        assert restored.overlap_info == original.overlap_info

    def test_default_stage_timings_is_empty_dict(self):
        rir = RawInferenceResult(
            task_id="t",
            audio_path="/data/audio/x.wav",
            audio_duration_s=10.0,
            language="en",
            raw_segments=[],
            diarize_records=[],
            overlap_info={},
            native_speaker_embeddings=None,
            config_snapshot={},
        )
        assert rir.stage_timings == {}


# ---------------------------------------------------------------------------
# TestRawTranscriptResult
# ---------------------------------------------------------------------------


def _make_raw_transcript_result(**overrides) -> RawTranscriptResult:
    defaults: dict = {
        "task_id": "task-abc-123",
        "audio_path": "",
        "audio_duration_s": 187.5,
        "language": "en",
        "raw_segments": [
            {"start": 0.0, "end": 3.1, "text": "Hello world"},
            {"start": 3.5, "end": 7.2, "text": "How are you"},
        ],
        "local_wav_path": "/tmp/task-abc-123.wav",  # noqa: S108
        "config_snapshot": dict(_MINIMAL_CONFIG),
        "stage_timings": {"transcribe_only": 8.4},
    }
    defaults.update(overrides)
    return RawTranscriptResult(**defaults)


class TestRawTranscriptResult:
    """Tests for RawTranscriptResult.serialize() / deserialize() round-trips.

    RawTranscriptResult is the Stage 2a → Stage 2b handoff in the Phase 4
    multi-GPU split path (ENGINE_GPU_SPLIT=true).  Correct round-trip
    serialization is critical: this object is passed through Celery/Redis
    between the gpu-transcribe and gpu-diarize workers.
    """

    def test_serialize_deserialize_roundtrip(self):
        original = _make_raw_transcript_result()
        payload = original.serialize()
        restored = RawTranscriptResult.deserialize(payload)

        assert restored.task_id == original.task_id
        assert restored.audio_path == original.audio_path
        assert restored.audio_duration_s == pytest.approx(original.audio_duration_s)
        assert restored.language == original.language
        assert restored.raw_segments == original.raw_segments
        assert restored.local_wav_path == original.local_wav_path
        assert restored.config_snapshot == original.config_snapshot
        assert restored.stage_timings == pytest.approx(original.stage_timings)

    def test_serialize_produces_json_serializable_dict(self):
        original = _make_raw_transcript_result()
        payload = original.serialize()

        json_str = json.dumps(payload)
        assert isinstance(json_str, str)

    def test_round_trip_through_json(self):
        """Full JSON encode/decode cycle preserves all fields (Redis transport simulation)."""
        original = _make_raw_transcript_result()
        json_str = json.dumps(original.serialize())
        restored = RawTranscriptResult.deserialize(json.loads(json_str))

        assert restored.task_id == original.task_id
        assert restored.language == original.language
        assert restored.audio_duration_s == pytest.approx(original.audio_duration_s)
        assert restored.raw_segments == original.raw_segments
        assert restored.local_wav_path == original.local_wav_path
        assert restored.config_snapshot == original.config_snapshot

    def test_local_wav_path_preserved(self):
        """local_wav_path must survive serialization — Stage 2b reloads audio from it."""
        path = "/tmp/transcription/abc-123-def-456.wav"  # noqa: S108
        original = _make_raw_transcript_result(local_wav_path=path)
        restored = RawTranscriptResult.deserialize(original.serialize())

        assert restored.local_wav_path == path

    def test_config_snapshot_preserved(self):
        """config_snapshot reconstructs EngineConfig in Stage 2b — must be exact."""
        snapshot = {
            "model_name": "large-v3",
            "device": "cuda",
            "enable_diarization": True,
            "min_speakers": 2,
            "max_speakers": 8,
        }
        original = _make_raw_transcript_result(config_snapshot=snapshot)
        restored = RawTranscriptResult.deserialize(original.serialize())

        assert restored.config_snapshot == snapshot

    def test_merged_stage_timings_preserved(self):
        """Stage 2a timing is preserved so Stage 2b can merge its own timing."""
        timings = {"transcribe_only": 104.8}
        original = _make_raw_transcript_result(stage_timings=timings)
        restored = RawTranscriptResult.deserialize(original.serialize())

        assert restored.stage_timings == pytest.approx(timings)

    def test_deserialize_missing_optional_fields_use_defaults(self):
        """Deserialize a minimal payload — optional fields fall back gracefully."""
        minimal: dict = {
            "audio_duration_s": 60.0,
            "language": "fr",
            "raw_segments": [],
            "local_wav_path": "/tmp/x.wav",  # noqa: S108
        }
        restored = RawTranscriptResult.deserialize(minimal)

        assert restored.task_id is None
        assert restored.audio_path == ""
        assert restored.config_snapshot == {}
        assert restored.stage_timings == {}

    def test_empty_segments_roundtrip(self):
        """Empty segments (no-speech audio) must serialize without error."""
        original = _make_raw_transcript_result(raw_segments=[])
        payload = original.serialize()
        restored = RawTranscriptResult.deserialize(payload)

        assert restored.raw_segments == []

    def test_none_task_id_roundtrip(self):
        """task_id=None is valid for benchmark/script paths."""
        original = _make_raw_transcript_result(task_id=None)
        payload = original.serialize()
        restored = RawTranscriptResult.deserialize(payload)

        assert restored.task_id is None
