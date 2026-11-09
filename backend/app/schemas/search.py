"""Pydantic schemas for search API responses."""

from pydantic import BaseModel
from pydantic import Field

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


class SetEmbeddingModelSchema(BaseModel):
    """Request to change embedding model."""

    model_id: str = Field(..., description="Model ID from the registry")


# Neural Search Model Schemas


# Result-type union (issue #462) — one parameter, extended by later lanes.
# Do NOT add a second `include_*`-style flag next to this one — see
# `api/endpoints/search.py::search_transcripts`.
SEARCH_RESULT_TYPES = ("transcripts", "summaries", "all")
