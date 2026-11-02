"""Cluster quality and tightest-pair scores, computed once for centroid refresh and re-cluster."""

import numpy as np
import pytest

from app.services.clustering.centroids import cluster_geometry_scores


def test_fewer_than_two_members_score_perfect():
    centroid = np.array([1.0, 0.0], dtype=np.float32)

    assert cluster_geometry_scores(None, centroid) == (1.0, 1.0)
    assert cluster_geometry_scores(np.array([[0.3, 0.0]], dtype=np.float32), centroid) == (1.0, 1.0)


def test_two_orthogonal_members_around_their_mean():
    arr = np.array([[2.0, 0.0], [0.0, 3.0]], dtype=np.float32)
    centroid = np.array([1.0, 1.0], dtype=np.float32) / np.sqrt(2.0)

    quality, tightest = cluster_geometry_scores(arr, centroid)

    # Each unit-normalised member sits at 45 degrees from the centroid.
    assert quality == pytest.approx(np.sqrt(0.5), abs=1e-6)
    assert tightest == pytest.approx(0.0, abs=1e-6)


def test_tightest_pair_is_the_minimum_over_all_pairs():
    arr = np.array([[1.0, 0.0], [1.0, 0.1], [0.0, 1.0]], dtype=np.float32)
    centroid = np.array([1.0, 0.0], dtype=np.float32)

    _, tightest = cluster_geometry_scores(arr, centroid)

    assert tightest == pytest.approx(0.0, abs=1e-6)
