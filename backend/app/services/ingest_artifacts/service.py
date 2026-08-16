"""Generate and persist the deterministic ingest artifacts for one file.

The orchestrator: read segments → facts + digest + keyphrases → upsert ``file_facts``.
Postgres in, Postgres out. No OpenSearch, no LLM, no model load (#403 **D6**, Stage 2
scope).

Callable from three places, and it needs to behave identically in all three:

- the transcription-completion path (``tasks/ingest_artifacts_task``, nlp queue),
- Stage 3's ``reindex_transcript``, which must regenerate digests inside the rebuild or
  every reindex silently destroys them (addendum **G1**),
- a rename, which changes the resolved speaker names the digest quotes (issue #405).

All three converge on :func:`generate_file_artifacts`, and the ``source_fingerprint``
short-circuit is what makes calling it on every reindex cheap.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.models.file_facts import FileFacts
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.utils.transcript_builders import compute_speaker_stats
from app.utils.transcript_builders import get_speaker_name

from .digest import DIGEST_SCHEMA_VERSION
from .digest import build_digest
from .facts import FACTS_SCHEMA_VERSION
from .facts import build_facts
from .keyphrases import KEYPHRASE_SCHEMA_VERSION
from .keyphrases import extract_keyphrases
from .provenance import validate_provenance

logger = logging.getLogger(__name__)

#: The three payload schema versions, joined. Stored on the row; a mismatch means
#: "regenerate", which is how an algorithm change rolls out on the next reindex.
GENERATOR_VERSION = f"{FACTS_SCHEMA_VERSION}.{DIGEST_SCHEMA_VERSION}.{KEYPHRASE_SCHEMA_VERSION}"


def load_ordered_segments(db: Session, file_id: int) -> list[dict[str, Any]]:
    """Read one file's segments in a **total** order, with resolved speaker names.

    ``start_time`` alone is not a total order — 3,072 tie groups over 6,152 segments on
    the eval corpus — and Postgres returns tied rows in physical order, which changes
    after any rewrite. Every artifact here is a function of segment *adjacency* (turns,
    sentence grouping, longest monologue), so a non-total order would make the digest
    non-reproducible in exactly the way that invalidated the first Stage 1 baseline
    (#433).

    Returns:
        Plain dicts, so the pure functions in this package never touch the ORM and are
        testable without a database.
    """
    segments = (
        db.query(TranscriptSegment)
        .options(joinedload(TranscriptSegment.speaker))
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(
            TranscriptSegment.start_time,
            TranscriptSegment.end_time,
            TranscriptSegment.id,
        )
        .all()
    )
    return [
        {
            "id": int(segment.id),
            "text": str(segment.text or ""),
            "start_time": float(segment.start_time or 0.0),
            "end_time": float(segment.end_time or 0.0),
            # The resolved display name, not the raw diarization label: the roster the
            # user sees is the roster the facts report, and a rename therefore changes
            # the fingerprint and triggers regeneration.
            "speaker": get_speaker_name(segment),
        }
        for segment in segments
    ]


def source_fingerprint(segments: list[dict[str, Any]]) -> str:
    """SHA-256 over the ordered segments — the "has anything changed?" key.

    Covers id, timings, resolved speaker and text. Editing a transcript, re-diarizing, or
    renaming a speaker all change it; re-running the pipeline over identical data does
    not. Hashing a stable field order (not ``json.dumps`` of a dict) keeps it independent
    of dict ordering.
    """
    digest = hashlib.sha256()
    for segment in segments:
        digest.update(
            "\x1f".join(
                (
                    str(segment["id"]),
                    f"{segment['start_time']:.3f}",
                    f"{segment['end_time']:.3f}",
                    str(segment["speaker"]),
                    str(segment["text"]),
                )
            ).encode("utf-8")
        )
        digest.update(b"\x1e")
    return digest.hexdigest()


def build_artifacts(
    segments: list[dict[str, Any]],
    *,
    duration: float | None,
    language: str | None,
    recorded_at: Any,
) -> dict[str, Any]:
    """Build all three payloads from ordered segments. Pure — no DB, no I/O."""
    resolved_language = (language or "en").split("-")[0].lower()

    facts = build_facts(
        segments,
        speaker_stats=_speaker_stats_from_dicts(segments),
        duration=duration,
        language=resolved_language,
        recorded_at=recorded_at,
    )
    digest = build_digest(segments, language=resolved_language)
    keyphrases = extract_keyphrases(
        " ".join(str(s.get("text") or "") for s in segments),
        language=resolved_language,
    )

    # Fail at the producer. A malformed provenance surfaces downstream as a citation that
    # deep-links nowhere, which is the silent-wrong-answer class this epic keeps hitting.
    for section in digest["sections"]:
        for sentence in section["sentences"]:
            validate_provenance(sentence["provenance"])

    return {"facts": facts, "digest": digest, "keyphrases": keyphrases}


class _StatSegment:
    """Adapter so :func:`compute_speaker_stats` can read plain dicts.

    ``compute_speaker_stats`` is shared with the summarization path, where it is handed
    ORM rows; duplicating its arithmetic for dicts would be two implementations of the
    same aggregate, which is how the two would drift.
    """

    __slots__ = ("end_time", "speaker", "start_time", "text")

    def __init__(self, data: dict[str, Any]) -> None:
        self.text = data.get("text") or ""
        self.start_time = float(data.get("start_time") or 0.0)
        self.end_time = float(data.get("end_time") or 0.0)
        self.speaker = _ResolvedSpeaker(str(data.get("speaker") or "Unknown Speaker"))


class _ResolvedSpeaker:
    """A speaker whose display name is already resolved and verified."""

    __slots__ = ("confidence", "display_name", "name", "suggested_name", "verified")

    def __init__(self, name: str) -> None:
        self.name = name
        self.display_name = name
        self.verified = True
        self.suggested_name = None
        self.confidence = None


def _speaker_stats_from_dicts(segments: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = compute_speaker_stats([_StatSegment(s) for s in segments])
    return stats


def generate_file_artifacts(
    db: Session,
    file_id: int,
    *,
    force: bool = False,
) -> FileFacts | None:
    """Generate and upsert ``file_facts`` for *file_id*.

    Args:
        db: Active session. The caller owns the transaction — this function flushes but
            does not commit, so a reindex batch can do many files in one transaction.
        file_id: ``MediaFile.id``.
        force: Regenerate even when the fingerprint and generator version both match.

    Returns:
        The persisted row, or ``None`` when the file has no segments (nothing to
        summarise — a file still in PROCESSING, or one whose transcript was cleared).
    """
    media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
    if media_file is None:
        logger.warning("file_facts: media file %s not found", file_id)
        return None

    segments = load_ordered_segments(db, file_id)
    if not segments:
        logger.info("file_facts: file %s has no segments; nothing to summarise", file_id)
        return None

    fingerprint = source_fingerprint(segments)
    existing = db.query(FileFacts).filter(FileFacts.media_file_id == file_id).first()
    if (
        existing is not None
        and not force
        and existing.source_fingerprint == fingerprint
        and existing.generator_version == GENERATOR_VERSION
    ):
        logger.debug("file_facts: file %s already current (fingerprint match)", file_id)
        return existing

    started = time.perf_counter()
    artifacts = build_artifacts(
        segments,
        duration=media_file.duration,
        language=media_file.language,
        recorded_at=media_file.upload_time,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    digest = artifacts["digest"]
    row = existing or FileFacts(media_file_id=file_id)
    row.generator_version = GENERATOR_VERSION
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
        "file_facts: file %s → %d sections / %d digest words / %d keyphrases in %d ms",
        file_id,
        row.section_count,
        row.digest_word_count,
        len(artifacts["keyphrases"]["phrases"]),
        elapsed_ms,
    )
    return row
