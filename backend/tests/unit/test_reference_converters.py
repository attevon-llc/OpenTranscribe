"""Unit tests for the diarization-boundary reference-converter scripts.

GPU-free, network-free. Covers:

* ``nlp_to_rttm.parse_nlp`` — header skipping, timed + ``--no-timing`` parsing.
* ``diarization_metrics.assign_words_from_turns`` — midpoint lookup correctness.
* ``diarization_metrics.words_to_rttm`` — same-speaker run collapsing.
* ``make_seam_labels`` — ``emit`` → ``turns_from_seam_labels`` round-trip.

The script modules live in ``backend/scripts`` (not a package), so that directory is added
to ``sys.path`` here before importing them by bare name.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import make_seam_labels  # noqa: E402
import nlp_to_rttm  # noqa: E402

from app.utils.diarization_metrics import assign_words_from_turns  # noqa: E402
from app.utils.diarization_metrics import read_rttm  # noqa: E402
from app.utils.diarization_metrics import words_to_rttm  # noqa: E402

# Tiny inline Earnings-21-style .nlp fixture: header + 5 tokens, two speakers.
# Layout: token|speaker|ts|endTs|punctuation|case|tags
_NLP_FIXTURE = """token|speaker|ts|endTs|punctuation|case|tags
Hello|0|0.00|0.40||UC|
there|0|0.45|0.80||LC|
how|1|2.00|2.30||LC|
are|1|2.30|2.55||LC|
you|1|2.55|2.90||LC|
"""


# ── nlp_to_rttm ────────────────────────────────────────────────────────────────


def test_parse_nlp_skips_header_and_reads_tokens(tmp_path: Path) -> None:
    """Header row is dropped; 5 timed tokens parsed with correct text/speaker/timing."""
    nlp_path = tmp_path / "sample.nlp"
    nlp_path.write_text(_NLP_FIXTURE, encoding="utf-8")

    words = nlp_to_rttm.parse_nlp(nlp_path.read_text(encoding="utf-8"))

    assert len(words) == 5
    assert [w["word"] for w in words] == ["Hello", "there", "how", "are", "you"]
    assert words[0]["speaker"] == "0"
    assert words[2]["speaker"] == "1"
    assert words[0]["start"] == 0.0
    assert words[0]["end"] == 0.40
    assert words[4]["end"] == 2.90


def test_parse_nlp_no_timing_mode(tmp_path: Path) -> None:
    """--no-timing drops timing: every token keeps speaker but start/end are None."""
    words = nlp_to_rttm.parse_nlp(_NLP_FIXTURE, no_timing=True)

    assert len(words) == 5
    assert all(w["start"] is None and w["end"] is None for w in words)
    assert [w["speaker"] for w in words] == ["0", "0", "1", "1", "1"]


def test_parse_nlp_comma_delimiter() -> None:
    """A comma-delimited .nlp parses identically when --delimiter ',' is given."""
    comma = _NLP_FIXTURE.replace("|", ",")
    words = nlp_to_rttm.parse_nlp(comma, delimiter=",")
    assert len(words) == 5
    assert words[0]["word"] == "Hello"


def test_write_outputs_emits_rttm_and_words_json(tmp_path: Path) -> None:
    """write_outputs produces both files for timed input and the RTTM has two turns."""
    words = nlp_to_rttm.parse_nlp(_NLP_FIXTURE)
    words_path, rttm_path = nlp_to_rttm.write_outputs(words, tmp_path, uri="rec1")

    assert words_path.exists()
    assert rttm_path is not None and rttm_path.exists()
    turns = read_rttm(str(rttm_path))
    assert len(turns) == 2  # speaker 0 run, then speaker 1 run
    assert turns[0][2] == "0"
    assert turns[1][2] == "1"


def test_write_outputs_no_rttm_without_timing(tmp_path: Path) -> None:
    """Without timing there is no RTTM, only words.json."""
    words = nlp_to_rttm.parse_nlp(_NLP_FIXTURE, no_timing=True)
    words_path, rttm_path = nlp_to_rttm.write_outputs(words, tmp_path)
    assert words_path.exists()
    assert rttm_path is None


# ── assign_words_from_turns (midpoint lookup) ──────────────────────────────────


def test_assign_words_from_turns_midpoint() -> None:
    """Each word's reference speaker is the turn covering its midpoint; gaps → None."""
    turns = [(0.0, 1.0, "A"), (2.0, 3.0, "B")]
    words = [
        {"word": "w0", "start": 0.0, "end": 0.4},  # mid 0.2 → A
        {"word": "w1", "start": 0.6, "end": 0.9},  # mid 0.75 → A
        {"word": "w2", "start": 1.4, "end": 1.6},  # mid 1.5 → silence → None
        {"word": "w3", "start": 2.1, "end": 2.5},  # mid 2.3 → B
        {"word": "w4", "start": 0.9, "end": 1.1},  # mid 1.0 == turn end (exclusive) → None
    ]
    out = assign_words_from_turns(words, turns)
    assert [w["speaker"] for w in out] == ["A", "A", None, "B", None]


def test_assign_words_from_turns_empty_turns() -> None:
    """With no turns every word gets speaker None."""
    words = [{"word": "x", "start": 0.0, "end": 0.5}]
    out = assign_words_from_turns(words, [])
    assert out[0]["speaker"] is None


# ── words_to_rttm (run collapsing) ─────────────────────────────────────────────


def test_words_to_rttm_collapses_consecutive_runs() -> None:
    """Consecutive same-speaker words collapse to one SPEAKER line per run."""
    words = [
        {"word": "a", "start": 0.0, "end": 0.5, "speaker": "A"},
        {"word": "b", "start": 0.5, "end": 1.0, "speaker": "A"},
        {"word": "c", "start": 1.0, "end": 1.5, "speaker": "B"},
        {"word": "d", "start": 1.5, "end": 2.0, "speaker": "A"},
    ]
    rttm = words_to_rttm(words, uri="rec")
    lines = [ln for ln in rttm.splitlines() if ln]
    assert len(lines) == 3  # A-run, B, A
    assert lines[0].split()[7] == "A"
    assert lines[1].split()[7] == "B"
    assert lines[2].split()[7] == "A"
    # First A-run spans 0.0 → 1.0 (dur 1.0).
    assert lines[0].split()[3] == "0.000"
    assert lines[0].split()[4] == "1.000"


def test_words_to_rttm_skips_none_speaker() -> None:
    """Words with speaker None are not emitted to RTTM."""
    words = [
        {"word": "a", "start": 0.0, "end": 0.5, "speaker": None},
        {"word": "b", "start": 0.5, "end": 1.0, "speaker": "A"},
    ]
    rttm = words_to_rttm(words)
    lines = [ln for ln in rttm.splitlines() if ln]
    assert len(lines) == 1
    assert lines[0].split()[7] == "A"


# ── make_seam_labels (emit + round-trip) ───────────────────────────────────────

_TRANSCRIPT = {
    "segments": [
        {
            "words": [
                {"word": "hello", "start": 0.0, "end": 0.5, "speaker": "SPEAKER_00"},
                {"word": "there", "start": 0.5, "end": 1.0, "speaker": "SPEAKER_00"},
                {"word": "hi", "start": 5.0, "end": 5.4, "speaker": "SPEAKER_01"},
                {"word": "back", "start": 5.4, "end": 5.9, "speaker": "SPEAKER_01"},
                {"word": "ok", "start": 10.0, "end": 10.3, "speaker": "SPEAKER_00"},
            ]
        }
    ]
}


def test_find_seams_locates_speaker_changes() -> None:
    """Two speaker changes → two seams with the right from/to and ±pad windows."""
    words = make_seam_labels.flatten_words(_TRANSCRIPT["segments"])
    seams = make_seam_labels.find_seams(words, pad=2.0)
    assert len(seams) == 2
    # First seam: boundary mid(1.0, 5.0) = 3.0 → [1.0, 5.0].
    assert seams[0]["from"] == "SPEAKER_00"
    assert seams[0]["to"] == "SPEAKER_01"
    assert seams[0]["start"] == 1.0
    assert seams[0]["end"] == 5.0
    assert seams[1]["from"] == "SPEAKER_01"
    assert seams[1]["to"] == "SPEAKER_00"


def test_emit_seam_labels_format(tmp_path: Path) -> None:
    """Emitted label lines are tab-separated 'start\\tend\\tSEAM a->b'."""
    words = make_seam_labels.flatten_words(_TRANSCRIPT["segments"])
    text = make_seam_labels.emit_seam_labels(words, pad=2.0)
    lines = [ln for ln in text.splitlines() if ln]
    assert len(lines) == 2
    cols = lines[0].split("\t")
    assert len(cols) == 3
    assert cols[2].startswith("SEAM SPEAKER_00->SPEAKER_01")


def test_seam_label_round_trip_to_rttm(tmp_path: Path) -> None:
    """A corrected Audacity label track reads back into turns and serializes to RTTM.

    Simulates the human correction step: seam markers are replaced by resolved
    single-speaker labels covering the turns, then parsed back.
    """
    corrected = tmp_path / "corrected.txt"
    corrected.write_text(
        "0.000\t1.000\tSPEAKER_00\n5.000\t5.900\tSPEAKER_01\n10.000\t10.300\tSPEAKER_00\n",
        encoding="utf-8",
    )
    turns = make_seam_labels.turns_from_seam_labels(corrected)
    assert turns == [
        (0.0, 1.0, "SPEAKER_00"),
        (5.0, 5.9, "SPEAKER_01"),
        (10.0, 10.3, "SPEAKER_00"),
    ]

    rttm_text = make_seam_labels.turns_to_rttm(turns, uri="rec")
    rttm_path = tmp_path / "reference.turns.rttm"
    rttm_path.write_text(rttm_text, encoding="utf-8")
    parsed = read_rttm(str(rttm_path))
    assert [t[2] for t in parsed] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert parsed[0][0] == 0.0
    assert parsed[1][1] == 5.9


def test_turns_from_seam_labels_skips_uncorrected_seams(tmp_path: Path) -> None:
    """Uncorrected 'SEAM a->b' marker lines are ignored; only resolved labels become turns."""
    mixed = tmp_path / "mixed.txt"
    mixed.write_text(
        "1.000\t5.000\tSEAM SPEAKER_00->SPEAKER_01\n0.000\t1.000\tSPEAKER_00\n",
        encoding="utf-8",
    )
    turns = make_seam_labels.turns_from_seam_labels(mixed)
    assert turns == [(0.0, 1.0, "SPEAKER_00")]
