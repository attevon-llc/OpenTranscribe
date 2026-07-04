"""
Optimized embedding storage and retrieval service.

This service provides high-performance embedding operations optimized for production:
1. Batch operations for bulk embedding storage/retrieval
2. GPU-accelerated tensor operations using PyTorch
3. OpenSearch native bulk indexing and retrieval
4. Memory-efficient streaming for large datasets

No backwards compatibility - designed for modern infrastructure.
"""

import logging
import os
from collections.abc import Iterator
from typing import Any

import numpy as np
import torch

logger = logging.getLogger(__name__)


class OptimizedEmbeddingService:
    """High-performance embedding service with GPU acceleration and bulk operations."""

    # Use GPU only on GPU workers to avoid CUDA context leaks in CPU worker children.
    _is_gpu_worker = os.environ.get("PRELOAD_GPU_MODELS", "").lower() == "true"
    device = torch.device("cuda" if _is_gpu_worker and torch.cuda.is_available() else "cpu")

    @staticmethod
    def bulk_store_embeddings(
        embeddings_data: list[dict[str, Any]], index_name: str | None = None
    ) -> dict[str, Any]:
        """
        Store multiple embeddings in OpenSearch using bulk indexing for optimal performance.

        Args:
            embeddings_data: List of dicts with embedding data
                Format: [{"id": int, "embedding": list[float], "metadata": dict}, ...]
            index_name: OpenSearch index name

        Returns:
            Dict with storage statistics and any errors
        """
        from app.services.opensearch_service import opensearch_client

        if not embeddings_data:
            return {"stored": 0, "errors": []}

        if not opensearch_client:
            logger.error("OpenSearch client not initialized")
            return {"stored": 0, "errors": ["OpenSearch client not initialized"]}

        try:
            # Prepare bulk operations
            bulk_operations = []
            for data in embeddings_data:
                # Index operation
                bulk_operations.append({"index": {"_index": index_name, "_id": data["id"]}})

                # Document data
                doc = {"embedding": data["embedding"], **data.get("metadata", {})}
                bulk_operations.append(doc)

            # Execute bulk operation
            response = opensearch_client.bulk(
                body=bulk_operations,
                index=index_name,
                refresh=True,  # Make immediately searchable
            )

            # Process results
            errors = []
            stored_count = 0

            for item in response.get("items", []):
                if "index" in item:
                    if item["index"].get("status") in [200, 201]:
                        stored_count += 1
                    else:
                        errors.append(item["index"].get("error", "Unknown error"))

            logger.info(f"Bulk stored {stored_count} embeddings in {index_name}")

            return {"stored": stored_count, "errors": errors, "took_ms": response.get("took", 0)}

        except Exception as e:
            logger.error(f"Error in bulk embedding storage: {e}")
            return {"stored": 0, "errors": [str(e)]}

    @staticmethod
    def bulk_retrieve_embeddings(
        ids: list[int], index_name: str | None = None
    ) -> dict[int, list[float] | None]:
        """
        Retrieve multiple embeddings using OpenSearch mget for optimal performance.

        Args:
            ids: List of embedding IDs to retrieve
            index_name: OpenSearch index name

        Returns:
            Dict mapping ID to embedding vector (None if not found)
        """
        from app.services.opensearch_service import opensearch_client

        if not ids:
            return {}

        if not opensearch_client:
            logger.error("OpenSearch client not initialized")
            return {id_: None for id_ in ids}

        try:
            # Use multi-get for efficient bulk retrieval
            response = opensearch_client.mget(
                index=index_name, body={"ids": ids}, _source=["embedding"]
            )

            results = {}
            for doc in response.get("docs", []):
                doc_id = int(doc["_id"])
                if doc.get("found") and "_source" in doc:
                    results[doc_id] = doc["_source"].get("embedding")
                else:
                    results[doc_id] = None

            logger.info(f"Bulk retrieved {len([r for r in results.values() if r])} embeddings")
            return results

        except Exception as e:
            logger.error(f"Error in bulk embedding retrieval: {e}")
            return {id_: None for id_ in ids}

    @staticmethod
    def compute_embedding_statistics(
        embeddings: list[np.ndarray | torch.Tensor | list[float]],
    ) -> dict[str, Any]:
        """
        Compute statistical analysis of embeddings using GPU-accelerated operations.

        Args:
            embeddings: List of embedding vectors

        Returns:
            Dict with statistical metrics
        """
        if not embeddings:
            return {}

        try:
            # Convert all to torch tensors
            tensor_embeddings = []
            for emb in embeddings:
                if isinstance(emb, list):
                    tensor_embeddings.append(torch.tensor(emb, dtype=torch.float32))
                elif isinstance(emb, np.ndarray):
                    tensor_embeddings.append(torch.from_numpy(emb).float())
                else:
                    tensor_embeddings.append(emb.float())

            # Stack into matrix and move to device
            embeddings_matrix = torch.stack(tensor_embeddings).to(OptimizedEmbeddingService.device)

            # Compute statistics using vectorized operations
            stats = {
                "count": len(embeddings),
                "dimension": embeddings_matrix.shape[1],
                "mean_norm": torch.linalg.norm(embeddings_matrix, dim=1).mean().item(),
                "std_norm": torch.linalg.norm(embeddings_matrix, dim=1).std().item(),
                "mean_values": embeddings_matrix.mean(dim=0).cpu().numpy().tolist(),
                "std_values": embeddings_matrix.std(dim=0).cpu().numpy().tolist(),
                "min_values": embeddings_matrix.min(dim=0)[0].cpu().numpy().tolist(),
                "max_values": embeddings_matrix.max(dim=0)[0].cpu().numpy().tolist(),
            }

            # Compute pairwise similarity statistics if not too many embeddings
            if len(embeddings) <= 100:
                from app.services.similarity_service import SimilarityService

                similarity_matrix = SimilarityService.pairwise_similarity_matrix(embeddings_matrix)

                # Get upper triangle (exclude diagonal)
                mask = torch.triu(torch.ones_like(similarity_matrix), diagonal=1).bool()
                similarities = similarity_matrix[mask]

                stats.update(
                    {
                        "avg_pairwise_similarity": similarities.mean().item(),
                        "std_pairwise_similarity": similarities.std().item(),
                        "min_pairwise_similarity": similarities.min().item(),
                        "max_pairwise_similarity": similarities.max().item(),
                    }
                )

            return stats

        except Exception as e:
            logger.error(f"Error computing embedding statistics: {e}")
            return {"error": str(e)}

    @staticmethod
    def optimize_embeddings_for_search(
        embeddings: list[np.ndarray | torch.Tensor], target_dimension: int | None = None
    ) -> list[torch.Tensor]:
        """
        Optimize embeddings for search performance using GPU acceleration.

        This method can apply:
        - Normalization for better cosine similarity performance
        - Dimensionality reduction if specified
        - Quantization for memory efficiency

        Args:
            embeddings: List of embedding vectors
            target_dimension: Optional target dimension for reduction

        Returns:
            List of optimized embedding tensors
        """
        if not embeddings:
            return []

        try:
            # Convert to tensor matrix
            tensor_embeddings = []
            for emb in embeddings:
                if isinstance(emb, np.ndarray):
                    tensor_embeddings.append(torch.from_numpy(emb).float())
                else:
                    tensor_embeddings.append(emb.float())

            embeddings_matrix = torch.stack(tensor_embeddings).to(OptimizedEmbeddingService.device)

            # L2 normalize for optimal cosine similarity performance
            normalized = torch.nn.functional.normalize(embeddings_matrix, p=2, dim=1)

            # Optional dimensionality reduction using PCA
            if target_dimension and target_dimension < embeddings_matrix.shape[1]:
                # Use PyTorch's SVD for GPU-accelerated PCA
                centered = normalized - normalized.mean(dim=0, keepdim=True)
                _, _, v_matrix = torch.svd(centered.T)

                # Take top components
                components = v_matrix[:, :target_dimension]
                reduced = torch.matmul(normalized, components)

                # Renormalize after reduction
                optimized = torch.nn.functional.normalize(reduced, p=2, dim=1)

                logger.info(
                    f"Reduced embeddings from {embeddings_matrix.shape[1]} to {target_dimension} dimensions"
                )
            else:
                optimized = normalized

            # Convert back to list of tensors
            result: list[torch.Tensor] = [optimized[i].cpu() for i in range(optimized.shape[0])]

            return result

        except Exception as e:
            logger.error(f"Error optimizing embeddings: {e}")
            # Return empty list on error since we can't safely cast mixed types
            return []  # type: ignore[return-value]

    @staticmethod
    def stream_embeddings_from_index(
        index_name: str, user_id: int, batch_size: int = 1000
    ) -> Iterator[list[dict[str, Any]]]:
        """
        Stream embeddings from OpenSearch in batches for memory-efficient processing.

        Args:
            index_name: OpenSearch index name
            user_id: User ID to filter results
            batch_size: Number of embeddings per batch

        Yields:
            Batches of embedding documents
        """
        from app.services.opensearch_service import opensearch_client

        if not opensearch_client:
            logger.error("OpenSearch client not initialized")
            return

        try:
            # Use scroll API for efficient large dataset iteration
            response = opensearch_client.search(
                index=index_name,
                body={
                    "query": {"term": {"user_id": user_id}},
                    "size": batch_size,
                    "_source": ["embedding", "speaker_id", "created_at"],
                },
                scroll="5m",
            )

            scroll_id = response["_scroll_id"]

            while True:
                hits = response.get("hits", {}).get("hits", [])
                if not hits:
                    break

                # Yield batch of documents
                batch = []
                for hit in hits:
                    doc = {"id": hit["_id"], **hit["_source"]}
                    batch.append(doc)

                yield batch

                # Get next batch
                response = opensearch_client.scroll(scroll_id=scroll_id, scroll="5m")

            # Clean up scroll context
            opensearch_client.clear_scroll(scroll_id=scroll_id)

        except Exception as e:
            logger.error(f"Error streaming embeddings: {e}")
            return

    @staticmethod
    def get_performance_metrics() -> dict[str, Any]:
        """Get performance metrics for the embedding service."""
        return {
            "device": str(OptimizedEmbeddingService.device),
            "cuda_available": torch.cuda.is_available(),
            "cuda_memory_allocated_mb": torch.cuda.memory_allocated() / (1024**2)
            if torch.cuda.is_available()
            else 0,
            "cuda_memory_reserved_mb": torch.cuda.memory_reserved() / (1024**2)
            if torch.cuda.is_available()
            else 0,
            "tensor_operations": "GPU-accelerated" if torch.cuda.is_available() else "CPU",
        }
