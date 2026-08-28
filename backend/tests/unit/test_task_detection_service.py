"""Decision logic of ``services/task_detection_service.py`` (1,126 LOC, previously untested).

Nothing in ``tests/`` referenced this module — no import, no call, not even a
name. That is the worst place in the codebase for a coverage hole: every
predicate here only runs *after* something has already gone wrong (a worker
died, the GPU OOM'd, a download 403'd), so a wrong threshold or an inverted
guard produces no symptom in normal operation and then either eats a real
transcript or hammers an external service during an incident.

Scope. These tests pin the *decisions*, not the SQL plumbing:

* the two-part stuck-task predicate (stale **and** over its per-type budget);
* the OOM signature and its exponential backoff;
* the retriable-error batch cap and category filter, and the
  YouTube-vs-transcription split that keeps download retries inside YouTube's
  rate limit;
* the unrecoverable-download pattern list;
* the LLM-task 6 h timeout and its task-type scope;
* the exact-message match that identifies over-aggressive recovery.

Deliberately NOT covered here (see the report):
``identify_incomplete_post_transcription_files`` (its
``limit(batch_size * 3)`` over ``completed_at ASC`` means a dev database full of
old COMPLETED files crowds out any row a test creates, so an assertion about it
would be ambient-data dependent) and everything that calls
``schedule_file_retry`` (it opens its **own** ``SessionLocal``, which cannot see
savepointed rows).

Ordering-sensitive queries are made deterministic by dating this suite's rows to
1999 so they sort ahead of anything the dev database holds; membership
assertions are always by id, never by list length.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.core.task_config import TaskRecoveryConfig
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services.task_detection_service import TaskDetectionService
from app.utils.error_classification import ErrorCategory

NOW = datetime.now(UTC)
ANCIENT = datetime(1999, 1, 1, tzinfo=UTC)

#: An explicit config so thresholds are asserted against known numbers rather than
#: whatever the shipped defaults happen to be. ``default`` is mandatory —
#: ``_is_task_duration_exceeded`` indexes it directly.
CONFIG = TaskRecoveryConfig(
    MAX_TASK_DURATIONS={"transcription": 3600, "default": 1800},
    STALENESS_THRESHOLD=300,
    ORPHANED_TASK_THRESHOLD=1,
    OOM_BACKOFF_BASE_MINUTES=10,
)


@pytest.fixture
def service() -> TaskDetectionService:
    return TaskDetectionService(config=CONFIG)


def _file(db, user, **kwargs) -> MediaFile:
    """A MediaFile with the NOT NULL columns filled and everything else overridable."""
    defaults = {
        "user_id": user.id,
        "filename": f"f-{uuid.uuid4().hex[:8]}.wav",
        "storage_path": f"user_{user.id}/{uuid.uuid4().hex[:8]}.wav",
        "file_size": 1024,
        "content_type": "audio/wav",
        "status": FileStatus.PROCESSING,
    }
    media_file = MediaFile(**{**defaults, **kwargs})
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _task(db, user, media_file, **kwargs) -> Task:
    defaults = {
        "id": f"task-{uuid.uuid4()}",
        "user_id": user.id,
        "media_file_id": media_file.id if media_file else None,
        "task_type": "transcription",
        "status": "pending",
    }
    task = Task(**{**defaults, **kwargs})
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# =============================================================================
# Task duration budget
# =============================================================================
def test_duration_exceeded_uses_the_per_type_budget(service):
    """A transcription gets its own 3600 s budget, not the 1800 s default.

    Catches the per-type lookup collapsing to ``default``: every transcription
    longer than 30 minutes would then be declared stuck and marked failed
    mid-run, which is exactly the false-positive failure mode
    ``identify_false_positive_failed_tasks`` exists to clean up after.
    """
    over = Task(id="a", user_id=1, task_type="transcription", status="in_progress")
    over.created_at = NOW - timedelta(seconds=3700)
    under = Task(id="b", user_id=1, task_type="transcription", status="in_progress")
    under.created_at = NOW - timedelta(seconds=2000)

    assert service._is_task_duration_exceeded(over, NOW) is True
    assert service._is_task_duration_exceeded(under, NOW) is False


def test_duration_exceeded_falls_back_to_default_for_an_unlisted_type(service):
    """An unlisted task type uses ``default`` rather than being exempt.

    Catches a ``.get(type)`` without a fallback (None) or a KeyError guard that
    returns False — either would make every post-transcription task type
    immortal, so a wedged summarization would never be recovered.
    """
    over = Task(id="c", user_id=1, task_type="summarization", status="in_progress")
    over.created_at = NOW - timedelta(seconds=2000)
    under = Task(id="d", user_id=1, task_type="summarization", status="in_progress")
    under.created_at = NOW - timedelta(seconds=1000)

    assert service._is_task_duration_exceeded(over, NOW) is True
    assert service._is_task_duration_exceeded(under, NOW) is False


def test_a_task_without_created_at_is_never_over_budget(service):
    """No creation time means no measurable duration, so the answer is False.

    Catches the None guard being dropped: subtracting from None raises TypeError
    inside the sweep loop, taking down the whole recovery pass.
    """
    task = Task(id="e", user_id=1, task_type="transcription", status="pending")
    assert service._is_task_duration_exceeded(task, NOW) is False


# =============================================================================
# Stuck / orphaned tasks
# =============================================================================
def test_stuck_tasks_need_both_staleness_and_an_exceeded_budget(service, db_session, normal_user):
    """Staleness alone is not enough — the duration check is the second half.

    Catches the duration filter being dropped from ``identify_stuck_tasks``: any
    pending task quiet for 5 minutes would be failed, which for a transcription
    that is legitimately mid-GPU-run destroys in-flight work.
    """
    media_file = _file(db_session, normal_user)
    stale_and_over = _task(
        db_session,
        normal_user,
        media_file,
        status="in_progress",
        created_at=NOW - timedelta(seconds=4000),
        updated_at=NOW - timedelta(seconds=600),
    )
    stale_but_young = _task(
        db_session,
        normal_user,
        media_file,
        status="in_progress",
        created_at=NOW - timedelta(seconds=600),
        updated_at=NOW - timedelta(seconds=600),
    )

    ids = {t.id for t in service.identify_stuck_tasks(db_session)}
    assert stale_and_over.id in ids
    assert stale_but_young.id not in ids


def test_orphaned_tasks_use_the_hour_threshold(service, db_session, normal_user):
    """Only tasks untouched for longer than ORPHANED_TASK_THRESHOLD hours count."""
    media_file = _file(db_session, normal_user)
    old = _task(db_session, normal_user, media_file, updated_at=NOW - timedelta(hours=2))
    recent = _task(db_session, normal_user, media_file, updated_at=NOW - timedelta(minutes=10))

    ids = {t.id for t in service.identify_orphaned_tasks(db_session)}
    assert old.id in ids
    assert recent.id not in ids


# =============================================================================
# OOM retry eligibility
# =============================================================================
def test_oom_detection_requires_both_cuda_and_out_of_memory(service, db_session, normal_user):
    """The signature is the conjunction; either word alone is a different failure.

    Catches the two ``ilike`` clauses becoming an OR: a plain host-RAM
    "out of memory" or any message merely mentioning CUDA would enter the OOM
    backoff path and be retried on the GPU forever instead of surfacing as an
    error the user can act on.
    """
    both = _file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        last_error_message="CUDA out of memory. Tried to allocate 2.00 GiB",
        retry_count=0,
    )
    memory_only = _file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        last_error_message="Worker ran out of memory",
        retry_count=0,
    )
    cuda_only = _file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        last_error_message="CUDA driver initialization failed",
        retry_count=0,
    )

    ids = {f.id for f in service.identify_oom_error_files(db_session)}
    assert both.id in ids
    assert memory_only.id not in ids
    assert cuda_only.id not in ids


def test_oom_backoff_grows_with_the_retry_count(service, db_session, normal_user):
    """``2 ** retry_count * 10`` minutes must have elapsed since the last attempt.

    At ``retry_count=1`` the window is 20 minutes: a 5-minute-old attempt is not
    yet eligible and a 25-minute-old one is. Catches the backoff being dropped or
    made linear — the health check runs every 10 minutes, so a file that OOMs the
    GPU would be re-dispatched every cycle, re-OOMing the only GPU this project
    has and starving every other queued transcription.
    """
    message = "CUDA out of memory"
    too_soon = _file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        last_error_message=message,
        retry_count=1,
        last_recovery_attempt=NOW - timedelta(minutes=5),
    )
    elapsed = _file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        last_error_message=message,
        retry_count=1,
        last_recovery_attempt=NOW - timedelta(minutes=25),
    )

    ids = {f.id for f in service.identify_oom_error_files(db_session)}
    assert elapsed.id in ids
    assert too_soon.id not in ids


def test_oom_detection_treats_a_null_retry_count_as_zero(service, db_session, normal_user):
    """A NULL ``retry_count`` must behave like 0, not raise.

    ``media_file.retry_count`` is a nullable Integer whose 0 is a Python-side
    default, so NULL is a reachable state. The backoff used to compute
    ``2 ** media_file.retry_count`` directly: on such a row that is a TypeError,
    and it is raised *outside* any per-file try/except in
    ``periodic_health_check`` step 4 — so one NULL row aborted the entire pass and
    silently disabled steps 5 through 7 (retriable errors, stuck LLM tasks,
    false-positive resets, post-transcription recovery) on every 10-minute cycle.
    This test fails with a TypeError against that implementation.

    The NULL is written with an explicit UPDATE, not by passing
    ``retry_count=None`` to the constructor: the ORM omits a None-valued column
    that has a ``server_default`` from the INSERT, so the row would come back as 0
    and the test could not fail. The ``is None`` assertion below guards exactly
    that — if the NULL ever stops landing, this test says so instead of quietly
    passing.
    """
    from sqlalchemy import update

    null_count = _file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        last_error_message="CUDA out of memory",
        last_recovery_attempt=NOW - timedelta(minutes=30),
    )
    db_session.execute(
        update(MediaFile).where(MediaFile.id == null_count.id).values(retry_count=None)
    )
    db_session.commit()
    db_session.refresh(null_count)
    assert null_count.retry_count is None

    ids = {f.id for f in service.identify_oom_error_files(db_session)}
    assert null_count.id in ids


# =============================================================================
# Retriable ERROR files
# =============================================================================
def _error_file(db, user, *, category: ErrorCategory, days_old: int, **kwargs) -> MediaFile:
    """A retriable-shaped ERROR file dated in 1999 so it sorts ahead of dev data."""
    return _file(
        db,
        user,
        status=FileStatus.ERROR,
        error_category=category.value,
        completed_at=ANCIENT + timedelta(days=days_old),
        retry_count=0,
        **kwargs,
    )


def test_retriable_errors_are_capped_at_the_batch_size(service, db_session, normal_user):
    """The cap is the throttle that keeps YouTube from banning the deployment.

    All three rows are dated 1999 so they are the oldest candidates in the table
    and ordering is deterministic. Catches the cap being removed (every failed
    download re-attempted in one cycle) and the ``completed_at ASC`` ordering
    being reversed (the newest failure retried first, so an old one starves).
    """
    first = _error_file(db_session, normal_user, category=ErrorCategory.NETWORK_ERROR, days_old=0)
    second = _error_file(db_session, normal_user, category=ErrorCategory.NETWORK_ERROR, days_old=1)
    third = _error_file(db_session, normal_user, category=ErrorCategory.NETWORK_ERROR, days_old=2)

    selected = service.identify_retriable_error_files(db_session, batch_size=2)
    assert [f.id for f in selected] == [first.id, second.id]
    assert third.id not in {f.id for f in selected}


def test_retriable_errors_exclude_permanently_failed_categories(service, db_session, normal_user):
    """A private/removed video is never retried, however old it is.

    The permanent row is the oldest candidate in the table, so it would be first
    if the category filter were widened. Catches exactly that: a deleted YouTube
    video would be re-requested every cycle forever, which is both pointless and
    the fastest way to look like an abusive client.
    """
    permanent = _error_file(
        db_session, normal_user, category=ErrorCategory.PRIVATE_OR_REMOVED, days_old=0
    )
    retriable = _error_file(
        db_session, normal_user, category=ErrorCategory.NETWORK_ERROR, days_old=1
    )

    ids = {f.id for f in service.identify_retriable_error_files(db_session, batch_size=5)}
    assert retriable.id in ids
    assert permanent.id not in ids


def test_retriable_errors_exhausted_by_retry_count_are_dropped(service, db_session, normal_user):
    """``should_retry`` caps AUTH_OR_RATE_LIMIT at 2 attempts.

    Catches the ``should_retry`` call being removed: a rate-limited download would
    keep retrying past its budget, and the per-category cap that distinguishes a
    throttle (2 tries, hours apart) from a transient network blip stops applying.
    """
    exhausted = _error_file(
        db_session, normal_user, category=ErrorCategory.AUTH_OR_RATE_LIMIT, days_old=0
    )
    exhausted.retry_count = 2
    fresh = _error_file(
        db_session, normal_user, category=ErrorCategory.AUTH_OR_RATE_LIMIT, days_old=1
    )
    db_session.commit()

    ids = {f.id for f in service.identify_retriable_error_files(db_session, batch_size=5)}
    assert fresh.id in ids
    assert exhausted.id not in ids


def test_retriable_split_separates_youtube_downloads_from_transcriptions(
    service, db_session, normal_user
):
    """A download is "``source_url`` set and no ``storage_path``"; everything else is not.

    Catches the discriminator inverting or being dropped, which would send
    YouTube downloads through the 20-wide transcription batch instead of the
    3-wide download batch — the exact traffic pattern that gets an IP
    rate-limited, and the reason the split exists at all.
    """
    download = _error_file(
        db_session,
        normal_user,
        category=ErrorCategory.NETWORK_ERROR,
        days_old=0,
        source_url="https://example.com/watch?v=abc",
        storage_path="",
    )
    transcription = _error_file(
        db_session, normal_user, category=ErrorCategory.NETWORK_ERROR, days_old=1
    )

    youtube_files, transcription_files = service.identify_retriable_error_files_split(
        db_session, youtube_batch_size=1, transcription_batch_size=1
    )
    assert [f.id for f in youtube_files] == [download.id]
    assert [f.id for f in transcription_files] == [transcription.id]


# =============================================================================
# Unrecoverable PENDING downloads
# =============================================================================
def test_stuck_pending_downloads_match_only_unrecoverable_messages(
    service, db_session, normal_user
):
    """Only messages naming a permanent condition are converted to ERROR.

    Catches the pattern list being widened to anything transient: a file that
    failed on a network timeout would be marked ERROR and dropped out of the
    retry path entirely, so a recoverable download becomes a permanent failure
    with no automatic second chance.
    """
    shared = {
        "status": FileStatus.PENDING,
        "file_size": 0,
        "storage_path": "",
        "upload_time": NOW - timedelta(hours=2),
    }
    private = _file(db_session, normal_user, last_error_message="This is a private video", **shared)
    transient = _file(
        db_session, normal_user, last_error_message="Network timeout, please retry", **shared
    )

    ids = {f.id for f in service.identify_stuck_pending_download_files(db_session)}
    assert private.id in ids
    assert transient.id not in ids


# =============================================================================
# LLM tasks and false-positive failures
# =============================================================================
def test_stuck_llm_tasks_are_scoped_to_llm_types_and_six_hours(service, db_session, normal_user):
    """Six hours in_progress, and only for the three LLM task types.

    Catches ``transcription`` being added to the swept types: a legitimate
    multi-hour transcription of a long recording would be marked failed and its
    file reset, destroying work that was still running. The 1-hour LLM task is the
    control for the threshold itself.
    """
    media_file = _file(db_session, normal_user)
    stuck_llm = _task(
        db_session,
        normal_user,
        media_file,
        task_type="summarization",
        status="in_progress",
        created_at=NOW - timedelta(hours=7),
    )
    young_llm = _task(
        db_session,
        normal_user,
        media_file,
        task_type="summarization",
        status="in_progress",
        created_at=NOW - timedelta(hours=1),
    )
    long_transcription = _task(
        db_session,
        normal_user,
        media_file,
        task_type="transcription",
        status="in_progress",
        created_at=NOW - timedelta(hours=7),
    )

    ids = {t.id for t in service.identify_stuck_llm_tasks(db_session)}
    assert stuck_llm.id in ids
    assert young_llm.id not in ids
    assert long_transcription.id not in ids


def test_false_positive_failures_match_the_exact_recovery_message(service, db_session, normal_user):
    """Only the recovery sweeper's own message marks a failure as suspect.

    Catches the match being loosened to a substring or an ``ilike``: a genuine
    task failure whose message merely mentions "stuck in processing" would be
    reset to pending and re-dispatched in a loop. The 5-day-old row is the control
    for the recency window that stops the sweeper resurrecting ancient failures.
    """
    media_file = _file(db_session, normal_user)
    recovered = _task(
        db_session,
        normal_user,
        media_file,
        status="failed",
        error_message="Task recovered after being stuck in processing",
        created_at=NOW - timedelta(hours=1),
    )
    genuine = _task(
        db_session,
        normal_user,
        media_file,
        status="failed",
        error_message="ffmpeg exited with code 1",
        created_at=NOW - timedelta(hours=1),
    )
    too_old = _task(
        db_session,
        normal_user,
        media_file,
        status="failed",
        error_message="Task recovered after being stuck in processing",
        created_at=NOW - timedelta(days=5),
    )

    ids = {t.id for t in service.identify_false_positive_failed_tasks(db_session)}
    assert recovered.id in ids
    assert genuine.id not in ids
    assert too_old.id not in ids


# =============================================================================
# Incomplete post-transcription detection: LLM speaker-ID existence proxy
# =============================================================================
def test_rejecting_every_suggestion_on_a_file_does_not_re_offer_speaker_id(
    service, db_session, normal_user
):
    """Audit follow-up to issue #603: ``suggestion_source`` must survive rejection.

    ``identify_incomplete_post_transcription_files`` treats
    ``Speaker.suggestion_source == "llm_analysis"`` as its *sole* existence
    proxy for "has LLM speaker ID already run on this file". A regression in
    ``_reject_speaker_suggestion`` (``api/endpoints/speakers.py``) used to null
    that column on rejection alongside ``suggested_name``/``confidence``, so a
    file where the user rejected every suggested speaker looked
    never-identified and was flagged ``missing_speaker_id`` again — re-offering
    (and re-dispatching) the exact suggestion the user had just rejected, held
    off only by the ~30 minute ``recently_attempted`` cooldown rather than
    actually prevented.

    This method's own module docstring above notes it is otherwise
    ambient-data dependent (a shared dev database full of old COMPLETED files
    can crowd this row out of the ``limit(batch_size * 3)`` window). Dating
    ``completed_at`` to year 1000 — far earlier than any real data — sorts
    this row first in the ``completed_at ASC`` candidate query regardless of
    what else is in the table, the same technique this suite's ``ANCIENT``
    constant uses elsewhere.
    """
    from unittest.mock import patch

    from app.api.endpoints.speakers import _reject_speaker_suggestion
    from app.models.media import Speaker

    media_file = _file(
        db_session,
        normal_user,
        status=FileStatus.COMPLETED,
        completed_at=datetime(1000, 1, 1, tzinfo=UTC),
    )
    speaker = Speaker(
        user_id=normal_user.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        suggested_name="Jane Doe",
        suggestion_source="llm_analysis",
        confidence=0.9,
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)

    with patch.object(TaskDetectionService, "_check_llm_configured_for_user", return_value=True):
        _reject_speaker_suggestion(speaker, speaker.id, db_session)

        results = service.identify_incomplete_post_transcription_files(db_session)

    ours = [r for r in results if r.media_file_id == media_file.id]
    assert ours, "the file must still be reported (other post-transcription steps are missing)"
    assert ours[0].missing_speaker_id is False, (
        "a rejected-but-recorded LLM suggestion must not be re-offered as "
        "missing speaker identification"
    )
