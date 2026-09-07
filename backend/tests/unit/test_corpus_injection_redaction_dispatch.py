"""An injected corpus must reach the same lifecycle state a transcribed one does.

``inject_meeting`` writes ``MediaFile`` / ``Speaker`` / ``TranscriptSegment`` rows
directly and is the **only** writer in the codebase that produces a
``FileStatus.COMPLETED`` file with a transcript without ever entering the
transcription pipeline. Every other creator — ``files/upload.py``,
``files/url_processing.py``, ``watch_sources/processing.py``,
``media_download_service.py``, ``storage_recovery_service.py`` — writes a
pre-transcription status and is finished by ``tasks/transcription/postprocess.py``,
whose ``_dispatch_redaction`` hands the file to the detection scan.

Injection skipped that step, so an injected row is born with
``redaction_status`` NULL. For an owner who has redaction enabled that is not
cosmetic: ``api/endpoints/files/crud._redaction_pending`` withholds the
transcript for NULL, so ``GET /api/files/{uuid}`` and ``/segments`` answer
``transcript_segments: []`` with ``redaction_pending: true``. Measured on the dev
stack 2026-09-07 against a freshly injected 6-segment meeting: 6 rows in
``transcript_segment``, ``total_segments: 0`` through the API.

**The repair is here, not in the gate.** NULL-means-withheld is deliberate
fail-closed behaviour shared by the transcript read, the subtitle download, the
bulk-export ZIP and the burned-in-subtitle render (see
``services/redaction/export_policy``); teaching any of them that NULL is
readable would export a transcript no detector ever examined. The injector
instead does what the pipeline does — it asks the same question
``_dispatch_redaction`` asks (does this owner mask?) and queues the same task.

Deliberately **not** "stamp ``done`` and move on": ``done`` means a scan
finished, ``redaction_coverage`` records which detectors ran, and a row claiming
``done`` with no coverage is precisely the "complete-looking span cache that has
no PII in it" that ``services/redaction/llm_guard`` exists to refuse. Writing a
terminal status nothing measured would make every masking path silently
no-op while reporting success.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core import constants as C  # noqa: N812
from app.scripts.corpus_injection.injector import dispatch_redaction
from app.scripts.corpus_injection.injector import inject_meeting
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import Turn


def _set_prefs(db_session, user, **prefs: str) -> None:
    from app import models

    for key, value in prefs.items():
        db_session.add(models.UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.flush()


@pytest.fixture
def redacting_user(db_session, normal_user):
    _set_prefs(db_session, normal_user, redaction_enabled="true")
    return normal_user


@pytest.fixture
def non_redacting_user(db_session, normal_user):
    """The default deployment: ``DEFAULT_REDACTION_ENABLED`` is False."""
    _set_prefs(db_session, normal_user, redaction_enabled="false")
    return normal_user


@pytest.fixture
def broker(monkeypatch):
    """Capture ``redaction_detect_task.delay`` — this suite has no broker."""
    from app.tasks import redaction_task

    published: list[dict] = []

    class _Async:
        id = "queued-redaction-task-id"

    def _delay(**kwargs):
        published.append(kwargs)
        return _Async()

    monkeypatch.setattr(redaction_task.redaction_detect_task, "delay", _delay)
    return published


def _inject(db_session, user):
    doc = MeetingDoc(
        corpus="redaction_dispatch_test",
        meeting_id=uuid_pkg.uuid4().hex[:12],
        title="A meeting whose transcript should be readable",
        turns=[
            Turn(turn_index=0, speaker="A", text="First thing said.", start=0.0, end=2.0),
            Turn(turn_index=1, speaker="B", text="Second thing said.", start=2.0, end=4.0),
        ],
        language="en",
    )
    record, _ = inject_meeting(db_session, doc, user.id, seed=uuid_pkg.uuid4().hex[:8])
    db_session.flush()
    return record


# ------------------------------------------------------------- why this is needed


def test_a_freshly_injected_transcript_is_withheld_by_the_read_gate(
    db_session, redacting_user, broker
):
    """The state injection leaves behind, asserted against the real gate.

    Not a redaction test — a *precondition* test. It fixes in place the fact the
    dispatch below exists to resolve, so that if the gate's rule ever changes
    this file explains itself rather than looking like ceremony.
    """
    from app.api.endpoints.files.crud import _redaction_pending
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.services.redaction.config import resolve_effective_config

    record = _inject(db_session, redacting_user)
    media = db_session.get(MediaFile, record.media_file_id)

    assert media.redaction_status is None, "injection does not run the pipeline that sets it"
    stored = (
        db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == media.id)
        .count()
    )
    assert stored == 2, "the rows are really there"

    cfg = resolve_effective_config(db_session, redacting_user.id)
    assert _redaction_pending(db_session, cfg, media) is True, (
        "an injected transcript is invisible through the API until something scans it"
    )


# ------------------------------------------------------------------- the dispatch


def test_injection_hands_the_file_to_the_real_detection_scan(db_session, redacting_user, broker):
    """The same task ``postprocess._dispatch_redaction`` queues, for the same reason."""
    record = _inject(db_session, redacting_user)

    task_id = dispatch_redaction(db_session, record, redacting_user.id)

    assert [call["file_id"] for call in broker] == [record.media_file_id]
    assert broker[0]["user_id"] == redacting_user.id
    assert task_id == "queued-redaction-task-id"


def test_no_scan_is_queued_when_the_owner_does_not_redact(db_session, non_redacting_user, broker):
    """Redaction is opt-out, and the gate is the owner's config — not a flag here.

    A deployment with redaction off must not pay a CPU scan per injected meeting.
    Its rows keep ``redaction_status`` NULL, which is exactly what a real
    transcription leaves them at, and the read gate does not withhold because the
    reader's policy is disabled.
    """
    record = _inject(db_session, non_redacting_user)

    assert dispatch_redaction(db_session, record, non_redacting_user.id) is None
    assert broker == [], "no owner asked for masking; nothing to scan for"


def test_dispatch_mode_none_publishes_nothing(db_session, redacting_user, broker):
    """``--dispatch none`` is a rows-only run; it must stay rows-only."""
    record = _inject(db_session, redacting_user)

    assert dispatch_redaction(db_session, record, redacting_user.id, mode="none") is None
    assert broker == []


def test_the_injector_never_fabricates_a_terminal_redaction_status(
    db_session, redacting_user, broker
):
    """``done`` would mean "a detector examined this text". Nothing did.

    Guards the tempting shortcut. ``llm_guard.resolve_llm_masking`` trusts
    ``done`` and consults ``redaction_coverage`` to catch a scan that skipped a
    detector; a hand-written ``done`` with no coverage passes both checks and
    every masking call then returns the transcript verbatim while logging
    success.
    """
    from app.models.media import MediaFile

    record = _inject(db_session, redacting_user)
    media = db_session.get(MediaFile, record.media_file_id)

    # The exact expected value, not "not one of the terminal two". A negated
    # assertion here would also pass on `processing` or a typo'd constant — states
    # that are equally wrong and equally invisible.
    assert media.redaction_status is None, (
        f"injection must leave the status NULL — the honest 'no scan has run' — but "
        f"it is {media.redaction_status!r}. {C.REDACTION_STATUS_DONE!r} in particular "
        "would assert a scan that never happened."
    )
    assert media.redaction_coverage is None, (
        "coverage records which detectors examined the text; none did"
    )
