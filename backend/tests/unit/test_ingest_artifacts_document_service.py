"""``ingest_artifacts/document_service.py`` — the document-owned ``file_facts`` upsert."""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.document import Document
from app.models.document import DocumentChunk
from app.models.file_facts import FileFacts
from app.models.user import User
from app.services.ingest_artifacts.document_service import DOCUMENT_GENERATOR_VERSION
from app.services.ingest_artifacts.document_service import document_source_fingerprint
from app.services.ingest_artifacts.document_service import generate_document_artifacts
from app.services.ingest_artifacts.document_service import load_ordered_document_chunks

_PARA_A = (
    "This report summarizes the quarterly budget review for the engineering "
    "department. Spending remained within the approved limits for every team. "
    "The infrastructure team requested additional cloud capacity for the next "
    "quarter."
)
_PARA_B = (
    "Headcount grew by four engineers during the period under review. Two of "
    "the new hires joined the platform team and two joined the search team."
)


def _new_user(db) -> User:
    user = User(
        email=f"docsvc_{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
        auth_type="local",
    )
    db.add(user)
    db.flush()
    return user


def _new_document(db, user: User, *, language: str | None = "en") -> Document:
    document = Document(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="report.pdf",
        storage_path="x/report.pdf",
        file_size=1,
        content_type="application/pdf",
        parser="docling.slim",
        page_count=2,
        language=language,
        has_embedded_text=True,
        ocr_applied=False,
        ocr_pages=0,
        parse_warnings=[],
    )
    db.add(document)
    db.flush()
    return document


def _add_chunks(db, document: Document) -> None:
    db.add(
        DocumentChunk(
            document_id=document.id,
            chunk_index=0,
            text=_PARA_A,
            char_start=0,
            char_end=len(_PARA_A),
            page=1,
        )
    )
    db.add(
        DocumentChunk(
            document_id=document.id,
            chunk_index=1,
            text=_PARA_B,
            char_start=len(_PARA_A) + 2,
            char_end=len(_PARA_A) + 2 + len(_PARA_B),
            page=2,
        )
    )
    db.flush()


@pytest.mark.xdist_group("ingest_artifacts_document_service")
def test_generate_document_artifacts_upserts_a_document_owned_file_facts_row(db_session):
    user = _new_user(db_session)
    document = _new_document(db_session, user)
    _add_chunks(db_session, document)

    row = generate_document_artifacts(db_session, document.id)

    assert row is not None
    assert row.document_id == document.id
    assert row.media_file_id is None
    assert row.generator_version == DOCUMENT_GENERATOR_VERSION
    assert row.digest["sections"], "expected at least one digest section"
    assert row.facts["chunk_count"] == 2
    assert row.facts["word_count"] > 0

    persisted = db_session.query(FileFacts).filter(FileFacts.document_id == document.id).one()
    assert persisted.id == row.id


@pytest.mark.xdist_group("ingest_artifacts_document_service")
def test_generate_document_artifacts_short_circuits_on_an_unchanged_fingerprint(db_session):
    user = _new_user(db_session)
    document = _new_document(db_session, user)
    _add_chunks(db_session, document)

    first = generate_document_artifacts(db_session, document.id)
    second = generate_document_artifacts(db_session, document.id)

    assert first is not None and second is not None
    assert first.id == second.id
    assert first.generated_at == second.generated_at, (
        "an unchanged source_fingerprint must skip regeneration entirely, not just "
        "produce an identical result"
    )


@pytest.mark.xdist_group("ingest_artifacts_document_service")
def test_generate_document_artifacts_force_regenerates(db_session):
    user = _new_user(db_session)
    document = _new_document(db_session, user)
    _add_chunks(db_session, document)

    first = generate_document_artifacts(db_session, document.id)
    second = generate_document_artifacts(db_session, document.id, force=True)

    assert first is not None and second is not None
    assert first.id == second.id  # still one upserted row, not a duplicate


def test_generate_document_artifacts_returns_none_for_a_document_with_no_chunks(db_session):
    user = _new_user(db_session)
    document = _new_document(db_session, user)
    assert generate_document_artifacts(db_session, document.id) is None


def test_document_source_fingerprint_changes_when_chunk_text_changes():
    a = [{"id": 1, "char_start": 0, "char_end": 5, "text": "hello"}]
    b = [{"id": 1, "char_start": 0, "char_end": 5, "text": "world"}]
    assert document_source_fingerprint(a) != document_source_fingerprint(b)
    assert document_source_fingerprint(a) == document_source_fingerprint(list(a))


@pytest.mark.xdist_group("ingest_artifacts_document_service")
def test_load_ordered_document_chunks_is_in_chunk_index_order(db_session):
    user = _new_user(db_session)
    document = _new_document(db_session, user)
    _add_chunks(db_session, document)

    chunks = load_ordered_document_chunks(db_session, document.id)
    assert [c["id"] for c in chunks] == sorted(c["id"] for c in chunks)
    assert chunks[0]["char_start"] == 0
    assert chunks[1]["char_start"] == len(_PARA_A) + 2


@pytest.mark.xdist_group("ingest_artifacts_document_service")
def test_generate_document_artifacts_never_coerces_undetected_language_to_english(db_session):
    """A document whose language could not be resolved must produce facts/digest with
    ``language: None`` — never a silently-assumed "en".
    """
    user = _new_user(db_session)
    document = _new_document(db_session, user, language=None)
    _add_chunks(db_session, document)

    row = generate_document_artifacts(db_session, document.id)

    assert row is not None
    assert row.language is None
    assert row.facts["language"] is None
    assert row.digest["language"] is None
