"""The speaker auto-accept gate must be written in raw-cosine space (issue #674).

OpenSearch's Lucene ``cosinesimil`` space reports ``(1 + cosine) / 2``, never raw
cosine. A raw-cosine threshold handed straight to ``min_score`` therefore gates at
half the value it names: ``min_score=0.75`` admits everything at raw cosine
``>= 0.50`` — the band ``core.constants`` classifies as *requires validation* — and
``_propagate_profile_assignment`` writes those rows ``verified=True``.

The repo-wide cosine invariant covers score *reads*; these tests cover the *write*
direction, which no read-site audit can reach.
"""

from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.core.constants import SPEAKER_CONFIDENCE_HIGH
from app.core.constants import SPEAKER_CONFIDENCE_MEDIUM
from app.services.similarity_service import SimilarityService
from app.utils.cosine_space import opensearch_score_from_raw_cosine
from app.utils.cosine_space import raw_cosine_from_opensearch_score

pytestmark = pytest.mark.unit


class _FakeCosinesimilIndex:
    """Stands in for an OpenSearch index mapped ``"space_type": "cosinesimil"``.

    Scores documents the way OpenSearch does — ``(1 + cosine) / 2`` — and applies
    ``min_score`` in that same space, so what these tests measure is the gate the
    real server would apply. The arithmetic is spelled out here rather than taken
    from :mod:`app.utils.cosine_space` on purpose: a fake that shares the function
    under test cannot disagree with it.
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


def _search(min_raw_cosine: float):
    return SimilarityService.opensearch_similarity_search(
        embedding=[0.0] * 8,
        user_id=1,
        index_name="speakers",
        min_raw_cosine=min_raw_cosine,
    )


def test_a_raw_cosine_gate_of_075_is_sent_to_opensearch_as_min_score_0875(fake_index):
    """The number on the wire is in cosinesimil space, not raw-cosine space."""
    index = fake_index([0.95])

    _search(SPEAKER_CONFIDENCE_HIGH)

    assert SPEAKER_CONFIDENCE_HIGH == 0.75, "the constant this test is calibrated against moved"
    assert index.last_body is not None, "no query reached OpenSearch"
    assert index.last_body["min_score"] == pytest.approx(0.875)


def test_a_candidate_at_raw_cosine_050_is_not_auto_accepted(fake_index):
    """Raw cosine 0.50 is the 'requires validation' band — it must not be admitted."""
    index = fake_index([SPEAKER_CONFIDENCE_MEDIUM, 0.80])

    results = _search(SPEAKER_CONFIDENCE_HIGH)

    assert [hit["speaker_id"] for hit in results] == [2]
    assert results[0]["similarity"] == pytest.approx(0.80)
    assert index.last_body["min_score"] == pytest.approx(0.875)


def test_a_candidate_at_raw_cosine_080_is_auto_accepted(fake_index):
    """The gate must not be so tight it rejects a genuine high-confidence match."""
    fake_index([0.80])

    results = _search(SPEAKER_CONFIDENCE_HIGH)

    assert len(results) == 1
    assert results[0]["similarity"] == pytest.approx(0.80)


def test_the_returned_similarity_is_raw_cosine_not_the_opensearch_score(fake_index):
    """The read direction stays converted — the fix must not double-convert."""
    fake_index([0.90, 0.76])

    results = _search(0.70)

    assert [pytest.approx(hit["similarity"]) for hit in results] == [
        pytest.approx(0.90),
        pytest.approx(0.76),
    ]


def test_cosine_space_helpers_convert_in_both_directions():
    assert opensearch_score_from_raw_cosine(0.75) == pytest.approx(0.875)
    assert opensearch_score_from_raw_cosine(0.50) == pytest.approx(0.75)
    assert opensearch_score_from_raw_cosine(-1.0) == pytest.approx(0.0)
    assert raw_cosine_from_opensearch_score(0.875) == pytest.approx(0.75)
    assert raw_cosine_from_opensearch_score(0.75) == pytest.approx(0.50)
    assert raw_cosine_from_opensearch_score(
        opensearch_score_from_raw_cosine(0.42)
    ) == pytest.approx(0.42)


class _StubQuery:
    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return None

    def all(self):
        return []


class _StubSession:
    """Minimal Session stand-in: the propagation path under test never reaches a row."""

    def query(self, *args, **kwargs):
        return _StubQuery()

    def commit(self):
        raise AssertionError("nothing should be committed when no candidate matches")

    def rollback(self):
        raise AssertionError("propagation raised instead of completing")


def test_profile_propagation_gates_at_the_high_confidence_constant(monkeypatch):
    """The auto-accept caller declares its space and reuses the constant.

    ``_propagate_profile_assignment`` swallows every exception, so the capture is
    asserted after the call: an empty capture means the search was never reached
    and the test fails rather than passing vacuously.
    """
    from app.services.speaker_matching_service import SpeakerMatchingService

    captured: dict = {}

    def fake_similarity_search(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        SimilarityService, "opensearch_similarity_search", staticmethod(fake_similarity_search)
    )
    monkeypatch.setattr(
        SpeakerMatchingService,
        "_get_speaker_embedding_for_propagation",
        lambda self, speaker_id: ([0.1] * 8, "stub-uuid"),
    )

    service = SpeakerMatchingService(cast("Session", _StubSession()), embedding_service=None)
    service._propagate_profile_assignment(matched_speaker_id=1, profile_id=2, user_id=3)

    assert captured, "the propagation path never reached the similarity search"
    assert captured["min_raw_cosine"] == SPEAKER_CONFIDENCE_HIGH
    assert "threshold" not in captured, "a bare 'threshold' kwarg names no similarity space"
