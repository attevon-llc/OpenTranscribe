"""
Task detection service for identifying problematic tasks and files.

This service is responsible for detecting various types of task and file issues
without performing any recovery actions. It follows the single responsibility principle
by separating detection from recovery. The detection methods live in three mixins
(``task_detection_stuck``, ``task_detection_errors``, ``task_detection_post_transcription``);
this class composes them and stays the public face (``TaskDetectionService`` and the
``task_detection_service`` singleton).
"""

from app.core.task_config import task_recovery_config
from app.services.task_detection_errors import TaskDetectionErrorsMixin
from app.services.task_detection_post_transcription import TaskDetectionPostTranscriptionMixin
from app.services.task_detection_stuck import TaskDetectionStuckMixin


class TaskDetectionService(
    TaskDetectionStuckMixin, TaskDetectionErrorsMixin, TaskDetectionPostTranscriptionMixin
):
    """Service for detecting task and file issues."""

    def __init__(self, config=None):
        """Initialize with optional configuration override."""
        self.config = config or task_recovery_config


task_detection_service = TaskDetectionService()
