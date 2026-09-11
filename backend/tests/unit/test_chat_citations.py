"""Citation extraction for RAG chat (issue #52).

Citations are the feature's trust surface: they are what lets a user verify an
answer against the recording. They are built from OUR chunk data and only
*selected* by the model's ``[n]`` markers — never parsed out of its prose.
"""

from __future__ import annotations

from app.services.chat.citations import DIGEST_SNIPPET_CHARS
from app.services.chat.citations import OVERVIEW_SNIPPET_CHARS
from app.services.chat.citations import SNIPPET_CHARS
from app.services.chat.citations import build_offered_citations
from app.services.chat.citations import build_overview_citations
from app.services.chat.citations import extract_used_citations
from app.services.chat.mapreduce.file_summaries import FileSummary
from app.services.chat.redactor import MaskedChunk
from app.services.ingest_artifacts.sizing import DIGEST_SECTION_MAX_WORDS
from app.services.search.chunk_retrieval import ChunkHit


def _masked(
    index: int,
    content: str = "some transcript content",
    *,
    expanded: bool = False,
    digest_section: int | None = None,
) -> MaskedChunk:
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
            expanded=expanded,
            digest_section=digest_section,
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


def test_unexpanded_citation_has_expanded_false_and_todays_snippet_cap():
    """Control (issue #526): a chunk ``context_expansion`` never touched is
    byte-identical to before this issue — same truncation length, no new
    ``expanded`` behaviour."""
    long_content = "word " * 200
    citation = build_offered_citations([_masked(0, long_content, expanded=False)])[0]
    assert citation["expanded"] is False
    assert len(citation["snippet"]) <= 245


def test_expanded_citation_is_marked_and_not_truncated_at_the_ordinary_limit():
    """The #526 reproduction: a citation whose ``chunk_index`` still names the
    original (short) indexed chunk must say so via ``expanded``, and the
    snippet must cover the FULL widened+masked text the model was given —
    not silently cut back down to the ordinary ~240-char cap, which would
    hide from the reader exactly the part of the excerpt a quote could live
    in beyond that boundary.

    RED before #526: ``build_citation`` had no ``expanded`` key at all (a
    ``KeyError`` on the assertion below) and truncated every citation's
    snippet to :data:`SNIPPET_CHARS` regardless of how the chunk got its text.
    """
    wide_content = "context before the turn. " * 20 + "the actual quoted material lives here."
    assert len(wide_content) > 240  # otherwise this test doesn't exercise truncation at all

    citation = build_offered_citations([_masked(0, wide_content, expanded=True)])[0]

    assert citation["expanded"] is True
    assert citation["snippet"].rstrip("…") == wide_content
    assert "the actual quoted material lives here." in citation["snippet"]


def test_digest_citation_never_reports_expanded_even_if_the_source_says_so():
    """Defensive: ``context_expansion`` never touches a digest hit (it excludes
    digests by construction — see ``needs_expansion``), so
    a digest citation must report ``expanded: False`` regardless of whatever
    the underlying ``ChunkHit.expanded`` happens to hold, rather than trusting
    a field that should never be set for this kind."""
    citation = build_offered_citations(
        [_masked(0, "a summary section", expanded=True, digest_section=2)]
    )[0]
    assert citation["kind"] == "digest"
    assert citation["expanded"] is False


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


# ---------------------------------------------------------------------------
# Per-kind snippet caps + content_chars (issue #832)
# ---------------------------------------------------------------------------


def test_a_digest_citation_at_the_ingest_word_bound_is_not_truncated():
    """A digest section is bounded at ingest to DIGEST_SECTION_MAX_WORDS words —
    exactly at that bound, the citation must show the whole thing.

    RED before #832: every digest citation was capped at the plain 240-char
    SNIPPET_CHARS, and a DIGEST_SECTION_MAX_WORDS-word section is always well
    past 240 chars, so this always truncated.
    """
    content = " ".join(["word"] * DIGEST_SECTION_MAX_WORDS)
    citation = build_offered_citations([_masked(0, content, digest_section=1)])[0]

    assert citation["kind"] == "digest"
    assert citation["snippet"] == content
    assert not citation["snippet"].endswith("…")


def test_a_digest_citation_longer_than_its_cap_is_still_truncated():
    """Control: DIGEST_SNIPPET_CHARS is a real cap, not 'no cap at all'."""
    content = "word " * 300
    assert len(content) > DIGEST_SNIPPET_CHARS

    citation = build_offered_citations([_masked(0, content, digest_section=1)])[0]

    assert citation["snippet"].endswith("…")
    assert len(citation["snippet"]) <= DIGEST_SNIPPET_CHARS + 5


def test_an_ordinary_chunk_citations_snippet_is_unchanged():
    """Regression guard: an ordinary (non-digest, non-expanded) chunk citation
    stays capped at the plain SNIPPET_CHARS, not the wider digest cap — the
    fix must not accidentally widen the chunk plane too."""
    long_content = "word " * 200
    assert len(long_content) > DIGEST_SNIPPET_CHARS  # exceeds BOTH caps

    citation = build_offered_citations([_masked(0, long_content)])[0]

    assert citation["kind"] == "chunk"
    assert citation["snippet"].endswith("…")
    assert len(citation["snippet"]) <= SNIPPET_CHARS + 5


def test_content_chars_is_the_pre_truncation_length():
    """content_chars is present, and equal to the pre-truncation length, for a
    chunk, a digest, AND an overview citation — even when the snippet was NOT
    truncated (a diagnostic that only appears on failure can't compute a rate)."""
    chunk_content = "short chunk text here"
    chunk_citation = build_offered_citations([_masked(0, chunk_content)])[0]
    assert chunk_citation["content_chars"] == len(chunk_content)
    assert not chunk_citation["snippet"].endswith("…")

    digest_content = "short digest text here"
    digest_citation = build_offered_citations([_masked(0, digest_content, digest_section=1)])[0]
    assert digest_citation["content_chars"] == len(digest_content)
    assert not digest_citation["snippet"].endswith("…")

    overview_content = "short overview text"
    summary = FileSummary(file_uuid="file-x", title="Recording X", digest=overview_content)
    overview_citation = build_overview_citations(((1, "file-x"),), [summary])[0]
    assert overview_citation["content_chars"] == len(overview_content)
    assert not overview_citation["snippet"].endswith("…")


def test_overview_citations_use_the_wider_cap():
    """An overview citation spans multiple joined digest sections
    (mapreduce.overview.sections_budget()'s ceiling of 3 per file), so it needs
    OVERVIEW_SNIPPET_CHARS — not the single-section DIGEST_SNIPPET_CHARS."""
    long_digest = "word " * 500
    assert len(long_digest) > OVERVIEW_SNIPPET_CHARS  # forces truncation somewhere

    summary = FileSummary(file_uuid="file-x", title="Recording X", digest=long_digest)
    citation = build_overview_citations(((1, "file-x"),), [summary])[0]

    assert citation["snippet"].endswith("…")
    # Proves it used the WIDER cap, not the single-section one.
    assert len(citation["snippet"]) > DIGEST_SNIPPET_CHARS
    assert len(citation["snippet"]) <= OVERVIEW_SNIPPET_CHARS + 5
