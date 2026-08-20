"""``ingest_artifacts/document_facts.py`` — the document plane's ``file_facts.facts``."""

from __future__ import annotations

from app.services.ingest_artifacts.document_facts import DOCUMENT_FACTS_SCHEMA_VERSION
from app.services.ingest_artifacts.document_facts import build_document_facts


def test_build_document_facts_shape():
    facts = build_document_facts(
        word_count=1200,
        chunk_count=6,
        page_count=4,
        language="fr",
        parser="docling.slim",
        has_embedded_text=True,
        ocr_applied=False,
        ocr_pages=0,
        warning_count=1,
    )
    assert facts["schema_version"] == DOCUMENT_FACTS_SCHEMA_VERSION
    assert facts["word_count"] == 1200
    assert facts["chunk_count"] == 6
    assert facts["page_count"] == 4
    assert facts["language"] == "fr"
    assert facts["parser"] == "docling.slim"
    assert facts["has_embedded_text"] is True
    assert facts["ocr_applied"] is False
    assert facts["ocr_pages"] == 0
    assert facts["warning_count"] == 1


def test_build_document_facts_never_coerces_an_undetected_language():
    """Documents whose language could not be detected must report None, not "en" —
    the exact class of bug fixed in ``services/documents/chunking.py``.
    """
    facts = build_document_facts(
        word_count=10,
        chunk_count=1,
        page_count=None,
        language=None,
        parser="docling.slim",
        has_embedded_text=True,
        ocr_applied=False,
        ocr_pages=0,
        warning_count=0,
    )
    assert facts["language"] is None
    assert facts["page_count"] is None
