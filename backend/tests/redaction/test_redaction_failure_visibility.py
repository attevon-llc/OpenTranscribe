"""Failures in the redaction plane are visible and never read as "nothing to mask" (lane B10)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.media import MediaFile
from app.models.user import User
from app.services.redaction import device
from app.services.redaction.detectors import pii_presidio
from app.services.redaction.reveal_audit import audit_unredacted_reveal
from app.services.redaction.service import RedactionService
from app.services.redaction.spans import MalformedSpanError
from app.services.redaction.spans import apply_redactions


def _traceback_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.exc_info or r.exc_text]


def test_a_cached_span_with_a_range_but_bad_fields_fails_closed():
    """It used to be dropped, so the text it covered went out unmasked."""
    broken = {"char_start": 5, "char_end": 9, "category": "pii"}  # no entity_type
    with pytest.raises(MalformedSpanError):
        apply_redactions("call John now", [broken], enabled_categories={"pii"})


def test_an_entry_with_no_character_range_is_still_ignored():
    masked, applied = apply_redactions("call John now", [{"toxicity": 0.9}])
    assert (masked, applied) == ("call John now", [])


def test_a_cuda_probe_error_is_logged_and_redaction_runs_on_cpu(caplog):
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: (_ for _ in ()).throw(RuntimeError("driver mismatch"))
        )
    )
    with patch.dict("sys.modules", {"torch": fake_torch}), caplog.at_level(logging.WARNING):
        assert device._cuda_available() is False
    assert _traceback_records(caplog)


def test_a_cuda_probe_bug_is_not_swallowed():
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: (_ for _ in ()).throw(TypeError("bug")))
    )
    with patch.dict("sys.modules", {"torch": fake_torch}), pytest.raises(TypeError):
        device._cuda_available()


def test_a_pii_detector_crash_is_recorded_and_logs_frames_without_the_text(caplog):
    secret = "Jane Roe 555-12-3456"

    def crash(*_a, **_k):
        raise RuntimeError(f"spaCy choked on: {secret}")

    failures: list[str] = []
    with (
        patch.object(pii_presidio, "detect_pii", crash),
        caplog.at_level(logging.WARNING),
    ):
        spans, _ = RedactionService.detect_segment_spans(
            secret, None, {}, run_profanity=False, run_toxicity=False, failures=failures
        )

    assert spans == []
    assert failures == ["pii"]
    records = _traceback_records(caplog)
    assert records
    assert all(secret not in (r.exc_text or "") for r in records)


def test_a_failed_reveal_audit_write_logs_a_traceback(caplog):
    db_file = MediaFile(id=1, uuid="u", organization_id=None)
    user = User(id=2, email="a@example.com")
    with (
        patch("app.auth.audit.audit_logger.log", side_effect=OSError("disk full")),
        caplog.at_level(logging.ERROR),
    ):
        audit_unredacted_reveal(db_file, user, {"pii"}, surface="transcript")
    assert _traceback_records(caplog)
