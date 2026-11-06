"""``HardwareConfig.log_vram_usage`` must not crash when the stats carry no VRAM numbers.

``get_vram_usage`` returns a full set of CUDA memory figures only on a CUDA device; on
CPU/MPS it returns ``{"device": ..., "message": "Not using CUDA"}`` and on failure
``{"error": ...}``. Logging is a side concern of the pipeline, so an incomplete dict
must degrade to one DEBUG line instead of a ``KeyError`` mid-transcription.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.utils.hardware_detection import HardwareConfig

LOGGER_NAME = "app.utils.hardware_detection"

FULL_STATS: dict[str, Any] = {
    "allocated_mb": 1234.0,
    "reserved_mb": 2048.0,
    "free_mb": 10000.0,
    "device_used_mb": 3000.0,
    "device_free_mb": 9000.0,
    "total_mb": 12288.0,
    "usage_percent": 10.04,
}


def _log_with_stats(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stats: dict[str, Any]
) -> list[logging.LogRecord]:
    config = HardwareConfig(force_device="cpu")
    monkeypatch.setattr(config, "get_vram_usage", lambda: stats)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        config.log_vram_usage("unit-test")
    return [r for r in caplog.records if r.name == LOGGER_NAME]


def _by_level(records: list[logging.LogRecord], level: int) -> list[logging.LogRecord]:
    return [r for r in records if r.levelno == level]


def test_non_cuda_stats_log_one_debug_line_naming_the_device(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    records = _log_with_stats(monkeypatch, caplog, {"device": "cpu", "message": "Not using CUDA"})

    assert _by_level(records, logging.INFO) == []
    debug = _by_level(records, logging.DEBUG)
    assert len(debug) == 1
    message = debug[0].getMessage()
    assert "cpu" in message
    assert "Not using CUDA" in message


def test_stats_missing_one_figure_log_debug_not_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    partial = {k: v for k, v in FULL_STATS.items() if k != "usage_percent"}
    partial["device"] = "cuda"

    records = _log_with_stats(monkeypatch, caplog, partial)

    assert _by_level(records, logging.INFO) == []
    debug = _by_level(records, logging.DEBUG)
    assert len(debug) == 1
    assert "cuda" in debug[0].getMessage()


def test_stats_without_device_or_message_still_log_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    records = _log_with_stats(monkeypatch, caplog, {"allocated_mb": 1.0})

    assert _by_level(records, logging.INFO) == []
    assert len(_by_level(records, logging.DEBUG)) == 1


def test_full_stats_log_one_info_line_with_the_figures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    records = _log_with_stats(monkeypatch, caplog, dict(FULL_STATS))

    assert _by_level(records, logging.DEBUG) == []
    info = _by_level(records, logging.INFO)
    assert len(info) == 1
    message = info[0].getMessage()
    assert "unit-test" in message
    for figure in ("1234MB alloc", "2048MB reserved", "12288MB total", "10.0%"):
        assert figure in message


def test_error_stats_log_one_debug_line_and_no_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    records = _log_with_stats(monkeypatch, caplog, {"error": "x"})

    assert _by_level(records, logging.INFO) == []
    debug = _by_level(records, logging.DEBUG)
    assert len(debug) == 1
    assert "x" in debug[0].getMessage()
