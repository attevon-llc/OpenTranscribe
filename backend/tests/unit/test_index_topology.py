"""Index topology (shards/replicas) for ``transcript_chunks`` — env-tunable at CREATION only.

Deployment-tiers plan: a laptop and a home server are single-node, so a replica is pure cost
(every replica shard sits UNASSIGNED forever, index status yellow); a managed multi-node AWS
domain is where a replica does something. Shard/replica counts are read from
``OPENSEARCH_CHUNKS_INDEX_SHARDS`` / ``OPENSEARCH_CHUNKS_INDEX_REPLICAS`` once, at import time,
into the module-level ``TRANSCRIPT_CHUNKS_INDEX_BODY`` constant that both
``ensure_chunks_index_exists`` (fresh install) and ``_get_index_body_with_dimension`` (also used
by the destructive ``recreate_index_for_dimension`` path) build indices from.

**The defaults must never move.** They are the shipped single-node topology, and a changed
default silently reshards every fresh install with no announcement — shards cannot be changed on
a live index without a full reindex into a new one. This module pins them byte-identically.
"""

from __future__ import annotations

from typing import Any
from typing import cast

from app.core.config import settings
from app.services.search import indexing_service as svc

# --------------------------------------------------------------------------------------- #
# The defaults, pinned byte-identically.
# --------------------------------------------------------------------------------------- #


def test_default_shard_count_is_one():
    """The shipped single-node default. Do not change this assertion without a deliberate,
    announced decision — it silently reshards every fresh install."""
    assert settings.OPENSEARCH_CHUNKS_INDEX_SHARDS == 1


def test_default_replica_count_is_zero():
    """The shipped single-node default. A replica needs a second node to mean anything —
    see the topology docstring on TRANSCRIPT_CHUNKS_INDEX_BODY."""
    assert settings.OPENSEARCH_CHUNKS_INDEX_REPLICAS == 0


def test_fresh_index_body_carries_the_configured_topology():
    """The module constant a fresh install actually creates the index from."""
    index_settings = cast(dict[str, Any], svc.TRANSCRIPT_CHUNKS_INDEX_BODY["settings"])["index"]
    assert index_settings["number_of_shards"] == settings.OPENSEARCH_CHUNKS_INDEX_SHARDS
    assert index_settings["number_of_replicas"] == settings.OPENSEARCH_CHUNKS_INDEX_REPLICAS


def test_fresh_index_body_topology_is_the_pinned_default_in_this_process():
    """Byte-identical pin end to end: with no override in this test process's env, a fresh
    index body is 1 shard / 0 replicas -- not merely "whatever settings says", but the actual
    shipped numbers."""
    index_settings = cast(dict[str, Any], svc.TRANSCRIPT_CHUNKS_INDEX_BODY["settings"])["index"]
    assert index_settings["number_of_shards"] == 1
    assert index_settings["number_of_replicas"] == 0


def test_recreate_dimension_path_shares_the_same_topology_source():
    """`_get_index_body_with_dimension` (used by BOTH the fresh-create path and the
    destructive `recreate_index_for_dimension`) deep-copies TRANSCRIPT_CHUNKS_INDEX_BODY, so
    a dimension change can never silently drop back to hardcoded 1/0 topology -- it stays
    whatever this process was configured with. This only builds a dict; it never touches a
    real cluster."""
    body = svc._get_index_body_with_dimension(384)
    index_settings = body["settings"]["index"]
    assert index_settings["number_of_shards"] == settings.OPENSEARCH_CHUNKS_INDEX_SHARDS
    assert index_settings["number_of_replicas"] == settings.OPENSEARCH_CHUNKS_INDEX_REPLICAS
    # And the dimension change itself still works -- this is a topology test, not a
    # regression bar for the dimension plumbing, so only spot-check it.
    assert body["mappings"]["properties"]["embedding"]["dimension"] == 384


# --------------------------------------------------------------------------------------- #
# Env-tunability at CREATION -- proven in a clean child process, since the settings
# singleton binds its defaults at class-body execution time and cannot be re-read in-process.
# --------------------------------------------------------------------------------------- #

_PRINT_TOPOLOGY = (
    "from app.services.search.indexing_service import TRANSCRIPT_CHUNKS_INDEX_BODY as b;"
    "idx = b['settings']['index'];"
    "print(f\"{idx['number_of_shards']}|{idx['number_of_replicas']}\")"
)


def test_shard_and_replica_counts_are_env_tunable_at_creation(run_in_clean_process, tmp_path):
    out = run_in_clean_process(
        _PRINT_TOPOLOGY,
        OPENSEARCH_CHUNKS_INDEX_SHARDS="3",
        OPENSEARCH_CHUNKS_INDEX_REPLICAS="2",
        UPLOAD_DIR=str(tmp_path / "up"),
        TEMP_DIR=str(tmp_path / "tmp"),
    )
    shards, replicas = out.split("|")
    assert shards == "3"
    assert replicas == "2"


def test_unset_env_still_yields_the_pinned_default_in_a_clean_process(
    run_in_clean_process, tmp_path
):
    """Sibling control for the tunability test above: prove the default really is what
    ships, not an artifact of this test process's own env already carrying an override."""
    out = run_in_clean_process(
        _PRINT_TOPOLOGY,
        unset=("OPENSEARCH_CHUNKS_INDEX_SHARDS", "OPENSEARCH_CHUNKS_INDEX_REPLICAS"),
        UPLOAD_DIR=str(tmp_path / "up"),
        TEMP_DIR=str(tmp_path / "tmp"),
    )
    shards, replicas = out.split("|")
    assert shards == "1"
    assert replicas == "0"


def test_negative_values_are_clamped_to_the_topology_floor(run_in_clean_process, tmp_path):
    """A negative shard/replica count is nonsensical to OpenSearch; the settings clamp to
    the same floor the field's own `max(..., N)` documents (1 shard, 0 replicas), rather
    than passing a negative number through to `indices.create` and failing there."""
    out = run_in_clean_process(
        _PRINT_TOPOLOGY,
        OPENSEARCH_CHUNKS_INDEX_SHARDS="-5",
        OPENSEARCH_CHUNKS_INDEX_REPLICAS="-5",
        UPLOAD_DIR=str(tmp_path / "up"),
        TEMP_DIR=str(tmp_path / "tmp"),
    )
    shards, replicas = out.split("|")
    assert shards == "1"
    assert replicas == "0"
