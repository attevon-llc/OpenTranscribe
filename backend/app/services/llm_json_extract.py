"""JSON extraction, truncated-JSON repair and summary-response parsing for :class:`LLMService`."""

import json
import logging
from typing import Any

from app.services.llm_host import LLMHost
from app.services.llm_types import LLMResponse
from app.utils.llm_log_safety import describe_llm_text
from app.utils.llm_log_safety import log_llm_text_excerpt

logger = logging.getLogger(__name__)


class LLMJsonExtractMixin(LLMHost):
    """Pull JSON out of model output and parse a summary response (mixed into ``LLMService``)."""

    def _parse_summary_response(
        self,
        response: LLMResponse,
        transcript_length: int,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Parse LLM response into flexible structured summary.

        IMPORTANT: This method accepts ANY valid JSON structure from custom AI prompts.
        No field validation is performed - we trust the LLM to follow the prompt format.
        """
        try:
            content = response.content.strip()

            # Handle response prefilling: if content starts with partial JSON due to prefill,
            # prepend the opening brace that was used in prefilling
            if not content.startswith("{") and not content.startswith("```"):
                content = "{" + content

            # Extract JSON from code blocks
            if content.startswith("```json") and content.endswith("```"):
                content = content[7:-3].strip()
            elif content.startswith("```") and content.endswith("```"):
                content = content[3:-3].strip()

            # Parse JSON - accept ANY structure
            summary_data: dict[str, Any] = json.loads(content)

            # NO FIELD VALIDATION - accept any structure from custom prompts

            # Add metadata
            metadata = {
                "provider": self.config.provider.value,
                "model": self.config.model,
                "usage_tokens": response.usage_tokens,
                "transcript_length": transcript_length,
                "user_context_window": self.user_context_window,
            }

            if extra_metadata:
                metadata.update(extra_metadata)

            summary_data["metadata"] = metadata

            logger.info(
                f"Successfully parsed flexible summary with fields: {list(summary_data.keys())}"
            )
            return summary_data

        except json.JSONDecodeError as e:
            logger.warning(f"Initial JSON parse failed: {e}")

            def _repaired_metadata() -> dict[str, Any]:
                metadata = {
                    "provider": self.config.provider.value,
                    "model": self.config.model,
                    "usage_tokens": response.usage_tokens,
                    "transcript_length": transcript_length,
                    "user_context_window": self.user_context_window,
                    "json_repaired": True,
                }
                if extra_metadata:
                    metadata.update(extra_metadata)
                return metadata

            # Attempt JSON repair for truncated responses (a response cut off at
            # the token limit, i.e. finish_reason="length" — nothing to close mid-
            # document, only an open string/array/object at the very end).
            repaired = self._repair_truncated_json(content)
            if repaired is not None:
                logger.info("JSON repair succeeded, using repaired summary")
                repaired["metadata"] = _repaired_metadata()
                return repaired

            # The bracket-closer above only fixes a trailing cut-off. It does
            # nothing for a MID-document syntax break, which produces a different
            # symptom: json.loads fails well past the actual defect (e.g.
            # "Expecting ':' delimiter" deep into an otherwise well-formed
            # document) rather than at end-of-string, because there is no open
            # bracket to close. Measured live: gemma-4-e4b, asked to summarize a
            # transcript that contains a literal `"` (a spoken aside quoting a
            # feet/inches measurement, e.g. `6'6"`), sometimes reproduces it
            # unescaped inside a JSON string value — one stray quote desyncs the
            # parser's in-string tracking for everything that follows.
            # `json_repair` (zero transitive deps, MIT) handles this class
            # directly instead of us hand-rolling a quote-scanner; it never
            # raises, so a non-dict/empty result means it couldn't make sense of
            # the content either.
            try:
                from json_repair import loads as repair_json_loads

                lib_repaired = repair_json_loads(content)
            except Exception:
                lib_repaired = None

            if isinstance(lib_repaired, dict) and lib_repaired:
                logger.info("json_repair recovered a malformed (non-truncated) summary response")
                lib_repaired["metadata"] = _repaired_metadata()
                return lib_repaired

            logger.exception(
                "Summary JSON repair also failed (%s)", describe_llm_text(response.content)
            )
            log_llm_text_excerpt(logger, "Unparseable summary response", response.content)

            # Return minimal error structure. error_detail/metadata.error are
            # rendered back to the requesting user via media_file.summary_data
            # (api/endpoints/summarization.py) -- never interpolate the raw
            # exception text, only its class name. Low-value fix (a
            # json.JSONDecodeError message is already just "column N" style
            # position info, never a secret), kept for consistency with the
            # other #914 sites in this module.
            return {
                "error": "JSON parsing failed",
                "error_detail": f"JSON parsing failed ({type(e).__name__})",
                "raw_response_preview": response.content[:500],
                "metadata": {
                    "provider": self.config.provider.value,
                    "model": self.config.model,
                    "error": f"JSON parsing failed ({type(e).__name__})",
                    "user_context_window": self.user_context_window,
                },
            }

    @staticmethod
    def _scan_json_structure(text: str) -> tuple[bool, list[str]]:
        """Walk *text* and return (ends inside a string, stack of still-open '{' / '[')."""
        in_string = False
        escape_next = False
        stack: list[str] = []  # tracks '{' and '['

        for char in text:
            if escape_next:
                escape_next = False
                continue
            if char == "\\":
                if in_string:
                    escape_next = True
                continue
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if char in ("{", "["):
                stack.append(char)
            elif char == "}":
                if stack and stack[-1] == "{":
                    stack.pop()
            elif char == "]" and stack and stack[-1] == "[":
                stack.pop()
        return in_string, stack

    @staticmethod
    def _closing_suffix(text: str, in_string: bool, stack: list[str]) -> str:
        """Characters that close an unterminated string and every still-open container."""
        repair = ""

        # Close unterminated string
        if in_string:
            repair += '"'

        # Check if we're mid-value in an object (e.g., truncated after a key's colon)
        # The closing brackets will handle structure
        stripped = (text + repair).rstrip()
        if stripped and stripped[-1] in (",", ":"):
            repair += '""'

        # Close open arrays and objects in reverse order
        for bracket in reversed(stack):
            if bracket == "{":
                repair += "}"
            elif bracket == "[":
                repair += "]"
        return repair

    def _repair_truncated_json(self, content: str) -> dict[str, Any] | None:
        """
        Attempt to repair truncated JSON by closing open strings, arrays, and objects.

        Returns parsed dict on success, None on failure.
        """
        try:
            text = content.rstrip()
            in_string, stack = self._scan_json_structure(text)
            repaired_content = text + self._closing_suffix(text, in_string, stack)
            result: dict[str, Any] = json.loads(repaired_content)
            return result

        except (json.JSONDecodeError, Exception) as e:
            logger.debug(f"JSON repair failed: {e}")
            return None

    def _strip_markdown_fences(self, content: str) -> str:
        """Remove a leading markdown code fence (differs from the other JSON-from-LLM parsers)."""
        if content.startswith("```json") and "```" in content[7:]:
            fence_end = content.find("```", 7)
            return content[7:fence_end].strip()

        if content.startswith("```") and "```" in content[3:]:
            fence_end = content.find("```", 3)
            result = content[3:fence_end].strip()
            if result.startswith(("json", "JSON")):
                return result[4:].lstrip()
            return result

        return content

    def _find_json_object_bounds(self, content: str) -> str:
        """Find and extract complete JSON object from content."""
        json_start = content.find("{")
        if json_start > 0:
            content = content[json_start:]

        if not content.startswith("{"):
            return content

        brace_count = 0
        for i, char in enumerate(content):
            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
                if brace_count == 0:
                    return content[: i + 1]

        return content

    def _extract_json_from_response(self, content: str) -> str:
        """Extract and clean JSON from LLM response content."""
        content = self._strip_markdown_fences(content)
        return self._find_json_object_bounds(content)
