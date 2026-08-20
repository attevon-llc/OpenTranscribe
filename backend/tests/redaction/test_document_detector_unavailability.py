"""``detect_and_store_document`` — the document twin of ``test_detector_unavailability.py``.

Same three outcomes, same reason they must not collapse (issue #324), now proven against
``RedactionService.detect_and_store_document`` / ``document_chunk.redactions`` instead of
``detect_and_store`` / ``transcript_segment.redactions``. The detector-layer behaviour
(``pii_presidio``, the two sinks) is already pinned by ``test_detector_unavailability.py``;
this file exists because ``detect_and_store_document`` is a structurally separate function
(own query, own status writes, own return shape) with no test coverage of its own before #362's
redaction work — mirroring the transcript-side fixture and assertions is the point, not
novelty.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core import constants as C  # noqa: N812
from app.services.redaction.detectors import pii_presidio
from app.services.redaction.service import RedactionService

TEXT = "call me on 555-867-5309 about the invoice"


class _Result:
    def __init__(self, entity_type: str, start: int, end: int, score: float) -> None:
        self.entity_type = entity_type
        self.start = start
        self.end = end
        self.score = score


class _WorkingAnalyzer:
    def analyze(self, text: str, language: str):  # noqa: ARG002
        start = text.find("555-867-5309")
        if start < 0:
            return []
        return [_Result("PHONE_NUMBER", start, start + len("555-867-5309"), 0.9)]


class _BrokenAnalyzer:
    def analyze(self, text: str, language: str):  # noqa: ARG002
        raise RuntimeError("nlp engine exploded")


@pytest.fixture
def seeded_document(db_session, normal_user, monkeypatch):
    """A completed English document with two chunks, and no toxicity model loading."""
    from app.core.enums import FileStatus
    from app.models.document import Document
    from app.models.document import DocumentChunk
    from app.services.redaction.detectors import toxicity as tox

    monkeypatch.setattr(tox, "score_texts", lambda texts, _lang: [None] * len(texts))

    doc = Document(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename=f"redaction-{uuid_pkg.uuid4().hex[:8]}.pdf",
        storage_path=f"redaction-test/{uuid_pkg.uuid4().hex}.pdf",
        file_size=1,
        content_type="application/pdf",
        language="en",
        status=FileStatus.COMPLETED,
    )
    db_session.add(doc)
    db_session.flush()
    for idx, text in enumerate((TEXT, "nothing sensitive here at all")):
        db_session.add(
            DocumentChunk(
                document_id=doc.id,
                chunk_index=idx,
                text=text,
                char_start=0,
                char_end=len(text),
            )
        )
    db_session.flush()
    return doc


def test_an_absent_presidio_does_not_flip_the_document_to_failed(
    db_session, seeded_document, monkeypatch
):
    """Same non-retryable-FAILED hazard as the transcript side, now for documents.

    A document has no inline-masking fallback the way a transcript chunk does inside
    chat (``chat/redactor._mask_from_document_chunk`` still refuses on non-``done``
    status), so marking every document FAILED on a Presidio-less box would refuse
    every document chunk to chat permanently rather than merely skip one detector.
    """
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: None)

    result = RedactionService.detect_and_store_document(db_session, seeded_document.id)

    assert result["status"] == "done"
    assert seeded_document.redaction_status == C.REDACTION_STATUS_DONE
    assert "pii" in result["skipped_detectors"], "an unexamined detector must be reported skipped"
    assert "pii" not in result["detectors"], "a detector that never ran must not be reported as ran"


def test_a_broken_presidio_still_flips_the_document_to_failed(
    db_session, seeded_document, monkeypatch
):
    """Twin of the transcript-side test: a detector that RAN and threw is re-run-worthy."""
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _BrokenAnalyzer())

    result = RedactionService.detect_and_store_document(db_session, seeded_document.id)

    assert result["status"] == "failed"
    assert result["detectors"] == ["pii"]
    assert seeded_document.redaction_status == C.REDACTION_STATUS_FAILED


def test_a_healthy_scan_reports_pii_as_run_and_caches_spans_on_the_chunk(
    db_session, seeded_document, monkeypatch
):
    """CONTROL for both of the above, and proof spans land on the CHUNK, not a rebuild.

    Unlike a transcript segment, a document chunk IS the retrieval unit — there is no
    ``chat/redactor``-side rebuild-from-segments step, so the cached spans must be
    directly readable off the same ``DocumentChunk`` row the detector scanned.
    """
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _WorkingAnalyzer())

    result = RedactionService.detect_and_store_document(db_session, seeded_document.id)

    assert result["status"] == "done"
    assert "pii" in result["detectors"]
    assert result["skipped_detectors"] == []
    assert result["pii_entities_found"] == 1

    first_chunk = seeded_document.chunks[0]
    assert first_chunk.redactions, "the healthy scan's spans must be cached on the chunk"
    assert first_chunk.redactions[0]["category"] == "pii"


def test_document_not_found_is_skipped_not_an_exception(db_session):
    """Mirrors ``detect_and_store``'s ``file_not_found`` disposition for a bad id."""
    result = RedactionService.detect_and_store_document(db_session, 999_999_999)

    assert result == {"status": "skipped", "reason": "file_not_found"}
