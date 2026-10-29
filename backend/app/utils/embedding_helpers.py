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
