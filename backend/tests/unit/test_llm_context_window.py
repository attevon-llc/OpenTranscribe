"""Context-window discovery (issue #533) — the probe, the verdicts, and the record.

The live behaviour every mocked shape below encodes was measured first
(2026-08-21, both reference servers):

* vLLM ``GET /v1/models`` → ``max_model_len: 60000`` for ``gemma-4-e4b``; an
  unlisted model is simply absent from ``data``.
* Ollama ``POST /api/show`` → ``model_info["qwen35.context_length"]: 262144``
  for ``qwen3.8:latest``; an unknown model answers **HTTP 404** (not a
  connection error), which must read as NOT_FOUND — an operator sent to debug
  the network for a typo'd model name is the wrong outcome.

The probe's one non-negotiable: **it never guesses.** Every failure shape maps
to a status whose reader falls back to the user's declared value; only
``MEASURED`` ever carries a number.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import cast
from urllib.parse import urlparse

import pytest
import requests
from sqlalchemy.orm import Session

from app.core.constants import LLM_CONTEXT_WINDOW_KEY_PREFIX
from app.core.constants import LLM_REASONING_CAPABILITY_KEY_PREFIX
from app.services import llm_context_window
from app.services.llm_context_window import ContextWindowStatus
from app.services.llm_context_window import discovery_key
from app.services.llm_context_window import measured_window
from app.services.llm_context_window import probe
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.utils.url_validation import PinnedTarget


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _vllm_config(model: str = "gemma-4-e4b") -> LLMConfig:
    return LLMConfig(
        provider=LLMProvider.VLLM, model=model, base_url="http://llm:8000/v1", api_key=""
    )


def _ollama_config(model: str = "qwen3.8:latest") -> LLMConfig:
    return LLMConfig(
        provider=LLMProvider.OLLAMA, model=model, base_url="http://llm:11434/v1", api_key=""
    )


class _FakeSession:
    """Stands in for the ``requests.Session`` ``pinned_requests_session`` yields."""

    def __init__(self, get_fn=None, post_fn=None) -> None:
        self._get_fn = get_fn
        self._post_fn = post_fn

    def get(self, url, **kwargs):
        return self._get_fn(url, **kwargs)

    def post(self, url, **kwargs):
        return self._post_fn(url, **kwargs)


def _patch_pinning(monkeypatch, *, get_fn=None, post_fn=None) -> None:
    """Bypass real DNS resolution/pinning so these discovery-LOGIC tests exercise
    the response-parsing code without touching the network — the pinning
    mechanism itself (rebinding, redirect handling, the loopback refusal) is
    covered end to end by ``test_ssrf_chat_completion.py``'s rebinding suite,
    against the same ``resolve_pinned_target``/``pinned_requests_session`` this
    module calls. Patched at the SOURCE module (``app.utils.url_validation``),
    matching how ``llm_context_window`` imports both names lazily inside the
    functions that use them — patching a module-level alias here would silently
    not apply.
    """
    import app.utils.url_validation as uv

    def fake_resolve(url, **kwargs):
        parsed = urlparse(url)
        return (
            PinnedTarget(
                original_url=url,
                url=url,
                address="203.0.113.1",  # RFC 5737 TEST-NET-3 — a fake, non-bindable address
                hostname=parsed.hostname or "",
                host_header=parsed.netloc,
                scheme=parsed.scheme,
                pinned=False,
            ),
            "",
        )

    @contextmanager
    def fake_session(target):
        yield _FakeSession(get_fn=get_fn, post_fn=post_fn)

    monkeypatch.setattr(uv, "resolve_pinned_target", fake_resolve)
    monkeypatch.setattr(uv, "pinned_requests_session", fake_session)


# ------------------------------------------------------------------ vLLM path


class TestVllmDiscovery:
    def test_the_measured_reference_shape_reads_max_model_len(self, monkeypatch):
        _patch_pinning(
            monkeypatch,
            get_fn=lambda url, **kw: _FakeResponse(
                {"data": [{"id": "gemma-4-e4b", "max_model_len": 60000}]}
            ),
        )
        result = probe(_vllm_config())
        assert result.status is ContextWindowStatus.MEASURED
        assert result.context_window == 60000

    def test_an_unlisted_model_is_not_found_never_a_guess(self, monkeypatch):
        _patch_pinning(
            monkeypatch,
            get_fn=lambda url, **kw: _FakeResponse(
                {"data": [{"id": "other", "max_model_len": 4096}]}
            ),
        )
        result = probe(_vllm_config())
        assert result.status is ContextWindowStatus.NOT_FOUND
        assert result.context_window is None

    def test_an_openai_compatible_server_without_the_extension_is_not_found(self, monkeypatch):
        """A clone can serve /v1/models with no max_model_len — that is 'the
        number could not be read', never 'the window is <anything>'."""
        _patch_pinning(
            monkeypatch,
            get_fn=lambda url, **kw: _FakeResponse({"data": [{"id": "gemma-4-e4b"}]}),
        )
        result = probe(_vllm_config())
        assert result.status is ContextWindowStatus.NOT_FOUND
        assert result.context_window is None

    def test_the_models_url_and_bearer_header_are_built_from_the_config(self, monkeypatch):
        seen: dict = {}

        def fake_get(url, headers=None, timeout=None, **kw):
            seen.update(url=url, headers=headers or {}, timeout=timeout)
            return _FakeResponse({"data": []})

        _patch_pinning(monkeypatch, get_fn=fake_get)
        config = LLMConfig(
            provider=LLMProvider.VLLM, model="m", base_url="http://llm:8000/v1/", api_key="sk-x"
        )
        probe(config)
        assert seen["url"] == "http://llm:8000/v1/models"
        assert seen["headers"]["Authorization"] == "Bearer sk-x"
        assert seen["timeout"] is not None


# ---------------------------------------------------------------- Ollama path


class TestOllamaDiscovery:
    def test_the_measured_reference_shape_reads_arch_context_length(self, monkeypatch):
        seen: dict = {}

        def fake_post(url, json=None, timeout=None, **kw):
            seen.update(url=url, body=json)
            return _FakeResponse({"model_info": {"qwen35.context_length": 262144}})

        _patch_pinning(monkeypatch, post_fn=fake_post)
        result = probe(_ollama_config())
        assert result.status is ContextWindowStatus.MEASURED
        assert result.context_window == 262144
        # /api/show lives at the server ROOT — the /v1 OpenAI-compat suffix must
        # be stripped or the call 404s on every Ollama that exists.
        assert seen["url"] == "http://llm:11434/api/show"
        assert seen["body"] == {"model": "qwen3.8:latest"}

    def test_any_architecture_prefix_is_accepted(self, monkeypatch):
        _patch_pinning(
            monkeypatch,
            post_fn=lambda url, **kw: _FakeResponse({"model_info": {"llama.context_length": 8192}}),
        )
        result = probe(_ollama_config(model="llama3:8b"))
        assert result.status is ContextWindowStatus.MEASURED
        assert result.context_window == 8192

    def test_a_404_for_an_unknown_model_is_not_found_not_unreachable(self, monkeypatch):
        """Measured live: Ollama 404s /api/show for a name it does not serve.
        UNREACHABLE would send the operator debugging the network for a typo."""
        _patch_pinning(
            monkeypatch,
            post_fn=lambda url, **kw: _FakeResponse({"error": "model not found"}, status_code=404),
        )
        result = probe(_ollama_config(model="no-such-model"))
        assert result.status is ContextWindowStatus.NOT_FOUND

    def test_a_show_without_context_length_is_not_found(self, monkeypatch):
        _patch_pinning(
            monkeypatch,
            post_fn=lambda url, **kw: _FakeResponse(
                {"model_info": {"general.architecture": "qwen35"}}
            ),
        )
        assert probe(_ollama_config()).status is ContextWindowStatus.NOT_FOUND


# ------------------------------------------------------- failure/stand-aside


class TestFailClosed:
    @pytest.mark.parametrize(
        "provider",
        [LLMProvider.ANTHROPIC, LLMProvider.OPENROUTER, LLMProvider.CUSTOM, LLMProvider.OPENAI],
    )
    def test_undiscoverable_providers_stand_aside(self, provider, monkeypatch):
        """UNSUPPORTED, and no HTTP call at all — a probe that dialled a custom
        clone's /v1/models uninvited is the 400-on-unknown-key mistake the
        reasoning probe's provider gate exists to prevent, one feature over."""

        def explode(*a, **kw):  # pragma: no cover - the assertion is that it never runs
            raise AssertionError("no HTTP call may be made for an undiscoverable provider")

        monkeypatch.setattr(llm_context_window.requests, "get", explode)
        monkeypatch.setattr(llm_context_window.requests, "post", explode)
        config = LLMConfig(provider=provider, model="m", base_url="http://x/v1", api_key="k")
        result = probe(config)
        assert result.status is ContextWindowStatus.UNSUPPORTED
        assert result.context_window is None

    def test_a_dead_endpoint_is_unreachable_and_never_raises(self, monkeypatch):
        def refuse(*a, **kw):
            raise requests.ConnectionError("connection refused")

        _patch_pinning(monkeypatch, get_fn=refuse)
        result = probe(_vllm_config())
        assert result.status is ContextWindowStatus.UNREACHABLE

    def test_the_detail_never_carries_the_endpoint_url(self, monkeypatch):
        """The key already hides the URL by hashing; the detail must not leak it
        back into the deployment-wide settings table via an exception message."""

        def refuse(*a, **kw):
            raise requests.ConnectionError("refused: http://secret-host:8000/v1/models")

        _patch_pinning(monkeypatch, get_fn=refuse)
        result = probe(_vllm_config())
        assert "secret-host" not in result.detail

    def test_a_private_endpoint_is_refused_before_any_http_call(self, monkeypatch):
        """H6: the probe used to dial a user-configured base_url with no SSRF
        guard at all — same class as the mediacms.py SSRF fix (#284 A0.1), the
        one sibling module that stayed unguarded. `resolve_pinned_target`
        refuses a private/internal target for real here (not mocked out, unlike
        every other test in this file) — LLM_ALLOW_PRIVATE_ENDPOINTS defaults to
        False, so a link-local metadata address must never reach `requests`.
        """

        from app.core.config import settings as app_settings

        def explode(*a, **kw):  # pragma: no cover - the assertion is that it never runs
            raise AssertionError("a refused SSRF target must never be dialled")

        monkeypatch.setattr(app_settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", False, raising=False)
        monkeypatch.setattr(llm_context_window.requests, "get", explode)
        config = LLMConfig(
            provider=LLMProvider.VLLM,
            model="m",
            base_url="http://169.254.169.254/latest/meta-data",
            api_key="",
        )
        result = probe(config)
        assert result.status is ContextWindowStatus.UNREACHABLE
        assert "169.254.169.254" not in result.detail


# ----------------------------------------------------------- key + the record


class TestDiscoveryKey:
    def test_changing_the_model_changes_the_key(self):
        a = discovery_key("vllm", "http://x/v1", "model-a")
        b = discovery_key("vllm", "http://x/v1", "model-b")
        assert a != b

    def test_a_trailing_slash_is_the_same_endpoint(self):
        assert discovery_key("vllm", "http://x/v1", "m") == discovery_key(
            "vllm", "http://x/v1/", "m"
        )

    def test_the_prefix_can_never_shadow_the_reasoning_measurement(self):
        assert LLM_CONTEXT_WINDOW_KEY_PREFIX != LLM_REASONING_CAPABILITY_KEY_PREFIX
        key = discovery_key("vllm", "http://x/v1", "m")
        assert key.startswith(LLM_CONTEXT_WINDOW_KEY_PREFIX)

    def test_the_url_is_hashed_not_stored(self):
        assert "x/v1" not in discovery_key("vllm", "http://x/v1", "m")


class TestMeasuredWindow:
    def test_a_measured_record_yields_its_number(self):
        assert measured_window({"status": "measured", "context_window": 60000}) == 60000

    @pytest.mark.parametrize(
        "stored",
        [
            {},
            {"status": "unsupported", "context_window": 60000},
            {"status": "unreachable"},
            {"status": "not_found"},
            {"status": "a-status-from-a-newer-build", "context_window": 60000},
            {"status": "measured", "context_window": None},
            {"status": "measured", "context_window": -1},
            {"status": "measured", "context_window": "60000"},
        ],
    )
    def test_everything_else_yields_none_and_the_declared_value_stands(self, stored):
        assert measured_window(stored) is None


class TestRecordRoundTrip:
    def test_record_then_read_yields_the_measurement(self, monkeypatch):
        stored: dict[str, str] = {}

        def fake_set(db, key, value, description=""):
            stored[key] = value

        def fake_get(db, key, default=None):
            return stored.get(key, default)

        import app.services.system_settings_service as sss

        monkeypatch.setattr(sss, "set_setting", fake_set)
        monkeypatch.setattr(sss, "get_setting", fake_get)

        config = _vllm_config()
        result = llm_context_window.ContextWindowProbeResult(
            status=ContextWindowStatus.MEASURED, context_window=60000, detail="max_model_len"
        )
        llm_context_window.record(cast(Session, None), config, result)
        assert len(stored) == 1
        (key,) = stored
        assert key.startswith(LLM_CONTEXT_WINDOW_KEY_PREFIX)
        payload = json.loads(stored[key])
        assert payload["context_window"] == 60000

        read = llm_context_window.read_record(
            cast(Session, None), "vllm", config.base_url, config.model
        )
        assert measured_window(read) == 60000

    def test_an_unreadable_record_reads_as_unprobed(self, monkeypatch):
        import app.services.system_settings_service as sss

        monkeypatch.setattr(sss, "get_setting", lambda db, key, default=None: "{not json")
        assert llm_context_window.read_record(cast(Session, None), "vllm", "http://x/v1", "m") == {}
