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


#: What a citation points at. ``chunk`` is somebody's words at a timestamp;
#: ``digest`` is derived text summarising a span of the same recording;
#: ``document`` (issue #463) is a chunk of a parsed non-media document — no
#: timeline, no speaker, addressed by page/section instead. The frontend must
#: render all three differently — a digest quoted as speech would attribute
#: to a person words nobody said (addendum **G7**), and a document rendered
#: as a transcript excerpt would invent a speaker and a timestamp for a PDF.
KIND_CHUNK = "chunk"
KIND_DIGEST = "digest"
KIND_DOCUMENT = "document"


def build_citation(index: int, chunk: MaskedChunk) -> dict:
    """Serialize one retrieved document as a citation payload (snippet masked).

    A digest citation differs in three ways, each of which is a wrong answer if
    omitted (addendum **G7**):

    * ``kind`` is ``digest``, so the UI can label it as a summary rather than
      render it as a quote;
    * ``speaker`` is ``None`` — a digest section spans several speakers and
      naming one of them merges people, which base rule 5 forbids;
    * ``start_time`` is the section's **real** start, carried through from the
      extractive builder's provenance. A digest indexed at ``start_time=0``
      would deep-link every summary citation to ``0:00``, which looks like a
      working link and is not.

    A **document** citation (``chunk.source.is_document``) applies the same
    "never a 0 sentinel" rule one step further: a document has no timeline at
    all, so ``start_time``/``end_time`` are ``None`` rather than
    ``chunk.start_time``'s inherited ``0.0`` default — a real ``0`` there would
    render as a clickable ``00:00`` on a thing that was never a recording. It
    carries ``page``/``section_path`` instead, for a
    ``/documents/{uuid}?chunk=N`` link the frontend builds from ``kind`` +
    ``file_uuid`` + ``chunk_index`` (never a server-constructed URL — same
    convention as every other citation kind).

    ``schemas/chat.py``'s ``Citation.section_path`` is a single ``str | None``
    (a breadcrumb, not a list) — the already-landed union this dict validates
    against on reload — so a document chunk's ``list[str]`` section path is
    joined with ``" > "`` here rather than passed through raw.
    """
    is_digest = getattr(chunk.source, "is_digest", False)
    is_document = getattr(chunk.source, "is_document", False)
    if is_document:
        kind = KIND_DOCUMENT
    elif is_digest:
        kind = KIND_DIGEST
    else:
        kind = KIND_CHUNK
    section_path = getattr(chunk.source, "section_path", None) if is_document else None
    return {
        "id": index,
        "kind": kind,
        "file_uuid": chunk.file_uuid,
        "title": chunk.title,
        "chunk_index": chunk.chunk_index,
        "digest_section": getattr(chunk.source, "digest_section", None),
        "start_time": None if is_document else chunk.start_time,
        "end_time": None if is_document else chunk.end_time,
        "speaker": None if (is_digest or is_document) else chunk.speaker,
        "snippet": _snippet(chunk.content),
        "page": getattr(chunk.source, "page", None) if is_document else None,
        "section_path": " > ".join(section_path) if section_path else None,
        "char_start": getattr(chunk.source, "char_start", None) if is_document else None,
        "char_end": getattr(chunk.source, "char_end", None) if is_document else None,
    }


def build_offered_citations(
    chunks: list[MaskedChunk], excerpt_ids: list[int] | None = None
) -> list[dict]:
    """Citations for the excerpts that actually reached the prompt.

    Sent as the ``sources`` SSE frame so the UI can show what is being consulted
    while the answer streams.

    ``excerpt_ids`` are the 1-based ids :func:`prompting.format_excerpts`
    emitted. Passing them is what keeps the citation list and the prompt in
    agreement: the excerpt budget can drop retrieved chunks, and citing a chunk
    the model never saw presents an answer as sourced when it is not
    (issue #384). ``None`` cites every chunk and is kept only for callers that
    do no budgeting at all.

    Args:
        chunks: The masked chunks retrieval produced, in rank order.
        excerpt_ids: 1-based ids of the chunks rendered into the prompt.

    Returns:
        One citation payload per rendered excerpt, in excerpt-id order.
    """
    if excerpt_ids is None:
        return [build_citation(i, chunk) for i, chunk in enumerate(chunks, start=1)]
    return [build_citation(i, chunks[i - 1]) for i in excerpt_ids if 1 <= i <= len(chunks)]


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
