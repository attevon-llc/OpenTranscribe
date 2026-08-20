"""``scope_digest_hits`` covers documents too (#403 Stage-6 gate, W2.3).

A collection can hold recordings and documents together, and a summary over
that collection must cover both — never silently drop the document half, and
never double-count a recording as if it were also a document (the two tables
share no uuid namespace, so that would require a genuine bug).

The document arm delegates its join to
``ingest_artifacts.scope.scope_facts_for_uuids`` (already tested on its own
terms in ``test_ingest_artifacts_scope.py``) rather than restating the
outer-join logic here; these tests are about the SEAM — that
``scope_digest_hits`` calls it correctly, for the right uuids, and folds the
result into the same ``DigestScopeHits`` shape the recording-only path always
returned.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.chat.mapreduce import scope_digest_hits
from app.services.ingest_artifacts.scope import ScopeFactsCoverage
from app.services.ingest_artifacts.scope import ScopeFactsHit

pytestmark = pytest.mark.unit


def _media_row(file_id: int, uuid: str, title: str, sections: int = 1):
    digest = {
        "sections": [
            {"index": i, "text": f"Section {i} of {title}.", "start_time": i * 10.0}
            for i in range(sections)
        ]
    }
    return (file_id, uuid, title, digest)


def _media_db(rows):
    """A session whose media outer-join returns `rows` and touches nothing else."""
    db = MagicMock()
    chain = db.query.return_value.outerjoin.return_value.filter.return_value
    chain.filter.return_value.all.return_value = rows
    return db


def _document_hit(
    uuid: str, filename: str, sections: int = 1, source_id: int = 900
) -> ScopeFactsHit:
    digest = {
        "sections": [
            {"index": i, "text": f"Doc section {i} of {filename}.", "start_time": 0.0}
            for i in range(sections)
        ]
    }
    return ScopeFactsHit(
        kind="document",
        source_id=source_id,
        uuid=uuid,
        title=filename,
        digest=digest,
        facts={},
        keyphrases={},
    )


def test_a_pure_media_scope_never_calls_the_document_arm():
    """Every uuid matched by the media query — the document arm must not run
    at all, which is also what keeps every EXISTING media-only test (and its
    mock, which cannot tell two different queries apart) byte-identical."""
    db = _media_db([_media_row(1, "uuid-1", "Weekly sync")])

    with patch("app.services.chat.mapreduce._document_scope_hits") as doc_arm:
        hits = scope_digest_hits(db, ["uuid-1"])

    doc_arm.assert_not_called()
    assert len(hits) == 1


def test_a_document_only_uuid_is_covered_by_the_document_arm():
    db = _media_db([])  # the media query matches nothing

    with patch(
        "app.services.ingest_artifacts.scope.scope_facts_for_uuids",
        return_value=ScopeFactsCoverage(
            hits=[_document_hit("doc-uuid-1", "report.pdf")],
            files_without_artifacts=0,
            files_total=1,
        ),
    ):
        hits = scope_digest_hits(db, ["doc-uuid-1"])

    assert len(hits) == 1
    assert hits[0].file_uuid == "doc-uuid-1"
    assert hits[0].is_document is True
    assert hits[0].title == "report.pdf"
    assert "Doc section 0 of report.pdf." in hits[0].content


def test_a_mixed_scope_covers_both_the_recording_and_the_document():
    db = _media_db([_media_row(1, "rec-uuid-1", "Weekly sync")])

    with patch(
        "app.services.ingest_artifacts.scope.scope_facts_for_uuids",
        return_value=ScopeFactsCoverage(
            hits=[_document_hit("doc-uuid-1", "report.pdf")],
            files_without_artifacts=0,
            files_total=1,
        ),
    ):
        hits = scope_digest_hits(db, ["rec-uuid-1", "doc-uuid-1"])

    kinds = {(h.file_uuid, h.is_document) for h in hits}
    assert ("rec-uuid-1", False) in kinds
    assert ("doc-uuid-1", True) in kinds
    assert len(hits) == 2


def test_a_recording_is_never_double_counted_as_a_document():
    """The uuid namespaces do not overlap, so the document arm must only ever
    be asked about uuids the media query did NOT already resolve."""
    db = _media_db([_media_row(1, "rec-uuid-1", "Weekly sync")])

    with patch("app.services.ingest_artifacts.scope.scope_facts_for_uuids") as delegate:
        hits = scope_digest_hits(db, ["rec-uuid-1"])

    delegate.assert_not_called()
    # Real state, not just mock bookkeeping: the recording is still served —
    # skipping the document arm must not also skip the file it was never
    # needed for.
    assert len(hits) == 1
    assert hits[0].file_uuid == "rec-uuid-1"
    assert hits[0].is_document is False


def test_a_document_with_no_artifacts_yet_is_counted_not_dropped():
    db = _media_db([])

    with patch(
        "app.services.ingest_artifacts.scope.scope_facts_for_uuids",
        return_value=ScopeFactsCoverage(hits=[], files_without_artifacts=1, files_total=1),
    ):
        hits = scope_digest_hits(db, ["doc-uuid-1"])

    assert hits == []
    assert hits.coverage["files_without_artifacts"] == 1


def test_files_without_artifacts_sums_the_media_and_document_halves():
    db = _media_db([(1, "rec-uuid-1", "No digest yet", None)])

    with patch(
        "app.services.ingest_artifacts.scope.scope_facts_for_uuids",
        return_value=ScopeFactsCoverage(hits=[], files_without_artifacts=1, files_total=1),
    ):
        hits = scope_digest_hits(db, ["rec-uuid-1", "doc-uuid-1"])

    assert hits.coverage["files_without_artifacts"] == 2


def test_a_broken_document_arm_degrades_without_losing_the_media_half():
    """One arm failing must not take down a map the other arm already answered."""
    db = _media_db([_media_row(1, "rec-uuid-1", "Weekly sync")])

    with patch(
        "app.services.ingest_artifacts.scope.scope_facts_for_uuids",
        side_effect=RuntimeError("boom"),
    ):
        hits = scope_digest_hits(db, ["rec-uuid-1", "doc-uuid-1"])

    assert len(hits) == 1
    assert hits[0].file_uuid == "rec-uuid-1"
    assert hits.coverage["files_without_artifacts"] == 1


def test_the_digest_scope_hits_type_is_preserved_not_flattened():
    from app.services.chat.mapreduce import DigestScopeHits

    db = _media_db([_media_row(1, "rec-uuid-1", "Weekly sync")])

    with patch(
        "app.services.ingest_artifacts.scope.scope_facts_for_uuids",
        return_value=ScopeFactsCoverage(
            hits=[_document_hit("doc-uuid-1", "report.pdf")],
            files_without_artifacts=0,
            files_total=1,
        ),
    ):
        hits = scope_digest_hits(db, ["rec-uuid-1", "doc-uuid-1"])

    assert isinstance(hits, DigestScopeHits)
    assert isinstance(hits.coverage, dict)
