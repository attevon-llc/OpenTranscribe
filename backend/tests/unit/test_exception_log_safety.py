"""A planted transcript string in an exception message must never reach the log."""

from __future__ import annotations

import logging

import pytest

from app.utils.exception_log_safety import MessageFreeTracebackFilter
from app.utils.exception_log_safety import format_frames_only

SECRET = "the patient said SECRET-TRANSCRIPT-TEXT"


def _render() -> None:
    raise ValueError(SECRET)


def test_frames_are_kept_and_message_is_dropped():
    try:
        _render()
    except ValueError as exc:
        text = format_frames_only(exc)
    assert SECRET not in text
    assert "_render" in text and "builtins.ValueError" in text


def test_chained_cause_message_is_dropped_too():
    try:
        try:
            _render()
        except ValueError as inner:
            raise RuntimeError("outer " + SECRET) from inner
    except RuntimeError as exc:
        text = format_frames_only(exc)
    assert SECRET not in text and "Caused by" in text


def test_filter_strips_message_from_formatted_record():
    log = logging.getLogger("test.exception_log_safety")
    log.addFilter(MessageFreeTracebackFilter())
    records: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Grab()
    log.addHandler(handler)
    with pytest.raises(ValueError) as caught:
        _render()
    try:
        log.error("export failed", exc_info=caught.value)
    finally:
        log.removeHandler(handler)
    rendered = logging.Formatter().format(records[0])
    assert SECRET not in rendered and "_render" in rendered
