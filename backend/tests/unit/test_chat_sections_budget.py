"""``sections_budget`` (#991): a section COUNT derived from a character budget.

The old arithmetic was ``budget_chars // files`` -- characters per file -- clamped
against a section count, so it returned its ceiling of 3 until the scope passed
4,000 files. The docstring's promise (a 25-file turn must not fetch three sections
apiece) was false at every realistic corpus size.
"""

from __future__ import annotations

import pytest

from app.services.chat.citations import DIGEST_SNIPPET_CHARS
from app.services.chat.mapreduce import DEFAULT_MAP_BUDGET_CHARS
from app.services.chat.mapreduce import sections_budget

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (1, 3),
        (4, 3),  # the AMI corpus size the #532 measurement uses must not change
        (5, 3),
        (6, 2),
        (8, 2),
        (9, 1),
        (25, 1),  # the docstring's own example
        (500, 1),
        (5000, 1),
    ],
)
def test_sections_per_file_shrinks_as_scope_grows(files: int, expected: int) -> None:
    assert sections_budget(files) == expected


def test_twenty_five_file_scope_does_not_get_three_sections_apiece() -> None:
    assert sections_budget(25) < 3


def test_budget_is_never_below_one_or_above_three() -> None:
    for files in (-5, 0, 1, 7, 100, 10**6):
        assert 1 <= sections_budget(files) <= 3


def test_empty_scope_is_treated_as_one_file() -> None:
    assert sections_budget(0) == sections_budget(1)
    assert sections_budget(-3) == sections_budget(1)


def test_allotment_fits_the_character_budget_when_it_can() -> None:
    """The point of the function: files x sections x section size <= budget.

    One section per file is the floor and may exceed the budget, so only the
    scopes that got more than the floor are held to it.
    """
    scopes = [(files, sections_budget(files)) for files in range(1, 200)]
    above_floor = [(files, per_file) for files, per_file in scopes if per_file > 1]
    assert above_floor, "the control scopes must exist or this proves nothing"
    costs = [files * per_file * DIGEST_SNIPPET_CHARS for files, per_file in above_floor]
    assert max(costs) <= DEFAULT_MAP_BUDGET_CHARS


def test_custom_budget_scales_the_allotment() -> None:
    assert sections_budget(10, budget_chars=10 * 3 * DIGEST_SNIPPET_CHARS) == 3
    assert sections_budget(10, budget_chars=10 * 2 * DIGEST_SNIPPET_CHARS) == 2
    assert sections_budget(10, budget_chars=10 * DIGEST_SNIPPET_CHARS - 1) == 1
