"""Reranker load retry behaviour.

The bug this guards: the loader used to set ``_load_attempted = True`` BEFORE
attempting the load, so a single failure disabled reranking for the lifetime of
the process. The failure modes that matter are transient — a container starting
before its model-cache volume is mounted, a blip fetching weights — and the
result was permanently degraded retrieval with no signal after one warning.
"""

from __future__ import annotations

import sys
import types

import pytest

from app.services.chat import reranker as reranker_mod


@pytest.fixture(autouse=True)
def _clean_reranker_state():
    reranker_mod.reset_reranker_state()
    yield
    reranker_mod.reset_reranker_state()


class _FakeEncoder:
    def __init__(self, *args, **kwargs):
        pass

    def predict(self, pairs):
        return [1.0] * len(pairs)


def _patch_load(monkeypatch, *, results):
    """Make CrossEncoder construction consume ``results`` one call at a time.

    An entry that is an exception is raised; anything else is returned.

    Injects a STUB module rather than patching the real ``sentence_transformers``:
    importing it pulls torch, which fails on a CPU-only host and is absent from
    ``requirements-ci.txt`` entirely. The loader does its import inside the
    function, so a stub in ``sys.modules`` is all it ever sees — and that is
    precisely the deployment this test is about (no model available).
    """
    calls = {"n": 0}

    def _factory(*args, **kwargs):
        outcome = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    stub = types.ModuleType("sentence_transformers")
    stub.CrossEncoder = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)
    return calls


def test_successful_load_is_cached_and_not_reloaded(monkeypatch):
    encoder = _FakeEncoder()
    calls = _patch_load(monkeypatch, results=[encoder])

    assert reranker_mod.get_reranker() is encoder
    assert reranker_mod.get_reranker() is encoder
    assert calls["n"] == 1, "weights must load once, not per request"


def test_failed_load_does_not_retry_on_every_call(monkeypatch):
    """A missing model must not cost a load attempt per chat message."""
    calls = _patch_load(monkeypatch, results=[RuntimeError("no model")])

    assert reranker_mod.get_reranker() is None
    assert reranker_mod.get_reranker() is None
    assert reranker_mod.get_reranker() is None
    assert calls["n"] == 1, "cooldown should suppress immediate retries"


def test_failed_load_is_retried_after_the_cooldown(monkeypatch):
    """THE regression: one transient failure must not disable reranking forever."""
    encoder = _FakeEncoder()
    calls = _patch_load(monkeypatch, results=[RuntimeError("cache not mounted yet"), encoder])

    assert reranker_mod.get_reranker() is None
    assert calls["n"] == 1

    # Simulate the cooldown elapsing rather than sleeping through it. `_retry_after`
    # is keyed per model name (#453/ML1 — the multilingual arm needs its own
    # independent cooldown), so clearing the whole dict is the equivalent of the
    # single float this used to reset to 0.0.
    reranker_mod._retry_after.clear()

    assert reranker_mod.get_reranker() is encoder, "should recover once the model appears"
    assert calls["n"] == 2


def test_cooldown_is_long_enough_to_not_hammer_a_missing_model():
    assert reranker_mod.RETRY_COOLDOWN_S >= 60


def test_rerank_passes_hits_through_when_the_model_is_unavailable(monkeypatch):
    """Degraded ranking beats a failed answer."""
    _patch_load(monkeypatch, results=[RuntimeError("no model")])

    class _Hit:
        def __init__(self, content):
            self.content = content
            self.score = 0.0

    hits = [_Hit("a"), _Hit("b"), _Hit("c")]
    assert reranker_mod.rerank("q", hits) == hits
