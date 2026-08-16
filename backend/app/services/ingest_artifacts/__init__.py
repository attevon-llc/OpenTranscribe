"""Deterministic ingest artifacts — the no-LLM tier (#383 Phase 2 / #403 Stage 2).

Public surface. Everything here runs on a CPU worker with ``LLM_PROVIDER`` empty, touches
only Postgres, and is reproducible: the same transcript produces byte-identical JSONB
every time. See ``CLAUDE.md`` in this directory for the why.
"""

from .digest import build_digest
from .digest import digest_text
from .facts import build_facts
from .index_mapping import DOC_TYPE_DIGEST
from .index_mapping import DOC_TYPE_FIELD
from .index_mapping import TARGET_INDEX_VERSION
from .index_mapping import build_digest_documents
from .index_mapping import chunk_plane_clause
from .keyphrases import extract_keyphrases
from .provenance import char_range_provenance
from .provenance import segment_provenance
from .provenance import validate_provenance
from .recorded_date import DateCandidate
from .recorded_date import Resolution
from .recorded_date import resolve as resolve_recorded_date
from .recorded_date_service import apply_resolution
from .recorded_date_service import resolve_for_file as resolve_recorded_date_for_file
from .recorded_date_service import set_manual_date
from .service import GENERATOR_VERSION
from .service import build_artifacts
from .service import generate_file_artifacts
from .service import load_ordered_segments
from .service import source_fingerprint

__all__ = [
    "DOC_TYPE_DIGEST",
    "DOC_TYPE_FIELD",
    "GENERATOR_VERSION",
    "TARGET_INDEX_VERSION",
    "DateCandidate",
    "Resolution",
    "apply_resolution",
    "build_artifacts",
    "build_digest",
    "build_digest_documents",
    "build_facts",
    "char_range_provenance",
    "chunk_plane_clause",
    "digest_text",
    "extract_keyphrases",
    "generate_file_artifacts",
    "load_ordered_segments",
    "resolve_recorded_date",
    "resolve_recorded_date_for_file",
    "segment_provenance",
    "set_manual_date",
    "source_fingerprint",
    "validate_provenance",
]
