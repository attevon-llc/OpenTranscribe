"""Issue #661 phase 1.5 — the reuse-SUCCESS log line must be visible at default (INFO) level.

Both FAILURE branches of ``_log_reuse_decision`` already log at INFO; the SUCCESS branch was
``logger.debug``, so a real worker's default-level log could show the optimisation NOT firing
but never show it firing — "no measurement means anything until the fast path is confirmed in
a real worker log" (E0's own rule) was only half satisfiable.
"""

from __future__ import annotations

import logging

from app.transcription import diar_shared_volume


def test_reuse_success_is_logged_at_info(caplog):
    with caplog.at_level(logging.INFO, logger="app.transcription.diar_shared_volume"):
        diar_shared_volume._log_reuse_decision("/scratch/opentranscribe/engine/x.wav", reused=True)
    assert any(
        "reusing staged WAV" in r.message
        and r.levelno == logging.INFO
        and r.name == "app.transcription.diar_shared_volume"
        for r in caplog.records
    )
