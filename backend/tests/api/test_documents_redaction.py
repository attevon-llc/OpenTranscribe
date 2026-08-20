"""Document chunk reads are masked at read time (v400, #362 lane C5).

``GET /documents/{uuid}/chunks`` used to serve every chunk's ``text`` raw, with no
regard for the caller's redaction policy — the exact unmasked-read-surface shape root
``CLAUDE.md``'s chat retrieval trap warns about, on a different plane. This pins the
fix using ``DocumentChunk.redactions`` (v396's cached span cache) + the default
``profanity`` category (``core/constants.DEFAULT_REDACTION_CATEGORIES``).
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.document import Document
from app.models.document import DocumentChunk
from app.models.prompt import UserSetting


def _enable_redaction(db_session, user, *, categories: str = '["profanity"]') -> None:
    """``core/constants.DEFAULT_REDACTION_ENABLED`` is False — a fresh test user has
    redaction OFF until a ``UserSetting`` row turns it on, same as
    ``tests/redaction/test_search_snippet_redaction.py``'s ``_policy`` helper.
    """
    db_session.add(
        UserSetting(user_id=user.id, setting_key="redaction_enabled", setting_value="true")
    )
    db_session.add(
        UserSetting(user_id=user.id, setting_key="redaction_categories", setting_value=categories)
    )
    db_session.commit()


def _make_document(db_session, owner, **overrides) -> Document:
    doc_uuid = uuid.uuid4()
    defaults = {
        "uuid": doc_uuid,
        "user_id": owner.id,
        "filename": f"{doc_uuid}.pdf",
        "storage_path": f"documents/test/{doc_uuid}.pdf",
        "file_size": 100,
        "content_type": "application/pdf",
        "status": "completed",
        "redaction_status": "done",
    }
    defaults.update(overrides)
    doc = Document(**defaults)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


def _make_chunk(db_session, document, **overrides) -> DocumentChunk:
    defaults = {
        "document_id": document.id,
        "chunk_index": 0,
        "text": "This is a damn good report.",
        "char_start": 0,
        "char_end": 28,
        "section_path": [],
        "block_types": [],
    }
    defaults.update(overrides)
    chunk = DocumentChunk(**defaults)
    db_session.add(chunk)
    db_session.commit()
    db_session.refresh(chunk)
    return chunk


_PROFANITY_SPAN = {
    "char_start": 10,
    "char_end": 14,
    "category": "profanity",
    "entity_type": "PROFANITY",
    "detector": "wordlist",
}


def test_a_default_category_span_is_masked_on_read(
    client, user_token_headers, normal_user, db_session
):
    _enable_redaction(db_session, normal_user)
    doc = _make_document(db_session, normal_user)
    _make_chunk(db_session, doc, redactions=[_PROFANITY_SPAN])

    response = client.get(f"/api/documents/{doc.uuid}/chunks", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    text = response.json()["chunks"][0]["text"]
    assert "damn" not in text
    assert "[PROFANITY]" in text
    assert text == "This is a [PROFANITY] good report."


def test_a_chunk_with_no_cached_spans_is_returned_unchanged(
    client, user_token_headers, normal_user, db_session
):
    _enable_redaction(db_session, normal_user)
    doc = _make_document(db_session, normal_user)
    _make_chunk(db_session, doc, redactions=None)

    response = client.get(f"/api/documents/{doc.uuid}/chunks", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["chunks"][0]["text"] == "This is a damn good report."


def test_a_pending_scan_withholds_the_chunks(client, user_token_headers, normal_user, db_session):
    _enable_redaction(db_session, normal_user)
    doc = _make_document(db_session, normal_user, redaction_status="pending")
    _make_chunk(db_session, doc)

    response = client.get(f"/api/documents/{doc.uuid}/chunks", headers=user_token_headers)

    assert response.status_code == status.HTTP_409_CONFLICT


def test_a_failed_scan_does_not_withhold_the_chunks(
    client, user_token_headers, normal_user, db_session
):
    """``failed`` means the scan could not run — trapping the reader forever would be
    worse than the (unmasked-for-uncached-spans) read this endpoint already allows.
    """
    _enable_redaction(db_session, normal_user)
    doc = _make_document(db_session, normal_user, redaction_status="failed")
    _make_chunk(db_session, doc)

    response = client.get(f"/api/documents/{doc.uuid}/chunks", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
