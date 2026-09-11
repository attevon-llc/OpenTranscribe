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
from app.services.ingest_artifacts.sizing import DIGEST_SECTION_MAX_WORDS

logger = logging.getLogger(__name__)

_CITATION_RE = re.compile(r"\[(\d{1,3})\]")

#: Ceiling for an ORDINARY (unexpanded, non-digest) chunk citation. A PREFIX
#: window, not centered or quote-aware — the first ``SNIPPET_CHARS`` characters
#: of the excerpt, cut on a word boundary, so a positional sample of the
#: excerpt is shown, not necessarily a quote-bearing one. Measured against a
#: committed eval baseline (issue #832): 31% of chunk citations exceed this
#: cap, and `quote_fidelity` (does a citation's displayed snippet actually
#: contain the quote the model claims to cite) scores 0.328 on the truncated
#: subset vs 0.671 on the complete one in the same run — truncation is a real,
#: measured cost, not a theoretical one. Raising this further is explicitly
#: DEFERRED (see the #832 follow-up issue): it needs a redaction-policy
#: decision (a local model already receives this text unmasked, but a wider
#: chunk-cap changes what a REMOTE provider or the on-screen card gets, and the
#: citation card currently clamps to 2 lines regardless of snippet length, so
#: raising this alone would have no reader-visible effect without a UI change
#: too).
SNIPPET_CHARS = 240

#: Snippet ceiling for an EXPANDED citation (issue #526). ``context_expansion``
#: already bounds a widened chunk to ``MAX_EXPANDED_WORDS`` (250) words before
#: masking ever sees it, so this is a generous char-per-word estimate on that
#: SAME bound, not a second independent cap — the point is "cover the whole
#: widened excerpt", not "pick a new limit". Masking can only shrink text
#: (placeholders replace spans, never lengthen them), so this is already an
#: overestimate of the true ceiling.
#:
#: ⚠️ Do not reuse this for an ordinary (unexpanded) chunk. The excerpt budget
#: (``prompting.format_excerpts``) is untouched by #526 — this only changes how
#: much of an ALREADY-SENT excerpt the citation shows the reader, never what
#: reaches the prompt.
EXPANDED_SNIPPET_CHARS = 10 * 250

#: Snippet ceiling for a DIGEST citation (issue #832). A digest section is
#: bounded at ingest to :data:`~app.services.ingest_artifacts.sizing.DIGEST_SECTION_MAX_WORDS`
#: words (``ingest_artifacts/sizing.py``) — and at the ordinary
#: :data:`SNIPPET_CHARS` cap, a measured probe run found **100% of digest
#: citations truncated (113/113, median snippet length 238 chars)**, by
#: construction: a digest section is always longer than 240 chars, so a
#: fixed-prefix window into it never covers the whole section. 10 chars/word
#: is a generous per-word estimate against that SAME bound — the point is
#: "cover the whole section", not "pick a new limit chosen by guesswork" — so
#: this is derived, not independent. Masking (redaction) can only ever SHRINK
#: text (placeholders replace spans, never lengthen them), so this is already
#: an overestimate of the true ceiling and safe with respect to the redaction
#: pipeline.
DIGEST_SNIPPET_CHARS = 10 * DIGEST_SECTION_MAX_WORDS

#: Snippet ceiling for an OVERVIEW citation (:func:`build_overview_citations`,
#: #532 arm (a)). A ``FileSummary.digest`` there is MULTIPLE digest sections
#: joined — up to ``mapreduce.overview.sections_budget()``'s ceiling of 3 per
#: file (verified against that function's own ``min(3, ...)`` cap, not
#: assumed) — so a citation covering the whole joined text needs three times
#: :data:`DIGEST_SNIPPET_CHARS`, not the single-section cap.
OVERVIEW_SNIPPET_CHARS = 3 * DIGEST_SNIPPET_CHARS


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """First ``limit`` chars of masked text, cut on a word boundary.

    ``limit`` defaults to :data:`SNIPPET_CHARS` for an ordinary chunk —
    unchanged from before issue #526, so an unexpanded citation's snippet is
    byte-identical to today's. :func:`build_citation` passes
    :data:`EXPANDED_SNIPPET_CHARS` for a chunk ``context_expansion`` widened,
    so the snippet can show the reader everything the model actually read
    instead of silently truncating a widened excerpt back down to the size of
    an ordinary one — the #526 defect (a citation naming a shorter span than
    what the model was given).
    """
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + "…"


#: What a citation points at. ``chunk`` is somebody's words at a timestamp;
#: ``digest`` is derived text summarising a span of the same recording.
#: The frontend must render the two differently — a digest quoted as speech
#: would attribute to a person words nobody said (addendum **G7**).
KIND_CHUNK = "chunk"
KIND_DIGEST = "digest"


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

    **``expanded`` (issue #526).** True only for a transcript-chunk citation
    whose ``start_time``/``end_time`` were widened by
    ``chat/context_expansion.py`` to the chunk's surrounding exchange before
    masking — never for a digest citation, which that module never touches
    (:func:`~app.services.chat.context_expansion.needs_expansion` excludes it
    by construction). It exists so the UI, and any reader
    inspecting the raw payload, can tell "this span is wider than its own
    indexed chunk" apart from an ordinary citation — a fabricated distinction
    would be dishonest given ``chunk_index`` still names the ORIGINAL,
    unexpanded chunk in the index (a reindex-free read-time widening has
    nothing else to name it by). When ``expanded`` is True the snippet is also
    NOT capped to the ordinary :data:`SNIPPET_CHARS` — see :func:`_snippet` —
    so what the reader sees is everything the model was actually given for
    this citation, not a truncated prefix of it.
    """
    is_digest = getattr(chunk.source, "is_digest", False)
    kind = KIND_DIGEST if is_digest else KIND_CHUNK
    expanded = bool(getattr(chunk, "expanded", False)) if kind == KIND_CHUNK else False
    if expanded:
        snippet_limit = EXPANDED_SNIPPET_CHARS
    elif is_digest:
        snippet_limit = DIGEST_SNIPPET_CHARS
    else:
        snippet_limit = SNIPPET_CHARS
    return {
        "id": index,
        "kind": kind,
        "file_uuid": chunk.file_uuid,
        "title": chunk.title,
        "chunk_index": chunk.chunk_index,
        "digest_section": getattr(chunk.source, "digest_section", None),
        "start_time": chunk.start_time,
        "end_time": chunk.end_time,
        "speaker": None if is_digest else chunk.speaker,
        "snippet": _snippet(chunk.content, snippet_limit),
        "expanded": expanded,
        # A plain integer count, NEVER prose (never the excerpt text itself) — see
        # module docstring's SNIPPET_CHARS/DIGEST_SNIPPET_CHARS comments. Exists so
        # a future measurement of "what cap actually covers what this deployment
        # produces" is a real number instead of a guess, and so the eval harness
        # can report a truncation rate without ever touching source text (#832).
        "content_chars": len(" ".join(chunk.content.split())),
    }


def build_overview_citations(
    cited_entries: tuple[tuple[int, str], ...],
    summaries: list,
) -> list[dict]:
    """Citation payloads for the overview's listed recordings (#532 arm (a)).

    EXPERIMENT support — delete with the arm. ``kind`` is ``digest`` (the
    entries ARE masked digest text with per-file provenance), so the UI's
    existing summary labelling applies. ``chunk_index``/``digest_section`` are
    ``None``: an overview entry cites the recording's digest as a whole, not
    one indexed section — the snippet carries exactly the text the model saw,
    which is the #384 property that matters.

    Args:
        cited_entries: ``Overview.cited_entries`` — ``(citation_id, file_uuid)``
            in listing order.
        summaries: The ``FileSummary`` list the overview was composed from
            (already masked by the map stage).

    Returns:
        One payload per cited entry, in id order. Entries whose file_uuid no
        longer matches a summary are skipped rather than cited empty.
    """
    by_uuid = {s.file_uuid: s for s in summaries}
    payloads: list[dict] = []
    for citation_id, file_uuid in cited_entries:
        summary = by_uuid.get(file_uuid)
        if summary is None:
            continue
        digest_text = summary.digest or ""
        payloads.append(
            {
                "id": citation_id,
                "kind": KIND_DIGEST,
                "file_uuid": file_uuid,
                "title": summary.title,
                "chunk_index": None,
                "digest_section": None,
                "start_time": None,
                "end_time": None,
                "speaker": None,
                "snippet": _snippet(digest_text, OVERVIEW_SNIPPET_CHARS),
                "expanded": False,
                "page": None,
                "section_path": None,
                "char_start": None,
                "char_end": None,
                # See build_citation's content_chars comment — same rule, same reason.
                "content_chars": len(" ".join(digest_text.split())),
            }
        )
    return payloads


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
