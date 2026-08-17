"""Tests for ``app/core/logging_config.py`` (issue #474).

``configure_logging`` is the single authority for the root logger's handler —
it must be idempotent, must fully replace any pre-existing handler (not just
add its own), and must select JSON vs. text formatting (plus the
``RequestIdFilter``) purely off ``settings.LOG_FORMAT``. Zero test coverage
before this file. These tests mutate the real root logger, so every test
restores its original handlers/level via the ``_isolated_root_logger`` fixture.
"""

from __future__ import annotations

import logging

import pytest

from app.core.logging_config import _JSON_FMT
from app.core.logging_config import _TEXT_FMT
from app.core.logging_config import RequestIdFilter
from app.core.logging_config import configure_logging

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_root_logger():
    """Save/restore the real root logger around each test.

    ``configure_logging`` is explicitly documented as the single authority for
    the root handler and removes every existing one — running it for real
    against the process's actual root logger (rather than a throwaway
    ``logging.Logger("x")``) is what proves that removal behavior, so the
    fixture's job is only to put things back afterward.
    """
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    for h in original_handlers:
        root.removeHandler(h)
    yield root
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in original_handlers:
        root.addHandler(h)
    root.setLevel(original_level)


def _make_record(msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


# =============================================================================
# configure_logging — text format (default)
# =============================================================================
def test_configure_logging_installs_exactly_one_handler(monkeypatch):
    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "text")

    root = configure_logging_and_get_root()

    assert len(root.handlers) == 1


def test_configure_logging_sets_root_level_info(monkeypatch):
    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "text")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    configure_logging()

    assert root.level == logging.INFO


def test_configure_logging_text_uses_the_text_formatter(monkeypatch):
    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "text")

    root = configure_logging_and_get_root()

    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.formatter is not None
    assert handler.formatter._fmt == _TEXT_FMT
    assert handler.filters == []


def test_configure_logging_marks_the_handler_as_managed(monkeypatch):
    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "text")

    root = configure_logging_and_get_root()

    assert getattr(root.handlers[0], "_obs_managed", None) is True


def test_configure_logging_removes_a_pre_existing_default_handler(monkeypatch):
    """A leftover ``logging.basicConfig()`` handler must not survive — it would
    otherwise double every log line (one bare line + one formatted one)."""
    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "text")
    root = logging.getLogger()
    stray = logging.StreamHandler()
    root.addHandler(stray)
    assert stray in root.handlers

    configure_logging()

    assert stray not in root.handlers
    assert len(root.handlers) == 1


def test_configure_logging_is_idempotent(monkeypatch):
    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "text")

    configure_logging()
    configure_logging()
    configure_logging()

    root = logging.getLogger()
    assert len(root.handlers) == 1


def test_configure_logging_reconfigures_when_format_changes(monkeypatch):
    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "text")
    configure_logging()
    root = logging.getLogger()
    formatter = root.handlers[0].formatter
    assert formatter is not None
    assert formatter._fmt == _TEXT_FMT

    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "json")
    configure_logging()

    assert len(root.handlers) == 1
    from pythonjsonlogger.json import JsonFormatter

    assert isinstance(root.handlers[0].formatter, JsonFormatter)


# =============================================================================
# configure_logging — json format
# =============================================================================
def test_configure_logging_json_installs_json_formatter_and_request_id_filter(monkeypatch):
    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "json")

    root = configure_logging_and_get_root()

    from pythonjsonlogger.json import JsonFormatter

    handler = root.handlers[0]
    assert isinstance(handler.formatter, JsonFormatter)
    assert any(isinstance(f, RequestIdFilter) for f in handler.filters)


def test_configure_logging_json_format_is_case_insensitive(monkeypatch):
    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "JSON")

    root = configure_logging_and_get_root()

    from pythonjsonlogger.json import JsonFormatter

    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_configure_logging_json_formatter_renames_asctime_and_levelname(monkeypatch):
    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "json")

    root = configure_logging_and_get_root()
    handler = root.handlers[0]
    formatted = handler.format(_make_record("json test message"))

    import json

    payload = json.loads(formatted)
    assert payload["message"] == "json test message"
    assert "timestamp" in payload
    assert "level" in payload
    # Renamed away — the raw pythonjsonlogger field names must not leak through.
    assert "asctime" not in payload
    assert "levelname" not in payload


def test_configure_logging_json_request_id_filter_injects_request_id(monkeypatch):
    monkeypatch.setattr("app.core.logging_config.settings.LOG_FORMAT", "json")
    monkeypatch.setattr("app.middleware.audit.get_request_id", lambda: "req-abc-123", raising=False)

    root = configure_logging_and_get_root()
    handler = root.handlers[0]
    record = _make_record("with request id")
    # Filters run via Handler.filter()/handle(), not .format() — apply them
    # explicitly the way logging's dispatch does before formatting.
    # (Filterer.filter returns the record itself, or False — not a bare bool.)
    assert handler.filter(record)
    formatted = handler.format(record)

    import json

    payload = json.loads(formatted)
    assert payload["request_id"] == "req-abc-123"


# =============================================================================
# RequestIdFilter, in isolation
# =============================================================================
def test_request_id_filter_attaches_dash_when_no_request_id(monkeypatch):
    monkeypatch.setattr("app.middleware.audit.get_request_id", lambda: None, raising=False)
    record = _make_record()
    filt = RequestIdFilter()

    result = filt.filter(record)

    assert result is True
    assert getattr(record, "request_id", None) == "-"


def test_request_id_filter_attaches_the_real_request_id(monkeypatch):
    monkeypatch.setattr("app.middleware.audit.get_request_id", lambda: "req-xyz-789", raising=False)
    record = _make_record()
    filt = RequestIdFilter()

    filt.filter(record)

    assert getattr(record, "request_id", None) == "req-xyz-789"


def test_request_id_filter_treats_empty_string_as_missing(monkeypatch):
    # get_request_id() or "-" — an empty string is falsy and must fall through too.
    monkeypatch.setattr("app.middleware.audit.get_request_id", lambda: "", raising=False)
    record = _make_record()
    filt = RequestIdFilter()

    filt.filter(record)

    assert getattr(record, "request_id", None) == "-"


# =============================================================================
# Module-level format constants
# =============================================================================
def test_format_constants_contain_the_expected_fields():
    assert "%(levelname)s" in _JSON_FMT
    assert "%(message)s" in _JSON_FMT
    assert "%(asctime)s" in _JSON_FMT
    assert _TEXT_FMT == "%(asctime)s %(levelname)s:%(name)s:%(message)s"


def configure_logging_and_get_root() -> logging.Logger:
    configure_logging()
    return logging.getLogger()
