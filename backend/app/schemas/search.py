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
        default_factory=list, description="Match sources: content, title, speaker, semantic"
    )
    relevance_percent: int = Field(0, description="Relevance confidence 0-100 for display")


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


class SuggestionItemSchema(BaseModel):
    """Auto-complete suggestion item."""

    type: str = Field(..., description="Suggestion type: title, speaker, or content")
    text: str = Field(..., description="Suggestion text")
    file_uuid: str | None = Field(None, description="File UUID if type is title")
    count: int | None = Field(None, description="Match count if applicable")


class FilterOptionsSchema(BaseModel):
    """Available filter options for search."""

    speakers: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[dict[str, Any]] = Field(default_factory=list)
    date_range: dict[str, Any] = Field(default_factory=dict)


class ReindexRequestSchema(BaseModel):
    """Request to trigger re-indexing."""

    file_uuids: list[str] | None = Field(
        None, description="Specific file UUIDs to re-index (None = all)"
    )


class ReindexStatusSchema(BaseModel):
    """Re-indexing status response."""

    total_files: int = Field(0)
    indexed_files: int = Field(0)
    pending_files: int = Field(0)
    in_progress: bool = Field(False)
    current_model: str = Field("")
    current_dimension: int = Field(0)
    last_indexed_at: str | None = Field(None)


class EmbeddingModelSchema(BaseModel):
    """Embedding model info."""

    model_id: str
    name: str
    dimension: int
    description: str
    size_mb: int


class EmbeddingModelsResponseSchema(BaseModel):
    """Available embedding models response."""

    models: list[EmbeddingModelSchema]
    current_model_id: str
    current_dimension: int


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


class NeuralModelsResponseSchema(BaseModel):
    """List of available neural models."""

    models: list[NeuralModelInfoSchema] = Field(default_factory=list)
    active_model: str | None = Field(None, description="Currently active model name")
    neural_search_enabled: bool = Field(False, description="Whether neural search is enabled")


class NeuralModelStatusSchema(BaseModel):
    """Neural search status."""

    enabled: bool = Field(False, description="Whether neural search is enabled in config")
    available: bool = Field(False, description="Whether neural search is currently available")
    active_model_name: str | None = Field(None, description="Active model name")
    active_model_id: str | None = Field(None, description="OpenSearch model ID")
    pipeline_configured: bool = Field(False, description="Whether ingest pipeline is configured")
    ml_commons_configured: bool = Field(
        False, description="Whether ML Commons settings are configured"
    )


class SetActiveNeuralModelSchema(BaseModel):
    """Request to set active neural model."""

    model_name: str = Field(..., description="Model name to activate")
    reindex: bool = Field(True, description="Whether to trigger reindex after model change")


# Result-type union (issue #462) — one parameter, extended by later lanes.
# "documents" (issue #463) searches the document-chunk plane and returns
# `document_results` / `document_total`, same shape convention as the
# `summary_results` / `summary_total` pair below. Do NOT add a second
# `include_documents`-style flag next to this one — see
# `api/endpoints/search.py::search_transcripts`.
SEARCH_RESULT_TYPES = ("transcripts", "summaries", "documents", "all")


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


class SummaryHitSchema(BaseModel):
    """A file-level summary search result."""

    file_uuid: str = Field(..., description="File UUID")
    file_id: int = Field(..., description="File integer ID")
    title: str = Field("", description="File title (falls back to filename)")
    matches: list[SummarySectionMatchSchema] = Field(
        default_factory=list, description="Matching sections, in document order"
    )


class DocumentChunkMatchSchema(BaseModel):
    """One matching chunk inside a document search result (issue #463)."""

    chunk_index: int = Field(0, description="Chunk index within the document")
    page: int | None = Field(None, description="1-based page number, when the source has pages")
    section_path: list[str] = Field(
        default_factory=list, description="Heading breadcrumb the chunk falls under"
    )
    snippet: str = Field(
        "", description="Matching text, masked under the reader's redaction policy"
    )
    score: float = Field(0.0, description="Relevance score")


class DocumentHitSchema(BaseModel):
    """A file-level document search result.

    ``file_id`` addresses ``Document.id`` here — a SEPARATE integer sequence
    from the ``MediaFile.id`` a transcript ``SearchHitSchema.file_id`` or a
    ``SummaryHitSchema.file_id`` addresses. The two can hold the same integer
    value for unrelated rows; never key a combined-results map by a bare
    ``file_id`` alone across result types. ``file_uuid`` is always unambiguous.
    """

    file_uuid: str = Field(..., description="Document UUID")
    file_id: int = Field(..., description="Document integer ID (Document.id)")
    title: str = Field("", description="Document filename")
    matches: list[DocumentChunkMatchSchema] = Field(
        default_factory=list, description="Matching chunks, best-scoring first"
    )
