"""PyAnnote v4 utility functions for speaker embeddings and overlap detection.

Provides shared utilities used by the transcription pipeline's diarizer:
- build_native_embeddings(): Speaker label -> L2-normalized centroid mapping
- extract_overlap_regions(): Overlapping speech region extraction
"""

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def build_native_embeddings(
    diarization: Any,
    centroids: np.ndarray | None,
) -> dict[str, np.ndarray]:
    """Build speaker label -> L2-normalized centroid mapping from PyAnnote output.

    Uses vectorized numpy L2-normalization for efficiency.

    Args:
        diarization: PyAnnote Annotation (exclusive diarization).
        centroids: ndarray of shape (num_speakers, embedding_dim) from PyAnnote,
            or None if OracleClustering was used.

    Returns:
        Dict mapping speaker labels to L2-normalized centroid vectors.
        Empty dict on any failure.
    """
    if centroids is None:
        logger.debug("No centroids returned by pipeline (OracleClustering?)")
        return {}

    try:
        labels = diarization.labels()
        if not labels:
            logger.warning("Diarization produced no speaker labels for centroid mapping")
            return {}

        # Truncate to available rows if labels exceed centroid rows
        n_usable = min(len(labels), centroids.shape[0])
        if n_usable < len(labels):
            dropped_labels = labels[n_usable:]
            logger.warning(
                f"Only {n_usable} centroid rows for {len(labels)} labels, "
                f"dropping speakers without centroids: {dropped_labels}"
            )

        # Vectorized L2-normalization of all centroids at once
        usable = centroids[:n_usable]
        norms = np.linalg.norm(usable, axis=1, keepdims=True)
        # Mask out near-zero vectors to avoid division by zero
        # Use [:, 0] instead of squeeze() to always produce 1-d array
        # (squeeze() on shape (1,1) gives 0-dim scalar, breaking indexing)
        valid_mask = norms[:, 0] > 1e-8
        normalized = np.where(norms > 1e-8, usable / norms, 0.0)

        embeddings = {labels[i]: normalized[i] for i in range(n_usable) if valid_mask[i]}

        skipped_labels = [labels[i] for i in range(n_usable) if not valid_mask[i]]
        if skipped_labels:
            logger.warning(
                f"Skipped {len(skipped_labels)} speakers with zero-norm centroids: {skipped_labels}"
            )

        logger.info(
            f"Built {len(embeddings)} native speaker embeddings "
            f"(dim={centroids.shape[1]}) from {len(labels)} labels"
        )
        return embeddings

    except (ValueError, IndexError, KeyError, TypeError, AttributeError) as e:
        logger.warning(f"Failed to build native embeddings: {e}", exc_info=True)
        return {}


def extract_overlap_regions(
    diarization: Any,
    min_duration: float = 0.25,
) -> list[dict[str, float]]:
    """Extract overlapping speech regions from a PyAnnote diarization annotation.

    Args:
        diarization: PyAnnote Annotation object with full diarization.
        min_duration: Minimum overlap region duration in seconds.
            Regions shorter than this are filtered as noise.

    Returns:
        List of overlap regions as dicts with 'start' and 'end' keys.
    """
    if not hasattr(diarization, "get_overlap"):
        return []

    try:
        # Build regions via list comprehension
        all_regions = [{"start": seg.start, "end": seg.end} for seg in diarization.get_overlap()]

        # Filter by minimum duration in a single pass
        if min_duration > 0:
            overlaps = [o for o in all_regions if (o["end"] - o["start"]) >= min_duration]
            filtered = len(all_regions) - len(overlaps)
            if filtered > 0:
                logger.debug(f"Filtered {filtered} overlap regions below {min_duration}s threshold")
            return overlaps

        return all_regions
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning(f"Could not extract overlap regions: {e}", exc_info=True)
        return []
