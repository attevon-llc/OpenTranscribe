"""A finished scan is not necessarily a complete one, and ``done`` cannot say which.

``redaction_status = done`` used to mean "the scan finished AND every detector examined
the text". ``e6048808`` split those on purpose: an *unavailable* detector now resolves to
``done`` with the detector reported in ``skipped_detectors``, because ``failed`` is turned
into a permanent, non-retryable refusal by ``llm_guard`` and flipping every file on a
deployment that merely lacks Presidio would break summarization, speaker identification
and topic extraction for good.

The residual that commit named and did not close is here: every read path that trusts
cached spans on the strength of ``done`` can now mask nothing, report success, and hand a
transcript to an LLM provider. ``mask_segment`` over an empty span list returns the input
unchanged — there is no error, no log line, and no way for the caller to tell.

These tests drive the **real** egress path — ``resolve_llm_masking`` followed by
``build_full_transcript``, which is exactly what ``tasks/summarization.py``,
``tasks/speaker_identification_task.py`` and ``tasks/topic_extraction.py`` do before
posting transcript text off-box — and the real ``detect_and_store``. Only the detector
weights are absent, which is the seam a deployment without them produces anyway.

The controls matter as much as the assertions. Three of them pin the deployments this
change must not touch: one that never asked for PII masking, one with no toxicity weights
(``toxicity`` **is** a default category, so getting that arm wrong breaks every
redaction-enabled user at once), and one whose files were scanned before the coverage
column existed.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core import constants as C  # noqa: N812
from app.services.redaction.detectors import pii_presidio
from app.services.redaction.detectors import toxicity as tox
from app.services.redaction.llm_guard import RedactionNotReadyError
from app.services.redaction.llm_guard import resolve_llm_masking
from app.services.redaction.service import RedactionService
from app.utils.transcript_builders import build_full_transcript

PHONE = "555-867-5309"
SEGMENT_TEXT = f"call me on {PHONE} about the invoice"


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


class _ThrowingPipe:
    """Toxicity weights present, inference broken — a fault a re-run may well clear."""

    def __call__(self, *_args, **_kwargs):
        raise RuntimeError("toxicity pipeline exploded")


# ------------------------------------------------------------------------ fixtures


@pytest.fixture
def working_pii(monkeypatch):
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _WorkingAnalyzer())


@pytest.fixture
def absent_pii(monkeypatch):
    """The seam a deployment with no Presidio install actually produces."""
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)


@pytest.fixture
def working_toxicity(monkeypatch):
    """Toxicity 'ran' and found nothing — keeps the ~500 MB weights out of the fast suite."""
    monkeypatch.setattr(tox, "score_text", lambda _text, _lang: None)
    monkeypatch.setattr(tox, "score_texts", lambda texts, _lang: [None] * len(texts))


@pytest.fixture
def absent_toxicity(monkeypatch):
    """No weights on this box — ``_get_pipe`` is where that becomes visible."""
    monkeypatch.setattr(tox, "_get_pipe", lambda _model_name: None)


@pytest.fixture
def broken_toxicity(monkeypatch):
    monkeypatch.setattr(tox, "_get_pipe", lambda _model_name: _ThrowingPipe())


@pytest.fixture
def queued_rescans(monkeypatch):
    """Capture ``redaction_detect_task.delay`` — there is no broker in this suite."""
    from app.tasks import redaction_task

    calls: list[dict] = []
    monkeypatch.setattr(
        redaction_task.redaction_detect_task, "delay", lambda **kwargs: calls.append(kwargs)
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

    PII is deliberately not in ``DEFAULT_REDACTION_CATEGORIES``, so wanting names and
    phone numbers masked is an opt-in — and that opt-in is exactly what makes a PII
    coverage gap blocking for their files.
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
    """The CPU-only deployment this change must not touch: redaction on, PII never asked for."""
    _set_prefs(
        db_session,
        normal_user,
        redaction_enabled="true",
        redaction_redact_before_llm="true",
    )
    return normal_user


def _seed_unscanned_file(db_session, user, language: str = "en"):
    """A completed transcript whose redaction scan has not run yet."""
    from app.core.enums import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"coverage-{uuid_pkg.uuid4().hex[:8]}.wav",
        storage_path=f"redaction-test/{uuid_pkg.uuid4().hex}",
        file_size=1,
        content_type="audio/wav",
        language=language,
        status=FileStatus.COMPLETED,
    )
    db_session.add(media)
    db_session.flush()
    segment = TranscriptSegment(
        uuid=uuid_pkg.uuid4(),
        media_file_id=media.id,
        start_time=0.0,
        end_time=5.0,
        text=SEGMENT_TEXT,
        redactions=None,
    )
    db_session.add(segment)
    db_session.flush()
    return media, segment


def _egress(db_session, media, segment) -> tuple[RedactionNotReadyError | None, str | None]:
    """Drive the real pre-LLM gate and, if it releases, the real transcript builder.

    Returns ``(refusal, outgoing)`` — exactly one is populated. ``outgoing`` is the
    string the provider would have received, so a failing assertion can print it.
    """
    try:
        cfg = resolve_llm_masking(db_session, media)
    except RedactionNotReadyError as exc:
        return exc, None
    return None, build_full_transcript([segment], cfg)


# --------------------------------------------------------------------- THE DEFECT


def test_a_scan_that_never_ran_pii_is_not_released_to_an_llm_provider(
    db_session, pii_masking_user, absent_pii, working_toxicity
):
    """THE P0. Measured through the gate the three LLM tasks actually call.

    Nothing in the sequence looks wrong from the inside: detection completes, the file
    reaches ``done``, the owner's policy resolves, masking is applied, and the resulting
    text is the input. The only thing that could have told the gate otherwise is a
    durable record of what the scan examined.
    """
    media, segment = _seed_unscanned_file(db_session, pii_masking_user)
    result = RedactionService.detect_and_store(db_session, media.id)
    assert result["status"] == "done", "an unavailable detector must not fail the scan"

    db_session.refresh(media)
    refusal, outgoing = _egress(db_session, media, segment)

    assert refusal is not None, (
        "the gate released a transcript whose PII detector never ran; the provider "
        f"would have received this verbatim: {outgoing!r}"
    )
    assert "pii" in str(refusal), refusal
    assert refusal.retryable is False, "re-running a scan does not install Presidio"


def test_masking_cached_spans_cannot_mask_what_was_never_detected(
    db_session, pii_masking_user, absent_pii, working_toxicity
):
    """Names what the refusal prevents, so the reason is not left implicit.

    ``mask_segment`` is a transform over the cached spans and nothing else. Given none,
    it returns the input — no error, no log line, and ``was_masked``-style flags
    everywhere upstream still read true. This is the property that makes trusting
    ``done`` alone a leak rather than a degradation.
    """
    from app.services.redaction.config import resolve_effective_config

    media, segment = _seed_unscanned_file(db_session, pii_masking_user)
    RedactionService.detect_and_store(db_session, media.id)
    db_session.refresh(segment)

    assert segment.redactions is None, "an absent Presidio finds nothing, correctly"
    cfg = resolve_effective_config(db_session, pii_masking_user.id)
    assert PHONE in build_full_transcript([segment], cfg), (
        "masking an unexamined segment is a no-op that reports success"
    )


def test_an_unavailable_pii_detector_is_recorded_on_the_file(
    db_session, pii_masking_user, absent_pii, working_toxicity
):
    """The durable half. ``skipped_detectors`` is a task RETURN VALUE and does not survive.

    It reaches a Celery result backend with a TTL and a WebSocket toast; a masker an hour
    later can read neither. The column is what a read path can actually consult.
    """
    media, _segment = _seed_unscanned_file(db_session, pii_masking_user)
    result = RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert media.redaction_status == C.REDACTION_STATUS_DONE
    assert media.redaction_coverage == ["profanity", "toxicity"], media.redaction_coverage
    assert "pii" in result["skipped_detectors"]


# ------------------------------------------------------------------- the controls


def test_a_complete_scan_still_releases_the_transcript_and_masks_it(
    db_session, pii_masking_user, working_pii, working_toxicity
):
    """CONTROL. Without this, a gate that refused EVERYTHING would pass every test above."""
    media, segment = _seed_unscanned_file(db_session, pii_masking_user)
    RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    db_session.refresh(segment)
    assert media.redaction_coverage == ["profanity", "pii", "toxicity"]

    refusal, outgoing = _egress(db_session, media, segment)
    assert refusal is None, f"a fully covered scan must not be refused: {refusal}"
    assert outgoing is not None
    assert PHONE not in outgoing, outgoing
    assert "[PHONE]" in outgoing, outgoing


def test_a_deployment_that_never_enabled_pii_is_untouched(
    db_session, default_categories_user, absent_pii, working_toxicity
):
    """CONTROL. The narrowness is the reason this control can be turned on at all.

    ``pii`` is not a default category, so the CPU-only deployment with no Presidio — the
    common one — asked for nothing the missing detector provides and must lose nothing.
    """
    media, segment = _seed_unscanned_file(db_session, default_categories_user)
    RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    refusal, outgoing = _egress(db_session, media, segment)
    assert refusal is None, f"a policy that never masks pii must not be blocked by it: {refusal}"
    assert outgoing is not None and PHONE in outgoing, (
        "this user does not mask PII, so their own transcript reaches their own provider"
    )


def test_a_file_scanned_before_the_coverage_column_existed_is_trusted(
    db_session, pii_masking_user, working_toxicity
):
    """CONTROL for the documented residual, asserted rather than left to a comment.

    NULL coverage cannot be distinguished from full coverage retroactively, and reading
    it as "nothing was examined" would refuse every pre-existing file on every deployment
    on upgrade day. It is read as "no worse than yesterday"; ``redaction.reindex_all``
    is the remedy, and re-scanning writes the column.
    """
    media, segment = _seed_unscanned_file(db_session, pii_masking_user)
    media.redaction_status = C.REDACTION_STATUS_DONE
    media.redaction_coverage = None
    db_session.flush()

    refusal, outgoing = _egress(db_session, media, segment)
    assert refusal is None, f"a pre-v391 row must not be refused: {refusal}"
    assert outgoing is not None


def test_a_non_english_transcript_is_not_refused_for_a_language_limit(
    db_session, pii_masking_user, working_pii, working_toxicity
):
    """CONTROL. A declared capability limit is not a coverage gap.

    Profanity and PII are English-only by design and unsupported languages already come
    back as ``skipped_detectors``. Unlike an absent dependency, no operator action can
    change that — so treating it as a gap would withhold every non-English transcript
    from every LLM feature permanently, which is a different decision from this one.
    """
    media, segment = _seed_unscanned_file(db_session, pii_masking_user, language="es")
    RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert "pii" not in (media.redaction_coverage or []), "Spanish PII detection cannot run"
    refusal, outgoing = _egress(db_session, media, segment)
    assert refusal is None, f"a language limit must not withhold the transcript: {refusal}"
    assert outgoing is not None


# ------------------------------------------------- task 2: toxicity swallows the same way


def test_an_absent_toxicity_model_is_reported_rather_than_swallowed(
    db_session, default_categories_user, working_pii, absent_toxicity
):
    """THE P1. ``score_texts`` returned ``[None] * len(texts)`` when the weights were absent.

    That is the same value a transcript of blank segments produces, so ``detect_and_store``
    wrote a full column of NULL toxicity scores, reported the detector as having run, and
    nothing anywhere recorded that no segment was ever examined for toxicity.
    """
    media, _segment = _seed_unscanned_file(db_session, default_categories_user)
    result = RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert result["status"] == "done", "missing weights are not a failure — re-running gets none"
    assert "toxicity" in result["skipped_detectors"], result
    assert media.redaction_coverage == ["profanity", "pii"], media.redaction_coverage


def test_a_toxicity_model_that_throws_marks_the_scan_failed(
    db_session, default_categories_user, working_pii, broken_toxicity
):
    """The other half of the split: it ran and raised, so re-running is worth doing.

    ``score_texts`` used to catch this itself and return a list of ``None``, so the batch
    ``except`` in ``detect_and_store`` — which already appended ``toxicity`` to the
    failures — could never fire for an inference error.
    """
    media, _segment = _seed_unscanned_file(db_session, default_categories_user)
    result = RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert result["status"] == "failed", result
    assert result["detectors"] == ["toxicity"]
    assert media.redaction_status == C.REDACTION_STATUS_FAILED


def test_a_box_without_toxicity_weights_still_answers_every_llm_feature(
    db_session, default_categories_user, working_pii, absent_toxicity
):
    """CONTROL, and the disaster this fix is shaped to avoid.

    ``toxicity`` IS a default category, so routing its outage into the blocking sink the
    obvious way would refuse every LLM feature for every redaction-enabled user on any
    box without the ~500 MB weights. It must not, and the reason is not pragmatism: the
    toxicity detector emits a per-segment SCORE and never a span, so its absence cannot
    leave one character unmasked. What it costs is the toxic FLAG, which is reported.
    """
    media, segment = _seed_unscanned_file(db_session, default_categories_user)
    RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert "toxicity" not in (media.redaction_coverage or []), "the gap is recorded"
    refusal, outgoing = _egress(db_session, media, segment)
    assert refusal is None, f"a detector that masks nothing must withhold nothing: {refusal}"
    assert outgoing is not None


def test_a_toxicity_outage_never_makes_a_masker_withhold_text(db_session, default_categories_user):
    """The rule above, asserted where it is decided rather than only where it is felt.

    ``blocking_detector_failures`` is read by three fail-closed maskers and by the
    segment-edit path. A ``toxicity`` entry that mapped to the ``toxicity`` category
    would make all four withhold on a detector that produces no spans.
    """
    from app.services.redaction.config import blocking_detector_failures
    from app.services.redaction.config import resolve_effective_config

    cfg = resolve_effective_config(db_session, default_categories_user.id)
    assert "toxicity" in cfg.enabled_categories, "the premise: it is a DEFAULT category"
    assert blocking_detector_failures(["toxicity"], cfg.enabled_categories) == set()
    assert blocking_detector_failures(["profanity"], cfg.enabled_categories) == {"profanity"}, (
        "control — a detector that DOES emit spans still blocks"
    )


def test_a_segment_edit_without_toxicity_weights_does_not_queue_a_rescan(
    db_session, default_categories_user, working_pii, absent_toxicity, queued_rescans
):
    """CONTROL. The rescan storm the naive fix produces, driven through the real endpoint.

    The API process never preloads toxicity weights, so if a toxicity outage were
    blocking, EVERY segment edit by EVERY redaction-enabled user would mark the file
    stale, withhold the transcript, and queue a full CPU re-scan that would end in
    exactly the same state.
    """
    from app.api.endpoints.files.crud import update_single_transcript_segment
    from app.schemas.media import TranscriptSegmentUpdate

    media, segment = _seed_unscanned_file(db_session, default_categories_user)
    RedactionService.detect_and_store(db_session, media.id)
    db_session.refresh(media)
    queued_rescans.clear()

    result = update_single_transcript_segment(
        db_session,
        str(media.uuid),
        str(segment.uuid),
        TranscriptSegmentUpdate(text="a different sentence entirely"),
        default_categories_user,
    )

    assert result.text == "a different sentence entirely"
    db_session.refresh(media)
    assert media.redaction_status == C.REDACTION_STATUS_DONE, (
        "a detector that masks nothing must not mark the file stale"
    )
    assert queued_rescans == []
