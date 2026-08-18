"""The bulk retry must outlast the transient errors it retries (#495 follow-on).

MEASURED root cause. Running three integration modules against the live dev cluster
reproduced, in 1 of 4 runs:

    Transient bulk index error (will retry): circuit_breaking_exception:
        Memory Circuit Breaker is open, please check your resources!
    Retrying 8 failed docs (attempt 1/2, backoff 1s)
    Retrying 8 failed docs (attempt 2/2, backoff 2s)
    8 documents failed after 2 retries

That is ML Commons' breaker, not OpenSearch's — ``_nodes/stats/breaker`` showed
``tripped=0`` on every OpenSearch breaker and the k-NN plugin reported
``graph_memory_usage: 0``. ML Commons refuses inference when *instantaneous* JVM
heap-used exceeds ``plugins.ml_commons.jvm_heap_memory_threshold`` (default **85**),
and the neural ingest pipeline embeds every document, so one moment over the line
fails the whole bulk.

It was a FALSE trip. Heap read **89%** while it happened; the same node measured
``old gen 382 MB`` of a 4 GB heap moments later, with a 36 MB chunk index. Bulk
indexing fills young gen with garbage faster than G1 collects it, so a healthy
cluster reads as an exhausted one until a collection runs.

Two fixes, one test module each side of it:

1. ``ml_model_service.configure_ml_settings`` now sets that threshold to 95 — the
   primary fix, pinned by ``test_ml_settings_raise_the_jvm_heap_threshold``.
2. The retry budget here, the second line of defence: 2 attempts at 1 s + 2 s could
   not outlast the condition, and would not outlast a rejected execution or a closed
   index either — both also in ``_RETRYABLE_ERROR_TYPES``.
"""

from __future__ import annotations

from typing import Any

import pytest


def test_the_retry_budget_outlasts_a_multi_second_transient(monkeypatch):
    """A breaker that stays open for several seconds must still be survivable.

    The old budget summed to 3 s of waiting across 2 attempts. This asserts the
    shape that failed — a transient clearing after the old budget would have
    expired — now succeeds.
    """
    from app.services.search import indexing_service
    from app.services.search.indexing_service import TranscriptIndexingService

    slept: list[float] = []
    monkeypatch.setattr(indexing_service.time, "sleep", lambda s: slept.append(s))

    docs = [{"file_uuid": "u", "chunk_index": n} for n in range(8)]
    calls = {"n": 0}

    class _Client:
        def bulk(self, body: list[Any], refresh: bool = False) -> dict[str, Any]:
            calls["n"] += 1
            # Open for the first two retry attempts — i.e. past the OLD budget.
            if calls["n"] <= 2:
                return {
                    "errors": True,
                    "items": [
                        {"index": {"error": {"type": "circuit_breaking_exception"}}} for _ in docs
                    ],
                }
            return {"errors": False, "items": [{"index": {}} for _ in docs]}

    monkeypatch.setattr(indexing_service, "opensearch_client", _Client())

    recovered = TranscriptIndexingService()._retry_failed_docs(docs, "transcript_chunks", True)

    assert recovered == len(docs), (
        "the breaker cleared within the budget and every document should have landed; "
        f"only {recovered} of {len(docs)} did"
    )
    assert calls["n"] == 3, f"expected 3 bulk attempts, got {calls['n']}"
    assert sum(slept) > 3.0, (
        f"total backoff {sum(slept):.1f}s does not exceed the old 1s+2s budget, so this "
        "test would have passed before the fix and proves nothing"
    )


def test_the_backoff_is_jittered_so_concurrent_workers_do_not_resynchronise(monkeypatch):
    """A fixed backoff turns the retry into the load that re-trips the breaker.

    Every worker indexing when a shared cluster resource goes over its limit fails
    at the same instant; on a fixed schedule they all come back at the same instant
    too. The jitter is what breaks that up, so it is asserted rather than assumed.
    """
    from app.services.search import indexing_service
    from app.services.search.indexing_service import TranscriptIndexingService

    docs = [{"file_uuid": "u", "chunk_index": 0}]

    class _AlwaysOpen:
        def bulk(self, body: list[Any], refresh: bool = False) -> dict[str, Any]:
            return {
                "errors": True,
                "items": [{"index": {"error": {"type": "circuit_breaking_exception"}}}],
            }

    monkeypatch.setattr(indexing_service, "opensearch_client", _AlwaysOpen())

    runs: list[list[float]] = []
    for _ in range(6):
        slept: list[float] = []
        monkeypatch.setattr(indexing_service.time, "sleep", lambda s, acc=slept: acc.append(s))
        TranscriptIndexingService()._retry_failed_docs(docs, "transcript_chunks", True)
        runs.append(slept)

    firsts = [run[0] for run in runs]
    assert len(set(firsts)) > 1, (
        f"every run waited exactly {firsts[0]}s — the backoff is not jittered, so N "
        "workers would retry in lockstep and re-trip the resource together"
    )
    # Still bounded: jitter must not turn a 1s wait into an arbitrary one.
    assert all(0.5 <= first <= 1.5 for first in firsts), firsts


def test_permanent_errors_are_still_not_retried(monkeypatch):
    """The control. A wider budget must not start retrying unretryable errors.

    A mapping rejection fails identically every time, so retrying it four times
    just spends the backoff before failing anyway.
    """
    from app.services.search.indexing_service import TranscriptIndexingService

    response = {
        "errors": True,
        "items": [
            {"index": {"error": {"type": "mapper_parsing_exception", "reason": "bad field"}}},
            {"index": {"error": {"type": "circuit_breaking_exception", "reason": "open"}}},
        ],
    }
    batch = [{"file_uuid": "u", "chunk_index": 0}, {"file_uuid": "u", "chunk_index": 1}]

    failed = TranscriptIndexingService()._extract_failed_docs(response, batch)

    assert failed == [batch[1]], (
        "only the transient error is eligible for retry; the mapping failure must be "
        f"dropped rather than retried: {failed}"
    )


def test_ml_settings_raise_the_jvm_heap_threshold(monkeypatch):
    """The PRIMARY fix: the setting whose default caused the false trip.

    ``configure_ml_settings`` already raised ``native_memory_threshold`` to 99 and
    left its JVM-heap sibling at the 85 default. That asymmetry is what let a
    cluster with a 382 MB live working set refuse to embed.
    """
    from app.services.search import ml_model_service

    captured: dict[str, Any] = {}

    class _Cluster:
        def put_settings(self, body: dict[str, Any]) -> None:
            captured.update(body["persistent"])

    class _Client:
        cluster = _Cluster()

    service = ml_model_service.OpenSearchMLModelService()
    monkeypatch.setattr(service, "_ensure_client", lambda: True)
    service._client = _Client()
    service._ml_settings_configured = False

    assert service.configure_ml_settings() is True

    threshold = captured.get("plugins.ml_commons.jvm_heap_memory_threshold")
    assert threshold is not None, (
        "the JVM heap threshold is not configured, so it sits at the 85 default that "
        "failed whole bulk loads on a cluster with a 382 MB working set"
    )
    assert 85 < threshold <= 99, f"expected a raised but still protective value, got {threshold}"
    # The sibling must not have been dropped while adding this one.
    assert captured.get("plugins.ml_commons.native_memory_threshold") == 99


@pytest.mark.parametrize(
    "error_type", ["circuit_breaking_exception", "es_rejected_execution_exception"]
)
def test_the_retryable_set_still_covers_the_errors_that_actually_fired(error_type: str):
    """`circuit_breaking_exception` is the one measured in the wild — pin it.

    If it were ever moved to the permanent set, a false breaker trip would silently
    drop documents instead of retrying them.
    """
    from app.services.search.indexing_service import _PERMANENT_ERROR_TYPES
    from app.services.search.indexing_service import _RETRYABLE_ERROR_TYPES

    assert error_type in _RETRYABLE_ERROR_TYPES
    assert error_type not in _PERMANENT_ERROR_TYPES
