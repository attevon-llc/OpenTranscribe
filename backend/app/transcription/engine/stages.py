"""Engine stage implementations.

Stages mirror the existing Celery chain:
- _PreprocessStage     → cpu queue Stage 1
- _GpuStage            → gpu queue Stage 2 (Phase 1a combined)
- _GpuRawStage         → gpu queue Stage 2 (Phase 1b split: transcribe + diarize together)
- _TranscribeOnlyStage → gpu-transcribe queue Stage 2a (Phase 4 multi-GPU split)
- _DiarizerOnlyStage   → gpu-diarize queue Stage 2b (Phase 4 multi-GPU split)
- _FinalizeStage       → cpu queue Stage 3

In Phase 1a, _GpuStage combines the full pipeline (identical to
TranscriptionPipeline.process) so Engine.process() is byte-equal.
Phase 1b splits preprocess and finalize into the proper stages.
Phase 4 further splits Stage 2 so transcription and diarization run on
separate GPUs (ENGINE_GPU_SPLIT=true).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import numpy as np

from app.core import stage_timing
from app.core.enums import TranscriptionStage

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.job import JobResult
    from app.transcription.engine.job import JobSpec
    from app.transcription.engine.job import PreprocessResult
    from app.transcription.engine.job import RawInferenceResult
    from app.transcription.engine.progress import ProgressCallback

logger = logging.getLogger(__name__)


class _PreprocessStage:
    """Stage 1 (CPU): decode audio to 16kHz WAV and write to shared volume."""

    def run(
        self,
        job: JobSpec,
        config: EngineConfig,
        callback: ProgressCallback | None = None,
    ) -> PreprocessResult:
        """Decode audio and write WAV to shared volume for Stage 2."""
        import os

        from app.transcription.engine.audio_loader import write_wav_to_shared_volume
        from app.transcription.engine.job import PreprocessResult
        from app.transcription.engine.progress import emit

        emit(callback, 0.10, "Decoding audio", TranscriptionStage.PREPARE)
        t0 = time.perf_counter()

        from faster_whisper.audio import decode_audio  # type: ignore[import]

        audio = decode_audio(job.audio_path, sampling_rate=16000)
        elapsed_decode = time.perf_counter() - t0
        audio_duration_s = len(audio) / 16000

        task_id = job.task_id or os.path.basename(job.audio_path)
        wav_path = write_wav_to_shared_volume(audio, config.shared_volume_path, task_id) or ""

        return PreprocessResult(
            task_id=job.task_id,
            file_id=job.file_id,
            user_id=job.user_id,
            local_wav_path=wav_path,
            minio_temp_object="",
            audio_duration_s=audio_duration_s,
            audio_sample_rate=16000,
            audio_channels=1,
            audio_size_bytes=audio.nbytes,
            vad_regions=None,
            config_snapshot=config.to_snapshot(),
            stage1_timings={"decode": elapsed_decode},
        )


def _apply_word_speaker_overrides(segments: list[dict], overrides: dict[str, str]) -> int:
    """Re-apply the gpu-split diarize worker's acoustic re-check relabels (issue #1205)."""
    from app.transcription.boundary_resolver import word_key

    applied = 0
    for seg in segments:
        for w in seg.get("words", []) or []:
            if "start" not in w or "end" not in w:
                continue
            speaker = overrides.get(word_key(w["start"], w["end"]))
            if speaker is not None and w.get("speaker") != speaker:
                w["speaker"] = speaker
                applied += 1
    return applied


class _FinalizeStage:
    """Stage 3 (CPU): segment dedup and speaker assignment."""

    def run(
        self,
        raw: RawInferenceResult,
        config: EngineConfig,
        callback: ProgressCallback | None = None,
    ) -> JobResult:
        """Reconstruct diarization result, dedup segments, assign speakers."""

        from app.transcription.diarize_result import DiarizeResult
        from app.transcription.engine.job import JobResult
        from app.transcription.engine.progress import emit

        t0 = time.perf_counter()
        tc = config.transcription_config

        transcript: dict = {"segments": raw.raw_segments, "language": raw.language}

        if raw.diarize_records:
            diarize_df = DiarizeResult(
                start=np.array([r["start"] for r in raw.diarize_records]),
                end=np.array([r["end"] for r in raw.diarize_records]),
                speaker=np.array([r["speaker"] for r in raw.diarize_records]),
            )

            if tc.enable_dedup:
                from app.utils.segment_dedup import clean_segments

                transcript["segments"] = clean_segments(transcript["segments"])

            emit(callback, 0.65, "Assigning speakers to transcript", TranscriptionStage.FINALIZE)
            from app.transcription.speaker_assigner import assign_speakers

            with stage_timing.stage("speaker_assignment"):
                result = assign_speakers(diarize_df, transcript)

            if raw.overlap_info.get("count", 0) > 0:
                result["overlap_info"] = raw.overlap_info
            if raw.native_speaker_embeddings:
                result["native_speaker_embeddings"] = raw.native_speaker_embeddings
            if raw.speaker_gender:
                result["speaker_gender"] = raw.speaker_gender
            if raw.word_speaker_overrides:
                _apply_word_speaker_overrides(
                    result.get("segments", []), raw.word_speaker_overrides
                )
        else:
            from app.utils.segment_dedup import clean_segments

            transcript["segments"] = clean_segments(transcript["segments"])
            for seg in transcript.get("segments", []):
                seg["speaker"] = "SPEAKER_00"
                for word in seg.get("words", []):
                    word["speaker"] = "SPEAKER_00"
            result = transcript

        return JobResult(
            segments=result.get("segments", []),
            language=result.get("language", ""),
            overlap_info=result.get("overlap_info", {}),
            native_speaker_embeddings=result.get("native_speaker_embeddings"),
            speaker_gender=result.get("speaker_gender"),
            stage_timings={**raw.stage_timings, "finalize": time.perf_counter() - t0},
            diarization_provider=raw.diarization_provider,
            diarization_model=raw.diarization_model,
        )
