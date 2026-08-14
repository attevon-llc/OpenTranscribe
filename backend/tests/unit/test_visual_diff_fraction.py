"""The visual-regression comparison must be able to fail (issue #431).

``tests/e2e/test_visual_regression.py`` needs a browser, so its comparison
maths was never covered by a test that runs in the fast suite. It was wrong in
a way that made eight screenshot tests unable to catch the failure they most
needed to: when the capture and the baseline had different heights, the old
implementation cropped BOTH to the shared top-left box and compared only that.

A page that rendered its nav bar and nothing else therefore produced a 60px
capture, was compared against the top 60px of a 1641px baseline, and scored
**0.00% — a pass**. That was live, not hypothetical: the ``speakers`` capture is
1585px against a 1641px baseline, so 56 rows were excluded from every run.

The maths lives in ``tests/e2e/_visual_diff.py`` so both this suite and the e2e
module import the SAME implementation. It could not be imported from
``test_visual_regression.py`` directly — that module carries
``pytest.mark.visual``, registered only in ``tests/e2e/pytest.ini``, so under
``--strict-markers`` the import alone is a collection error here. Copying the
maths into this file instead would let the copy and the real implementation
drift, which is how a guard ends up guarding nothing.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.e2e._visual_diff import CHANNEL_NOISE_THRESHOLD
from tests.e2e._visual_diff import DIFF_TOLERANCE
from tests.e2e._visual_diff import diff_fraction as _diff_fraction

#: Matches the real baselines: 1280px viewport, full-page height.
_H, _W = 1641, 1280


def _noise_image(seed: int = 0) -> np.ndarray:
    """A deterministic image with enough entropy that cropping is detectable."""
    return np.random.RandomState(seed).randint(0, 255, (_H, _W, 3), dtype=np.uint8)


@pytest.mark.parametrize(
    ("rendered_rows", "description"),
    [
        (60, "nav bar only"),
        (500, "top ~30% of the page"),
        (_H - 56, "the exact 56-row shortfall seen live on the speakers page"),
    ],
)
def test_a_truncated_render_cannot_pass(rendered_rows: int, description: str) -> None:
    """A short capture must be charged for the rows it never rendered.

    This is the must-fire case. Every one of these scored 0.00% before the fix.
    """
    baseline = _noise_image()
    truncated = baseline[:rendered_rows].copy()

    fraction = _diff_fraction(truncated, baseline)

    assert fraction > DIFF_TOLERANCE, (
        f"A capture containing only {description} ({rendered_rows} of {_H} rows) "
        f"scored {fraction:.2%}, within the {DIFF_TOLERANCE:.2%} tolerance. The "
        f"comparison is ignoring un-rendered content, so a blank page passes."
    )


def test_identical_images_are_still_clean() -> None:
    """The must-stay-clean case: no false alarm on a genuine match.

    Without this, "charge everything as differing" would satisfy the test above
    while making the whole suite permanently red — equally useless.
    """
    baseline = _noise_image()
    assert _diff_fraction(baseline.copy(), baseline) == 0.0


def test_a_partial_change_is_measured_proportionally() -> None:
    """The magnitude must stay meaningful, not just the pass/fail bit.

    Blanking 100 of 1641 rows is 6.1% of the image. A comparison that reported
    "different" without a proportional number would make the 0.5% tolerance
    arbitrary, and the tolerance is what separates anti-aliasing jitter from a
    real regression.
    """
    baseline = _noise_image()
    changed = baseline.copy()
    changed[:100] = 0

    fraction = _diff_fraction(changed, baseline)
    expected = 100 / _H
    assert abs(fraction - expected) < 0.005, (
        f"Expected ~{expected:.2%} for 100 blanked rows, got {fraction:.2%}."
    )


def test_sub_threshold_noise_is_still_ignored() -> None:
    """Anti-aliasing jitter must not be charged — the reason a tolerance exists.

    A uniform +1/255 shift on every channel is imperceptible and is exactly what
    differs between two runs on the same machine. If the shape fix had also
    changed the per-pixel rule, this suite would fail on its own re-runs.
    """
    baseline = _noise_image()
    jittered = np.clip(baseline.astype(np.int16) + (CHANNEL_NOISE_THRESHOLD - 1), 0, 255).astype(
        np.uint8
    )

    assert _diff_fraction(jittered, baseline) == 0.0


def test_a_wider_render_is_charged_too() -> None:
    """The shortfall can be in either direction, and either is a real change.

    Charging against the LARGER area means a capture that grew is measured
    against its own size; charging against the smaller one would let extra
    content be free.
    """
    baseline = _noise_image()
    taller = np.zeros((_H * 2, _W, 3), dtype=np.uint8)
    taller[:_H] = baseline

    fraction = _diff_fraction(taller, baseline)
    assert fraction >= 0.5, (
        f"A capture twice the baseline's height scored {fraction:.2%}; the extra "
        f"half of the page is not being counted."
    )
