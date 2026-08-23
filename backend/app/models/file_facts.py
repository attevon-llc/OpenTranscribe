"""``file_facts`` — the deterministic ingest artifacts (#383 Phase 2, #403 Stage 2).

One row per ``media_file``: exact statistics, an extractive digest with per-sentence
provenance, and keyphrases. All three are produced by ``services/ingest_artifacts`` with
**no LLM** (#403 **D6**), so a deployment with ``LLM_PROVIDER`` empty still has a summary
tier to search, aggregate over, and compose an overview from.

**Why a sidecar table and not columns on ``media_file``.** The #383 plan text says
``MediaFile.file_facts`` / ``MediaFile.extractive_digest``, and this deviates:

- ``media_file`` is ~70 columns and is loaded as a whole entity by every gallery page and
  every permission subquery. The digest is the largest artifact this epic adds and is read
  by exactly two callers (Stage 3's indexer, Stage 4's aggregation path).
- Stage 3 must answer "which files need their digest regenerated" cheaply, on the reindex
  path, per addendum **G1**. Against a sidecar that is an indexed scan of a narrow table;
  against ``media_file`` it is a scan of the widest table in the schema.
- ``generator_version`` and ``source_fingerprint`` are lifecycle state that would be three
  more columns on that same hot row.

Every constraint the database enforces is declared here. The repo carries 24 DDL-only
constraints invisible to the ORM (``.rag-403/ddl-orm-divergence.md``); this table does not
add a 25th.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.media import MediaFile


class FileFacts(Base):
    """Deterministic per-file artifacts: statistics, extractive digest, keyphrases."""

    __tablename__ = "file_facts"

    #: No ``index=True``. Most models in this package carry it on the PK and it is a
    #: no-op that the DDL never honoured — ``ix_media_file_id`` does not exist in the
    #: database — so copying the habit here would create a 25th ORM↔DDL divergence.
    #: The primary key's own unique index is the index.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: ``ON DELETE CASCADE``: the artifacts are a pure function of the transcript, so they
    #: have no meaning once the file is gone, and a manual cleanup pass would be one more
    #: thing to forget in ``file_cleanup_service``.
    #: The FK is **named**, matching the migration. An unnamed ``ForeignKey`` produces a
    #: constraint whose Python-side name is ``None`` while Postgres assigns
    #: ``file_facts_media_file_id_fkey`` — i.e. the ORM would not be declaring the object
    #: the database actually enforces, which is the 24-divergence pattern this table is
    #: explicitly not repeating. Caught by
    #: ``test_v389_migration_consistency.test_the_orm_declares_every_constraint_the_database_enforces``.
    media_file_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("media_file.id", ondelete="CASCADE", name="file_facts_media_file_id_fkey"),
        nullable=False,
    )

    #: ``"{facts}.{digest}.{keyphrases}"`` schema versions, e.g. ``"1.1.1"``. Stage 3
    #: regenerates any row whose version differs from the code's, which is what makes an
    #: algorithm change roll out on the next reindex instead of needing a backfill task.
    generator_version: Mapped[str] = mapped_column(String(32), nullable=False)

    #: SHA-256 over the ordered ``(id, start_time, end_time, text)`` of every segment.
    #: Regeneration is skipped when it matches — so a reindex of an unchanged transcript
    #: costs a hash, not a TextRank. Speaker renames deliberately DO change it (the
    #: resolved display name is part of the digest's speaker attribution), which is how
    #: issue #405's rename becomes a digest-regeneration trigger for free.
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    language: Mapped[str | None] = mapped_column(String(16), nullable=True)

    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    digest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    keyphrases: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: Denormalised out of ``digest`` so "how big is the summary tier" and "which files
    #: produced an empty digest" are index scans, not JSONB traversals.
    digest_word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    section_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Wall-clock cost of generating this row. The Stage 2 gate is a p95, and a gate whose
    #: measurement lives only in a benchmark script stops being measurable in production.
    generation_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: ``onupdate`` as well as ``server_default``: the row is upserted in place on
    #: regeneration, so without it the timestamp would report when the artifacts were
    #: FIRST built and "when was this digest last rebuilt" would be unanswerable.
    generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    media_file: Mapped[MediaFile] = relationship("MediaFile", back_populates="facts_row")

    __table_args__ = (
        # One row per file. Also what makes the upsert in
        # `services/ingest_artifacts/service.py` an ON CONFLICT rather than a read-modify-
        # write race between the pipeline and a manual regeneration.
        UniqueConstraint("media_file_id", name="uq_file_facts_media_file"),
        CheckConstraint("digest_word_count >= 0", name="ck_file_facts_digest_word_count"),
        CheckConstraint("section_count >= 0", name="ck_file_facts_section_count"),
        CheckConstraint("generation_ms IS NULL OR generation_ms >= 0", name="ck_file_facts_ms"),
        # Stage 3's "which rows are stale" query.
        Index("ix_file_facts_generator_version", "generator_version"),
    )

    def __repr__(self) -> str:
        return (
            f"<FileFacts media_file_id={self.media_file_id} "
            f"v={self.generator_version} sections={self.section_count}>"
        )
