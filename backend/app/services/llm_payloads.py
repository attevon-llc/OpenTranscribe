"""Provider request payloads and response extraction for :class:`LLMService`."""

import json
import logging
from typing import Any

from app.services.llm_host import LLMHost
from app.services.llm_types import LLMProvider

logger = logging.getLogger(__name__)

# OpenAI reasoning models that don't support temperature/sampling parameters
# These models use internal reasoning processes incompatible with temperature control
# See: https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/reasoning
OPENAI_REASONING_MODEL_PREFIXES = (
    "o1",  # o1, o1-mini, o1-preview
    "o3",  # o3, o3-mini
    "o4",  # o4-mini
    "gpt-5",  # gpt-5 series
)


class LLMPayloadMixin(LLMHost):
    """Per-provider payload building and response parsing (mixed into ``LLMService``)."""

    def _is_reasoning_model(self) -> bool:
        """
        Check if the current model is an OpenAI reasoning model.

        OpenAI reasoning models (o1, o3, o4, gpt-5 series) don't support
        temperature, top_p, presence_penalty, or frequency_penalty parameters.
        These parameters must be omitted entirely from API requests.
        """
        if self.config.provider not in [LLMProvider.OPENAI, LLMProvider.OPENROUTER]:
            return False

        model_lower = self.config.model.lower()
        return any(model_lower.startswith(prefix) for prefix in OPENAI_REASONING_MODEL_PREFIXES)

    def _prepare_claude_payload(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        """Prepare payload for Claude/Anthropic API."""
        system_message = ""
        user_messages = []

        for msg in messages:
            if msg.get("role") == "system":
                system_message = msg.get("content", "")
            elif msg.get("role") in ["user", "assistant"]:
                user_messages.append({"role": msg["role"], "content": msg["content"]})

        # Add response prefilling for JSON output if requested
        if (
            kwargs.get("prefill_json", False)
            and user_messages
            and user_messages[-1]["role"] == "user"
        ):
            user_messages.append({"role": "assistant", "content": "{"})

        # The Messages API rejects a final assistant (prefill) turn that ends in
        # whitespace, and an empty one; a prefill like "<thinking>\n" is natural to write
        # (issue #1043).
        if user_messages and user_messages[-1]["role"] == "assistant":
            stripped = str(user_messages[-1]["content"] or "").rstrip()
            if stripped:
                user_messages[-1] = {"role": "assistant", "content": stripped}
            else:
                user_messages.pop()

        payload = {
            "model": self.config.model,
            "messages": user_messages,
            "max_tokens": kwargs.get("max_tokens", self.config.response_tokens),
            "temperature": kwargs.get("temperature", self.config.temperature),
        }

        if system_message:
            payload["system"] = system_message

        return payload

    def _prepare_ollama_payload(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        """Prepare payload for Ollama native API."""
        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": kwargs.get("temperature", self.config.temperature),
                "num_predict": kwargs.get("max_tokens", self.config.response_tokens),
                "num_ctx": kwargs.get("num_ctx", self.user_context_window),
            },
        }

        if "format" in kwargs:
            payload["format"] = kwargs["format"]

        return payload

    def _prepare_openai_payload(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        """Prepare payload for OpenAI-compatible APIs."""
        payload = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.config.response_tokens),
            "stream": False,
        }

        # Reasoning models don't support temperature
        if not self._is_reasoning_model():
            payload["temperature"] = kwargs.get("temperature", self.config.temperature)
            # Only when the caller asked for it. Sending a default would override
            # whatever the provider or model already tunes to, and reasoning
            # models reject it outright (handled by the branch above).
            if kwargs.get("top_p") is not None:
                payload["top_p"] = kwargs["top_p"]
        else:
            logger.info(
                f"Reasoning model detected ({self.config.model}): "
                f"omitting temperature and sampling parameters"
            )

        # Add vLLM-specific parameters
        if self.config.provider == LLMProvider.VLLM:
            payload.update(
                {
                    "top_p": kwargs.get("top_p", 0.9),
                    "frequency_penalty": kwargs.get("frequency_penalty", 0.0),
                    "presence_penalty": kwargs.get("presence_penalty", 0.0),
                }
            )
            # Reasoning must be ACTIVATED, or vLLM streams it as the answer (issue #439).
            #
            # A reasoning-capable chat template only emits its "start of thought"
            # token when asked (gemma4 and qwen3 both spell that ask
            # `enable_thinking`). Unasked, gemma4's template instead appends an
            # already-closed empty thought channel to the prompt — the model reasons
            # regardless, so its *generated* text carries no opener, only a bare
            # closer. vLLM's streaming reasoning parser enters reasoning mode on the
            # opener alone, so it never fires and the whole chain-of-thought is
            # streamed on `delta.content`; worse, the gemma4 parser disables
            # special-token stripping to protect its boundary tokens, so the bare
            # closer reaches the answer as a literal control token.
            #
            # Asking for thinking is therefore the fix, and it is the server's own
            # mechanism: the parser then splits the block itself and reasoning
            # arrives on `delta.reasoning` / `delta.reasoning_content`, both of which
            # `llm_stream.parse_openai_sse` already routes to the collapsible display.
            # A template with no such flag simply ignores an unused kwarg.
            #
            # vLLM only: `chat_template_kwargs` is its extension, and "custom"
            # OpenAI-clones 400 on unknown payload keys — the same reason they are
            # excluded from `llm_stream.USAGE_OPTION_PROVIDERS`.
            #
            # Three arms, because they are three different requests (issue #64):
            #   absent / True -> {"enable_thinking": True}   the #439 fix, the default
            #   False         -> {"enable_thinking": False}  the measured "off" arm
            #   None          -> the key omitted entirely     the probe's control arm
            # `False` sends the key rather than dropping it: on gemma-4-e4b the two
            # are byte-identical, but on a template where the switch actually works
            # they are not — and shipping the control arm as the off switch would
            # mean the toggle a user presses is not the request that was measured.
            enable_thinking = kwargs.get("enable_thinking", True)
            if enable_thinking is not None:
                payload["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}

        return payload

    def _prepare_payload(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        """
        Prepare request payload for the API based on provider requirements.

        Converts standard OpenAI-format messages to provider-specific formats
        and adds appropriate parameters for each LLM provider.

        Args:
            messages: List of message dictionaries in OpenAI format
            **kwargs: Additional parameters to override defaults
                - prefill_json: If True, adds JSON prefill to force structured output

        Returns:
            Dictionary containing the API request payload

        Note:
            - Claude/Anthropic: Separates system messages from user/assistant messages
            - Ollama: Uses native /api/chat format with messages array
            - Other providers: Use standard OpenAI format with provider-specific params
            - Response prefilling: For Claude, adds assistant message with "{" to force JSON
        """
        if self.config.provider in [LLMProvider.CLAUDE, LLMProvider.ANTHROPIC]:
            return self._prepare_claude_payload(messages, **kwargs)

        if self.config.provider == LLMProvider.OLLAMA:
            return self._prepare_ollama_payload(messages, **kwargs)

        return self._prepare_openai_payload(messages, **kwargs)

    def _extract_claude_response(self, data: dict) -> tuple[str, int | None, str | None]:
        """Extract content, usage tokens, and finish reason from Claude/Anthropic response."""
        if "content" not in data or not data["content"]:
            raise Exception("No content in Claude response")

        content_blocks = data["content"]
        if isinstance(content_blocks, list) and content_blocks:
            content = content_blocks[0].get("text", "")
        else:
            content = str(content_blocks)

        usage_tokens = None
        if "usage" in data:
            usage_tokens = data["usage"].get("output_tokens", 0) + data["usage"].get(
                "input_tokens", 0
            )

        finish_reason = data.get("stop_reason")
        return content, usage_tokens, finish_reason

    def _extract_ollama_response(self, data: dict) -> tuple[str, int | None, str | None]:
        """Extract content, usage tokens, and finish reason from Ollama response."""
        if "message" not in data:
            logger.error(
                f"Ollama response missing 'message' field. Response keys: {list(data.keys())}"
            )
            logger.debug(f"Full Ollama response: {json.dumps(data, indent=2)}")
            raise Exception("No message in Ollama response")

        content = data["message"].get("content", "")
        finish_reason = data.get("done_reason", "stop")

        if not content:
            message = data.get("message")
            message_keys = sorted(message) if isinstance(message, dict) else type(message).__name__
            # Keys only: an empty-content message can still carry the model's
            # reasoning under "thinking" (issue #1022).
            logger.error(
                f"Ollama message field exists but content is empty. Message keys: {message_keys}"
            )
            logger.debug(f"Full Ollama response: {json.dumps(data, indent=2)}")

        usage_tokens = None
        if "prompt_eval_count" in data and "eval_count" in data:
            usage_tokens = data.get("prompt_eval_count", 0) + data.get("eval_count", 0)

        return content, usage_tokens, finish_reason

    def _extract_openai_response(self, data: dict) -> tuple[str, int | None, str | None]:
        """Extract content, usage tokens, and finish reason from OpenAI-compatible response."""
        if "choices" not in data or not data["choices"]:
            raise Exception("No choices in LLM response")

        choice = data["choices"][0]
        # `or ""`, not a default: a reasoning model that spends its whole token
        # budget inside the thought channel returns `"content": null` with
        # `finish_reason: "length"` — the key is present, so `.get(k, "")` yields
        # None and every caller that concatenates or strips this blows up.
        content = choice.get("message", {}).get("content") or ""
        finish_reason = choice.get("finish_reason")

        usage_tokens = None
        if "usage" in data:
            usage_tokens = data["usage"].get("total_tokens")

        return content, usage_tokens, finish_reason

    def _extract_response_content(self, data: dict) -> tuple[str, int | None, str | None]:
        """Extract content from response based on provider type."""
        if self.config.provider in [LLMProvider.CLAUDE, LLMProvider.ANTHROPIC]:
            return self._extract_claude_response(data)
        elif self.config.provider == LLMProvider.OLLAMA:
            return self._extract_ollama_response(data)
        else:
            return self._extract_openai_response(data)
