"""The two FFmpeg.wasm build Dockerfiles must pin the same emsdk toolchain.

``frontend/Dockerfile.prod`` ships the core; ``frontend/ffmpeg-wasm-build/Dockerfile`` is
the standalone copy that the dev ``prebuild`` and the CI cache use. They once drifted
(6.0.13 vs 3.1.40), so dev and CI tested a different toolchain than the one shipped.
"""

from __future__ import annotations

import re
from pathlib import Path

_FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
_FROM = re.compile(r"^FROM\s+emscripten/emsdk:(\S+)\s+AS\s+emsdk-base\s*$", re.MULTILINE)


def emsdk_pins(text: str) -> list[str]:
    return _FROM.findall(text)


def test_emsdk_pin_detector_finds_a_pin_and_ignores_other_images() -> None:
    assert emsdk_pins("FROM emscripten/emsdk:6.0.13 AS emsdk-base\n") == ["6.0.13"]
    assert emsdk_pins("FROM node:22 AS emsdk-base\n") == []


def test_both_ffmpeg_wasm_dockerfiles_pin_the_same_emsdk() -> None:
    prod = emsdk_pins((_FRONTEND / "Dockerfile.prod").read_text())
    standalone = emsdk_pins((_FRONTEND / "ffmpeg-wasm-build" / "Dockerfile").read_text())
    assert len(prod) == 1, prod
    assert len(standalone) == 1, standalone
    assert prod == standalone
