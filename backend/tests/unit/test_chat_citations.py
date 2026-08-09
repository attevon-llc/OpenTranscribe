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


# ---------------------------------------------------------------------------
# Citations must describe the prompt, not the retrieval result (issue #384)
# ---------------------------------------------------------------------------


def test_offered_citations_are_limited_to_the_excerpts_that_reached_the_prompt():
    """The excerpt budget can drop retrieved chunks; citations must drop with them.

    Offering a citation for a chunk the model was never given presents the answer
    as sourced when it is not — the "fabricated sources" failure mode.
    """
    chunks = [_masked(i) for i in range(5)]
    offered = build_offered_citations(chunks, [1, 2])

    assert [c["id"] for c in offered] == [1, 2]
    assert [c["file_uuid"] for c in offered] == ["file-0", "file-1"]


def test_offered_citations_follow_excerpt_ids_not_list_position():
    """Ids are assigned over the INPUT list, so a skipped chunk leaves a gap.

    A citation card must deep-link to the recording behind the id the model was
    shown, not to whatever happens to sit at that position in the offered list.
    """
    chunks = [_masked(i) for i in range(5)]
    offered = build_offered_citations(chunks, [2, 4])

    assert [c["id"] for c in offered] == [2, 4]
    assert [c["file_uuid"] for c in offered] == ["file-1", "file-3"]


def test_no_excerpts_reaching_the_prompt_offers_no_citations():
    chunks = [_masked(i) for i in range(3)]
    assert build_offered_citations(chunks, []) == []


def test_out_of_range_excerpt_ids_are_ignored():
    """Defensive: an id with no chunk behind it must never index out of bounds."""
    chunks = [_masked(0)]
    assert [c["id"] for c in build_offered_citations(chunks, [1, 7, 0])] == [1]


def test_extracted_citations_cannot_exceed_what_was_offered():
    """A model citing [3] when 2 excerpts were sent gets that marker dropped."""
    offered = build_offered_citations([_masked(i) for i in range(5)], [1, 2])
    used = extract_used_citations("Per [1] and [3] and [5].", offered)

    assert [c["id"] for c in used] == [1]
