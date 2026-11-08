"""Speaker task re-exports for backward compatibility.

The actual task implementations live in:
- speaker_identification_task.py  (LLM speaker ID)
- speaker_update_task.py          (background speaker updates)
- speaker_embedding_task.py       (embedding extraction)
- speaker_embedding_reassignment_task.py (embedding update on segment reassignment)

This module re-exports all Celery tasks so that existing task routing
(by task name) continues to work without changes.
"""

from app.tasks.speaker_embedding_reassignment_task import update_speaker_embedding_on_reassignment
from app.tasks.speaker_embedding_task import extract_speaker_embeddings_task
from app.tasks.speaker_identification_task import identify_speakers_llm_task
from app.tasks.speaker_update_task import process_speaker_update_background

__all__ = [
    "extract_speaker_embeddings_task",
    "identify_speakers_llm_task",
    "process_speaker_update_background",
    "update_speaker_embedding_on_reassignment",
]
