"""Turn the excerpts offered to the model into citations the UI can link.

Citations are built from OUR structured chunk data, keyed by the excerpt ids we
assigned — never parsed out of model prose beyond the ``[n]`` marker itself. That
is what lets the frontend construct ``/files/{uuid}?t={seconds}`` links safely: the
model can influence *which* citation is shown, never *where* it points.
"""

from __future__ import annotations

import logging
import re

from app.services.chat.redactor import MaskedChunk

logger = logging.getLogger(__name__)

_CITATION_RE = re.compile(r"\[(\d{1,3})\]")
SNIPPET_CHARS = 240


def _snippet(text: str) -> str:
    """First ~240 chars of masked text, cut on a word boundary."""
    clean = " ".join(text.split())
    if len(clean) <= SNIPPET_CHARS:
        return clean
    cut = clean[:SNIPPET_CHARS]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + "…"


def build_citation(index: int, chunk: MaskedChunk) -> dict:
    """Serialize one chunk as a citation payload (snippet already masked)."""
    return {
        "id": index,
        "file_uuid": chunk.file_uuid,
        "title": chunk.title,
        "chunk_index": chunk.chunk_index,
        "start_time": chunk.start_time,
        "end_time": chunk.end_time,
        "speaker": chunk.speaker,
        "snippet": _snippet(chunk.content),
    }


def build_offered_citations(chunks: list[MaskedChunk]) -> list[dict]:
    """Citations for every excerpt offered to the model.

    Sent as the ``sources`` SSE frame BEFORE generation so the UI can show what
    is being consulted while the answer streams.
    """
    return [build_citation(i, chunk) for i, chunk in enumerate(chunks, start=1)]


def extract_used_citations(answer: str, offered: list[dict]) -> list[dict]:
    """Filter the offered citations down to those the answer actually references.

    Args:
        answer: The model's completed text.
        offered: Citations produced by :func:`build_offered_citations`.

    Returns:
        Referenced citations in first-mention order. Out-of-range markers (the
        model inventing ``[9]`` when 4 excerpts were offered) are dropped.
    """
    if not answer or not offered:
        return []

    by_id = {citation["id"]: citation for citation in offered}
    seen: set[int] = set()
    used: list[dict] = []
    for match in _CITATION_RE.finditer(answer):
        cid = int(match.group(1))
        if cid in seen:
            continue
        citation = by_id.get(cid)
        if citation is None:
            logger.debug("Answer referenced unknown citation [%d]; ignoring", cid)
            continue
        seen.add(cid)
        used.append(citation)
    return used
