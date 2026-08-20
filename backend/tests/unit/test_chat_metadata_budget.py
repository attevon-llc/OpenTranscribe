"""`cap_msg_metadata` — bounding a pathological `msg_metadata` blob (issue #52+).

`ChatMessage.msg_metadata` is an unbounded JSONB column. Wave 2 adds
container-valued diagnostics (a query plan, a per-speaker resolution result,
a list of failed retrieval legs) with no natural size limit, so a
pathological scope or a router bug could balloon one persisted message far
past anything the diagnostics panel needs to render. These tests pin: normal
metadata is untouched, an oversized container is dropped rather than
truncated, scalars always survive, and the cap announces itself rather than
silently rendering less than it should.
"""

from __future__ import annotations

import json

import pytest

from app.core.chat_metadata_budget import CAPPED_MARKER_KEY
from app.core.chat_metadata_budget import DEFAULT_MAX_METADATA_BYTES
from app.core.chat_metadata_budget import DROPPED_KEYS_KEY
from app.core.chat_metadata_budget import cap_msg_metadata

pytestmark = pytest.mark.unit


def test_none_and_empty_pass_through_unchanged():
    assert cap_msg_metadata(None) is None
    assert cap_msg_metadata({}) == {}


def test_ordinary_small_metadata_is_returned_unchanged_object():
    """The common case: no copy, no marker, same object."""
    metadata = {"retrieved": 12, "chunks_used": 4, "rewritten_query": "what did dana say"}
    assert cap_msg_metadata(metadata) is metadata


def test_metadata_exactly_at_the_ceiling_is_untouched():
    # Build a dict whose JSON is exactly at the boundary, then confirm no marker.
    payload = {"note": "x" * 50}
    size = len(json.dumps(payload).encode("utf-8"))
    result = cap_msg_metadata(payload, max_bytes=size)
    assert result == payload
    assert CAPPED_MARKER_KEY not in result


def test_an_oversized_container_is_dropped_whole_not_truncated():
    metadata = {
        "retrieved": 12,
        "plan": {"steps": ["step " + str(i) for i in range(2000)]},
    }
    result = cap_msg_metadata(metadata, max_bytes=500)

    assert result is not None
    assert result["retrieved"] == 12  # the scalar survives
    assert "plan" not in result  # dropped whole
    assert result[CAPPED_MARKER_KEY] is True
    assert "plan" in result[DROPPED_KEYS_KEY]
    # No fragment of the dropped list — never a partial/truncated plan.
    assert "step 0" not in json.dumps(result)


def test_scalars_always_survive_a_container_drop():
    metadata = {
        "retrieved": 5,
        "chunks_used": 2,
        "map_source": "llm-batch",
        "llm_calls": 3,
        "legs_failed": ["speaker_resolution"] * 5000,  # the oversized one
    }
    result = cap_msg_metadata(metadata, max_bytes=200)

    assert result is not None  # an over-budget dict is never capped away entirely
    for scalar_key in ("retrieved", "chunks_used", "map_source", "llm_calls"):
        assert result[scalar_key] == metadata[scalar_key]
    assert "legs_failed" not in result


def test_the_smallest_offending_container_is_dropped_first():
    """A budget that fits ONE of two oversized containers keeps the smaller one."""
    small_container = {"a": "x" * 100}
    large_container = {"a": "x" * 10_000}
    metadata = {"small": small_container, "large": large_container}

    # Budget big enough for the small one plus overhead, but not the large one.
    budget = len(json.dumps({"small": small_container}).encode("utf-8")) + 50
    result = cap_msg_metadata(metadata, max_bytes=budget)

    assert result is not None  # a budget that fits the small container caps, not drops all
    assert "small" in result
    assert "large" not in result
    assert result[DROPPED_KEYS_KEY] == ["large"]


def test_multiple_containers_are_dropped_until_the_budget_is_met():
    metadata = {
        "keep_me": "short scalar",
        "c1": {"data": "x" * 3000},
        "c2": {"data": "y" * 3000},
        "c3": {"data": "z" * 3000},
    }
    result = cap_msg_metadata(metadata, max_bytes=200)

    assert result is not None  # a scalar key alone always keeps the result non-empty
    total_size = len(json.dumps(result).encode("utf-8"))
    assert total_size <= 200 + 300  # small slack for the marker keys themselves
    assert result["keep_me"] == "short scalar"
    assert set(result[DROPPED_KEYS_KEY]) == {"c1", "c2", "c3"}


def test_capping_is_idempotent():
    """Capping an already-capped dict must not crash or re-cap forever."""
    metadata = {"retrieved": 1, "plan": {"steps": list(range(5000))}}
    once = cap_msg_metadata(metadata, max_bytes=300)
    twice = cap_msg_metadata(once, max_bytes=300)
    assert twice == once


def test_default_budget_comfortably_fits_realistic_diagnostics():
    """A real turn's metadata — retrieval + overview + plan diagnostics all
    at once — must fit under the DEFAULT budget without being capped."""
    realistic = {
        "rewritten_query": "what did the team decide about pricing in march",
        "retrieved": 48,
        "reranked": 12,
        "chunks_used": 10,
        "cache_hit": False,
        "files_searched": 25,
        "timings_ms": {"rewrite": 120, "retrieve": 340, "rerank": 90, "total": 550},
        "map_source": "llm-batch",
        "llm_calls": 4,
        "legs_failed": [],
        "speaker_resolution": {"matched": ["Dana Whitfield", "Bo O'Malley"], "ambiguous": []},
        "plan": {"steps": ["resolve scope", "route intent", "aggregate", "compose"]},
    }
    result = cap_msg_metadata(realistic)
    assert result is realistic
    assert CAPPED_MARKER_KEY not in result


def test_a_pathologically_large_scalar_is_returned_best_effort_not_crashed():
    """Documented limitation: this caps CONTAINERS, not individual scalar
    length. A caller producing an unbounded string needs its own cap — but
    this function must never raise regardless."""
    metadata = {"rewritten_query": "q" * 50_000}
    result = cap_msg_metadata(metadata, max_bytes=100)
    assert result is not None
    assert result["rewritten_query"] == metadata["rewritten_query"]


def test_a_non_json_safe_container_value_degrades_instead_of_crashing():
    class Unserializable:
        pass

    metadata = {"retrieved": 1, "weird": {"obj": Unserializable()}}
    # default=str in the size probe means this is treated as (a possibly
    # large) string rather than raising.
    result = cap_msg_metadata(metadata, max_bytes=DEFAULT_MAX_METADATA_BYTES)
    assert result is metadata  # small enough either way, but must not raise
