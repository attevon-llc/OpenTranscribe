"""Resolve a MIXED scope of recording and document uuids against ``file_facts``.

The #403 Stage-6 gate: a collection can hold recordings and documents together, and a
map over that collection must cover both — never silently drop the document half or
under-report ``files_without_artifacts`` for it. ``chat/mapreduce.scope_digest_hits`` is
the existing map for **recordings only** (``MediaFile`` outer-joined to
``FileFacts.media_file_id``) and lives outside this lane's file set; this module is what
its document-aware successor needs, offered by shape rather than by editing that file
directly.

⚠️ **Coverage is not ranking.** Same rule ``mapreduce.scope_digest_hits`` documents for
itself: this answers "which files are in scope", by reading ``file_facts`` for every
uuid the caller names, never by ranking relevance and hoping the top-K happens to cover
every file. Raising a `size` parameter would not fix a ranked leg's coverage gap, because
a ranked leg has no coverage guarantee at any K — so this module has no ranking step at
all, only two outer joins and a set difference.

⚠️ **Outer join, not inner**, same reasoning ``scope_digest_hits`` gives: a media file or
document that has not yet been through artifact generation must still be *counted* in
scope, in ``files_without_artifacts`` — never simply absent, which is indistinguishable
from never having been in scope at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Literal

SourceKind = Literal["media", "document"]


@dataclass(frozen=True)
class ScopeFactsHit:
    """One in-scope file's stored artifacts, regardless of which table owns it."""

    kind: SourceKind
    source_id: int
    uuid: str
    title: str
    digest: dict[str, Any]
    facts: dict[str, Any]
    keyphrases: dict[str, Any]


@dataclass
class ScopeFactsCoverage:
    """The map's hits, plus the coverage of the map itself.

    ``files_without_artifacts`` counts every scope member that produced no hit — either
    a real row with no ``file_facts`` yet, or a uuid that matched neither table at all
    (deleted between scope resolution and this read, or simply invalid) — because a
    member silently absent from ``hits`` is indistinguishable from one that was never in
    scope, which is the exact defect ``mapreduce.scope_digest_hits`` documents fixing
    for the outer-join case alone. Never larger than ``files_total``.
    """

    hits: list[ScopeFactsHit] = field(default_factory=list)
    files_without_artifacts: int = 0
    files_total: int = 0


def scope_facts_for_uuids(db: Any, file_uuids: list[str]) -> ScopeFactsCoverage:
    """One ``file_facts`` row per uuid in *file_uuids*, covering both media and documents.

    Args:
        db: SQLAlchemy session.
        file_uuids: The resolved scope — a mix of ``media_file.uuid`` and
            ``document.uuid`` values, however the caller's scope resolver produced it.
            Bounded, same precondition ``scope_digest_hits`` documents: an unbounded
            scope cannot be mapped over.

    Returns:
        A :class:`ScopeFactsCoverage` naming every hit and counting every member the
        map could not produce a hit for — never dropping one silently.
    """
    wanted = list(dict.fromkeys(str(u) for u in file_uuids))  # de-duplicate, keep order
    if not wanted:
        return ScopeFactsCoverage([], 0, 0)

    from app.models.document import Document
    from app.models.file_facts import FileFacts
    from app.models.media import MediaFile

    media_rows = (
        db.query(
            MediaFile.id,
            MediaFile.uuid,
            MediaFile.title,
            MediaFile.filename,
            FileFacts.digest,
            FileFacts.facts,
            FileFacts.keyphrases,
        )
        .outerjoin(FileFacts, FileFacts.media_file_id == MediaFile.id)
        .filter(MediaFile.uuid.in_(wanted))
        .all()
    )
    document_rows = (
        db.query(
            Document.id,
            Document.uuid,
            Document.filename,
            FileFacts.digest,
            FileFacts.facts,
            FileFacts.keyphrases,
        )
        .outerjoin(FileFacts, FileFacts.document_id == Document.id)
        .filter(Document.uuid.in_(wanted))
        .all()
    )

    hits: list[ScopeFactsHit] = []
    missing = 0
    seen: set[str] = set()

    for source_id, uuid, title, filename, digest, facts, keyphrases in media_rows:
        seen.add(str(uuid))
        if digest is None:
            missing += 1
            continue
        hits.append(
            ScopeFactsHit(
                kind="media",
                source_id=int(source_id),
                uuid=str(uuid),
                title=str(title or filename or ""),
                digest=digest,
                facts=facts or {},
                keyphrases=keyphrases or {},
            )
        )

    for source_id, uuid, filename, digest, facts, keyphrases in document_rows:
        seen.add(str(uuid))
        if digest is None:
            missing += 1
            continue
        hits.append(
            ScopeFactsHit(
                kind="document",
                source_id=int(source_id),
                uuid=str(uuid),
                title=str(filename or ""),
                digest=digest,
                facts=facts or {},
                keyphrases=keyphrases or {},
            )
        )

    # A scope uuid that matched NEITHER table (deleted since scope resolution, or a
    # bad uuid) is still counted rather than dropped — same rule as the outer join.
    missing += len(set(wanted) - seen)

    return ScopeFactsCoverage(hits=hits, files_without_artifacts=missing, files_total=len(wanted))
