"""Collection must not import torch (U2, TEST_SPEED_PLAN).

A module-level ``import torch`` in a unit/api test costs every collecting process
(each xdist worker) seconds and hundreds of MB. The repo rule is that heavy optional
deps are imported inside the function or fixture that uses them. ``pytest.importorskip``
is not an acceptable substitute: it turns a missing torch into a skip.

``tests/integration`` and ``tests/onnx`` are exempt because they are real GPU/model
modules that cannot run without torch anyway.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
GUARDED = ("unit", "api")


def _module_level_torch_imports(tree: ast.Module) -> list[int]:
    """Line numbers of torch imports executed at import time (not inside def/class-free scopes)."""
    hits: list[int] = []

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.Import):
                if any(a.name == "torch" or a.name.startswith("torch.") for a in node.names):
                    hits.append(node.lineno)
            elif isinstance(node, ast.ImportFrom):
                if (
                    node.level == 0
                    and node.module
                    and (node.module == "torch" or node.module.startswith("torch."))
                ):
                    hits.append(node.lineno)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    child = getattr(node, field, None)
                    if not child:
                        continue
                    visit(
                        [c for c in child if isinstance(c, ast.stmt)]
                        if field != "handlers"
                        else [s for h in child for s in h.body]
                    )

    visit(tree.body)
    return hits


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import torch\n", [1]),
        ("import torch.nn as nn\n", [1]),
        ("from torch import nn\n", [1]),
        ("try:\n    import torch\nexcept ImportError:\n    torch = None\n", [2]),
        ("if True:\n    from torch.utils import data\n", [2]),
        ("def f():\n    import torch\n", []),
        ("class C:\n    def m(self):\n        import torch\n", []),
        ("import torchaudio\nimport numpy\n", []),
    ],
)
def test_detector_fires_and_stays_clean(source: str, expected: list[int]) -> None:
    assert _module_level_torch_imports(ast.parse(source)) == expected


def test_no_module_level_torch_import_in_unit_or_api_tests() -> None:
    files = [p for sub in GUARDED for p in sorted((TESTS / sub).rglob("*.py"))]
    assert files, "guard scanned no files"
    offenders = {
        str(p.relative_to(TESTS)): lines
        for p in files
        if (lines := _module_level_torch_imports(ast.parse(p.read_text(encoding="utf-8"))))
    }
    assert not offenders, (
        "module-level torch import under tests/unit or tests/api; import it inside the "
        f"test or fixture that uses it: {offenders}"
    )
