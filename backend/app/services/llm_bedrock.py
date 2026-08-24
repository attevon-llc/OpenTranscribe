"""Amazon Bedrock provider, via the Converse / ConverseStream APIs.

Bedrock is the AWS-native way to reach a hosted LLM, and it is the only provider
here that is *not* an HTTP endpoint we POST to: it is a boto3 SDK call. That is
why it lives in its own module rather than as a fourth parser in ``llm_stream``.

**Why Converse and not InvokeModel.** ``InvokeModel`` takes each vendor's native
body shape — Anthropic's Messages format for Claude, a different one for Nova,
another for Llama — so supporting it means a per-vendor adapter and a per-vendor
usage parser. ``Converse`` is a single request/response shape across *every*
Bedrock model, including a single ``usage`` block. One integration reaches Claude,
Nova, Llama and Mistral, and the metering path has one shape to parse. For
Claude-only deployments that need same-day Anthropic API parity (Batches, Files,
fast mode), the Anthropic-operated Mantle client is the alternative — it is not
used here because it would not cover the non-Claude models.

**Why this fits the existing architecture unchanged.** ``chat_completion_stream``
is a *synchronous* ``Iterator[LLMStreamEvent]``; the chat service drives it with
``iterate_in_threadpool`` and cancels via a ``threading.Event``
(``services/chat/service.py``). boto3's ``EventStream`` is a synchronous iterator
cancelled the same way, so it drops in without ``aioboto3`` or any async plumbing.

**Credentials.** Resolved by boto3's standard chain — instance role, task role,
profile, or environment. Bedrock is therefore the one provider with no API key to
store, which is why ``api_key`` is not consulted anywhere below.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Any

from app.core import constants as C  # noqa: N812
from app.services.llm_stream import LLMStreamEvent

logger = logging.getLogger(__name__)

# Cross-region inference profiles are invoked with a geography-prefixed model ID
# (``us.anthropic.claude-...``) rather than the bare foundation-model ID. The prefix
# lets AWS route to a nearby region when the home region is saturated, which is the
# difference between a throttle and a served request at peak.
GEO_PREFIXES = ("us.", "eu.", "apac.", "jp.", "au.", "global.", "us-gov.")


class BedrockNotConfiguredError(RuntimeError):
    """boto3 is missing, or no region is set. Surfaced as a normal provider error."""


def _client(region: str):
    """Build a ``bedrock-runtime`` client.

    Not cached: botocore clients are not documented as thread-safe for concurrent
    use, and this runs under a threadpool. Construction is cheap relative to an
    LLM call, so a per-call client trades negligible latency for no shared state.
    """
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - boto3 is a hard dependency
        raise BedrockNotConfiguredError("boto3 is not installed") from exc

    if not region:
        raise BedrockNotConfiguredError(
            "No AWS region configured for Bedrock (set BEDROCK_REGION or AWS_REGION)"
        )
    return boto3.client("bedrock-runtime", region_name=region)


def resolve_model_id(model: str, region: str) -> str:
    """Return the model ID to invoke, applying a geo prefix when one is needed.

    A bare foundation-model ID works only where that model is provisioned in the
    calling region; the prefixed inference-profile ID load-balances across the
    geography. We prefix only when the caller has not already done so and has not
    passed a full ARN — AWS rotates model IDs often enough that hardcoding a
    catalog here would rot, so an explicit ID from settings always wins.
    """
    ident = (model or "").strip()
    if not ident:
        raise BedrockNotConfiguredError("No Bedrock model ID configured")
    # A full inference-profile ARN or an already-prefixed ID is used verbatim.
    if ident.startswith("arn:") or ident.startswith(GEO_PREFIXES):
        return ident
    prefix = C.BEDROCK_GEO_PREFIX_BY_REGION.get(region.split("-")[0], "")
    return f"{prefix}{ident}" if prefix else ident


def split_system_messages(
    messages: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split OpenAI-style messages into Converse ``system`` blocks and turns.

    Converse models system prompts as a **separate top-level field**, and rejects a
    ``system`` role inside ``messages``. It also requires strictly alternating
    user/assistant turns, so consecutive same-role messages are merged rather than
    passed through — the chat history builder can legitimately emit two user turns
    in a row after an edit-and-resubmit.

    Returns:
        ``(system_blocks, converse_messages)``.
    """
    system_blocks: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role", "user")
        content = str(message.get("content") or "")
        if not content:
            continue
        if role == "system":
            system_blocks.append({"text": content})
            continue
        role = "assistant" if role == "assistant" else "user"
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"][0]["text"] += "\n\n" + content
            continue
        turns.append({"role": role, "content": [{"text": content}]})

    # Converse requires the first turn to be `user`; a leading assistant turn is
    # only reachable through a malformed history, but it would 400 the whole call.
    if turns and turns[0]["role"] == "assistant":
        turns.insert(0, {"role": "user", "content": [{"text": "(continued)"}]})

    return system_blocks, turns


def build_request_metadata(attribution: dict[str, Any] | None) -> dict[str, str]:
    """Stamp tenant attribution onto the request for AWS-side cost allocation.

    ``requestMetadata`` rides into Bedrock's own invocation logs, so our usage
    records can be reconciled against the AWS bill rather than merely trusted.
    AWS caps this at 16 entries with keys and values of 256 characters, and
    rejects the whole request if any entry violates that — so values are coerced
    and truncated here rather than at the call site.
    """
    if not attribution:
        return {}
    metadata: dict[str, str] = {}
    for key, value in list(attribution.items())[:16]:
        if value is None:
            continue
        metadata[str(key)[:256]] = str(value)[:256]
    return metadata


def _usage_event(usage: dict[str, Any]) -> LLMStreamEvent:
    """Build a usage event from a Converse ``usage`` block.

    Cache tokens are reported separately and priced differently from ordinary
    input tokens — reads far below and writes above — so they are carried as
    distinct fields rather than summed into ``prompt_tokens``.
    """
    return LLMStreamEvent(
        type="usage",
        prompt_tokens=usage.get("inputTokens"),
        completion_tokens=usage.get("outputTokens"),
        cache_read_tokens=usage.get("cacheReadInputTokens"),
        cache_write_tokens=usage.get("cacheWriteInputTokens"),
    )


#: Bedrock reports mid-stream failures as exception members of the event stream
#: rather than raising, so they have to be matched by key.
_STREAM_ERROR_KEYS = (
    "internalServerException",
    "modelStreamErrorException",
    "validationException",
    "serviceUnavailableException",
)


def translate_stream_event(
    raw: dict[str, Any], model_id: str
) -> tuple[LLMStreamEvent | None, str | None]:
    """Translate one ConverseStream event into our normalized form.

    Split out from the stream loop so each event shape can be unit-tested against a
    recorded payload without standing up a Bedrock client.

    Returns:
        ``(event, stop_reason)``. ``event`` is None for events that carry no output
        (``messageStart``, ``contentBlockStop``); ``stop_reason`` is set only by
        ``messageStop``.
    """
    if "contentBlockDelta" in raw:
        text = raw["contentBlockDelta"].get("delta", {}).get("text")
        return (LLMStreamEvent(type="delta", text=str(text)) if text else None), None

    if "messageStop" in raw:
        return None, raw["messageStop"].get("stopReason")

    if "metadata" in raw:
        # The metadata event arrives last and is the only place usage is reported
        # on a streamed Converse call.
        usage = raw["metadata"].get("usage") or {}
        return (_usage_event(usage) if usage else None), None

    if "throttlingException" in raw:
        message = raw["throttlingException"].get("message", "throttled")
        logger.warning("Bedrock throttled for %s: %s", model_id, message)
        return LLMStreamEvent(type="error", message=f"Bedrock throttled: {message}"), None

    for key in _STREAM_ERROR_KEYS:
        if key in raw:
            message = (raw[key] or {}).get("message", key)
            logger.error("Bedrock stream error for %s: %s", model_id, message)
            return LLMStreamEvent(type="error", message=f"Bedrock error: {message}"), None

    return None, None


def stream_converse(
    *,
    model: str,
    region: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float | None = None,
    top_p: float | None = None,
    cancel_event: threading.Event | None = None,
    attribution: dict[str, Any] | None = None,
) -> Iterator[LLMStreamEvent]:
    """Stream a completion from Bedrock, yielding normalized events.

    Mirrors the contract of the HTTP stream parsers: text arrives as ``delta``
    events, token counts as exactly one ``usage`` event, and the stream always
    terminates with exactly one ``done`` or ``error``.

    Args:
        model: Foundation-model ID, inference-profile ID, or profile ARN.
        region: AWS region to call.
        messages: Chat messages in OpenAI format (``system`` role permitted).
        max_tokens: Output ceiling.
        temperature: Sampling temperature, omitted when None. Some newer models
            reject non-default sampling parameters, so callers that don't need it
            should leave it unset rather than passing a default.
        top_p: Nucleus sampling, sent as ``topP``. Omitted when None for the same
            reason as temperature.
        cancel_event: Set by the caller on client disconnect or Stop.
        attribution: Tenant/user identifiers for ``requestMetadata``.

    Yields:
        :class:`LLMStreamEvent` values.
    """
    try:
        client = _client(region)
        model_id = resolve_model_id(model, region)
    except BedrockNotConfiguredError as exc:
        yield LLMStreamEvent(type="error", message=str(exc))
        return

    system_blocks, turns = split_system_messages(messages)
    if not turns:
        yield LLMStreamEvent(type="error", message="No messages to send to Bedrock")
        return

    inference_config: dict[str, Any] = {"maxTokens": max_tokens}
    if temperature is not None:
        inference_config["temperature"] = temperature
    if top_p is not None:
        inference_config["topP"] = top_p

    request: dict[str, Any] = {
        "modelId": model_id,
        "messages": turns,
        "inferenceConfig": inference_config,
    }
    if system_blocks:
        request["system"] = system_blocks
    metadata = build_request_metadata(attribution)
    if metadata:
        request["requestMetadata"] = metadata

    logger.info(
        "Starting Bedrock stream: %s (region=%s, %d turns, %d system blocks)",
        model_id,
        region,
        len(turns),
        len(system_blocks),
    )

    try:
        response = client.converse_stream(**request)
    except Exception as exc:  # noqa: BLE001 — surfaced in-band like every other provider
        logger.error("Bedrock ConverseStream failed for %s: %s", model_id, exc)
        yield LLMStreamEvent(type="error", message=f"Bedrock error: {exc}")
        return

    finish_reason: str | None = None
    saw_usage = False
    try:
        for raw in response["stream"]:
            if cancel_event is not None and cancel_event.is_set():
                yield LLMStreamEvent(type="done", finish_reason="cancelled")
                return

            event, stop_reason = translate_stream_event(raw, model_id)
            if stop_reason is not None:
                finish_reason = stop_reason
            if event is None:
                continue
            if event.type == "usage":
                saw_usage = True
            yield event
            if event.type == "error":
                return
    except Exception as exc:  # noqa: BLE001
        logger.error("Bedrock stream interrupted for %s: %s", model_id, exc)
        yield LLMStreamEvent(type="error", message=f"Bedrock stream interrupted: {exc}")
        return

    if not saw_usage:
        # Every current Bedrock model emits a metadata event; a miss means an
        # unexpected shape, and metering must be able to tell that apart from a
        # genuinely free call.
        logger.warning("Bedrock stream for %s produced no usage metadata", model_id)

    yield LLMStreamEvent(type="done", finish_reason=finish_reason or "stop")
