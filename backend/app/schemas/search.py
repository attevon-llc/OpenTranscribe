"""Pydantic schemas for search API responses."""

from typing import Any

from pydantic import BaseModel
from pydantic import Field


class SearchOccurrenceSchema(BaseModel):
    """A single matching snippet within a file."""

    snippet: str = Field(..., description="Highlighted text snippet")
    speaker: str = Field("", description="Speaker name for this segment")
    start_time: float = Field(0.0, description="Start time in seconds")
    end_time: float = Field(0.0, description="End time in seconds")
    chunk_index: int = Field(0, description="Chunk index within the file")
    score: float = Field(0.0, description="Relevance score")
    match_type: str = Field("content", description="Match type: content, title, or speaker")
    has_keyword_match: bool = Field(True, description="False for semantic-only hits")
    highlight_type: str = Field("keyword", description="Highlight type: keyword or semantic")
    similar_words: list[str] = Field(
        default_factory=list,
        description="Semantically similar words to highlight (for semantic matches)",
    )


class SearchHitSchema(BaseModel):
    """A file-level search result with multiple occurrences."""

    file_uuid: str = Field(..., description="File UUID")
    file_id: int = Field(..., description="File integer ID")
    title: str = Field("", description="File title")
    speakers: list[str] = Field(default_factory=list, description="All speakers in file")
    tags: list[str] = Field(default_factory=list, description="Tags on the file")
    upload_time: str = Field("", description="Upload timestamp ISO string")
    language: str = Field("", description="Language code")
    content_type: str = Field("", description="MIME content type (e.g. audio/mpeg, video/mp4)")
    relevance_score: float = Field(0.0, description="Best relevance score")
    occurrences: list[SearchOccurrenceSchema] = Field(
        default_factory=list, description="Matching snippets"
    )
    total_occurrences: int = Field(0, description="Total match count in file")
    title_highlighted: str = Field("", description="Title with highlight marks if matched")
    keyword_occurrences: int = Field(0, description="Count of keyword-matched occurrences")
    semantic_only: bool = Field(False, description="True if only semantic matches, no keywords")
    semantic_confidence: str = Field("", description="Semantic confidence: '', 'high', or 'low'")
    match_sources: list[str] = Field(
        default_factory=list,
        description=(
            "Match sources: content, title, speaker, semantic, metadata_speaker. "
            "'semantic' and 'metadata_speaker' are not members of the request-side "
            "`sources` vocabulary — semantic hits ride with `content` and "
            "`metadata_speaker` is a `speaker`-pill enrichment."
        ),
    )
    relevance_percent: int = Field(0, description="Relevance confidence 0-100 for display")


# Source union (issue #760) — the REQUEST vocabulary for which fields/legs a
# search targets, and the same words `SearchHitSchema.match_sources` reports
# back. One parameter, closed vocabulary; the `include_*` prohibition on
# `SEARCH_RESULT_TYPES` below applies to this one too.
#
# `content`/`title`/`speaker` are FIELDS in the transcript leg's one query;
# `summary` is a separate OpenSearch leg (issue #963 rebuilt it onto the same
# index/RRF machinery as the transcript leg — it is no longer a slow Postgres
# FTS query, so unlike the original #760 plan it is included in the default
# selection below). Three field selectors and a leg, not four peers.
SEARCH_SOURCES = ("content", "title", "speaker", "summary")

#: Legacy `result_type` → source set. `transcripts` reproduces
#: `_get_search_fields`'s historical field list exactly, for byte-identical
#: back-compat when `sources` is not supplied.
RESULT_TYPE_TO_SOURCES: dict[str, frozenset[str]] = {
    "transcripts": frozenset({"content", "title", "speaker"}),
    "summaries": frozenset({"summary"}),
    "all": frozenset(SEARCH_SOURCES),
}


class SourceCountsSchema(BaseModel):
    """Corpus-wide matching counts per source, for the toggle labels (issue #760).

    Independent of which sources are selected — a disabled pill still shows
    what it would return. `None` means the count could not be computed;
    render NO number, never `0` — "0 matches" and "the count failed" must
    never be indistinguishable.
    """

    content: int | None = None
    title: int | None = None
    speaker: int | None = None
    summary: int | None = None


class SearchResponseSchema(BaseModel):
    """Complete search response."""

    query: str = Field(..., description="Original search query")
    results: list[SearchHitSchema] = Field(default_factory=list)
    total_results: int = Field(0, description="Total matching snippets")
    total_files: int = Field(0, description="Total matching files")
    page: int = Field(1, description="Current page number")
    page_size: int = Field(20, description="Results per page")
    total_pages: int = Field(0, description="Total number of pages")
    search_time_ms: float = Field(0.0, description="Search execution time in ms")
    filters_applied: dict[str, Any] = Field(default_factory=dict, description="Active filters")
    search_mode: str = Field("hybrid", description="Search mode: hybrid or keyword")
    sources: list[str] | None = Field(
        None, description="The resolved source set (issue #760), only when `sources` was sent."
    )
    source_counts: SourceCountsSchema | None = Field(
        None, description="Corpus-wide per-source counts; see SourceCountsSchema."
    )
    summary_unavailable: str | None = Field(
        None,
        description=(
            "Machine-readable reason the summary leg returned nothing despite being "
            "requested, e.g. 'masking_unavailable'. An absent/empty `summary_results` "
            "with this key set must NEVER render as 'no summaries matched'."
        ),
    )


class EmbeddingModelSchema(BaseModel):
    """Embedding model info."""

    model_id: str
    name: str
    dimension: int
    description: str
    size_mb: int


class SetEmbeddingModelSchema(BaseModel):
    """Request to change embedding model."""

    model_id: str = Field(..., description="Model ID from the registry")


# Neural Search Model Schemas


class NeuralModelInfoSchema(BaseModel):
    """OpenSearch neural model info."""

    model_name: str = Field(
        ..., description="Model name (e.g., huggingface/sentence-transformers/...)"
    )
    display_name: str = Field(..., description="Human-readable model name")
    dimension: int = Field(..., description="Embedding dimension")
    size_mb: int = Field(0, description="Model size in MB")
    languages: list[str] = Field(default_factory=list, description="Supported languages")
    model_format: str = Field("TORCH_SCRIPT", description="Model format")
    is_default: bool = Field(False, description="Whether this is the default model")
    # OpenSearch status fields
    model_id: str | None = Field(None, description="OpenSearch model ID if registered")
    state: str = Field(
        "NOT_REGISTERED", description="Model state: NOT_REGISTERED, REGISTERED, DEPLOYING, DEPLOYED"
    )
    is_active: bool = Field(False, description="Whether this is the currently active model")


# Result-type union (issue #462) — one parameter, extended by later lanes.
# Do NOT add a second `include_*`-style flag next to this one — see
# `api/endpoints/search.py::search_transcripts`.
SEARCH_RESULT_TYPES = ("transcripts", "summaries", "all")


class SummarySectionMatchSchema(BaseModel):
    """One matching leaf inside a summary, addressable for scroll-to-section."""

    key_path: str = Field(
        ...,
        description=(
            "JSON key-path to the matching leaf in summary_data, e.g. "
            "'major_topics[0].key_points[2]'. Stable against the same summary_data "
            "shape the summary detail endpoint returns."
        ),
    )
    snippet: str = Field(
        ..., description="The matching leaf text, masked under the reader's policy"
    )
    score: float = Field(
        0.0,
        description=(
            "The OpenSearch RRF fusion score for this leaf (issue #963). Not comparable "
            "to the transcript leg's per-chunk scores or to summary_total's window "
            "semantics — an intra-leg relevance signal only."
        ),
    )


class SummaryHitSchema(BaseModel):
    """A file-level summary search result."""

    file_uuid: str = Field(..., description="File UUID")
    file_id: int = Field(..., description="File integer ID")
    title: str = Field("", description="File title (falls back to filename)")
    matches: list[SummarySectionMatchSchema] = Field(
        default_factory=list, description="Matching sections, ordered by relevance"
    )
