"""``document`` / ``document_chunk`` — the document ingestion plane (#362, #403 Stage 6c).

**Own table, not a ``media_file`` discriminator.** ``media_file`` is ~70 columns loaded
whole by every gallery page and every permission subquery, and most of it is A/V-specific
state (duration, waveform, speakers, diarization) that is meaningless for a PDF. ``document``
carries only what a parsed text document actually needs. ``file_facts`` (v390) is the
precedent for a narrow sidecar table living beside the wide one; this goes further and gives
documents their own first-class row because, unlike ``file_facts``, a document is not a
derived artifact of something else — it is the thing itself, with its own upload/list/detail/
delete lifecycle.

``document_chunk`` is the durable-storage half of ``services/documents/chunking.py``
(``chunk_document`` returns ``DocumentChunk.to_row()`` dicts in exactly this shape). It is
**not** the OpenSearch document shape — keeping them apart is what lets an index rebuild read
these rows instead of re-parsing the original file, mirroring why ``transcript_segment`` and
the ``transcript_chunks`` index are two different things for transcripts.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.enums import FileStatus
from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.file_facts import FileFacts
    from app.models.user import User


class Document(Base):
    """A non-media file (PDF/DOCX/HTML/PPTX/…) uploaded, parsed and made searchable.

    ``status`` reuses :class:`~app.core.enums.FileStatus` rather than a parallel enum: the
    lifecycle is the same shape as a transcript's (queued → processing → completed/error),
    and reusing it keeps one status vocabulary for the API's status-detail responses and the
    gallery's status badges instead of two that drift.
    """

    __tablename__ = "document"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: ``unique=True`` deliberately NOT set here — it would create an unnamed unique
    #: constraint the ORM cannot mirror. ``uq_document_uuid`` is declared explicitly in
    #: ``__table_args__`` instead, matching the migration's named ``CONSTRAINT``.
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid7, index=True
    )
    #: FK is **named**, matching the migration (``document_user_id_fkey``). An unnamed
    #: ``ForeignKey`` produces a constraint whose Python-side name is ``None`` while
    #: Postgres assigns its own — the ORM would then not be declaring the object the
    #: database actually enforces, the 24-divergence pattern this table's docstring
    #: says it does not repeat. Caught by
    #: ``test_v393_migration_consistency.test_the_orm_declares_every_constraint_the_database_enforces``.
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", name="document_user_id_fkey"), nullable=False
    )
    #: Cloud-edition seam: tenant scope (NULL = personal), same convention as
    #: ``MediaFile.organization_id``.
    organization_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("organization.id", name="document_organization_id_fkey"),
        nullable=True,
        index=True,
    )

    filename: Mapped[str] = mapped_column(String, nullable=False, index=True)
    storage_path: Mapped[str] = mapped_column(String, nullable=False)  # Path in MinIO/S3
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)  # detected mime
    #: imohash (32 hex) — same fingerprint convention as ``MediaFile.file_hash`` /
    #: watch-source dedup, and what task #7's document auto-import must not bypass.
    file_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    status: Mapped[FileStatus] = mapped_column(
        SAEnum(
            FileStatus,
            native_enum=False,
            create_constraint=False,
            values_callable=lambda e: [s.value for s in e],
        ),
        nullable=False,
        default=FileStatus.PENDING,
        index=True,
    )
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    # --- Parse result (services/documents/ir.py:ParsedDocument) ---
    #: Which tier parsed it: ``docling.slim`` / ``docling.serve`` / ``tika`` — the
    #: registry's ``DocumentParser.name``, never inferred at a call site.
    parser: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: ``ir.IR_VERSION`` at parse time. A reparse sweep is a version comparison against
    #: this column, the same pattern ``file_facts.generator_version`` uses for its rows.
    parse_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    has_embedded_text: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ocr_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ocr_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: ``ParsedDocument.warnings`` — "anything that made this parse worse than it should
    #: have been". An empty list is the only "nothing was lost" claim; NULL means "never
    #: parsed", which read-paths must not conflate with "parsed cleanly" (issue #69's
    #: silent-degradation class this column exists to surface instead of hide).
    parse_warnings: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    #: Denormalised out of the chunk rows so "how big is this document" is a column read,
    #: not a JOIN — same reasoning as ``file_facts.digest_word_count``.
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Redaction lifecycle — same trio as MediaFile, same reason ---
    #: pending | processing | done | failed (None = never scanned). Document text lands in
    #: the same ``transcript_chunks`` index as transcripts and inherits the same masking
    #: contract (root CLAUDE.md's chat retrieval trap), so it needs the same per-file
    #: coverage tracking both LLM egress paths enforce for media files.
    redaction_status: Mapped[str | None] = mapped_column(String, nullable=True)
    redaction_model_version: Mapped[str | None] = mapped_column(String, nullable=True)
    redaction_coverage: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship("User", foreign_keys=[user_id])
    chunks: Mapped[list[DocumentChunk]] = relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentChunk.chunk_index",
    )
    #: The document-owned counterpart of ``MediaFile.facts_row`` (v398, #403 Stage 6) —
    #: deterministic facts/digest/keyphrases from ``services/ingest_artifacts``, keyed by
    #: ``file_facts.document_id`` rather than ``media_file_id``. ``passive_deletes``
    #: because the FK is ON DELETE CASCADE, same reasoning ``MediaFile.facts_row`` gives.
    facts_row: Mapped[FileFacts | None] = relationship(
        "FileFacts",
        back_populates="document",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("uuid", name="uq_document_uuid"),
        CheckConstraint("file_size >= 0", name="ck_document_file_size"),
        CheckConstraint("page_count IS NULL OR page_count >= 0", name="ck_document_page_count"),
        CheckConstraint("ocr_pages >= 0", name="ck_document_ocr_pages"),
        CheckConstraint("word_count >= 0", name="ck_document_word_count"),
        CheckConstraint("chunk_count >= 0", name="ck_document_chunk_count"),
    )

    def __repr__(self) -> str:
        return f"<Document id={self.id} filename={self.filename!r} status={self.status}>"


class DocumentChunk(Base):
    """One retrieval chunk of a parsed document — durable storage only.

    Mirrors ``services/documents/chunking.py``'s ``DocumentChunk.to_row()`` exactly. Never
    holds the OpenSearch document shape or an embedding; Stage 6b's indexer maps these rows
    to index documents at index time, and a reindex reads these rows again rather than
    re-parsing the original file.
    """

    __tablename__ = "document_chunk"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("document.id", ondelete="CASCADE", name="document_chunk_document_id_fkey"),
        nullable=False,
    )
    #: 0-based, contiguous within a document — the ``{uuid}_{chunk_index}`` id the v6 index
    #: already uses for transcript chunks (``backend/app/services/search/CLAUDE.md``).
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    block_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    #: Cached detection spans (v396), mirroring ``TranscriptSegment.redactions`` — a list
    #: of span dicts addressing ``text`` by offset. NULL means "never scanned"; an empty
    #: list means "scanned, nothing found". Never recomputed except by a rescan; masking
    #: is a read-time transform via ``RedactionService.mask_segment``, same as transcripts.
    redactions: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    #: Cached toxicity score dict (v396), mirroring ``TranscriptSegment.toxicity``. A
    #: score, not a span list — toxicity has no maskable offsets (see
    #: ``redaction/CLAUDE.md``'s ``_DETECTOR_CATEGORIES`` gotcha).
    toxicity: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    document: Mapped[Document] = relationship("Document", back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunk_index"),
        CheckConstraint("char_end >= char_start", name="ck_document_chunk_char_range"),
    )

    def __repr__(self) -> str:
        return f"<DocumentChunk document_id={self.document_id} index={self.chunk_index}>"
