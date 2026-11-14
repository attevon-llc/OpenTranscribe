"""The reasoning probe finishes inside every timeout between it and the browser.

Three numbers have to agree: the probe's worst case (three sequential arms, each bounded by
``LLM_REASONING_PROBE_TIMEOUT_S``), nginx's ``proxy_read_timeout`` in front of the API, and
the SPA's own timeout for this one call. A probe that outlives either of the other two
records its verdict and then reports a failure to the user who asked for it. At 120 s per
arm the probe's worst case was 360 s, past nginx's 300 s.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.llm_defaults import LLM_REASONING_PROBE_TIMEOUT_S

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_ARMS = 3


def _nginx_proxy_read_timeout_s() -> int:
    """The server-level ``proxy_read_timeout`` (the first one; later ones are WebSocket-only)."""
    text = (_REPO / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    match = re.search(r"^\s*proxy_read_timeout\s+(\d+)s;", text, re.MULTILINE)
    assert match, "frontend/nginx.conf no longer sets a server-level proxy_read_timeout"
    return int(match.group(1))


def _spa_probe_timeout_s() -> float:
    text = (_REPO / "frontend" / "src" / "lib" / "api" / "llmSettings.ts").read_text(
        encoding="utf-8"
    )
    match = re.search(r"REASONING_PROBE_TIMEOUT_MS\s*=\s*([\d_]+)\s*;", text)
    assert match, "llmSettings.ts no longer exports REASONING_PROBE_TIMEOUT_MS"
    return int(match.group(1).replace("_", "")) / 1000


def test_the_probe_ends_before_nginx_gives_up():
    assert _nginx_proxy_read_timeout_s() > _ARMS * LLM_REASONING_PROBE_TIMEOUT_S


def test_the_spa_waits_for_the_whole_probe():
    assert _spa_probe_timeout_s() > _ARMS * LLM_REASONING_PROBE_TIMEOUT_S


def test_the_spa_answers_before_nginx_would_cut_it_off():
    """Past nginx's timeout the browser would see a gateway error, not its own timeout."""
    assert _spa_probe_timeout_s() < _nginx_proxy_read_timeout_s()


def test_the_probe_runs_exactly_three_arms(monkeypatch: pytest.MonkeyPatch):
    """``_ARMS`` above is only true while ``probe`` makes three calls; count them."""
    from app.services import llm_reasoning
    from app.services.llm_types import LLMConfig
    from app.services.llm_types import LLMProvider

    timeouts: list[float] = []

    class _Service:
        def __init__(self, config: LLMConfig) -> None:
            self.config = config

        def chat_completion_raw(self, messages, *, timeout, **kwargs):
            timeouts.append(timeout)
            return {"choices": [{"message": {"content": "36", "reasoning": ""}}]}

        def close(self) -> None:
            pass

    monkeypatch.setattr(llm_reasoning, "LLMService", _Service)

    llm_reasoning.probe(
        LLMConfig(provider=LLMProvider.VLLM, model="m", base_url="http://vllm:8000/v1")
    )

    assert timeouts == [LLM_REASONING_PROBE_TIMEOUT_S] * _ARMS
