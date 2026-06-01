"""Unit tests for the SpeakerUpdate schema's server-side length validation.

Mirrors the frontend's display_name <= 100 char cap; the backend is the system of
record (thin-frontend / fat-backend). Pure Pydantic — no DB or live stack needed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.media import SpeakerUpdate


def test_display_name_at_limit_is_accepted() -> None:
    update = SpeakerUpdate(display_name="x" * 100)
    assert update.display_name == "x" * 100


def test_display_name_over_limit_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SpeakerUpdate(display_name="x" * 101)


def test_name_and_suggested_name_over_limit_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SpeakerUpdate(name="x" * 101)
    with pytest.raises(ValidationError):
        SpeakerUpdate(suggested_name="x" * 101)


def test_none_and_empty_are_allowed() -> None:
    # None (no change) and empty string (clear) must still be valid.
    assert SpeakerUpdate().display_name is None
    assert SpeakerUpdate(display_name="").display_name == ""
    assert SpeakerUpdate(display_name="Alice").display_name == "Alice"
