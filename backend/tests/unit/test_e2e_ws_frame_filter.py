"""``tests/e2e/ws_frames.py``: the frame-loss decision behind the missed-frame browser test."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[1] / "e2e" / "ws_frames.py"
_spec = importlib.util.spec_from_file_location("ws_frames_under_test", _PATH)
assert _spec is not None and _spec.loader is not None
ws_frames = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws_frames)

PP = json.dumps({"type": "post_processing_status", "data": {"file_id": "f"}})
TX = json.dumps({"type": "transcription_status", "data": {"file_id": "f"}})


def _run(frames: list) -> tuple[list, Any]:
    fltr = ws_frames.FrameFilter()
    delivered: list = []
    for frame in frames:
        fltr.relay(frame, delivered.append)
    return delivered, fltr


def test_post_processing_frames_are_withheld_and_counted() -> None:
    delivered, fltr = _run([PP, PP])
    assert delivered == []
    assert fltr.dropped == [PP, PP]


def test_every_other_frame_still_reaches_the_page_in_order() -> None:
    delivered, fltr = _run([TX, PP, "ping", b"\x00\x01", TX])
    assert delivered == [TX, "ping", b"\x00\x01", TX]
    assert fltr.dropped == [PP]


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "null", '{"no_type": 1}', b"{}"])
def test_malformed_or_untyped_payloads_are_never_mistaken_for_the_dropped_type(raw) -> None:
    delivered, fltr = _run([raw])
    assert delivered == [raw]
    assert fltr.dropped == []


def test_the_dropped_types_are_configurable() -> None:
    fltr = ws_frames.FrameFilter(drop_types=("transcription_status",))
    delivered: list = []
    fltr.relay(TX, delivered.append)
    fltr.relay(PP, delivered.append)
    assert delivered == [PP]
