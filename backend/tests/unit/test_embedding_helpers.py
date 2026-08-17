"""Tests for ``app/utils/embedding_helpers.py`` (issue #474).

Pure numpy math, no DB/network — L2 normalization, mean aggregation, and the
incremental weighted-average update used across speaker embedding tasks, migration,
and consistency repair. Assertions are on exact/near-exact numeric values (norms,
specific vector components) rather than "no exception raised", since a wrong
normalization or a swapped weighting term would silently corrupt speaker matching.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.utils.embedding_helpers import aggregate_embeddings
from app.utils.embedding_helpers import l2_normalize
from app.utils.embedding_helpers import weighted_embedding_update


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


class TestWeightedEmbeddingUpdate:
    def test_first_update_from_count_zero_averages_equally(self):
        # old_count=0 means the "old" embedding contributes nothing to the sum, so the
        # weighted average collapses to new_embedding alone (before normalization).
        old = np.array([1.0, 0.0])
        new = np.array([0.0, 1.0])
        updated, new_count = weighted_embedding_update(old, new, old_count=0)
        assert new_count == 1
        assert np.allclose(updated, [0.0, 1.0])

    def test_incremental_update_weights_by_prior_count(self):
        # old represents the average of 4 prior samples; adding a 5th should move the
        # result only 1/5 of the way toward `new`.
        old = np.array([1.0, 0.0])
        new = np.array([0.0, 1.0])
        updated, new_count = weighted_embedding_update(old, new, old_count=4)
        assert new_count == 5
        raw_weighted = (old * 4 + new) / 5  # [0.8, 0.2]
        expected = l2_normalize(raw_weighted)
        assert np.allclose(updated, expected)
        assert np.isclose(np.linalg.norm(updated), 1.0)

    def test_count_increments_by_exactly_one(self):
        old = np.array([1.0, 0.0])
        new = np.array([1.0, 0.0])
        _, new_count = weighted_embedding_update(old, new, old_count=99)
        assert new_count == 100

    def test_identical_embeddings_leave_direction_unchanged(self):
        old = np.array([0.6, 0.8])
        new = np.array([0.6, 0.8])
        updated, _ = weighted_embedding_update(old, new, old_count=10)
        assert np.allclose(updated, [0.6, 0.8])
