"""Unit tests for the Earnings-21 cpWER scorer (issue #193).

Earnings-21 ``.nlp`` references carry token + integer speaker but NO word timing, so the
scorer uses cpWER (speaker-attributed WER via meeteval) which needs no timing. These tests
prove the reference builder groups tokens by speaker correctly and that a perfect
hypothesis scores cpWER 0 — before any real audio number is trusted.

The scorer's transcribe/diarize path needs the GPU/model stack, so only the pure pieces
(``build_reference_words`` + the shared ``cpwer``) are exercised here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``scripts.score_earnings21_cpwer`` importable (backend/ is on sys.path via conftest,
# so ``scripts`` resolves to backend/scripts as a namespace package).
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from scripts.score_earnings21_cpwer import build_reference_words  # noqa: E402
from scripts.score_earnings21_cpwer import normalize_token  # noqa: E402
from scripts.score_earnings21_cpwer import normalize_words  # noqa: E402

# Tiny inline Earnings-21 .nlp: header + 2 speakers, a few tokens.
# Columns: token|speaker|ts|endTs|punctuation|case|tags|wer_tags  (ts/endTs empty, as in
# the real dataset). Speaker 0 says "good morning everyone", speaker 1 says "thanks for".
_NLP_FIXTURE = """token|speaker|ts|endTs|punctuation|case|tags|wer_tags
good|0||||UC|[]|[]
morning|0||||LC|[]|[]
everyone|0||,|LC|[]|[]
thanks|1||||UC|[]|[]
for|1||||LC|[]|[]
"""


@pytest.fixture
def nlp_path(tmp_path: Path) -> Path:
    """Write the inline .nlp fixture to a temp file and return its path."""
    p = tmp_path / "0000001.nlp"
    p.write_text(_NLP_FIXTURE, encoding="utf-8")
    return p


def test_build_reference_groups_by_speaker(nlp_path: Path):
    """The ref builder drops the header, keeps token order, and labels each speaker."""
    ref = build_reference_words(nlp_path)

    # Header skipped, 5 tokens parsed in order.
    assert [w["word"] for w in ref] == ["good", "morning", "everyone", "thanks", "for"]

    # Integer speaker ids kept as string labels; timing dropped (None) in --no-timing mode.
    assert [w["speaker"] for w in ref] == ["0", "0", "0", "1", "1"]
    assert all(w["start"] is None and w["end"] is None for w in ref)

    # Two distinct speakers, grouped correctly.
    by_speaker: dict[str, list[str]] = {}
    for w in ref:
        by_speaker.setdefault(w["speaker"], []).append(w["word"])
    assert by_speaker == {"0": ["good", "morning", "everyone"], "1": ["thanks", "for"]}


def test_perfect_hypothesis_cpwer_zero(nlp_path: Path):
    """A hypothesis identical to the reference (relabeled) scores cpWER 0."""
    pytest.importorskip("meeteval")
    from app.utils.diarization_metrics import cpwer

    ref = build_reference_words(nlp_path)

    # Perfect hypothesis: same tokens/order, speakers relabeled to the engine's scheme.
    # cpWER is permutation-invariant, so distinct labels with the same grouping → 0.
    relabel = {"0": "SPEAKER_00", "1": "SPEAKER_01"}
    hyp = [{**w, "speaker": relabel[w["speaker"]]} for w in ref]

    assert cpwer(ref, hyp) == pytest.approx(0.0)


def test_swapped_speakers_still_zero(nlp_path: Path):
    """cpWER is invariant to which hyp label maps to which ref label."""
    pytest.importorskip("meeteval")
    from app.utils.diarization_metrics import cpwer

    ref = build_reference_words(nlp_path)
    swap = {"0": "SPEAKER_99", "1": "SPEAKER_00"}
    hyp = [{**w, "speaker": swap[w["speaker"]]} for w in ref]

    assert cpwer(ref, hyp) == pytest.approx(0.0)


def test_normalize_token_lowercases_and_strips():
    """Mixed case, punctuation, and diacritics collapse; punct-only tokens vanish."""
    assert normalize_token("Good") == "good"
    assert normalize_token("Culp's") == "culp's"
    assert normalize_token("call,") == "call"
    assert normalize_token("2020") == "2020"
    assert normalize_token("naïve") == "naive"
    assert normalize_token("--") == ""  # punctuation-only → dropped by normalize_words


def test_normalize_words_drops_empty_and_keeps_speaker():
    """normalize_words rewrites the word, keeps the speaker, and drops empty tokens."""
    words = [
        {"word": "Good", "speaker": "0"},
        {"word": "...", "speaker": "0"},  # punct-only → dropped
        {"word": "Morning!", "speaker": "1"},
    ]
    out = normalize_words(words)
    assert [(w["word"], w["speaker"]) for w in out] == [("good", "0"), ("morning", "1")]


def test_normalized_cpwer_ignores_case_and_punctuation(nlp_path: Path):
    """After normalization, a case/punctuation-only difference scores cpWER 0."""
    pytest.importorskip("meeteval")
    from app.utils.diarization_metrics import cpwer

    ref = build_reference_words(nlp_path)
    # Hypothesis differs only by case + trailing punctuation + a relabel.
    relabel = {"0": "SPEAKER_00", "1": "SPEAKER_01"}
    hyp = [{**w, "word": w["word"].upper() + ",", "speaker": relabel[w["speaker"]]} for w in ref]

    # Raw cpWER would be nonzero (case/punct mismatch); normalized must be 0.
    assert cpwer(normalize_words(ref), normalize_words(hyp)) == pytest.approx(0.0)
