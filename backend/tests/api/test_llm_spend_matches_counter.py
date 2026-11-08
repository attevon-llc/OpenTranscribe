"""The AI usage panel's total equals the token-limit counter for a chat turn (issue #790).

A chat turn spends tokens on a planner call, a query rewrite and the streamed answer. The limit
counts all three at the LLM seam; the panel reads ``usage_event`` rows. Drives the REAL
``LLMService`` (only the network is stubbed) against the real ``llm_token_budget`` and
``usage_event`` tables and reads the panel through ``GET /api/usage/me``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest

from app.models.system_settings import SystemSettings
from app.services.chat.hooks import ChatCompletionContext
from app.services.chat.usage import record_chat_usage
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService
from app.services.llm_stream import LLMStreamEvent
from tests.llm_stream_helpers import drain


def _body(prompt: int, completion: int) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


class _Service(LLMService):
    bodies: list[dict[str, Any]]

    def _resolve_endpoint(self) -> str:
        return "https://api.example.invalid/v1/chat/completions"

    def _get_headers(self) -> dict[str, str]:
        return {}

    def _send_llm_request(self, *_a: Any, **_k: Any) -> Any:
        return self.bodies.pop(0)

    async def _chat_completion_stream_untracked(self, messages, cancel_event=None, **kwargs):
        yield LLMStreamEvent(type="delta", text="The answer.")
        yield LLMStreamEvent(type="usage", prompt_tokens=400, completion_tokens=60)
        yield LLMStreamEvent(type="done", finish_reason="stop")


class _Session:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._savepoint = session.begin_nested()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def commit(self) -> None:
        self._savepoint.commit()

    def rollback(self) -> None:
        self._savepoint.rollback()

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _own_sessions_are_the_test_session(db_session, monkeypatch):
    @contextlib.contextmanager
    def scope() -> Iterator[Any]:
        yield _Session(db_session)

    monkeypatch.setattr("app.db.session_utils.session_scope", scope)
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _Session(db_session))


def test_panel_total_equals_the_counter_for_planner_rewrite_and_answer(
    client, db_session, normal_user, user_token_headers
):
    db_session.add(SystemSettings(key="llm.budget.user_daily_tokens", value="1000000"))
    db_session.commit()
    svc = _Service(
        LLMConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-test",
            api_key="k",
            base_url="https://api.example.invalid/v1",
        )
    )
    svc.budget_user_id = normal_user.id
    svc.bodies = [_body(120, 30), _body(80, 20)]
    ask = [{"role": "user", "content": "q"}]

    svc.chat_completion(ask, max_tokens=100)  # planner
    svc.chat_completion(ask, max_tokens=100)  # query rewrite
    drain(svc.chat_completion_stream(ask))  # the answer
    record_chat_usage(  # what the turn's message-complete hook records for the answer
        ChatCompletionContext(
            conversation_uuid="c",
            message_uuid="m-1",
            user_id=normal_user.id,
            organization_id=None,
            provider="openai",
            model="gpt-test",
            prompt_tokens=400,
            completion_tokens=60,
            total_tokens=460,
            tokens_estimated=False,
            retrieved_chunks=1,
            success=True,
        )
    )

    body = client.get(
        "/api/usage/me", headers={"Authorization": user_token_headers["Authorization"]}
    ).json()

    assert body["budget"]["used_today"] == 150 + 100 + 460
    assert body["totals"]["total_tokens"] == body["budget"]["used_today"]
    assert [r["event_type"] for r in body["by_feature"]] == ["chat.tokens"]
