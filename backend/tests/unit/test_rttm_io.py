"""The DER scripts share one RTTM reader (scripts/rttm_io.py)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

pytest.importorskip("pyannote.core")

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


@pytest.fixture(scope="module")
def rttm_io():
    sys.path.insert(0, str(_SCRIPTS))
    try:
        return importlib.import_module("rttm_io")
    finally:
        sys.path.remove(str(_SCRIPTS))


def test_speaker_label_keeps_spaces_so_shared_first_names_stay_distinct(rttm_io, tmp_path):
    path = tmp_path / "clip.rttm"
    path.write_text(
        "SPEAKER clip 1 0.00 2.00 <NA> <NA> Andrej Karpathy <NA> <NA>\n"
        "SPEAKER clip 1 3.00 1.00 <NA> <NA> Andrej Smith <NA> <NA>\n"
        "SPEAKER clip 1 5.00 1.00 <NA> <NA> spk0 <NA> <NA>\n"
        "LEXEME ignored line\n",
        encoding="utf-8",
    )
    ann = rttm_io.read_rttm(path)
    assert sorted(ann.labels()) == ["Andrej Karpathy", "Andrej Smith", "spk0"]
    assert ann.uri == "clip"
    assert ann.get_timeline().duration() == pytest.approx(4.0)
