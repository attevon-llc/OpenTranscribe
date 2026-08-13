""" "Could not look" must never read as "found nothing" (issue #324, #403).

``pii_presidio`` used to swallow its own failure one level BELOW the ``failures``
sink that every fail-closed masker reads. Measured on the real detector layer with
only the analyzer removed — which IS the deployment being modelled, a CPU-only box
with no working Presidio:

    analyzer is None          -> spans=[] failures=[]      blocking={}      PASSES THROUGH
    analyzer.analyze() raises -> spans=[] failures=[]      blocking={}      PASSES THROUGH
    detect_pii() raises       -> spans=[] failures=['pii'] blocking={'pii'} withheld

Only the third reached the sink, and it is the least likely of the three in
production. So a user who had explicitly ENABLED ``pii`` still got chunks posted
to their LLM provider unmasked, silently, while ``mask_chunks`` reported
``was_masked=True``.

These tests need no Presidio: they patch ``_get_analyzer``, which is the seam an
absent install produces anyway, so they run in CPU-only CI. The tests that prove
the REAL analyzer finds real entities live in ``test_presidio.py`` and are
``models``-marked; this module is about the two outcomes that are NOT spans.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core import constants as C  # noqa: N812
from app.services.redaction.config import blocking_detector_failures
from app.services.redaction.config import detection_config_for_all
from app.services.redaction.detectors import DetectorUnavailableError
from app.services.redaction.detectors import pii_presidio
from app.services.redaction.service import RedactionService

TEXT = "call me on 555-867-5309 about the invoice"


class _Result:
    """The shape ``AnalyzerEngine.analyze`` returns (only the fields we read)."""

    def __init__(self, entity_type: str, start: int, end: int, score: float) -> None:
        self.entity_type = entity_type
        self.start = start
        self.end = end
        self.score = score


class _WorkingAnalyzer:
    """Finds the one phone number in ``TEXT`` and nothing anywhere else."""

    def analyze(self, text: str, language: str):  # noqa: ARG002
        start = text.find("555-867-5309")
        if start < 0:
            return []
        return [_Result("PHONE_NUMBER", start, start + len("555-867-5309"), 0.9)]


class _BrokenAnalyzer:
    """Built successfully, then throws — a transient fault, not a missing install."""

    def analyze(self, text: str, language: str):  # noqa: ARG002
        raise RuntimeError("nlp engine exploded")


# --------------------------------------------------------------------- detector


def test_a_working_analyzer_still_returns_spans(monkeypatch):
    """CONTROL. The raises below must be conditional, not unconditional.

    Without this, a ``detect_pii`` that raised on every call would satisfy every
    other test in this module while detecting nothing at all, forever.
    """
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _WorkingAnalyzer())

    spans = pii_presidio.detect_pii(TEXT, None, detection_config_for_all())

    assert [s.entity_type for s in spans] == ["PHONE"]
    assert TEXT[spans[0].char_start : spans[0].char_end] == "555-867-5309"


def test_an_unbuildable_analyzer_raises_instead_of_returning_no_spans(monkeypatch):
    """Row 1 of the table. Returning ``[]`` here is a clean-looking lie."""
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)

    with pytest.raises(DetectorUnavailableError):
        pii_presidio.detect_pii(TEXT, None, detection_config_for_all())


def test_an_analyze_failure_propagates_rather_than_skipping_the_chunk(monkeypatch):
    """Row 2. A chunk that raised is a chunk nobody examined.

    It must NOT be a ``DetectorUnavailableError``: a built analyzer that threw is
    worth re-running, and that difference is what decides whether
    ``detect_and_store`` marks the file FAILED or merely skipped.
    """
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _BrokenAnalyzer())

    with pytest.raises(RuntimeError) as excinfo:
        pii_presidio.detect_pii(TEXT, None, detection_config_for_all())

    assert not isinstance(excinfo.value, DetectorUnavailableError)


def test_a_later_chunk_failing_does_not_return_the_earlier_chunks_findings(monkeypatch):
    """Long text is analyzed in 2000-char chunks; a partial pass is not a pass.

    ``continue``-ing past the failing chunk returned the spans found so far and
    reported them as the segment's complete result — the same shape as row 1, one
    frame lower, and invisible to a caller that only sees a span list.
    """
    first_ok = "my number is 555-867-5309. "
    long_text = first_ok + ("filler word " * 400)
    assert len(long_text) > 2000, "text must span more than one analyze() chunk"

    calls: list[int] = []

    class _FailsOnTheSecondChunk:
        def analyze(self, text: str, language: str):  # noqa: ARG002
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("nlp engine exploded on chunk 2")
            start = text.index("555-867-5309")
            return [_Result("PHONE_NUMBER", start, start + len("555-867-5309"), 0.9)]

    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _FailsOnTheSecondChunk())

    with pytest.raises(RuntimeError):
        pii_presidio.detect_pii(long_text, None, detection_config_for_all())

    assert len(calls) > 1, "the second chunk was never reached — the test proves nothing"


# ------------------------------------------------------------------- both sinks


def test_unavailability_reaches_the_failures_sink_every_masker_reads(monkeypatch):
    """The whole point: the sink is what makes a fail-closed masker fail closed."""
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)

    failures: list[str] = []
    spans, _tox = RedactionService.detect_segment_spans(
        TEXT, None, detection_config_for_all(), run_toxicity=False, failures=failures
    )

    assert spans == []
    assert failures == ["pii"]
    assert blocking_detector_failures(failures, {"pii"}) == {"pii"}


def test_unavailability_is_recorded_in_the_second_sink_and_a_failure_is_not(monkeypatch):
    """``unavailable`` is a strict SUBSET of ``failures``, and only detect_and_store reads it."""
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)
    failures: list[str] = []
    unavailable: list[str] = []
    RedactionService.detect_segment_spans(
        TEXT,
        None,
        detection_config_for_all(),
        run_toxicity=False,
        failures=failures,
        unavailable=unavailable,
    )
    assert failures == ["pii"]
    assert unavailable == ["pii"]

    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _BrokenAnalyzer())
    failures = []
    unavailable = []
    RedactionService.detect_segment_spans(
        TEXT,
        None,
        detection_config_for_all(),
        run_toxicity=False,
        failures=failures,
        unavailable=unavailable,
    )
    assert failures == ["pii"], "a broken detector is still a failure"
    assert unavailable == [], "a broken detector is NOT an unavailable one"


# --------------------------------------------------------------- detect_and_store


@pytest.fixture
def seeded_file(db_session, normal_user, monkeypatch):
    """A completed English file with two segments, and no toxicity model loading.

    ``score_texts`` is stubbed because the toxicity weights are a ~500 MB download
    the fast suite must not make; every OTHER detector, the whole failure/skip
    bookkeeping and the status write are the real code under test.
    """
    from app.core.enums import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.services.redaction.detectors import toxicity as tox

    monkeypatch.setattr(tox, "score_texts", lambda texts, _lang: [None] * len(texts))

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename=f"redaction-{uuid_pkg.uuid4().hex[:8]}.wav",
        storage_path=f"redaction-test/{uuid_pkg.uuid4().hex}",
        file_size=1,
        content_type="audio/wav",
        language="en",
        status=FileStatus.COMPLETED,
    )
    db_session.add(media)
    db_session.flush()
    for idx, text in enumerate((TEXT, "nothing sensitive here at all")):
        db_session.add(
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=media.id,
                start_time=float(idx),
                end_time=float(idx) + 1.0,
                text=text,
            )
        )
    db_session.flush()
    return media


def test_an_absent_presidio_does_not_flip_the_file_to_failed(db_session, seeded_file, monkeypatch):
    """THE regression that would hurt most users, and the reason this is a skip.

    ``redaction_status = failed`` is not an inert label. ``llm_guard`` turns it
    into a NON-retryable ``RedactionNotReadyError``, which ``defer_for_redaction``
    re-raises immediately — so marking every file FAILED on a deployment that
    simply has no Presidio would permanently break summarization, speaker
    identification and topic extraction for every user with ``redact_before_llm``
    on. Re-running installs nothing, so FAILED ("re-run me") is the wrong word.
    """
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)

    result = RedactionService.detect_and_store(db_session, seeded_file.id)

    assert result["status"] == "done"
    assert seeded_file.redaction_status == C.REDACTION_STATUS_DONE
    assert "pii" in result["skipped_detectors"], "an unexamined detector must be reported skipped"
    assert "pii" not in result["detectors"], "a detector that never ran must not be reported as ran"


def test_a_broken_presidio_still_flips_the_file_to_failed(db_session, seeded_file, monkeypatch):
    """The #324 contract survives: a detector that RAN and threw is re-run-worthy.

    Twin of the test above, differing only in which fault is injected — without it,
    "unavailable is a skip" could be read as "PII failures no longer matter".
    """
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _BrokenAnalyzer())

    result = RedactionService.detect_and_store(db_session, seeded_file.id)

    assert result["status"] == "failed"
    assert result["detectors"] == ["pii"]
    assert seeded_file.redaction_status == C.REDACTION_STATUS_FAILED


def test_a_healthy_scan_reports_pii_as_run_and_skips_nothing(db_session, seeded_file, monkeypatch):
    """CONTROL for both of the above: neither branch may fire on a working install."""
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _WorkingAnalyzer())

    result = RedactionService.detect_and_store(db_session, seeded_file.id)

    assert result["status"] == "done"
    assert "pii" in result["detectors"]
    assert result["skipped_detectors"] == []
    assert result["pii_entities_found"] == 1
