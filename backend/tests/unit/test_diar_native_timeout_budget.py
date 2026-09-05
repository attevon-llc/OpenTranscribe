"""Issue #656 Step 4: the per-request /diarize timeout must be duration-scaled, not flat.

A flat DIAR_NATIVE_TIMEOUT_S is wrong in both directions: 3600s is meaningless for a
30-second memo, and a naively lowered flat value would fail a genuine multi-hour recording.
See the DIAR_SIDECAR_* comment in core/constants.py for the derivation this pins.
"""

from __future__ import annotations

import logging

import numpy as np

import app.transcription.diarizer_native as diarizer_native
from app.transcription.diarizer_native import _diarize_budget_s
from app.transcription.diarizer_native import _warn_if_timeout_risks_redelivery


class TestDurationScaledBudget:
    def test_a_30_second_clip_gets_a_small_budget_not_the_flat_ceiling(self, monkeypatch):
        monkeypatch.setattr(diarizer_native, "_TIMEOUT_S", 1800.0)
        audio = np.zeros(16000 * 30, dtype=np.float32)  # 30s
        budget = _diarize_budget_s(audio)
        assert budget <= 400, (
            f"a 30s clip should get a small floor-dominated budget, got {budget}s "
            "(today, before Step 4, every call used the flat 3600s ceiling)"
        )
        assert budget == 300 + 30 / 3.0

    def test_a_4_7_hour_clip_is_capped_at_the_ceiling(self, monkeypatch):
        monkeypatch.setattr(diarizer_native, "_TIMEOUT_S", 1800.0)
        audio = np.zeros(int(16000 * 4.7 * 3600), dtype=np.float32)
        budget = _diarize_budget_s(audio)
        assert budget == 1800.0, "above ~1.25h the ceiling must bind exactly"

    def test_default_ceiling_is_1800_not_3600(self):
        """The default was lowered from 3600 -> 1800 (Step 4); the real per-request budget
        now comes from _diarize_budget_s, not this flat value directly."""
        assert diarizer_native._TIMEOUT_S <= 1800

    def test_budget_never_exceeds_the_configured_ceiling(self, monkeypatch):
        monkeypatch.setattr(diarizer_native, "_TIMEOUT_S", 900.0)
        audio = np.zeros(int(16000 * 10 * 3600), dtype=np.float32)  # absurdly long
        assert _diarize_budget_s(audio) == 900.0


class TestRedeliveryRiskWarning:
    def test_warns_at_the_old_default_of_3600(self, caplog):
        caplog.set_level(logging.WARNING)
        _warn_if_timeout_risks_redelivery(3600.0, 21600.0)
        assert any("redelivery" in r.message for r in caplog.records), (
            "restoring the old 3600s default must produce a loud warning: two attempts "
            "(7200s) exceed 25% of the default 21600s CELERY_VISIBILITY_TIMEOUT"
        )

    def test_silent_at_the_new_default_of_1800(self, caplog):
        caplog.set_level(logging.WARNING)
        _warn_if_timeout_risks_redelivery(1800.0, 21600.0)
        assert not any("redelivery" in r.message for r in caplog.records)
