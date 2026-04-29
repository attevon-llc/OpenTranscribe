"""EngineConfig: engine-specific settings that wrap TranscriptionConfig."""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.transcription.config import TranscriptionConfig


@dataclass
class EngineConfig:
    """Combined engine + transcription config.

    Engine-specific settings live here; model/hardware settings delegate
    to a wrapped TranscriptionConfig resolved via from_environment().
    """

    # Backend selection
    transcriber_backend: str = "faster_whisper"  # faster_whisper | whisperx | cloud
    diarizer_backend: str = "pyannote"  # pyannote (only supported in v1)

    # Stage-specific toggles
    precompute_vad: bool = False  # Phase 3a — Silero VAD in Stage 1
    gpu_split: bool = False  # Phase 4 — separate gpu-transcribe / gpu-diarize queues

    # Shared-volume handoff path (Opt-3A) — /tmp is always world-writable in containers
    shared_volume_path: str = "/tmp"  # noqa: S108  # nosec B108

    # Internal: wrapped TranscriptionConfig (set by from_environment)
    _transcription_config: TranscriptionConfig | None = field(default=None, repr=False)

    @classmethod
    def from_db_with_env_fallback(cls, db) -> EngineConfig:
        """Read engine settings from SystemSettings DB, fall back to env vars."""
        from app.services.system_settings_service import get_setting
        from app.services.system_settings_service import get_setting_bool

        return cls(
            transcriber_backend=(
                get_setting(db, "engine.transcriber_backend")
                or os.getenv("ENGINE_TRANSCRIBER_BACKEND")
                or "faster_whisper"
            ),
            diarizer_backend=(
                get_setting(db, "engine.diarizer_backend")
                or os.getenv("ENGINE_DIARIZER_BACKEND")
                or "pyannote"
            ),
            gpu_split=get_setting_bool(
                db,
                "engine.gpu_split",
                default=os.getenv("ENGINE_GPU_SPLIT", "false").lower() == "true",
            ),
            precompute_vad=get_setting_bool(
                db,
                "engine.precompute_vad",
                default=os.getenv("ENGINE_PRECOMPUTE_VAD", "false").lower() == "true",
            ),
            shared_volume_path=(
                get_setting(db, "engine.shared_volume_path")
                or os.getenv("ENGINE_SHARED_VOLUME_PATH")
                or "/tmp"  # noqa: S108  # nosec B108
            ),
        )

    @classmethod
    def from_environment(cls, **overrides) -> EngineConfig:
        """Build EngineConfig from env vars, wrapping TranscriptionConfig."""
        from app.transcription.config import TranscriptionConfig

        # Extract TranscriptionConfig overrides (keys that TranscriptionConfig knows about)
        tc_keys = {f.name for f in TranscriptionConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        tc_overrides = {k: v for k, v in overrides.items() if k in tc_keys}
        engine_overrides = {k: v for k, v in overrides.items() if k not in tc_keys}

        tc = TranscriptionConfig.from_environment(**tc_overrides)

        engine = cls(
            transcriber_backend=os.getenv("ENGINE_TRANSCRIBER_BACKEND", "faster_whisper"),
            diarizer_backend=os.getenv("ENGINE_DIARIZER_BACKEND", "pyannote"),
            precompute_vad=os.getenv("ENGINE_PRECOMPUTE_VAD", "false").lower() == "true",
            gpu_split=os.getenv("ENGINE_GPU_SPLIT", "false").lower() == "true",
            shared_volume_path=os.getenv("ENGINE_SHARED_VOLUME_PATH", "/tmp"),  # noqa: S108  # nosec B108
        )
        for k, v in engine_overrides.items():
            if hasattr(engine, k):
                setattr(engine, k, v)
        engine._transcription_config = tc
        return engine

    @property
    def transcription_config(self) -> TranscriptionConfig:
        if self._transcription_config is None:
            from app.transcription.config import TranscriptionConfig

            self._transcription_config = TranscriptionConfig.from_environment()
        return self._transcription_config

    def to_snapshot(self) -> dict:
        """Serialize engine-side config fields for cross-stage handoff."""
        tc = self.transcription_config
        return {
            "transcriber_backend": self.transcriber_backend,
            "diarizer_backend": self.diarizer_backend,
            "precompute_vad": self.precompute_vad,
            "gpu_split": self.gpu_split,
            "shared_volume_path": self.shared_volume_path,
            # TranscriptionConfig fields needed downstream
            "model_name": tc.model_name,
            "device": tc.device,
            "diarization_device": tc.diarization_device,
            "compute_type": tc.compute_type,
            "batch_size": tc.batch_size,
            "beam_size": tc.beam_size,
            "concurrent_requests": tc.concurrent_requests,
            "enable_diarization": tc.enable_diarization,
            "enable_dedup": tc.enable_dedup,
            "enable_native_embeddings": tc.enable_native_embeddings,
            "enable_overlap_detection": tc.enable_overlap_detection,
            "source_language": tc.source_language,
            "translate_to_english": tc.translate_to_english,
            "min_speakers": tc.min_speakers,
            "max_speakers": tc.max_speakers,
            "num_speakers": tc.num_speakers,
            "overlap_min_duration": tc.overlap_min_duration,
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> EngineConfig:
        """Reconstruct a partial EngineConfig from a cross-stage snapshot.

        Note: the wrapped TranscriptionConfig is rebuilt from snapshot fields;
        DB / env reads are skipped (we use pinned values from Stage 1).
        """
        from app.transcription.config import TranscriptionConfig

        tc = TranscriptionConfig(
            model_name=snapshot.get("model_name", "large-v3-turbo"),
            device=snapshot.get("device", "cuda"),
            diarization_device=snapshot.get("diarization_device", "cuda"),
            compute_type=snapshot.get("compute_type", "float16"),
            batch_size=snapshot.get("batch_size", 16),
            beam_size=snapshot.get("beam_size", 5),
            concurrent_requests=snapshot.get("concurrent_requests", 1),
            enable_diarization=snapshot.get("enable_diarization", True),
            enable_dedup=snapshot.get("enable_dedup", True),
            enable_native_embeddings=snapshot.get("enable_native_embeddings", True),
            enable_overlap_detection=snapshot.get("enable_overlap_detection", True),
            source_language=snapshot.get("source_language", "auto"),
            translate_to_english=snapshot.get("translate_to_english", False),
            min_speakers=snapshot.get("min_speakers", 1),
            max_speakers=snapshot.get("max_speakers", 20),
            num_speakers=snapshot.get("num_speakers"),
            overlap_min_duration=snapshot.get("overlap_min_duration", 0.25),
        )
        engine = cls(
            transcriber_backend=snapshot.get("transcriber_backend", "faster_whisper"),
            diarizer_backend=snapshot.get("diarizer_backend", "pyannote"),
            precompute_vad=snapshot.get("precompute_vad", False),
            gpu_split=snapshot.get("gpu_split", False),
            shared_volume_path=snapshot.get("shared_volume_path", "/tmp"),  # noqa: S108  # nosec B108
        )
        engine._transcription_config = tc
        return engine
