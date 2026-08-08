"""Citation extraction for RAG chat (issue #52).

Citations are the feature's trust surface: they are what lets a user verify an
answer against the recording. They are built from OUR chunk data and only
*selected* by the model's ``[n]`` markers — never parsed out of its prose.
"""

from __future__ import annotations

from app.services.chat.citations import build_offered_citations
from app.services.chat.citations import extract_used_citations
from app.services.chat.redactor import MaskedChunk
from app.services.search.chunk_retrieval import ChunkHit


def _masked(index: int, content: str = "some transcript content") -> MaskedChunk:
    return MaskedChunk(
        source=ChunkHit(
            file_uuid=f"file-{index}",
            file_id=index,
            chunk_index=index,
            content=content,
            title=f"Recording {index}",
            speaker="Dana",
            start_time=float(index * 60),
            end_time=float(index * 60 + 30),
        ),
        content=content,
    )


def test_offered_citations_are_numbered_from_one():
    offered = build_offered_citations([_masked(0), _masked(1)])
    assert [c["id"] for c in offered] == [1, 2]


def test_offered_citation_carries_deep_link_fields():
    citation = build_offered_citations([_masked(3)])[0]
    assert citation["file_uuid"] == "file-3"
    assert citation["start_time"] == 180.0
    assert citation["speaker"] == "Dana"
    assert citation["title"] == "Recording 3"


def test_snippet_uses_masked_text():
    """Whatever the LLM saw is what the user sees in the citation card."""
    citation = build_offered_citations([_masked(0, "my number is [PHONE]")])[0]
    assert citation["snippet"] == "my number is [PHONE]"


def test_snippet_is_truncated_on_a_word_boundary():
    citation = build_offered_citations([_masked(0, "word " * 200)])[0]
    assert len(citation["snippet"]) <= 245
    assert citation["snippet"].endswith("…")
    assert "wor…" not in citation["snippet"]


def test_only_referenced_citations_are_returned():
    offered = build_offered_citations([_masked(i) for i in range(4)])
    used = extract_used_citations("The team agreed [2] and shipped [4].", offered)
    assert [c["id"] for c in used] == [2, 4]


def test_citations_are_ordered_by_first_mention():
    offered = build_offered_citations([_masked(i) for i in range(4)])
    used = extract_used_citations("First [3], then [1], and again [3].", offered)
    assert [c["id"] for c in used] == [3, 1]


def test_hallucinated_citation_numbers_are_dropped():
    """The model inventing [9] when 3 excerpts existed must not break the UI."""
    offered = build_offered_citations([_masked(i) for i in range(3)])
    used = extract_used_citations("As shown [9] and [2].", offered)
    assert [c["id"] for c in used] == [2]


def test_answer_with_no_markers_yields_no_citations():
    offered = build_offered_citations([_masked(0)])
    assert extract_used_citations("I could not find that in the transcripts.", offered) == []


def test_no_offered_chunks_yields_no_citations():
    assert extract_used_citations("Something [1]", []) == []


def test_bracketed_non_citations_are_ignored():
    offered = build_offered_citations([_masked(0)])
    used = extract_used_citations("They said [inaudible] and [crosstalk].", offered)
    assert used == []
