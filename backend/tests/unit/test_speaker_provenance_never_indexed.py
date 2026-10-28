"""The record of how an identity came to be never reaches a search index or a language model.

``identity_source_at`` / ``_user_id`` / ``_detail`` and ``suggestion_detail`` name the profile or
speaker a voice matched and the person who made an identity. They are readable through the
speaker API (after the privacy redaction) and nowhere else: not in an OpenSearch document, not in
a prompt. Withholding them while voiceprints are off is only possible because nothing else
copies them, so this fails on the first module on the index or LLM side that names one.
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

#: The columns of the record.
RECORD_COLUMNS = (
    "identity_source_at",
    "identity_source_user_id",
    "identity_source_detail",
    "suggestion_detail",
)
_PATTERN = re.compile(r"\b(" + "|".join(RECORD_COLUMNS) + r")\b")

#: Everything that builds an index document or a model prompt.
_GUARDED_DIRS = ("services/search", "services/opensearch_service", "services/chat")
_GUARDED_FILES = (
    "services/llm_service.py",
    "services/llm_stream.py",
    "services/topic_extraction_service.py",
    "services/formatting_service.py",
    "services/ingest_artifacts/service.py",
    "tasks/search_indexing_task.py",
    "tasks/reindex_task.py",
    "tasks/rename_propagation_task.py",
    "tasks/summarization.py",
    "tasks/topic_extraction.py",
    "tasks/speaker_identification_task.py",
    "utils/transcript_builders.py",
)


def mentions(source: str) -> list[str]:
    """Record columns ``source`` names."""
    return sorted(set(_PATTERN.findall(source)))


def _guarded() -> list[Path]:
    paths = [p for d in _GUARDED_DIRS for p in sorted((APP / d).rglob("*.py"))]
    paths += [APP / f for f in _GUARDED_FILES]
    return paths


def test_the_guard_covers_real_files():
    paths = _guarded()
    assert len(paths) > 20 and all(p.is_file() for p in paths), "a renamed module left the guard"


def test_no_index_or_prompt_builder_names_the_record():
    found = {
        p.relative_to(APP).as_posix(): mentions(p.read_text())
        for p in _guarded()
        if mentions(p.read_text())
    }
    assert not found, (
        "These modules build search documents or model prompts and name the provenance record "
        f"of a speaker's identity: {found}. It is served by the speaker API alone."
    )


def test_the_detector_fires_on_a_mention_and_stays_clean_otherwise():
    assert mentions('doc["identity_source_detail"] = speaker.identity_source_detail') == [
        "identity_source_detail"
    ]
    assert mentions("speaker.suggestion_detail") == ["suggestion_detail"]
    assert mentions("speaker.suggestion_source; identity_source; identity_source_atlas") == []
