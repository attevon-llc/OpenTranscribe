"""Synchronous drivers for the async LLM stream API, for tests that feed canned lines.

Production parses a provider's stream incrementally (``llm_stream.parse_stream`` over an
``httpx`` response's ``aiter_lines``); these run the same parsers over a plain list so a
test can assert on the events directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from collections.abc import Iterable
from collections.abc import Iterator

from app.services.llm_stream import AnthropicSSEParser
from app.services.llm_stream import LLMStreamEvent
from app.services.llm_stream import OllamaNDJSONParser
from app.services.llm_stream import OpenAISSEParser
from app.services.llm_stream import StreamParser
from app.services.llm_stream import parse_stream


async def collect[T](stream: AsyncIterator[T]) -> list[T]:
    """Every item an async iterator yields."""
    return [item async for item in stream]


def drain[T](stream: AsyncIterator[T]) -> list[T]:
    """:func:`collect`, from synchronous test code."""
    return asyncio.run(collect(stream))


async def _lines(lines: Iterable[str]) -> AsyncIterator[str]:
    for line in lines:
        yield line


def _parse(parser: StreamParser, lines: Iterable[str]) -> Iterator[LLMStreamEvent]:
    return iter(drain(parse_stream(parser, _lines(lines))))


def parse_openai_sse(lines: Iterable[str]) -> Iterator[LLMStreamEvent]:
    return _parse(OpenAISSEParser(), lines)


def parse_anthropic_sse(lines: Iterable[str]) -> Iterator[LLMStreamEvent]:
    return _parse(AnthropicSSEParser(), lines)


def parse_ollama_ndjson(lines: Iterable[str]) -> Iterator[LLMStreamEvent]:
    return _parse(OllamaNDJSONParser(), lines)
