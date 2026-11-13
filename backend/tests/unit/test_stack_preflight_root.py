"""The preflight finds the repo root from backend/tests, backend/mutants/tests and archive trees.

mutmut 3 runs pytest from a copy of the tests under ``backend/mutants/``; a fixed ``parents[2]``
resolved to ``backend/`` there, ``scripts/lib/stack_target.py`` was "missing", and every mutation
run died with ``failed to collect stats, runner returned 3``.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from tests import stack_preflight

_SOURCE = Path(stack_preflight.__file__)


def _load_copy(copy: Path):
    spec = importlib.util.spec_from_file_location("_preflight_copy", copy)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("tests_dir", ["backend/tests", "backend/mutants/tests"])
def test_stack_target_resolves_from_every_tree_layout(tmp_path: Path, tests_dir: str):
    target = tmp_path / "scripts" / "lib" / "stack_target.py"
    target.parent.mkdir(parents=True)
    target.write_text("# marker\n")
    copy_dir = tmp_path / tests_dir
    copy_dir.mkdir(parents=True)
    copy = copy_dir / "stack_preflight.py"
    shutil.copy(_SOURCE, copy)

    module = _load_copy(copy)

    assert target.resolve() == module._STACK_TARGET_PATH
    assert module._STACK_TARGET_PATH.is_file()


def test_missing_marker_is_a_loud_error_not_a_wrong_path(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="stack_target.py"):
        stack_preflight.find_repo_root(tmp_path / "nowhere" / "x.py")
