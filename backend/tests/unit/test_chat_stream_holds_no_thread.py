"""A chat reply waiting on the model holds no worker thread (audit H1).

The provider's token stream used to be a blocking ``requests`` iterator driven by
``iterate_in_threadpool``: every open reply parked one thread of the API's shared pool
for as long as the model took, so a few dozen slow answers could take every thread the
rest of the API needed. The stream is now ``httpx.AsyncClient.stream`` + ``aiter_lines``.

This drives the real ``LLMService`` and the real ``_answer_frames`` against a real HTTP
server that sends one token and then holds the connection open, and counts the threads
borrowed while every reply is waiting.
"""

from __future__ import annotations

import asyncio
import http.server
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from typing import cast

import anyio.to_thread
import pytest

from app.core import threadpool
from app.core.config import settings
from app.core.redaction_config import EffectiveRedactionConfig
from app.services import llm_budget
from app.services.chat import service as chat_service
from app.services.chat.output_redactor import OutputRedactor
from app.services.chat.turn import ChatTurn

STREAMS = 8
HOLD_TIMEOUT_S = 20.0


class _HeldHandler(http.server.BaseHTTPRequestHandler):
    """One token at once, then nothing until the test releases the stream."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._chunk(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n')
        cast("Any", self.server).release.wait(HOLD_TIMEOUT_S)
        self._chunk(b'data: {"choices":[{"delta":{"content":" rest"}}]}\n\n')
        self._chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%X\r\n%s\r\n" % (len(data), data))

    def log_message(self, *args: Any) -> None:
        return None


@pytest.fixture
def held_llm_server(monkeypatch) -> Iterator[tuple[int, threading.Event]]:
    monkeypatch.setattr(settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HeldHandler)
    server.daemon_threads = True
    release = threading.Event()
    cast("Any", server).release = release
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], release
    finally:
        release.set()
        server.shutdown()


def _service(port: int):
    from app.services.llm_service import LLMService
    from app.services.llm_types import LLMConfig
    from app.services.llm_types import LLMProvider

    service = LLMService(
        LLMConfig(
            provider=LLMProvider.CUSTOM,
            model="test-model",
            api_key="sk-test",
            base_url=f"http://127.0.0.1:{port}/v1",
        )
    )
    # The token limits are database work and not what this measures.
    cast("Any", service)._reserve_budget = lambda *_a, **_k: llm_budget.NO_RESERVATION
    return service


def _borrowed_threads() -> int:
    """Threads in use on the default limiter, plus chat's own limiter where it exists."""
    borrowed = int(anyio.to_thread.current_default_thread_limiter().borrowed_tokens)
    chat_limiter = getattr(threadpool, "chat_thread_limiter", None)
    if chat_limiter is not None:
        borrowed += int(chat_limiter().borrowed_tokens)
    return borrowed


@pytest.mark.asyncio
async def test_replies_waiting_on_the_model_hold_no_thread(held_llm_server, monkeypatch):
    port, release = held_llm_server
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)
    inactive = cast(
        "EffectiveRedactionConfig", SimpleNamespace(enabled=False, enabled_categories=set())
    )
    first_tokens = [asyncio.Event() for _ in range(STREAMS)]
    answers: list[list[str]] = [[] for _ in range(STREAMS)]

    async def _reply(i: int) -> None:
        async for frame in chat_service._answer_frames(
            _service(port),
            [{"role": "user", "content": "hi"}],
            {},
            turn=ChatTurn(),
            cancel_event=threading.Event(),
            answer_redactor=OutputRedactor(inactive),
            reasoning_redactor=OutputRedactor(inactive),
            started=time.monotonic(),
            assistant_message_uuid=f"message-{i}",
            conversation_uuid="conversation",
        ):
            if frame.startswith("event: delta"):
                answers[i].append(frame)
                first_tokens[i].set()

    tasks = [asyncio.create_task(_reply(i)) for i in range(STREAMS)]
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in first_tokens)), 10)
        # Every reply has its first token. Each one asked for the next token in the same
        # loop step it handled the first, so all are now waiting on the held connection.
        borrowed_while_waiting = _borrowed_threads()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)

    assert borrowed_while_waiting == 0, (
        f"{borrowed_while_waiting} worker threads were held by {STREAMS} replies waiting "
        "on the model"
    )
    assert all(len(frames) == 2 for frames in answers), answers
