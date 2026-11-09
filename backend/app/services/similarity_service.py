"""
High-performance similarity computation service using PyTorch and OpenSearch.

This service provides GPU-accelerated similarity calculations optimized for production:
1. OpenSearch native kNN with cosine similarity (primary method)
2. PyTorch GPU-accelerated vectorized operations
3. Advanced batching and tensor operations

No fallbacks - requires modern dependencies for optimal performance.
"""

import logging
from typing import Any

import numpy as np
import torch
from torch.nn import functional as nn_functional

from app.core.env import env_bool
from app.services.search.availability_field import exclude_unavailable_clause
from app.utils.cosine_space import opensearch_score_from_raw_cosine
from app.utils.cosine_space import raw_cosine_from_opensearch_score

logger = logging.getLogger(__name__)


class SimilarityService:
    """GPU-accelerated similarity service leveraging PyTorch and OpenSearch."""

    # Use GPU only on GPU workers to avoid CUDA context leaks in CPU worker children.
    _is_gpu_worker = env_bool("PRELOAD_GPU_MODELS", False)
    device = torch.device("cuda" if _is_gpu_worker and torch.cuda.is_available() else "cpu")

    @staticmethod
    def cosine_similarity(
        embedding1: np.ndarray | torch.Tensor, embedding2: np.ndarray | torch.Tensor
    ) -> float:
        """
        Compute cosine similarity using PyTorch's optimized implementation.

        Uses GPU acceleration when available for maximum performance.

        Args:
            embedding1: First embedding vector (numpy array or torch tensor)
            embedding2: Second embedding vector (numpy array or torch tensor)

        Returns:
            Cosine similarity in ``[-1, 1]`` — the same space as
            :meth:`opensearch_similarity_search`'s ``similarity`` and as the
            ``SPEAKER_CONFIDENCE_*`` constants. Negatives are real and are
            preserved (issue #690).
        """
        # Convert to torch tensors and move to appropriate device
        if isinstance(embedding1, np.ndarray):
            embedding1 = torch.from_numpy(embedding1).float()
        if isinstance(embedding2, np.ndarray):
            embedding2 = torch.from_numpy(embedding2).float()

        embedding1 = embedding1.to(SimilarityService.device)
        embedding2 = embedding2.to(SimilarityService.device)

        # Use PyTorch's optimized cosine similarity
        similarity = nn_functional.cosine_similarity(
            embedding1.unsqueeze(0), embedding2.unsqueeze(0), dim=1
        ).item()

        # Bound float32 rounding to cosine's real domain — NOT to [0, 1].
        # Clamping the negative half to 0.0 made "opposite" and "orthogonal"
        # indistinguishable and put this method in a different space from
        # `opensearch_similarity_search` on the same class (issue #690).
        return float(max(-1.0, min(1.0, similarity)))

    @staticmethod
    def opensearch_similarity_search(
        embedding: list[float] | np.ndarray | torch.Tensor,
        user_id: int,
        index_name: str = "speakers",
        min_raw_cosine: float = 0.7,
        max_results: int = 50,
        exclude_ids: list[int] | None = None,
        organization_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        High-performance OpenSearch kNN similarity search with advanced features.

        Leverages OpenSearch's native HNSW algorithm with cosine similarity for
        optimal performance at scale.

        The gate is stated in **raw cosine** and converted to OpenSearch's
        ``cosinesimil`` score space on the way out, so callers never have to know
        the index's scoring convention (issue #674).

        Args:
            embedding: Query embedding vector (any format)
            user_id: User ID for filtering
            index_name: OpenSearch index to search
            min_raw_cosine: Minimum **raw cosine** similarity a hit must reach,
                in ``[-1, 1]`` — the same space as ``SPEAKER_CONFIDENCE_*`` and as
                the ``similarity`` this function returns. Never pass a value
                already in ``cosinesimil`` score space.
            max_results: Maximum number of results (increased default)
            exclude_ids: Optional list of IDs to exclude from results
            organization_id: Active org id (None = personal) — tenant gate.

        Returns:
            List of similarity matches whose ``similarity`` is **raw cosine** in
            ``[-1, 1]`` — the same space :meth:`cosine_similarity` returns.

        Note:
            There is deliberately no recency boost. A ``boost_recent`` parameter
            was documented here as an active scoring behaviour and referenced
            nowhere in the body, so the signature promised a feature the search
            never had (issue #690). Ranking is pure kNN cosine.
        """
        from app.services.opensearch_service import _speaker_org_filter_clauses
        from app.services.opensearch_service import opensearch_client

        # Convert embedding to list format for OpenSearch
        if isinstance(embedding, torch.Tensor):
            embedding_list = embedding.cpu().numpy().tolist()
        elif isinstance(embedding, np.ndarray):
            embedding_list = embedding.tolist()
        else:
            embedding_list = embedding

        # Build advanced query with multiple filters (incl. tenant gate)
        must_filters = [
            {"term": {"user_id": user_id}},
            *_speaker_org_filter_clauses(organization_id, user_ids=[user_id]),
            exclude_unavailable_clause(),  # a trashed file's voices are never matched (#1101)
        ]
        must_not_filters = []

        if exclude_ids:
            must_not_filters.append({"terms": {"speaker_id": exclude_ids}})

        # Simple kNN query - more reliable than script_score
        search_body = {
            "size": max_results,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding_list,
                        "k": max_results,
                        "filter": {"bool": {"must": must_filters, "must_not": must_not_filters}},
                    }
                }
            },
            "_source": [
                "speaker_id",
                "user_id",
                "media_file_id",
                "profile_id",
                "created_at",
                "display_name",
                "confidence",
            ],
            # `min_score` is compared against the index's `cosinesimil` score,
            # which is `(1 + cosine) / 2` — NOT raw cosine. Writing a raw-cosine
            # value straight in here gates at roughly half the named similarity.
            "min_score": opensearch_score_from_raw_cosine(min_raw_cosine),
        }

        if opensearch_client is None:
            logger.warning("OpenSearch client is not available")
            return []

        response = opensearch_client.search(index=index_name, body=search_body)

        # Convert OpenSearch cosinesimil scores to raw cosine similarity
        results = []
        for hit in response.get("hits", {}).get("hits", []):
            similarity = raw_cosine_from_opensearch_score(hit["_score"])

            result = {
                "similarity": similarity,
                **hit["_source"],
            }
            results.append(result)

        logger.info(
            f"OpenSearch kNN search returned {len(results)} results "
            f"above raw cosine {min_raw_cosine}"
        )
        return results
