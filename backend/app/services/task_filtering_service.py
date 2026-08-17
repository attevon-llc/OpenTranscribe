"""
Task Filtering Service for server-side task filtering and processing.

This service handles the complex filtering logic that was previously done on the frontend.

The service provides comprehensive task filtering and enrichment including:
- Multi-criteria filtering (status, type, age, date ranges)
- Age category computation and display formatting
- Task duration calculations with proper timezone handling
- Status display text generation
- Computed field addition for frontend consumption

All filtering logic is centralized to reduce frontend complexity and ensure
consistent behavior across the application.
"""

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class TaskFilteringService:
    """Service for filtering and processing tasks with backend logic."""

    @staticmethod
    def filter_tasks_by_criteria(
        tasks: list[dict[str, Any]],
        status: str | None = None,
        task_type: str | None = None,
        age_filter: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Filter tasks based on multiple criteria.

        Args:
            tasks: List of task dictionaries
            status: Task status filter
            task_type: Task type filter
            age_filter: Age filter ("today", "week", "month", "older")
            date_from: Start date filter (YYYY-MM-DD)
            date_to: End date filter (YYYY-MM-DD)

        Returns:
            Filtered list of tasks
        """
        filtered_tasks = []

        for task in tasks:
            # Filter by status (special "needs_attention" filter)
            if status == "needs_attention":
                if not TaskFilteringService._needs_attention(task):
                    continue
            elif status and task.get("status") != status:
                continue

            # Filter by task type
            if task_type and task.get("task_type") != task_type:
                continue

            # Filter by age
            if age_filter and not TaskFilteringService._matches_age_filter(task, age_filter):
                continue

            # Filter by custom date range
            if (date_from or date_to) and not TaskFilteringService._matches_date_range(
                task, date_from, date_to
            ):
                continue

            # Add computed fields for frontend display
            task_with_computed = TaskFilteringService._add_computed_fields(task)
            filtered_tasks.append(task_with_computed)

        return filtered_tasks

    @staticmethod
    def _matches_age_filter(task: dict[str, Any], age_filter: str) -> bool:
        """
        Check if task matches age filter criteria.

        Args:
            task: Task dictionary
            age_filter: Age filter string

        Returns:
            True if task matches age filter
        """
        created_at = task.get("created_at")
        if not created_at:
            return False

        # Ensure created_at is a datetime object
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        now = datetime.now(UTC)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)

        diff_hours = (now - created_at).total_seconds() / 3600

        age_thresholds = {
            "today": 24,
            "week": 24 * 7,
            "month": 24 * 30,
        }

        if age_filter in age_thresholds:
            return diff_hours <= age_thresholds[age_filter]  # type: ignore[no-any-return]
        elif age_filter == "older":
            return diff_hours > 24 * 30  # type: ignore[no-any-return]

        return True

    @staticmethod
    def _matches_date_range(
        task: dict[str, Any], date_from: str | None, date_to: str | None
    ) -> bool:
        """
        Check if task matches date range criteria.

        Args:
            task: Task dictionary
            date_from: Start date string (YYYY-MM-DD)
            date_to: End date string (YYYY-MM-DD)

        Returns:
            True if task matches date range
        """
        created_at = task.get("created_at")
        if not created_at:
            return False

        # Ensure created_at is a datetime object
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        if date_from:
            try:
                from_date = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=UTC)
                if created_at < from_date:
                    return False
            except ValueError:
                logger.warning(f"Invalid date_from format: {date_from}")

        if date_to:
            try:
                to_date = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=UTC)
                # Include the entire end date
                to_date = to_date.replace(hour=23, minute=59, second=59, microsecond=999999)
                if created_at > to_date:
                    return False
            except ValueError:
                logger.warning(f"Invalid date_to format: {date_to}")

        return True

    @staticmethod
    def _add_computed_fields(task: dict[str, Any]) -> dict[str, Any]:
        """
        Add computed fields to task for frontend display.

        Args:
            task: Task dictionary

        Returns:
            Task dictionary with computed fields
        """
        # Make a copy to avoid modifying original
        enriched_task = task.copy()

        # Add age category
        enriched_task["age_category"] = TaskFilteringService._compute_age_category(task)

        # Add formatted duration
        enriched_task["formatted_duration"] = TaskFilteringService._format_task_duration(task)

        # Add status display text
        enriched_task["status_display"] = TaskFilteringService._format_status_display(task)

        # Add formatted processing time
        from app.services.formatting_service import FormattingService

        # FormattingService.format_processing_time requires real datetime objects (it reads
        # .tzinfo directly) — normalize the same way _matches_age_filter/_matches_date_range/
        # _format_task_duration already do, so a caller passing ISO-string timestamps (a
        # supported shape everywhere else in this class) doesn't crash here instead.
        computed_created_at = task.get("created_at")
        if isinstance(computed_created_at, str):
            computed_created_at = datetime.fromisoformat(computed_created_at.replace("Z", "+00:00"))
        computed_completed_at = task.get("completed_at")
        if isinstance(computed_completed_at, str):
            computed_completed_at = datetime.fromisoformat(
                computed_completed_at.replace("Z", "+00:00")
            )

        enriched_task["formatted_processing_time"] = FormattingService.format_processing_time(
            computed_created_at, computed_completed_at
        )

        return enriched_task

    @staticmethod
    def _compute_age_category(task: dict[str, Any]) -> str:
        """
        Compute age category for a task.

        Args:
            task: Task dictionary

        Returns:
            Age category string
        """
        created_at = task.get("created_at")
        if not created_at:
            return "unknown"

        # Ensure created_at is a datetime object
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        now = datetime.now(UTC)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)

        diff_hours = (now - created_at).total_seconds() / 3600

        if diff_hours <= 24:
            return "today"
        elif diff_hours <= 24 * 7:
            return "week"
        elif diff_hours <= 24 * 30:
            return "month"
        else:
            return "older"

    @staticmethod
    def _format_task_duration(task: dict[str, Any]) -> str | None:
        """
        Format task duration for display.

        Args:
            task: Task dictionary

        Returns:
            Formatted duration string or None
        """
        created_at = task.get("created_at")
        completed_at = task.get("completed_at")

        if not created_at:
            return None

        # Ensure dates are datetime objects
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        if completed_at:
            if isinstance(completed_at, str):
                completed_at = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            duration_seconds = (completed_at - created_at).total_seconds()
        else:
            # Task still running, calculate duration from now
            now = datetime.now(UTC)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            duration_seconds = (now - created_at).total_seconds()

        # Format duration
        if duration_seconds < 60:
            return f"{int(duration_seconds)}s"
        elif duration_seconds < 3600:
            minutes = int(duration_seconds / 60)
            return f"{minutes}m"
        else:
            hours = int(duration_seconds / 3600)
            minutes = int((duration_seconds % 3600) / 60)
            return f"{hours}h {minutes}m" if minutes > 0 else f"{hours}h"

    @staticmethod
    def _format_status_display(task: dict[str, Any]) -> str:
        """
        Format status for display.

        Args:
            task: Task dictionary

        Returns:
            Human-readable status
        """
        status = task.get("status", "unknown")
        status_map = {
            "pending": "Pending",
            "in_progress": "In Progress",
            "completed": "Completed",
            "failed": "Failed",
        }
        return status_map.get(status, status.title())  # type: ignore[no-any-return]

    @staticmethod
    def _needs_attention(task: dict[str, Any]) -> bool:
        """Check if a task needs attention (error, stuck processing, or long pending)."""
        task_status = task.get("status", "")
        if task_status in ("failed", "error"):
            return True
        created_at = task.get("created_at")
        if not created_at:
            return False
        now = datetime.now(UTC)
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return False
        if not created_at.tzinfo:
            created_at = created_at.replace(tzinfo=UTC)
        age_hours = (now - created_at).total_seconds() / 3600
        if task_status == "in_progress" and age_hours > 1:
            return True
        return bool(task_status == "pending" and age_hours > 2)
