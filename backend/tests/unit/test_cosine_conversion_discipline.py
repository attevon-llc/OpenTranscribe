"""Cosine-space discipline guard (issue #674, cleanup K3).

OpenSearch ``cosinesimil`` scores are ``(1 + cosine) / 2``. Converting one by hand
(``2.0 * hit["_score"] - 1.0``) is exactly the open-coded arithmetic
``app.utils.cosine_space`` exists to replace: the space a number lives in should be
stated by a function name, not inferred from the surrounding arithmetic.

The scan tokenizes every ``backend/app`` module, so comments and docstrings (which
legitimately *describe* the formula) are exempt and only executable code counts.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2] / "app"
HELPER = Path("utils") / "cosine_space.py"

# ``2.0 * <expr containing score> - 1.0`` (read) and ``(1 + <expr containing score>) / 2``
# (write); the window is bounded so a long multi-line statement cannot match by accident.
_READ = re.compile(r"\b2(?:\.0)?\s*\*[^\n;]{0,80}?-\s*1(?:\.0)?\b(?!\s*\.)")
_WRITE = re.compile(r"\(\s*1(?:\.0)?\s*\+[^\n;]{0,80}?\)\s*/\s*2(?:\.0)?\b")


def _code_lines(source: str) -> list[tuple[int, str]]:
    """Return ``(first_line_number, text)`` per logical code line, minus comments/docstrings."""
    lines: list[tuple[int, str]] = []
    current: list[str] = []
    start = 0
    kinds: list[int] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
            continue
        if tok.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
            # A statement that is only a string literal is a docstring.
            if current and not (len(kinds) == 1 and kinds[0] == tokenize.STRING):
                lines.append((start, " ".join(current)))
            current, kinds = [], []
            continue
        if not current:
            start = tok.start[0]
        current.append(tok.string)
        kinds.append(tok.type)
    return lines


def find_inline_conversions(source: str) -> list[int]:
    """Line numbers of open-coded score<->cosine conversions in executable code."""
    hits: list[int] = []
    for lineno, text in _code_lines(source):
        for pattern in (_READ, _WRITE):
            for match in pattern.finditer(text):
                if "score" in match.group(0).lower():
                    hits.append(lineno)
    return hits


def _violations() -> list[str]:
    found: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR)
        if rel == HELPER:
            continue
        for lineno in find_inline_conversions(path.read_text(encoding="utf-8")):
            found.append(f"app/{rel}:{lineno}")
    return found


def test_no_inline_cosinesimil_conversion_outside_cosine_space() -> None:
    violations = _violations()
    assert not violations, (
        "Open-coded cosinesimil conversion; call app.utils.cosine_space instead:\n  "
        + "\n  ".join(violations)
    )


def test_scanner_fires_on_a_planted_read_conversion() -> None:
    assert find_inline_conversions('x = 1\nscore = 2.0 * hit["_score"] - 1.0\n') == [2]
    assert find_inline_conversions("cos = 2.0 * float(score) - 1.0\n") == [1]


def test_scanner_fires_on_a_planted_write_conversion() -> None:
    assert find_inline_conversions("min_score = (1.0 + threshold_score) / 2.0\n") == [1]
    assert find_inline_conversions("min_score = (1 + score) / 2\n") == [1]


def test_scanner_fires_on_a_multiline_statement() -> None:
    src = 'row = {\n    "s": 2.0 * float(hit["_score"]) - 1.0,\n}\n'
    assert find_inline_conversions(src) == [1]


def test_scanner_ignores_comments_and_docstrings() -> None:
    src = (
        '"""Returns (1 + score) / 2; convert with 2.0 * hit["_score"] - 1.0."""\n'
        "# score = 2.0 * hit['_score'] - 1.0\n"
        "def f():\n"
        '    """Also 2.0 * score - 1.0."""\n'
        "    return 1  # (1 + score) / 2\n"
    )
    assert find_inline_conversions(src) == []


def test_scanner_ignores_unrelated_arithmetic() -> None:
    src = (
        "backoff = base * (2 ** (attempt - 1))\n"
        "mid = 2.0 * width - 1.0\n"
        "avg = (1 + total) / 2\n"
        "x = score - 1.0\n"
        "y = 2.0 * score\n"
    )
    assert find_inline_conversions(src) == []
