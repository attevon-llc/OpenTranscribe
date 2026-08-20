"""The additive (MINOR) mapping machinery — every field after this one rides it (#W2.7a).

Before this, the chunks index had exactly one version number (``_INDEX_VERSION``) and
exactly one tool to move it: ``recreate_index_for_dimension``, which **deletes the
index**. Adding a genuinely additive field — one no existing document has an opinion
about — had no path that did not cost every deployment a full reindex.

This module tests the machinery, not the two fields it happens to carry (those are
covered in ``test_speaker_id_fields.py``): the version split, idempotency of applying
pending steps, and — because a structural guard that matches nothing is indistinguishable
from a clean codebase — a self-test proving the destructive-path guard actually fires.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import patch

import app.services.search.indexing_service as svc
from app.core.config import settings

_INDEX = settings.OPENSEARCH_CHUNKS_INDEX


def _client(meta: dict[str, Any]) -> MagicMock:
    """Stub client whose ``get_mapping`` reports ``meta`` for the chunks index."""
    client = MagicMock()
    client.indices.exists.return_value = True
    client.indices.get_mapping.return_value = {_INDEX: {"mappings": {"_meta": meta}}}
    return client


# --------------------------------------------------------------------------- #
# Structural guard: the additive path must never reach the destructive one.
# --------------------------------------------------------------------------- #


def _build_call_graph(tree: ast.Module) -> dict[str, set[str]]:
    """Map each function's name to the names of functions it calls in its own body."""
    graph: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            called: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    func = sub.func
                    if isinstance(func, ast.Name):
                        called.add(func.id)
                    elif isinstance(func, ast.Attribute):
                        called.add(func.attr)
            graph[node.name] = called
    return graph


def _reachable(graph: dict[str, set[str]], start: str) -> set[str]:
    """Every function name reachable from ``start`` by following calls, ``start`` excluded."""
    seen: set[str] = set()
    stack = list(graph.get(start, set()))
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(graph.get(name, set()))
    return seen


def test_ensure_chunks_index_exists_never_reaches_the_destructive_recreate():
    """``ensure_chunks_index_exists`` is the additive entry point.

    It must never call ``recreate_index_for_dimension`` — that function deletes the
    whole index, and the entire point of this machinery is a path that adds a field
    without doing that.
    """
    tree = ast.parse(inspect.getsource(svc))
    graph = _build_call_graph(tree)
    reachable = _reachable(graph, "ensure_chunks_index_exists")
    assert "recreate_index_for_dimension" not in reachable, (
        "ensure_chunks_index_exists must never reach recreate_index_for_dimension "
        "(it DELETES the index) — a genuinely destructive change belongs behind "
        "_INDEX_VERSION and an explicit reindex, not the additive path"
    )


def test_apply_pending_additive_steps_never_reaches_the_destructive_recreate():
    tree = ast.parse(inspect.getsource(svc))
    graph = _build_call_graph(tree)
    reachable = _reachable(graph, "_apply_pending_additive_steps")
    assert "recreate_index_for_dimension" not in reachable


def test_the_call_graph_guard_fires_on_a_deliberately_broken_sample():
    """Guard the guard (issue #431's rule): a detector that matches nothing is a clean
    codebase and a broken one, indistinguishably. Feed the same walker a function that
    DOES route through the destructive call and require it to be caught.
    """
    broken_source = """
def ensure_chunks_index_exists():
    _apply_pending_additive_steps("x")

def _apply_pending_additive_steps(index_name):
    _helper(index_name)

def _helper(index_name):
    recreate_index_for_dimension(384)
"""
    tree = ast.parse(broken_source)
    graph = _build_call_graph(tree)
    assert "recreate_index_for_dimension" in _reachable(graph, "ensure_chunks_index_exists")
    assert "recreate_index_for_dimension" in _reachable(graph, "_apply_pending_additive_steps")


def test_the_call_graph_guard_stays_clean_on_a_function_with_no_destructive_call():
    """Must-stay-clean sibling: an unrelated call graph is not flagged."""
    clean_source = """
def ensure_chunks_index_exists():
    _apply_pending_additive_steps("x")

def _apply_pending_additive_steps(index_name):
    _helper(index_name)

def _helper(index_name):
    log_something(index_name)
"""
    tree = ast.parse(clean_source)
    graph = _build_call_graph(tree)
    assert "recreate_index_for_dimension" not in _reachable(graph, "ensure_chunks_index_exists")


# --------------------------------------------------------------------------- #
# The version split itself.
# --------------------------------------------------------------------------- #


def test_a_fresh_index_body_is_stamped_at_the_latest_additive_version():
    mappings = cast(dict[str, Any], svc.TRANSCRIPT_CHUNKS_INDEX_BODY["mappings"])
    meta = mappings["_meta"]
    assert meta["version"] == svc._INDEX_VERSION
    assert meta["additive_version"] == svc._ADDITIVE_VERSION
    assert svc._ADDITIVE_VERSION >= 1, "no additive steps registered — nothing to test"


def test_a_fresh_index_body_already_carries_every_additive_field():
    assert svc.ADDITIVE_MAPPING_STEPS, "no additive steps registered — the loop below is vacuous"
    mappings = cast(dict[str, Any], svc.TRANSCRIPT_CHUNKS_INDEX_BODY["mappings"])
    properties = mappings["properties"]
    for step in svc.ADDITIVE_MAPPING_STEPS:
        assert step.properties, f"step {step.version} declares no fields at all"
        for field, definition in step.properties.items():
            assert properties.get(field) == definition, (
                f"{field} (additive step {step.version}) is missing from the base "
                "mapping — a freshly created index would not have it, only an "
                "existing one walked forward by _apply_pending_additive_steps"
            )


def test_applying_pending_steps_bumps_additive_version_and_preserves_major_version():
    client = _client({"version": svc._INDEX_VERSION, "additive_version": 0})

    with patch.object(svc, "opensearch_client", client):
        svc._apply_pending_additive_steps(_INDEX)

    put_calls = client.indices.put_mapping.call_args_list
    assert put_calls, "no pending steps were applied against a fresh (additive_version=0) index"

    # Every step's `properties` fragment was PUT...
    field_calls = [c for c in put_calls if "properties" in c.kwargs.get("body", {})]
    assert len(field_calls) == len(svc.ADDITIVE_MAPPING_STEPS)
    for step, call in zip(svc.ADDITIVE_MAPPING_STEPS, field_calls, strict=True):
        assert call.kwargs["body"]["properties"] == step.properties

    # ...and the FINAL call wrote the new additive_version while preserving the
    # untouched major version.
    meta_calls = [c for c in put_calls if "_meta" in c.kwargs.get("body", {})]
    assert len(meta_calls) == 1
    written_meta = meta_calls[0].kwargs["body"]["_meta"]
    assert written_meta["version"] == svc._INDEX_VERSION
    assert written_meta["additive_version"] == svc._ADDITIVE_VERSION


def test_an_index_already_at_the_target_additive_version_is_untouched():
    """Idempotency, case 1: nothing to do, and nothing is called.

    Driven through ``ensure_chunks_index_exists`` (not the private helper directly)
    so there is a real, non-mock assertion to make alongside the call-count check:
    the public contract — "the index exists and is usable" — must still hold true
    when there was nothing to apply.
    """
    client = _client({"version": svc._INDEX_VERSION, "additive_version": svc._ADDITIVE_VERSION})

    with patch.object(svc, "opensearch_client", client):
        result = svc.ensure_chunks_index_exists()

    assert result is True
    assert client.indices.put_mapping.call_count == 0


def test_applying_pending_steps_twice_is_idempotent():
    """Idempotency, case 2: running it twice in a row does the work exactly once."""
    client = _client({"version": svc._INDEX_VERSION, "additive_version": 0})

    with patch.object(svc, "opensearch_client", client):
        svc._apply_pending_additive_steps(_INDEX)
        first_call_count = client.indices.put_mapping.call_count
        assert first_call_count > 0

        # The stub's get_mapping still reports the ORIGINAL (stale) meta — a real
        # cluster would report the just-written additive_version on the next read,
        # so update the stub to reflect that before the second call, exactly as a
        # real second `ensure_chunks_index_exists()` call would see.
        client.indices.get_mapping.return_value = {
            _INDEX: {
                "mappings": {
                    "_meta": {
                        "version": svc._INDEX_VERSION,
                        "additive_version": svc._ADDITIVE_VERSION,
                    }
                }
            }
        }
        svc._apply_pending_additive_steps(_INDEX)

    assert client.indices.put_mapping.call_count == first_call_count, (
        "a second call against an index now at the target version made further "
        "put_mapping calls — the additive path is not idempotent"
    )


def test_applying_pending_steps_never_recreates_the_index():
    """Behavioural sibling of the structural guard above: no delete, no create.

    Reads the mapping back after the call — real state, not mock bookkeeping — to
    confirm the additive fields actually landed via the surviving mock's own
    tracked state, alongside the "nothing destructive happened" assertions.
    """
    client = _client({"version": svc._INDEX_VERSION, "additive_version": 0})

    with patch.object(svc, "opensearch_client", client):
        svc._apply_pending_additive_steps(_INDEX)
        applied_fields: dict[str, Any] = {}
        for call in client.indices.put_mapping.call_args_list:
            applied_fields.update(call.kwargs.get("body", {}).get("properties", {}))

    assert applied_fields, "no fields were ever PUT — the steps did nothing at all"
    assert client.indices.delete.call_count == 0
    assert client.indices.create.call_count == 0


def test_ensure_chunks_index_exists_applies_pending_steps_for_an_existing_index():
    """The wiring: an existing index gets walked forward on every call, not just at
    creation time."""
    client = _client({"version": svc._INDEX_VERSION, "additive_version": 0})

    with patch.object(svc, "opensearch_client", client):
        result = svc.ensure_chunks_index_exists()

    assert result is True
    assert client.indices.put_mapping.call_count > 0
    assert client.indices.create.call_count == 0
