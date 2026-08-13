"""Pixel-comparison maths for the visual-regression suite.

Split out of ``test_visual_regression.py`` so it can be imported by a fast unit
test. It cannot be imported from that module directly: it carries
``pytest.mark.visual``, which is registered in ``tests/e2e/pytest.ini`` and not
in ``pyproject.toml``, so under ``--strict-markers`` the import alone is a
collection error in the fast suite.

Copying the maths into the unit test instead would let the copy and the real
implementation drift — the exact way a guard ends up guarding nothing. One
implementation, two importers.

Not named ``test_*``, so pytest does not collect it.
"""

from __future__ import annotations

import numpy as np

# Allowed fraction of differing pixels before a comparison is treated as a real
# visual change (covers sub-pixel anti-aliasing / font-hinting jitter).
DIFF_TOLERANCE = 0.005  # 0.5%

# Per-channel intensity delta below which a pixel is considered "same" (ignores
# imperceptible 1-2/255 rendering noise).
CHANNEL_NOISE_THRESHOLD = 12


def diff_fraction(a: np.ndarray, b: np.ndarray) -> float:
    """Return the fraction of pixels that differ beyond the noise threshold.

    Shapes need not match. Where they differ, the overlapping box is compared
    pixel-by-pixel and **every non-overlapping pixel counts as differing**,
    measured against the larger of the two areas.

    This used to crop both images to the shared box and compare only that,
    which made the suite unable to fail on the failure it most needed to catch:
    a page that renders its nav bar and nothing else produced a 60px-tall
    capture, was compared against only the top 60px of a 1641px baseline, and
    scored **0.00% — a pass**. Measured, not theorised. It was also live: the
    speakers capture is 1585px against a 1641px baseline, so 56 rows of every
    run were silently excluded.

    Args:
        a: First image as an RGB uint8 array.
        b: Second image as an RGB uint8 array.

    Returns:
        Differing-pixel count divided by the larger area, in [0, 1].
    """
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    delta = np.abs(a[:h, :w].astype(np.int16) - b[:h, :w].astype(np.int16))
    # A pixel differs if ANY channel exceeds the per-channel noise threshold.
    differing = np.any(delta > CHANNEL_NOISE_THRESHOLD, axis=-1)

    total = max(a.shape[0] * a.shape[1], b.shape[0] * b.shape[1])
    unmatched = total - (h * w)
    return float(differing.sum() + unmatched) / float(total)
