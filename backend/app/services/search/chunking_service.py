"""Transcript chunking service for search indexing."""

import logging
import re
from typing import Any

from app.core.config import settings
from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABEL

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

#: Latched once per process: has an NLTK load already been attempted and failed?
#:
#: This replaces a 5-minute ``_nltk_unavailable_until`` retry cooldown, which made
#: chunk boundaries **time-dependent within a single re-index** (issue #449). punkt
#: and the regex fallback disagree on abbreviations — punkt keeps ``Dr.`` and
#: ``p.m.`` inside a sentence, the regex splits on them — and sentence boundaries
#: drive chunk boundaries. So after one failed load, files early in a pass were
#: chunked by the regex and files more than five minutes later by punkt: the same
#: corpus, chunked two different ways, in one run, with nothing recording which.
#:
#: That is issue #433's failure mode ("re-indexing one unchanged corpus produced
#: three different chunk counts") arriving by a second route after the ordering
#: bug behind it was fixed.
#:
#: Latching trades a retry for determinism, which is the right trade here: NLTK is
#: either installed in the image with its corpora or it is not, and that does not
#: change while the process runs. A worker that starts without punkt now chunks
#: the whole pass consistently rather than switching part-way through.
_nltk_load_failed: bool = False


def _get_nltk_tokenizer(language: str = "english"):
    """Load the NLTK punkt sentence tokenizer for the given language.

    Tokenizers are cached after first load. Falls back to English if the
    requested language model is not available, then to None if NLTK itself is
    unavailable — and that None is **latched for the life of the process**, so a
    re-index cannot switch splitters part-way through (issue #449).

    Args:
        language: NLTK punkt language name (e.g. "english", "german").

    Returns:
        The tokenizer on success, or None if NLTK/punkt is unavailable.
    """
    global _nltk_load_failed

    if _nltk_load_failed:
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

    # WARNING, not debug: this decides how every chunk in this process is split,
    # and a worker that silently differs from its peers produces an index whose
    # boundaries depend on which worker happened to handle each file. It is the
    # one line that makes a mixed index detectable after the fact.
    _nltk_load_failed = True
    logger.warning(
        "NLTK punkt unavailable; this process will use the REGEX sentence "
        "splitter for every transcript it chunks. The two disagree on "
        "abbreviations, so chunks produced here will not match those from a "
        "worker that has punkt. Install the punkt corpora to make the index "
        "uniform."
    )
    return None


def reset_sentence_splitter_state() -> None:
    """Clear the latched splitter decision and the tokenizer cache.

    For tests only. The latch is deliberately permanent in production — see
    ``_nltk_load_failed`` — so a suite that exercises both splitters needs an
    explicit way to undo it rather than waiting out a cooldown that no longer
    exists.
    """
    global _nltk_load_failed
    _nltk_load_failed = False
    _nltk_tokenizers.clear()


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


# Scripts written WITHOUT spaces between words ("scriptio continua"): CJK
# ideographs, kana, and the Thai/Lao/Khmer/Myanmar families. Deliberately EXCLUDES
# Hangul — Korean is written with spaces, so counting it per-character would make
# Korean chunks several times smaller than every other language's.
_NO_SPACE_SCRIPT = (
    "぀-ヿ"  # hiragana + katakana
    "㐀-䶿"  # CJK ext A
    "一-鿿"  # CJK unified
    "豈-﫿"  # CJK compatibility
    "ｦ-ﾟ"  # halfwidth katakana
    "฀-๿"  # Thai
    "຀-໿"  # Lao
    "က-႟"  # Myanmar
    "ក-៿"  # Khmer
)
_NO_SPACE_CHAR_RE = re.compile(f"[{_NO_SPACE_SCRIPT}]")

#: Sentence terminators no punkt model recognises. Devanagari's danda is the one
#: that matters in practice: Hindi uses spaces, so it clears the scriptio-continua
#: check and would otherwise be handed to English punkt.
_FOREIGN_TERMINATOR_RE = re.compile("[。！？।॥…‥]")

#: One "word" for sizing purposes: either a single scriptio-continua character, or
#: a run of anything else that is not whitespace. Chunk budgets are expressed in
#: words, and ``str.split()`` reports a 10,000-character Chinese transcript as
#: **one word** — so every size check passed and the whole transcript became a
#: single chunk (issue #448). Counting each CJK character as a word is slightly
#: conservative (a character is nearer a syllable than a word), which errs toward
#: chunks that fit the embedding model's window rather than ones that overflow it.
_WORD_SPAN_RE = re.compile(f"[{_NO_SPACE_SCRIPT}]|[^\\s{_NO_SPACE_SCRIPT}]+")

# Regex fallback for sentence splitting when NLTK is unavailable, or when the
# script is one punkt has no model for.
#
# TWO alternatives, because the whitespace requirement differs. A Latin full stop
# needs the following space to avoid splitting "3.14" and "Dr. Chen"; the CJK and
# Devanagari terminators are never used inside a token, and the text that uses
# them has no spaces at all — so requiring ``\s+`` after them (as this pattern
# did) meant it never matched a single Chinese or Japanese sentence boundary.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[。！？…‥।॥])\s*")


def count_words(text: str) -> int:
    """Count words for chunk sizing, correctly for scripts written without spaces.

    Args:
        text: The text to measure.

    Returns:
        Number of word-equivalents. For Latin/Cyrillic/Hangul text this equals
        ``len(text.split())``; for CJK/Thai it counts characters, which
        ``str.split()`` cannot do because there is nothing to split on.
    """
    return len(_WORD_SPAN_RE.findall(text))


def _word_spans(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` offsets of each word-equivalent in *text*.

    Slicing the ORIGINAL string by these offsets preserves its spacing. The
    alternative — ``" ".join(text.split()[i:j])`` — inserts a space between every
    character of a Chinese chunk, corrupting both what the user reads and what
    the embedding model receives.
    """
    return [m.span() for m in _WORD_SPAN_RE.finditer(text)]


def _sentence_joiner(text: str) -> str:
    """The separator to rejoin sentences of *text* with.

    Empty for scriptio continua. The sentences were produced by splitting a
    string that had no spaces in it, so rejoining them with `" "` inserts
    separators the original never had — visible to the reader as
    ``产品。 达娜`` and fed to the embedding model as different text than was
    indexed elsewhere.
    """
    return "" if _NO_SPACE_CHAR_RE.search(text) else " "


def _punkt_can_read(text: str, language: str | None) -> bool:
    """Whether an NLTK punkt model is a sensible splitter for this text.

    ``_PUNKT_LANG_MAP.get(language, "english")`` used to send every unmapped
    language to the ENGLISH tokenizer, which loads fine, runs fine, and returns
    a Chinese transcript as **one sentence** — English punkt looks for ``.``,
    ``!``, ``?`` and Chinese uses ``。``, ``！``, ``？``. The regex fallback below
    already handled those terminators but was never reached, because punkt had
    not failed; it had merely been wrong.

    ``language=None`` (unknown — no caller should ever coerce this to ``"en"``
    just because English is the default; see this module's
    #448 fix) falls straight through to the text-based disqualifiers below, the
    same as any other unmapped code.
    """
    if language in _PUNKT_LANG_MAP:
        return True
    # Two independent disqualifiers, and the second is easy to miss: Devanagari
    # is written WITH spaces, so the scriptio-continua check alone passed it to
    # English punkt, which does not know the danda `।` ends a sentence and
    # returned a whole Hindi transcript as one sentence.
    return not (_NO_SPACE_CHAR_RE.search(text) or _FOREIGN_TERMINATOR_RE.search(text))


def split_into_sentences(text: str, language: str | None = "en") -> list[str]:
    """Split text into sentences using NLTK punkt with regex fallback.

    **Public, and it has to be.** ``services/ingest_artifacts/digest.py`` and
    the transcript chunker split text the same way,
    so the digest's sentence boundaries and the index's chunk boundaries stay the
    SAME boundaries — a second implementation would drift and the digest would cite
    spans the chunks do not contain. ``test_compose_sentence_splitter_mounts`` also
    derives which workers need the punkt mount by walking the import graph outward
    from this name, so privatising it silently empties that check.

    Args:
        text: Input text to split.
        language: ISO 639-1 language code (e.g. "en", "de", "fr"), or ``None``
            when the caller genuinely does not know — pass ``None``, not a
            coerced ``"en"``: :func:`_punkt_can_read` only applies its
            script/terminator disqualifiers to a language it does not
            recognise, so defaulting an unknown language to English silently
            defeats that guard (issue #448) instead of taking its no-language
            path.

    Returns:
        List of sentence strings. Returns [text] if splitting fails.
    """
    if not text or not text.strip():
        return []

    if _punkt_can_read(text, language):
        # `language or ""`: None (unknown) never matches a real map key, so this
        # still falls through to "english" — same outcome as before, just typed
        # correctly now that `_punkt_can_read` legitimately returns True for a
        # None language (the no-language path taken when the script/terminator
        # checks find nothing disqualifying).
        nltk_lang = _PUNKT_LANG_MAP.get(language or "", "english")
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
        sentence_words = count_words(sentence)
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
        word_count = count_words(turn_text)

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
                    speaker_id=turn.get("speaker_id"),
                    profile_id=turn.get("profile_id"),
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
        # COPY. `seg.get("words")` returns the CALLER's list, and the turn it seeds is
        # later `.extend()`ed — so without the copy this grows the caller's own segment
        # dicts, and chunking the same list twice gives different results. Measured before
        # the fix: segments[0]["words"] went 1 -> 2 -> 3 across successive calls, and 330 of
        # 648 swept target/overlap/segment configurations produced first != second.
        #
        # That is issue #433's shape ("re-indexing one unchanged corpus produced three
        # different chunk counts") reappearing one layer down: the accumulated list flips
        # _compute_chunk_timestamp's `len(word_ts) >= words_before + chunk_word_count`
        # guard, so a chunk that interpolated its timestamp on the first pass reads real
        # word timings on the second.
        return list(seg.get("words") or [])

    current_turn = {
        "speaker": segments[0].get("speaker", UNKNOWN_SPEAKER_LABEL),
        "speaker_id": segments[0].get("speaker_id"),
        "profile_id": segments[0].get("profile_id"),
        "text": segments[0].get("text", "").strip(),
        "start": segments[0].get("start", 0.0),
        "end": segments[0].get("end", 0.0),
        "word_timestamps": _collect_words(segments[0]),
    }

    for seg in segments[1:]:
        seg_speaker = seg.get("speaker", UNKNOWN_SPEAKER_LABEL)
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
                # Taken from the FIRST segment of the new turn. Turns are grouped
                # by speaker NAME, so this is a display-name-keyed lookup, not an
                # id-keyed one — two distinct Speaker rows sharing one display name
                # would merge into one turn and this simply carries whichever id
                # opened it, the same approximation the name grouping already makes.
                "speaker_id": seg.get("speaker_id"),
                "profile_id": seg.get("profile_id"),
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
    total_words = max(count_words(turn["text"]), 1)
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
    sentences = split_into_sentences(text, language)

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
        sentence_words = count_words(sentence)

        # If adding this sentence would exceed target and we already have content,
        # finalize the current chunk
        if current_word_count + sentence_words > target_words and current_sentences:
            chunk_text = _sentence_joiner(text).join(current_sentences)
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
                    speaker_id=turn.get("speaker_id"),
                    profile_id=turn.get("profile_id"),
                )
            )
            chunk_index += 1

            # Compute overlap: select trailing sentences from current chunk
            overlap_sentences = _compute_overlap_sentences(current_sentences, overlap_words)
            overlap_word_count = sum(count_words(s) for s in overlap_sentences)

            # Advance word offset past the non-overlapping portion
            words_before_current += chunk_word_count - overlap_word_count

            # Start new chunk with overlap sentences
            current_sentences = list(overlap_sentences)
            current_word_count = overlap_word_count

        current_sentences.append(sentence)
        current_word_count += sentence_words

    # Flush remaining sentences as the last chunk
    if current_sentences:
        chunk_text = _sentence_joiner(text).join(current_sentences)
        chunk_start, _ = _compute_chunk_timestamp(
            turn, words_before_current, count_words(chunk_text)
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
                speaker_id=turn.get("speaker_id"),
                profile_id=turn.get("profile_id"),
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
    text = turn["text"]
    # Offsets into the ORIGINAL string, not a token list. Slicing the source keeps
    # its spacing intact; `" ".join(text.split()[i:j])` would put a space between
    # every character of a Chinese chunk, corrupting what the reader sees and what
    # gets embedded. It also collapsed runs of whitespace in every other language.
    spans = _word_spans(text)
    total_words = len(spans)
    chunks = []
    pos = 0
    chunk_index = start_chunk_index

    while pos < total_words:
        end_pos = min(pos + target_words, total_words)
        chunk_text = text[spans[pos][0] : spans[end_pos - 1][1]]

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
                speaker_id=turn.get("speaker_id"),
                profile_id=turn.get("profile_id"),
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
    speaker_id: int | None = None,
    profile_id: int | None = None,
) -> dict[str, Any]:
    """Create a chunk document dict.

    ``organization_id`` is only written when set (org file). Community/personal
    docs are indexed WITHOUT the field, matching the personal-scope search filter
    (``must_not exists organization_id``) so behavior is unchanged there.

    ``speaker_id`` / ``profile_id`` (issue W2.7) follow the same only-when-known
    convention, for the same reason: a document indexed before these fields
    existed, or whose turn has no resolved ``Speaker`` row, carries neither key
    at all rather than an explicit ``null`` — every reader must use an
    ``exists`` compat arm (:func:`~app.services.ingest_artifacts.index_mapping`
    has none of its own for these two; see ``services/search/CLAUDE.md``'s
    existing compat-arm pattern) rather than assume the field is populated.
    **Never folded into ``embedding_text``** — that field is what the ingest
    pipeline embeds, and an id has no semantic content a vector search could
    use; adding it would silently reshape the vector of every document that
    carries one.
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
    if speaker_id is not None:
        chunk["speaker_id"] = speaker_id
    if profile_id is not None:
        chunk["profile_id"] = profile_id
    return chunk
