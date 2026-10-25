"""A format with no builder must raise, never be served as CSV (issue #564).

``build_export_content`` used to end in a bare ``return _build_csv(...)``: any token added to
``VALID_FORMATS`` without its own branch was silently exported as CSV with HTTP 200. The final
branch is now an explicit ``csv`` check followed by a raise.
"""

from __future__ import annotations

import pytest

from app.services import transcript_export_service as service

pytestmark = pytest.mark.unit


def _export(export_format: str) -> str:
    return service.build_export_content(
        export_format=export_format,
        segments=[],
        speakers=[],
        comments=[],
        include_comments=False,
    )


def test_a_valid_token_without_a_builder_raises_instead_of_returning_csv(monkeypatch) -> None:
    monkeypatch.setattr(service, "VALID_FORMATS", (*service.VALID_FORMATS, "bogus"))
    with pytest.raises(AssertionError, match="bogus"):
        _export("bogus")


def test_an_unlisted_token_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="Unsupported export format"):
        _export("exe")


@pytest.mark.parametrize("export_format", service.VALID_FORMATS)
def test_every_listed_format_has_a_builder(export_format: str) -> None:
    """With no segments each builder still returns text; none falls into the raise."""
    assert isinstance(_export(export_format), str)


def test_csv_is_an_explicit_branch_that_still_works() -> None:
    assert _export("csv").startswith("Start Time,End Time,Speaker,Text")
