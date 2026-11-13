"""Guard for the shared upload in test_lite_mode_mocked_providers.py: the read-only
happy-path class must use one class-scoped upload, and the classes that mutate
settings must keep their own function-scoped one."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[1] / "integration" / "test_lite_mode_mocked_providers.py"
TREE = ast.parse(SRC.read_text())


def _class(name: str) -> ast.ClassDef:
    return next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == name)


def _fixture_scope(name: str) -> str:
    fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "scope" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
    return "function"


def _test_args(cls: ast.ClassDef) -> list[set[str]]:
    return [
        {a.arg for a in n.args.args}
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
    ]


def test_happy_path_fixture_is_class_scoped() -> None:
    assert _fixture_scope("happy_path_file") == "class"
    assert _fixture_scope("uploaded_file") == "function"


def test_all_six_happy_path_tests_share_the_class_scoped_upload() -> None:
    args = _test_args(_class("TestMockedAsrHappyPath"))
    assert len(args) == 6
    assert all("happy_path_file" in a and "uploaded_file" not in a for a in args)


def test_mutating_classes_keep_their_own_upload() -> None:
    for name in ("TestMockedAsrPlusMockedLlm", "TestPostProcessingOnRealWorkers"):
        args = _test_args(_class(name))
        assert args
        assert all("uploaded_file" in a and "happy_path_file" not in a for a in args)
