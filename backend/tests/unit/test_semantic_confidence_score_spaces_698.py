"""Issue #698: ``semantic_confidence`` must not compare three score spaces to one threshold.

``SEARCH_SEMANTIC_HIGH_CONFIDENCE`` (0.010) was compared against ``best_score`` on THREE paths
that put wildly different numbers in that field:

| path | score space | typical magnitude |
|---|---|---|
| fused hybrid (RRF) | rank-fusion score | ~0.016 at rank 1 — what the threshold was tuned for |
| ``_bm25_leg_is_starved`` arm | raw ``cosinesimil`` | [0, 1], 0.5 = orthogonal |
| Phase 2 (``_build_search_hit_from_bucket``) | raw BM25 | ~1-30 |

**Chosen fix: option 2** — label confidence only on the fused RRF path (the one the threshold
was tuned for); omit the badge everywhere else. An absent badge beats a meaningless "high" one.

Each test below pins one path in isolation, per the issue's acceptance criteria: a single test
proves nothing, because the old shape passed trivially on two of three paths.
"""

from __future__ import annotations

import pytest

import app.services.search.hybrid_search_service as hss
from app.core.config import settings
from app.services.search.hybrid_search_service import HybridSearchService

SETTING_NAME = "SEARCH_SEMANTIC_HIGH_CONFIDENCE"


def _collapsed_response(score: float) -> dict:
    """A one-file collapsed response whose single inner hit is semantic-only.

    No ``highlight`` on the inner hit and an empty query mean ``_process_inner_hits`` records
    ``keyword_count == 0`` — the ``is_semantic_only`` branch under test — regardless of which
    OpenSearch body actually produced the score.
    """
    return {
        "hits": {
            "hits": [
                {
                    "_score": score,
                    "_source": {
                        "file_uuid": "file-under-test",
                        "file_id": 1,
                        "title": "Quarterly planning",
                        "speakers": [],
                        "tags": [],
                        "language": "en",
                        "content_type": "audio/wav",
                    },
                    "inner_hits": {
                        "segments": {
                            "hits": {
                                "total": {"value": 1},
                                "hits": [
                                    {
                                        "_score": score,
                                        "_source": {
                                            "content": "we should revisit the roadmap",
                                            "speaker": "SPEAKER_00",
                                            "start_time": 0.0,
                                            "end_time": 3.0,
                                            "chunk_index": 0,
                                        },
                                    }
                                ],
                            }
                        }
                    },
                }
            ]
        }
    }


class _CountingClient:
    """Fake OpenSearch client whose `.count()` answer drives `_bm25_leg_is_starved`."""

    def __init__(self, count: int) -> None:
        self._count = count
        self.count_calls: list[dict] = []

    def count(self, index: str, body: dict) -> dict:
        self.count_calls.append({"index": index, "body": body})
        return {"count": self._count}


@pytest.fixture(autouse=True)
def _fixed_threshold(monkeypatch):
    """Pin the setting so these tests don't depend on the coded default."""
    monkeypatch.setattr(settings, SETTING_NAME, 0.010)


def test_fused_rrf_path_labels_confidence_and_a_genuinely_high_score_reads_high():
    """CONTROL: the path the threshold was tuned for still works.

    A fused hybrid RRF hit with a score above threshold must still report "high" — the fix must
    not be indistinguishable from "always low".
    """
    hits, _ = HybridSearchService()._process_collapsed_results(
        _collapsed_response(0.02), "", is_fused_rrf=True
    )
    assert len(hits) == 1
    assert hits[0].semantic_confidence == "high"


def test_fused_rrf_path_reports_low_below_threshold():
    hits, _ = HybridSearchService()._process_collapsed_results(
        _collapsed_response(0.005), "", is_fused_rrf=True
    )
    assert len(hits) == 1
    assert hits[0].semantic_confidence == "low"


def test_starved_bm25_arm_is_reachable_and_never_labels_confidence(monkeypatch):
    """The `_bm25_leg_is_starved` arm is genuinely reached, not simulated.

    Drives `_build_collapsed_search_body` with a real (faked) OpenSearch client whose `.count()`
    reports zero keyword matches — the exact condition `_bm25_leg_is_starved` checks — and
    confirms the body it returns carries no fusion pipeline (`needs_pipeline is False`), which is
    the production signal `_search_with_collapse` uses to compute `is_fused_rrf`. A raw
    `cosinesimil` score of 0.5 (orthogonal — the worst possible match) is then fed through
    `_process_collapsed_results` with that same signal, and must NOT read "high": before the
    fix, 0.5 >= 0.010 always did.
    """
    svc = HybridSearchService()
    starved_client = _CountingClient(count=0)
    monkeypatch.setattr(hss, "get_opensearch_client", lambda: starved_client)
    monkeypatch.setattr(svc, "_get_neural_model_id", lambda: "fake-model-id")

    body, needs_pipeline = svc._build_collapsed_search_body(
        "space exploration",
        filters=[],
        page=1,
        page_size=20,
        has_speaker_filter=False,
        use_neural=True,
        sort_by="relevance",
        sort_order="desc",
    )

    # The starvation check really ran, and really found zero keyword matches.
    assert starved_client.count_calls, "the starvation pre-check (`.count()`) was never called"
    assert needs_pipeline is False, (
        "a starved keyword leg must route to the neural-only body with no fusion pipeline"
    )
    assert "hybrid" not in body["query"], "starved body must not be a fused hybrid query"

    # Orthogonal cosinesimil score: the worst possible match, and >= the 0.010 threshold, which
    # is exactly the shape that used to read "high" for essentially every unfused semantic hit.
    hits, _ = svc._process_collapsed_results(
        _collapsed_response(0.5), "space exploration", is_fused_rrf=needs_pipeline
    )
    assert len(hits) == 1
    assert hits[0].semantic_confidence == "", (
        "the raw-cosinesimil starved arm must never label confidence — score space is not RRF"
    )


def test_starved_bm25_arm_high_raw_cosine_still_unlabeled(monkeypatch):
    """Even a strong raw-cosine match (0.9, i.e. a real cosine of 0.8) gets no badge here.

    A cosinesimil score of 0.9 is a genuinely good semantic match, but it lives in [0, 1] space,
    not RRF space, so labelling it against the RRF-tuned threshold is still meaningless — the
    fix withholds the badge on this path unconditionally, not merely at the orthogonal boundary.
    """
    svc = HybridSearchService()
    starved_client = _CountingClient(count=0)
    monkeypatch.setattr(hss, "get_opensearch_client", lambda: starved_client)
    monkeypatch.setattr(svc, "_get_neural_model_id", lambda: "fake-model-id")

    _, needs_pipeline = svc._build_collapsed_search_body(
        "artificial intelligence",
        filters=[],
        page=1,
        page_size=20,
        has_speaker_filter=False,
        use_neural=True,
        sort_by="relevance",
        sort_order="desc",
    )
    assert needs_pipeline is False

    hits, _ = svc._process_collapsed_results(
        _collapsed_response(0.9), "artificial intelligence", is_fused_rrf=needs_pipeline
    )
    assert hits[0].semantic_confidence == ""


def test_phase2_bm25_path_never_labels_confidence_even_at_a_typical_bm25_score():
    """Phase 2 (`_build_search_hit_from_bucket`) is pure BM25 — scores ~1-30, not RRF's ~0-0.065.

    A typical BM25 score of 12.0 would have read "high" against the old 0.010 threshold on
    almost every semantic-only Phase-2 result. The fix is that this path never reads the
    threshold at all.
    """
    bucket = {
        "key": "file-under-test",
        "title_kw": {"buckets": [{"key": "Quarterly planning"}]},
        "language_kw": {"buckets": [{"key": "en"}]},
        "content_type_kw": {"buckets": [{"key": "audio/wav"}]},
        "speakers_kw": {"buckets": []},
        "tags_kw": {"buckets": []},
        "max_duration": {"value": 60.0},
        "max_file_size": {"value": 1024},
        "max_upload_time": {"value_as_string": "2026-01-01T00:00:00Z"},
        "min_file_id": {"value": 1},
    }
    phase2 = {
        "occurrences": [object()],
        "title_highlighted": "",
        "match_sources": [],
        "keyword_count": 0,
        "semantic_count": 1,
        "best_score": 12.0,
    }
    hit = HybridSearchService()._build_search_hit_from_bucket(bucket, phase2, "")
    assert hit is not None
    assert hit.semantic_confidence == ""


def test_phase2_no_p2_lookup_entry_returns_none_not_a_fabricated_hit():
    """Dead-code removal (#698): the old `best_score = 0.5` branch is gone.

    With no Phase 2 lookup entry for the file, the function must return None rather than
    constructing a `SearchHit` from fabricated defaults (`occurrences=[]`, `best_score=0.5`) that
    the very next line then always discarded (`if not occurrences: return None`) — proving the
    assignment was unreachable.
    """
    bucket = {
        "key": "file-under-test",
        "title_kw": {"buckets": [{"key": "Quarterly planning"}]},
        "language_kw": {"buckets": []},
        "content_type_kw": {"buckets": []},
        "speakers_kw": {"buckets": []},
        "tags_kw": {"buckets": []},
        "max_duration": {"value": 0.0},
        "max_file_size": {"value": 0},
        "max_upload_time": {"value_as_string": ""},
        "min_file_id": {"value": 1},
    }
    assert HybridSearchService()._build_search_hit_from_bucket(bucket, None, "") is None


def test_dead_best_score_literal_is_gone_from_source():
    """Source-level guard: `best_score = 0.5` must not reappear in the bucket builder.

    A behavioural test alone cannot see a dead default reintroduced under a reachable-looking
    guard; this pins the exact literal the issue names as dead code.
    """
    import inspect

    src = inspect.getsource(HybridSearchService._build_search_hit_from_bucket)
    assert "0.5" not in src, (
        "the dead `best_score = 0.5` fallback (issue #698) must not be reintroduced"
    )


def test_backfill_starved_groups_never_labels_confidence(monkeypatch):
    """`_backfill_starved_groups` always queries plain BM25 — must pass `is_fused_rrf=False`.

    Regression guard for the second `_process_collapsed_results` call site: a future edit that
    drops the explicit `is_fused_rrf=False` would silently default to `True` if the parameter's
    default ever changed, mislabeling backfilled BM25 hits.
    """
    svc = HybridSearchService()

    class _FakeClient:
        def search(self, index: str, body: dict) -> dict:
            return _collapsed_response(15.0)

    grouped = svc._backfill_starved_groups(
        client=_FakeClient(),
        grouped=[],
        search_query="widgets",
        filters=[],
        page=1,
        page_size=20,
        has_speaker_filter=False,
        sort_by="relevance",
        sort_order="desc",
        query="widgets",
    )
    assert len(grouped) == 1
    assert grouped[0].semantic_confidence == ""
