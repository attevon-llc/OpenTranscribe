"""Tests for ``scripts/guards/check_api_boundary.py`` (framework and ORM leaking across layers)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[3]
_GUARD = _REPO / "scripts" / "guards" / "check_api_boundary.py"


def _load() -> ModuleType:
    sys.path.insert(0, str(_GUARD.parent))
    spec = importlib.util.spec_from_file_location("check_api_boundary_under_test", _GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()

PROBE = (
    "from fastapi import HTTPException\n\n\ndef f():\n    raise HTTPException(status_code=400)\n"
)
ORM = "def f(db):\n    return db.query(1).all()\n"


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _run(root: Path, baseline: Path, *extra: str) -> int:
    code: int = guard.main(["--root", str(root), "--baseline", str(baseline), *extra])
    return code


def _record(root: Path, baseline: Path) -> None:
    assert _run(root, baseline, "--write-baseline", "--reason", "layering: removal planned") == 0


@pytest.mark.unit
def test_clean_tree_passes_with_empty_baseline(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/services/ok.py": "x = 1\n"})
    assert _run(root, tmp_path / "b.txt") == 0


@pytest.mark.unit
def test_planted_import_and_raise_outside_api_fail(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/services/_probe.py": PROBE})
    counts = guard.scan(root)
    assert counts == {
        "fastapi_import::backend/app/services/_probe.py": 1,
        "http_exception_raise::backend/app/services/_probe.py": 1,
    }
    assert _run(root, tmp_path / "b.txt") == 1


@pytest.mark.unit
def test_lazy_import_and_bare_raise_are_counted(tmp_path: Path) -> None:
    src = "def f():\n    import starlette.requests\n    raise HTTPException\n"
    counts = guard.count_source(src, "backend/app/services/x.py")
    assert counts["fastapi_import"] == 1
    assert counts["http_exception_raise"] == 1


@pytest.mark.unit
def test_planted_orm_call_in_api_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/api/endpoints/x.py": ORM})
    assert _run(root, tmp_path / "b.txt") == 1
    assert guard.scan(root) == {"orm_call_in_api::backend/app/api/endpoints/x.py": 1}


@pytest.mark.unit
def test_orm_call_outside_api_is_not_counted(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/services/x.py": ORM})
    assert guard.scan(root) == {}


@pytest.mark.unit
def test_http_layer_is_not_counted(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "backend/app/api/endpoints/x.py": PROBE,
            "backend/app/middleware/m.py": PROBE,
            "backend/app/main.py": PROBE,
        },
    )
    assert guard.scan(root) == {}


@pytest.mark.unit
def test_growth_fails_and_shrink_is_loosened_until_tightened(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/api/a.py": ORM + "\n" + ORM.replace("f(", "g(")})
    baseline = tmp_path / "b.txt"
    _record(root, baseline)
    assert _run(root, baseline) == 0

    (root / "backend/app/api/a.py").write_text(ORM, encoding="utf-8")
    assert _run(root, baseline) == 1  # loosened baseline
    assert _run(root, baseline, "--tighten") == 0
    assert baseline.read_text(encoding="utf-8").split("\t")[1] == "1"

    (root / "backend/app/api/a.py").write_text(ORM + ORM.replace("f(", "g("), encoding="utf-8")
    assert _run(root, baseline) == 1  # grew past baseline


@pytest.mark.unit
def test_stale_entry_fails_and_tighten_removes_it(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/services/_probe.py": PROBE})
    baseline = tmp_path / "b.txt"
    _record(root, baseline)
    (root / "backend/app/services/_probe.py").write_text("x = 1\n", encoding="utf-8")
    assert _run(root, baseline) == 1
    assert _run(root, baseline, "--tighten") == 0
    assert baseline.read_text(encoding="utf-8") == ""


@pytest.mark.unit
def test_unparseable_file_fails_closed(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/services/broken.py": "def (:\n"})
    assert _run(root, tmp_path / "b.txt") == 2


@pytest.mark.unit
def test_real_repo_passes_with_committed_baseline() -> None:
    assert guard.main(["--root", str(_REPO)]) == 0
