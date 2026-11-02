"""Token estimation and transcript chunking for :class:`LLMService`."""

import logging
import re

from app.services.llm_types import LLMHost
from app.services.search.sentence_splitter import split_into_sentences

logger = logging.getLogger(__name__)


class LLMChunkingMixin(LLMHost):
    """Token heuristic and transcript chunking (mixed into ``LLMService``)."""

    def estimate_tokens(self, text: str) -> int:
        """Public wrapper over the internal token heuristic (see :meth:`_estimate_tokens`)."""
        return self._estimate_tokens(text)

    def _estimate_tokens(self, text: str) -> int:
        """
        Estimate token count using more accurate heuristics.

        This method provides a reasonable approximation for OpenAI-style tokenization
        without requiring the tiktoken library. The estimation is conservative to
        prevent context window overflow.

        Args:
            text: Input text to tokenize

        Returns:
            Estimated token count

        Note:
            - Uses word-based and character-based heuristics
            - Accounts for common punctuation and formatting
            - Returns slightly higher estimates to be safe
        """
        if not text:
            return 0

        # Basic word count
        words = text.split()
        word_count = len(words)

        # Character-based estimation for better accuracy
        char_count = len(text)

        # Combine both methods:
        # - English averages ~4.7 characters per token
        # - But also consider word boundaries and punctuation
        from app.core.constants import CHARS_PER_TOKEN_ESTIMATE
        from app.core.constants import SUBWORD_TOKENIZATION_FACTOR
        from app.core.constants import TOKEN_ESTIMATION_BUFFER

        char_based_estimate = char_count / CHARS_PER_TOKEN_ESTIMATE
        word_based_estimate = word_count * SUBWORD_TOKENIZATION_FACTOR

        # Use the higher estimate to be conservative
        estimated_tokens = max(char_based_estimate, word_based_estimate)

        # Add buffer for safety
        return int(estimated_tokens * TOKEN_ESTIMATION_BUFFER)

    def _split_oversized_chunk_by_sentences(self, chunk: str, available_tokens: int) -> list[str]:
        """Split an oversized chunk into smaller chunks by sentence boundaries."""
        sentences = split_into_sentences(chunk, language=None)  # transcript language unknown here
        sub_chunks = []
        sub_chunk = ""

        for sentence in sentences:
            test_chunk = sub_chunk + sentence + " "
            if self._estimate_tokens(test_chunk) <= available_tokens:
                sub_chunk = test_chunk
            else:
                if sub_chunk.strip():
                    sub_chunks.append(sub_chunk.strip())
                sub_chunk = sentence + " "

        if sub_chunk.strip():
            sub_chunks.append(sub_chunk.strip())

        return sub_chunks

    def _split_by_speaker_segments(self, transcript: str, available_tokens: int) -> list[str]:
        """Split transcript by speaker changes into appropriately sized chunks."""
        speaker_segments = re.split(r"(\n[A-Z_][A-Z0-9_]*:\s*\[\d+:\d+\])", transcript)
        chunks = []
        current_chunk = ""
        current_size = 0

        for segment in speaker_segments:
            segment_size = self._estimate_tokens(segment)

            if current_size + segment_size > available_tokens and current_chunk:
                chunks.append(current_chunk.strip())
                logger.debug(f"Created chunk {len(chunks)}: {len(current_chunk)} chars")
                current_chunk = segment
                current_size = segment_size
            else:
                current_chunk += segment
                current_size += segment_size

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks

    def _chunk_transcript_intelligently(
        self, transcript: str, chunk_overlap: int = 200
    ) -> list[str]:
        """
        Split transcript into intelligent chunks using ONLY user's max_tokens setting
        """
        # Reserve the ACTUAL response budget, not a flat guess -- this used to subtract a
        # flat 2000 regardless of self.response_tokens (up to 16384), so a transcript sized
        # to "fit" here could still overflow the model's real context once the requested
        # response tokens were added back in. Measured live: a 3-hour transcript (204,655
        # chars) was accepted as a single chunk, then vLLM 400'd at 45001 input + 15000
        # output against a 60000-token model (issue #645-adjacent finding). The extra 2000
        # keeps the original margin for prompt-template/speaker-data overhead on top of the
        # response reservation.
        available_tokens = self.user_context_window - self.response_tokens - 2000
        estimated_tokens = self._estimate_tokens(transcript)

        logger.info(
            f"Chunking transcript: {len(transcript)} chars, estimated {estimated_tokens} tokens"
        )
        logger.info(
            f"Using user context window: {self.user_context_window}, available for content: {available_tokens}"
        )

        if estimated_tokens <= available_tokens:
            logger.info("Transcript fits in single chunk")
            return [transcript]

        target_chars_per_chunk = int(available_tokens * 2.5)
        logger.info(f"Target chars per chunk: {target_chars_per_chunk}")

        chunks = self._split_by_speaker_segments(transcript, available_tokens)

        # Handle oversized chunks by splitting on sentences
        final_chunks = []
        for chunk in chunks:
            if self._estimate_tokens(chunk) <= available_tokens:
                final_chunks.append(chunk)
            else:
                logger.warning("Chunk too large, splitting by sentences")
                final_chunks.extend(
                    self._split_oversized_chunk_by_sentences(chunk, available_tokens)
                )

        if not final_chunks and transcript:
            logger.warning("No chunks created, truncating original transcript")
            final_chunks = [transcript[:target_chars_per_chunk]]

        logger.info(
            f"Split transcript into {len(final_chunks)} chunks using user context window: {self.user_context_window}"
        )
        return final_chunks
