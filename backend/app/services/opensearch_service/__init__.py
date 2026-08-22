"""OpenSearch speaker/voiceprint plane and transcript document store.

Split out of a single 3670-line module (issue #284, A3.5). Every public name
this package previously exported is re-exported here, so
``from app.services.opensearch_service import <name>`` keeps working unchanged.

Where things live:

- :mod:`client` — the ``opensearch_client`` singleton and low-level primitives.
- :mod:`aliases` — ``speakers`` alias management, active-index resolution.
- :mod:`indices` — index creation / bootstrap.
- :mod:`repair` — corruption detection, index repair and rebuild.
- :mod:`transcripts` — transcript indexing and full-text search.
- :mod:`speaker_write` / :mod:`speaker_read` / :mod:`speaker_maintenance` /
  :mod:`speaker_metadata` / :mod:`speaker_collections` — the speaker documents.
- :mod:`profiles` / :mod:`clusters` / :mod:`matching` — the kNN read paths.

.. warning::
   ``cosinesimil`` returns ``(1 + cosine) / 2``, never raw cosine. Every kNN
   score read must convert with ``2.0 * hit["_score"] - 1.0``.
"""

from typing import Any

from app.core.config import settings
from app.services.opensearch_service.aliases import _reindex_and_alias
from app.services.opensearch_service.aliases import get_active_speaker_index
from app.services.opensearch_service.aliases import get_active_versioned_index
from app.services.opensearch_service.aliases import get_speaker_embedding_dimension
from app.services.opensearch_service.aliases import get_write_index
from app.services.opensearch_service.aliases import invalidate_active_speaker_index_cache
from app.services.opensearch_service.aliases import migrate_to_alias_based_indices
from app.services.opensearch_service.aliases import swap_speaker_alias
from app.services.opensearch_service.client import KnnProbeResult
from app.services.opensearch_service.client import _get_alias_target
from app.services.opensearch_service.client import _get_index_embedding_dimension
from app.services.opensearch_service.client import _get_sentence_transformer
from app.services.opensearch_service.client import _is_alias
from app.services.opensearch_service.client import _is_index_corruption_error
from app.services.opensearch_service.client import _safe_index_exists
from app.services.opensearch_service.client import _speaker_org_filter_clauses
from app.services.opensearch_service.client import get_opensearch_client
from app.services.opensearch_service.client import probe_knn_health
from app.services.opensearch_service.client import probe_knn_health_cached
from app.services.opensearch_service.client import reset_knn_health_cache
from app.services.opensearch_service.clusters import delete_cluster_embedding
from app.services.opensearch_service.clusters import find_matching_clusters
from app.services.opensearch_service.clusters import store_cluster_embedding
from app.services.opensearch_service.clusters import update_cluster_embedding
from app.services.opensearch_service.indices import _ensure_versioned_speaker_index
from app.services.opensearch_service.indices import create_speaker_index_v4
from app.services.opensearch_service.indices import ensure_indices_exist
from app.services.opensearch_service.indices import ensure_v4_index_exists
from app.services.opensearch_service.matching import _extract_speaker_match
from app.services.opensearch_service.matching import batch_find_matching_speakers
from app.services.opensearch_service.matching import find_matching_speaker
from app.services.opensearch_service.matching import msearch_speaker_similarities
from app.services.opensearch_service.profiles import find_matching_profiles
from app.services.opensearch_service.profiles import get_profile_embedding
from app.services.opensearch_service.profiles import msearch_profile_knn_batch
from app.services.opensearch_service.profiles import remove_profile_embedding
from app.services.opensearch_service.profiles import store_profile_embedding
from app.services.opensearch_service.profiles import store_profile_embedding_v4
from app.services.opensearch_service.repair import _repair_index
from app.services.opensearch_service.repair import check_and_repair_indices
from app.services.opensearch_service.repair import rebuild_speaker_index
from app.services.opensearch_service.speaker_collections import bulk_update_collection_assignments
from app.services.opensearch_service.speaker_collections import get_speakers_in_collection
from app.services.opensearch_service.speaker_collections import move_speaker_to_profile_collection
from app.services.opensearch_service.speaker_collections import update_speaker_collections
from app.services.opensearch_service.speaker_maintenance import cleanup_orphaned_speaker_embeddings
from app.services.opensearch_service.speaker_maintenance import merge_speaker_embeddings
from app.services.opensearch_service.speaker_maintenance import remove_speaker_embedding
from app.services.opensearch_service.speaker_metadata import find_speaker_across_media
from app.services.opensearch_service.speaker_metadata import sync_speaker_profiles_to_opensearch
from app.services.opensearch_service.speaker_metadata import update_speaker_display_name
from app.services.opensearch_service.speaker_metadata import update_speaker_profile
from app.services.opensearch_service.speaker_metadata import update_speaker_segment_count
from app.services.opensearch_service.speaker_read import get_all_speaker_embeddings
from app.services.opensearch_service.speaker_read import get_speaker_document
from app.services.opensearch_service.speaker_read import get_speaker_embedding
from app.services.opensearch_service.speaker_read import get_speaker_embeddings_batch
from app.services.opensearch_service.speaker_read import iter_speaker_embeddings
from app.services.opensearch_service.speaker_write import add_speaker_embedding
from app.services.opensearch_service.speaker_write import add_speaker_embedding_v4
from app.services.opensearch_service.speaker_write import bulk_add_speaker_embeddings_v4
from app.services.opensearch_service.transcripts import index_transcript
from app.services.opensearch_service.transcripts import update_transcript_title


def __getattr__(name: str) -> Any:
    """Forward the mutable module globals to the module that owns them.

    ``opensearch_client`` is rebound at runtime by
    :func:`client.get_opensearch_client` when the eager construction at import
    time failed. A plain re-export would freeze the value seen at package
    import, so it is resolved on every attribute access instead.
    """
    if name == "opensearch_client":
        from app.services.opensearch_service import client

        return client.opensearch_client
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "_ensure_versioned_speaker_index",
    "_extract_speaker_match",
    "_get_alias_target",
    "_get_index_embedding_dimension",
    "_get_sentence_transformer",
    "_is_alias",
    "_is_index_corruption_error",
    "_reindex_and_alias",
    "_repair_index",
    "_safe_index_exists",
    "_speaker_org_filter_clauses",
    "KnnProbeResult",
    "probe_knn_health",
    "probe_knn_health_cached",
    "reset_knn_health_cache",
    "add_speaker_embedding",
    "add_speaker_embedding_v4",
    "batch_find_matching_speakers",
    "bulk_add_speaker_embeddings_v4",
    "bulk_update_collection_assignments",
    "check_and_repair_indices",
    "cleanup_orphaned_speaker_embeddings",
    "create_speaker_index_v4",
    "delete_cluster_embedding",
    "ensure_indices_exist",
    "ensure_v4_index_exists",
    "find_matching_clusters",
    "find_matching_profiles",
    "find_matching_speaker",
    "find_speaker_across_media",
    "get_active_speaker_index",
    "get_active_versioned_index",
    "get_all_speaker_embeddings",
    "get_opensearch_client",
    "get_profile_embedding",
    "get_speaker_document",
    "get_speaker_embedding",
    "get_speaker_embedding_dimension",
    "get_speaker_embeddings_batch",
    "get_speakers_in_collection",
    "get_write_index",
    "index_transcript",
    "invalidate_active_speaker_index_cache",
    "iter_speaker_embeddings",
    "merge_speaker_embeddings",
    "migrate_to_alias_based_indices",
    "move_speaker_to_profile_collection",
    "msearch_profile_knn_batch",
    "msearch_speaker_similarities",
    "opensearch_client",
    "rebuild_speaker_index",
    "remove_profile_embedding",
    "remove_speaker_embedding",
    "settings",
    "store_cluster_embedding",
    "store_profile_embedding",
    "store_profile_embedding_v4",
    "swap_speaker_alias",
    "sync_speaker_profiles_to_opensearch",
    "update_cluster_embedding",
    "update_speaker_collections",
    "update_speaker_display_name",
    "update_speaker_profile",
    "update_speaker_segment_count",
    "update_transcript_title",
]
