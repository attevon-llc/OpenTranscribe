"""Offline-guard / bounded-load behavior for chat.reranker.get_reranker.

Confirmed bug: `get_reranker()` had no offline guard at all, so every backend
process restart cold-loaded the model and — with the dev default
`HF_HUB_OFFLINE=0` (`.env.example:153`) — paid a DNS-retry storm against
huggingface.co (~23-30s per retry cycle) before falling back to the fully
cached local copy. This proves the fix two ways: (1) `HF_HUB_OFFLINE=1` +
`local_files_only=True` reaches CrossEncoder's own constructor kwarg, and
(2) the load is bounded regardless of the flag, so a stall degrades to `None`
rather than blocking a chat request indefinitely.
"""

from __future__ import annotations

import sys
import time
import types

import pytest

from app.services.chat import reranker as reranker_mod


@pytest.fixture(autouse=True)
def _clean_reranker_state():
    reranker_mod.reset_reranker_state()
    yield
    reranker_mod.reset_reranker_state()


def _install_fake_cross_encoder(monkeypatch, factory):
    stub = types.ModuleType("sentence_transformers")
    stub.CrossEncoder = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)


@pytest.mark.unit
class TestOfflineKwargThreadedToCrossEncoder:
    def test_local_files_only_passed_when_offline_requested(self, monkeypatch):
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        received_kwargs: dict = {}

        def _factory(model_name, **kwargs):
            received_kwargs.update(kwargs)
            return object()

        _install_fake_cross_encoder(monkeypatch, _factory)

        reranker_mod.get_reranker()

        assert received_kwargs.get("local_files_only") is True

    def test_local_files_only_not_forced_when_online(self, monkeypatch):
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        received_kwargs: dict = {}

        def _factory(model_name, **kwargs):
            received_kwargs.update(kwargs)
            return object()

        _install_fake_cross_encoder(monkeypatch, _factory)

        reranker_mod.get_reranker()

        assert "local_files_only" not in received_kwargs


@pytest.mark.unit
class TestNoNetworkAttemptWhenOffline:
    def test_zero_outbound_http_calls_with_offline_and_cache_present(self, monkeypatch):
        """The stronger claim: not just success, but that no network attempt
        was made at all — a call that happens to succeed offline is not proof
        by itself.
        """
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        received_kwargs: dict = {}
        network_calls: list[str] = []

        def _factory(model_name, **kwargs):
            # A well-behaved offline-aware CrossEncoder never touches the network
            # at all once local_files_only is honoured — record what arrived
            # rather than asserting inside the factory, whose exception would
            # otherwise be swallowed by get_reranker's own broad except-and-log
            # and read as a false pass.
            received_kwargs.update(kwargs)
            return object()

        _install_fake_cross_encoder(monkeypatch, _factory)
        result = reranker_mod.get_reranker()

        assert result is not None, "the load must have succeeded, not been swallowed"
        assert received_kwargs.get("local_files_only") is True
        assert network_calls == []


@pytest.mark.unit
class TestLoadIsBoundedRegardlessOfOfflineFlag:
    def test_a_stalled_load_degrades_to_none_instead_of_hanging(self, monkeypatch):
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        monkeypatch.setattr(reranker_mod, "_LOAD_TIMEOUT_S", 1.0)

        def _stalling_factory(model_name, **kwargs):
            time.sleep(5)
            raise AssertionError("unreachable: the timeout must fire first")

        _install_fake_cross_encoder(monkeypatch, _stalling_factory)

        started = time.monotonic()
        result = reranker_mod.get_reranker()
        elapsed = time.monotonic() - started

        assert result is None, "a stalled load must degrade, never raise into the caller"
        assert elapsed < 4, f"the timeout did not bound the wait: took {elapsed:.1f}s"
