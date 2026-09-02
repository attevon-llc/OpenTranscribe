"""``SimilarityService`` must report one cosine space from every method (issue #690).

Cosine lives in ``[-1, 1]``. ``cosine_similarity`` and ``batch_cosine_similarity``
used to clamp to ``[0, 1]`` while ``opensearch_similarity_search`` — a method on the
*same class* — returned the unclamped ``(2 * score) - 1`` conversion. Two callers of
one class therefore got different ranges for the same quantity, and every negative
collapsed to exactly ``0.0``, making "opposite" and "orthogonal" indistinguishable.

The negative region is populated in practice, not theoretical, and OpenSearch really
does return it. Measured against a live ``lucene``/``cosinesimil`` index at five
points — cosine ``-1.0 -> 0.0``, ``-0.6 -> 0.2``, ``0.0 -> 0.5``, ``+0.6 -> 0.8``,
``+1.0 -> 1.0`` — so ``(1 + cos) / 2`` is exact, ``2 * score - 1`` inverts it
exactly, and a negative cosine comes back **unclamped**. Independently,
``backend/app/services/CLAUDE.md`` records a measured different-speaker mean of
**0.094** over 134 AMI ground-truth windows, *with real negatives*.

These tests pin the restored invariant — the torch path and the OpenSearch path
agree on the same vector pair, across the whole domain — and pin the removal of the
``boost_recent`` parameter, which documented a recency boost that was referenced
nowhere in the body.
"""

import ast
import inspect
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from app.services.similarity_service import SimilarityService

pytestmark = pytest.mark.unit

# The query vector every pair below is scored against.
UNIT_X = np.array([1.0, 0.0, 0.0], dtype=np.float64)

# (label, other vector, exact cosine against UNIT_X). Hand-computed: every vector
# is unit-norm, so the cosine is just its x component.
COSINE_PAIRS = [
    ("opposite", [-1.0, 0.0, 0.0], -1.0),
    ("mostly opposite", [-0.6, 0.8, 0.0], -0.6),
    ("orthogonal", [0.0, 1.0, 0.0], 0.0),
    ("mostly aligned", [0.6, 0.8, 0.0], 0.6),
    ("identical", [1.0, 0.0, 0.0], 1.0),
]
NEGATIVE_PAIRS = [pair for pair in COSINE_PAIRS if pair[2] < 0.0]


class _FakeCosinesimilIndex:
    """Stands in for an OpenSearch index mapped ``"space_type": "cosinesimil"``.

    Scores documents the way OpenSearch does — ``(1 + cosine) / 2`` — and applies
    ``min_score`` in that same space. The arithmetic is spelled out here rather
    than taken from :mod:`app.utils.cosine_space` on purpose: a fake that shares
    the function under test cannot disagree with it.
    """

    def __init__(self, raw_cosines: list[float]) -> None:
        self._raw_cosines = list(raw_cosines)
        self.last_body: dict | None = None

    def search(self, index: str, body: dict) -> dict:
        self.last_body = body
        min_score = body.get("min_score")
        hits = []
        for speaker_id, raw_cosine in enumerate(self._raw_cosines, start=1):
            score = (1.0 + raw_cosine) / 2.0
            if min_score is not None and score < min_score:
                continue
            hits.append({"_score": score, "_source": {"speaker_id": speaker_id}})
        return {"hits": {"hits": hits}}


@pytest.fixture
def fake_index(monkeypatch):
    """Install a fake cosinesimil index and hand the factory back to the test."""
    from app.services import opensearch_service

    def _install(raw_cosines: list[float]) -> _FakeCosinesimilIndex:
        index = _FakeCosinesimilIndex(raw_cosines)
        monkeypatch.setattr(opensearch_service, "opensearch_client", index)
        return index

    return _install


# ---------------------------------------------------------------------------
# The clamp: negatives must survive both in-process paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "other", "expected"), NEGATIVE_PAIRS)
def test_cosine_similarity_preserves_a_negative_cosine(label, other, expected):
    """A [0, 1] clamp reported 0.0 here, erasing the sign and the magnitude."""
    result = SimilarityService.cosine_similarity(UNIT_X, np.array(other, dtype=np.float64))

    assert result == pytest.approx(expected, abs=1e-6), label
    assert result < 0.0, f"{label}: the negative half of the cosine domain was clamped away"


def test_batch_cosine_similarity_preserves_negative_cosines():
    """The batch path clamped identically and must move with its sibling."""
    targets = [np.array(other, dtype=np.float64) for _, other, _ in COSINE_PAIRS]

    results = SimilarityService.batch_cosine_similarity(UNIT_X, targets)

    assert len(results) == len(COSINE_PAIRS)
    assert results == [pytest.approx(expected, abs=1e-6) for _, _, expected in COSINE_PAIRS]
    assert results[0] < 0.0, "cosine -1.0 came back non-negative"
    assert results[1] < 0.0, "cosine -0.6 came back non-negative"


def test_the_two_in_process_methods_report_the_same_value():
    """One class, one space: the scalar and batch paths must not diverge."""
    targets = [np.array(other, dtype=np.float64) for _, other, _ in COSINE_PAIRS]

    batched = SimilarityService.batch_cosine_similarity(UNIT_X, targets)
    scalar = [SimilarityService.cosine_similarity(UNIT_X, target) for target in targets]

    assert len(scalar) == len(COSINE_PAIRS)
    assert batched == [pytest.approx(value, abs=1e-6) for value in scalar]


def test_the_clamp_still_bounds_float_rounding_to_the_cosine_domain():
    """Removing the [0, 1] clamp must not remove bounding — only widen it."""
    aligned = SimilarityService.cosine_similarity(UNIT_X, UNIT_X)
    opposite = SimilarityService.cosine_similarity(UNIT_X, -UNIT_X)

    assert aligned <= 1.0
    assert opposite >= -1.0
    assert aligned == pytest.approx(1.0, abs=1e-6)
    assert opposite == pytest.approx(-1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# The invariant being restored: both paths agree on the SAME vector pair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "other", "expected"), COSINE_PAIRS)
def test_the_torch_path_and_the_opensearch_path_agree_on_one_vector_pair(
    fake_index, label, other, expected
):
    """The actual defect: the same pair scored two different numbers.

    The in-process torch cosine and the ``cosinesimil`` round trip are two
    implementations of one quantity. Before the fix they agreed on the positive
    half and disagreed by the whole magnitude on the negative half — the torch
    path saying ``0.0`` where OpenSearch said ``-0.6``.
    """
    other_vector = np.array(other, dtype=np.float64)

    in_process = SimilarityService.cosine_similarity(UNIT_X, other_vector)

    # The index holds one document, at exactly this pair's true cosine.
    index = fake_index([expected])
    results = SimilarityService.opensearch_similarity_search(
        embedding=UNIT_X.tolist(),
        user_id=1,
        index_name="speakers",
        # -1.0 admits the whole domain, so the gate cannot hide a disagreement.
        min_raw_cosine=-1.0,
    )

    assert index.last_body is not None, f"{label}: no query reached OpenSearch"
    assert len(results) == 1, f"{label}: the candidate was gated out before it could be compared"
    assert results[0]["similarity"] == pytest.approx(in_process, abs=1e-6), (
        f"{label}: the torch path and the OpenSearch path report different cosines"
    )
    assert in_process == pytest.approx(expected, abs=1e-6), label


# ---------------------------------------------------------------------------
# boost_recent: a documented scoring feature that never existed
# ---------------------------------------------------------------------------


def test_opensearch_similarity_search_has_no_boost_recent_parameter():
    params = inspect.signature(SimilarityService.opensearch_similarity_search).parameters

    # Control: prove we are inspecting the function we think we are, so the
    # absence assertion below cannot pass against the wrong object.
    assert "min_raw_cosine" in params
    assert "boost_recent" not in params


def test_passing_boost_recent_is_now_rejected(fake_index):
    """An old call site keeping the kwarg fails loudly instead of being ignored.

    Bound through a ``Callable[..., object]`` deliberately: the call this test
    makes is one the *static* signature forbids — that is the property under
    test — so spelling it directly would be a type error rather than a runtime
    assertion. The rejection itself is real, and mypy still checks every other
    call to this function in the tree.
    """
    fake_index([0.9])
    search: Callable[..., object] = SimilarityService.opensearch_similarity_search

    with pytest.raises(TypeError, match="boost_recent"):
        search(embedding=[0.0] * 8, user_id=1, boost_recent=True)


def test_the_search_still_works_without_it(fake_index):
    """The control for the test above: the same call minus the dead kwarg passes."""
    fake_index([0.9])

    results = SimilarityService.opensearch_similarity_search(
        embedding=[0.0] * 8,
        user_id=1,
    )

    assert len(results) == 1
    assert results[0]["similarity"] == pytest.approx(0.9)


def _names_boost_recent_in_code(source: str) -> bool:
    """True when ``boost_recent`` appears as CODE — a parameter, argument or name.

    Deliberately AST-based rather than a substring search: this file and
    ``similarity_service``'s own docstring both mention the removed parameter by
    name to explain why it is gone, and prose about a deletion is not a call site.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "boost_recent":
            return True
        if isinstance(node, ast.arg) and node.arg == "boost_recent":
            return True
        if isinstance(node, ast.Name) and node.id == "boost_recent":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "boost_recent":
            return True
    return False


def test_the_boost_recent_detector_fires_on_real_code():
    """Guard the guard: a scanner that matches nothing passes everything."""
    assert _names_boost_recent_in_code("search(user_id=1, boost_recent=True)")
    assert _names_boost_recent_in_code("def search(embedding, boost_recent=True): pass")
    assert _names_boost_recent_in_code("x = boost_recent")
    # ...and stays clean on prose that merely names it.
    assert not _names_boost_recent_in_code('"""There is no boost_recent parameter."""')


def test_no_production_call_site_names_boost_recent():
    """A dead parameter is only gone once no code under ``app/`` references it."""
    app_root = Path(__file__).resolve().parents[2] / "app"
    sources = sorted(app_root.rglob("*.py"))

    assert len(sources) > 100, f"the scan found only {len(sources)} files — wrong root?"
    assert app_root / "services" / "similarity_service.py" in sources

    offenders = [
        str(path.relative_to(app_root))
        for path in sources
        if _names_boost_recent_in_code(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
