"""LLM speaker identification for :class:`LLMService`."""

import json
import logging

from app.core.constants import LLM_OUTPUT_LANGUAGES
from app.services.llm_json_extract import LLMJsonExtractMixin
from app.services.llm_types import LLMResponse
from app.utils.llm_log_safety import describe_llm_text
from app.utils.llm_log_safety import log_llm_text_excerpt
from app.utils.speaker_labels import looks_like_a_person_name

logger = logging.getLogger(__name__)


class LLMSpeakerIdMixin(LLMJsonExtractMixin):
    """Speaker identification prompt building and response parsing (mixed into ``LLMService``)."""

    def _build_known_speakers_context(self, known_speakers: list) -> str:
        """Build context string from known speaker profiles."""
        if not known_speakers:
            return "\n\nNo known speaker profiles provided for comparison.\n"

        context = "\n\nKNOWN SPEAKER PROFILES:\n"
        for i, speaker in enumerate(known_speakers[:15]):  # Limit to prevent token overflow
            description = speaker.get("description", "No description available")
            context += f"{i + 1}. {speaker['name']}: {description}\n"
        return context

    def _truncate_transcript_for_speakers(self, transcript: str, available_tokens: int) -> str:
        """Truncate transcript while preserving beginning and end context."""
        max_chars = available_tokens * 3  # Rough char to token ratio
        if len(transcript) <= max_chars:
            return transcript

        half_length = max_chars // 2
        return (
            transcript[:half_length]
            + "\n\n[... middle content truncated ...]\n\n"
            + transcript[-half_length:]
        )

    def _validate_speaker_prediction(self, pred: dict) -> bool:
        """Validate a single speaker prediction has required fields and sufficient confidence."""
        if not isinstance(pred, dict):
            return False

        required_fields = ["speaker_label", "predicted_name", "confidence"]
        if not all(field in pred for field in required_fields):
            logger.warning(
                "Skipping speaker prediction with missing fields (has: %s)", sorted(pred.keys())
            )
            return False

        if not looks_like_a_person_name(pred.get("predicted_name")):
            logger.info("Skipping speaker prediction whose predicted_name is not a name")
            return False

        confidence = pred.get("confidence", 0.0)
        return isinstance(confidence, (int, float)) and confidence >= 0.5

    def _parse_speaker_identification_response(self, response: LLMResponse) -> dict:
        """Parse and validate speaker identification LLM response."""
        content = self._extract_json_from_response(response.content.strip())

        try:
            result = json.loads(content)
        except json.JSONDecodeError as e:
            # "error" here has no reader today (only speaker_predictions is
            # consumed, by tasks/speaker_identification_task.py) -- sanitized
            # anyway, on the same "never echo raw exception text" rule as every
            # other #914 site, since a dead field is one caller-change away from
            # becoming a leak the moment something starts reading it.
            logger.exception(
                "Failed to parse LLM identification response as JSON (%s)",
                describe_llm_text(response.content),
            )
            log_llm_text_excerpt(logger, "Unparseable identification response", response.content)
            return {
                "speaker_predictions": [],
                "error": f"Invalid JSON response ({type(e).__name__})",
            }

        if not isinstance(result, dict):
            logger.error("LLM response is not a valid JSON object")
            return {
                "speaker_predictions": [],
                "error": "Invalid response format - not a JSON object",
            }

        if "speaker_predictions" not in result:
            logger.error("LLM response missing required 'speaker_predictions' field")
            return {
                "speaker_predictions": [],
                "error": "Invalid response format - missing speaker_predictions",
            }

        predictions = result["speaker_predictions"]
        if not isinstance(predictions, list):
            logger.error("speaker_predictions is not a list")
            return {
                "speaker_predictions": [],
                "error": "Invalid response format - speaker_predictions must be a list",
            }

        valid_predictions = [p for p in predictions if self._validate_speaker_prediction(p)]

        logger.info(
            f"Speaker identification completed: {len(valid_predictions)} valid predictions from {len(predictions)} total"
        )

        return {
            "speaker_predictions": valid_predictions,
            "overall_confidence": result.get("overall_confidence", "unknown"),
            "analysis_notes": result.get("analysis_notes", "No additional notes provided"),
        }

    def identify_speakers(
        self,
        transcript: str,
        speaker_segments: list,
        known_speakers: list,
        output_language: str = "en",
        metadata_context: str = "",
    ) -> dict:
        """
        Use LLM to suggest speaker identifications based on contextual analysis of speech patterns,
        conversation content, and known speaker profiles.

        Args:
            transcript: Full transcript text with speaker labels
            speaker_segments: List of speaker segments with metadata including timestamps and text
            known_speakers: List of known speaker profiles with names and descriptions
            output_language: Language code for output reasoning (default: "en")
            metadata_context: File metadata (title, author, description, tags) for extra context

        Returns:
            Dictionary containing speaker predictions with confidence scores and reasoning
        """
        try:
            # Build language instruction for non-English output
            output_language_name = LLM_OUTPUT_LANGUAGES.get(output_language, "English")
            if output_language_name != "English":
                language_instruction = (
                    f"\n\nIMPORTANT: Generate all reasoning, analysis_notes, and explanations "
                    f"in {output_language_name}. Speaker names should remain as identified "
                    f"(names are language-agnostic), but all descriptive text must be in {output_language_name}."
                )
            else:
                language_instruction = ""

            system_prompt = f"""You are an expert linguist and conversation analyst specializing in speaker identification. Your task is to analyze transcripts and identify speakers based on multiple contextual clues.{language_instruction}

ANALYSIS METHODOLOGY:
1. Speech Patterns & Style:
   - Vocabulary complexity and professional terminology
   - Sentence structure and communication style
   - Use of technical jargon, industry-specific language
   - Formal vs. informal speech patterns

2. Content Analysis:
   - Topics of expertise and knowledge domains
   - Professional roles and responsibilities mentioned
   - Personal anecdotes or experiences shared
   - Areas where speakers demonstrate authority or deep knowledge

3. Conversational Dynamics:
   - Who asks questions vs. provides answers
   - Leadership patterns and decision-making roles
   - Deference patterns between speakers
   - Introduction patterns and name mentions

4. Context Clues:
   - Direct name mentions in conversation
   - Role references ("as the CEO", "from engineering", etc.)
   - Historical context from previous conversations
   - Cross-references to known speaker profiles
   - File metadata (title, description, author, tags) may contain speaker names or roles
   - METADATA SPEAKER HINTS (if provided) contain names extracted from metadata with roles and confidence scores
     * Hosts typically speak first, introduce guests, and ask questions
     * Guests are introduced, answer questions, and demonstrate focused expertise
     * Channel/uploader name often corresponds to the primary host
     * Higher-confidence hints should carry more weight in your analysis

CONFIDENCE SCORING:
- 0.9-1.0: Multiple strong indicators align (name mentioned + role + speech pattern match)
- 0.7-0.89: Strong contextual match with known profile (expertise area + communication style)
- 0.5-0.69: Moderate confidence based on partial indicators
- Below 0.5: Insufficient evidence for reliable identification

Only provide predictions with confidence >= 0.5. Explain your reasoning clearly for each identification."""

            known_speakers_context = self._build_known_speakers_context(known_speakers)

            speaker_labels = list(
                set(
                    seg.get("speaker_label", "Unknown")
                    for seg in speaker_segments
                    if seg.get("speaker_label")
                )
            )

            # Build metadata section for additional context
            metadata_section = ""
            if metadata_context:
                # Split structured hints from raw metadata
                if "SPEAKER HINTS FROM METADATA:" in metadata_context:
                    parts = metadata_context.split("\nFile Title:", 1)
                    hints_section = parts[0]
                    raw_section = "File Title:" + parts[1] if len(parts) > 1 else ""
                    metadata_section = f"\n{hints_section}\n"
                    if raw_section:
                        metadata_section += f"\nFILE METADATA:\n{raw_section}\n"
                else:
                    metadata_section = f"\nFILE METADATA:\n{metadata_context}\n"

            reserved_tokens = (
                len(system_prompt) // 3
                + len(known_speakers_context) // 3
                + len(metadata_section) // 3
                + 2500
            )
            available_tokens = max(1000, self.user_context_window - reserved_tokens)
            transcript_content = self._truncate_transcript_for_speakers(
                transcript, available_tokens
            )

            user_prompt = f"""TRANSCRIPT TO ANALYZE:
{transcript_content}
{metadata_section}
CURRENT SPEAKER LABELS: {", ".join(speaker_labels)}
{known_speakers_context}

TASK:
Analyze this conversation transcript and identify each speaker label based on the methodology described. Look for patterns in:
- Speech complexity and professional vocabulary usage
- Areas of expertise demonstrated through conversation content
- Leadership and authority patterns in the discussion
- Any direct or indirect name mentions or role references
- Communication styles and interpersonal dynamics
- File metadata clues (title, description, author, tags)
- Metadata speaker hints (names extracted from metadata with roles and confidence scores)

For each speaker you can identify with reasonable confidence (>=0.5), provide a detailed analysis.

RESPONSE FORMAT (JSON):
{{
    "speaker_predictions": [
        {{
            "speaker_label": "SPEAKER_1",
            "predicted_name": "John Smith",
            "confidence": 0.85,
            "reasoning": "Detailed explanation of evidence including speech patterns, expertise areas, and specific quotes or behaviors that led to this identification",
            "evidence_types": ["speech_pattern", "expertise", "role_reference", "name_mention"]
        }}
    ],
    "overall_confidence": "high",
    "analysis_notes": "Brief summary of the identification process and any challenges encountered"
}}

IMPORTANT: Only include predictions with confidence >= 0.5. If you cannot confidently identify any speakers, return an empty predictions array."""

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
                {
                    "role": "assistant",
                    "content": "Let me identify the most relevant evidence for each speaker:\n\nRELEVANT QUOTES AND EVIDENCE:",
                },
            ]

            response_tokens = min(self.config.response_tokens, self.user_context_window // 4)
            response = self.chat_completion(
                messages=messages,
                max_tokens=response_tokens,
                temperature=0.2,
            )

            if not response or not response.content:
                logger.warning("LLM returned empty response for speaker identification")
                return {"speaker_predictions": [], "error": "No response from LLM"}

            return self._parse_speaker_identification_response(response)

        except Exception as e:
            # Same dead-field rationale as _parse_speaker_identification_response
            # above -- sanitized rather than trusted to stay unread.
            logger.exception("Speaker identification failed")
            return {
                "speaker_predictions": [],
                "error": f"Identification process failed ({type(e).__name__})",
            }
