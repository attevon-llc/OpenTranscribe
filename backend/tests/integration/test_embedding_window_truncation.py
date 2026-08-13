"""The digest size constant, checked against the model that will actually embed it.

``app/services/ingest_artifacts/sizing.py`` derives the digest section budget from a
measurement of the deployed ``all-MiniLM-L6-v2``: it truncates at **128 wordpieces**, not
the ~256 the #383 review addendum (G8) assumed. That number is the difference between a
digest whose vector represents the whole digest and one whose vector represents its
opening — and the failure is completely silent, because a truncated embedding is a
perfectly valid embedding of the wrong text.

So the constant needs a test that talks to the model. Everything here runs against a real
OpenSearch ML Commons deployment and **skips loudly** when none is reachable; a stand-in
could only confirm that we believe the number.

Point at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 pytest backend/tests/integration/test_embedding_window_truncation.py \\
        -m integration

Reproduce the measurement by hand with ``scripts/measure_embedding_window.py``.
"""

from __future__ import annotations

import math
import os

import pytest

from app.services.ingest_artifacts import sizing
from app.services.ingest_artifacts.index_mapping import build_embedding_text

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). The whole point of this suite is "
            "the deployed model's real truncation; a stand-in cannot supply it."
        ),
    ),
]

#: Rare enough that any model able to see it must move its vector.
TAIL = "zebra kaleidoscope quantum marmalade"

#: Common single-wordpiece words, so word count tracks wordpiece count closely.
FILLER = [
    "the",
    "project",
    "team",
    "met",
    "to",
    "review",
    "the",
    "budget",
    "and",
    "the",
    "schedule",
    "for",
    "the",
    "next",
    "release",
    "we",
    "agreed",
    "that",
    "the",
    "plan",
    "is",
    "fine",
    "and",
    "that",
    "the",
    "work",
    "will",
    "start",
    "on",
    "monday",
]


@pytest.fixture(scope="module")
def embedder():
    """A callable ``(texts) -> vectors`` against the deployed model, or a skip."""
    from app.services.search.indexing_service import get_opensearch_client

    client = get_opensearch_client()
    if client is None:
        pytest.skip("OpenSearch client unavailable")
    assert client is not None  # narrowing for mypy; pytest.skip already returned

    # No try/except around the request: the module gate already established that
    # OpenSearch is reachable, so a transport error here is a real failure and must be
    # seen as one. Only the *absence of a model* is a legitimate skip, and that is an
    # empty hit list, not an exception.
    found = client.transport.perform_request(
        "POST",
        "/_plugins/_ml/models/_search",
        body={"query": {"match_all": {}}, "size": 1, "_source": ["model_id"]},
    )
    hits = found["hits"]["hits"]
    if not hits:
        pytest.skip(
            "OpenSearch is up but no ML Commons model is deployed; there is nothing to "
            "measure the truncation of"
        )
    model_id = hits[0]["_source"]["model_id"]

    def _embed(texts: list[str]) -> list[list[float]]:
        response = client.transport.perform_request(
            "POST",
            f"/_plugins/_ml/_predict/text_embedding/{model_id}",
            body={
                "text_docs": texts,
                "return_number": True,
                "target_response": ["sentence_embedding"],
            },
        )
        return [r["output"][0]["data"] for r in response["inference_results"]]

    return _embed


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def _words(count: int) -> str:
    return " ".join((FILLER * (count // len(FILLER) + 1))[:count])


def test_the_model_does_truncate_beyond_the_measured_window(embedder):
    """The negative control for every other test here.

    Without it, "a max-length section is not truncated" would pass just as happily against
    a model that never truncates anything — which is exactly the assertion-against-an-empty
    -index failure mode issue #431 exists for.
    """
    over = _words(sizing.EMBEDDING_CONTENT_WORDPIECES + 60)
    a, b = embedder([over, f"{over} {TAIL}"])
    assert _cosine(a, b) >= 0.999999, (
        "appending distinctive words to an over-long text still moved the vector — the "
        "deployed model's window is LARGER than sizing.py assumes, so the derived digest "
        "budget is stale (re-run scripts/measure_embedding_window.py)"
    )


def test_text_inside_the_window_is_not_truncated(embedder):
    """The other side of the control: below the limit, appending must change the vector."""
    under = _words(40)
    a, b = embedder([under, f"{under} {TAIL}"])
    assert _cosine(a, b) < 0.99, "the model appears to ignore appended text entirely"


def test_the_declared_window_matches_the_deployed_model(embedder):
    """Binary-search the real cutoff and compare it to the constant sizing.py derives from.

    A tolerance of ±4 words absorbs the filler vocabulary's wordpiece ratio without
    absorbing the 2x error the addendum's 256 would represent.
    """
    low, high = 1, 400
    while low + 1 < high:
        mid = (low + high) // 2
        prefix = _words(mid)
        a, b = embedder([prefix, f"{prefix} {TAIL}"])
        if _cosine(a, b) >= 0.999999:
            high = mid
        else:
            low = mid

    assert abs(high - sizing.EMBEDDING_CONTENT_WORDPIECES) <= 4, (
        f"the deployed model truncates at ~{high} single-wordpiece words but sizing.py "
        f"declares a {sizing.EMBEDDING_CONTENT_WORDPIECES}-wordpiece content budget"
    )


def test_a_maximum_length_digest_document_survives_whole(embedder):
    """The property the constant exists to guarantee, end to end.

    A worst-case section — the hard word cap, a long title, a six-name roster — must still
    be entirely inside the window, i.e. appending to it MUST move its vector.
    """
    body = _words(sizing.DIGEST_SECTION_MAX_WORDS)
    embedding_text = build_embedding_text(
        title="Quarterly planning and budget review",
        recorded_at="2026-08-12",
        roster=["Dana", "Marcus", "Priya", "Sam", "Alex", "Robin"],
        body=body,
    )
    a, b = embedder([embedding_text, f"{embedding_text} {TAIL}"])
    assert _cosine(a, b) < 0.999999, (
        "a maximum-length digest document is already at the truncation boundary — its "
        "tail is not contributing to its own embedding"
    )


def test_the_wordpiece_estimate_is_conservative_on_real_transcript_prose(embedder):
    """``estimate_wordpieces`` must over-count, never under-count.

    It is calibrated on raw ASR text, which carries more punctuation tokens than digest
    prose. Under-counting would let a section past the cap and truncate silently.
    """
    sample = (
        "We agreed to move the launch to November and revisit the marketing budget once "
        "engineering confirms the date, because the awareness campaign only pays for "
        "itself if it runs into the launch window itself."
    )
    estimated = sizing.estimate_wordpieces(sample)
    assert estimated >= len(sample.split()), "the estimate is below one piece per word"
    assert sizing.fits_embedding_window(sample) is (
        estimated + sizing.HEADER_WORDPIECE_RESERVE <= sizing.EMBEDDING_CONTENT_WORDPIECES
    )
    # And the model agrees this length is inside the window.
    a, b = embedder([sample, f"{sample} {TAIL}"])
    assert _cosine(a, b) < 0.999999
