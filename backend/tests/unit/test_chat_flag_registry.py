"""Completeness tests for the declarative `chat.*` admin-flag registry.

Before `core/chat_flag_registry.py`, adding one admin-tunable `chat.*` flag
needed edits in FOUR places, and a missing entry in one of them —
`api/endpoints/chat/admin_settings.py`'s old hand-written `_DESCRIPTIONS`
dict — was not caught anywhere until an admin tried to save the new flag and
got a `KeyError`, i.e. a 500. These tests are the completeness check: they
fail the moment the registry and its callers (`services.chat.settings`,
`schemas.chat.ChatAdminSettingsUpdate`, and the endpoint's own descriptions)
disagree on field set, setting key, or description — which is exactly the
class of drift that used to reach production silently.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.api.endpoints.chat.admin_settings import _DESCRIPTIONS as ENDPOINT_DESCRIPTIONS
from app.core.chat_flag_registry import BY_FIELD
from app.core.chat_flag_registry import CHAT_FLAG_REGISTRY
from app.core.chat_flag_registry import DESCRIPTIONS
from app.core.chat_flag_registry import SETTING_KEYS as REGISTRY_SETTING_KEYS
from app.core.chat_flag_registry import find_missing_pieces
from app.schemas.chat import ChatAdminSettingsUpdate
from app.services.chat.settings import DEFAULTS as SETTINGS_DEFAULTS
from app.services.chat.settings import SETTING_KEYS as REAL_SETTING_KEYS
from app.services.chat.settings import ChatSettings

pytestmark = pytest.mark.unit

#: `ChatSettings` fields that are deliberately NOT registered flags, so they
#: are excluded before comparing the dataclass's field set to the registry.
#: `max_output_tokens` is resolved from tenant limits (`apply_tenant_limits`),
#: never admin- or user-set directly, so it has no `SystemSettings` row, no
#: registry entry, and no `ChatAdminSettingsUpdate` field either.
_CHATSETTINGS_FIELDS_WITH_NO_REGISTRY_ENTRY = frozenset({"max_output_tokens"})


def _chatsettings_registrable_fields() -> set[str]:
    return {
        f.name
        for f in dataclasses.fields(ChatSettings)
        if f.name not in _CHATSETTINGS_FIELDS_WITH_NO_REGISTRY_ENTRY
    }


# The dict this replaced, verbatim — proves the migration is byte-identical,
# not just "close enough". If this ever needs to change, the registry's
# `description` fields should change with it in the same commit.
_OLD_HAND_WRITTEN_DESCRIPTIONS = {
    "candidate_pool": "Chunks retrieved before reranking",
    "final_chunks": "Chunks included in the prompt",
    "max_chunks_per_file": "Maximum chunks contributed by any one recording",
    "rerank_enabled": "Rerank retrieved chunks with a CPU cross-encoder",
    "rerank_max_pairs": "Maximum (query, chunk) pairs scored per message",
    "query_rewrite_enabled": "Expand follow-up questions into standalone queries",
    "cache_ttl_seconds": "Retrieval cache lifetime (0 disables)",
    "semantic_cache_enabled": "Reuse results for near-identical questions",
    "semantic_cache_threshold": "Cosine similarity required for a semantic cache hit",
    "history_max_turns": "Prior exchanges (question + answer) replayed to the model",
    "messages_per_hour": "Per-user hourly message ceiling",
    "max_concurrent_streams": "Per-user simultaneous streaming replies",
    "retention_days": "Delete conversations older than N days (0 keeps forever)",
    "speaker_facet_content_scope": (
        "Score the speaker facet by spoken content instead of recording titles"
    ),
    "speaker_stats_enabled": "Answer 'who talked most' from exact per-speaker talk time",
    "map_tier_summaries": (
        "Prefer each file's fresh LLM summary over its digest in the collection map"
    ),
    "speaker_resolver_enabled": (
        "Resolve a speaker named in the question text into a parallel retrieval leg"
    ),
    "map_tier_speaker_summaries": (
        "Prefer each file's fresh LLM speaker analysis over its digest in the per-speaker map"
    ),
    "recurrence_enabled": (
        "Detect items recurring across multiple recordings and surface a <recurrence> block"
    ),
    # #403 W2.6 — added in the same commit as the registry entries, per this
    # test's own docstring above.
    "planner_enabled": "Plan multi-part questions into parallel retrieval legs before answering",
    "planner_max_parallel_legs": "Maximum retrieval legs run in parallel for a planned turn",
    "enrichment_enabled": (
        "Reconcile merged multi-leg evidence into a <synthesis> block before answering"
    ),
    # Issue #523 — added in the same commit as its registry entry, same rule.
    "context_expansion_enabled": (
        "Widen a short retrieved chunk to its surrounding exchange before masking"
    ),
    # #532 experiment arms — added with their registry entries, same rule.
    "overview_citable": ("#532 arm (a): overview file entries get citation ids the model can use"),
    "overview_block_rule": (
        "#532 arm (b): attach the anti-narrowing rule to the overview block itself"
    ),
    "overview_after_excerpts": (
        "#532 arm (c): place the overview after the excerpts instead of before"
    ),
    # GH #514 — added with its registry entry, same rule as the arms above.
    "trace_enabled": ("Stream a live query-execution trace for each answer (GH #514)"),
}


def test_migration_is_byte_identical_to_the_old_hand_written_dict():
    assert DESCRIPTIONS == _OLD_HAND_WRITTEN_DESCRIPTIONS


def test_the_endpoint_now_sources_its_descriptions_from_the_registry():
    """Not just equal — the SAME object, so there is no second copy to drift."""
    assert ENDPOINT_DESCRIPTIONS is DESCRIPTIONS


def test_every_registry_field_has_a_setting_key_matching_settings_py():
    assert REGISTRY_SETTING_KEYS == REAL_SETTING_KEYS


def test_every_registry_field_exists_on_chat_admin_settings_update():
    schema_fields = set(ChatAdminSettingsUpdate.model_fields)
    registry_fields = set(BY_FIELD)
    assert registry_fields == schema_fields


def test_every_registry_field_exists_on_the_chatsettings_dataclass():
    """The one comparison nothing previously made — see `find_missing_pieces`'s
    `dataclass_fields` docstring for why `ChatSettings` cannot be derived the
    way `SETTING_KEYS`/`DEFAULTS` now are."""
    assert set(BY_FIELD) == _chatsettings_registrable_fields()


def test_every_registry_default_matches_the_coded_default_in_settings_py():
    """The registry's `default` is sourced from `core.constants`, same as
    `services.chat.settings.DEFAULTS` — this proves neither drifted from the
    other independently."""
    assert BY_FIELD, "the registry is empty — this test would pass vacuously"
    for field, spec in BY_FIELD.items():
        assert spec.default == SETTINGS_DEFAULTS[field], field


def test_find_missing_pieces_reports_nothing_for_the_real_registry():
    problems = find_missing_pieces(
        setting_keys=REAL_SETTING_KEYS,
        schema_fields=set(ChatAdminSettingsUpdate.model_fields),
        dataclass_fields=_chatsettings_registrable_fields(),
    )
    assert problems == {}


# ---------------------------------------------------------------------------
# `find_missing_pieces` must-fire controls: a detector that never fires is
# indistinguishable from a clean registry (see backend/tests/CLAUDE.md's
# note on auditor self-tests).
# ---------------------------------------------------------------------------


def test_find_missing_pieces_fires_on_a_field_absent_from_setting_keys():
    incomplete_setting_keys = dict(REAL_SETTING_KEYS)
    del incomplete_setting_keys["final_chunks"]

    problems = find_missing_pieces(setting_keys=incomplete_setting_keys)

    assert "final_chunks" in problems
    assert any("SETTING_KEYS" in p for p in problems["final_chunks"])


def test_find_missing_pieces_fires_on_a_setting_key_absent_from_the_registry():
    extended_setting_keys = dict(REAL_SETTING_KEYS)
    extended_setting_keys["a_field_the_registry_has_never_heard_of"] = "chat.made.up"

    problems = find_missing_pieces(setting_keys=extended_setting_keys)

    assert "a_field_the_registry_has_never_heard_of" in problems


def test_find_missing_pieces_fires_on_a_setting_key_value_mismatch():
    mismatched = dict(REAL_SETTING_KEYS)
    mismatched["final_chunks"] = "chat.rag.wrong_key_entirely"

    problems = find_missing_pieces(setting_keys=mismatched)

    assert "final_chunks" in problems
    assert any("mismatch" in p for p in problems["final_chunks"])


def test_find_missing_pieces_fires_on_a_field_absent_from_the_schema():
    incomplete_schema_fields = set(ChatAdminSettingsUpdate.model_fields) - {"retention_days"}

    problems = find_missing_pieces(schema_fields=incomplete_schema_fields)

    assert "retention_days" in problems
    assert any("ChatAdminSettingsUpdate" in p for p in problems["retention_days"])


def test_find_missing_pieces_fires_on_a_field_absent_from_the_chatsettings_dataclass():
    incomplete_dataclass_fields = _chatsettings_registrable_fields() - {"retention_days"}

    problems = find_missing_pieces(dataclass_fields=incomplete_dataclass_fields)

    assert "retention_days" in problems
    assert any("ChatSettings dataclass" in p for p in problems["retention_days"])


def test_find_missing_pieces_fires_on_a_dataclass_field_absent_from_the_registry():
    extended_dataclass_fields = _chatsettings_registrable_fields() | {
        "a_field_the_registry_has_never_heard_of"
    }

    problems = find_missing_pieces(dataclass_fields=extended_dataclass_fields)

    assert "a_field_the_registry_has_never_heard_of" in problems


def test_find_missing_pieces_skips_comparisons_that_were_not_asked_for():
    """Passing neither argument must report nothing — it is not a trick way
    to always fail."""
    assert find_missing_pieces() == {}


# ---------------------------------------------------------------------------
# The 500-on-save bug, reproduced and proven structurally impossible now
# ---------------------------------------------------------------------------


def test_a_registered_field_never_keyerrors_the_old_dict_subscript_way():
    """Reproduces the exact old bug shape: `_DESCRIPTIONS[field]` on a dict
    missing an entry raises KeyError, which is what turned into a 500 the
    first time an admin saved a newly added flag with no description."""
    incomplete = {"candidate_pool": "only this one is described"}

    with pytest.raises(KeyError):
        _ = incomplete["final_chunks"]  # the old access pattern

    # The endpoint no longer uses that pattern (see its `.get(..., fallback)`),
    # and the real registry has every SETTING_KEYS field described anyway:
    assert REAL_SETTING_KEYS, "SETTING_KEYS is empty — this test would pass vacuously"
    for field in REAL_SETTING_KEYS:
        assert field in DESCRIPTIONS


def test_the_registry_covers_every_flag_the_admin_ui_can_actually_save():
    """One more angle on the same guarantee, phrased as the admin-facing
    property: nothing PUT-able through `ChatAdminSettingsUpdate` can reach
    `update_chat_admin_settings` without a setting key AND a description."""
    fields = ChatAdminSettingsUpdate.model_fields
    assert fields, "ChatAdminSettingsUpdate has no fields — this test would pass vacuously"
    for field in fields:
        assert field in REAL_SETTING_KEYS, f"{field} has no SystemSettings key"
        assert field in DESCRIPTIONS, f"{field} has no admin-UI description"


# ---------------------------------------------------------------------------
# Registry shape sanity — every spec has a description and correct bounds
# ---------------------------------------------------------------------------


def test_every_spec_has_a_non_empty_description():
    assert CHAT_FLAG_REGISTRY, "the registry is empty — this test would pass vacuously"
    for spec in CHAT_FLAG_REGISTRY:
        assert spec.description.strip(), spec.field


def test_bool_flags_carry_no_numeric_bounds():
    bool_specs = [spec for spec in CHAT_FLAG_REGISTRY if spec.value_type is bool]
    # Real, non-conditional assertion this test would fail without: at least
    # one bool-typed flag must exist, or the loop below never runs and the
    # test passes having checked nothing.
    assert bool_specs, "no bool-typed flags registered — nothing here was actually checked"
    for spec in bool_specs:
        assert spec.ge is None and spec.le is None, spec.field


def test_numeric_flags_carry_bounds_matching_the_schema():
    schema_fields = ChatAdminSettingsUpdate.model_fields
    numeric_specs = [spec for spec in CHAT_FLAG_REGISTRY if spec.value_type is not bool]
    assert numeric_specs, "no numeric flags registered — nothing here was actually checked"

    for spec in numeric_specs:
        metadata = schema_fields[spec.field].metadata
        ge_constraints = [meta.ge for meta in metadata if hasattr(meta, "ge")]
        le_constraints = [meta.le for meta in metadata if hasattr(meta, "le")]
        # Every numeric field is bounded on both ends in both places — an
        # empty constraint list here would make the loop below check nothing
        # for this field, same failure shape as the conditional it replaces.
        assert ge_constraints, f"{spec.field}: schema has no `ge` constraint to compare"
        assert le_constraints, f"{spec.field}: schema has no `le` constraint to compare"
        assert ge_constraints == [spec.ge], spec.field
        assert le_constraints == [spec.le], spec.field


def test_no_flag_is_marked_experimental_yet():
    """Documents current reality: `experimental=True` is plumbing for a
    future planner/enrichment toggle, not a live behaviour. If this ever
    fails, the admin UI's "Experimental" subsection needs real controls, not
    just its current explanatory-copy placeholder."""
    assert all(not spec.experimental for spec in CHAT_FLAG_REGISTRY)
