"""Plain-text table and CSV output shared by the engine benchmark scripts."""

from __future__ import annotations

import csv
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def format_cell(val: Any) -> str:
    """Render one table cell: None as an em dash, booleans as Y/N."""
    if val is None:
        return '—'
    if isinstance(val, bool):
        return 'Y' if val else 'N'
    return str(val)


def print_table(rows: Sequence[dict], columns: Sequence[str]) -> None:
    """Print rows as a left-aligned plain-text table with the given columns."""
    widths = {col: len(col) for col in columns}
    for row in rows:
        for col in columns:
            widths[col] = max(widths[col], len(format_cell(row.get(col))))

    header = '  '.join(col.ljust(widths[col]) for col in columns)
    separator = '  '.join('-' * widths[col] for col in columns)
    print(header)
    print(separator)
    for row in rows:
        print('  '.join(format_cell(row.get(col)).ljust(widths[col]) for col in columns))


def write_csv(rows: Sequence[dict], output_path: Path, columns: Sequence[str]) -> None:
    """Write rows to a CSV file, creating parent directories and ignoring extra keys."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    logger.info('CSV written to %s', output_path)
