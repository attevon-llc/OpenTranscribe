"""Job dataclasses: spec, result, and inter-stage handoff types.

JobSpec → run_preprocess() → PreprocessResult
PreprocessResult → run_gpu_stage() → RawInferenceResult        (single-GPU)
PreprocessResult → run_transcribe_only() → RawTranscriptResult (multi-GPU Stage 2a)
RawTranscriptResult → run_diarize_only() → RawInferenceResult  (multi-GPU Stage 2b)
RawInferenceResult → run_cpu_finalize() → JobResult
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from typing import Any


@dataclass
class JobSpec:
    """Input spec for a single transcription job."""

    audio_path: str
    task_id: str
    file_id: int = 0
    user_id: int = 0

    @classmethod
    def from_audio_path(cls, audio_path: str, task_id: str = "") -> JobSpec:
        return cls(audio_path=audio_path, task_id=task_id)


@dataclass
class JobResult:
    """Final output from Engine.process() / run_cpu_finalize().

    Shape is a superset of what TranscriptionPipeline.process() returns,
    so to_pipeline_dict() can be used as a drop-in replacement.
    """

    segments: list[dict]
    language: str
    overlap_info: dict = field(default_factory=dict)
    native_speaker_embeddings: dict[str, Any] | None = None
    # Per-speaker gender when the diarization engine produced it (sidecar path).
    speaker_gender: dict | None = None
    stage_timings: dict[str, float] = field(default_factory=dict)

    def to_pipeline_dict(self) -> dict:
        """Return the dict shape TranscriptionPipeline.process() returns."""
        result: dict = {
            "segments": self.segments,
            "language": self.language,
        }
        if self.overlap_info.get("count", 0) > 0:
            result["overlap_info"] = self.overlap_info
        if self.native_speaker_embeddings:
            result["native_speaker_embeddings"] = self.native_speaker_embeddings
        if self.speaker_gender:
            result["speaker_gender"] = self.speaker_gender
        return result


@dataclass
class PreprocessResult:
    """Stage 1 → Stage 2 handoff. Serializable through Redis.

    Size: ~1 KB per file (no audio bytes — paths + metadata only).
    """

    task_id: str
    file_id: int
    user_id: int
    local_wav_path: str  # shared-volume WAV path; Stage 2 mmap-loads
    minio_temp_object: str  # fallback when local volume unavailable
    audio_duration_s: float
    audio_sample_rate: int  # always 16000 after preprocess
    audio_channels: int  # always 1 after preprocess
    audio_size_bytes: int
    vad_regions: list[tuple[float, float]] | None  # [(start_s, end_s), ...] if precompute_vad
    config_snapshot: dict  # frozen EngineConfig fields for Stage 2 + 3
    stage1_timings: dict[str, float] = field(default_factory=dict)

    def serialize(self) -> dict:
        return {
            "task_id": self.task_id,
            "file_id": self.file_id,
            "user_id": self.user_id,
            "local_wav_path": self.local_wav_path,
            "minio_temp_object": self.minio_temp_object,
            "audio_duration_s": self.audio_duration_s,
            "audio_sample_rate": self.audio_sample_rate,
            "audio_channels": self.audio_channels,
            "audio_size_bytes": self.audio_size_bytes,
            "vad_regions": self.vad_regions,
            "config_snapshot": self.config_snapshot,
            "stage1_timings": self.stage1_timings,
        }

    @classmethod
    def deserialize(cls, payload: dict) -> PreprocessResult:
        vad = payload.get("vad_regions")
        if vad is not None:
            vad = [tuple(r) for r in vad]  # type: ignore[assignment]
        return cls(
            task_id=payload["task_id"],
            file_id=payload["file_id"],
            user_id=payload["user_id"],
            local_wav_path=payload["local_wav_path"],
            minio_temp_object=payload.get("minio_temp_object", ""),
            audio_duration_s=payload["audio_duration_s"],
            audio_sample_rate=payload.get("audio_sample_rate", 16000),
            audio_channels=payload.get("audio_channels", 1),
            audio_size_bytes=payload.get("audio_size_bytes", 0),
            vad_regions=vad,
            config_snapshot=payload.get("config_snapshot", {}),
            stage1_timings=payload.get("stage1_timings", {}),
        )


@dataclass
class RawInferenceResult:
    """Serializable handoff from GPU stage to CPU stage.

    All fields are JSON-safe (numpy arrays converted to lists at boundary).
    Size budget: 0.6–2.5 MB for a 4.7 h file — under Celery/Redis limits.
    """

    task_id: str
    audio_path: str  # for re-loading if needed
    audio_duration_s: float
    language: str
    raw_segments: list[dict]  # Whisper segments before dedup/speaker-assignment
    diarize_records: list[dict]  # DiarizeResult.to_records()
    overlap_info: dict
    native_speaker_embeddings: dict[str, list[float]] | None  # numpy → list at boundary
    config_snapshot: dict
    stage_timings: dict[str, float] = field(default_factory=dict)
    # Per-speaker gender from the sidecar, classified off the same audio as diarization.
    # None when the engine did not produce it (fork path, or feature disabled).
    speaker_gender: dict | None = None

    def serialize(self) -> dict:
        emb = None
        if self.native_speaker_embeddings is not None:
            emb = {
                k: v.tolist() if hasattr(v, "tolist") else list(v)
                for k, v in self.native_speaker_embeddings.items()
            }
        return {
            "task_id": self.task_id,
            "audio_path": self.audio_path,
            "audio_duration_s": self.audio_duration_s,
            "language": self.language,
            "raw_segments": self.raw_segments,
            "diarize_records": self.diarize_records,
            "overlap_info": self.overlap_info,
            "native_speaker_embeddings": emb,
            "speaker_gender": self.speaker_gender,
            "config_snapshot": self.config_snapshot,
            "stage_timings": self.stage_timings,
        }

    @classmethod
    def deserialize(cls, payload: dict) -> RawInferenceResult:
        return cls(
            task_id=payload["task_id"],
            audio_path=payload["audio_path"],
            audio_duration_s=payload["audio_duration_s"],
            language=payload["language"],
            raw_segments=payload["raw_segments"],
            diarize_records=payload.get("diarize_records", []),
            overlap_info=payload.get("overlap_info", {}),
            native_speaker_embeddings=payload.get("native_speaker_embeddings"),
            speaker_gender=payload.get("speaker_gender"),
            config_snapshot=payload.get("config_snapshot", {}),
            stage_timings=payload.get("stage_timings", {}),
        )

    @staticmethod
    def _json_size(payload: dict) -> int:
        return len(json.dumps(payload))


@dataclass
class RawTranscriptResult:
    """Stage 2a (gpu-transcribe) → Stage 2b (gpu-diarize) handoff.

    Carries the raw Whisper output so the diarize worker can reload the audio
    and run PyAnnote without re-running transcription.  All fields are
    JSON-safe (no numpy arrays at boundary).
    """

    task_id: str | None
    audio_path: str  # empty string when shared-volume WAV was used
    audio_duration_s: float
    language: str
    raw_segments: list[dict]  # from transcriber.transcribe()["segments"]
    local_wav_path: str  # diarizer reloads audio from this path
    config_snapshot: dict
    stage_timings: dict[str, float]

    def serialize(self) -> dict:
        return {
            "task_id": self.task_id,
            "audio_path": self.audio_path,
            "audio_duration_s": self.audio_duration_s,
            "language": self.language,
            "raw_segments": self.raw_segments,
            "local_wav_path": self.local_wav_path,
            "config_snapshot": self.config_snapshot,
            "stage_timings": self.stage_timings,
        }

    @classmethod
    def deserialize(cls, d: dict) -> RawTranscriptResult:
        return cls(
            task_id=d.get("task_id"),
            audio_path=d.get("audio_path", ""),
            audio_duration_s=d["audio_duration_s"],
            language=d["language"],
            raw_segments=d["raw_segments"],
            local_wav_path=d["local_wav_path"],
            config_snapshot=d.get("config_snapshot", {}),
            stage_timings=d.get("stage_timings", {}),
        )
