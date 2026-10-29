"""Tests for ``app/utils/embedding_helpers.py`` (issue #474).

Pure numpy math, no DB/network — L2 normalization and mean aggregation used across
speaker embedding tasks, migration, and consistency repair. Assertions are on exact/near-exact numeric values (norms,
specific vector components) rather than "no exception raised", since a wrong
normalization or a swapped weighting term would silently corrupt speaker matching.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.utils.embedding_helpers import aggregate_embeddings
from app.utils.embedding_helpers import l2_normalize


class TestL2Normalize:
    def test_normalizes_to_unit_length(self):
        vec = np.array([3.0, 4.0])
        result = l2_normalize(vec)
        assert np.isclose(np.linalg.norm(result), 1.0)
        assert np.allclose(result, [0.6, 0.8])

    def test_already_unit_vector_is_unchanged(self):
        vec = np.array([1.0, 0.0, 0.0])
        result = l2_normalize(vec)
        assert np.allclose(result, vec)

    def test_zero_vector_is_returned_unchanged_not_divided(self):
        # norm == 0 -> division would produce NaNs; the function must short-circuit.
        vec = np.zeros(4)
        result = l2_normalize(vec)
        assert np.array_equal(result, vec)
        assert not np.isnan(result).any()

    def test_negative_components_preserve_direction(self):
        vec = np.array([-3.0, 4.0])
        result = l2_normalize(vec)
        assert np.isclose(np.linalg.norm(result), 1.0)
        assert np.allclose(result, [-0.6, 0.8])


class TestAggregateEmbeddings:
    def test_mean_of_identical_vectors_is_normalized_form_of_that_vector(self):
        vecs = [np.array([3.0, 4.0]), np.array([3.0, 4.0])]
        result = aggregate_embeddings(vecs)
        assert np.allclose(result, [0.6, 0.8])

    def test_mean_of_orthogonal_unit_vectors(self):
        vecs = [np.array([1.0, 0.0]), np.array([0.0, 1.0])]
        result = aggregate_embeddings(vecs)
        # raw mean is [0.5, 0.5], normalized -> [1/sqrt2, 1/sqrt2]
        expected = 1.0 / np.sqrt(2.0)
        assert np.allclose(result, [expected, expected])
        assert np.isclose(np.linalg.norm(result), 1.0)

    def test_single_embedding_returns_its_normalized_form(self):
        vecs = [np.array([6.0, 8.0])]
        result = aggregate_embeddings(vecs)
        assert np.allclose(result, [0.6, 0.8])

    def test_empty_list_raises_value_error(self):
        with pytest.raises(ValueError, match="empty"):
            aggregate_embeddings([])

    def test_vectors_that_cancel_out_produce_a_zero_result_without_crashing(self):
        vecs = [np.array([1.0, 0.0]), np.array([-1.0, 0.0])]
        result = aggregate_embeddings(vecs)
        assert np.allclose(result, [0.0, 0.0])
