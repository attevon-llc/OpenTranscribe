"""Transcript chunking service for search indexing."""

import logging
import re
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

# NLTK punkt language map: ISO 639-1 → NLTK punkt language name.
# punkt_tab ships tokenizers for these 18 languages.
_PUNKT_LANG_MAP: dict[str, str] = {
    "cs": "czech",
    "da": "danish",
    "nl": "dutch",
    "en": "english",
    "et": "estonian",
    "fi": "finnish",
    "fr": "french",
    "de": "german",
    "el": "greek",
    "it": "italian",
    "no": "norwegian",
    "pl": "polish",
    "pt": "portuguese",
    "ru": "russian",
    "sl": "slovene",
    "es": "spanish",
    "sv": "swedish",
    "tr": "turkish",
}

# Cache of loaded NLTK tokenizers keyed by language name
_nltk_tokenizers: dict[str, Any] = {}
_nltk_unavailable_until: float = 0.0  # time.time() value; NLTK retried after this


def _get_nltk_tokenizer(language: str = "english"):
    """Load the NLTK punkt sentence tokenizer for the given language.

    Tokenizers are cached after first load. Falls back to English if the
    requested language model is not available, then to None if NLTK itself
    is unavailable. Retries after a 5-minute cooldown.

    Args:
        language: NLTK punkt language name (e.g. "english", "german").

    Returns:
        The tokenizer on success, or None if NLTK/punkt is unavailable.
    """
    import time

    global _nltk_unavailable_until

    if time.time() < _nltk_unavailable_until:
        return None

    if language in _nltk_tokenizers:
        return _nltk_tokenizers[language]

    try:
        import nltk.data

        tokenizer = _load_punkt_model(nltk.data, language)
        if tokenizer is None and language != "english":
            tokenizer = _load_punkt_model(nltk.data, "english")
        if tokenizer is not None:
            _nltk_tokenizers[language] = tokenizer
            logger.debug(f"Loaded NLTK punkt tokenizer for '{language}'")
            return tokenizer
    except Exception as e:
        logger.debug(f"NLTK punkt tokenizer not available, using regex fallback: {e}")

    _nltk_unavailable_until = time.time() + 300  # 5-minute cooldown
    return None


def _load_punkt_model(nltk_data_module: Any, language: str) -> Any:
    """Try loading punkt_tab first, fall back to punkt."""
    try:
        return nltk_data_module.load(f"tokenizers/punkt_tab/{language}.pickle")
    except LookupError:
        pass
    try:
        return nltk_data_module.load(f"tokenizers/punkt/{language}.pickle")
    except LookupError:
        return None


# Regex fallback for sentence splitting when NLTK is unavailable
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？।])\s+")


def _split_into_sentences(text: str, language: str = "en") -> list[str]:
    """Split text into sentences using NLTK punkt with regex fallback.

    Args:
        text: Input text to split.
        language: ISO 639-1 language code (e.g. "en", "de", "fr").

    Returns:
        List of sentence strings. Returns [text] if splitting fails.
    """
    if not text or not text.strip():
        return []

    nltk_lang = _PUNKT_LANG_MAP.get(language, "english")
    tokenizer = _get_nltk_tokenizer(nltk_lang)
    if tokenizer is not None:
        try:
            return list(tokenizer.tokenize(text))
        except Exception as e:
            logger.debug(f"NLTK tokenizer failed for language '{language}': {e}")

    # Regex fallback: split on sentence-ending punctuation followed by uppercase
    sentences = _SENTENCE_SPLIT_RE.split(text)
    return [s for s in sentences if s.strip()]


def _compute_overlap_sentences(sentences: list[str], target_words: int) -> list[str]:
    """Select trailing sentences for overlap that fit within target_words.

    Walks backward through the sentence list, accumulating sentences until
    the target word count is reached or exceeded.

    Args:
        sentences: List of sentences to select from (the end of a chunk).
        target_words: Target number of overlap words.

    Returns:
        List of trailing sentences whose combined word count is close to target_words.
    """
    if not sentences or target_words <= 0:
        return []

    overlap: list[str] = []
    word_count = 0

    max_words = target_words * 2  # Hard cap at 2x target to prevent runaway overlap
    for sentence in reversed(sentences):
        sentence_words = len(sentence.split())
        if word_count + sentence_words > target_words and overlap:
            break
        if word_count + sentence_words > max_words:
            break
        overlap.insert(0, sentence)
        word_count += sentence_words

    return overlap


def chunk_transcript_by_speaker_turns(
    segments: list[dict[str, Any]],
    file_uuid: str,
    file_id: int,
    user_id: int,
    title: str,
    speakers: list[str],
    tags: list[str],
    upload_time: str,
    language: str = "en",
    content_type: str = "",
    duration: float | None = None,
    file_size: int | None = None,
    collection_ids: list[int] | None = None,
    target_words: int | None = None,
    overlap_words: int | None = None,
    organization_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Chunk transcript segments into search-optimized documents.

    Strategy:
    1. Group consecutive segments by same speaker into speaker turns
    2. If a turn exceeds target_words, split with sliding window overlap
    3. If a turn is very short, merge with adjacent turns (same speaker)
    4. Each chunk retains: start_time, end_time, speaker, file metadata

    Args:
        segments: List of transcript segments with keys: start, end, text, speaker
        file_uuid: UUID of the media file
        file_id: Integer ID of the media file
        user_id: Integer ID of the file owner
        title: File title
        speakers: All speaker names in the file
        tags: Tags associated with the file
        upload_time: ISO timestamp of upload
        language: Language code
        target_words: Target words per chunk (default from settings)
        overlap_words: Overlap words between chunks (default from settings)

    Returns:
        List of chunk dicts ready for indexing.
    """
    if target_words is None:
        target_words = settings.SEARCH_CHUNK_TARGET_WORDS
    if overlap_words is None:
        overlap_words = settings.SEARCH_CHUNK_OVERLAP_WORDS

    # Step 1: Group consecutive segments by same speaker into turns
    turns = _group_segments_into_speaker_turns(segments)

    # Step 2: Split long turns with sliding window, merge short turns
    chunks: list[dict[str, Any]] = []
    chunk_index = 0

    for turn in turns:
        turn_text = turn["text"]
        turn_words = turn_text.split()
        word_count = len(turn_words)

        if word_count < 20 and chunks:
            # Very short turn - try to merge with last chunk if same speaker
            last_chunk = chunks[-1]
            if last_chunk["speaker"] == turn["speaker"]:
                last_chunk["content"] += " " + turn_text
                last_chunk["end_time"] = turn["end"]
                continue

        if word_count <= target_words:
            # Turn fits in one chunk
            chunks.append(
                _make_chunk(
                    content=turn_text,
                    speaker=turn["speaker"],
                    start_time=turn["start"],
                    end_time=turn["end"],
                    chunk_index=chunk_index,
                    file_uuid=file_uuid,
                    file_id=file_id,
                    user_id=user_id,
                    title=title,
                    speakers=speakers,
                    tags=tags,
                    upload_time=upload_time,
                    language=language,
                    content_type=content_type,
                    duration=duration,
                    file_size=file_size,
                    collection_ids=collection_ids,
                    organization_id=organization_id,
                )
            )
            chunk_index += 1
        else:
            # Long monologue - split with sliding window
            sub_chunks = _split_long_turn(
                turn,
                target_words,
                overlap_words,
                chunk_index,
                file_uuid,
                file_id,
                user_id,
                title,
                speakers,
                tags,
                upload_time,
                language,
                content_type=content_type,
                duration=duration,
                file_size=file_size,
                collection_ids=collection_ids,
                organization_id=organization_id,
            )
            chunks.extend(sub_chunks)
            chunk_index += len(sub_chunks)

    logger.info(
        f"Chunked transcript for file {file_uuid}: "
        f"{len(segments)} segments -> {len(turns)} turns -> {len(chunks)} chunks"
    )
    return chunks


def _group_segments_into_speaker_turns(
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group consecutive segments by the same speaker into turns."""
    if not segments:
        return []

    turns = []

    def _collect_words(seg: dict[str, Any]) -> list[dict[str, Any]]:
        return seg.get("words") or []

    current_turn = {
        "speaker": segments[0].get("speaker", "Unknown"),
        "text": segments[0].get("text", "").strip(),
        "start": segments[0].get("start", 0.0),
        "end": segments[0].get("end", 0.0),
        "word_timestamps": _collect_words(segments[0]),
    }

    for seg in segments[1:]:
        seg_speaker = seg.get("speaker", "Unknown")
        seg_text = seg.get("text", "").strip()

        if seg_speaker == current_turn["speaker"]:
            # Same speaker - extend current turn
            current_turn["text"] += " " + seg_text
            current_turn["end"] = seg.get("end", current_turn["end"])
            current_turn["word_timestamps"].extend(_collect_words(seg))
        else:
            # New speaker - save current turn and start new one
            if current_turn["text"].strip():
                turns.append(current_turn)
            current_turn = {
                "speaker": seg_speaker,
                "text": seg_text,
                "start": seg.get("start", 0.0),
                "end": seg.get("end", 0.0),
                "word_timestamps": _collect_words(seg),
            }

    # Don't forget the last turn
    if current_turn["text"].strip():
        turns.append(current_turn)

    return turns


def _compute_chunk_timestamp(
    turn: dict[str, Any],
    words_before: int,
    chunk_word_count: int,
) -> tuple[float, float]:
    """Compute chunk start/end using word timestamps if available, else interpolate.

    Args:
        turn: Speaker turn dict with 'start', 'end', 'text', and optional 'word_timestamps'.
        words_before: Number of words before this chunk in the turn.
        chunk_word_count: Number of words in this chunk.

    Returns:
        Tuple of (start_time, end_time) for the chunk.
    """
    word_ts = turn.get("word_timestamps")
    if word_ts and len(word_ts) >= words_before + chunk_word_count:
        first_word = word_ts[words_before]
        last_word_idx = min(words_before + chunk_word_count - 1, len(word_ts) - 1)
        last_word = word_ts[last_word_idx]
        # Use .get() with fallbacks for defensive handling of malformed entries
        chunk_start = first_word.get("start", turn["start"])
        chunk_end = last_word.get("end", turn["end"])
        if isinstance(chunk_start, (int, float)) and isinstance(chunk_end, (int, float)):
            return chunk_start, chunk_end

    # Fallback: linear interpolation
    total_words = max(len(turn["text"].split()), 1)
    turn_duration = turn["end"] - turn["start"]
    time_per_word = turn_duration / total_words
    chunk_start = turn["start"] + (words_before * time_per_word)
    chunk_end = turn["start"] + ((words_before + chunk_word_count) * time_per_word)
    return chunk_start, chunk_end


def _split_long_turn(
    turn: dict[str, Any],
    target_words: int,
    overlap_words: int,
    start_chunk_index: int,
    file_uuid: str,
    file_id: int,
    user_id: int,
    title: str,
    speakers: list[str],
    tags: list[str],
    upload_time: str,
    language: str,
    content_type: str = "",
    duration: float | None = None,
    file_size: int | None = None,
    collection_ids: list[int] | None = None,
    organization_id: int | None = None,
) -> list[dict[str, Any]]:
    """Split a long speaker turn into overlapping chunks at sentence boundaries.

    Uses NLTK punkt tokenizer (with regex fallback) to split at sentence
    boundaries, producing more coherent chunks for search and RAG.
    """
    text = turn["text"]
    chunks: list[dict[str, Any]] = []
    chunk_index = start_chunk_index

    # Guard against infinite loop when overlap >= target
    overlap_words = min(overlap_words, target_words - 1)

    # Split into sentences using language-aware tokenizer
    sentences = _split_into_sentences(text, language)

    if len(sentences) <= 1:
        # Single sentence or splitting failed -- fall back to word-count splitting
        return _split_long_turn_by_words(
            turn,
            target_words,
            overlap_words,
            start_chunk_index,
            file_uuid,
            file_id,
            user_id,
            title,
            speakers,
            tags,
            upload_time,
            language,
            content_type,
            duration,
            file_size,
            collection_ids,
            organization_id,
        )

    # Accumulate sentences into chunks respecting target_words
    current_sentences: list[str] = []
    current_word_count = 0
    words_before_current = 0  # running word offset for timestamp interpolation

    for sentence in sentences:
        sentence_words = len(sentence.split())

        # If adding this sentence would exceed target and we already have content,
        # finalize the current chunk
        if current_word_count + sentence_words > target_words and current_sentences:
            chunk_text = " ".join(current_sentences)
            chunk_word_count = current_word_count

            chunk_start, chunk_end = _compute_chunk_timestamp(
                turn, words_before_current, chunk_word_count
            )

            chunks.append(
                _make_chunk(
                    content=chunk_text,
                    speaker=turn["speaker"],
                    start_time=round(chunk_start, 2),
                    end_time=round(chunk_end, 2),
                    chunk_index=chunk_index,
                    file_uuid=file_uuid,
                    file_id=file_id,
                    user_id=user_id,
                    title=title,
                    speakers=speakers,
                    tags=tags,
                    upload_time=upload_time,
                    language=language,
                    content_type=content_type,
                    duration=duration,
                    file_size=file_size,
                    collection_ids=collection_ids,
                    organization_id=organization_id,
                )
            )
            chunk_index += 1

            # Compute overlap: select trailing sentences from current chunk
            overlap_sentences = _compute_overlap_sentences(current_sentences, overlap_words)
            overlap_word_count = sum(len(s.split()) for s in overlap_sentences)

            # Advance word offset past the non-overlapping portion
            words_before_current += chunk_word_count - overlap_word_count

            # Start new chunk with overlap sentences
            current_sentences = list(overlap_sentences)
            current_word_count = overlap_word_count

        current_sentences.append(sentence)
        current_word_count += sentence_words

    # Flush remaining sentences as the last chunk
    if current_sentences:
        chunk_text = " ".join(current_sentences)
        chunk_start, _ = _compute_chunk_timestamp(
            turn, words_before_current, len(chunk_text.split())
        )
        chunk_end = turn["end"]  # Last chunk extends to end of turn

        chunks.append(
            _make_chunk(
                content=chunk_text,
                speaker=turn["speaker"],
                start_time=round(chunk_start, 2),
                end_time=round(chunk_end, 2),
                chunk_index=chunk_index,
                file_uuid=file_uuid,
                file_id=file_id,
                user_id=user_id,
                title=title,
                speakers=speakers,
                tags=tags,
                upload_time=upload_time,
                language=language,
                content_type=content_type,
                duration=duration,
                file_size=file_size,
                collection_ids=collection_ids,
                organization_id=organization_id,
            )
        )

    return chunks


def _split_long_turn_by_words(
    turn: dict[str, Any],
    target_words: int,
    overlap_words: int,
    start_chunk_index: int,
    file_uuid: str,
    file_id: int,
    user_id: int,
    title: str,
    speakers: list[str],
    tags: list[str],
    upload_time: str,
    language: str,
    content_type: str = "",
    duration: float | None = None,
    file_size: int | None = None,
    collection_ids: list[int] | None = None,
    organization_id: int | None = None,
) -> list[dict[str, Any]]:
    """Split a long speaker turn into overlapping chunks by word count.

    Fallback when sentence splitting is not effective (single long sentence).
    """
    # Defensive guard: ensure overlap cannot equal or exceed target (would cause infinite loop)
    overlap_words = min(overlap_words, target_words - 1)
    words = turn["text"].split()
    total_words = len(words)
    chunks = []
    pos = 0
    chunk_index = start_chunk_index

    while pos < total_words:
        end_pos = min(pos + target_words, total_words)
        chunk_text = " ".join(words[pos:end_pos])

        chunk_start, chunk_end = _compute_chunk_timestamp(turn, pos, end_pos - pos)

        chunks.append(
            _make_chunk(
                content=chunk_text,
                speaker=turn["speaker"],
                start_time=round(chunk_start, 2),
                end_time=round(chunk_end, 2),
                chunk_index=chunk_index,
                file_uuid=file_uuid,
                file_id=file_id,
                user_id=user_id,
                title=title,
                speakers=speakers,
                tags=tags,
                upload_time=upload_time,
                language=language,
                content_type=content_type,
                duration=duration,
                file_size=file_size,
                collection_ids=collection_ids,
                organization_id=organization_id,
            )
        )
        chunk_index += 1

        # Advance with overlap
        pos = end_pos if end_pos >= total_words else end_pos - overlap_words

    return chunks


def _make_chunk(
    content: str,
    speaker: str,
    start_time: float,
    end_time: float,
    chunk_index: int,
    file_uuid: str,
    file_id: int,
    user_id: int,
    title: str,
    speakers: list[str],
    tags: list[str],
    upload_time: str,
    language: str,
    content_type: str = "",
    duration: float | None = None,
    file_size: int | None = None,
    collection_ids: list[int] | None = None,
    organization_id: int | None = None,
) -> dict[str, Any]:
    """Create a chunk document dict.

    ``organization_id`` is only written when set (org file). Community/personal
    docs are indexed WITHOUT the field, matching the personal-scope search filter
    (``must_not exists organization_id``) so behavior is unchanged there.
    """
    chunk: dict[str, Any] = {
        "file_id": file_id,
        "file_uuid": file_uuid,
        "user_id": user_id,
        "chunk_index": chunk_index,
        "content": content,
        "title": title,
        "speaker": speaker,
        "speakers": speakers,
        "tags": tags,
        "upload_time": upload_time,
        "language": language,
        "start_time": round(start_time, 2),
        "end_time": round(end_time, 2),
        "content_type": content_type,
        "duration": duration,
        "file_size": file_size,
        "collection_ids": collection_ids or [],
    }
    if organization_id is not None:
        chunk["organization_id"] = organization_id
    return chunk
