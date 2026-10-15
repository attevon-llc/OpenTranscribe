"""Nothing under ``app/`` reads the Hugging Face token except the resolver.

A second reader is how a token saved in Settings gets ignored by one model loader while
the others honour it. The environment variable is declared once (``core/config.py``) and
read once (``services/huggingface_token.py``); every other site calls ``get_hf_token()``.
Prose that merely mentions the name (docstrings, messages) is fine and is not matched:
only an attribute access, a subscript or an exact-name string argument is.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"
_NAME = "HUGGINGFACE_TOKEN"
_ALLOWED = {"core/config.py", "services/huggingface_token.py"}


def _readers(source: str) -> list[int]:
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == _NAME
            or isinstance(node, ast.Constant)
            and node.value == _NAME
        ):
            hits.append(node.lineno)
    return hits


def test_detector_fires_on_every_reader_shape():
    assert _readers("settings.HUGGINGFACE_TOKEN") == [1]
    assert _readers('os.getenv("HUGGINGFACE_TOKEN")') == [1]
    assert _readers('os.environ["HUGGINGFACE_TOKEN"]') == [1]


def test_detector_ignores_prose():
    assert _readers('msg = "set HUGGINGFACE_TOKEN in .env"') == []
    assert _readers('"""HUGGINGFACE_TOKEN is documented here."""') == []


def test_only_the_resolver_reads_the_token():
    offenders = []
    scanned = 0
    for path in sorted(_APP.rglob("*.py")):
        rel = path.relative_to(_APP).as_posix()
        scanned += 1
        if rel in _ALLOWED:
            continue
        offenders += [f"{rel}:{n}" for n in _readers(path.read_text(encoding="utf-8"))]
    assert scanned > 500
    assert offenders == [], (
        f"read the token via services.huggingface_token.get_hf_token(): {offenders}"
    )


def test_the_resolver_itself_still_reads_it():
    # guards the guard: an allow-listed module that stops reading would hide a rename
    assert _readers((_APP / "core/config.py").read_text(encoding="utf-8"))
    assert _readers((_APP / "services/huggingface_token.py").read_text(encoding="utf-8"))
