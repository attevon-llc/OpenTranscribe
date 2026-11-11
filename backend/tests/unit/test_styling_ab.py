"""The styling A/B tool must be able to FAIL (plan section 6, ``styling_ab.py``).

A "pixel-identical" claim is only evidence if the tool that makes it can say no. These tests
pin the pure parts: the manifest validator, the computed-style diff, the verdict rules, the
heat-map writer and the negative-control rule. The browser half needs a stack and is
exercised by ``scripts/e2e/styling-ab.sh --selfcheck`` / ``--negative-control``.

The e2e helpers import each other flat (``from _visual_diff import ...``) because they run
under ``tests/e2e``'s own rootdir, so this loads them with that directory on ``sys.path``
(same approach as ``test_a11y_allowlist_format.py``).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

_E2E_DIR = Path(__file__).resolve().parents[1] / "e2e"


def _load(name: str) -> ModuleType:
    if str(_E2E_DIR) not in sys.path:
        sys.path.insert(0, str(_E2E_DIR))
    module = __import__(name)
    assert isinstance(module, ModuleType)
    return module


ab = _load("styling_ab")
screens = _load("styling_screens")


# --- manifest ---------------------------------------------------------------------------
def test_shipped_manifest_is_well_formed() -> None:
    assert screens.validate() == []


def test_manifest_covers_the_plan_screens() -> None:
    ids = {s.id for s in screens.SCREENS}
    for required in ("login", "gallery", "file-detail", "confirm-delete", "ui-kit", "ui-kit-focus"):
        assert required in ids
    assert len(ids) == len(screens.SCREENS)


def test_validator_flags_a_duplicate_id_and_a_malformed_step() -> None:
    """Must-fire: the validator is not a rubber stamp."""
    bad = (
        screens.Screen("dup", "/"),
        screens.Screen("dup", "/x", steps=("click",)),
        screens.Screen("noslash", "x"),
        screens.Screen("weird:step", "/", steps=("teleport:.x",)),
    )
    problems = "\n".join(screens.validate(bad))
    assert "duplicate screen id 'dup'" in problems
    assert "malformed step 'click'" in problems
    assert "must start with '/'" in problems
    assert "malformed step 'teleport:.x'" in problems


def test_all_four_variants_are_the_default() -> None:
    assert screens.VARIANTS == ("en-light", "en-dark", "ar-light", "ar-dark")


# --- computed-style diff ----------------------------------------------------------------
BEFORE = {
    "body>div:1": {"color": "rgb(0, 0, 0)", "padding-left": "8px"},
    "body>div:1>button:1": {"padding-left": "16px"},
}


def test_identical_snapshots_have_no_changes() -> None:
    """Must-stay-clean."""
    assert ab.diff_snapshots(BEFORE, {k: dict(v) for k, v in BEFORE.items()}) == []


def test_a_one_pixel_padding_change_is_found() -> None:
    after = {k: dict(v) for k, v in BEFORE.items()}
    after["body>div:1>button:1"]["padding-left"] = "17px"
    changes = ab.diff_snapshots(BEFORE, after)
    assert [(c.key, c.prop, c.before, c.after) for c in changes] == [
        ("body>div:1>button:1", "padding-left", "16px", "17px")
    ]


def test_a_vanished_or_added_element_is_a_change() -> None:
    after = {"body>div:1": dict(BEFORE["body>div:1"]), "body>p:1": {"color": "red"}}
    props = {(c.key, c.before, c.after) for c in ab.diff_snapshots(BEFORE, after)}
    assert ("body>div:1>button:1", "present", "absent") in props
    assert ("body>p:1", "absent", "present") in props


def test_aggregate_groups_by_property_and_value() -> None:
    per_screen = {
        "login__en-light": [ab.Change("a", "padding-left", "16px", "17px")],
        "gallery__en-light": [
            ab.Change("b", "padding-left", "16px", "17px"),
            ab.Change("c", "padding-left", "16px", "17px"),
        ],
    }
    (line,) = ab.aggregate_changes(per_screen)
    assert line.startswith("padding-left: 16px -> 17px  (3 elements; screens ")
    assert "gallery__en-light" in line
    assert "login__en-light" in line


# --- verdict rules ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mode", "fraction", "computed", "control", "expected"),
    [
        ("zero", 0.0, 0, False, "PASS"),
        ("zero", ab.ZERO_PIXEL_FRACTION, 0, False, "PASS"),
        ("zero", ab.ZERO_PIXEL_FRACTION * 1.5, 0, False, "FAIL"),
        ("zero", 0.0, 1, False, "FAIL"),  # invisible to the eye, found by computed style
        ("intended", 0.2, 5, False, "CHANGED"),
        ("intended", 0.0, 0, False, "PASS"),
        ("intended", 0.0, 1, True, "FAIL"),  # a control screen is held to the zero rule
    ],
)
def test_verdict_rules(
    mode: str, fraction: float, computed: int, control: bool, expected: str
) -> None:
    assert ab.verdict(mode, fraction, computed, is_control=control) == expected


def test_error_and_skip_are_never_a_pass() -> None:
    assert ab.verdict("zero", 0.0, 0, error="boom") == "ERROR"
    assert ab.verdict("zero", 0.0, 0, skipped="gated") == "SKIP"


# --- heat map ---------------------------------------------------------------------------
def _flat(h: int, w: int, value: int = 200) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


def test_diff_image_is_clean_for_identical_images() -> None:
    img = _flat(10, 10)
    out = ab.make_diff_image(img, img.copy())
    assert not np.any(np.all(out == (255, 0, 255), axis=-1))


def test_diff_image_paints_only_the_differing_pixel() -> None:
    a, b = _flat(10, 10), _flat(10, 10)
    b[3, 4] = (0, 0, 0)
    out = ab.make_diff_image(a, b)
    magenta = np.all(out == (255, 0, 255), axis=-1)
    assert magenta.sum() == 1
    assert magenta[3, 4]


def test_diff_image_charges_unmatched_area_when_sizes_differ() -> None:
    out = ab.make_diff_image(_flat(10, 10), _flat(12, 10))
    assert np.all(out[10:, :] == (255, 0, 255))
    assert not np.any(np.all(out[:10, :] == (255, 0, 255), axis=-1))


# --- negative control -------------------------------------------------------------------
def _row(key: str, verdict: str, props: list[str], btn: bool = True) -> dict[str, object]:
    return {"key": key, "verdict": verdict, "changed_props": props, "has_btn": btn}


def test_negative_control_ok_when_every_btn_screen_fails_with_padding() -> None:
    rows = [
        _row("a", "FAIL", ["padding-left", "padding-right"]),
        _row("b", "FAIL", ["padding-left", "padding-right", "width"]),
        _row("c", "PASS", [], btn=False),
    ]
    ok, line = ab.negative_control_verdict(rows)
    assert ok, line
    assert line.startswith("NEGATIVE CONTROL OK")


def test_negative_control_broken_when_a_btn_screen_passes() -> None:
    """Must-fire: a tool that cannot fail is not evidence."""
    rows = [_row("a", "FAIL", ["padding-left", "padding-right"]), _row("b", "PASS", [])]
    ok, line = ab.negative_control_verdict(rows)
    assert not ok
    assert "b" in line


def test_negative_control_broken_when_padding_is_not_listed() -> None:
    ok, _ = ab.negative_control_verdict([_row("a", "FAIL", ["color"])])
    assert not ok


def test_negative_control_broken_with_no_btn_anywhere() -> None:
    ok, _ = ab.negative_control_verdict([_row("a", "PASS", [], btn=False)])
    assert not ok


# --- selection and steps ----------------------------------------------------------------
def test_select_screens_globs_and_rejects_unknown() -> None:
    chosen = ab.select_screens("settings-profile,login*")
    assert {s.id for s in chosen} >= {"settings-profile", "login"}
    with pytest.raises(SystemExit):
        ab.select_screens("no-such-screen")


def test_split_step_keeps_colons_inside_selectors() -> None:
    assert ab.split_step("click:a:hover") == ("click", "a:hover")
    assert ab.split_step("settings:profile") == ("settings", "profile")


# --- upload-state screens ---------------------------------------------------------------
def test_sample_wav_is_a_fixed_decodable_file() -> None:
    import io
    import wave

    first = ab.sample_wav()
    assert first == ab.sample_wav()
    with wave.open(io.BytesIO(first)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (1, 8000, 800)


def test_upload_screens_are_in_the_manifest_and_well_formed() -> None:
    by_id = screens.by_id()
    for sid in ("upload-tags", "upload-record", "upload-failed", "upload-active"):
        assert sid in by_id
        assert screens.validate((by_id[sid],)) == []


def test_only_screens_that_hold_a_request_skip_network_idle() -> None:
    # A held request keeps `networkidle` from firing, so exactly those screens opt out.
    held = {s.id for s in screens.SCREENS if any(st.endswith("|0") for st in s.steps)}
    skipping = {s.id for s in screens.SCREENS if not s.network_idle}
    assert held == skipping == {"upload-active"}


def test_screens_that_queue_an_upload_run_in_their_own_context() -> None:
    # The upload queue persists in the browser profile; a shared context restores it (and the
    # tray badge) into whichever screen is photographed next.
    queues = {s.id for s in screens.SCREENS if "click:.nav-submit" in s.steps}
    assert (
        queues
        == {s.id for s in screens.SCREENS if s.isolated}
        == {"upload-failed", "upload-active"}
    )


def test_new_step_kinds_are_rejected_when_malformed() -> None:
    bad = (screens.Screen("x", "/", steps=("pause", "route:")),)
    problems = "\n".join(screens.validate(bad))
    assert "malformed step 'pause'" in problems
    assert "malformed step 'route:'" in problems


def test_mock_route_zero_holds_the_request_and_other_status_answers_it() -> None:
    registered: list[Callable[[Any], Any]] = []

    class FakePage:
        def route(self, glob: str, handler: Callable[[Any], Any]) -> None:
            registered.append(handler)

    class FakeRoute:
        fulfilled: dict[str, object] | None = None

        def fulfill(self, **kw: object) -> None:
            self.fulfilled = kw

    ab._mock_route(FakePage(), "**/x", 0)
    ab._mock_route(FakePage(), "**/x", 500)
    held, failing = registered
    route = FakeRoute()
    assert held(route) is None and route.fulfilled is None
    failing(route)
    assert route.fulfilled is not None and route.fulfilled["status"] == 500
