"""Tests for ``app/services/pipeline_timing_service.py`` (issue #474).

Flushes a ``benchmark:{task_id}`` Redis hash into ``file_pipeline_timing``. Split by
what's genuinely out-of-process:

* The parsing pipeline (``_to_ms``/``_coerce_*``, ``_extract_timestamps``,
  ``_extract_context``, ``_compute_derived_durations``, ``build_row_payload``,
  ``derived_durations``) is pure ``dict[str, str] -> dict[str, Any]`` logic — tested
  directly with real input dicts, no mocking.
* ``record_pipeline_timing`` needs a real Postgres row (the upsert semantics, the
  FK to ``media_file``, and the exception-swallowing contract are all real DB
  behavior) — exercised against the savepoint-isolated ``db_session``, with only
  ``session_scope`` patched to hand out that session (module-level import, same
  pattern as ``test_dispatch.py``) and ``benchmark_timing.fetch_all`` stubbed to
  avoid touching the live dev-stack Redis for what is fundamentally a DB test.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.media import MediaFile
from app.models.pipeline_timing import FilePipelineTiming
from app.services import pipeline_timing_service as svc

pytestmark = pytest.mark.unit


# =============================================================================
# _to_ms / _coerce_int / _coerce_float / _coerce_json
# =============================================================================


def test_to_ms_converts_float_seconds_string_to_epoch_ms():
    assert svc._to_ms("12.345") == 12345


def test_to_ms_converts_integer_seconds_string():
    assert svc._to_ms("1700000000") == 1700000000000


def test_to_ms_returns_none_for_non_numeric_string():
    assert svc._to_ms("not-a-number") is None


def test_coerce_int_truncates_a_float_string():
    assert svc._coerce_int("42.9") == 42


def test_coerce_int_returns_none_on_garbage():
    assert svc._coerce_int("nope") is None


def test_coerce_float_parses_a_plain_float_string():
    assert svc._coerce_float("3.14") == pytest.approx(3.14)


def test_coerce_float_returns_none_on_garbage():
    assert svc._coerce_float("nope") is None


def test_coerce_json_parses_a_json_array_string():
    assert svc._coerce_json("[1, 2, 3]") == [1, 2, 3]


def test_coerce_json_returns_none_on_invalid_json():
    assert svc._coerce_json("{not json") is None


# =============================================================================
# _extract_timestamps
# =============================================================================


def test_extract_timestamps_converts_known_markers_to_ms_suffixed_keys():
    raw = {"ffmpeg_start": "100.0", "ffmpeg_end": "102.5"}

    out = svc._extract_timestamps(raw)

    assert out == {"ffmpeg_start_ms": 100000, "ffmpeg_end_ms": 102500}


def test_extract_timestamps_ignores_markers_not_present_in_raw():
    out = svc._extract_timestamps({"ffmpeg_start": "10.0"})

    assert "ffmpeg_end_ms" not in out
    assert out == {"ffmpeg_start_ms": 10000}


def test_extract_timestamps_ignores_unknown_marker_names_for_forward_compat():
    raw = {"some_future_marker_not_yet_known": "5.0"}

    out = svc._extract_timestamps(raw)

    assert out == {}


def test_extract_timestamps_drops_a_marker_that_fails_to_parse():
    raw = {"ffmpeg_start": "garbage", "ffmpeg_end": "5.0"}

    out = svc._extract_timestamps(raw)

    assert out == {"ffmpeg_end_ms": 5000}


# =============================================================================
# _extract_context
# =============================================================================


def test_extract_context_reads_string_int_float_and_json_keys():
    raw = {
        "whisper_model": "large-v3",
        "file_size_bytes": "204800",
        "audio_duration_s": "12.5",
        "queue_depth_at_dispatch": "[1, 2]",
    }

    out = svc._extract_context(raw)

    assert out == {
        "whisper_model": "large-v3",
        "file_size_bytes": 204800,
        "audio_duration_s": 12.5,
        "queue_depth_at_dispatch": [1, 2],
    }


def test_extract_context_excludes_an_empty_string_value_for_a_string_key():
    """`raw[str_key] != ""` is the explicit guard -- an empty string must not
    become a stored empty context value."""
    out = svc._extract_context({"whisper_model": ""})

    assert "whisper_model" not in out


def test_extract_context_drops_an_int_key_that_fails_to_coerce():
    out = svc._extract_context({"retry_count": "not-an-int"})

    assert "retry_count" not in out


def test_extract_context_drops_a_json_key_with_invalid_json():
    out = svc._extract_context({"per_retry_timings": "{not json"})

    assert "per_retry_timings" not in out


def test_extract_context_ignores_keys_outside_the_known_sets():
    out = svc._extract_context({"totally_unknown_field": "value"})

    assert out == {}


# =============================================================================
# _compute_derived_durations
# =============================================================================


def test_compute_derived_durations_user_perceived_uses_http_request_received_when_present():
    row = {"http_request_received_ms": 1000, "completion_notified_ms": 4500}

    out = svc._compute_derived_durations(row)

    assert out["user_perceived_duration_ms"] == 3500


def test_compute_derived_durations_falls_back_to_dispatch_timestamp_when_no_http_marker():
    row = {"dispatch_timestamp_ms": 1000, "postprocess_end_ms": 2000}

    out = svc._compute_derived_durations(row)

    assert out["user_perceived_duration_ms"] == 1000


def test_compute_derived_durations_prefers_completion_notified_over_postprocess_end():
    row = {
        "http_request_received_ms": 1000,
        "postprocess_end_ms": 3000,
        "completion_notified_ms": 5000,
    }

    out = svc._compute_derived_durations(row)

    assert out["user_perceived_duration_ms"] == 4000


def test_compute_derived_durations_omits_user_perceived_when_completion_precedes_start():
    """A negative duration is nonsensical (clock skew / bad data) and must be omitted,
    not stored as a negative number."""
    row = {"http_request_received_ms": 5000, "completion_notified_ms": 4000}

    out = svc._compute_derived_durations(row)

    assert "user_perceived_duration_ms" not in out


def test_compute_derived_durations_includes_the_exact_zero_duration_boundary():
    """completion_ms >= http_start_ms -- equal is included, not excluded."""
    row = {"http_request_received_ms": 5000, "completion_notified_ms": 5000}

    out = svc._compute_derived_durations(row)

    assert out["user_perceived_duration_ms"] == 0


def test_compute_derived_durations_fully_indexed_takes_the_max_async_end_candidate():
    row = {
        "http_request_received_ms": 1000,
        "search_index_end_ms": 3000,
        "clustering_end_ms": 6000,
        "summary_end_ms": 4500,
    }

    out = svc._compute_derived_durations(row)

    assert out["fully_indexed_duration_ms"] == 5000  # max(6000) - 1000


def test_compute_derived_durations_omits_both_keys_when_no_markers_present():
    assert svc._compute_derived_durations({}) == {}


def test_compute_derived_durations_omits_fully_indexed_without_any_async_end_marker():
    row = {"http_request_received_ms": 1000}

    out = svc._compute_derived_durations(row)

    assert "fully_indexed_duration_ms" not in out
    assert "user_perceived_duration_ms" not in out


def test_compute_derived_durations_treats_an_epoch_ms_of_zero_as_a_real_start_time():
    """FIX (suspected real bug, see this file's final report / issue #474 notes):
    ``http_start_ms = row.get("http_request_received_ms") or row.get("dispatch_timestamp_ms")``
    (pipeline_timing_service.py:175) and the ``if http_start_ms and completion_ms`` guard
    (:178) use falsy-``or``/``and`` rather than an explicit ``is not None`` check. An
    ``http_request_received_ms`` of exactly ``0`` (a legitimate epoch-ms value -- the Unix
    epoch instant -- and, more realistically, any future duration-shaped marker that can
    legitimately be 0) is falsy, so it is silently discarded in favor of
    ``dispatch_timestamp_ms`` and the whole duration computation is skipped even though a
    real, present ``0`` is exactly as valid a start time as any other integer.

    Confirmed against the real function before writing this test: with the current `or`/`and`
    checks, ``_compute_derived_durations({"http_request_received_ms": 0,
    "completion_notified_ms": 4000})`` returns ``{}`` -- no ``user_perceived_duration_ms`` at
    all -- which is wrong; the correct answer is ``4000``. This test pins the CORRECT
    behavior and is expected to fail until the source uses ``is not None`` checks instead of
    truthiness for both ``http_start_ms`` and ``completion_ms``.
    """
    row = {"http_request_received_ms": 0, "completion_notified_ms": 4000}

    out = svc._compute_derived_durations(row)

    assert out.get("user_perceived_duration_ms") == 4000


# =============================================================================
# build_row_payload / derived_durations — the combined pipeline
# =============================================================================


def test_build_row_payload_combines_timestamps_context_and_derived_durations():
    # Realistic epoch-second values (as real markers actually are, via time.time()) --
    # NOT 0.0, which hits the falsy-zero gap pinned separately in
    # test_compute_derived_durations_treats_an_epoch_ms_of_zero_as_a_real_start_time.
    raw = {
        "http_request_received": "1000.0",
        "completion_notified": "1010.0",
        "whisper_model": "large-v3",
        "file_size_bytes": "1024",
        "search_index_end_ms": "not-parsed-here",  # only *_ms bare markers are timestamps
    }

    row = svc.build_row_payload(raw)

    assert row["http_request_received_ms"] == 1000000
    assert row["completion_notified_ms"] == 1010000
    assert row["whisper_model"] == "large-v3"
    assert row["file_size_bytes"] == 1024
    assert row["user_perceived_duration_ms"] == 10000


def test_derived_durations_returns_only_the_two_duration_keys():
    raw = {
        "http_request_received": "1000.0",
        "completion_notified": "1003.0",
        "whisper_model": "large-v3",
        "file_size_bytes": "1024",
    }

    out = svc.derived_durations(raw)

    assert out == {"user_perceived_duration_ms": 3000}
    assert "whisper_model" not in out
    assert "file_size_bytes" not in out


def test_derived_durations_empty_when_no_relevant_markers_present():
    assert svc.derived_durations({"whisper_model": "large-v3"}) == {}


# =============================================================================
# now_ms
# =============================================================================


def test_now_ms_is_a_plausible_current_epoch_millisecond_value():
    import time

    before = int(time.time() * 1000)
    value = svc.now_ms()
    after = int(time.time() * 1000)

    assert before <= value <= after


# =============================================================================
# record_pipeline_timing — real DB, stubbed Redis read
# =============================================================================


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def media_file(db_session, normal_user):
    mf = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename="pipeline_timing_test.mp3",
        storage_path=f"pipeline_timing/{uuid_pkg.uuid4()}.mp3",
        file_size=2048,
        content_type="audio/mpeg",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def _patched(db_session, raw: dict[str, str]):
    return (
        patch(f"{svc.__name__}.session_scope", lambda: _yield_session(db_session)),
        patch(f"{svc.__name__}.benchmark_timing.fetch_all", return_value=raw),
    )


def test_record_pipeline_timing_returns_false_when_redis_hash_is_empty(db_session, media_file):
    p1, p2 = _patched(db_session, {})
    with p1, p2:
        result = svc.record_pipeline_timing("empty-task", media_file.id)

    assert result is False
    row = db_session.execute(
        select(FilePipelineTiming).where(FilePipelineTiming.task_id == "empty-task")
    ).scalar_one_or_none()
    assert row is None


def test_record_pipeline_timing_writes_a_real_row_with_parsed_fields(db_session, media_file):
    task_id = f"task-{uuid_pkg.uuid4().hex[:10]}"
    raw = {
        "http_request_received": "1000.0",
        "completion_notified": "1005.0",
        "whisper_model": "large-v3",
        "file_size_bytes": "4096",
    }
    p1, p2 = _patched(db_session, raw)
    with p1, p2:
        result = svc.record_pipeline_timing(task_id, media_file.id, user_id=None)

    assert result is True
    row = db_session.execute(
        select(FilePipelineTiming).where(FilePipelineTiming.task_id == task_id)
    ).scalar_one()
    assert row.file_id == media_file.id
    assert row.http_request_received_ms == 1000000
    assert row.completion_notified_ms == 1005000
    assert row.whisper_model == "large-v3"
    assert row.file_size_bytes == 4096
    assert row.user_perceived_duration_ms == 5000


def test_record_pipeline_timing_upserts_the_same_task_id_rather_than_duplicating(
    db_session, media_file
):
    task_id = f"task-{uuid_pkg.uuid4().hex[:10]}"

    p1, p2 = _patched(db_session, {"whisper_model": "small"})
    with p1, p2:
        svc.record_pipeline_timing(task_id, media_file.id)

    p1, p2 = _patched(db_session, {"whisper_model": "large-v3", "file_size_bytes": "999"})
    with p1, p2:
        result = svc.record_pipeline_timing(task_id, media_file.id)

    assert result is True
    rows = (
        db_session.execute(select(FilePipelineTiming).where(FilePipelineTiming.task_id == task_id))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].whisper_model == "large-v3"
    assert rows[0].file_size_bytes == 999


def test_record_pipeline_timing_stores_user_id_when_provided(db_session, media_file, normal_user):
    task_id = f"task-{uuid_pkg.uuid4().hex[:10]}"
    p1, p2 = _patched(db_session, {"whisper_model": "small"})
    with p1, p2:
        svc.record_pipeline_timing(task_id, media_file.id, user_id=normal_user.id)

    row = db_session.execute(
        select(FilePipelineTiming).where(FilePipelineTiming.task_id == task_id)
    ).scalar_one()
    assert row.user_id == normal_user.id


def test_record_pipeline_timing_with_only_unknown_keys_still_writes_a_bare_row(
    db_session, media_file
):
    """raw has content (fetch_all didn't return {}) but nothing recognized parses out of
    it -- payload is empty aside from task_id/file_id, so the upsert takes the
    on_conflict_do_nothing branch (`update_cols` is empty). Must not raise."""
    task_id = f"task-{uuid_pkg.uuid4().hex[:10]}"
    p1, p2 = _patched(db_session, {"some_future_field_nobody_parses_yet": "42"})
    with p1, p2:
        result = svc.record_pipeline_timing(task_id, media_file.id)

    assert result is True
    row = db_session.execute(
        select(FilePipelineTiming).where(FilePipelineTiming.task_id == task_id)
    ).scalar_one()
    assert row.file_id == media_file.id
    assert row.whisper_model is None


def test_record_pipeline_timing_returns_false_and_does_not_raise_on_fk_violation(db_session):
    """file_id referencing a MediaFile that doesn't exist -- record_pipeline_timing's
    job is to be best-effort and never propagate, per the module docstring."""
    p1, p2 = _patched(db_session, {"whisper_model": "small"})
    with p1, p2:
        result = svc.record_pipeline_timing("orphan-task", file_id=-999999)

    assert result is False
