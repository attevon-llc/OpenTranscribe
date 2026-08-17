"""Tests for ``app/services/task_filtering_service.py`` (issue #474).

Pure logic, no DB/Redis/network — the service is a ``@staticmethod``-only class over
plain ``dict`` "task" records supplied by ``app/api/endpoints/tasks.py`` (confirmed live
caller: ``GET /tasks`` calls ``TaskFilteringService.filter_tasks_by_criteria`` after
building per-task dicts from ``MediaFile``/``TaskModel`` rows).

**FIX (real bug): ``_add_computed_fields`` crashed on a string ``created_at``.**
``_matches_age_filter``, ``_matches_date_range``, and ``_format_task_duration`` all
explicitly normalize ``task["created_at"]`` from an ISO string to a ``datetime`` before
using it — the module's own docstrings and code make clear string timestamps are a
supported input shape, not just a defensive no-op for a case that never happens. But
``_add_computed_fields`` forwarded ``task.get("created_at")``/``task.get("completed_at")``
to ``FormattingService.format_processing_time`` **without** that same normalization, and
that function immediately does ``created_at.tzinfo`` — an ``AttributeError`` on a ``str``.
Confirmed failing against the old code before the fix (see the module docstring below the
fix for how to reproduce with a `git archive` tree if re-verifying). Fixed by normalizing
both fields the same way the sibling helpers already do, immediately inside
``_add_computed_fields`` before either is used.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from app.services.task_filtering_service import TaskFilteringService as TaskFiltering

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _task(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "completed",
        "task_type": "transcription",
        "created_at": datetime.now(UTC) - timedelta(hours=2),
        "completed_at": datetime.now(UTC) - timedelta(hours=1),
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------------------
# FIX: _add_computed_fields must accept string timestamps, exactly like its siblings
# --------------------------------------------------------------------------------------


def test_add_computed_fields_accepts_iso_string_timestamps_without_raising():
    """The FIX. Was: AttributeError: 'str' object has no attribute 'tzinfo'."""
    created = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)
    completed = datetime(2024, 1, 1, 10, 5, 30, tzinfo=UTC)
    task = _task(created_at=created.isoformat(), completed_at=completed.isoformat())

    result = TaskFiltering.filter_tasks_by_criteria([task])

    assert len(result) == 1
    enriched = result[0]
    # format_detailed_duration renders 5.5 minutes -> "5m 30s"; the key point under test
    # is that it is a real formatted string and not None/a crash.
    assert enriched["formatted_processing_time"] is not None
    assert isinstance(enriched["formatted_processing_time"], str)


def test_add_computed_fields_string_and_datetime_created_at_produce_the_same_result():
    """A string and an equivalent datetime must format identically -- proves the fix
    normalizes rather than special-cases the string branch into different behavior."""
    created = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)
    completed = datetime(2024, 1, 1, 10, 5, 30, tzinfo=UTC)

    from_datetime = TaskFiltering.filter_tasks_by_criteria(
        [_task(created_at=created, completed_at=completed)]
    )[0]
    from_string = TaskFiltering.filter_tasks_by_criteria(
        [_task(created_at=created.isoformat(), completed_at=completed.isoformat())]
    )[0]

    assert from_datetime["formatted_processing_time"] == from_string["formatted_processing_time"]
    assert from_datetime["age_category"] == from_string["age_category"]
    assert from_datetime["formatted_duration"] == from_string["formatted_duration"]


def test_add_computed_fields_with_no_completed_at_still_formats_using_now():
    """completed_at=None (task still running) is the other real-world shape production
    sends -- FormattingService.format_processing_time falls back to datetime.now(UTC)."""
    task = _task(created_at=datetime.now(UTC) - timedelta(minutes=5), completed_at=None)

    enriched = TaskFiltering.filter_tasks_by_criteria([task])[0]

    # ~5 minutes elapsed against "now" -> format_detailed_duration renders "5m[ Ns]", not the
    # None/AttributeError this call used to raise before the fix, and not a bare seconds
    # value like "12s" (which would mean created_at/completed_at were measured wrong).
    assert enriched["formatted_processing_time"] is not None
    assert enriched["formatted_processing_time"].startswith("5m")


# --------------------------------------------------------------------------------------
# filter_tasks_by_criteria -- status / needs_attention / task_type
# --------------------------------------------------------------------------------------


def test_filter_by_plain_status_keeps_only_matching_tasks():
    tasks = [_task(status="completed"), _task(status="failed"), _task(status="pending")]

    result = TaskFiltering.filter_tasks_by_criteria(tasks, status="failed")

    assert len(result) == 1
    assert result[0]["status"] == "failed"


def test_needs_attention_filter_includes_failed_and_stuck_but_excludes_healthy():
    now = datetime.now(UTC)
    tasks = [
        _task(status="failed", created_at=now),  # needs attention: failed
        _task(status="in_progress", created_at=now - timedelta(hours=2)),  # stuck > 1h
        _task(status="in_progress", created_at=now - timedelta(minutes=5)),  # fresh, fine
        _task(status="pending", created_at=now - timedelta(hours=3)),  # stuck pending > 2h
        _task(status="pending", created_at=now - timedelta(minutes=5)),  # fresh pending, fine
        _task(status="completed", created_at=now - timedelta(days=5)),  # done, never flagged
    ]

    result = TaskFiltering.filter_tasks_by_criteria(tasks, status="needs_attention")

    assert len(result) == 3
    statuses = sorted((t["status"], str(t["created_at"])) for t in result)
    assert [s for s, _ in statuses] == ["failed", "in_progress", "pending"]


def test_filter_by_task_type_and_status_combine_as_an_and():
    tasks = [
        _task(status="completed", task_type="transcription"),
        _task(status="completed", task_type="summarization"),
        _task(status="failed", task_type="transcription"),
    ]

    result = TaskFiltering.filter_tasks_by_criteria(
        tasks, status="completed", task_type="transcription"
    )

    assert len(result) == 1
    assert result[0]["task_type"] == "transcription"


def test_a_task_with_no_created_at_is_excluded_by_an_age_filter_rather_than_crashing():
    tasks = [_task(created_at=None), _task()]

    result = TaskFiltering.filter_tasks_by_criteria(tasks, age_filter="today")

    # The None-created_at task is dropped by _matches_age_filter's `if not created_at: return False`.
    assert len(result) == 1
    assert result[0]["created_at"] is not None


# --------------------------------------------------------------------------------------
# _matches_age_filter -- boundary behavior (<=, not <)
# --------------------------------------------------------------------------------------


def test_age_filter_today_boundary_is_inclusive_at_24_hours_exclusive_just_past():
    """diff_hours <= 24 -> matches. The threshold is checked against `datetime.now(UTC)`
    computed *inside* the function, so the fixtures use a few seconds of slack on each side
    rather than an exact 24h boundary — an exact boundary is inherently racy against
    however long the test process takes to reach the assertion."""
    now = datetime.now(UTC)
    just_under_24h = _task(id="under", created_at=now - timedelta(hours=23, minutes=59, seconds=55))
    just_over_24h = _task(id="over", created_at=now - timedelta(hours=24, seconds=5))

    result = TaskFiltering.filter_tasks_by_criteria(
        [just_under_24h, just_over_24h], age_filter="today"
    )

    assert [t["id"] for t in result] == ["under"]


def test_age_filter_older_requires_strictly_more_than_30_days():
    now = datetime.now(UTC)
    just_under_30d = _task(id="under", created_at=now - timedelta(days=29, hours=23, minutes=55))
    just_over_30d = _task(id="over", created_at=now - timedelta(days=30, hours=1))

    result = TaskFiltering.filter_tasks_by_criteria(
        [just_under_30d, just_over_30d], age_filter="older"
    )

    assert [t["id"] for t in result] == ["over"]


def test_age_filter_accepts_iso_string_created_at():
    now = datetime.now(UTC)
    recent = _task(created_at=(now - timedelta(hours=1)).isoformat())
    old = _task(created_at=(now - timedelta(days=40)).isoformat())

    result = TaskFiltering.filter_tasks_by_criteria([recent, old], age_filter="today")

    assert len(result) == 1


# --------------------------------------------------------------------------------------
# _matches_date_range
# --------------------------------------------------------------------------------------


def test_date_range_filters_by_inclusive_from_and_to():
    in_range = _task(id="in_range", created_at=datetime(2024, 6, 15, 12, tzinfo=UTC))
    before_range = _task(id="before_range", created_at=datetime(2024, 6, 9, 12, tzinfo=UTC))
    after_range = _task(id="after_range", created_at=datetime(2024, 6, 21, 12, tzinfo=UTC))
    on_the_to_boundary = _task(
        id="on_the_to_boundary", created_at=datetime(2024, 6, 20, 23, 59, 0, tzinfo=UTC)
    )

    # _add_computed_fields returns a COPY (task.copy()), so identity/dict-equality checks
    # against the input dicts never match the output — filter and assert on the "id" marker.
    result = TaskFiltering.filter_tasks_by_criteria(
        [in_range, before_range, after_range, on_the_to_boundary],
        date_from="2024-06-10",
        date_to="2024-06-20",
    )

    assert {t["id"] for t in result} == {"in_range", "on_the_to_boundary"}


def test_date_range_with_malformed_from_date_logs_and_does_not_filter_out_the_task():
    task = _task(created_at=datetime(2024, 6, 15, tzinfo=UTC))

    result = TaskFiltering.filter_tasks_by_criteria([task], date_from="not-a-date")

    # A malformed bound is logged and skipped, not treated as "reject everything" --
    # pinning this documents the current (permissive) behavior explicitly.
    assert len(result) == 1


# --------------------------------------------------------------------------------------
# _format_task_duration
# --------------------------------------------------------------------------------------


def test_format_task_duration_renders_seconds_minutes_and_hours():
    now = datetime.now(UTC)

    seconds_task = _task(created_at=now - timedelta(seconds=45), completed_at=now)
    minutes_task = _task(created_at=now - timedelta(minutes=5), completed_at=now)
    hours_task = _task(created_at=now - timedelta(hours=2, minutes=15), completed_at=now)
    exact_hour_task = _task(created_at=now - timedelta(hours=3), completed_at=now)

    result = TaskFiltering.filter_tasks_by_criteria(
        [seconds_task, minutes_task, hours_task, exact_hour_task]
    )

    by_identity = {
        id(t): r
        for t, r in zip(
            [seconds_task, minutes_task, hours_task, exact_hour_task], result, strict=True
        )
    }
    assert by_identity[id(seconds_task)]["formatted_duration"] == "45s"
    assert by_identity[id(minutes_task)]["formatted_duration"] == "5m"
    assert by_identity[id(hours_task)]["formatted_duration"] == "2h 15m"
    assert by_identity[id(exact_hour_task)]["formatted_duration"] == "3h"


def test_format_task_duration_with_no_completed_at_measures_against_now():
    task = _task(created_at=datetime.now(UTC) - timedelta(seconds=30), completed_at=None)

    enriched = TaskFiltering.filter_tasks_by_criteria([task])[0]

    assert enriched["formatted_duration"] is not None
    # Should be a small number of seconds, not None and not an hours/minutes value.
    assert enriched["formatted_duration"].endswith("s")


# --------------------------------------------------------------------------------------
# _format_status_display
# --------------------------------------------------------------------------------------


def test_format_status_display_maps_known_statuses_to_human_text():
    tasks = [
        _task(status="pending"),
        _task(status="in_progress"),
        _task(status="completed"),
        _task(status="failed"),
    ]

    result = TaskFiltering.filter_tasks_by_criteria(tasks)

    displays = [t["status_display"] for t in result]
    assert displays == ["Pending", "In Progress", "Completed", "Failed"]


def test_format_status_display_title_cases_an_unrecognized_status():
    task = _task(status="cancelled")

    enriched = TaskFiltering.filter_tasks_by_criteria([task])[0]

    assert enriched["status_display"] == "Cancelled"


# --------------------------------------------------------------------------------------
# _compute_age_category
# --------------------------------------------------------------------------------------


def test_compute_age_category_buckets_correctly():
    now = datetime.now(UTC)
    tasks = [
        _task(created_at=now - timedelta(hours=1)),  # today
        _task(created_at=now - timedelta(days=3)),  # week
        _task(created_at=now - timedelta(days=20)),  # month
        _task(created_at=now - timedelta(days=45)),  # older
        _task(created_at=None),  # unknown
    ]

    result = TaskFiltering.filter_tasks_by_criteria(tasks)

    assert [t["age_category"] for t in result] == ["today", "week", "month", "older", "unknown"]
