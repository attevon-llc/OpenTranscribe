"""Project scope inheritance (issue #360).

The property under test is the one that leaks the whole library when it is got
wrong: downstream, an EMPTY scope means "everything the caller can access",
while an explicitly-resolved-but-empty file list means "match nothing". Project
inheritance sits directly on top of that distinction.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.endpoints.chat.common import resolve_effective_scope

FILE_A = "019f294e-e5dd-7000-a43c-ce30604f0787"
FILE_B = "019f294f-2174-7000-81d7-3925c1e55285"


def conversation(**scope):
    """A stand-in exposing only what the resolver reads."""
    return SimpleNamespace(
        scope={
            "file_uuids": scope.get("file_uuids", []),
            "collection_uuids": scope.get("collection_uuids", []),
            "tag_names": scope.get("tag_names", []),
            "speakers": scope.get("speakers", []),
        }
    )


def project(**scope):
    default = {
        "file_uuids": scope.get("file_uuids", []),
        "collection_uuids": scope.get("collection_uuids", []),
        "tag_names": scope.get("tag_names", []),
        "speakers": scope.get("speakers", []),
    }
    has_scope = bool(default["file_uuids"] or default["collection_uuids"] or default["tag_names"])
    return SimpleNamespace(default_scope=default, has_scope=has_scope)


def test_ungrouped_conversation_is_unchanged():
    """The pre-#360 path: no project, scope passes through untouched."""
    result = resolve_effective_scope(conversation(file_uuids=[FILE_A]), None)
    assert result.file_uuids == [FILE_A]


def test_empty_scope_with_no_project_stays_empty():
    """Empty must stay empty — downstream that means 'all accessible'."""
    result = resolve_effective_scope(conversation(), None)
    assert result.is_empty


def test_empty_conversation_inherits_the_project_scope():
    result = resolve_effective_scope(conversation(), project(file_uuids=[FILE_A, FILE_B]))
    assert result.file_uuids == [FILE_A, FILE_B]


def test_conversation_scope_wins_over_the_project():
    """An explicit per-chat selection is more specific than the project default."""
    result = resolve_effective_scope(
        conversation(file_uuids=[FILE_A]), project(file_uuids=[FILE_B])
    )
    assert result.file_uuids == [FILE_A]


def test_project_without_a_scope_does_not_narrow_anything():
    """THE trap: a scopeless project must not turn 'all accessible' into 'nothing'.

    Returning an empty-but-present file list here would make every answer in the
    project report that no relevant excerpts exist.
    """
    result = resolve_effective_scope(conversation(), project(system_prompt_only=True))
    assert result.is_empty
    assert result.file_uuids == []


def test_collections_and_tags_are_inherited_too():
    result = resolve_effective_scope(
        conversation(), project(collection_uuids=[FILE_A], tag_names=["Client X"])
    )
    assert result.collection_uuids == [FILE_A]
    assert result.tag_names == ["Client X"]


def test_conversation_speakers_survive_inheriting_project_recordings():
    """Speakers are a separate axis: 'what did Dana say' across the client's calls."""
    result = resolve_effective_scope(conversation(speakers=["Dana"]), project(file_uuids=[FILE_A]))
    assert result.file_uuids == [FILE_A]
    assert result.speakers == ["Dana"]


def test_project_speakers_apply_when_the_conversation_names_none():
    result = resolve_effective_scope(conversation(), project(file_uuids=[FILE_A], speakers=["Ana"]))
    assert result.speakers == ["Ana"]


@pytest.mark.parametrize("axis", ["file_uuids", "collection_uuids", "tag_names"])
def test_any_conversation_axis_suppresses_inheritance(axis):
    """Scope is inherited as a unit, not merged field by field."""
    result = resolve_effective_scope(
        conversation(**{axis: [FILE_A] if axis != "tag_names" else ["own"]}),
        project(file_uuids=[FILE_B]),
    )
    assert result.file_uuids != [FILE_B] or axis == "file_uuids"
