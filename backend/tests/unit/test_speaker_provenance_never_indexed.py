"""The record of how an identity came to be never reaches a search index or a language model.

``identity_source_at`` / ``_user_id`` / ``_detail`` and ``suggestion_detail`` name the profile or
speaker a voice matched and the person who made an identity. They are readable through the
speaker API (after the privacy redaction) and nowhere else: not in an OpenSearch document, not in
a prompt. Withholding them while voiceprints are off is only possible because nothing else
copies them, so this fails on the first module on the index or LLM side that names one.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

#: The columns of the record.
RECORD_COLUMNS = (
    "identity_source_at",
    "identity_source_user_id",
    "identity_source_detail",
    "suggestion_detail",
)
#: The attributes ``speaker_provenance_view`` hangs on a row: they CARRY the record (and the
#: person), so reading one is reading the record by another name.
ATTACHED_ATTRIBUTES = ("identity", "suggestion")
_NAMES = frozenset(RECORD_COLUMNS) | frozenset(ATTACHED_ATTRIBUTES)

#: Everything that builds an index document or a model prompt, or answers on a surface that has
#: no identity block (an API response must not carry the record where the schema does not).
_GUARDED_DIRS = (
    "services/search",
    "services/opensearch_service",
    "services/chat",
    "api/endpoints/chat",
)
_GUARDED_FILES = (
    # model prompts
    "services/llm_service.py",
    "services/llm_stream.py",
    "services/topic_extraction_service.py",
    "services/formatting_service.py",
    "utils/transcript_builders.py",
    "utils/prompt_manager.py",
    "tasks/summarization.py",
    "tasks/topic_extraction.py",
    "tasks/speaker_identification_task.py",
    # index documents and the writers that rebuild them
    "services/ingest_artifacts/service.py",
    "tasks/search_indexing_task.py",
    "tasks/reindex_task.py",
    "tasks/rename_propagation_task.py",
    "tasks/embedding_migration_v4.py",
    "tasks/speaker_embedding_migration.py",
    "services/speaker_rename_tracker.py",
    # builds the cluster Inbox items: a response with no identity block
    "services/speaker_clustering_service.py",
)


def mentions(source: str) -> list[str]:
    """Names of the record (or of the attributes that carry it) that ``source`` USES.

    By syntax, not by text: an attribute read, a string handed to ``getattr`` or used as a key,
    a keyword argument. A docstring or a comment that talks about the record is not a use, and
    a longer identifier that merely contains a name (``identity_source_atlas``) is not the name.
    A keyword called ``identity`` / ``suggestion`` is some other function's parameter, not a
    read of the row's attribute, so only the record columns count as keywords.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in _NAMES:
            found.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in _NAMES:
                found.add(node.value)
        elif isinstance(node, ast.keyword) and node.arg in RECORD_COLUMNS:
            found.add(str(node.arg))
        elif isinstance(node, ast.Name) and node.id in RECORD_COLUMNS:
            found.add(node.id)
    return sorted(found)


def _guarded() -> list[Path]:
    paths = [p for d in _GUARDED_DIRS for p in sorted((APP / d).rglob("*.py"))]
    paths += [APP / f for f in _GUARDED_FILES]
    return paths


def test_the_guard_covers_real_files():
    paths = _guarded()
    assert len(paths) > 30 and all(p.is_file() for p in paths), "a renamed module left the guard"
    covered = {p.relative_to(APP).as_posix() for p in paths}
    for known in (
        "services/speaker_clustering_service.py",
        "tasks/embedding_migration_v4.py",
        "tasks/speaker_embedding_migration.py",
        "services/speaker_rename_tracker.py",
        "utils/prompt_manager.py",
        "api/endpoints/chat/messages.py",
    ):
        assert known in covered, f"{known} fell out of the guard"


def test_no_index_or_prompt_builder_names_the_record():
    found = {
        p.relative_to(APP).as_posix(): mentions(p.read_text())
        for p in _guarded()
        if mentions(p.read_text())
    }
    assert not found, (
        "These modules build search documents, model prompts or responses with no identity "
        f"block and name the provenance record of a speaker's identity: {found}. It is served by "
        "the speaker API's identity and suggestion objects alone."
    )


def test_the_detector_fires_on_a_use_and_stays_clean_otherwise():
    assert mentions('doc["identity_source_detail"] = speaker.identity_source_detail') == [
        "identity_source_detail"
    ]
    assert mentions("speaker.suggestion_detail") == ["suggestion_detail"]
    assert mentions("speaker.identity_source_at") == ["identity_source_at"]
    assert mentions("f(identity_source_user_id=1)") == ["identity_source_user_id"]
    # the attached objects carry the record by another name
    assert mentions('getattr(row, "identity", None)') == ["identity"]
    assert mentions("row.suggestion") == ["suggestion"]
    assert mentions('{"suggestion": s}') == ["suggestion"]
    # not uses
    assert mentions("speaker.suggestion_source; identity_source; identity_source_atlas") == []
    assert mentions('"""The identity_source_detail column is described here."""') == []
    assert mentions("# speaker.identity_source_detail") == []
    assert mentions("topic(suggestion=text)") == []


def test_a_record_placed_in_the_inbox_item_or_the_facts_mask_is_caught():
    """The two placements an earlier version of this guard missed (the file was not listed, and
    the attached ``identity`` object was not a name it knew)."""
    inbox = (APP / "services/speaker_clustering_service.py").read_text()
    facts = (APP / "services/ingest_artifacts/service.py").read_text()
    in_item = inbox + "\n\ndef _leak(speaker):\n    return {'d': speaker.identity_source_detail}\n"
    in_mask = facts + "\n\ndef _leak(row):\n    return getattr(row, 'identity', None)\n"

    assert mentions(inbox) == [] and mentions(facts) == [], "control: the real modules are clean"
    assert mentions(in_item) == ["identity_source_detail"]
    assert mentions(in_mask) == ["identity"]
