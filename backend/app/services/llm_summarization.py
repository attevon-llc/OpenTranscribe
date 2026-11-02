"""Chunked summary pipeline for :class:`LLMService`."""

import json
import logging
from typing import Any

from app.core.languages import LLM_OUTPUT_LANGUAGES
from app.services.llm_chunking import LLMChunkingMixin
from app.services.llm_json_extract import LLMJsonExtractMixin
from app.services.llm_types import LLMResponse

logger = logging.getLogger(__name__)

SECTION_SCOPE_RULES = (
    " The transcript below is one slice of a longer recording; its boundaries were "
    "produced by an automatic splitter, not by the meeting itself. Report only what "
    "this slice contains and never draw a conclusion about the recording as a whole: "
    "an absence of decisions here means this slice holds none, not that the meeting "
    "reached none. Do not label the summary as a fragment or an incomplete exchange "
    "because the slice starts or ends mid-sentence; only the first slice (abrupt "
    "start) or the last slice (abrupt end) can observe a genuinely truncated "
    "recording."
)

SECTION_MERGE_RULES = (
    " You are merging slices of one recording that were summarized independently and "
    "are given in chronological order. Take the union of their facts: every topic, "
    "decision, action item and follow-up present in any slice must appear in the "
    "final summary, deduplicated, never dropped. A slice that reports nothing "
    "describes only its own span; it can never cancel, weaken or reframe the content "
    "of another slice, whatever wording it uses. Build the summary from the "
    "substantive slices, and describe the recording as truncated only if a slice "
    "explicitly says so, never because slice boundaries are abrupt. A slice carrying "
    '"_error": true could not be processed for technical reasons: say so plainly and '
    "never read it as evidence that nothing happened during that span."
)


class LLMSummarizationMixin(LLMChunkingMixin, LLMJsonExtractMixin):
    """Summary generation over single/multiple chunks and section merge (mixed into ``LLMService``)."""

    def _is_truncated(self, response: "LLMResponse") -> bool:
        """Check if an LLM response was truncated due to token limit."""
        return response.finish_reason in ("length", "max_tokens")

    def generate_summary(
        self,
        transcript: str,
        speaker_data: dict[str, Any] | None = None,
        user_id: int | None = None,
        output_language: str = "en",
        organization_context: str = "",
        prompt_uuid: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate structured summary from transcript.

        Args:
            transcript: Full transcript text with speaker labels
            speaker_data: Optional speaker statistics (talk time, word count, etc.)
            user_id: Optional user ID for loading custom prompts
            output_language: ISO 639-1 code for output language (default: "en")
            organization_context: Organization/project context to inject into prompts

        Returns:
            Structured summary dict with metadata
        """
        from app.utils.prompt_manager import get_user_active_prompt

        prompt_template = get_user_active_prompt(user_id, prompt_uuid=prompt_uuid)

        # Get language name for prompt
        output_language_name = LLM_OUTPUT_LANGUAGES.get(output_language, "English")
        logger.info(f"Generating summary in {output_language_name} ({output_language})")
        if organization_context:
            logger.info(f"Organization context provided ({len(organization_context)} chars)")

        # Chunk transcript using ONLY user's context window setting
        transcript_chunks = self._chunk_transcript_intelligently(transcript)

        if len(transcript_chunks) == 1:
            # Single chunk processing
            logger.info(f"Processing transcript as single section ({len(transcript)} chars)")
            return self._process_single_chunk(
                transcript_chunks[0],
                speaker_data,
                prompt_template,
                output_language_name,
                organization_context,
            )
        else:
            # Multi-chunk processing
            logger.info(f"Processing transcript in {len(transcript_chunks)} sections")
            return self._process_multiple_chunks(
                transcript_chunks,
                speaker_data,
                prompt_template,
                output_language_name,
                organization_context,
            )

    def _build_org_context_block(self, organization_context: str) -> str:
        """Build the organization context block for system prompts."""
        if not organization_context or not organization_context.strip():
            return ""
        return (
            " Use the following organization/project context to inform your analysis"
            " and make the summary more relevant to the organization's domain:"
            f" [{organization_context.strip()}]"
        )

    def _process_single_chunk(
        self,
        transcript: str,
        speaker_data: dict[str, Any] | None,
        prompt_template: str,
        output_language_name: str = "English",
        organization_context: str = "",
    ) -> dict[str, Any]:
        """Process single transcript chunk"""
        formatted_prompt = prompt_template.format(
            transcript=transcript,
            speaker_data=json.dumps(speaker_data or {}, indent=2),
        )

        # Build system message with language instruction
        language_instruction = (
            f" Generate all output text in {output_language_name}."
            if output_language_name != "English"
            else ""
        )
        org_context_block = self._build_org_context_block(organization_context)

        messages = [
            {
                "role": "system",
                "content": f"You are an expert meeting analyst. Analyze transcripts and generate structured summaries in the exact JSON format specified.{language_instruction}{org_context_block}",
            },
            {"role": "user", "content": formatted_prompt},
        ]

        # Use response prefilling to force JSON output (bypasses preamble)
        response = self.chat_completion(
            messages, max_tokens=self.response_tokens, temperature=0.1, prefill_json=True
        )

        # Retry with doubled tokens if response was truncated
        if self._is_truncated(response):
            retry_tokens = min(self.response_tokens * 2, self.user_context_window // 2)
            logger.warning(
                f"Summary response truncated (finish_reason={response.finish_reason}), "
                f"retrying with max_tokens={retry_tokens}"
            )
            response = self.chat_completion(
                messages, max_tokens=retry_tokens, temperature=0.1, prefill_json=True
            )

        return self._parse_summary_response(response, len(transcript))

    def _process_multiple_chunks(
        self,
        chunks: list[str],
        speaker_data: dict[str, Any] | None,
        prompt_template: str,
        output_language_name: str = "English",
        organization_context: str = "",
    ) -> dict[str, Any]:
        """Process multiple transcript chunks in parallel using ThreadPoolExecutor.

        Each chunk is summarized independently, then combined into a final summary.
        Parallelism is capped at min(num_chunks, 4) to avoid overwhelming the LLM API.
        """
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import as_completed

        num_chunks = len(chunks)
        max_workers = min(num_chunks, 4)
        _error_placeholder: dict[str, Any] = {
            "key_points": [],
            "speakers_in_section": [],
            "decisions": [],
            "action_items": [],
            "topics_discussed": [],
        }
        # Pre-fill with error placeholders; every slot is overwritten on success.
        section_summaries: list[dict[str, Any]] = [
            {
                **_error_placeholder,
                "_error": True,
                "key_points": [f"Section {i + 1}: Not processed"],
            }
            for i in range(num_chunks)
        ]

        logger.info(f"Processing {num_chunks} sections in parallel (max_workers={max_workers})")

        def _process_one(index: int, chunk: str) -> tuple[int, dict[str, Any]]:
            """Process a single chunk, returning (index, result)."""
            logger.info(f"Processing section {index + 1}/{num_chunks} ({len(chunk)} chars)")
            return (
                index,
                self._summarize_section(
                    chunk,
                    index + 1,
                    num_chunks,
                    speaker_data,
                    prompt_template,
                    output_language_name,
                    organization_context,
                ),
            )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_one, i, chunk): i for i, chunk in enumerate(chunks)}

            for future in as_completed(futures):
                idx = futures[future]
                try:
                    result_idx, section_summary = future.result()
                    section_summaries[result_idx] = section_summary
                    logger.info(f"Section {result_idx + 1} processing completed successfully")
                except Exception as e:
                    # `section_summaries` is handed to _combine_sections, which json.dumps
                    # it straight into the COMBINER PROMPT — so this string is egressed to
                    # the LLM provider and can be echoed back into the user-visible summary
                    # (#914). An httpx/openai error here quotes the provider base_url, and
                    # for a self-hosted vLLM that is an internal host:port. The section
                    # number and the fact of the failure are what the reader needs.
                    logger.exception(f"Failed to process section {idx + 1}")
                    section_summaries[idx] = {
                        "_error": True,
                        "key_points": [
                            f"Section {idx + 1}: Processing failed ({type(e).__name__})"
                        ],
                        "speakers_in_section": [],
                        "decisions": [],
                        "action_items": [],
                        "topics_discussed": [],
                    }

        # Combine sections into final summary
        logger.info("Combining section summaries into final comprehensive summary")
        return self._combine_sections(
            section_summaries,
            speaker_data,
            prompt_template,
            num_chunks,
            output_language_name,
            organization_context,
        )

    def _summarize_section(
        self,
        chunk: str,
        section_num: int,
        total_sections: int,
        speaker_data: dict[str, Any] | None,
        prompt_template: str,
        output_language_name: str = "English",
        organization_context: str = "",
    ) -> dict[str, Any]:
        """Summarize a single section"""
        formatted_prompt = prompt_template.format(
            transcript=chunk,
            speaker_data=json.dumps(speaker_data or {}, indent=2),
        )

        # Build system message with language instruction
        language_instruction = (
            f" Generate all output text in {output_language_name}."
            if output_language_name != "English"
            else ""
        )
        org_context_block = self._build_org_context_block(organization_context)

        messages = [
            {
                "role": "system",
                "content": (
                    f"You are analyzing section {section_num} of {total_sections}. "
                    "Provide a structured summary of this section."
                    f"{SECTION_SCOPE_RULES}"
                    f"{language_instruction}{org_context_block}"
                ),
            },
            {"role": "user", "content": formatted_prompt},
        ]

        # Use response prefilling for consistent JSON output
        response = self.chat_completion(
            messages, max_tokens=min(4000, self.response_tokens), temperature=0.1, prefill_json=True
        )

        try:
            # Fences only, not `_extract_json_from_response`: an Anthropic prefill reply
            # arrives without its opening brace, and brace-bound extraction would then
            # return the first nested object instead of leaving the text for json_repair.
            content = self._strip_markdown_fences(response.content.strip())

            parsed_result: dict[str, Any] = json.loads(content)
            return parsed_result
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse section {section_num} JSON: {e}")

            # Same recovery as `_parse_summary_response` for a mid-document break
            # (e.g. an unescaped literal `"` in the source transcript reproduced
            # verbatim in a string value) — see that method for the full
            # rationale. Worth doing here too: an unrecovered section becomes a
            # "Failed to parse" placeholder that gets stitched verbatim into the
            # combine-step prompt, degrading the final summary for that section.
            try:
                from json_repair import loads as repair_json_loads

                lib_repaired = repair_json_loads(content)
            except Exception:
                lib_repaired = None

            if isinstance(lib_repaired, dict) and lib_repaired:
                logger.info(f"json_repair recovered section {section_num}'s summary")
                return lib_repaired

            logger.error(f"Section {section_num} JSON repair also failed")
            return {
                "_error": True,
                "key_points": [f"Section {section_num}: Failed to parse structured summary"],
                "speakers_in_section": [],
                "decisions": [],
                "action_items": [],
                "topics_discussed": [],
            }

    def _combine_sections(
        self,
        sections: list[dict[str, Any]],
        speaker_data: dict[str, Any] | None,
        prompt_template: str,
        total_sections: int,
        output_language_name: str = "English",
        organization_context: str = "",
    ) -> dict[str, Any]:
        """Combine multiple section summaries into final summary"""
        # Numbering the slices explicitly is what lets the model tell "nothing in
        # this span" apart from "nothing in the whole recording".
        labelled_sections = [
            {"section": i + 1, "of": len(sections), "summary": section}
            for i, section in enumerate(sections)
        ]
        combined_content = (
            "SECTION SUMMARIES TO COMBINE — independent slices of ONE recording, "
            f"in chronological order:\n{json.dumps(labelled_sections, indent=2)}"
        )

        formatted_prompt = prompt_template.format(
            transcript=combined_content,
            speaker_data=json.dumps(speaker_data or {}, indent=2),
        )

        # Build system message with language instruction
        language_instruction = (
            f" Generate all output text in {output_language_name}."
            if output_language_name != "English"
            else ""
        )
        org_context_block = self._build_org_context_block(organization_context)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are combining multiple section summaries into a "
                    "comprehensive BLUF format summary."
                    f"{SECTION_MERGE_RULES}"
                    f"{language_instruction}{org_context_block}"
                ),
            },
            {"role": "user", "content": formatted_prompt},
        ]

        try:
            # Use response prefilling for final combined summary
            response = self.chat_completion(
                messages, max_tokens=self.response_tokens, temperature=0.1, prefill_json=True
            )

            # Retry with doubled tokens if response was truncated
            if self._is_truncated(response):
                retry_tokens = min(self.response_tokens * 2, self.user_context_window // 2)
                logger.warning(
                    f"Combined summary truncated (finish_reason={response.finish_reason}), "
                    f"retrying with max_tokens={retry_tokens}"
                )
                response = self.chat_completion(
                    messages, max_tokens=retry_tokens, temperature=0.1, prefill_json=True
                )

            return self._parse_summary_response(
                response,
                0,
                {"sections_processed": total_sections, "processing_method": "multi-section"},
            )
        except Exception as e:
            # metadata.error lands in media_file.summary_data and is rendered
            # back to the requesting user (api/endpoints/summarization.py) --
            # never interpolate the raw exception text, only its class name.
            logger.exception("Failed to combine sections")
            return {
                "bluf": "Multi-section summary generation completed with partial results.",
                "brief_summary": f"Summary generated from {len(sections)} sections.",
                "major_topics": [],
                "action_items": [],
                "key_decisions": [],
                "follow_up_items": [],
                "metadata": {
                    "provider": self.config.provider.value,
                    "model": self.config.model,
                    "sections_processed": len(sections),
                    "error": f"Section combining failed ({type(e).__name__})",
                },
            }
