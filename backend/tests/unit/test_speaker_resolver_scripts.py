"""Script-aware speaker-mention resolution — the non-Latin ladder (issue #453 family).

`speaker_resolver.py`'s original ladder was English-first: candidate extraction
only matched ``[A-Za-z]`` capitalized words, so a Chinese, Japanese, Korean,
Thai, Arabic or Hindi name in the question text was never even ATTEMPTED
against the roster — independent of anything the matching ladder itself could
do, because no candidate reached it.

This suite pins the fix for six scripts, each with a REAL match and a REAL
decline — a resolver that matches everything is exactly as broken as one that
matches nothing, and only testing the positive case would miss that. Grouped
by what each script structurally needs:

- **zh / ja / th** (scriptio continua — no word boundaries): the
  grapheme-level, unanchored prefix rung (``_prefix_rung(..., anchored=False)``).
- **ko** (Hangul — spaced, no case, particles attach to names): the
  suffix-tolerant, anchored prefix rung, bounded by
  :data:`speaker_resolver._KOREAN_PARTICLE_MAX_CHARS`.
- **ar / hi** (Arabic, Hindi/Devanagari — spaced, no case, no particles):
  ordinary tokenization with no common-word/sentence-initial filter, since
  that filter exists to use a capitalization signal these scripts lack.

The explicit 2-character-name fuzzy-threshold case (the brief's own example)
gets its own group, since it is what makes :func:`_fuzzy_threshold` script-
dependent rather than a copy of the flat Latin constant.
"""

from __future__ import annotations

import pytest

from app.services.chat.speaker_resolver import Roster
from app.services.chat.speaker_resolver import RosterEntry
from app.services.chat.speaker_resolver import extract_script_aware_candidates
from app.services.chat.speaker_resolver import match_candidate
from app.services.chat.speaker_resolver import resolve_speaker_mentions

pytestmark = pytest.mark.unit


def _roster(*names: str) -> Roster:
    return Roster(entries=tuple(RosterEntry(name=n, profile_id=None, file_count=1) for n in names))


def _best_match(text: str, roster: Roster) -> str | None:
    """Resolve the first uniquely-matched candidate in *text*, or None.

    Mirrors what `resolve_speaker_mentions` does internally, but without a DB
    session — these tests exercise extraction + the matching ladder together,
    the same shape `test_chat_speaker_resolver.py` uses for the Latin ladder.
    """
    for candidate in extract_script_aware_candidates(text):
        outcome = match_candidate(candidate, roster)
        if outcome.matched is not None:
            return outcome.matched
    return None


# ---------------------------------------------------------------------------
# zh — Chinese: scriptio continua, grapheme-level UNANCHORED prefix match.
# ---------------------------------------------------------------------------


def test_zh_name_mid_run_is_matched_via_grapheme_prefix():
    """No punctuation separates the name from the surrounding text at all —
    real Chinese questions are not guaranteed to comma-pause before a name."""
    roster = _roster("达娜", "小明")
    assert _best_match("那次会议上达娜说了什么？", roster) == "达娜"


def test_zh_completely_unrelated_name_declines():
    """A real decline, not merely an absent match: a 2-character candidate
    that shares NO characters with the roster entry must not match."""
    roster = _roster("达娜")
    outcome = match_candidate("小明", roster)
    assert outcome.matched is None
    assert outcome.reason == "no_roster_match"


def test_zh_two_roster_names_in_one_run_declines_as_ambiguous():
    """Both names are genuinely present, but with no segmenter this module
    cannot tell them apart from one run — per the module's design constraint,
    that is ambiguity (decline), never a best-effort pick of one."""
    roster = _roster("达娜", "小明")
    candidates = extract_script_aware_candidates("会议上小明和达娜都发言了")
    outcomes = [match_candidate(c, roster) for c in candidates]
    assert all(o.matched is None for o in outcomes)
    assert any(o.ambiguous_with for o in outcomes)


# ---------------------------------------------------------------------------
# ja — Japanese: same scriptio-continua path, katakana name.
# ---------------------------------------------------------------------------


def test_ja_katakana_name_mid_run_is_matched():
    roster = _roster("ダナ")
    assert _best_match("プロジェクトについてダナは何と言いましたか", roster) == "ダナ"


def test_ja_unrelated_katakana_name_declines():
    roster = _roster("ダナ")
    outcome = match_candidate("タロウ", roster)
    assert outcome.matched is None
    assert outcome.reason == "no_roster_match"


# ---------------------------------------------------------------------------
# th — Thai: same scriptio-continua path (`_NO_SPACE_CHAR_RE` covers it too).
# ---------------------------------------------------------------------------


def test_th_name_mid_run_is_matched():
    roster = _roster("ดานา")
    assert _best_match("เมื่อวานดานาพูดว่าอะไร", roster) == "ดานา"


def test_th_unrelated_name_declines():
    roster = _roster("ดานา")
    outcome = match_candidate("สมชาย", roster)
    assert outcome.matched is None
    assert outcome.reason == "no_roster_match"


# ---------------------------------------------------------------------------
# ko — Korean: spaced, no case, suffix-tolerant ANCHORED prefix (particles).
# ---------------------------------------------------------------------------


def test_ko_name_plus_particle_is_matched():
    """'다나가' = 다나 (Dana) + 가 (subject particle). Korean has spaces, so
    this arrives as one whitespace-isolated token, unlike the CJK case."""
    roster = _roster("다나")
    assert _best_match("다나가 뭐라고 했어요?", roster) == "다나"


def test_ko_name_plus_a_different_short_particle_is_matched():
    roster = _roster("민수")
    assert _best_match("민수는 회의에 참석했어요", roster) == "민수"


def test_ko_token_extending_far_past_the_particle_bound_declines():
    """A name followed by a much longer unrelated suffix (a full extra word,
    not a particle) must NOT match — the particle tolerance is bounded, and
    this must not leak through the fuzzy rung's short-name floor either
    (see `_FUZZY_MAX_LENGTH_DELTA`'s docstring for the measured leak this
    guards)."""
    roster = _roster("다나")
    outcome = match_candidate("다나였습니다", roster)  # "was Dana" — copula, not a particle
    assert outcome.matched is None
    assert outcome.reason == "no_roster_match"


def test_ko_typo_with_no_particle_still_fuzzy_matches():
    roster = _roster("다나")
    outcome = match_candidate("타나", roster)  # one-character substitution, ratio 0.5
    assert outcome.matched == "다나"


def test_ko_unrelated_name_declines():
    roster = _roster("다나")
    outcome = match_candidate("민수", roster)
    assert outcome.matched is None
    assert outcome.reason == "no_roster_match"


# ---------------------------------------------------------------------------
# ar — Arabic: spaced, no case, ordinary tokenization, no common-word filter.
# ---------------------------------------------------------------------------


def test_ar_name_token_is_matched():
    roster = _roster("دانا")
    assert _best_match("دانا قالت ماذا عن الميزانية؟", roster) == "دانا"


def test_ar_unrelated_name_declines():
    roster = _roster("دانا")
    outcome = match_candidate("سارة", roster)
    assert outcome.matched is None
    assert outcome.reason == "no_roster_match"


def test_ar_typo_fuzzy_matches_below_the_flat_latin_floor():
    """'دينا' vs 'دانا' — a one-character substitution, ratio 0.75. Below the
    flat 0.85 Latin floor (which would refuse this typo outright) but above
    the length-4 adapted floor (0.70), so this is a genuine save, not a case
    the old flat constant would have caught anyway."""
    roster = _roster("دانا")
    outcome = match_candidate("دينا", roster)
    assert outcome.matched == "دانا"


# ---------------------------------------------------------------------------
# hi — Hindi/Devanagari: spaced, no case, combining vowel signs (matras) must
# stay attached to their base consonant during tokenization.
# ---------------------------------------------------------------------------


def test_hi_name_token_is_matched():
    roster = _roster("दाना")
    assert _best_match("दाना ने क्या कहा?", roster) == "दाना"


def test_hi_tokenization_keeps_combining_matras_attached():
    """Regression pin: a plain `\\w`-based tokenizer splits Devanagari at
    every combining vowel sign, since `\\w` does not treat a Mark character as
    a word character. Measured before the fix: 'दाना' fragmented into ['द',
    'न'] with both matras silently dropped as boundaries."""
    candidates = extract_script_aware_candidates("दाना ने क्या कहा?")
    assert "दाना" in candidates
    assert "द" not in candidates


def test_hi_unrelated_name_declines():
    roster = _roster("दाना")
    outcome = match_candidate("अमित", roster)
    assert outcome.matched is None
    assert outcome.reason == "no_roster_match"


def test_hi_typo_fuzzy_matches_below_the_flat_latin_floor():
    """'अमीत' vs 'अमित' — a one-vowel-sign substitution, ratio 0.75. Below the
    flat 0.85 Latin floor but above the length-4 adapted floor (0.70)."""
    roster = _roster("अमित")
    outcome = match_candidate("अमीत", roster)
    assert outcome.matched == "अमित"


# ---------------------------------------------------------------------------
# The 2-character-name fuzzy threshold, explicitly (the brief's own example).
# ---------------------------------------------------------------------------


def test_two_character_cjk_name_typo_matches_below_the_flat_latin_floor():
    """`difflib` ratio for a one-character substitution in a 2-character name
    is 0.5 — far below the 0.85 Latin floor, so this is the case a flat
    threshold would refuse outright regardless of script-awareness elsewhere."""
    roster = _roster("达娜")
    outcome = match_candidate("达丽", roster)  # one character differs
    assert outcome.matched == "达娜"


def test_two_character_cjk_name_unrelated_candidate_still_declines():
    """The lowered floor is not a blanket "accept anything short" rule: a
    2-character candidate sharing NO characters with the entry must still
    decline, exactly as it did before script-awareness existed."""
    roster = _roster("达娜")
    outcome = match_candidate("王刚", roster)
    assert outcome.matched is None
    assert outcome.reason == "no_roster_match"


def test_two_character_name_ambiguous_typo_declines_rather_than_guesses():
    """Two roster entries both plausibly typo-match a 2-character candidate —
    ambiguity, not a coin flip, per the module's design constraint."""
    roster = _roster("达娜", "达丽")  # both one character away from "达莉"
    outcome = match_candidate("达莉", roster)
    assert outcome.matched is None
    assert set(outcome.ambiguous_with) == {"达娜", "达丽"}


# ---------------------------------------------------------------------------
# End to end through resolve_speaker_mentions (no DB — roster supplied
# directly, same pattern test_chat_speaker_resolver.py uses).
# ---------------------------------------------------------------------------


def test_resolve_speaker_mentions_matches_a_non_latin_roster(db_session):
    roster = _roster("达娜")
    result = resolve_speaker_mentions(
        db_session, "那次会议上达娜说了什么？", user_id=1, roster=roster
    )
    assert result.matched == ("达娜",)


def test_resolve_speaker_mentions_declines_an_unrelated_non_latin_question(db_session):
    roster = _roster("达娜")
    result = resolve_speaker_mentions(
        db_session,
        "预算是多少？",
        user_id=1,
        roster=roster,  # "what is the budget?"
    )
    assert result.matched == ()


def test_resolve_speaker_mentions_matches_across_mixed_scripts_in_one_roster(db_session):
    """A roster is not one script — a deployment can have both an English and
    a Korean name, and each question should match against whichever script it
    is written in without the other roster entry interfering."""
    roster = _roster("Dana", "다나")
    en_result = resolve_speaker_mentions(
        db_session, "What did Dana say about pricing?", user_id=1, roster=roster
    )
    ko_result = resolve_speaker_mentions(
        db_session, "다나가 뭐라고 했어요?", user_id=1, roster=roster
    )
    assert en_result.matched == ("Dana",)
    assert ko_result.matched == ("다나",)
