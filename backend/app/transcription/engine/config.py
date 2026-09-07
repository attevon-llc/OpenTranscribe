"""EngineConfig: engine-specific settings that wrap TranscriptionConfig."""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from app.core.constants import ENGINE_SHARED_VOLUME_DEFAULT
from app.core.constants import resolve_engine_shared_volume_path

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
    diarizer_backend: str = "native"  # native (default, primary) | pyannote (failover)

    # Stage-specific toggles
    precompute_vad: bool = False  # Phase 3a — Silero VAD in Stage 1
    gpu_split: bool = False  # Phase 4 — separate gpu-transcribe / gpu-diarize queues

    # Boundary acoustic re-check (issue #193, Phase 3). DB-controlled via
    # engine.boundary_acoustic_* SystemSettings (admin Engine panel) → env → defaults.
    # Reassigns short absorbed backchannels by voiceprint inside the GPU stage; off by default.
    boundary_acoustic_recheck_enabled: bool = False
    boundary_acoustic_cosine_margin: float = 0.05
    boundary_acoustic_max_word_dur: float = 1.0

    # Shared-volume handoff path (Opt-3A) — the engine/ namespace of pipeline_scratch
    shared_volume_path: str = ENGINE_SHARED_VOLUME_DEFAULT

    # Internal: wrapped TranscriptionConfig (set by from_environment)
    _transcription_config: TranscriptionConfig | None = field(default=None, repr=False)

    @staticmethod
    def _db_env_float(db, key: str, env: str, default: float) -> float:
        """Resolve a float engine setting from DB → env → default (no crash on junk).

        ``db=None`` (e.g. from_environment / no session) skips the DB read.
        """
        raw: str | None = None
        if db is not None and key:
            from app.services.system_settings_service import get_setting

            raw = get_setting(db, key)
        if raw is None:
            raw = os.getenv(env)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    @classmethod
    def from_db_with_env_fallback(cls, db) -> EngineConfig:
        """Read engine settings from SystemSettings DB, fall back to env vars."""
        from app.services.system_settings_service import get_settings_map

        # Single SELECT for every engine key instead of one round-trip per key.
        vals = get_settings_map(
            db,
            [
                "engine.transcriber_backend",
                "engine.diarizer_backend",
                "engine.gpu_split",
                "engine.precompute_vad",
                "engine.boundary_acoustic_recheck_enabled",
                "engine.boundary_acoustic_cosine_margin",
                "engine.boundary_acoustic_max_word_dur",
                "engine.shared_volume_path",
            ],
        )

        def _bool(key: str, env: str) -> bool:
            v = vals.get(key)
            if v is not None:
                return v.lower() in ("true", "1", "yes", "on")
            return os.getenv(env, "false").lower() == "true"

        def _float(key: str, env: str, default: float) -> float:
            raw = vals.get(key)
            if raw is None:
                raw = os.getenv(env)
            if raw is None or raw == "":
                return default
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        return cls(
            transcriber_backend=(
                vals.get("engine.transcriber_backend")
                or os.getenv("ENGINE_TRANSCRIBER_BACKEND")
                or "faster_whisper"
            ),
            diarizer_backend=(
                vals.get("engine.diarizer_backend")
                or os.getenv("ENGINE_DIARIZER_BACKEND")
                or "native"
            ),
            gpu_split=_bool("engine.gpu_split", "ENGINE_GPU_SPLIT"),
            precompute_vad=_bool("engine.precompute_vad", "ENGINE_PRECOMPUTE_VAD"),
            boundary_acoustic_recheck_enabled=_bool(
                "engine.boundary_acoustic_recheck_enabled",
                "ENGINE_BOUNDARY_ACOUSTIC_RECHECK_ENABLED",
            ),
            boundary_acoustic_cosine_margin=_float(
                "engine.boundary_acoustic_cosine_margin",
                "ENGINE_BOUNDARY_ACOUSTIC_COSINE_MARGIN",
                0.05,
            ),
            boundary_acoustic_max_word_dur=_float(
                "engine.boundary_acoustic_max_word_dur",
                "ENGINE_BOUNDARY_ACOUSTIC_MAX_WORD_DUR",
                1.0,
            ),
            shared_volume_path=(
                vals.get("engine.shared_volume_path") or resolve_engine_shared_volume_path()
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
            # Mirrors tc.diarizer_backend below (single decision point: DB > env > default,
            # resolved once by TranscriptionConfig._resolve_diarizer_backend) rather than
            # re-resolving independently from env alone — this is what to_snapshot() serializes
            # for the split/multi-GPU stages, so it must agree with what ModelManager will
            # actually select.
            diarizer_backend=tc.diarizer_backend,
            precompute_vad=os.getenv("ENGINE_PRECOMPUTE_VAD", "false").lower() == "true",
            gpu_split=os.getenv("ENGINE_GPU_SPLIT", "false").lower() == "true",
            boundary_acoustic_recheck_enabled=os.getenv(
                "ENGINE_BOUNDARY_ACOUSTIC_RECHECK_ENABLED", "false"
            ).lower()
            == "true",
            boundary_acoustic_cosine_margin=cls._db_env_float(
                None, "", "ENGINE_BOUNDARY_ACOUSTIC_COSINE_MARGIN", 0.05
            ),
            boundary_acoustic_max_word_dur=cls._db_env_float(
                None, "", "ENGINE_BOUNDARY_ACOUSTIC_MAX_WORD_DUR", 1.0
            ),
            shared_volume_path=resolve_engine_shared_volume_path(),
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
            "boundary_acoustic_recheck_enabled": self.boundary_acoustic_recheck_enabled,
            "boundary_acoustic_cosine_margin": self.boundary_acoustic_cosine_margin,
            "boundary_acoustic_max_word_dur": self.boundary_acoustic_max_word_dur,
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
            diarizer_backend=snapshot.get("diarizer_backend", "native"),
        )
        engine = cls(
            transcriber_backend=snapshot.get("transcriber_backend", "faster_whisper"),
            diarizer_backend=snapshot.get("diarizer_backend", "native"),
            precompute_vad=snapshot.get("precompute_vad", False),
            gpu_split=snapshot.get("gpu_split", False),
            boundary_acoustic_recheck_enabled=snapshot.get(
                "boundary_acoustic_recheck_enabled", False
            ),
            boundary_acoustic_cosine_margin=snapshot.get("boundary_acoustic_cosine_margin", 0.05),
            boundary_acoustic_max_word_dur=snapshot.get("boundary_acoustic_max_word_dur", 1.0),
            shared_volume_path=snapshot.get("shared_volume_path", ENGINE_SHARED_VOLUME_DEFAULT),
        )
        engine._transcription_config = tc
        return engine
