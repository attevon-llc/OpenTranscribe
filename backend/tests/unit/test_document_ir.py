"""The IR offset invariant, and the ways a producer can break it.

``text[char_start:char_end] == block.text`` is what makes chat citations, viewer
highlights, ``char_range`` digest provenance and cached redaction spans address the same
coordinate system. If it can be violated silently, every one of those points at the wrong
passage — the silent-wrong-answer class, in its purest form.

Every negative case here was written by breaking a real IR and watching
:func:`validate_ir` stay green before the check existed.
"""

from __future__ import annotations

import pytest

from app.services.documents.ir import BLOCK_SEPARATOR
from app.services.documents.ir import IR_VERSION
from app.services.documents.ir import Block
from app.services.documents.ir import IRBuilder
from app.services.documents.ir import IRValidationError
from app.services.documents.ir import ParsedDocument
from app.services.documents.ir import validate_ir


def _doc(blocks: list[Block], text: str) -> ParsedDocument:
    return ParsedDocument(text=text, blocks=blocks, parser="test", parser_version="0")


class TestTheBuilderKeepsTheInvariant:
    def test_every_block_slices_back_to_its_own_text(self):
        builder = IRBuilder()
        builder.add("heading", "Introduction", level=1)
        builder.add("paragraph", "The first paragraph.")
        builder.add("list_item", "A bullet")
        doc = builder.build(parser="test", parser_version="0")

        assert len(doc.blocks) == 3, "the loop below proves nothing over an empty block list"
        for block in doc.blocks:
            assert doc.text[block.char_start : block.char_end] == block.text

    def test_offsets_account_for_the_separator_the_builder_inserts(self):
        """The off-by-`len(separator)` bug this class exists to make impossible."""
        builder = IRBuilder()
        builder.add("paragraph", "AAA")
        builder.add("paragraph", "BBB")
        doc = builder.build(parser="test", parser_version="0")

        assert doc.text == f"AAA{BLOCK_SEPARATOR}BBB"
        assert doc.blocks[1].char_start == 3 + len(BLOCK_SEPARATOR)

    def test_text_is_stripped_before_offsets_are_taken(self):
        """Storing untrimmed text and trimmed offsets is the same bug wearing a hat."""
        builder = IRBuilder()
        block = builder.add("paragraph", "   padded   ")
        doc = builder.build(parser="test", parser_version="0")

        assert block is not None
        assert block.text == "padded"
        assert doc.text[block.char_start : block.char_end] == "padded"

    def test_an_empty_block_is_dropped_rather_than_stored_as_a_zero_length_range(self):
        builder = IRBuilder()
        assert builder.add("paragraph", "   \n\t ") is None
        assert len(builder) == 0

    def test_an_unknown_block_type_is_refused_at_the_producer(self):
        with pytest.raises(IRValidationError, match="unknown block type"):
            IRBuilder().add("sidebar", "text")

    def test_an_unknown_source_is_refused(self):
        with pytest.raises(IRValidationError, match="unknown block source"):
            IRBuilder().add("paragraph", "text", source="hallucinated")


class TestSectionPathTracksTheHeadingStream:
    def test_a_deeper_heading_nests_and_a_shallower_one_pops(self):
        builder = IRBuilder()
        builder.add("heading", "1 Methods", level=1)
        builder.add("heading", "1.1 Data", level=2)
        deep = builder.add("paragraph", "under data")
        builder.add("heading", "2 Results", level=1)
        shallow = builder.add("paragraph", "under results")

        assert deep is not None and shallow is not None
        assert deep.section_path == ["1 Methods", "1.1 Data"]
        assert shallow.section_path == ["2 Results"]

    def test_a_heading_carries_the_path_above_it_not_including_itself(self):
        """Otherwise a breadcrumb prepended to indexed content repeats the heading."""
        builder = IRBuilder()
        builder.add("heading", "1 Methods", level=1)
        sub = builder.add("heading", "1.1 Data", level=2)

        assert sub is not None
        assert sub.section_path == ["1 Methods"]

    def test_a_sibling_heading_at_the_same_level_replaces_rather_than_nests(self):
        builder = IRBuilder()
        builder.add("heading", "A", level=2)
        builder.add("heading", "B", level=2)
        body = builder.add("paragraph", "text")

        assert body is not None
        assert body.section_path == ["B"]


class TestValidateIrCatchesABrokenProducer:
    def test_a_block_whose_text_does_not_match_its_slice_is_rejected(self):
        doc = _doc([Block("paragraph", "WRONG", 0, 5)], "right")
        with pytest.raises(IRValidationError, match="but block.text is"):
            validate_ir(doc)

    def test_overlapping_blocks_are_rejected(self):
        text = "abcdef"
        doc = _doc(
            [Block("paragraph", "abcd", 0, 4), Block("paragraph", "cdef", 2, 6)],
            text,
        )
        with pytest.raises(IRValidationError, match="overlapping"):
            validate_ir(doc)

    def test_an_offset_past_the_end_of_the_text_is_rejected(self):
        doc = _doc([Block("paragraph", "abc", 0, 99)], "abc")
        with pytest.raises(IRValidationError, match="outside text"):
            validate_ir(doc)

    def test_a_reversed_range_is_rejected(self):
        doc = _doc([Block("paragraph", "", 4, 2)], "abcdef")
        with pytest.raises(IRValidationError, match="char_start 4 > char_end 2"):
            validate_ir(doc)

    def test_a_stale_ir_version_is_rejected(self):
        doc = _doc([], "")
        doc.ir_version = IR_VERSION + 1
        with pytest.raises(IRValidationError, match="ir_version"):
            validate_ir(doc)

    def test_a_confidence_outside_zero_to_one_is_rejected(self):
        doc = _doc([Block("paragraph", "abc", 0, 3, source="ocr", confidence=1.4)], "abc")
        with pytest.raises(IRValidationError, match="confidence"):
            validate_ir(doc)

    def test_more_ocr_pages_than_pages_is_rejected(self):
        """A coverage number above 100 % means the counter and the parser disagree."""
        doc = _doc([], "")
        doc.page_count = 3
        doc.ocr_pages = 5
        with pytest.raises(IRValidationError, match="exceeds page_count"):
            validate_ir(doc)

    def test_a_well_formed_ir_passes(self):
        """The negative control: without it every assertion above could be vacuous.

        A `validate_ir` that raised unconditionally would satisfy every test above.
        """
        builder = IRBuilder()
        builder.add("heading", "Title", level=1)
        builder.add("paragraph", "Body text.")
        doc = builder.build(parser="test", parser_version="0")

        validate_ir(doc)  # must not raise; a validator that always raised would
        # satisfy every negative case above
        assert len(doc.blocks) == 2
        assert doc.text == "Title\n\nBody text."


class TestTheArtifactRoundTrip:
    def test_to_dict_from_dict_preserves_every_offset_and_the_invariant(self):
        builder = IRBuilder()
        builder.add("heading", "H", level=2)
        builder.add("table", "a: 1", table=[["a"], ["1"]], page=3)
        builder.add("paragraph", "OCR'd", source="ocr", confidence=0.8, page=3)
        original = builder.build(
            parser="docling.slim",
            parser_version="2.119.0",
            page_count=3,
            ocr_applied=True,
            ocr_pages=1,
            warnings=["something degraded"],
        )

        restored = ParsedDocument.from_dict(original.to_dict())

        validate_ir(restored)
        assert restored.text == original.text
        assert restored.warnings == ["something degraded"]
        assert restored.ocr_pages == 1
        assert [b.table for b in restored.blocks] == [b.table for b in original.blocks]
        assert [b.section_path for b in restored.blocks] == [
            b.section_path for b in original.blocks
        ]
        assert [(b.char_start, b.char_end) for b in restored.blocks] == [
            (b.char_start, b.char_end) for b in original.blocks
        ]

    def test_the_serialised_block_drops_defaults_but_never_an_offset(self):
        builder = IRBuilder()
        block = builder.add("paragraph", "plain")
        builder.build(parser="t", parser_version="0")

        assert block is not None
        payload = block.to_dict()
        assert "char_start" in payload and "char_end" in payload
        assert "confidence" not in payload
        assert "source" not in payload  # the "text" default
