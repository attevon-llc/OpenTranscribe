"""Editing a segment must never CACHE a detector outage as a clean result.

The two fail-closed fixes that came before this one (chat input masking, the
detector layer) fail open **per request**: a broken detector leaks that turn, and
the next turn with a working detector is fine. The segment-edit path is different
in kind, because it *writes*:

    edit text -> re-detect inline -> segment.redactions = <what came back>

``detect_segment_spans`` swallows a detector exception and returns the spans it
did collect, so "found nothing" and "could not look" are the same value (issue
#324). Persisting that value put the outage in the database: every later read
took the cached-span path, found nothing to mask, and sent the segment on. The
detector never had to fail again — the leak outlived its cause, and nothing
distinguished "we looked and found no PII" from "we could not look".

These tests need no Presidio. They patch ``_get_analyzer``, which is the seam an
absent install produces anyway, and stub the toxicity scorer (a ~500 MB download
the fast suite must not make). Everything else — the real endpoint function, the
real config resolution, the real chat masker — is the code under test.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core import constants as C  # noqa: N812
from app.schemas.media import TranscriptSegmentUpdate
from app.services.redaction.detectors import pii_presidio

PHONE = "555-867-5309"
ORIGINAL_TEXT = "nothing sensitive here at all"
EDITED_TEXT = f"call me on {PHONE} about the invoice"


class _Result:
    """The shape ``AnalyzerEngine.analyze`` returns (only the fields we read)."""

    def __init__(self, entity_type: str, start: int, end: int, score: float) -> None:
        self.entity_type = entity_type
        self.start = start
        self.end = end
        self.score = score


class _WorkingAnalyzer:
    """Finds the one phone number and nothing else."""

    def analyze(self, text: str, language: str):  # noqa: ARG002
        start = text.find(PHONE)
        if start < 0:
            return []
        return [_Result("PHONE_NUMBER", start, start + len(PHONE), 0.9)]


class _BrokenAnalyzer:
    """Built successfully, then throws — a transient fault, not a missing install."""

    def analyze(self, text: str, language: str):  # noqa: ARG002
        raise RuntimeError("nlp engine exploded")


@pytest.fixture
def no_toxicity_model(monkeypatch):
    """Keep the toxicity weights out of the fast suite; every other detector is real."""
    from app.services.redaction.detectors import toxicity as tox

    monkeypatch.setattr(tox, "score_text", lambda _text, _lang: None)
    monkeypatch.setattr(tox, "score_texts", lambda texts, _lang: [None] * len(texts))


@pytest.fixture
def queued_rescans(monkeypatch):
    """Capture ``redaction_detect_task.delay`` — there is no broker in this suite."""
    from app.tasks import redaction_task

    calls: list[dict] = []
    monkeypatch.setattr(
        redaction_task.redaction_detect_task,
        "delay",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


def _set_prefs(db_session, user, **prefs: str) -> None:
    from app import models

    for key, value in prefs.items():
        db_session.add(models.UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.flush()


@pytest.fixture
def pii_masking_user(db_session, normal_user):
    """An owner who explicitly turned redaction AND the PII category on.

    PII is deliberately not a default category, so a user who wants names and
    phone numbers masked has to ask — and that ask is exactly what makes a PII
    detector outage blocking for their files.
    """
    _set_prefs(
        db_session,
        normal_user,
        redaction_enabled="true",
        redaction_categories='["profanity", "toxicity", "custom", "pii"]',
        redaction_redact_before_llm="true",
    )
    return normal_user


@pytest.fixture
def default_categories_user(db_session, normal_user):
    """The CPU-only deployment this fix must not touch: redaction on, PII never asked for."""
    _set_prefs(
        db_session,
        normal_user,
        redaction_enabled="true",
        redaction_redact_before_llm="true",
    )
    return normal_user


def _seed_scanned_file(db_session, user):
    """A completed file whose redaction scan already finished cleanly."""
    from app.core.enums import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"edit-redetect-{uuid_pkg.uuid4().hex[:8]}.wav",
        storage_path=f"redaction-test/{uuid_pkg.uuid4().hex}",
        file_size=1,
        content_type="audio/wav",
        language="en",
        status=FileStatus.COMPLETED,
        redaction_status=C.REDACTION_STATUS_DONE,
        redaction_model_version=C.REDACTION_MODEL_VERSION,
    )
    db_session.add(media)
    db_session.flush()
    segment = TranscriptSegment(
        uuid=uuid_pkg.uuid4(),
        media_file_id=media.id,
        start_time=0.0,
        end_time=5.0,
        text=ORIGINAL_TEXT,
        redactions=None,
    )
    db_session.add(segment)
    db_session.flush()
    return media, segment


def _edit(db_session, media, segment, user, text=EDITED_TEXT):
    """Drive the REAL endpoint function, not the helper it calls."""
    from app.api.endpoints.files.crud import update_single_transcript_segment

    return update_single_transcript_segment(
        db_session,
        str(media.uuid),
        str(segment.uuid),
        TranscriptSegmentUpdate(text=text),
        user,
    )


# ------------------------------------------------------------------- the control


def test_a_working_detector_still_caches_spans_for_the_edited_text(
    db_session, pii_masking_user, no_toxicity_model, queued_rescans, monkeypatch
):
    """CONTROL. Without this, a path that marked EVERY edit stale would pass the rest.

    It also pins the alignment property the whole design turns on: the spans that
    get persisted are detected against the NEW text, so their offsets address the
    text they are stored beside.
    """
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _WorkingAnalyzer())
    media, segment = _seed_scanned_file(db_session, pii_masking_user)

    result = _edit(db_session, media, segment, pii_masking_user)

    assert result.text == EDITED_TEXT
    db_session.refresh(segment)
    db_session.refresh(media)
    spans = segment.redactions or []
    assert [s["entity_type"] for s in spans] == ["PHONE"]
    covered = EDITED_TEXT[spans[0]["char_start"] : spans[0]["char_end"]]
    assert covered == PHONE, "the persisted span must address the edited text, not the old one"
    assert media.redaction_status == C.REDACTION_STATUS_DONE
    assert queued_rescans == [], "a successful scan needs no repair"


# --------------------------------------------------- what the edit must NOT persist


def test_an_unavailable_pii_detector_is_not_persisted_as_a_clean_segment(
    db_session, pii_masking_user, no_toxicity_model, queued_rescans, monkeypatch
):
    """THE defect. The edit succeeds; what it writes must not claim the segment is clean.

    ``redaction_status`` is the file-level record of whether the cached spans can
    be trusted, and every status-aware reader already understands ``pending``:
    the transcript read withholds, the chat cached path refuses, ``llm_guard``
    defers. Leaving it ``done`` beside an unexamined segment is the leak.
    """
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)
    media, segment = _seed_scanned_file(db_session, pii_masking_user)

    result = _edit(db_session, media, segment, pii_masking_user)

    assert result.text == EDITED_TEXT, "a detector outage must not block a transcript edit"
    db_session.refresh(media)
    assert media.redaction_status == C.REDACTION_STATUS_PENDING, (
        "an unexamined segment cached under status=done is a permanent leak"
    )
    assert queued_rescans == [{"file_id": media.id, "user_id": pii_masking_user.id}], (
        "the API process has no Presidio; only a worker re-scan can repair this"
    )


def test_a_detector_that_ran_and_raised_is_also_not_cached_as_clean(
    db_session, pii_masking_user, no_toxicity_model, queued_rescans, monkeypatch
):
    """Twin of the above with the other fault injected.

    ``detect_and_store`` treats these two differently (FAILED vs skipped) because
    re-running installs nothing. The edit path must NOT inherit that split: both
    mean this segment was never examined, and both are repaired by the same
    worker re-scan.
    """
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _BrokenAnalyzer())
    media, segment = _seed_scanned_file(db_session, pii_masking_user)

    _edit(db_session, media, segment, pii_masking_user)

    db_session.refresh(media)
    assert media.redaction_status == C.REDACTION_STATUS_PENDING
    assert len(queued_rescans) == 1


def test_re_detection_failing_outright_is_also_not_cached_as_clean(
    db_session, pii_masking_user, no_toxicity_model, queued_rescans, monkeypatch
):
    """The sibling hole: the whole re-detection blowing up, not just one detector.

    That was a ``logger.warning`` and nothing else, which left the file at
    ``done`` carrying the PREVIOUS text's spans against the NEW text — offsets
    that address different characters now, so the mask lands in the wrong place
    and the edited content is disclosed anyway.
    """
    from app.services.redaction.service import RedactionService

    def _explode(*_args, **_kwargs):
        raise RuntimeError("detector layer is wedged")

    monkeypatch.setattr(RedactionService, "detect_segment_spans", staticmethod(_explode))
    media, segment = _seed_scanned_file(db_session, pii_masking_user)

    result = _edit(db_session, media, segment, pii_masking_user)

    assert result.text == EDITED_TEXT
    db_session.refresh(media)
    assert media.redaction_status == C.REDACTION_STATUS_PENDING
    assert len(queued_rescans) == 1


# --------------------------------------------------- what a later READ then sees


def test_a_later_chat_turn_does_not_send_the_edited_segment_unmasked(
    db_session, pii_masking_user, no_toxicity_model, queued_rescans, monkeypatch
):
    """The consequence the edit-side assertions cannot see.

    Watching the edit request succeed proves nothing: the defect is entirely in
    what it WROTE. This drives the real chat input masker over a chunk covering
    the edited segment, which is the path that egresses to a provider.
    """
    from app.services.chat.redactor import mask_chunks
    from app.services.search.chunk_retrieval import ChunkHit

    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)
    media, segment = _seed_scanned_file(db_session, pii_masking_user)
    _edit(db_session, media, segment, pii_masking_user)

    chunk = ChunkHit(
        file_uuid=str(media.uuid),
        file_id=media.id,
        chunk_index=0,
        content=EDITED_TEXT,
        start_time=0.0,
        end_time=5.0,
    )
    masked = mask_chunks(db_session, [chunk], pii_masking_user.id)

    assert len(masked) == 1
    assert PHONE not in masked[0].content, "the cached outage leaked the edited text to the LLM"
    assert masked[0].content == "", "an unmaskable chunk must contribute nothing"


def test_the_transcript_read_withholds_until_the_repair_scan_lands(
    db_session, pii_masking_user, no_toxicity_model, queued_rescans, monkeypatch
):
    """The other read surface: the file detail response.

    ``_redaction_pending`` is the existing gate, and it keys off exactly the
    status the edit path now writes — no new concept, no new reader.
    """
    from app.api.endpoints.files.crud import _redaction_pending
    from app.services.redaction.config import resolve_effective_config

    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)
    media, segment = _seed_scanned_file(db_session, pii_masking_user)
    cfg = resolve_effective_config(db_session, pii_masking_user.id)
    assert _redaction_pending(db_session, cfg, media) is False, "a scanned file starts readable"

    _edit(db_session, media, segment, pii_masking_user)
    db_session.refresh(media)

    assert _redaction_pending(db_session, cfg, media) is True


# ------------------------------------------------------- the queue has to DRAIN


def test_the_queued_rescan_drains_to_a_terminal_status_and_cannot_re_queue(
    db_session, pii_masking_user, no_toxicity_model, queued_rescans, monkeypatch
):
    """A queue that can never drain is its own defect.

    The worry: CPU-only box, ``pii`` enabled, Presidio never coming back — edit
    sets ``pending``, the re-scan meets the same unavailability, sets ``pending``
    again, and the file is stuck on the slow inline path forever while every edit
    piles on more work.

    It cannot happen, and the reason is ``e6048808``'s unavailable-vs-failed
    split. ``detect_and_store`` subtracts unavailability before the FAILED
    decision and finishes ``done`` with the detector reported in
    ``skipped_detectors``. It writes ``processing`` then ``done`` or ``failed``
    and **never writes** ``pending`` — so the only writer of ``pending`` is an
    edit, and one hop is the whole cycle.

    (The file then returns to the cached path with a scan that never ran PII.
    That is the residual hazard #78 pins, pre-existing and unchanged here: this
    fix stops the edit path from CREATING it, and does not claim to close it.)
    """
    from app.services.redaction.service import RedactionService

    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)
    media, segment = _seed_scanned_file(db_session, pii_masking_user)
    _edit(db_session, media, segment, pii_masking_user)
    db_session.refresh(media)
    assert media.redaction_status == C.REDACTION_STATUS_PENDING

    # What the queued task does, running the real detect_and_store.
    result = RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert result["status"] == "done"
    assert media.redaction_status == C.REDACTION_STATUS_DONE, (
        "the repair scan must reach a terminal status; re-writing pending is the loop"
    )
    assert "pii" in result["skipped_detectors"]
    assert len(queued_rescans) == 1, "the repair scan must not queue another repair scan"


def test_a_broken_detector_drains_to_failed_rather_than_looping(
    db_session, pii_masking_user, no_toxicity_model, queued_rescans, monkeypatch
):
    """The other arm of the split, and the reason it is a split.

    A detector that RAN and threw is worth re-running, so the repair scan ends
    ``failed`` — terminal, and loud. ``llm_guard`` then refuses non-retryably
    (correct: the detector genuinely broke) and chat's cached path still refuses
    a non-``done`` file and masks inline. Terminal either way; the difference is
    only which terminal.
    """
    from app.services.redaction.service import RedactionService

    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _BrokenAnalyzer())
    media, segment = _seed_scanned_file(db_session, pii_masking_user)
    _edit(db_session, media, segment, pii_masking_user)

    result = RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert result["status"] == "failed"
    assert media.redaction_status == C.REDACTION_STATUS_FAILED
    assert len(queued_rescans) == 1


def test_editing_again_while_a_rescan_is_pending_does_not_queue_a_second_one(
    db_session, pii_masking_user, no_toxicity_model, queued_rescans, monkeypatch
):
    """Editing five segments in a row must not queue five full-file scans.

    Each scan re-detects the WHOLE file, so a per-edit dispatch would multiply
    CPU work on the redaction worker for no additional coverage.
    """
    from app.models.media import TranscriptSegment

    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)
    media, segment = _seed_scanned_file(db_session, pii_masking_user)
    second = TranscriptSegment(
        uuid=uuid_pkg.uuid4(),
        media_file_id=media.id,
        start_time=6.0,
        end_time=9.0,
        text="and another thing entirely",
    )
    db_session.add(second)
    db_session.flush()

    _edit(db_session, media, segment, pii_masking_user)
    _edit(db_session, media, second, pii_masking_user, text=f"reach me on {PHONE} instead")

    db_session.refresh(media)
    assert media.redaction_status == C.REDACTION_STATUS_PENDING
    assert len(queued_rescans) == 1, "the second edit rode the scan the first one queued"


# ----------------------------------------------------------------- the narrowness


def test_a_deployment_that_never_enabled_pii_is_completely_unaffected(
    db_session, default_categories_user, no_toxicity_model, queued_rescans, monkeypatch
):
    """A CPU-only box with no Presidio that never asked for PII masking.

    Withholding on every detector failure regardless of policy would take this
    deployment's transcript reads away and queue a pointless re-scan on every
    single segment edit, for a category it never enabled. Only a failure feeding
    an ENABLED category may withhold — that narrowness is the whole reason
    ``blocking_detector_failures`` exists.
    """
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)
    media, segment = _seed_scanned_file(db_session, default_categories_user)

    result = _edit(db_session, media, segment, default_categories_user)

    assert result.text == EDITED_TEXT
    db_session.refresh(media)
    assert media.redaction_status == C.REDACTION_STATUS_DONE, (
        "a category this user never enabled must not withhold their transcript"
    )
    assert queued_rescans == []


def test_the_owners_policy_decides_not_the_editors(
    db_session, pii_masking_user, admin_user, no_toxicity_model, queued_rescans, monkeypatch
):
    """An admin with PII off editing a PII-masking owner's file.

    The content belongs to the owner and the owner's policy governs it — the same
    subject ``llm_guard.resolve_llm_masking`` uses. Resolving the EDITOR's config
    here would let an admin whose own redaction is off cache an unexamined
    segment as clean in someone else's transcript.
    """
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)
    media, segment = _seed_scanned_file(db_session, pii_masking_user)

    _edit(db_session, media, segment, admin_user)

    db_session.refresh(media)
    assert media.redaction_status == C.REDACTION_STATUS_PENDING
    assert queued_rescans == [{"file_id": media.id, "user_id": pii_masking_user.id}]
