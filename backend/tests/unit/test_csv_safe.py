"""Spreadsheet-formula neutralisation (``app/utils/csv_safe.py``, issue #1254)."""

from __future__ import annotations

import csv
import io

import pytest

from app.utils.csv_safe import render_csv
from app.utils.csv_safe import safe_cell

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n")


@pytest.mark.parametrize("lead", _FORMULA_STARTS)
def test_every_formula_lead_character_is_neutralised(lead):
    value = f'{lead}HYPERLINK("http://x")'
    assert safe_cell(value) == f"'{value}"


@pytest.mark.parametrize("value", ["plain", "", "a=b", "x-y", "2026-10-21T00:00:00+00:00"])
def test_ordinary_text_is_left_alone(value):
    assert safe_cell(value) == value


@pytest.mark.parametrize("value", [None, 0, 3, True, 2.5])
def test_non_strings_pass_through(value):
    assert safe_cell(value) is value


def test_render_csv_neutralises_the_header_and_every_cell():
    text = render_csv(["=h1", "ok"], [["=1+1", "@SUM(A1)"], ["-2", "+3"], ["fine", None]])
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["'=h1", "ok"]
    assert rows[1] == ["'=1+1", "'@SUM(A1)"]
    assert rows[2] == ["'-2", "'+3"]
    assert rows[3] == ["fine", ""]
    for row in rows:
        assert not any(cell.startswith(_FORMULA_STARTS) for cell in row)
