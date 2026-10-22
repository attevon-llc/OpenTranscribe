"""Spreadsheet-formula neutralisation for CSV exports.

A CSV cell that begins with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return is
interpreted as a formula by Excel, LibreOffice and Google Sheets. Any export that carries
text a person typed (a legal basis, a reference, a reason) must pass every string cell
through :func:`safe_cell` so opening the file cannot execute anything.

The neutralisation is a leading single quote, the convention the spreadsheet applications
themselves honour (the quote is not displayed). It is applied to *every* string cell, not
to a hand-picked list of "free text" columns, because the next column added is the one
that gets forgotten.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from collections.abc import Sequence
from typing import Any

#: First characters that make a spreadsheet read the cell as a formula or a command.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def safe_cell(value: Any) -> Any:
    """Return ``value`` with a leading quote when a spreadsheet would read it as a formula.

    Non-strings (numbers, ``None``) pass through untouched; callers stringify datetimes and
    uuids themselves, and those never start with a formula character.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def render_csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Render ``rows`` as CSV text with every cell (and the header) neutralised."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([safe_cell(h) for h in header])
    for row in rows:
        writer.writerow([safe_cell(cell) for cell in row])
    return buffer.getvalue()
