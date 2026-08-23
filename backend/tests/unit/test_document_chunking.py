"""Chunking a parsed document, and the offsets that must survive it.

The invariant this suite exists for: a chunk's ``text`` is a **verbatim slice** of the IR
text at its own ``char_start``/``char_end``. Every downstream anchor — a chat citation, a
viewer highlight, a cached redaction span, a ``char_range`` digest provenance — resolves
by slicing that string. A chunker that joins block texts instead produces output that
*looks* right and offsets that are quietly off by the separators.

The real-corpus class at the bottom runs the whole thing over parsed PDFs and DOCX files,
because a chunker is exactly the kind of code that is correct on three synthetic blocks
and wrong on a 40-page contract.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.documents import ParseOptions
from app.services.documents import ParseSource
from app.services.documents import detect_document_mime
from app.services.documents.chunking import MIN_CHUNK_WORDS
from app.services.documents.chunking import NON_CONTENT_BLOCKS
from app.services.documents.chunking import chunk_document
from app.services.documents.ir import IRBuilder

NAS_ROOT = Path(os.environ.get("RAG_EVAL_DATA_DIR", "/mnt/nas/opentranscribe-benchmarks"))
DOCS = NAS_ROOT / "documents"


def _doc(blocks, **build):
    builder = IRBuilder()
    for spec in blocks:
        builder.add(**spec)
    return builder.build(parser="test", parser_version="0", **build)


def _para(words: int, word: str = "word") -> str:
    return " ".join([word] * words)


class TestTheOffsetInvariantSurvivesChunking:
    def test_every_chunk_slices_back_out_of_the_ir_text(self):
        document = _doc(
            [
                {"block_type": "heading", "text": "1 Scope", "level": 1},
                {"block_type": "paragraph", "text": _para(100)},
                {"block_type": "heading", "text": "2 Term", "level": 1},
                {"block_type": "paragraph", "text": _para(100)},
            ]
        )
        chunks = chunk_document(document, target_words=60)

        assert len(chunks) >= 2, "the loop below proves nothing over a single chunk"
        for chunk in chunks:
            assert document.text[chunk.char_start : chunk.char_end] == chunk.text

    def test_chunks_are_ordered_contiguously_indexed_and_non_overlapping(self):
        document = _doc([{"block_type": "paragraph", "text": _para(80)} for _ in range(6)])
        chunks = chunk_document(document, target_words=50)

        assert len(chunks) >= 3
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
        for earlier, later in zip(chunks, chunks[1:], strict=False):
            assert earlier.char_end <= later.char_start

    def test_a_chunk_is_a_slice_not_a_join_so_separators_are_included(self):
        """The discriminating case. Joining block texts gives the same words and offsets
        that are short by ``len(BLOCK_SEPARATOR)`` per block boundary."""
        document = _doc(
            [
                {"block_type": "paragraph", "text": "First."},
                {"block_type": "paragraph", "text": "Second."},
            ]
        )
        chunks = chunk_document(document, target_words=500)

        assert len(chunks) == 1
        assert chunks[0].text == "First.\n\nSecond."
        assert "".join(b.text for b in document.blocks) != chunks[0].text


class TestSectionBoundaries:
    def test_a_top_level_heading_starts_a_new_chunk(self):
        document = _doc(
            [
                {"block_type": "heading", "text": "1 Scope", "level": 1},
                {"block_type": "paragraph", "text": _para(20)},
                {"block_type": "heading", "text": "2 Term", "level": 1},
                {"block_type": "paragraph", "text": _para(20)},
            ]
        )
        chunks = chunk_document(document, target_words=500)

        assert len(chunks) == 2, "two sections were merged into one chunk"
        assert chunks[0].text.startswith("1 Scope")
        assert chunks[1].text.startswith("2 Term")

    def test_a_deeper_subheading_does_not_break_its_parent_section(self):
        """The negative control for the test above: if any heading broke, a document with
        numbered subheadings would chunk into single sentences."""
        document = _doc(
            [
                {"block_type": "heading", "text": "1 Scope", "level": 1},
                {"block_type": "paragraph", "text": _para(10)},
                {"block_type": "heading", "text": "1.1 Detail", "level": 2},
                {"block_type": "paragraph", "text": _para(10)},
            ]
        )
        chunks = chunk_document(document, target_words=500)
        assert len(chunks) == 1

    def test_the_section_path_reaches_the_chunk(self):
        document = _doc(
            [
                {"block_type": "heading", "text": "3 Methods", "level": 1},
                {"block_type": "heading", "text": "3.1 Data", "level": 2},
                {"block_type": "paragraph", "text": _para(40)},
            ]
        )
        chunks = chunk_document(document, target_words=500, title="A Paper")

        assert chunks
        assert chunks[0].section_path[0] == "A Paper"
        assert "3 Methods" in chunks[0].section_path

    def test_the_title_is_not_prepended_to_the_text(self):
        """Contextual enrichment belongs at indexing time. Prepending it here would break
        ``text == ir_text[char_start:char_end]`` for every chunk in the corpus."""
        document = _doc([{"block_type": "paragraph", "text": _para(30)}])
        chunks = chunk_document(document, target_words=500, title="A Paper")

        assert chunks
        assert not chunks[0].text.startswith("A Paper")
        assert document.text[chunks[0].char_start : chunks[0].char_end] == chunks[0].text


class TestTables:
    def test_a_table_is_its_own_chunk(self):
        document = _doc(
            [
                {"block_type": "paragraph", "text": _para(10)},
                {
                    "block_type": "table",
                    "text": "Item: Widget | Cost: 42",
                    "table": [["Item", "Cost"], ["Widget", "42"]],
                },
                {"block_type": "paragraph", "text": _para(10)},
            ]
        )
        chunks = chunk_document(document, target_words=500)

        assert len(chunks) == 3, "the table was merged with the prose around it"
        assert chunks[1].block_types == ["table"]

    def test_a_table_is_never_split_even_past_the_target(self):
        """Half a table is not a retrievable unit — the header row lands in one chunk and
        its values in another, which is exactly the shape that makes numeric queries miss."""
        rows = [["Item", "Cost"], *[[f"item{i}", str(i)] for i in range(200)]]
        big = " | ".join(f"Item: item{i} | Cost: {i}" for i in range(200))
        document = _doc([{"block_type": "table", "text": big, "table": rows}])
        chunks = chunk_document(document, target_words=20)

        assert len(chunks) == 1
        assert chunks[0].text == big


class TestNonContentBlocks:
    def test_running_heads_and_page_numbers_are_not_chunked(self):
        """They repeat on every page, so including them makes every chunk of a document
        look alike to an embedding model."""
        document = _doc(
            [
                {"block_type": "page_header", "text": "CONFIDENTIAL DRAFT", "page": 1},
                {"block_type": "paragraph", "text": _para(30), "page": 1},
                {"block_type": "page_footer", "text": "Page 1 of 9", "page": 1},
            ]
        )
        chunks = chunk_document(document, target_words=500)

        assert len(chunks) == 1
        assert "CONFIDENTIAL DRAFT" not in chunks[0].text
        assert "Page 1 of 9" not in chunks[0].text

    def test_they_remain_in_the_ir_so_the_viewer_can_still_render_them(self):
        document = _doc(
            [
                {"block_type": "page_header", "text": "CONFIDENTIAL DRAFT", "page": 1},
                {"block_type": "paragraph", "text": _para(30), "page": 1},
            ]
        )
        assert any(b.type in NON_CONTENT_BLOCKS for b in document.blocks)
        assert "CONFIDENTIAL DRAFT" in document.text

    def test_a_heading_with_no_body_does_not_become_a_chunk_on_its_own(self):
        """A 3-word chunk's embedding is dominated by a section number, which then
        outranks the section's real content."""
        document = _doc([{"block_type": "heading", "text": "Appendix A", "level": 1}])
        assert chunk_document(document, target_words=500) == []

    def test_a_heading_with_a_body_does_become_a_chunk(self):
        """The negative control: the rule above must not swallow real content."""
        document = _doc(
            [
                {"block_type": "heading", "text": "Appendix A", "level": 1},
                {"block_type": "paragraph", "text": _para(MIN_CHUNK_WORDS + 5)},
            ]
        )
        assert len(chunk_document(document, target_words=500)) == 1


class TestPageAttribution:
    def test_a_chunk_reports_the_first_page_it_touches(self):
        """A citation should land the reader where the passage begins, not where it ends."""
        document = _doc(
            [
                {"block_type": "paragraph", "text": _para(40), "page": 7},
                {"block_type": "paragraph", "text": _para(40), "page": 8},
            ]
        )
        chunks = chunk_document(document, target_words=500)
        assert len(chunks) == 1
        assert chunks[0].page == 7

    def test_an_unpaginated_format_yields_no_page_rather_than_zero(self):
        """``page=0`` would deep-link a Markdown file to a page it does not have — the
        same class of bug as a ``start_time=0`` citation into a document."""
        document = _doc([{"block_type": "paragraph", "text": _para(30)}])
        chunks = chunk_document(document, target_words=500)
        assert chunks[0].page is None


class TestTheRowShape:
    def test_to_row_carries_everything_the_storage_layer_needs(self):
        document = _doc(
            [
                {"block_type": "heading", "text": "1 Scope", "level": 1},
                {"block_type": "paragraph", "text": _para(30), "page": 2},
            ]
        )
        row = chunk_document(document, target_words=500)[0].to_row()

        assert set(row) == {
            "chunk_index",
            "text",
            "char_start",
            "char_end",
            "page",
            "section_path",
            "block_types",
        }
        assert row["block_types"] == ["heading", "paragraph"]


@pytest.mark.skipif(
    not (DOCS / "docling-fixtures").is_dir(),
    reason=(
        "$RAG_EVAL_DATA_DIR/documents not present. Synthetic blocks cannot falsify a "
        "chunker; a 40-page contract can. Fetch with scripts/fetch-rag-eval-data.sh."
    ),
)
class TestAgainstRealParsedDocuments:
    """End to end: real bytes → parser → IR → chunks, with the invariant asserted."""

    @pytest.fixture(scope="class")
    def parsed(self):
        from app.services.documents.backends.docling_slim import DoclingSlimParser

        parser = DoclingSlimParser()
        paths = [
            *sorted((DOCS / "pmc-oa" / "pdf").glob("*.pdf"))[:8],
            *sorted((DOCS / "contractnli" / "contract-nli" / "raw").glob("*.pdf"))[:8],
            *sorted(
                (DOCS / "docling-fixtures" / "tests" / "data" / "docx" / "sources").glob("*.docx")
            )[:8],
        ]
        out = []
        skipped: list[str] = []
        for path in paths:
            data = path.read_bytes()
            mime = detect_document_mime(path.name, data[:512], data)
            if mime is None:
                continue
            try:
                out.append(
                    (
                        path.name,
                        parser.parse(
                            ParseSource(filename=path.name, mime=mime, data=data),
                            options=ParseOptions(max_pages=60),
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001, S112 - coverage is the backends suite's job
                # Not swallowed: the count is asserted below, so a corpus that stopped
                # parsing would fail this fixture rather than shrink it silently.
                skipped.append(f"{path.name}: {type(exc).__name__}: {exc}"[:80])
                continue
        assert len(skipped) <= 4, f"{len(skipped)} of {len(paths)} failed to parse: {skipped[:5]}"
        return out

    def test_the_offset_invariant_holds_on_every_chunk_of_every_document(self, parsed):
        assert len(parsed) >= 10, f"only {len(parsed)} real documents parsed"

        total_chunks = 0
        broken: list[str] = []
        for name, document in parsed:
            for chunk in chunk_document(document):
                total_chunks += 1
                if document.text[chunk.char_start : chunk.char_end] != chunk.text:
                    broken.append(f"{name}#{chunk.chunk_index}")
        assert total_chunks >= 50, f"only {total_chunks} chunks over {len(parsed)} documents"
        assert not broken, f"{len(broken)} chunks do not slice back: {broken[:5]}"

    def test_no_real_document_chunks_to_nothing(self, parsed):
        """The failure ``docling#3335`` produces upstream: zero chunks, reported as success."""
        empty = [name for name, document in parsed if not chunk_document(document)]
        assert not empty, f"{len(empty)} real documents produced zero chunks: {empty[:5]}"

    def test_chunk_sizes_cluster_near_the_transcript_target(self, parsed):
        """Heterogeneous chunk lengths across the two planes distort RRF, which fuses them
        inside one query. Tables are excluded — they are deliberately un-split.

        This assertion is what caught the defect it now guards. Before long blocks were
        split at sentence boundaries, **69 of 126** non-table chunks were over 3x the
        200-word target: a pypdfium2 page with no blank lines arrives as ONE paragraph
        block of 800-1,200 words, and the block-boundary chunker had no way to cut inside
        it. Measured after the fix over the same 24 documents: 476 chunks, median 187
        words, p90 200, p99 382, max 545 — **0 over 3x, 2 over 2x**.
        """
        from app.core.config import settings

        target = int(settings.SEARCH_CHUNK_TARGET_WORDS)
        sizes = [
            len(chunk.text.split())
            for _, document in parsed
            for chunk in chunk_document(document)
            if chunk.block_types != ["table"]
        ]
        assert len(sizes) >= 200, f"only {len(sizes)} chunks — the sample got thinner"

        oversized = [s for s in sizes if s > target * 2]
        assert len(oversized) / len(sizes) < 0.02, (
            f"{len(oversized)}/{len(sizes)} non-table chunks are over 2x the {target}-word "
            f"target (measured: 2/476). Either the long-block split stopped firing or the "
            f"section-break rule did."
        )
        assert max(sizes) <= target * 4, (
            f"a {max(sizes)}-word chunk got through; the largest measured is 545"
        )

    def test_long_blocks_are_actually_being_split(self, parsed):
        """The negative control for the assertion above.

        A chunker that emitted one chunk per document would trivially satisfy "few chunks
        are oversized" if the documents were short. This asserts the split is doing work:
        real PDFs must produce many more chunks than they have blocks over the target.
        """
        oversized_blocks = sum(
            1
            for _, document in parsed
            for block in document.blocks
            if len(block.text.split()) > 400
        )
        assert oversized_blocks >= 5, (
            f"only {oversized_blocks} over-long blocks in the sample — this test is not "
            f"exercising the split path at all"
        )
        chunks = sum(len(chunk_document(document)) for _, document in parsed)
        assert chunks > oversized_blocks * 2
