"""
Transcription module for OpenTranscribe.

Pipeline tasks (3-stage Celery chain for maximum GPU utilization):
- preprocess.py: CPU task — download, FFmpeg audio extraction, MinIO temp staging
- core.py: GPU task — Whisper transcription + PyAnnote diarization + DB save
- postprocess.py: CPU task — speaker embeddings, search indexing, downstream dispatch
- dispatch.py: Chain orchestration and batch dispatch

Shared utilities:
- audio_processor.py: Audio file processing and conversion
- metadata_extractor.py: Media metadata extraction utilities
- speaker_processor.py: Speaker diarization and management
- storage.py: Database storage utilities
- notifications.py: WebSocket notification utilities
"""

from .dispatch import dispatch_batch_transcription
from .dispatch import dispatch_transcription_pipeline

__all__ = [
    "dispatch_transcription_pipeline",
    "dispatch_batch_transcription",
]
