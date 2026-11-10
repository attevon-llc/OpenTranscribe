"""Branches that run only when the environment is degraded or incomplete."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.utils import diarization_metrics
from app.utils.hardware_detection import resolve_cuda_device_index


def test_cuda_device_count_failure_falls_back_to_ordinal_zero():
    def _boom():
        raise RuntimeError("driver/library version mismatch")

    assert resolve_cuda_device_index(SimpleNamespace(device_count=_boom)) == 0


def test_cpwer_names_the_missing_meeteval_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "meeteval", None)
    monkeypatch.setitem(sys.modules, "meeteval.wer", None)

    with pytest.raises(ImportError, match="meeteval is required for cpWER"):
        diarization_metrics.cpwer([], [])
