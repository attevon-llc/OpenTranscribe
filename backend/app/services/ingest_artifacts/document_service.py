"""Generate and persist the deterministic ingest artifacts for one document.

The document analog of ``service.py``: read ``document_chunk`` rows → facts + digest +
keyphrases → upsert ``file_facts`` (document-owned row, v398). Postgres in, Postgres
out — no OpenSearch, no LLM, no model load (#403 **D6**), same as the transcript path.

Callable from wherever a document's chunks change:

- the parse-completion path (``tasks/document_tasks.py``, dispatched fire-and-forget
  after chunking, mirroring how ``transcription/postprocess.enrich_and_dispatch``
  dispatches the transcript equivalent),
- a future reparse/reindex sweep, the same ``source_fingerprint`` short-circuit
  ``generate_file_artifacts`` gives Stage 3's reindex path.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document import DocumentChunk
from app.models.file_facts import FileFacts

from .document_digest import DIGEST_SCHEMA_VERSION
from .document_digest import build_document_digest
from .document_facts import DOCUMENT_FACTS_SCHEMA_VERSION
from .document_facts import build_document_facts
from .keyphrases import KEYPHRASE_SCHEMA_VERSION
from .keyphrases import extract_keyphrases
from .provenance import validate_provenance

logger = logging.getLogger(__name__)

#: The document plane's three payload schema versions, joined — same rollout mechanism
#: ``ingest_artifacts.service.GENERATOR_VERSION`` uses for transcripts. Deliberately its
#: own constant (not shared with the transcript one) even though a media-owned row and a
#: document-owned row can never collide on the same `file_facts` id: the two payload
#: shapes evolve independently (``DOCUMENT_FACTS_SCHEMA_VERSION`` vs
#: ``facts.FACTS_SCHEMA_VERSION``), so a shared version string would force them to bump
#: in lockstep for no reason.
DOCUMENT_GENERATOR_VERSION = (
    f"{DOCUMENT_FACTS_SCHEMA_VERSION}.{DIGEST_SCHEMA_VERSION}.{KEYPHRASE_SCHEMA_VERSION}"
)


def load_ordered_document_chunks(db: Session, document_id: int) -> list[dict[str, Any]]:
    """Read one document's chunks in chunk_index order, as plain dicts.

    Plain dicts for the same reason ``service.load_ordered_segments`` returns them: the
    pure builders in this package never touch the ORM, so they are testable without a
    database.
    """
    chunks = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index)
        .all()
    )
    return [
        {
            "id": int(chunk.id),
            "text": str(chunk.text or ""),
            "char_start": int(chunk.char_start),
            "char_end": int(chunk.char_end),
            "page": chunk.page,
        }
        for chunk in chunks
    ]


def document_source_fingerprint(chunks: list[dict[str, Any]]) -> str:
    """SHA-256 over the ordered chunks — the document plane's "has anything changed?" key.

    Covers id, char range and text. A reparse that produced identical chunks
    (byte-identical source, unchanged parser version) leaves this unchanged, so
    regeneration short-circuits exactly as ``service.source_fingerprint`` does for
    transcripts.
    """
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(
            "\x1f".join(
                (
                    str(chunk["id"]),
                    str(chunk["char_start"]),
                    str(chunk["char_end"]),
                    str(chunk["text"]),
                )
            ).encode("utf-8")
        )
        digest.update(b"\x1e")
    return digest.hexdigest()


def build_document_artifacts(
    chunks: list[dict[str, Any]],
    *,
    language: str | None,
    page_count: int | None,
    parser: str | None,
    has_embedded_text: bool | None,
    ocr_applied: bool,
    ocr_pages: int,
    warning_count: int,
) -> dict[str, Any]:
    """Build all three payloads from ordered document chunks. Pure — no DB, no I/O."""
    digest = build_document_digest(chunks, language=language)

    # Fail at the producer — the same rule `service.build_artifacts` applies: a
    # malformed provenance surfaces downstream as a citation that deep-links nowhere.
    for section in digest["sections"]:
        for sentence in section["sentences"]:
            validate_provenance(sentence["provenance"])

    full_text = " ".join(str(chunk.get("text") or "") for chunk in chunks)
    keyphrases = extract_keyphrases(full_text, language=(language or "en").split("-")[0].lower())

    facts = build_document_facts(
        word_count=len(full_text.split()),
        chunk_count=len(chunks),
        page_count=page_count,
        language=language,
        parser=parser,
        has_embedded_text=has_embedded_text,
        ocr_applied=ocr_applied,
        ocr_pages=ocr_pages,
        warning_count=warning_count,
    )

    return {"facts": facts, "digest": digest, "keyphrases": keyphrases}


def generate_document_artifacts(
    db: Session,
    document_id: int,
    *,
    force: bool = False,
) -> FileFacts | None:
    """Generate and upsert the document-owned ``file_facts`` row for *document_id*.

    Args:
        db: Active session. The caller owns the transaction — this function flushes
            but does not commit, matching ``generate_file_artifacts``'s contract.
        document_id: ``Document.id``.
        force: Regenerate even when the fingerprint and generator version both match.

    Returns:
        The persisted row, or ``None`` when the document has no chunks (still
        processing, or chunking produced nothing extractable).
    """
    document = db.query(Document).filter(Document.id == document_id).first()
    if document is None:
        logger.warning("file_facts: document %s not found", document_id)
        return None

    chunks = load_ordered_document_chunks(db, document_id)
    if not chunks:
        logger.info("file_facts: document %s has no chunks; nothing to summarise", document_id)
        return None

    fingerprint = document_source_fingerprint(chunks)
    existing = db.query(FileFacts).filter(FileFacts.document_id == document_id).first()
    if (
        existing is not None
        and not force
        and existing.source_fingerprint == fingerprint
        and existing.generator_version == DOCUMENT_GENERATOR_VERSION
    ):
        logger.debug("file_facts: document %s already current (fingerprint match)", document_id)
        return existing

    started = time.perf_counter()
    artifacts = build_document_artifacts(
        chunks,
        language=document.language,
        page_count=document.page_count,
        parser=document.parser,
        has_embedded_text=document.has_embedded_text,
        ocr_applied=document.ocr_applied,
        ocr_pages=document.ocr_pages,
        warning_count=len(document.parse_warnings or []),
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    digest = artifacts["digest"]
    row = existing or FileFacts(document_id=document_id)
    row.generator_version = DOCUMENT_GENERATOR_VERSION
    row.source_fingerprint = fingerprint
    row.language = digest["language"]
    row.facts = artifacts["facts"]
    row.digest = digest
    row.keyphrases = artifacts["keyphrases"]
    row.digest_word_count = int(digest["word_count"])
    row.section_count = len(digest["sections"])
    row.generation_ms = elapsed_ms
    if existing is None:
        db.add(row)
    db.flush()

    logger.info(
        "file_facts: document %s → %d sections / %d digest words / %d keyphrases in %d ms",
        document_id,
        row.section_count,
        row.digest_word_count,
        len(artifacts["keyphrases"]["phrases"]),
        elapsed_ms,
    )
    return row
