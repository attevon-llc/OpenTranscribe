"""Remove what the injector wrote — through the canonical destroyer, not raw SQL.

An injected meeting is a real ``MediaFile`` with transcript segments, speakers and
``transcript_chunks`` documents in OpenSearch. Deleting only the rows (what the
integration tests used to do) orphans the chunks, the speaker embeddings, any cluster
the speakers formed, and the owner's caches. :func:`app.services.file_cleanup_service.purge_media_file`
is the single destroy every other delete path shares, so this module only decides WHICH
files are injected ones and hands each to it.

Selection is deliberately narrower than "has a ``rag_eval`` block". A file is purged only
when ALL hold:

* the owner is the named user;
* ``metadata_important['rag_eval']`` says ``injected`` for the named corpus and seed;
* the file's uuid equals ``ids.file_uuid(corpus, meeting_id, seed)`` recomputed from that
  block. The uuid is a pure function of those three values, so a row that merely carries
  a copied-looking metadata block (or a hand-edited one) cannot match by accident.

``seed`` is part of the key on purpose: ``""`` is the canonical namespace (the eval corpus
a measurement is recorded against), and purging it is a decision, so the CLI requires
``--yes`` for it like for any other seed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.scripts.corpus_injection import ids
from app.scripts.corpus_injection.rows import RAG_EVAL_KEY

logger = logging.getLogger(__name__)


@dataclass
class PurgeReport:
    """What a purge pass found and did. ``failed`` is non-empty on any partial result."""

    matched: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.failed


def find_injected_files(db: Session, user_id: int, corpus: str, seed: str = "") -> list[MediaFile]:
    """Files the injector wrote for ``(corpus, seed)`` under ``user_id``, uuid-verified."""
    rows = (
        db.execute(
            select(MediaFile)
            .where(MediaFile.user_id == user_id)
            .where(MediaFile.metadata_important[RAG_EVAL_KEY]["injected"].as_boolean().is_(True))
            .where(MediaFile.metadata_important[RAG_EVAL_KEY]["corpus"].as_string() == corpus)
            .where(MediaFile.metadata_important[RAG_EVAL_KEY]["seed"].as_string() == seed)
            .order_by(MediaFile.id)
        )
        .scalars()
        .all()
    )
    verified: list[MediaFile] = []
    for media_file in rows:
        block = (media_file.metadata_important or {}).get(RAG_EVAL_KEY, {})
        expected = ids.file_uuid(corpus, str(block.get("meeting_id", "")), seed)
        if str(media_file.uuid) != str(expected):
            logger.warning(
                "Not purging %s: carries a rag_eval block for %s/%s but its uuid is not the "
                "injector's derived one.",
                media_file.uuid,
                corpus,
                seed or "<canonical>",
            )
            continue
        verified.append(media_file)
    return verified


def purge_injected(
    db: Session, user_id: int, corpus: str, seed: str = "", *, dry_run: bool = False
) -> PurgeReport:
    """Destroy every injected file for ``(corpus, seed)``; report instead of raising.

    Each file goes through ``purge_media_file`` (storage, OpenSearch chunks and speaker
    embeddings, the row and its children, caches, speaker clusters). A file the destroyer
    refuses (legal hold) or reports residue for lands in ``failed`` — residue means a copy
    of its data may remain in a store, so it is not counted as cleaned.
    """
    from app.services.file_cleanup_service import purge_media_file

    report = PurgeReport()
    for media_file in find_injected_files(db, user_id, corpus, seed):
        file_uuid = str(media_file.uuid)
        report.matched.append(file_uuid)
        if dry_run:
            continue
        result = purge_media_file(db, media_file)
        if result.get("deleted") and not result.get("residual_errors"):
            report.deleted.append(file_uuid)
        else:
            report.failed.append(
                {
                    "file_uuid": file_uuid,
                    "error": result.get("error"),
                    "residual_errors": result.get("residual_errors") or [],
                }
            )
    return report
