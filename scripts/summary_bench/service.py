"""`EvalLLMService`: the app's own `LLMService` plus harness hooks, never an edit to app code.

Arms are injected by patching the two module globals `SECTION_SCOPE_RULES` and
`SECTION_MERGE_RULES` of `app.services.llm_summarization`; both are interpolated into f-strings
at call time, so every call looks the name up again, including the calls that run on the
slice `ThreadPoolExecutor`. For that reason arms run SEQUENTIALLY: two `patch.object` contexts on
one module from two threads would race.

**Applied check, on every call.** Patching proves nothing by itself, so the wrapper around
`chat_completion` asserts that each slice call's system message contains the arm's scope rules
and the merge call's contains its merge rules, and records the SHA-256 of each system message.
A summary whose calls fail this is recorded `void: arm_not_applied`.

The hooks are four overrides (plus a transport seam for tests and token accounting). A unit test
asserts each still exists on `LLMService` with the expected parameter names, so an app rename
breaks the harness test instead of silently bypassing a hook.
"""

from __future__ import annotations

import contextlib
import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

from summary_bench.arms import Arm

MODE_EXCEPTION = 'exception'
MODE_PARSE = 'parse'
#: Content that json_repair cannot turn into a non-empty dict, so `_summarize_section` ends at
#: its `_error: True` placeholder. A test proves this against the installed json_repair.
UNPARSEABLE_CONTENT = 'I am unable to produce a summary for this section.'
_MERGE_MARKER = 'You are combining multiple section summaries'
_MODULE = 'app.services.llm_summarization'


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Injection:
    """Force one slice to fail: ``slice_index`` is 1-based, as the pipeline numbers sections."""

    mode: str
    slice_index: int


@dataclass
class CallRecord:
    """What one LLM call looked like, with no prose: hashes, sizes and timings."""

    phase: str  # "slice" | "merge" | "single"
    section: int | None
    system_sha256: str
    applied: bool | None  # None when the call was injected and never sent
    injected: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    latency_s: float | None = None


@dataclass
class Recorder:
    """Thread-safe log of everything an `EvalLLMService` did for one cell."""

    calls: list[CallRecord] = field(default_factory=list)
    slice_outputs: dict[int, dict[str, Any]] = field(default_factory=dict)
    merge_inputs: list[list[dict[str, Any]]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, record: CallRecord) -> None:
        with self._lock:
            self.calls.append(record)

    @property
    def applied_failures(self) -> int:
        return sum(1 for c in self.calls if c.applied is False)


def build_service_class() -> type:
    """Define `EvalLLMService` lazily: importing the app needs the environment bootstrapped."""
    from app.services.llm_service import LLMService
    from app.services.llm_types import LLMResponse

    class EvalLLMService(LLMService):
        """`LLMService` with recording, arm checking, seeding and failure injection."""

        def __init__(
            self,
            config: Any,
            *,
            arm: Arm,
            seed: int | None = None,
            injection: Injection | None = None,
            transport: Callable[..., dict[str, Any]] | None = None,
        ) -> None:
            super().__init__(config)
            self.arm = arm
            self.seed = seed
            self.injection = injection
            self.recorder = Recorder()
            self._transport = transport
            self._tl = threading.local()

        # -- hooks ------------------------------------------------------------------------

        def _summarize_section(
            self, chunk: str, section_num: int, total_sections: int, *args: Any
        ) -> dict[str, Any]:
            if (
                self.injection
                and self.injection.mode == MODE_EXCEPTION
                and section_num == self.injection.slice_index
            ):
                raise RuntimeError('injected slice failure')
            self._tl.phase, self._tl.section = 'slice', section_num
            try:
                output: dict[str, Any] = super()._summarize_section(
                    chunk, section_num, total_sections, *args
                )
            finally:
                self._tl.phase = self._tl.section = None
            self.recorder.slice_outputs[section_num] = output
            return output

        def _combine_sections(self, sections: list[dict[str, Any]], *args: Any) -> dict[str, Any]:
            self.recorder.merge_inputs.append([dict(s) for s in sections])
            self._tl.phase, self._tl.section = 'merge', None
            try:
                combined: dict[str, Any] = super()._combine_sections(sections, *args)
            finally:
                self._tl.phase = None
            return combined

        def chat_completion(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
            phase = getattr(self._tl, 'phase', None) or 'single'
            section = getattr(self._tl, 'section', None)
            system = next((m['content'] for m in messages if m.get('role') == 'system'), '')
            applied = self._applied(phase, system)
            record = CallRecord(phase, section, _sha(system), applied)
            if (
                self.injection
                and self.injection.mode == MODE_PARSE
                and phase == 'slice'
                and section == self.injection.slice_index
            ):
                record.injected = True
                record.applied = None
                self.recorder.add(record)
                return LLMResponse(content=UNPARSEABLE_CONTENT, finish_reason='stop')
            started = time.monotonic()
            self._tl.last_usage = None
            try:
                response = super().chat_completion(messages, **kwargs)
            finally:
                record.latency_s = round(time.monotonic() - started, 3)
                usage = getattr(self._tl, 'last_usage', None) or {}
                record.prompt_tokens = usage.get('prompt_tokens')
                record.completion_tokens = usage.get('completion_tokens')
                self.recorder.add(record)
            record.finish_reason = response.finish_reason
            return response

        def _prepare_payload(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
            payload: dict[str, Any] = super()._prepare_payload(messages, **kwargs)
            # vLLM's OpenAI server accepts `seed`; other OpenAI-compatible servers may reject
            # unknown keys (llm_payloads.py), so it is sent to vLLM only.
            if self.seed is not None and self.config.provider.value == 'vllm':
                payload['seed'] = self.seed
            return payload

        def _send_llm_request(
            self, url: str, payload: dict, headers: dict, timeout: int
        ) -> dict[str, Any]:
            send = self._transport or super()._send_llm_request
            data: dict[str, Any] = send(url, payload, headers, timeout)
            usage = data.get('usage') or {}
            self._tl.last_usage = {
                'prompt_tokens': usage.get('prompt_tokens') or usage.get('input_tokens'),
                'completion_tokens': usage.get('completion_tokens') or usage.get('output_tokens'),
            }
            return data

        # -- applied check ----------------------------------------------------------------

        def _applied(self, phase: str, system: str) -> bool | None:
            if phase == 'slice':
                return self.arm.scope_rules in system
            if phase == 'merge':
                return _MERGE_MARKER in system and self.arm.merge_rules in system
            return None

    return EvalLLMService


@contextlib.contextmanager
def inject_arm(arm: Arm) -> Iterator[None]:
    """Patch the two rule globals for the duration of one summary (sequential use only)."""
    import importlib

    module = importlib.import_module(_MODULE)
    with (
        mock.patch.object(module, 'SECTION_SCOPE_RULES', arm.scope_rules),
        mock.patch.object(module, 'SECTION_MERGE_RULES', arm.merge_rules),
    ):
        yield


@contextlib.contextmanager
def default_prompt(prompt: str) -> Iterator[None]:
    """Serve the default summary prompt without a database (`get_user_active_prompt` reads one)."""
    from app.utils import prompt_manager

    with mock.patch.object(
        prompt_manager, 'get_user_active_prompt', lambda *_args, **_kwargs: prompt
    ):
        yield
