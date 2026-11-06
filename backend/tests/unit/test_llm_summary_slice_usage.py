"""A multi-slice summary records the tokens of EVERY call, not just the caller thread's.

``_process_multiple_chunks`` summarizes slices on ``ThreadPoolExecutor`` workers. The open
usage run is found through a ``ContextVar`` (``services/llm_usage.py``); an executor thread
starts with an EMPTY context (Python docs, ``contextvars``), so without
``contextvars.copy_context().run`` every slice call saw no run and was silently dropped,
leaving only the merge call in the metered total.

Drives the real ``LLMService.chat_completion``; only the HTTP send is stubbed, and the
per-call token counts are distinct so a missing call changes the sum.
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import patch

from app.services import llm_usage
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService

SECTIONS = 4
SECTION_RE = re.compile(r"section (\d+) of")
MERGE_PROMPT_TOKENS = 9000
MERGE_COMPLETION_TOKENS = 900


def _slice_tokens(section_num: int) -> tuple[int, int]:
    return 100 * section_num, 10 * section_num


class _FakeProviderService(LLMService):
    """The real service with only the network send replaced by per-call usage."""

    def _resolve_endpoint(self) -> str:
        return "https://api.example.invalid/v1/chat/completions"

    def _get_headers(self) -> dict[str, str]:
        return {}

    def _send_llm_request(
        self, url: str, payload: dict, headers: dict, timeout: int
    ) -> dict[str, Any]:
        match = SECTION_RE.search(payload["messages"][0]["content"])
        if match:
            prompt, completion = _slice_tokens(int(match.group(1)))
        else:
            prompt, completion = MERGE_PROMPT_TOKENS, MERGE_COMPLETION_TOKENS
        return {
            "choices": [{"message": {"content": json.dumps({})}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }


def _service() -> LLMService:
    return _FakeProviderService(
        LLMConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-test",
            api_key="k",
            base_url="https://api.example.invalid/v1",
        )
    )


def test_every_slice_call_and_the_merge_call_land_in_the_open_run() -> None:
    expected_prompt = sum(_slice_tokens(n)[0] for n in range(1, SECTIONS + 1)) + MERGE_PROMPT_TOKENS
    expected_completion = (
        sum(_slice_tokens(n)[1] for n in range(1, SECTIONS + 1)) + MERGE_COMPLETION_TOKENS
    )
    chunks = [f"chunk {i}" for i in range(SECTIONS)]

    # _flush would open a DB session; the accumulator is what this test is about.
    with (
        patch.object(llm_usage, "_flush"),
        llm_usage.llm_usage_run("summary", file_id=None, run_id="run-1") as run,
    ):
        _service()._process_multiple_chunks(chunks, None, "{transcript}\n{speaker_data}")

    assert run.calls == SECTIONS + 1
    assert run.prompt_tokens == expected_prompt
    assert run.completion_tokens == expected_completion
    assert not run.tokens_estimated, "provider usage was reported for every call"
