"""Self-validation unit tests for diarization metrics (issue #193, §4.6).

These prove the metric math on inputs with KNOWN answers before any real number is
trusted: identity, a deliberate 2-word bleed, permutation invariance, overlap exclusion,
midpoint lookup, RTTM collapse, edge cases, and the paired bootstrap.
"""

from __future__ import annotations

import pytest

from app.utils.diarization_metrics import assign_words_from_turns
from app.utils.diarization_metrics import boundary_prf
from app.utils.diarization_metrics import count_bleed_islands
from app.utils.diarization_metrics import flatten_words
from app.utils.diarization_metrics import island_histogram
from app.utils.diarization_metrics import map_hyp_to_ref
from app.utils.diarization_metrics import paired_bootstrap_wser
from app.utils.diarization_metrics import speaker_count_match
from app.utils.diarization_metrics import words_to_rttm
from app.utils.diarization_metrics import wser


def _words(speakers: list[str], dur: float = 1.0) -> list[dict]:
    """Build a parallel word list, one word per speaker label, 1s apart."""
    return [
        {"word": f"w{i}", "start": i * dur, "end": (i + 1) * dur, "speaker": s}
        for i, s in enumerate(speakers)
    ]


def test_identity_wser_zero():
    ref = _words(["A", "A", "B", "B", "A"])
    res = wser(ref, ref)
    assert res["wser"] == 0.0
    assert res["t_wser"] == 0.0
    assert res["n_excluded"] == 0
    seq = [w["speaker"] for w in ref]
    assert count_bleed_islands(seq, seq) == []


def test_known_two_word_bleed():
    # ref: 7×A then 3×B. hyp bleeds words 3-4 (A→B), flanked by A on both sides.
    ref = _words(["A"] * 7 + ["B"] * 3)
    hyp = _words(["A", "A", "A", "B", "B", "A", "A", "B", "B", "B"])
    res = wser(ref, hyp)
    assert res["wser"] == pytest.approx(0.2)  # exactly 2 / 10
    assert res["n_word_errors"] == 2

    ref_seq = [w["speaker"] for w in ref]
    hyp_seq = map_hyp_to_ref([w["speaker"] for w in hyp], res["perm"])
    islands = count_bleed_islands(ref_seq, hyp_seq, max_island=3)
    assert len(islands) == 1
    assert islands[0][2] == 2  # length 2
    assert island_histogram(islands) == {"1": 0, "2": 1, "3": 0, "4+": 0}


def test_permutation_invariance():
    ref = _words(["A"] * 7 + ["B"] * 3)
    hyp = _words(["A", "A", "A", "B", "B", "A", "A", "B", "B", "B"])
    swap = {"A": "B", "B": "A"}
    hyp_swapped = _words([swap[w["speaker"]] for w in hyp])
    assert wser(ref, hyp_swapped)["wser"] == pytest.approx(wser(ref, hyp)["wser"])


def test_overlap_exclusion():
    ref = _words(["A"] * 7 + ["B"] * 3)
    ref[0]["speaker"] = "OVERLAP"  # un-scorable
    hyp = _words(["A", "A", "A", "B", "B", "A", "A", "B", "B", "B"])
    res = wser(ref, hyp)
    assert res["n_excluded"] == 1
    assert res["n_scored"] == 9
    assert res["wser"] == pytest.approx(2 / 9)


def test_single_speaker():
    ref = _words(["A"] * 5)
    res = wser(ref, ref)
    assert res["wser"] == 0.0
    assert speaker_count_match(ref, ref)["match"] is True
    assert count_bleed_islands(["A"] * 5, ["A"] * 5) == []


def test_count_mismatch():
    ref = _words(["A"] * 5)  # single ref speaker
    hyp = _words(["A", "B", "B", "A", "A"])  # hyp invents a 2-word B island
    sc = speaker_count_match(ref, hyp)
    assert sc["match"] is False
    assert sc["ref_speakers"] == 1 and sc["hyp_speakers"] == 2
    res = wser(ref, hyp)
    assert res["wser"] == pytest.approx(0.4)  # 2 / 5


def test_empty_inputs_no_crash():
    assert flatten_words([]) == []
    res = wser([], [])
    assert res["wser"] == 0.0 and res["n_scored"] == 0


def test_mismatched_inventory_raises():
    with pytest.raises(ValueError):
        wser(_words(["A", "B"]), _words(["A"]))


def test_assign_words_from_turns_midpoint():
    turns = [(0.0, 2.0, "A"), (2.0, 3.0, "B")]
    words = [
        {"word": "x", "start": 0.0, "end": 1.0},  # mid 0.5 → A
        {"word": "y", "start": 1.0, "end": 2.0},  # mid 1.5 → A
        {"word": "z", "start": 2.0, "end": 3.0},  # mid 2.5 → B
        {"word": "q", "start": 5.0, "end": 6.0},  # mid 5.5 → None (silence)
    ]
    out = [w["speaker"] for w in assign_words_from_turns(words, turns)]
    assert out == ["A", "A", "B", None]


def test_words_to_rttm_collapses():
    words = _words(["A", "A", "B"])
    rttm = words_to_rttm(words, uri="clip").strip().splitlines()
    assert len(rttm) == 2
    assert rttm[0].startswith("SPEAKER clip 1 0.000 2.000") and rttm[0].endswith("A <NA> <NA>")
    assert rttm[1].startswith("SPEAKER clip 1 2.000 1.000") and rttm[1].endswith("B <NA> <NA>")


def test_boundary_prf_perfect_and_off():
    ref = ["A", "A", "B", "B"]
    assert boundary_prf(ref, ref)["f1"] == 1.0
    hyp = ["A", "B", "B", "B"]  # boundary moved to index 1
    prf = boundary_prf(ref, hyp)
    assert prf["f1"] == 0.0


def test_paired_bootstrap_detects_improvement():
    # Every file improves OFF→ON; the bootstrap CI must exclude 0.
    per_file = [(8, 2, 100), (6, 1, 90), (10, 3, 120), (5, 0, 70)]
    res = paired_bootstrap_wser(per_file, n_boot=500, seed=1)
    assert res["mean"] > 0
    assert res["significant"] is True


def test_categorize_errors_boundary_vs_interior():
    from app.utils.diarization_metrics import categorize_errors

    # ref boundaries between idx 3/4 and 5/6. hyp errors: idx 0 (interior/backchannel),
    # idx 3 (adjacent to the 3/4 boundary).
    ref = _words(["A", "A", "A", "A", "B", "B", "A", "A", "A", "A"])
    hyp = _words(["B", "A", "A", "B", "B", "B", "A", "A", "A", "A"])
    res = categorize_errors(ref, hyp, perm={}, boundary_window=2)
    assert res["boundary_errors"] == 1
    assert res["interior_errors"] == 1
    assert res["interior_examples"][0]["ref"] == "A" and res["interior_examples"][0]["hyp"] == "B"


def test_der_identity_zero():
    pytest.importorskip("pyannote.metrics")
    from app.utils.diarization_metrics import der

    turns = [(0.0, 5.0, "A"), (5.0, 10.0, "B")]
    res = der(turns, turns, collar=0.0)
    assert res["der"] == pytest.approx(0.0, abs=1e-6)


def test_cpwer_identity_zero():
    pytest.importorskip("meeteval")
    from app.utils.diarization_metrics import cpwer

    words = [
        {"word": "hello", "start": 0, "end": 1, "speaker": "A"},
        {"word": "world", "start": 1, "end": 2, "speaker": "B"},
    ]
    assert cpwer(words, words) == pytest.approx(0.0)
