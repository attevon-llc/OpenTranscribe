"""RTTM reading shared by the diarization DER scripts."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyannote.core import Annotation


def read_rttm(path: Path) -> Annotation:
    """Parse an RTTM file into a pyannote ``Annotation``.

    Speaker labels may contain spaces ("Andrej Karpathy"), so the label is everything from
    field 8 on. Taking ``parts[7]`` alone truncates it, which silently merges two speakers
    that share a first name and so changes the speaker count the DER scripts compare.
    """
    from pyannote.core import Annotation, Segment

    ann = Annotation(uri=path.stem)
    with path.open(encoding='utf-8') as fh:
        for line in fh:
            parts = line.split()
            if not parts or parts[0] != 'SPEAKER':
                continue
            start, dur = float(parts[3]), float(parts[4])
            label = ' '.join(parts[7:]).replace('<NA>', '').strip() or parts[7]
            ann[Segment(start, start + dur)] = label
    return ann
