"""Shared embedding math utilities.

L2 normalization, aggregation, and similarity helpers used across
speaker embedding tasks, migration, and consistency repair.
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2-normalize a vector. Returns zero vector if norm is 0."""
    norm = np.linalg.norm(vec)
    if norm > 0:
        normalized: np.ndarray = vec / norm
        return normalized
    return vec


def aggregate_embeddings(embeddings: list[np.ndarray]) -> np.ndarray:
    """Compute L2-normalized mean of a list of embeddings."""
    if not embeddings:
        raise ValueError("Cannot aggregate empty embedding list")
    mean_vec = np.mean(np.array(embeddings), axis=0)
    return l2_normalize(mean_vec)


def weighted_embedding_update(
    old_embedding: np.ndarray,
    new_embedding: np.ndarray,
    old_count: int,
) -> tuple[np.ndarray, int]:
    """Compute weighted average for incremental embedding update.

    Returns:
        Tuple of (updated_embedding, new_count).
    """
    new_count = old_count + 1
    weighted = (old_embedding * old_count + new_embedding) / new_count
    return l2_normalize(weighted), new_count
