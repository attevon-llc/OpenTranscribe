"""``_mark_upload_dispatch_failed`` persists a consistent ``last_error_message``.

A DomainError replaced the HTTPException that used to flow here; ``str(exc)`` keeps the
"<status>: " prefix the stored text has always had, whereas ``.message`` is the bare detail.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.endpoints.files import upload
from app.core.exceptions import ASRConfigurationError
from app.core.exceptions import InvalidInputError


def _db_with_row(row):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    return db


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (InvalidInputError("bad input"), "400: bad input"),
        (HTTPException(status_code=400, detail="bad input"), "400: bad input"),
        (ASRConfigurationError("no model"), "no model"),
        (RuntimeError("boom"), "boom"),
    ],
)
def test_stored_message(monkeypatch, exc, expected):
    monkeypatch.setattr("app.utils.task_utils.update_media_file_status", lambda *a, **k: None)
    row = SimpleNamespace(last_error_message=None)

    upload._mark_upload_dispatch_failed(_db_with_row(row), 1, 1, exc)

    assert row.last_error_message == expected
