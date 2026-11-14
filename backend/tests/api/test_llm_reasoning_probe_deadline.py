"""A reasoning probe that outlasts the request deadline still records its verdict.

Seen on a live stack: ``POST .../reasoning-probe`` against Qwen3-4B-AWQ on vLLM measured
"works" (on=4543, off=0) in 107 s, past ``REQUEST_DEADLINE_SECONDS`` (55). The deadline
refused the verdict write, the route answered 503 ``server_busy``, and ``GET .../reasoning``
stayed ``unknown``, so the chat reasoning toggle never appeared for that model.

The probe here is a stand-in that, whenever a deadline is set, moves the deadline's clock
past it, as a 107 s generation would; nothing sleeps and the provider is never dialled.
Everything else (the middleware, the route, the record and the read back) is the real code.
"""

from __future__ import annotations

import time

import pytest
from fastapi import status
from sqlalchemy import event

from app.core.config import settings
from app.core.enums import ReasoningOffSwitch
from app.db import request_deadline
from app.db.request_deadline import _deadline
from app.services import llm_reasoning

_BASE = "/api/llm-settings"

#: Long enough that auth and the config read (the statements before the probe) cannot hit
#: it even on a loaded host, so a refusal can only come from the write after the probe.
_DEADLINE_S = 5.0


def _create_vllm_config(client, headers) -> str:
    created = client.post(
        _BASE,
        json={
            "name": "Slow local model",
            "provider": "vllm",
            "model_name": "qwen3-4b",
            "base_url": "https://api.openai.com/v1",
            "max_tokens": 16384,
            "temperature": "0.3",
        },
        headers=headers,
    )
    assert created.status_code == status.HTTP_200_OK, created.text
    return str(created.json()["uuid"])


class _Clock:
    """``time`` for ``app.db.request_deadline``: real monotonic time plus a settable lead."""

    def __init__(self) -> None:
        self.lead = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.lead


@pytest.fixture
def slow_probe(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, db_session
) -> dict[str, object]:
    """Replace the three generations with one that runs past any deadline it is given."""
    seen: dict[str, object] = {}
    clock = _Clock()
    monkeypatch.setattr(request_deadline, "time", clock)
    # The app engine carries the deadline listener; the test session's engine does not, so
    # without this no statement in these tests could ever be refused.
    test_engine = db_session.get_bind().engine
    request_deadline.install_request_deadline(test_engine)
    request.addfinalizer(
        lambda: event.remove(
            test_engine, "before_cursor_execute", request_deadline._refuse_statement_past_deadline
        )
    )

    def probe(config):
        deadline = _deadline.get()
        seen["deadline"] = deadline
        seen["holding_transaction"] = db_session.in_transaction()
        if deadline is not None:
            clock.lead += _DEADLINE_S + 1.0
        return llm_reasoning.ReasoningProbeResult(
            off_switch=ReasoningOffSwitch.WORKS,
            reasoning_chars_on=4543,
            reasoning_chars_off=0,
            reasoning_chars_omitted=4400,
            detail="off removed the reasoning (on=4543, off=0, omitted=4400)",
        )

    monkeypatch.setattr(llm_reasoning, "probe", probe)
    monkeypatch.setattr(settings, "REQUEST_DEADLINE_SECONDS", _DEADLINE_S)
    return seen


def test_a_probe_longer_than_the_deadline_still_records_its_verdict(
    client, user_token_headers, slow_probe
):
    config_uuid = _create_vllm_config(client, user_token_headers)

    probed = client.post(
        f"{_BASE}/config/{config_uuid}/reasoning-probe", headers=user_token_headers
    )
    read_back = client.get(f"{_BASE}/config/{config_uuid}/reasoning", headers=user_token_headers)

    assert probed.status_code == status.HTTP_200_OK, probed.text
    assert probed.json()["off_switch"] == "works"
    assert read_back.status_code == status.HTTP_200_OK, read_back.text
    assert read_back.json()["off_switch"] == "works"
    assert read_back.json()["reasoning_chars_on"] == 4543


def test_the_probe_runs_without_a_deadline_or_an_open_transaction(
    client, user_token_headers, slow_probe
):
    """The generations hold no pool connection, and nothing can refuse the write after."""
    config_uuid = _create_vllm_config(client, user_token_headers)

    client.post(f"{_BASE}/config/{config_uuid}/reasoning-probe", headers=user_token_headers)

    assert slow_probe["deadline"] is None
    assert slow_probe["holding_transaction"] is False


def test_an_ordinary_route_still_gets_the_deadline(client, user_token_headers, slow_probe):
    """Control: the exemption is this route's alone; its read-only sibling keeps the deadline."""
    from app.middleware.request_deadline import is_exempt

    config_uuid = _create_vllm_config(client, user_token_headers)

    assert is_exempt("POST", f"{_BASE}/config/{config_uuid}/reasoning-probe")
    assert not is_exempt("GET", f"{_BASE}/config/{config_uuid}/reasoning")
    assert not is_exempt("POST", f"{_BASE}/config/{config_uuid}/context-window-probe")
