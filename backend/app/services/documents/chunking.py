"""IR → chunk rows. Where the parser half ends and the indexing half (Stage 6b) begins.

This produces **durable storage only**: dicts destined for ``document_chunk`` rows. It
never touches OpenSearch, never embeds, and never decides an index mapping — Stage 3 owns
the index and Stage 6b will read these rows.

Four decisions, each with the reason it is not the obvious alternative:

* **Chunk on the IR's block structure, not on Docling's chunkers.** ``docling#3335`` has
  the DOCX chunkers returning zero chunks when the document contains a table, and a
  chunker we do not control is a chunker whose output length distribution we cannot hold
  equal to the transcript plane's. Heterogeneous chunk lengths distort RRF, and RRF is
  what fuses these against transcript chunks in one query.
* **Reuse the transcript chunker's target size**, read at call time rather than declared
  again. Two settings that must agree are one that will not.
* **A table is never split.** Half a table is not a retrievable unit — the header row
  carrying the column names ends up in a different chunk from the values.
* **Every chunk carries ``char_start``/``char_end`` into the IR text.** That is the whole
  reason the IR has one canonical string: it is what lets a citation, a viewer highlight
  and a cached redaction span address the same passage. It is also the ``char_range``
  arm of the #403 **D3** provenance union, so a document digest can join the summary
  tier without a second addressing scheme.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from .ir import Block
from .ir import ParsedDocument

logger = logging.getLogger(__name__)

#: Blocks excluded from a chunk's indexed content. Running heads and page numbers repeat
#: on every page, so including them makes every chunk of a document look alike to an
#: embedding model — the exact noise olmOCR-bench's 753 ``absent`` assertions were built
#: to detect. They stay in the IR (the viewer renders them, and their offsets keep the
#: coordinate system contiguous); they just do not become chunk text.
NON_CONTENT_BLOCKS: frozenset[str] = frozenset({"page_header", "page_footer"})

#: A heading alone is not a chunk. Emitting one produces a 3-word chunk whose embedding is
#: dominated by a section number, which then outranks the section's actual content.
MIN_CHUNK_WORDS = 12


def _target_words() -> int:
    """The transcript chunker's target, read from the same place it reads it.

    ``chunking_service.chunk_transcript`` defaults to ``settings.SEARCH_CHUNK_TARGET_WORDS``
    (`chunking_service.py:219-220`); this reads the identical value rather than declaring a
    document-side twin. Heterogeneous chunk lengths across the two planes distort RRF, and
    RRF fuses them inside one query against one index — so "documents use 250 words" would
    be a retrieval change disguised as a config default.
    """
    from app.core.config import settings

    return int(settings.SEARCH_CHUNK_TARGET_WORDS)


@dataclass
class DocumentChunk:
    """One chunk, in the shape the ``document_chunk`` row takes.

    Deliberately **not** the OpenSearch document shape. Stage 6b maps these to index
    documents; keeping the two apart is what lets a reindex read rows instead of
    re-parsing the original — ``delete_transcript_chunks`` is an unqualified delete, so a
    rebuild that needed the source file back would lose every document whose original had
    been cleaned up.
    """

    chunk_index: int
    text: str
    char_start: int
    char_end: int
    page: int | None = None
    section_path: list[str] = field(default_factory=list)
    block_types: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        return {
            "chunk_index": self.chunk_index,
            "text": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "page": self.page,
            "section_path": self.section_path,
            "block_types": self.block_types,
        }


def chunk_document(
    document: ParsedDocument,
    *,
    target_words: int | None = None,
    title: str | None = None,
) -> list[DocumentChunk]:
    """Split a parsed document into retrieval chunks.

    Args:
        document: A validated :class:`~app.services.documents.ir.ParsedDocument`.
        target_words: Override the transcript chunker's target. Tests use it; production
            should not.
        title: Document title, recorded on the chunk's ``section_path`` head so a
            breadcrumb is available to whatever builds the indexed string. **Not**
            prepended to ``text`` here — ``text`` must stay a verbatim slice of the IR or
            ``char_start``/``char_end`` stop addressing it, and contextual enrichment is
            an indexing-time concern.

    Returns:
        Chunks in reading order with contiguous ``chunk_index`` from 0.
    """
    target = target_words or _target_words()
    content = [b for b in document.blocks if b.type not in NON_CONTENT_BLOCKS]

    chunks: list[DocumentChunk] = []
    pending: list[Block] = []

    def flush() -> None:
        if not pending:
            return
        chunk = _make_chunk(document, pending, len(chunks), title)
        if chunk is not None:
            chunks.append(chunk)
        pending.clear()

    for block in content:
        # A table is its own chunk, always: split mid-row it is worthless for retrieval,
        # and merged into prose its linearised `header: cell` text swamps the sentence
        # around it.
        if block.type == "table":
            flush()
            pending.append(block)
            flush()
            continue

        # A single block can be longer than the whole target. Measured on real corpora:
        # a pypdfium2 page with no blank lines is ONE paragraph block of 800-1,200 words,
        # and without this, 69 of 126 non-table chunks came out over 3x the target — chunks
        # whose embeddings are averaged over a whole page and whose citations point at it.
        if len(block.text.split()) > target * 2:
            flush()
            for start, end in _split_long_block(document, block, target):
                chunk = _make_chunk_from_range(document, block, start, end, len(chunks), title)
                if chunk is not None:
                    chunks.append(chunk)
            continue

        # Break BEFORE a heading at or above the current section's depth: that heading
        # starts a new section, and carrying the tail of the previous one into it is how
        # a chunk comes to answer for two topics at once.
        if block.type == "heading" and pending and _starts_new_section(pending, block):
            flush()

        pending.append(block)
        if _word_count(pending) >= target:
            flush()

    flush()
    return chunks


def _starts_new_section(pending: list[Block], heading: Block) -> bool:
    """Does *heading* close the section the pending blocks belong to?"""
    current = [b.level for b in pending if b.type == "heading" and b.level is not None]
    if not current:
        return True
    return (heading.level or 1) <= min(current)


def _word_count(blocks: list[Block]) -> int:
    return sum(len(b.text.split()) for b in blocks)


def _make_chunk(
    document: ParsedDocument, blocks: list[Block], index: int, title: str | None
) -> DocumentChunk | None:
    """Build one chunk as a **verbatim slice** of the IR text.

    Slicing rather than joining the blocks' own text is the point: the slice includes the
    separators the builder wrote, so ``document.text[char_start:char_end] == chunk.text``
    holds and a highlight computed from the chunk lands on the same characters the
    citation named. Joining would produce text that is *nearly* the slice and offsets that
    are quietly wrong.
    """
    char_start = blocks[0].char_start
    char_end = blocks[-1].char_end
    text = document.text[char_start:char_end]
    if not text.strip():
        return None
    if len(text.split()) < MIN_CHUNK_WORDS and all(b.type == "heading" for b in blocks):
        return None

    section_path = list(blocks[0].section_path)
    if blocks[0].type == "heading":
        section_path = [*section_path, blocks[0].text]
    if title:
        section_path = [title, *section_path]

    pages = [b.page for b in blocks if b.page is not None]
    return DocumentChunk(
        chunk_index=index,
        text=text,
        char_start=char_start,
        char_end=char_end,
        # The FIRST page the chunk touches, not the last: a citation should land the
        # reader where the passage begins.
        page=min(pages) if pages else None,
        section_path=section_path,
        block_types=sorted({b.type for b in blocks}),
    )


def _split_long_block(document: ParsedDocument, block: Block, target: int) -> list[tuple[int, int]]:
    """Cut one over-long block into ``(char_start, char_end)`` ranges at sentence bounds.

    Sentence boundaries come from ``services/search/chunking_service.split_into_sentences``
    — **imported, not reimplemented**. That function is already shared by the transcript
    chunker and ``services/ingest_artifacts/digest``, and it is public for exactly this
    reason: three sentence splitters would put chunk boundaries, digest boundaries and
    document boundaries in three different places over the same words.

    (The plan asks for ``_sliding_window`` to be *extracted* out of ``_split_long_turn``
    and shared. That edit lands in ``services/search/``, which Stage 3 owns while it
    rebuilds the index; deferred to Stage 6b rather than taken here. The overlap this
    produces is zero, where the transcript chunker overlaps — a difference to settle
    when the two are unified, not to guess at now.)

    Offsets are located by searching forward in the IR text from the block's own start,
    never by arithmetic on sentence lengths: the separators between sentences belong to
    the document, and re-deriving them is how offsets drift.
    """
    from app.services.search.chunking_service import split_into_sentences

    # `document.language` is the parser's detected language, or None when it could
    # not tell (ir.py's ParsedDocument.language). Passing None here — never
    # defaulting to "en" — is deliberate: split_into_sentences's own guard
    # (`chunking_service._punkt_can_read`, issue #448) only applies its
    # script/terminator disqualifiers when the language is NOT a recognised punkt
    # code, so a hardcoded "en" defeated the guard for every document regardless of
    # actual language — Thai and Devanagari text was silently handed to English
    # punkt and returned as one giant "sentence". None takes the guard's own
    # no-language path: it falls through to the text-based script/terminator check
    # instead of trusting a language nobody actually asserted.
    sentences = split_into_sentences(block.text, language=document.language)
    spans: list[tuple[int, int]] = []
    cursor = block.char_start
    for sentence in sentences:
        found = document.text.find(sentence, cursor, block.char_end)
        if found < 0:
            continue
        spans.append((found, found + len(sentence)))
        cursor = found + len(sentence)

    if not spans:
        return _hard_word_split(document, block, target)

    ranges: list[tuple[int, int]] = []
    start = spans[0][0]
    end = spans[0][1]
    words = len(document.text[start:end].split())
    for span_start, span_end in spans[1:]:
        span_words = len(document.text[span_start:span_end].split())
        if words + span_words > target:
            ranges.append((start, end))
            start, end, words = span_start, span_end, span_words
            continue
        end = span_end
        words += span_words
    ranges.append((start, end))
    return ranges


def _hard_word_split(document: ParsedDocument, block: Block, target: int) -> list[tuple[int, int]]:
    """Last resort for a block with no locatable sentence boundaries.

    Reached by a page of tabular text with no terminal punctuation, and by any language
    the sentence splitter has no model for. Cutting on whitespace runs keeps the ranges
    verbatim slices, which a naive ``' '.join`` of word lists would not.
    """
    import re

    text = document.text[block.char_start : block.char_end]
    boundaries = [m.start() for m in re.finditer(r"\s+", text)]
    if not boundaries:
        return [(block.char_start, block.char_end)]

    ranges: list[tuple[int, int]] = []
    start = 0
    words_seen = 0
    for boundary in boundaries:
        words_seen += 1
        if words_seen >= target:
            ranges.append((block.char_start + start, block.char_start + boundary))
            start = boundary + 1
            words_seen = 0
    if start < len(text):
        ranges.append((block.char_start + start, block.char_end))
    return ranges


def _make_chunk_from_range(
    document: ParsedDocument,
    block: Block,
    char_start: int,
    char_end: int,
    index: int,
    title: str | None,
) -> DocumentChunk | None:
    """A chunk carved out of the middle of one block, inheriting its section and page."""
    text = document.text[char_start:char_end]
    if not text.strip():
        return None
    section_path = list(block.section_path)
    if title:
        section_path = [title, *section_path]
    return DocumentChunk(
        chunk_index=index,
        text=text,
        char_start=char_start,
        char_end=char_end,
        page=block.page,
        section_path=section_path,
        block_types=[block.type],
    )
