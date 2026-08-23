"""The document-digest half of the digest-tier index shape (#362 / #403 Stage 6).

``index_mapping.py`` already reserves ``DOC_TYPE_DOCUMENT_DIGEST = "document_digest"``
(#403 D1's value set was declared complete up front so a Stage-3-era reader would not
need revisiting to recognise it) but only ever built the transcript-shaped document via
``build_digest_documents``. This module is the document-shaped twin — Stage 3's indexer
imports whichever builder matches the artifact it just generated, same as it already
chooses between the transcript and document *chunk* planes.

Nothing here writes to OpenSearch — pure dict building, exactly like the module it
extends.
"""

from __future__ import annotations

from typing import Any

from .index_mapping import DOC_TYPE_DOCUMENT_DIGEST
from .index_mapping import DOC_TYPE_FIELD
from .index_mapping import build_embedding_text
from .index_mapping import digest_chunk_index
from .index_mapping import digest_document_id


def build_document_digest_documents(
    *,
    file_uuid: str,
    file_id: int,
    digest: dict[str, Any],
    base_metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Turn a stored document digest into the documents Stage 3 will index.

    A document digest section has no speaker roster and no timestamp.
    ``start_time``/``end_time`` are left **unset** here rather than defaulted to 0 —
    defaulting would deep-link a citation to ``0:00``, the exact plausible-looking wrong
    answer addendum G7 exists to prevent for the transcript digest, and a document
    digest must not reintroduce the same trap in a new field. ``page`` is the first page
    the section's sentences touch, mirroring ``documents/chunking.py``'s own "the FIRST
    page a chunk touches" rule for the identical citation reason.

    Args:
        file_uuid: ``Document.uuid``.
        file_id: ``Document.id``. Present alongside the uuid for the same reason
            :func:`~.index_mapping.build_digest_documents` carries both — the ACL
            rewrite keys on ``file_id``, the tenant backfill on ``file_uuid``.
        digest: The stored ``file_facts.digest`` payload (``document_digest.build_document_digest``).
        base_metadata: Per-document fields shared with ``document_chunk`` index
            documents (Stage 6c's own base_metadata).

    Returns:
        One document per digest section, in section order.
    """
    documents: list[dict[str, Any]] = []
    for section in digest.get("sections", []):
        index = int(section["index"])
        body = str(section["text"])
        pages = list(section.get("pages") or [])
        document: dict[str, Any] = dict(base_metadata)
        document.update(
            {
                "file_id": file_id,
                "file_uuid": file_uuid,
                DOC_TYPE_FIELD: DOC_TYPE_DOCUMENT_DIGEST,
                "chunk_index": digest_chunk_index(index),
                "digest_section": index,
                "content": body,
                "embedding_text": build_embedding_text(
                    title=base_metadata.get("title"),
                    recorded_at=None,
                    roster=[],
                    body=body,
                ),
                "page": pages[0] if pages else None,
            }
        )
        # A document digest has no single speaker and no timestamp; drop anything
        # `base_metadata` happened to carry for those fields rather than let a
        # meaningless value ride along into the index.
        document.pop("speaker", None)
        document.pop("speakers", None)
        document.pop("start_time", None)
        document.pop("end_time", None)
        documents.append(document)

    return documents


def document_digest_document_ids(file_uuid: str, digest: dict[str, Any]) -> list[str]:
    """Ids matching :func:`build_document_digest_documents`, in the same order."""
    return [digest_document_id(file_uuid, int(s["index"])) for s in digest.get("sections", [])]
