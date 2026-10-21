"""The caption segmenter: every cue a viewer sees obeys the broadcast rules.

``app.services.captions`` replaced a player that showed each diarized segment as ONE cue
(a minute of speech covering the video) and an export that wrapped a segment into
blocks timed only by character share. Each test here states one rule from
``captions/rules.py`` and checks the cues a real input produces against it; the helpers
read the rendered lines, never internal state.
"""

from __future__ import annotations

import itertools
import re

import pytest

from app.services.captions import CJK_RULES
from app.services.captions import LATIN_RULES
from app.services.captions import CaptionSegment
from app.services.captions import build_caption_track
from app.services.captions import to_srt
from app.services.captions import to_webvtt
from app.services.captions.linebreak import TokenRun
from app.services.captions.linebreak import ends_sentence
from app.services.captions.scripts import display_width
from app.services.captions.source import render_speaker_label
from app.services.captions.tokens import tokenize_segment

LONG_TURN = (
    "So what we found over the last quarter is that the retention numbers, which everyone "
    "was worried about, actually improved by 3.5 percent, and Dr. Jane Smith from the "
    "analytics team thinks that is mostly because of the onboarding changes we shipped in "
    "March. But we still need to look at churn in the enterprise segment, because those "
    "accounts behave very differently and the sample is small. I would like to propose that "
    "we run a proper cohort analysis before the board meeting next month and come back with "
    "a recommendation that everyone can sign off on without another round of debate. The "
    "other thing I want to flag is the hiring plan, since two of the open roles have been "
    "posted for almost ninety days now and we have not had a single qualified candidate, "
    "which tells me the compensation band is wrong rather than the job description."
)


def _words(text: str, start: float, end: float) -> list[dict]:
    """Even word timestamps across [start, end], 20% of each slot left as a gap."""
    tokens = text.split()
    slot = (end - start) / len(tokens)
    return [
        {"word": w, "start": start + i * slot, "end": start + i * slot + 0.8 * slot}
        for i, w in enumerate(tokens)
    ]


def _assert_broadcast_shape(track, rules=LATIN_RULES) -> None:
    assert track.cues, "the segmenter produced no cues"
    for cue in track.cues:
        assert 1 <= len(cue.lines) <= rules.max_lines, cue.lines
        for line in cue.lines:
            assert len(line) <= rules.max_line_chars, f"{len(line)} chars: {line!r}"
        assert cue.end > cue.start
        assert cue.end - cue.start <= rules.max_duration + 1e-6, (cue.start, cue.end)
    for prev, nxt in itertools.pairwise(track.cues):
        assert prev.end <= nxt.start + 1e-9, f"cues overlap: {prev.end} > {nxt.start}"


def _spoken_text(track) -> str:
    """Every cue's text with speaker labels removed, joined back into one string."""
    words: list[str] = []
    for cue in track.cues:
        for part in cue.parts:
            words.extend(" ".join(part.lines).split())
    return " ".join(words)


class TestLongSingleSpeakerSegment:
    """The owner's report: a 90 s segment must not be shown as one block."""

    @pytest.fixture
    def track(self):
        return build_caption_track(
            [
                CaptionSegment(
                    0.0,
                    90.0,
                    LONG_TURN,
                    speaker_key=1,
                    speaker_label="Alice Johnson",
                    words=_words(LONG_TURN, 0.0, 90.0),
                )
            ]
        )

    def test_every_cue_is_at_most_two_short_lines(self, track):
        _assert_broadcast_shape(track)
        assert len(track.cues) >= 12, f"a 90 s turn produced only {len(track.cues)} cues"

    def test_no_word_is_lost_or_repeated(self, track):
        assert _spoken_text(track) == " ".join(LONG_TURN.split())

    def test_the_name_is_shown_once_per_turn(self, track):
        labelled = [cue for cue in track.cues if cue.parts[0].show_label]
        assert labelled == [track.cues[0]]
        assert track.cues[0].lines[0].startswith("Alice Johnson: ")
        assert not any("Alice Johnson" in line for cue in track.cues[1:] for line in cue.lines)

    def test_no_line_ends_on_an_article_or_preposition(self, track):
        dangling = [
            line
            for cue in track.cues
            for line in cue.lines
            if line.split()[-1].lower() in {"a", "an", "the", "of", "to"}
        ]
        assert dangling == []

    def test_a_decimal_and_an_abbreviation_do_not_end_a_sentence(self):
        assert not ends_sentence("3.5")
        assert not ends_sentence("Dr.")
        assert not ends_sentence("U.S.")
        assert ends_sentence("March.")
        assert ends_sentence("debate?”")


class TestTiming:
    def test_word_timestamps_set_the_cue_times(self):
        """A cue starts when its first word is spoken, not at an interpolated guess."""
        text = "First sentence is here. Second sentence follows after a long pause."
        words = _words("First sentence is here.", 10.0, 12.0) + _words(
            "Second sentence follows after a long pause.", 20.0, 23.0
        )
        track = build_caption_track([CaptionSegment(10.0, 23.0, text, speaker_key=1, words=words)])

        assert track.interpolated_segments == 0
        assert [c.start for c in track.cues] == pytest.approx([10.0, 20.0])
        # The pause is respected: the first cue is not held across eight seconds of silence.
        assert track.cues[0].end <= 12.0 + LATIN_RULES.max_linger + 1e-6

    def test_without_word_timestamps_cues_are_interpolated_and_reported(self):
        track = build_caption_track([CaptionSegment(0.0, 60.0, LONG_TURN, speaker_key=1)])

        assert track.interpolated_segments == 1
        _assert_broadcast_shape(track)
        starts = [c.start for c in track.cues]
        assert starts == sorted(starts) and starts[0] == 0.0
        assert track.cues[-1].end <= 60.0 + 1e-6
        assert "NOTE" in to_webvtt(track) and "interpolated" in to_webvtt(track)

    def test_ends_are_held_for_reading_speed_when_there_is_room(self):
        """Fast speech followed by silence: the cue stays up long enough to read."""
        text = "We need to ship the migration before Friday."
        words = _words(text, 5.0, 6.0)
        track = build_caption_track([CaptionSegment(5.0, 6.0, text, speaker_key=1, words=words)])
        (cue,) = track.cues
        assert cue.end - cue.start > 1.0, "the cue was not held at all"
        assert cue.chars / (cue.end - cue.start) <= LATIN_RULES.max_cps + 1e-6
        assert cue.end <= words[-1]["end"] + LATIN_RULES.max_linger + 1e-6

    def test_a_cue_is_never_held_into_the_next_speakers_cue(self):
        a = "This is a fairly long sentence spoken very quickly by the first person."
        b = "And this is the reply from somebody else entirely, also fairly long."
        track = build_caption_track(
            [
                CaptionSegment(
                    0.0, 1.5, a, speaker_key=1, speaker_label="Ann", words=_words(a, 0, 1.5)
                ),
                CaptionSegment(
                    1.6, 3.2, b, speaker_key=2, speaker_label="Ben", words=_words(b, 1.6, 3.2)
                ),
            ]
        )
        _assert_broadcast_shape(track)
        ann = [c for c in track.cues if c.parts[0].speaker_label == "Ann"]
        ben = [c for c in track.cues if c.parts[0].speaker_label == "Ben"]
        assert ann and ben and ann[-1].end <= ben[0].start


class TestSpeakers:
    def test_many_one_word_segments_read_as_captions_not_flicker(self):
        utterance = "okay so let me just share my screen and then we can walk through it"
        words = utterance.split(" ")
        segments = [
            CaptionSegment(i * 0.4, i * 0.4 + 0.3, w, speaker_key=7, speaker_label="Sam")
            for i, w in enumerate(words)
        ]
        track = build_caption_track(segments)

        _assert_broadcast_shape(track)
        assert len(track.cues) <= 3, [c.lines for c in track.cues]
        assert _spoken_text(track) == " ".join(words)
        assert sum(c.parts[0].show_label for c in track.cues) == 1

    def test_the_name_returns_when_the_speaker_changes(self):
        segments = [
            CaptionSegment(
                0, 3, "Shall we start with the budget?", speaker_key=1, speaker_label="Ann"
            ),
            CaptionSegment(
                4, 7, "Sure, I have the numbers here.", speaker_key=2, speaker_label="Ben"
            ),
            CaptionSegment(
                8, 11, "Great, go ahead and share them.", speaker_key=1, speaker_label="Ann"
            ),
        ]
        track = build_caption_track(segments)
        first_lines = [c.lines[0] for c in track.cues]
        assert first_lines == [
            "Ann: Shall we start with the budget?",
            "Ben: Sure, I have the numbers here.",
            "Ann: Great, go ahead and share them.",
        ]

    def test_overlapping_speakers_never_produce_overlapping_cues(self):
        segments = [
            CaptionSegment(
                0.0, 1.0, "No way.", speaker_key=1, speaker_label="Ann", overlap_group="g"
            ),
            CaptionSegment(
                0.4, 1.2, "Yes way.", speaker_key=2, speaker_label="Ben", overlap_group="g"
            ),
            CaptionSegment(
                2.0, 9.0, LONG_TURN[:200], speaker_key=1, speaker_label="Ann", overlap_group="h"
            ),
            CaptionSegment(
                2.5, 9.5, LONG_TURN[200:400], speaker_key=2, speaker_label="Ben", overlap_group="h"
            ),
        ]
        track = build_caption_track(segments)

        _assert_broadcast_shape(track)
        dialogue = track.cues[0]
        assert dialogue.lines == ["Ann: No way.", "Ben: Yes way."]

    def test_a_rapid_one_word_reply_shares_a_two_line_dialogue_cue(self):
        segments = [
            CaptionSegment(
                0.0, 1.8, "Are we ready to go live?", speaker_key=1, speaker_label="Ann"
            ),
            CaptionSegment(1.9, 2.2, "Yes.", speaker_key=2, speaker_label="Ben"),
            CaptionSegment(6.0, 8.0, "Then let us begin.", speaker_key=1, speaker_label="Ann"),
        ]
        track = build_caption_track(segments)
        assert track.cues[0].lines == ["Ann: Are we ready to go live?", "Ben: Yes."]
        assert track.cues[1].lines == ["Ann: Then let us begin."]

    def test_no_label_when_speakers_are_not_wanted(self):
        track = build_caption_track([CaptionSegment(0, 3, "Hello there.", speaker_key=1)])
        assert track.cues[0].lines == ["Hello there."]


class TestScripts:
    def test_punctuation_free_asr_text_still_breaks_into_legal_cues(self):
        text = re.sub(r"[.,?!]", "", LONG_TURN).lower()
        track = build_caption_track(
            [CaptionSegment(0, 80, text, speaker_key=1, words=_words(text, 0, 80))]
        )
        _assert_broadcast_shape(track)
        assert _spoken_text(track) == " ".join(text.split())

    def test_without_punctuation_a_segment_boundary_is_the_preferred_break(self):
        first = "the quarterly numbers look fine"
        second = "this is a real mess and here is more of it"
        track = build_caption_track(
            [
                CaptionSegment(0.0, 1.5, first, speaker_key=1, speaker_label="Alice"),
                CaptionSegment(1.5, 3.0, second, speaker_key=1, speaker_label="Alice"),
            ]
        )
        assert [c.lines for c in track.cues] == [[f"Alice: {first}", second]]

    def test_cjk_uses_character_limits_and_never_starts_a_line_with_closing_punctuation(self):
        text = (
            "我们需要在下个月的董事会会议之前进行适当的队列分析，然后提出一个大家都能同意的建议。"
            "另外，招聘计划也需要重新讨论，因为两个职位已经开放了将近九十天。"
        )
        track = build_caption_track([CaptionSegment(0, 20, text, speaker_key=1)])

        _assert_broadcast_shape(track, CJK_RULES)
        assert all(cue.rules == CJK_RULES for cue in track.cues)
        assert not any(line[0] in "，。、" for cue in track.cues for line in cue.lines)
        assert "".join(line for cue in track.cues for line in cue.lines) == text

    def test_arabic_text_is_kept_in_logical_order_within_latin_limits(self):
        text = (
            "نحتاج إلى إجراء تحليل مناسب قبل اجتماع مجلس الإدارة في الشهر المقبل "
            "ثم نعود بتوصية يمكن للجميع الموافقة عليها دون جولة أخرى من النقاش"
        )
        track = build_caption_track([CaptionSegment(0, 12, text, speaker_key=1)])
        _assert_broadcast_shape(track)
        assert _spoken_text(track) == " ".join(text.split())

    def test_a_long_url_is_never_split_inside_the_token(self):
        url = "https://example.com/a/very/long/path/that/keeps/going/and/going/for/ever.html"
        text = f"The document lives at {url} if you want to read it later today."
        track = build_caption_track([CaptionSegment(0, 8, text, speaker_key=1)])
        lines = [line for cue in track.cues for line in cue.lines]
        assert url in lines, lines  # alone on its line, whole
        assert _spoken_text(track) == " ".join(text.split())

    def test_a_number_is_not_separated_from_its_unit(self):
        run = TokenRun(tokenize_segment("we raised 5 million last year", start=0, end=3).tokens)
        between_number_and_unit = run.break_penalty(3)
        assert between_number_and_unit > max(run.break_penalty(2), run.break_penalty(4))


class TestRedaction:
    def test_a_placeholder_is_one_token_timed_by_the_words_it_replaced(self):
        original = "Please email john.smith@example.com before noon tomorrow."
        masked = "Please email [EMAIL] before noon tomorrow."
        words = [
            {"word": "Please", "start": 1.0, "end": 1.3},
            {"word": "email", "start": 1.4, "end": 1.7},
            {"word": "john.smith@example.com", "start": 2.0, "end": 3.5},
            {"word": "before", "start": 3.6, "end": 3.9},
            {"word": "noon", "start": 4.0, "end": 4.2},
            {"word": "tomorrow.", "start": 4.3, "end": 4.8},
        ]
        seg = tokenize_segment(masked, start=1.0, end=4.8, words=words, source_text=original)
        placeholder = next(t for t in seg.tokens if t.text == "[EMAIL]")

        assert not seg.interpolated
        assert placeholder.start == pytest.approx(2.0)
        assert placeholder.end == pytest.approx(3.5)
        track = build_caption_track(
            [CaptionSegment(1.0, 4.8, masked, speaker_key=1, words=words, source_text=original)]
        )
        rendered = to_srt(track) + to_webvtt(track)
        assert "john.smith" not in rendered and "[EMAIL]" in rendered


class TestRendering:
    def test_webvtt_escapes_names_and_text_and_marks_the_voice(self):
        track = build_caption_track(
            [CaptionSegment(0, 3, "a < b & c", speaker_key=1, speaker_label="<b>Eve</b>")]
        )
        vtt = to_webvtt(track)
        assert vtt.startswith("WEBVTT\n")
        assert (
            "<v &lt;b&gt;Eve&lt;/b&gt;><c.speaker>&lt;b&gt;Eve&lt;/b&gt;</c>: a &lt; b &amp; c</v>"
            in vtt
        )
        assert "<b>Eve" not in vtt

    def test_webvtt_has_no_style_block_because_ffmpeg_reads_such_a_file_as_empty(self):
        track = build_caption_track(
            [
                CaptionSegment(0, 2, "First words here.", speaker_key=1, speaker_label="Ann"),
                CaptionSegment(3, 5, "Second words.", speaker_key=2, speaker_label="Ben"),
            ]
        )
        vtt = to_webvtt(track)
        blocks = vtt.strip().split("\n\n")
        assert blocks[0] == "WEBVTT"
        assert "STYLE" not in vtt
        assert all(b.startswith("NOTE") or "-->" in b for b in blocks[1:]), blocks
        assert sum("-->" in b for b in blocks) == len(track.cues) == 2

    def test_srt_numbers_cues_and_uses_comma_milliseconds(self):
        track = build_caption_track(
            [
                CaptionSegment(0.29, 2.0, "First words here.", speaker_key=1, speaker_label="Ann"),
                CaptionSegment(5.0, 7.0, "Second words.", speaker_key=2, speaker_label="Ben"),
            ]
        )
        srt = to_srt(track)
        assert srt.startswith("1\n00:00:00,290 --> ")
        assert "\n2\n00:00:05,000 --> " in srt
        assert "Ann: First words here." in srt and "Ben: Second words." in srt

    def test_a_placeholder_speaker_renders_through_the_localised_template(self):
        assert render_speaker_label("SPEAKER_03", "Speaker {number}") == "Speaker 03"
        assert render_speaker_label("SPEAKER_03", None) == "SPEAKER_03"
        assert render_speaker_label("Alice", "Speaker {number}") == "Alice"


# Real ASR-style input for the cross-language review (captions i18n pass). Each case below
# failed against the segmenter that shipped before it, with these exact texts.
JA_INTRO = (
    "番組へようこそ。本日のゲストは、CERNでAtlasプロジェクトを12年間率いてこられた佐藤博士です。"
    "新しい検出器がなぜ450万ユーロもかかるのか、説明していただきます。"
)
JA_SCHEDULE = (
    "なるほど。スケジュールについてもうかがいます。最初の結果は2027年3月に発表される予定です。"
    "チームはそれまでに準備できますか。それとも前回のように遅れが出ますか？"
)
JA_EVENTS = (
    "古い検出器は毎秒約40件のイベントしか記録できませんでしたが、新しいものは2,000件以上を処理でき、"
    "初期宇宙についてわかることが変わります。"
)
_NO_LINE_START = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶー々、。，．！？」』）】")
_THAI_LEADING_VOWELS = set("เแโใไ")
_THAI_DEPENDENT = {chr(c) for c in (*range(0x0E30, 0x0E3B), *range(0x0E45, 0x0E4F))}
THAI_RUN = (
    "ยินดีต้อนรับกลับสู่รายการแขกรับเชิญของเราวันนี้คือดร.สุดาผู้นำโครงการแอตลาสที่เซิร์นมา12ปี"
    "และจะอธิบายว่าทำไมเครื่องตรวจจับใหม่จึงมีราคา4.5ล้านยูโร"
)


def _pair_words(text: str, start: float, end: float) -> list[dict]:
    """Whisper-style CJK words: two characters each, time shared by length."""
    chunks = [text[i : i + 2] for i in range(0, len(text), 2)]
    slot = (end - start) / len(chunks)
    return [
        {"word": c, "start": start + i * slot, "end": start + (i + 0.9) * slot}
        for i, c in enumerate(chunks)
    ]


def _all_lines(track) -> list[str]:
    return [line for cue in track.cues for line in cue.lines]


class TestJapanese:
    @pytest.mark.parametrize("text", [JA_INTRO, JA_SCHEDULE], ids=["intro", "schedule"])
    @pytest.mark.parametrize("timed", [True, False], ids=["words", "interpolated"])
    def test_no_line_starts_with_a_small_kana_a_long_vowel_mark_or_a_closing_mark(
        self, text, timed
    ):
        words = _pair_words(text, 0, 20) if timed else None
        track = build_caption_track(
            [CaptionSegment(0, 20, text, speaker_key=1, speaker_label="田中 太郎", words=words)]
        )
        _assert_broadcast_shape(track, CJK_RULES)
        starts = [line[0] for line in _all_lines(track)]
        assert not [c for c in starts if c in _NO_LINE_START], _all_lines(track)

    @pytest.mark.parametrize("text", [JA_INTRO, JA_SCHEDULE], ids=["intro", "schedule"])
    def test_a_katakana_word_is_never_broken_across_lines(self, text):
        track = build_caption_track([CaptionSegment(0, 20, text, speaker_key=1)])
        lines = _all_lines(track)
        katakana_words = re.findall(r"[ァ-ヶー]{3,}", text)
        assert katakana_words
        for word in katakana_words:
            assert any(word in line for line in lines), (word, lines)

    @pytest.mark.parametrize("timed", [True, False], ids=["words", "interpolated"])
    def test_a_katakana_word_too_long_for_the_rest_of_the_line_moves_whole(self, timed):
        thanks = "お招きいただきありがとうございます。"
        segments = [
            CaptionSegment(
                18.58,
                22.4,
                thanks,
                speaker_key=1,
                speaker_label="佐藤 さくら",
                words=_pair_words(thanks, 18.58, 22.4) if timed else None,
            ),
            CaptionSegment(
                22.43,
                36.7,
                JA_EVENTS,
                speaker_key=1,
                speaker_label="佐藤 さくら",
                words=_pair_words(JA_EVENTS, 22.43, 36.7) if timed else None,
            ),
        ]
        track = build_caption_track(segments)
        _assert_broadcast_shape(track, CJK_RULES)
        assert any("イベント" in line for line in _all_lines(track)), _all_lines(track)

    @pytest.mark.parametrize("text", [JA_INTRO, JA_SCHEDULE], ids=["intro", "schedule"])
    def test_a_number_is_not_separated_from_its_counter(self, text):
        track = build_caption_track([CaptionSegment(0, 20, text, speaker_key=1)])
        lines = _all_lines(track)
        assert len(lines) > 1
        for above, below in itertools.pairwise(lines):
            assert not (above[-1].isdigit() and "぀" <= below[0] <= "鿿"), lines


class TestLongNames:
    def test_a_long_speaker_name_never_pushes_the_first_line_past_the_limit(self):
        label = "Jürgen Müller-Lüdenscheidt"
        text = "Willkommen zurück in der Sendung. Unser heutiger Gast ist Frau Dr. Anke Schäfer."
        track = build_caption_track(
            [CaptionSegment(0, 6, text, speaker_key=1, speaker_label=label)]
        )
        _assert_broadcast_shape(track)
        assert track.cues[0].lines[0].startswith(f"{label}: ")

    def test_a_name_too_long_to_share_a_line_gets_a_line_of_its_own(self):
        label = "Prof. Dr. Maximilian von Hohenfels"
        segments = [
            CaptionSegment(0, 5, "We should talk about the budget first.", 1, label),
            CaptionSegment(5.2, 5.8, "Yes.", 2, "Ann"),
            CaptionSegment(5.9, 6.5, "Agreed.", 1, label),
        ]
        track = build_caption_track(segments)
        _assert_broadcast_shape(track)
        assert track.cues[0].lines[0] == f"{label}:"
        assert track.cues[0].lines[1] == "We should talk about the budget first."
        vtt = to_webvtt(track)
        assert f"<c.speaker>{label}</c>:\nWe should talk about the budget first." in vtt


class TestOrdinalDates:
    def test_a_german_day_ordinal_is_not_a_sentence_end(self):
        text = (
            "Wir liegen im Plan. Wenn alles gut geht, veröffentlichen wir die erste "
            "Veröffentlichung am 15. März und laden alle zur Einführungsveranstaltung ein."
        )
        track = build_caption_track(
            [CaptionSegment(0, 9, text, speaker_key=1, words=_words(text, 0, 9))]
        )
        _assert_broadcast_shape(track)
        for above, below in itertools.pairwise(_all_lines(track)):
            assert not (above.endswith(" 15.") or above == "15."), (above, below)
        assert ends_sentence("done.") and not ends_sentence("15.")


class TestThai:
    @staticmethod
    def _width(line: str) -> int:
        return display_width(line)

    def test_an_unspaced_run_is_cut_to_the_line_limit_at_syllable_safe_points(self):
        track = build_caption_track([CaptionSegment(0, 12, THAI_RUN, speaker_key=1)])
        lines = _all_lines(track)
        assert all(self._width(line) <= LATIN_RULES.max_line_chars for line in lines), lines
        assert "".join(lines) == THAI_RUN
        assert not [ln for ln in lines if ln[0] in _THAI_DEPENDENT], lines
        assert not [ln for ln in lines if ln[-1] in _THAI_LEADING_VOWELS], lines

    def test_with_word_timestamps_breaks_fall_on_the_recognizers_word_boundaries(self):
        cuts = [0, 6, 10, 14, 19, 24, 27, 33, 38, 45, 50, 57, 64, 70, 77, 84, 90, 96, 103]
        cuts = [c for c in cuts if c < len(THAI_RUN)] + [len(THAI_RUN)]
        words = [
            {"word": THAI_RUN[a:b], "start": a * 0.1, "end": b * 0.1 - 0.02}
            for a, b in itertools.pairwise(cuts)
        ]
        track = build_caption_track(
            [CaptionSegment(0, len(THAI_RUN) * 0.1, THAI_RUN, speaker_key=1, words=words)]
        )
        lines = _all_lines(track)
        assert all(self._width(line) <= LATIN_RULES.max_line_chars for line in lines), lines
        boundaries = set(cuts)
        offset = 0
        for line in lines:
            assert offset in boundaries, (offset, lines)
            offset += len(line)
        assert offset == len(THAI_RUN)
