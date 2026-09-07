"""Unit tests for CrisperWhisper word-level output normalization.

Crafted on raw-CrisperWhisper-shaped input (comma-prefixed tokens, punctuation glued
to the front of the next word, filler ``[UM]`` tokens, and an inverted ``start > end``
timestamp). Asserts the normalizer:

* preserves real words (no fabrication, no spurious drops),
* strips the comma/space tokenization artifacts so text reads normally,
* keeps genuine filler tokens in clean form,
* produces monotonic, non-overlapping, in-bounds timestamps,
* is a no-op-safe pass-through on already-clean (turbo-shaped) segments.
"""

from __future__ import annotations

from app.transcription.crisperwhisper_normalize import normalize_crisperwhisper


def _word(text: str, start: float, end: float, prob: float = 0.9) -> dict:
    return {"word": text, "start": start, "end": end, "probability": prob}


def _crisper_segment() -> dict:
    """A segment shaped exactly like raw CrisperWhisper output from the dump."""
    words = [
        _word("Code's", 0.000, 0.280),  # first word: no leading separator
        _word(",not", 0.280, 0.380),  # comma-prefixed
        _word(",even", 0.380, 0.480),
        _word(",the", 0.480, 0.580),
        _word(",right", 0.580, 0.760),
        _word(",verb", 0.760, 1.200),
        _word(",anymore", 1.200, 1.720),
        _word(".Right", 1.720, 2.000),  # period glued to next word's front
        _word(".But", 2.000, 2.260),
        _word(",I", 2.260, 2.340),
        _word(",have", 2.340, 2.580),
        _word(",to", 2.580, 2.860),
        _word(",[UM]", 2.860, 3.580),  # filler token — must be KEPT
        _word(",express", 3.580, 4.160),
        _word(",can,", 4.160, 4.260),  # trailing-comma artifact
    ]
    # Comma-jammed text exactly as CrisperWhisper would build it.
    text = "Code's,not,even,the,right,verb,anymore.Right.But,I,have,to,[UM],express,can"
    return {"text": text, "start": 0.0, "end": 4.26, "words": words}


class TestSurfaceNormalization:
    def test_word_count_preserved(self):
        """Every real word survives — nothing fabricated, nothing extra dropped."""
        seg = _crisper_segment()
        before = len(seg["words"])
        out = normalize_crisperwhisper([seg])
        assert len(out) == 1
        assert len(out[0]["words"]) == before

    def test_words_have_leading_space_convention(self):
        """Each normalized word carries exactly one leading space (whisper standard)."""
        out = normalize_crisperwhisper([_crisper_segment()])
        assert out[0]["words"], "normalizer produced no words to check"
        for w in out[0]["words"]:
            assert w["word"].startswith(" ")
            # No comma prefix, no double space.
            assert not w["word"].startswith(" ,")
            assert not w["word"].startswith("  ")

    def test_comma_artifacts_stripped_from_text(self):
        """Rebuilt segment text reads as normal spaced words, no comma jamming."""
        out = normalize_crisperwhisper([_crisper_segment()])
        text = out[0]["text"]
        assert ",not" not in text
        assert ",even" not in text
        # The bare words are present and space-separated.
        assert "not even the right verb" in text
        # Whitespace split now yields the real word count, not 1.
        assert len(text.split()) == len(out[0]["words"])

    def test_glued_punctuation_moved_to_previous_word(self):
        """`.Right`/`.But` → period attaches to the PREVIOUS word, next word is clean."""
        out = normalize_crisperwhisper([_crisper_segment()])
        surfaces = [w["word"].strip() for w in out[0]["words"]]
        assert "anymore." in surfaces  # period moved off ".Right"
        assert "Right." in surfaces  # period moved off ".But"
        assert "But" in surfaces  # ".But" front-period removed
        # No surface still carries a glued leading period.
        for s in surfaces:
            assert not s.startswith(".")

    def test_filler_token_kept_and_cleaned(self):
        """[UM] is genuine transcription — kept, only normalized to lower case."""
        out = normalize_crisperwhisper([_crisper_segment()])
        surfaces = [w["word"].strip() for w in out[0]["words"]]
        assert "[um]" in surfaces
        # Never invent a backchannel that was not in the input.
        assert "yeah" not in " ".join(surfaces).lower()

    def test_trailing_comma_artifact_stripped(self):
        """A trailing-comma token (',can,') yields the bare word."""
        out = normalize_crisperwhisper([_crisper_segment()])
        surfaces = [w["word"].strip() for w in out[0]["words"]]
        assert "can" in surfaces
        assert "can," not in surfaces


class TestTimestampRepair:
    def test_inverted_timestamp_repaired(self):
        """A start > end word is corrected to a valid non-negative span."""
        seg = {
            "text": "hello world",
            "start": 0.0,
            "end": 2.0,
            "words": [
                _word("hello", 0.0, 0.5),
                _word(",world", 1.5, 1.2),  # inverted: start 1.5 > end 1.2
            ],
        }
        out = normalize_crisperwhisper([seg])
        w = out[0]["words"][1]
        assert w["end"] >= w["start"]

    def test_no_start_after_end_anywhere(self):
        out = normalize_crisperwhisper([_crisper_segment()])
        assert out[0]["words"], "normalizer produced no words to check"
        for w in out[0]["words"]:
            assert w["end"] >= w["start"]

    def test_monotonic_non_overlapping(self):
        out = normalize_crisperwhisper([_crisper_segment()])
        words = out[0]["words"]
        assert len(words) >= 2, "need at least 2 words to check monotonic ordering"
        for i in range(1, len(words)):
            assert words[i]["start"] >= words[i - 1]["end"] - 1e-9

    def test_timestamps_clamped_to_segment_bounds(self):
        seg = {
            "text": "a b",
            "start": 1.0,
            "end": 2.0,
            "words": [
                _word("a", 0.2, 1.5),  # start below seg_start
                _word(",b", 1.5, 9.0),  # end above seg_end
            ],
        }
        out = normalize_crisperwhisper([seg])
        assert out[0]["words"], "normalizer produced no words to check"
        for w in out[0]["words"]:
            assert w["start"] >= 1.0 - 1e-9
            assert w["end"] <= 2.0 + 1e-9


class TestCleanInputPassThrough:
    def test_turbo_shaped_segment_is_noop_safe(self):
        """Already-clean (leading-space) words round-trip without corruption."""
        seg = {
            "text": "Hello there friend.",
            "start": 0.0,
            "end": 1.5,
            "words": [
                _word(" Hello", 0.0, 0.4),
                _word(" there", 0.4, 0.9),
                _word(" friend.", 0.9, 1.4),
            ],
        }
        out = normalize_crisperwhisper([seg])
        surfaces = [w["word"].strip() for w in out[0]["words"]]
        assert surfaces == ["Hello", "there", "friend."]
        assert out[0]["text"] == "Hello there friend."
        assert len(out[0]["words"]) == 3

    def test_clean_input_word_count_preserved(self):
        seg = {
            "text": "one two three",
            "start": 0.0,
            "end": 3.0,
            "words": [
                _word(" one", 0.0, 1.0),
                _word(" two", 1.0, 2.0),
                _word(" three", 2.0, 3.0),
            ],
        }
        out = normalize_crisperwhisper([seg])
        assert len(out[0]["words"]) == 3
        assert out[0]["text"] == "one two three"

    def test_segment_without_words_passes_through(self):
        seg = {"text": "no words here", "start": 0.0, "end": 1.0, "words": []}
        out = normalize_crisperwhisper([seg])
        assert out[0]["text"] == "no words here"
        assert out[0]["words"] == []

    def test_input_not_mutated(self):
        seg = _crisper_segment()
        original_first = seg["words"][1]["word"]
        normalize_crisperwhisper([seg])
        assert seg["words"][1]["word"] == original_first


class TestRealCapturedRun:
    """Regression on the exact shape captured from a live CrisperWhisper GPU run."""

    def test_real_run_produces_clean_spaced_text(self):
        # First 8 words of the captured 95-word k30.wav run.
        words = [
            _word("Code's", 0.000, 0.280, 0.783),
            _word(",not", 0.280, 0.380, 0.333),
            _word(",even", 0.380, 0.480, 0.213),
            _word(",the", 0.480, 0.580, 0.524),
            _word(",right", 0.580, 0.760, 0.496),
            _word(",verb", 0.760, 1.200, 0.483),
            _word(",anymore", 1.200, 1.720, 0.717),
            _word(".Right", 1.720, 2.000, 0.310),
        ]
        seg = {
            "text": "Code's,not,even,the,right,verb,anymore.Right",
            "start": 0.0,
            "end": 2.0,
            "words": words,
        }
        out = normalize_crisperwhisper([seg])
        assert out[0]["text"] == "Code's not even the right verb anymore. Right"
        assert len(out[0]["words"]) == 8
