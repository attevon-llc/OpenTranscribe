"""
High-performance similarity computation service using PyTorch and OpenSearch.

This service provides GPU-accelerated similarity calculations optimized for production:
1. OpenSearch native kNN with cosine similarity (primary method)
2. PyTorch GPU-accelerated vectorized operations
3. Advanced batching and tensor operations

No fallbacks - requires modern dependencies for optimal performance.
"""

import logging
import os
from typing import Any

import numpy as np
import torch
from torch.nn import functional as nn_functional

from app.utils.cosine_space import opensearch_score_from_raw_cosine
from app.utils.cosine_space import raw_cosine_from_opensearch_score

logger = logging.getLogger(__name__)


class SimilarityService:
    """GPU-accelerated similarity service leveraging PyTorch and OpenSearch."""

    # Use GPU only on GPU workers to avoid CUDA context leaks in CPU worker children.
    _is_gpu_worker = os.environ.get("PRELOAD_GPU_MODELS", "").lower() == "true"
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
            Cosine similarity score between 0 and 1
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

        # Ensure result is in valid range and return as Python float
        return float(max(0.0, min(1.0, similarity)))

    @staticmethod
    def batch_cosine_similarity(
        query_embedding: np.ndarray | torch.Tensor,
        target_embeddings: list[np.ndarray | torch.Tensor],
    ) -> list[float]:
        """
        GPU-accelerated batch cosine similarity using PyTorch.

        Processes all comparisons in parallel using vectorized tensor operations.

        Args:
            query_embedding: Single query embedding
            target_embeddings: List of target embeddings to compare against

        Returns:
            List of similarity scores in the same order as target_embeddings
        """
        if not target_embeddings:
            return []

        # Convert all inputs to torch tensors
        if isinstance(query_embedding, np.ndarray):
            query_tensor = torch.from_numpy(query_embedding).float()
        else:
            query_tensor = query_embedding.float()

        target_tensors = []
        for target in target_embeddings:
            if isinstance(target, np.ndarray):
                target_tensors.append(torch.from_numpy(target).float())
            else:
                target_tensors.append(target.float())

        # Stack targets into a single tensor and move to device
        targets_matrix = torch.stack(target_tensors).to(SimilarityService.device)
        query_tensor = query_tensor.to(SimilarityService.device)

        # Compute all similarities at once using PyTorch's vectorized operations
        similarities = nn_functional.cosine_similarity(
            query_tensor.unsqueeze(0).expand(targets_matrix.size(0), -1), targets_matrix, dim=1
        )

        # Convert to Python floats and ensure valid range
        result = []
        for sim in similarities:
            score = float(sim.item())
            result.append(max(0.0, min(1.0, score)))

        return result

    @staticmethod
    def opensearch_similarity_search(
        embedding: list[float] | np.ndarray | torch.Tensor,
        user_id: int,
        index_name: str = "speakers",
        min_raw_cosine: float = 0.7,
        max_results: int = 50,
        exclude_ids: list[int] | None = None,
        boost_recent: bool = True,
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
            boost_recent: Whether to boost recent embeddings in scoring
            organization_id: Active org id (None = personal) — tenant gate.

        Returns:
            List of similarity matches with raw-cosine scores and metadata
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
            *_speaker_org_filter_clauses(organization_id),
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

    @staticmethod
    def pairwise_similarity_matrix(
        embeddings: np.ndarray | torch.Tensor | list[np.ndarray],
    ) -> torch.Tensor:
        """
        Compute pairwise cosine similarity matrix for all embeddings.

        Highly optimized for large-scale similarity computations using GPU acceleration.

        Args:
            embeddings: List of embeddings or matrix of embeddings

        Returns:
            Similarity matrix where element [i,j] is similarity between embedding i and j
        """
        # Convert to tensor matrix
        if isinstance(embeddings, list):
            tensor_embeddings = torch.stack(
                [
                    torch.from_numpy(emb).float() if isinstance(emb, np.ndarray) else emb.float()
                    for emb in embeddings
                ]
            )
        elif isinstance(embeddings, np.ndarray):
            tensor_embeddings = torch.from_numpy(embeddings).float()
        else:
            tensor_embeddings = embeddings.float()

        tensor_embeddings = tensor_embeddings.to(SimilarityService.device)

        # Compute pairwise cosine similarity using matrix operations
        # This is extremely efficient on GPU for large matrices
        similarity_matrix = nn_functional.cosine_similarity(
            tensor_embeddings.unsqueeze(1), tensor_embeddings.unsqueeze(0), dim=2
        )

        return similarity_matrix

    @staticmethod
    def top_k_similar(
        query_embedding: np.ndarray | torch.Tensor,
        candidate_embeddings: np.ndarray | torch.Tensor | list[np.ndarray],
        k: int = 10,
        min_raw_cosine: float = 0.7,
    ) -> list[tuple[int, float]]:
        """
        Find top-k most similar embeddings using GPU-accelerated operations.

        Args:
            query_embedding: Query embedding
            candidate_embeddings: Pool of candidate embeddings
            k: Number of top results to return
            min_raw_cosine: Minimum **raw cosine** similarity, in ``[-1, 1]``.
                This path computes cosine directly in torch, so no
                ``cosinesimil`` conversion applies — the name says so rather than
                leaving the space to be inferred (issue #674).

        Returns:
            List of (index, similarity_score) tuples sorted by similarity (highest first)
        """
        # Convert query to tensor
        if isinstance(query_embedding, np.ndarray):
            query_tensor = torch.from_numpy(query_embedding).float()
        else:
            query_tensor = query_embedding.float()

        # Convert candidates to tensor matrix
        if isinstance(candidate_embeddings, list):
            candidates_tensor = torch.stack(
                [
                    torch.from_numpy(emb).float() if isinstance(emb, np.ndarray) else emb.float()
                    for emb in candidate_embeddings
                ]
            )
        elif isinstance(candidate_embeddings, np.ndarray):
            candidates_tensor = torch.from_numpy(candidate_embeddings).float()
        else:
            candidates_tensor = candidate_embeddings.float()

        # Move to device
        query_tensor = query_tensor.to(SimilarityService.device)
        candidates_tensor = candidates_tensor.to(SimilarityService.device)

        # Compute all similarities at once
        similarities = nn_functional.cosine_similarity(
            query_tensor.unsqueeze(0), candidates_tensor, dim=1
        )

        # Filter by threshold and get top-k
        valid_indices = torch.where(similarities >= min_raw_cosine)[0]
        if len(valid_indices) == 0:
            return []

        valid_similarities = similarities[valid_indices]
        top_k_values, top_k_indices_in_valid = torch.topk(
            valid_similarities, k=min(k, len(valid_similarities)), largest=True
        )

        # Convert back to original indices
        results = []
        for i, sim_val in zip(top_k_indices_in_valid, top_k_values, strict=True):
            original_idx = int(valid_indices[i].item())
            similarity = float(sim_val.item())
            results.append((original_idx, similarity))

        return results

    @staticmethod
    def get_device_info() -> dict[str, Any]:
        """Get information about the compute device being used."""
        return {
            "device": str(SimilarityService.device),
            "cuda_available": torch.cuda.is_available(),
            "cuda_device_count": torch.cuda.device_count() if torch.cuda.is_available() else 0,
            "gpu_memory_gb": torch.cuda.get_device_properties(0).total_memory / (1024**3)
            if torch.cuda.is_available()
            else 0,
        }
