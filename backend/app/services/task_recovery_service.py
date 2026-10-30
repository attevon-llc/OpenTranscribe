"""
Task recovery service for handling recovery operations.

This service is responsible for performing recovery actions on tasks and files
identified as problematic. It follows the single responsibility principle
by separating recovery actions from detection. The recovery methods live in three mixins
(``task_recovery_stuck``, ``task_recovery_retry``, ``task_recovery_post_transcription``);
this class composes them and stays the public face (``TaskRecoveryService`` and the
``task_recovery_service`` singleton), which is also where tests patch ``schedule_file_retry``.
"""

from app.core.task_config import task_recovery_config
from app.services.task_recovery_post_transcription import TaskRecoveryPostTranscriptionMixin
from app.services.task_recovery_stuck import TaskRecoveryStuckMixin


class TaskRecoveryService(TaskRecoveryStuckMixin, TaskRecoveryPostTranscriptionMixin):
    """Service for performing task and file recovery operations."""

    def __init__(self, config=None):
        """Initialize with optional configuration override."""
        self.config = config or task_recovery_config


task_recovery_service = TaskRecoveryService()
