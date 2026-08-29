"""Offline-guard / bounded-load behavior for opensearch_service.client._get_sentence_transformer.

Same class of latent stall as the chat reranker (second-highest priority sibling
site per the investigation): `SentenceTransformer("all-MiniLM-L6-v2")` had no
offline guard and no bound, so a cold load on a degraded network path could sit
retrying against huggingface.co for the rest of the request's budget even with
the model fully cached locally.
"""

from __future__ import annotations

import sys
import time
import types

import pytest

from app.services.opensearch_service import client as client_mod


@pytest.fixture(autouse=True)
def _clean_singleton():
    client_mod._sentence_transformer_model = None
    yield
    client_mod._sentence_transformer_model = None


def _install_fake_sentence_transformer(monkeypatch, factory):
    stub = types.ModuleType("sentence_transformers")
    stub.SentenceTransformer = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)


@pytest.mark.unit
class TestOfflineKwargThreadedToSentenceTransformer:
    def test_local_files_only_passed_when_offline_requested(self, monkeypatch):
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        received_kwargs: dict = {}

        def _factory(model_name, **kwargs):
            received_kwargs.update(kwargs)
            return object()

        _install_fake_sentence_transformer(monkeypatch, _factory)

        result = client_mod._get_sentence_transformer()

        assert result is not None
        assert received_kwargs.get("local_files_only") is True

    def test_local_files_only_not_forced_when_online(self, monkeypatch):
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        received_kwargs: dict = {}

        def _factory(model_name, **kwargs):
            received_kwargs.update(kwargs)
            return object()

        _install_fake_sentence_transformer(monkeypatch, _factory)

        client_mod._get_sentence_transformer()

        assert "local_files_only" not in received_kwargs

    def test_singleton_still_caches_across_calls(self, monkeypatch):
        """The bound/offline-aware load must not reintroduce a reload per call."""
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        calls = {"n": 0}

        def _factory(model_name, **kwargs):
            calls["n"] += 1
            return object()

        _install_fake_sentence_transformer(monkeypatch, _factory)

        first = client_mod._get_sentence_transformer()
        second = client_mod._get_sentence_transformer()

        assert first is second
        assert calls["n"] == 1


@pytest.mark.unit
class TestLoadIsBoundedRegardlessOfOfflineFlag:
    def test_a_stalled_load_fails_fast_instead_of_hanging(self, monkeypatch):
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        monkeypatch.setattr(client_mod, "_SENTENCE_TRANSFORMER_LOAD_TIMEOUT_S", 1.0)

        def _stalling_factory(model_name, **kwargs):
            time.sleep(5)
            raise AssertionError("unreachable: the timeout must fire first")

        _install_fake_sentence_transformer(monkeypatch, _stalling_factory)

        started = time.monotonic()
        with pytest.raises(TimeoutError, match="did not complete within"):
            client_mod._get_sentence_transformer()
        elapsed = time.monotonic() - started

        assert elapsed < 4, f"the timeout did not bound the wait: took {elapsed:.1f}s"
